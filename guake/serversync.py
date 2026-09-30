# -*- coding: utf-8; -*-
"""
Manual sync of the saved servers between your own devices over Tailscale.

A Guake with sharing turned on answers ``GET /guake/servers/v1`` on its
Tailscale address, only to devices signed in as the same Tailscale user
(see :mod:`guake.serversyncshare`). The answer holds the server list and
the tombstones of deleted servers; never passwords or key files.

Syncing is a pull: the device where you press Sync fetches every other
device's list, :func:`plan_sync` turns the differences into proposed
changes, and nothing is written until you accept them in the sync dialog
(:mod:`guake.serversyncdialogs`). Deletions and changes that deserve a
second look (ssh options, remote commands, a saved password going to a
new destination) are never pre-selected and must be confirmed.

Known limitation: servers matched by name but saved under different ids
(two separate backup imports) keep their ids, so renaming one on a single
device makes the other devices see a new server.

This module has no GTK dependency so it can be unit tested headless.
"""

import json
import logging
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import replace
from typing import Dict
from typing import List
from typing import NamedTuple
from typing import Optional

from guake.servers import CONTROL_CHARS
from guake.servers import SERVERS_SCHEMA_VERSION
from guake.servers import Server
from guake.servers import ServersFile
from guake.servers import Tombstone
from guake.servers import dump_tombstones
from guake.servers import is_ssh_config_server
from guake.servers import parse_server_entries
from guake.servers import parse_tombstones

log = logging.getLogger(__name__)

# GSettings key (guake.general) turning sharing on.
SHARE_SETTING = "servers-sync-share"
SYNC_PORT = 47655
SYNC_PATH = "/guake/servers/v1"
FETCH_TIMEOUT = 5
TAILSCALE_TIMEOUT = 5
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_SYNCED_SERVERS = 5000
MAX_PARALLEL_FETCHES = 8

ADD, UPDATE, DELETE = "add", "update", "delete"

# ssh settings that make ssh run a local program or load local code. A
# server carrying one is refused when it comes from another device.
RISKY_SSH_KEYWORDS = frozenset(
    {
        "proxycommand",
        "localcommand",
        "permitlocalcommand",
        "knownhostscommand",
        "include",
        "proxyusefdpass",
        "pkcs11provider",
        "securitykeyprovider",
    }
)
# Flags taking a local file with the same effect (-F config, -I pkcs11
# library, -E log file to write).
RISKY_SSH_FLAGS = frozenset("FIE")
# ssh flags that take an argument, so the rest of their token is a value.
SSH_FLAGS_WITH_VALUE = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
# Fields whose change is flagged for review in the sync dialog.
COMMAND_FIELDS = ("options", "command", "jump_host")
DESTINATION_FIELDS = ("host", "port", "user", "jump_host", "options")


class SyncError(Exception):
    """Tailscale is missing, stopped or logged out, or a device answered
    badly; the message says which."""


# -- Tailscale -----------------------------------------------------------------


class Device(NamedTuple):
    name: str
    ip: str
    online: bool


class TailscaleSelf(NamedTuple):
    name: str
    ip: str
    user_id: int


def _run_tailscale(command: str, *args) -> dict:
    """Run ``tailscale <command> --json <args>``. The flag must come before
    the positional arguments: the CLI stops parsing flags at the first one."""
    executable = shutil.which("tailscale")
    if executable is None:
        raise SyncError(_("Tailscale is not installed."))
    try:
        result = subprocess.run(
            [executable, command, "--json", *args],
            capture_output=True,
            text=True,
            timeout=TAILSCALE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SyncError(_("Cannot run tailscale: {error}").format(error=e)) from e
    if result.returncode != 0:
        raise SyncError(
            result.stderr.strip() or _("tailscale {command} failed").format(command=command)
        )
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        raise SyncError(_("Unexpected answer from tailscale")) from e
    if not isinstance(data, dict):
        raise SyncError(_("Unexpected answer from tailscale"))
    return data


def _ipv4(node: dict) -> str:
    return next((ip for ip in node.get("TailscaleIPs") or [] if ":" not in ip), "")


def _node_name(node: dict) -> str:
    dns = (node.get("DNSName") or "").split(".")[0]
    return dns or node.get("HostName") or _ipv4(node)


def tailscale_status() -> dict:
    status = _run_tailscale("status")
    if status.get("BackendState") not in (None, "Running"):
        raise SyncError(
            _("Tailscale is not connected ({state}).").format(state=status["BackendState"])
        )
    return status


def self_info(status: dict) -> TailscaleSelf:
    node = status.get("Self") or {}
    ip = _ipv4(node)
    if not ip or "UserID" not in node:
        raise SyncError(_("Tailscale is not connected."))
    # Tagged devices share one pseudo user, so "same user" would mean
    # every tagged device of the tailnet.
    if node.get("Tags"):
        raise SyncError(_("This is a tagged Tailscale device; sync needs one signed in as you."))
    return TailscaleSelf(name=_node_name(node), ip=ip, user_id=node["UserID"])


def own_devices(status: dict) -> List[Device]:
    """The other (untagged) devices of the Tailscale user this device is
    signed in as."""
    me = self_info(status)
    devices = []
    for node in (status.get("Peer") or {}).values():
        ip = _ipv4(node)
        if node.get("UserID") == me.user_id and not node.get("Tags") and ip:
            devices.append(Device(name=_node_name(node), ip=ip, online=bool(node.get("Online"))))
    return sorted(devices, key=lambda d: (not d.online, d.name.lower()))


def whois_user_id(ip: str) -> Optional[int]:
    """The Tailscale user owning the device at ``ip``; None for unknown and
    tagged devices."""
    try:
        data = _run_tailscale("whois", ip)
    except SyncError as e:
        log.info("tailscale whois %s: %s", ip, e)
        return None
    if (data.get("Node") or {}).get("Tags"):
        return None
    return (data.get("UserProfile") or {}).get("ID")


# -- payload -------------------------------------------------------------------


def build_payload(servers_file: ServersFile) -> dict:
    """What a device shares: its own servers (not the ~/.ssh/config ones,
    each device reads its own) and its tombstones."""
    return {
        "schema_version": SERVERS_SCHEMA_VERSION,
        "servers": [s.to_dict() for s in servers_file.servers if not is_ssh_config_server(s)],
        "deleted": dump_tombstones(servers_file.deleted),
    }


def risky_ssh_options(options: str) -> List[str]:
    """The settings in an ``options`` string that let ssh run local code,
    e.g. ``["ProxyCommand"]`` for ``-o ProxyCommand=...``."""
    try:
        tokens = shlex.split(options)
    except ValueError:
        return [options]
    found = []
    expect_option = False
    for token in tokens:
        if expect_option:
            expect_option = False
            keyword = token.replace("=", " ").split()[0] if token.strip() else ""
            if keyword.lower() in RISKY_SSH_KEYWORDS:
                found.append(keyword)
            continue
        if not token.startswith("-") or token.startswith("--"):
            continue
        for position, flag in enumerate(token[1:], start=1):
            if flag in RISKY_SSH_FLAGS:
                found.append("-" + flag)
            if flag == "o":
                value_start = position + 1
                rest = token[value_start:]
                if rest:
                    keyword = rest.replace("=", " ").split()[0]
                    if keyword.lower() in RISKY_SSH_KEYWORDS:
                        found.append(keyword)
                else:
                    expect_option = True
            if flag in SSH_FLAGS_WITH_VALUE:
                break
    return found


def unsafe_reason(server: Server) -> str:
    """Why a server received from another device is refused, or ``""``."""
    risky = risky_ssh_options(server.options)
    if risky:
        return _("SSH options that run local programs: {options}").format(options=", ".join(risky))
    if CONTROL_CHARS.search(server.options) or CONTROL_CHARS.search(server.command):
        return _("control characters in the SSH options or command")
    return ""


class Snapshot(NamedTuple):
    """A device's shared list, and how many of its servers were refused."""

    shared: ServersFile
    refused: int


def _clamped(timestamp: float, now: float) -> float:
    # A device whose clock (or data) is in the future would otherwise win
    # every comparison, forever, and pass that on to the others.
    return min(timestamp, now)


def parse_payload(data, now: Optional[float] = None) -> Snapshot:
    if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
        raise SyncError(_("Unexpected answer"))
    if data.get("schema_version", 0) > SERVERS_SCHEMA_VERSION:
        raise SyncError(_("That device runs a newer Guake; update this one first."))
    if len(data["servers"]) > MAX_SYNCED_SERVERS:
        raise SyncError(_("too many servers"))
    now = time.time() if now is None else now
    servers, refused = [], 0
    for server in parse_server_entries(data["servers"]):
        if is_ssh_config_server(server):
            continue
        reason = unsafe_reason(server)
        if reason:
            log.warning("Refusing synced server %r: %s", server.name, reason)
            refused += 1
            continue
        servers.append(server.with_changes(updated_at=_clamped(server.updated_at, now)))
    deleted = {
        sid: t._replace(at=_clamped(t.at, now))
        for sid, t in parse_tombstones(data.get("deleted", {})).items()
    }
    return Snapshot(ServersFile(servers=servers, deleted=deleted), refused)


# -- planning ------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One proposed change to the local servers.

    ``server`` is what gets saved (ADD / UPDATE) or the local server that
    gets removed (DELETE); ``previous`` is the local copy an UPDATE
    replaces. ``when`` is the time of the change on ``device``.
    ``recommended`` changes are pre-selected in the dialog; ``warning``
    says why a change needs a closer look (and a confirmation)."""

    kind: str
    server: Server
    device: str
    when: float
    previous: Optional[Server] = None
    recommended: bool = True
    warning: str = ""


def _find_local(local: List[Server], server_id: str, name: str) -> Optional[Server]:
    wanted = name.lower()
    return next((s for s in local if s.id == server_id), None) or next(
        (s for s in local if wanted and s.name.lower() == wanted), None
    )


def _keep_newest(changes: Dict[str, Change], key: str, change: Change) -> None:
    current = changes.get(key)
    if current is None or change.when > current.when:
        changes[key] = change


def review_warning(kind: str, server: Server, previous: Optional[Server]) -> str:
    """Why an incoming ADD or UPDATE should not be applied blindly."""
    if kind == ADD:
        if any(getattr(server, f) for f in COMMAND_FIELDS):
            return _("Runs with SSH options, a jump host or a remote command: check them.")
        return ""
    changed = {f for f in DESTINATION_FIELDS if getattr(server, f) != getattr(previous, f)}
    if previous.use_password and changed:
        return _("The saved password would be sent to the changed destination.")
    if any(getattr(server, f) != getattr(previous, f) for f in COMMAND_FIELDS):
        return _("Changes SSH options, the jump host or the remote command: check them.")
    return ""


def _merged_tombstones(local_deleted, peers):
    """Newest tombstone per id and per lower-cased name, local and remote."""
    by_id: Dict[str, Tombstone] = {}
    by_name: Dict[str, Tombstone] = {}
    all_tombstones = [local_deleted, *(p.deleted for p in peers.values())]
    for tombstones in all_tombstones:
        for sid, tomb in tombstones.items():
            for mapping, key in ((by_id, sid), (by_name, tomb.name.lower())):
                if key and (key not in mapping or tomb.at > mapping[key].at):
                    mapping[key] = tomb
    return by_id, by_name


def _deleted_since(server: Server, by_id, by_name) -> bool:
    tombs = [t for t in (by_id.get(server.id), by_name.get(server.name.lower())) if t]
    return any(t.at >= server.updated_at for t in tombs)


def _plan_additions(local, peers, by_id, by_name) -> List[Change]:
    """Servers no local entry matches, newest copy per id, then per name."""
    per_id: Dict[str, Change] = {}
    for device, snapshot in peers.items():
        for incoming in snapshot.servers:
            if _find_local(local, incoming.id, incoming.name) is not None:
                continue
            if _deleted_since(incoming, by_id, by_name):
                continue
            _keep_newest(per_id, incoming.id, Change(ADD, incoming, device, incoming.updated_at))
    per_name: Dict[str, Change] = {}
    for change in per_id.values():
        _keep_newest(per_name, change.server.name.lower(), change)
    return list(per_name.values())


def plan_sync(
    local: List[Server], local_deleted: Dict[str, Tombstone], peers: Dict[str, ServersFile]
) -> List[Change]:
    """Changes that bring the local list up to date with ``peers`` (device
    name -> its shared list). The most recent event per server wins; a
    local server newer than the peer's copy is left alone (that device
    picks it up when it syncs). Deletions and changes with a warning are
    not recommended: the user has to tick and confirm them."""
    by_id, by_name = _merged_tombstones(local_deleted, peers)
    changes: Dict[str, Change] = {}
    for device, snapshot in peers.items():
        for incoming in snapshot.servers:
            match = _find_local(local, incoming.id, incoming.name)
            if match is None:
                continue
            merged = incoming.with_changes(id=match.id)
            if merged == match or incoming.updated_at < match.updated_at:
                continue
            _keep_newest(
                changes, match.id, Change(UPDATE, merged, device, incoming.updated_at, match)
            )
        for sid, tomb in snapshot.deleted.items():
            match = _find_local(local, sid, tomb.name)
            if match is not None and tomb.at > match.updated_at:
                _keep_newest(changes, match.id, Change(DELETE, match, device, tomb.at, match))

    planned = [*_plan_additions(local, peers, by_id, by_name), *changes.values()]
    order = {ADD: 0, UPDATE: 1, DELETE: 2}
    return sorted(
        (_with_review(c) for c in planned),
        key=lambda c: (order[c.kind], c.server.name.lower()),
    )


def _with_review(change: Change) -> Change:
    if change.kind == DELETE:
        return replace(change, recommended=False)
    warning = review_warning(change.kind, change.server, change.previous)
    # Same timestamp but different settings: both copies were edited
    # before sync existed. Offer it, but let the user decide.
    stale = change.kind == UPDATE and change.when <= change.previous.updated_at
    return replace(change, recommended=not (warning or stale), warning=warning)


def needs_confirmation(change: Change) -> bool:
    return change.kind == DELETE or bool(change.warning)


def apply_changes(store, changes: List[Change]) -> None:
    """Write the accepted ``changes`` to ``store`` (a ServerStore)."""
    upserts = [c.server for c in changes if c.kind in (ADD, UPDATE)]
    removals = {
        c.server.id: Tombstone(at=c.when, name=c.server.name) for c in changes if c.kind == DELETE
    }
    store.apply_sync(upserts, removals)


# -- client --------------------------------------------------------------------


class PeerResult(NamedTuple):
    device: Device
    snapshot: Optional[Snapshot]
    error: str


class Collected(NamedTuple):
    me: TailscaleSelf
    results: List[PeerResult]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


# No proxy (the request must go straight through the Tailscale interface)
# and no redirects (a device must not point us somewhere else).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def fetch_peer(ip: str, port: int = SYNC_PORT, timeout: float = FETCH_TIMEOUT) -> Snapshot:
    url = f"http://{ip}:{port}{SYNC_PATH}"
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            body = response.read(MAX_PAYLOAD_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise SyncError(_("refused: not signed in to the same Tailscale account")) from e
        if e.code == 429:
            raise SyncError(_("busy, try again in a moment")) from e
        raise SyncError(_("error {code}").format(code=e.code)) from e
    except (OSError, ValueError) as e:
        raise SyncError(_("Guake is not running there, or sharing is turned off")) from e
    if len(body) > MAX_PAYLOAD_BYTES:
        raise SyncError(_("answer too large"))
    try:
        return parse_payload(json.loads(body.decode("utf-8")))
    except ValueError as e:
        raise SyncError(_("Unexpected answer")) from e


def _fetch_result(device: Device, port: int) -> PeerResult:
    if not device.online:
        return PeerResult(device, None, _("offline"))
    try:
        return PeerResult(device, fetch_peer(device.ip, port), "")
    except SyncError as e:
        return PeerResult(device, None, str(e))


def collect(port: int = SYNC_PORT) -> Collected:
    """Fetch the server list of every other device of this Tailscale user.
    Blocking; run it off the GTK main loop. Raises SyncError."""
    status = tailscale_status()
    devices = own_devices(status)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FETCHES) as pool:
        results = list(pool.map(lambda d: _fetch_result(d, port), devices))
    return Collected(me=self_info(status), results=results)
