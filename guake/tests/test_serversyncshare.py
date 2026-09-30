# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

import socket
import threading

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest

from guake import serversync as sync
from guake import serversyncshare as share
from guake.servers import Server
from guake.servers import ServersFile

ME = 111
LOCAL = sync.TailscaleSelf("me", "127.0.0.1", ME)
REMOTE_ME = sync.TailscaleSelf("me", "100.1.1.1", ME)


def payload():
    return sync.build_payload(ServersFile([Server(name="web", host="web.lan", updated_at=5)], {}))


# --- authorisation ----------------------------------------------------------------


def test_authorizer_accepts_only_our_user_and_caches():
    calls = []

    def whois(ip):
        calls.append(ip)
        return {"100.1.1.2": ME}.get(ip, 999)

    auth = share.Authorizer(REMOTE_ME, whois=whois, clock=lambda: 0.0)
    assert auth.allows("100.1.1.2")
    assert auth.allows("100.1.1.2")
    assert not auth.allows("100.1.1.4")
    assert calls == ["100.1.1.2", "100.1.1.4"]


def test_authorizer_refuses_this_devices_own_address():
    auth = share.Authorizer(REMOTE_ME, whois=lambda ip: ME)
    assert not auth.allows(REMOTE_ME.ip)


def test_request_budget_is_per_address():
    now = [0.0]
    auth = share.Authorizer(REMOTE_ME, whois=lambda ip: ME, clock=lambda: now[0])
    assert not any(auth.over_budget("a") for _ in range(share.MAX_REQUESTS_PER_ADDRESS))
    assert auth.over_budget("a")
    assert not auth.over_budget("b")
    now[0] = share.REQUEST_WINDOW_SECONDS
    assert not auth.over_budget("a")


# --- HTTP -------------------------------------------------------------------------


@pytest.fixture
def http_server():
    started = []

    def start(handler):
        httpd = share.BoundedHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1]

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def sharing_handler(allowed_user):
    # The test client connects from 127.0.0.1, so "me" must be elsewhere.
    return share.make_handler(share.Authorizer(REMOTE_ME, whois=lambda ip: allowed_user), payload)


def test_fetch_from_authorised_device(http_server):
    port = http_server(sharing_handler(ME))
    assert [s.name for s in sync.fetch_peer("127.0.0.1", port).shared.servers] == ["web"]


def test_fetch_refused_for_other_user(http_server):
    port = http_server(sharing_handler(999))
    with pytest.raises(sync.SyncError, match="same Tailscale account"):
        sync.fetch_peer("127.0.0.1", port)


def test_fetch_from_device_not_sharing():
    with pytest.raises(sync.SyncError, match="sharing is turned off"):
        sync.fetch_peer("127.0.0.1", 1, timeout=1)


def test_fetch_does_not_follow_redirects(http_server):
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):  # pylint: disable=invalid-name
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    port = http_server(Redirect)
    with pytest.raises(sync.SyncError, match="302"):
        sync.fetch_peer("127.0.0.1", port)


def test_collect_reports_each_device(mocker, http_server):
    port = http_server(sharing_handler(ME))
    status = {
        "BackendState": "Running",
        "Self": {"HostName": "me", "TailscaleIPs": ["100.1.1.1"], "UserID": ME},
        "Peer": {
            "a": {
                "HostName": "laptop",
                "TailscaleIPs": ["127.0.0.1"],
                "UserID": ME,
                "Online": True,
            },
            "b": {"HostName": "old", "TailscaleIPs": ["100.1.1.3"], "UserID": ME, "Online": False},
        },
    }
    mocker.patch("guake.serversync.tailscale_status", return_value=status)
    collected = sync.collect(port)
    assert collected.me.name == "me"
    assert [(r.device.name, bool(r.snapshot), r.error) for r in collected.results] == [
        ("laptop", True, ""),
        ("old", False, "offline"),
    ]


def test_connections_beyond_the_limit_are_dropped(mocker):
    mocker.patch.object(share, "MAX_CONCURRENT_REQUESTS", 1)
    release = threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):  # pylint: disable=invalid-name
            release.wait(5)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = share.BoundedHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        busy = socket.create_connection(("127.0.0.1", port))
        busy.sendall(b"GET / HTTP/1.0\r\n\r\n")
        extra = socket.create_connection(("127.0.0.1", port))
        extra.settimeout(3)
        assert extra.recv(1) == b""  # closed without an answer
        release.set()
        busy.close()
        extra.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- lifecycle --------------------------------------------------------------------


@pytest.fixture
def tailscale(mocker):
    """Pretend Tailscale runs with this device on 127.0.0.1."""
    me = mocker.patch("guake.serversyncshare.self_info", return_value=LOCAL)
    mocker.patch("guake.serversyncshare.tailscale_status", return_value={})
    return me


@pytest.fixture
def sharing():
    sharing = share.SyncSharing(payload, port=0)
    yield sharing
    sharing.stop()


def test_sharing_starts_only_when_wanted(tailscale, sharing):
    sharing.reconcile()
    assert not sharing.running
    sharing.set_wanted(True)
    sharing.reconcile()
    assert sharing.running


def test_sharing_stops_when_no_longer_wanted(tailscale, sharing):
    sharing.set_wanted(True)
    sharing.reconcile()
    sharing.set_wanted(False)
    sharing.reconcile()
    assert not sharing.running


def test_sharing_restarts_when_tailscale_user_changes(tailscale, sharing):
    sharing.set_wanted(True)
    sharing.reconcile()
    first = sharing._server  # pylint: disable=protected-access
    sharing.reconcile()
    assert sharing._server is first  # pylint: disable=protected-access
    tailscale.return_value = LOCAL._replace(user_id=222)
    sharing.reconcile()
    assert sharing.running and sharing._server is not first  # pylint: disable=protected-access


def test_sharing_stops_when_tailscale_goes_away(tailscale, sharing):
    sharing.set_wanted(True)
    sharing.reconcile()
    tailscale.side_effect = sync.SyncError("logged out")
    sharing.reconcile()
    assert not sharing.running


def test_turning_off_during_start_does_not_leave_it_running(tailscale, sharing):
    def turned_off_meanwhile(status):
        sharing.set_wanted(False)
        return LOCAL

    tailscale.side_effect = turned_off_meanwhile
    sharing.set_wanted(True)
    sharing.reconcile()
    assert not sharing.running


def test_real_server_is_not_a_plain_threading_server():
    assert issubclass(share.BoundedHTTPServer, ThreadingHTTPServer)
