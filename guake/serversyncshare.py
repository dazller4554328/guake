# -*- coding: utf-8; -*-
"""
The sharing half of server sync: a small HTTP endpoint on this device's
Tailscale address that hands the saved servers to the user's other devices.

A request is answered only when ``tailscale whois`` says it comes from
another untagged device of the same Tailscale user. Requests from this
device's own address are refused (any local user could make one), each
address gets a small request budget, a handful of requests are served at
once, and each has a hard deadline.

The protocol, the payload and the merge rules live in :mod:`guake.serversync`.
"""

import json
import logging
import socket
import threading
import time

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Callable
from typing import Dict
from typing import Optional
from typing import Tuple

from guake.serversync import SYNC_PATH
from guake.serversync import SYNC_PORT
from guake.serversync import SyncError
from guake.serversync import TailscaleSelf
from guake.serversync import self_info
from guake.serversync import tailscale_status
from guake.serversync import whois_user_id

log = logging.getLogger(__name__)

# Whois answers are cached so a burst of requests does not fork a
# ``tailscale`` process each.
WHOIS_CACHE_SECONDS = 60
MAX_REQUESTS_PER_ADDRESS = 10
REQUEST_WINDOW_SECONDS = 10
MAX_CONCURRENT_REQUESTS = 8
# Per read, then for the whole request (a client trickling bytes).
HANDLER_TIMEOUT = 5
REQUEST_DEADLINE = 15


class Authorizer:
    """Decides whether a client address may read the servers."""

    def __init__(self, me: TailscaleSelf, whois=whois_user_id, clock=time.monotonic):
        self.me = me
        self._whois = whois
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[bool, float]] = {}
        self._windows: Dict[str, Tuple[float, int]] = {}

    def over_budget(self, ip: str) -> bool:
        now = self._clock()
        with self._lock:
            self._windows = {
                k: v for k, v in self._windows.items() if now - v[0] < REQUEST_WINDOW_SECONDS
            }
            start, count = self._windows.get(ip, (now, 0))
            self._windows[ip] = (start, count + 1)
            return count + 1 > MAX_REQUESTS_PER_ADDRESS

    def allows(self, ip: str) -> bool:
        if ip == self.me.ip:
            return False
        now = self._clock()
        with self._lock:
            cached = self._cache.get(ip)
        if cached is not None and now - cached[1] < WHOIS_CACHE_SECONDS:
            return cached[0]
        allowed = self._whois(ip) == self.me.user_id
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if now - v[1] < WHOIS_CACHE_SECONDS}
            self._cache[ip] = (allowed, now)
        return allowed


def make_handler(authorizer: Authorizer, payload: Callable[[], dict]):
    class Handler(BaseHTTPRequestHandler):
        timeout = HANDLER_TIMEOUT
        server_version = "GuakeServerSync/1"

        def setup(self):
            super().setup()
            self._deadline = threading.Timer(REQUEST_DEADLINE, self._abort)
            self._deadline.daemon = True
            self._deadline.start()

        def finish(self):
            self._deadline.cancel()
            super().finish()

        def _abort(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def do_GET(self):  # pylint: disable=invalid-name
            client = self.client_address[0]
            if authorizer.over_budget(client):
                self._reply(429, {"error": "too many requests"})
            elif self.path != SYNC_PATH:
                self._reply(404, {"error": "not found"})
            elif not authorizer.allows(client):
                log.warning("Refused server sync request from %s", client)
                self._reply(403, {"error": "forbidden"})
            else:
                log.info("Sharing servers with %s", client)
                self._reply(200, payload())

        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # pylint: disable=redefined-builtin
            log.debug("sync server: " + format, *args)

    return Handler


class BoundedHTTPServer(ThreadingHTTPServer):
    """Drops connections beyond ``MAX_CONCURRENT_REQUESTS`` in flight."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class SyncSharing:
    """Serves this device's servers to its sibling devices while wanted.

    The GTK side calls :meth:`set_wanted` when the setting changes and runs
    :meth:`reconcile` on a worker thread (it calls ``tailscale`` and may
    block for seconds). ``reconcile`` also restarts the endpoint when the
    Tailscale address or user changed. ``payload`` is called on a worker
    thread, so it should read from disk rather than from GTK state."""

    def __init__(self, payload: Callable[[], dict], port: int = SYNC_PORT):
        self._payload = payload
        self.port = port
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._serving_as: Optional[TailscaleSelf] = None
        self._wanted = False

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def wanted(self) -> bool:
        return self._wanted

    def set_wanted(self, wanted: bool) -> None:
        self._wanted = wanted

    def reconcile(self) -> None:
        with self._lock:
            if not self._wanted:
                self._stop_locked()
                return
            try:
                me = self_info(tailscale_status())
            except SyncError as e:
                log.info("Server sharing unavailable: %s", e)
                self._stop_locked()
                return
            if self._server is not None and me == self._serving_as:
                return
            self._stop_locked()
            self._start_locked(me)
            # Turned off while tailscale was answering.
            if not self._wanted:
                self._stop_locked()

    def stop(self) -> None:
        self.set_wanted(False)
        with self._lock:
            self._stop_locked()

    def _start_locked(self, me: TailscaleSelf) -> None:
        try:
            httpd = BoundedHTTPServer(
                (me.ip, self.port), make_handler(Authorizer(me), self._payload)
            )
        except OSError as e:
            log.warning("Cannot share servers on %s:%d: %s", me.ip, self.port, e)
            return
        threading.Thread(target=httpd.serve_forever, name="guake-server-sync", daemon=True).start()
        self._server, self._serving_as = httpd, me
        log.info("Sharing servers on %s:%d", me.ip, self.port)

    def _stop_locked(self) -> None:
        httpd, self._server, self._serving_as = self._server, None, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            log.info("Stopped sharing servers")
