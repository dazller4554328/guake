# -*- coding: utf-8; -*-
"""
Saved SSH servers ("connection manager").

This module holds the data model, the on-disk store and the ssh command
builder. It has no GTK dependency so it can be unit tested without a
display. The GTK dialog lives in :mod:`guake.serverdialog` and the menu
integration in :mod:`guake.menus`.

Servers are persisted as JSON in ``~/.config/guake/servers.json``::

    {
        "schema_version": 1,
        "servers": [
            {
                "id": "b2a4...",
                "name": "web-1",
                "host": "10.0.0.5",
                "user": "root",
                "port": 22,
                "group": "Production",
                "identity_file": "~/.ssh/id_ed25519",
                "jump_host": "",
                "options": "-o ServerAliveInterval=30",
                "command": "tmux attach || tmux",
                "use_password": false,
                "color": "#3584e4",
                "updated_at": 1759218000.0
            }
        ],
        "deleted": {
            "c7f1...": {"at": 1759219000.0, "name": "old-box"}
        },
        "groups": {
            "Production": {"color": "#e62d42", "updated_at": 1759218500.0}
        }
    }

``updated_at`` is when the entry last changed and ``deleted`` remembers
recently removed servers. Both let :mod:`guake.serversync` tell a server
edited or removed on another device from one that was never there.

``groups`` holds the colour picked for a group. Every server of the group
that has no colour of its own is shown in it (tab, server list, SFTP panel);
a group without an entry gets a colour derived from its name.

Passwords are never written to this file. When ``use_password`` is true the
password is stored in the desktop keyring (see :mod:`guake.serversecrets`)
and handed to ``sshpass`` through the environment at connection time.
"""

import json
import logging
import math
import os
import re
import shlex
import shutil
import time
import uuid

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import Tuple

from guake.tabcolors import COLOR_PATTERN
from guake.tabcolors import auto_color

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

log = logging.getLogger(__name__)

SERVERS_SCHEMA_VERSION = 1
SERVERS_FILENAME = "servers.json"
DEFAULT_SSH_PORT = 22
SSH_CONFIG_GROUP = "SSH config"
SSH_CONFIG_ID_PREFIX = "sshconfig:"
# Deleted servers are remembered this long so that a sync with a device that
# still has them offers to delete them instead of bringing them back.
TOMBSTONE_MAX_AGE = 180 * 24 * 3600
MAX_GROUP_NAME_LENGTH = 200

# sshpass exit statuses (see sshpass(1)).
SSHPASS_HOST_KEY_UNKNOWN = 6
SSHPASS_HOST_KEY_CHANGED = 7

# Shell used to run the connection wrapper. Deliberately not the user's
# login shell: the wrapper is POSIX sh and would break under fish.
WRAPPER_SHELL = "/bin/sh"


@dataclass(frozen=True)
class Server:
    """One saved SSH connection. Instances are immutable; use
    :func:`dataclasses.replace` (or :meth:`with_changes`) to derive an
    updated copy."""

    name: str
    host: str
    id: str = ""
    user: str = ""
    port: int = DEFAULT_SSH_PORT
    group: str = ""
    identity_file: str = ""
    jump_host: str = ""
    options: str = ""
    command: str = ""
    use_password: bool = False
    # Tab colour, "#rrggbb"; empty picks one automatically.
    color: str = ""
    # Seconds since the epoch of the last change; 0 for entries saved before
    # servers could be synced. Set by ServerStore, not by the editor, and
    # ignored when comparing servers.
    updated_at: float = dataclass_field(default=0.0, compare=False)

    def __post_init__(self):
        if not self.id:
            object.__setattr__(self, "id", uuid.uuid4().hex)
        if self.color and not COLOR_PATTERN.match(self.color):
            raise ValueError(f"Invalid colour for server {self.name!r}: {self.color!r}")
        object.__setattr__(self, "color", self.color.lower())
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError(f"Invalid port for server {self.name!r}: {self.port!r}")
        if not self.name.strip():
            raise ValueError("A server needs a name")
        if not self.host.strip():
            raise ValueError(f"Server {self.name!r} needs a host")
        if not _is_timestamp(self.updated_at):
            raise ValueError(f"Invalid update time for server {self.name!r}")
        self._check_fields()

    def _check_fields(self):
        """Refuse values ssh would read as options (``-oProxyCommand=...``) or
        that could smuggle terminal escape sequences, whether typed in the
        editor or coming from servers.json or a backup."""
        for field in ("name", "host", "user", "jump_host", "identity_file", "group"):
            if CONTROL_CHARS.search(getattr(self, field)):
                raise ValueError(f"The {field} of server {self.name!r} has control characters")
        for field in ("host", "user", "jump_host"):
            value = getattr(self, field)
            if value.startswith("-") or any(c.isspace() for c in value):
                raise ValueError(
                    f"The {field} of server {self.name!r} cannot start with '-' or contain spaces"
                )
        if "@" in self.user:
            raise ValueError(f"The user of server {self.name!r} cannot contain '@'")

    @property
    def target(self) -> str:
        """``user@host`` or just ``host`` when no user is set."""
        return f"{self.user}@{self.host}" if self.user else self.host

    def with_changes(self, **changes) -> "Server":
        return replace(self, **changes)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Server":
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        if "port" in known:
            known["port"] = int(known["port"])
        if "updated_at" in known:
            known["updated_at"] = _as_timestamp(known["updated_at"])
        color = known.get("color")
        if color not in (None, "") and not (isinstance(color, str) and COLOR_PATTERN.match(color)):
            log.warning("Ignoring invalid colour %r of server %r", color, known.get("name"))
            known.pop("color")
        return cls(**known)


def _is_timestamp(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _as_timestamp(value) -> float:
    if not _is_timestamp(value):
        raise ValueError(f"Invalid timestamp {value!r}")
    return float(value)


class Tombstone(NamedTuple):
    """A removed server: when, and its name (to recognise it on a device
    that saved the same server under another id)."""

    at: float
    name: str


def parse_tombstones(raw) -> Dict[str, Tombstone]:
    """Validate the ``deleted`` mapping of a servers file or a sync peer,
    dropping malformed entries."""
    if not isinstance(raw, dict):
        return {}
    tombstones = {}
    for server_id, entry in raw.items():
        try:
            name = entry.get("name", "")
            if not isinstance(server_id, str) or not server_id or len(server_id) > 128:
                raise ValueError("bad id")
            if not isinstance(name, str) or CONTROL_CHARS.search(name):
                raise ValueError("bad name")
            tombstones[server_id] = Tombstone(at=_as_timestamp(entry.get("at")), name=name)
        except (AttributeError, ValueError) as e:
            log.warning("Ignoring invalid deleted server entry %r: %s", server_id, e)
    return tombstones


def dump_tombstones(tombstones: Dict[str, Tombstone]) -> dict:
    return {sid: {"at": t.at, "name": t.name} for sid, t in sorted(tombstones.items())}


def prune_tombstones(
    tombstones: Dict[str, Tombstone], live_ids: Iterable[str], now: float
) -> Dict[str, Tombstone]:
    """Forget tombstones of servers that exist again, and old ones."""
    live = set(live_ids)
    return {
        sid: t
        for sid, t in tombstones.items()
        if sid not in live and now - t.at < TOMBSTONE_MAX_AGE
    }


class GroupColor(NamedTuple):
    """The colour picked for a group; empty means "back to automatic"."""

    color: str
    at: float = 0.0


def parse_group_colors(raw) -> Dict[str, GroupColor]:
    """Group colours from the servers file or a sync payload; malformed
    entries are dropped."""
    colors = {}
    for name, entry in raw.items() if isinstance(raw, dict) else ():
        if not isinstance(name, str) or not name.strip() or CONTROL_CHARS.search(name):
            continue
        if len(name) > MAX_GROUP_NAME_LENGTH:
            continue
        if not isinstance(entry, dict):
            continue
        color = entry.get("color", "")
        if not isinstance(color, str) or (color and not COLOR_PATTERN.match(color)):
            continue
        when = entry.get("updated_at", 0.0)
        colors[name] = GroupColor(
            color=color.lower(), at=float(when) if _is_timestamp(when) else 0.0
        )
    return colors


def dump_group_colors(colors: Dict[str, GroupColor]) -> dict:
    return {
        name: {"color": entry.color, "updated_at": entry.at}
        for name, entry in sorted(colors.items())
    }


def newest_group_colors(*sources: Dict[str, GroupColor]) -> Dict[str, GroupColor]:
    """Merge group colours, the most recently picked one winning; the
    earlier source wins a tie."""
    merged: Dict[str, GroupColor] = {}
    for source in sources:
        for name, entry in source.items():
            if name not in merged or entry.at > merged[name].at:
                merged[name] = entry
    return merged


def group_color(group: str, colors: Dict[str, GroupColor]) -> str:
    """Colour of ``group``: the one picked for it, else one derived from
    its name (the same on every device)."""
    saved = colors.get(group)
    if saved is not None and saved.color:
        return saved.color
    return auto_color(f"group:{group.strip().lower()}")


def server_color(server: Server, colors: Dict[str, GroupColor]) -> str:
    """Colour a server is shown in: its own, else its group's, else (for an
    ungrouped server) an automatic one."""
    if server.color:
        return server.color
    if server.group:
        return group_color(server.group, colors)
    return auto_color(server.id)


def sort_key(server: Server) -> Tuple[str, str]:
    return (server.group.lower(), server.name.lower())


def group_servers(servers: Iterable[Server]) -> List[Tuple[str, List[Server]]]:
    """Return ``[(group_name, [servers...]), ...]`` sorted by group then name.
    Ungrouped servers come first under the empty group name."""
    grouped = {}
    for server in sorted(servers, key=sort_key):
        grouped.setdefault(server.group, []).append(server)
    return sorted(grouped.items(), key=lambda item: (item[0] != "", item[0].lower()))


class ServersFile(NamedTuple):
    servers: List[Server]
    deleted: Dict[str, Tombstone]
    groups: Dict[str, GroupColor] = {}


def parse_server_entries(raw_servers) -> List[Server]:
    """Build servers from a list of dicts, skipping (and logging) bad ones."""
    servers = []
    for raw in raw_servers if isinstance(raw_servers, list) else []:
        try:
            servers.append(Server.from_dict(raw))
        except (AttributeError, TypeError, ValueError) as e:
            log.error("Skipping invalid server entry %r: %s", raw, e)
    return servers


def load_servers_file(path: Path) -> ServersFile:
    """Read the servers file. A missing file yields no servers. A broken
    file is logged and also yields no servers, so Guake keeps working."""
    empty = ServersFile(servers=[], deleted={}, groups={})
    path = Path(path)
    if not path.exists():
        return empty
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.error("Cannot read servers file %s: %s", path, e)
        return empty
    if not isinstance(data, dict) or "servers" not in data:
        log.error("Servers file %s has an unexpected layout", path)
        return empty
    if data.get("schema_version", 0) > SERVERS_SCHEMA_VERSION:
        log.error(
            "Servers file %s was written by a newer Guake (schema %s > %s)",
            path,
            data.get("schema_version"),
            SERVERS_SCHEMA_VERSION,
        )
        return empty
    return ServersFile(
        servers=parse_server_entries(data["servers"]),
        deleted=parse_tombstones(data.get("deleted", {})),
        groups=parse_group_colors(data.get("groups", {})),
    )


def load_servers(path: Path) -> List[Server]:
    return load_servers_file(path).servers


def save_servers(
    path: Path,
    servers: Iterable[Server],
    deleted: Optional[Dict[str, Tombstone]] = None,
    groups: Optional[Dict[str, GroupColor]] = None,
) -> None:
    """Write the servers file atomically (write to a temp file, then rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SERVERS_SCHEMA_VERSION,
        "servers": [s.to_dict() for s in sorted(servers, key=sort_key)],
        "deleted": dump_tombstones(deleted or {}),
        "groups": dump_group_colors(groups or {}),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, path)
    log.info("Saved %d server(s) to %s", len(payload["servers"]), path)


@dataclass(frozen=True)
class MergeResult:
    added: List[Server]
    updated: List[Server]
    unchanged: List[Server]


class ServerStore:
    """In-memory list of servers backed by a JSON file.

    Every mutating call returns the new list and writes it to disk; the
    stored list itself is never mutated in place.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._servers: List[Server] = []
        self._deleted: Dict[str, Tombstone] = {}
        self._groups: Dict[str, GroupColor] = {}
        self.reload()

    @property
    def servers(self) -> List[Server]:
        return list(self._servers)

    @property
    def deleted(self) -> Dict[str, Tombstone]:
        return dict(self._deleted)

    def reload(self) -> List[Server]:
        loaded = load_servers_file(self.path)
        self._servers = loaded.servers
        self._deleted = loaded.deleted
        self._groups = loaded.groups
        return self.servers

    def get(self, server_id: str) -> Optional[Server]:
        return next((s for s in self._servers if s.id == server_id), None)

    def find_by_name(self, name: str) -> Optional[Server]:
        wanted = name.strip().lower()
        return next((s for s in self._servers if s.name.lower() == wanted), None)

    def groups(self) -> List[str]:
        return sorted({s.group for s in self._servers if s.group}, key=str.lower)

    @property
    def group_colors(self) -> Dict[str, GroupColor]:
        return dict(self._groups)

    def group_color(self, group: str) -> str:
        return group_color(group, self._groups)

    def has_group_color(self, group: str) -> bool:
        """Whether a colour was picked for ``group`` (it is not automatic)."""
        return bool(self._groups.get(group, GroupColor("")).color)

    def color_for(self, server: Server) -> str:
        return server_color(server, self._groups)

    def set_group_color(self, group: str, color: str) -> None:
        """Pick the colour of ``group``; an empty colour goes back to the
        automatic one. The choice is remembered with its time so the newest
        one wins a sync."""
        if color and not COLOR_PATTERN.match(color):
            raise ValueError(f"Invalid colour for group {group!r}: {color!r}")
        if not group.strip():
            raise ValueError("A group needs a name")
        groups = {**self._groups, group: GroupColor(color=color.lower(), at=time.time())}
        self._commit(self._servers, groups=groups)

    def apply_group_colors(self, incoming: Dict[str, GroupColor]) -> Dict[str, GroupColor]:
        """Take the group colours picked more recently on another device (or
        saved in a backup), for the groups in use here. Returns the entries
        that changed. A time in the future counts as now, or that entry
        would win every later comparison."""
        now = time.time()
        live = set(self.groups())
        incoming = {
            name: entry._replace(at=min(entry.at, now))
            for name, entry in incoming.items()
            if name in live
        }
        merged = newest_group_colors(self._groups, incoming)
        changed = {name: entry for name, entry in merged.items() if self._groups.get(name) != entry}
        if changed:
            self._commit(self._servers, groups=merged)
        return changed

    def add(self, server: Server) -> List[Server]:
        return self._commit([*self._servers, server])

    def update(self, server: Server) -> List[Server]:
        if self.get(server.id) is None:
            raise KeyError(f"Unknown server id {server.id}")
        return self._commit([server if s.id == server.id else s for s in self._servers])

    def remove(self, server_id: str) -> List[Server]:
        server = self.get(server_id)
        deleted = self._deleted
        if server is not None:
            deleted = {**deleted, server_id: Tombstone(at=time.time(), name=server.name)}
        return self._commit([s for s in self._servers if s.id != server_id], deleted)

    def import_servers(self, candidates: Iterable[Server]) -> List[Server]:
        """Add every candidate whose name is not already saved. Returns the
        list of servers that were actually added."""
        existing = {s.name.lower() for s in self._servers}
        added = [c for c in candidates if c.name.lower() not in existing]
        if added:
            self._commit([*self._servers, *added])
        return added

    def merge(self, incoming: Iterable[Server]) -> "MergeResult":
        """Add or update servers from a backup. An incoming server replaces
        the saved one with the same id, or else the one with the same name
        (keeping the local id so its keyring password stays attached).
        Anything else is added. Changed entries are stamped as edited now,
        so an imported backup wins the next server sync, as a manual edit
        would."""
        current = list(self._servers)
        added, updated, unchanged = [], [], []
        for server in incoming:
            match = next((s for s in current if s.id == server.id), None) or next(
                (s for s in current if s.name.lower() == server.name.lower()), None
            )
            if match is None:
                added.append(server)
                current.append(server)
                continue
            merged = server.with_changes(id=match.id)
            if merged == match:
                unchanged.append(match)
                continue
            updated.append(merged)
            current = [merged if s.id == match.id else s for s in current]
        if added or updated:
            self._commit(current)
        return MergeResult(added=added, updated=updated, unchanged=unchanged)

    def apply_sync(self, upserts: Iterable[Server], removals: Dict[str, Tombstone]) -> List[Server]:
        """Save servers received from another device and remove the ones
        deleted there, keeping their timestamps so the devices agree on
        which copy is newest."""
        by_id = {s.id: s for s in self._servers}
        by_id.update({s.id: s for s in upserts})
        kept = [s for sid, s in by_id.items() if sid not in removals]
        return self._commit(kept, {**self._deleted, **removals}, stamp=False)

    def _stamped(self, servers: List[Server], now: float) -> List[Server]:
        """New or changed servers get ``updated_at = now``; unchanged ones
        keep the saved copy (and so its timestamp)."""
        saved = {s.id: s for s in self._servers}
        stamped = []
        for server in servers:
            previous = saved.get(server.id)
            if previous is not None and previous == server:
                stamped.append(previous)
            else:
                stamped.append(server.with_changes(updated_at=now))
        return stamped

    def _commit(
        self,
        servers: List[Server],
        deleted: Optional[Dict[str, Tombstone]] = None,
        stamp: bool = True,
        groups: Optional[Dict[str, GroupColor]] = None,
    ) -> List[Server]:
        now = time.time()
        if stamp:
            servers = self._stamped(servers, now)
        tombstones = prune_tombstones(
            self._deleted if deleted is None else deleted, (s.id for s in servers), now
        )
        groups = self._groups if groups is None else groups
        save_servers(self.path, servers, tombstones, groups)
        self._servers = sorted(servers, key=sort_key)
        self._deleted = tombstones
        self._groups = groups
        return self.servers


def _ssh_argv(server: Server, pre_options: List[str], command: str, force_tty: bool) -> List[str]:
    argv = ["ssh", *pre_options]
    if is_ssh_config_server(server):
        if force_tty:
            argv.append("-t")
        return [*argv, server.host, *([command] if command else [])]
    if server.port != DEFAULT_SSH_PORT:
        argv += ["-p", str(server.port)]
    if server.identity_file:
        argv += ["-i", os.path.expanduser(server.identity_file)]
    if server.jump_host:
        argv += ["-J", server.jump_host]
    if server.options:
        try:
            argv += shlex.split(server.options)
        except ValueError as e:
            raise ValueError(f"Invalid SSH options for server {server.name!r}: {e}") from e
    if force_tty:
        argv.append("-t")
    argv.append(server.target)
    if command:
        argv.append(command)
    return argv


def build_ssh_argv(server: Server) -> List[str]:
    """The plain ``ssh`` command line for a server (no wrapper, no sshpass).

    Entries that come straight from ``~/.ssh/config`` are launched as a bare
    ``ssh <alias>`` so that ssh applies the whole config stanza itself.
    """
    # Force a tty so interactive remote commands (tmux, htop...) work.
    return _ssh_argv(server, [], server.command, force_tty=bool(server.command))


def build_hostkey_argv(server: Server) -> List[str]:
    """An ``ssh`` command that only gets as far as the host key check.

    sshpass refuses to answer ssh's "The authenticity of host ... can't be
    established" question and exits instead, so a server with a saved
    password could never be reached the first time. This command lets ssh ask
    the user in the terminal (showing the real fingerprint and honouring
    ~/.ssh/config, jump hosts and known_hosts settings) and then stops at
    authentication, since the only method it offers is "none".
    """
    # ssh keeps the first value given for an option, so ours goes first.
    return _ssh_argv(server, ["-o", "PreferredAuthentications=none"], "true", force_tty=False)


def known_hosts_name(server: Server) -> str:
    """How ``ssh-keygen -R`` names the server in known_hosts."""
    if server.port == DEFAULT_SSH_PORT:
        return server.host
    return f"[{server.host}]:{server.port}"


def forget_host_key_command(server: Server) -> str:
    """The command that removes the server's old key from known_hosts.
    For a ~/.ssh/config alias the real name is only known to ssh, so ask it."""
    if is_ssh_config_server(server):
        alias = shlex.quote(server.host)
        return (
            f'ssh-keygen -R "$(ssh -G {alias} | awk \'$1 == "hostname" {{h=$2}} '
            f'$1 == "port" {{p=$2}} END {{print (p == 22 ? h : "[" h "]:" p)}}\')"'
        )
    return shlex.join(["ssh-keygen", "-R", known_hosts_name(server)])


CLOSED_MESSAGE = "Connection to {name} closed (exit status {status})."
RESTORED_MESSAGE = "Tab for {name} restored, not connected yet."
RECONNECT_PROMPT = "Press r then Enter to reconnect, or Enter to close this tab: "
HOSTKEY_UNKNOWN_MESSAGE = (
    "[Guake] First connection to {name}: ssh does not know this server yet. "
    "Check the fingerprint below and type yes to trust it."
)
HOSTKEY_REJECTED_MESSAGE = (
    "[Guake] The host key is not trusted, so the saved password was not sent. "
    "Press r to be asked again."
)
HOSTKEY_CHANGED_MESSAGE = (
    "[Guake] WARNING: THE HOST KEY HAS CHANGED since you last connected. Someone "
    "could be intercepting the connection, so the saved password was not sent. If "
    "the server was reinstalled, remove the old key with: {command}"
)
NO_SSHPASS_MESSAGE = (
    "[Guake] 'sshpass' is not installed, so the saved password cannot be used. "
    "Enter it manually or install sshpass."
)


@dataclass(frozen=True)
class LaunchMessages:
    """Texts the connection wrapper prints in the terminal (translated by the
    caller). ``{name}`` is the server name, ``{status}`` the ssh exit status
    and ``{command}`` the command that forgets an old host key."""

    closed: str = CLOSED_MESSAGE
    reconnect_prompt: str = RECONNECT_PROMPT
    no_sshpass: str = NO_SSHPASS_MESSAGE
    restored: str = RESTORED_MESSAGE
    hostkey_unknown: str = HOSTKEY_UNKNOWN_MESSAGE
    hostkey_rejected: str = HOSTKEY_REJECTED_MESSAGE
    hostkey_changed: str = HOSTKEY_CHANGED_MESSAGE


def _print_line(text: str, blank_before: bool = False) -> str:
    fmt = "\\n%s\\n" if blank_before else "%s\\n"
    return f"printf '{fmt}' {shlex.quote(text)}"


def _hostkey_handling(server: Server, messages: LaunchMessages) -> str:
    """Shell lines run right after ``sshpass ssh`` exits with ``$status``.

    On an unknown host key, let ssh ask the user once and try again. On a
    rejected or changed key, explain why the password was not sent."""
    forget = forget_host_key_command(server)
    return (
        f'  if [ "$status" -eq {SSHPASS_HOST_KEY_UNKNOWN} ] && [ "$hostkey_asked" -eq 0 ]; then\n'
        "    hostkey_asked=1\n"
        f"    {_print_line(messages.hostkey_unknown.format(name=server.name))}\n"
        # ssh asks on /dev/tty; its stderr only has the expected auth failure.
        f"    {shlex.join(build_hostkey_argv(server))} 2>/dev/null\n"
        "    continue\n"
        "  fi\n"
        '  case "$status" in\n'
        f"    {SSHPASS_HOST_KEY_UNKNOWN}) "
        f"{_print_line(messages.hostkey_rejected, blank_before=True)} ;;\n"
        f"    {SSHPASS_HOST_KEY_CHANGED}) "
        f"{_print_line(messages.hostkey_changed.format(command=forget), blank_before=True)} ;;\n"
        "  esac\n"
    )


def build_launch(
    server: Server,
    password: Optional[str] = None,
    messages: LaunchMessages = LaunchMessages(),
    deferred: bool = False,
) -> Tuple[List[str], List[str]]:
    """Return ``(argv, extra_env)`` to spawn in a terminal for ``server``.

    The command runs ssh inside a small POSIX sh loop so that, when the
    connection ends, the tab stays open showing why and offers to reconnect
    (like Tabby) instead of vanishing with the error message.

    With ``deferred`` (used when Guake restores its tabs) the tab first waits
    for the user to ask for the connection instead of dialling every saved
    server at startup.

    When ``password`` is given and ``sshpass`` is installed the password is
    passed through the ``SSHPASS`` environment variable, never on the command
    line where ``ps`` would show it. It stays in the wrapper shell's
    environment while the tab is open so that reconnecting works; that is
    readable by the user's own processes only, the same exposure as any
    ``sshpass -e`` invocation. sshpass will not answer ssh's question about
    an unknown host key, so the wrapper then lets ssh ask the user directly.
    """
    ssh_argv = build_ssh_argv(server)
    extra_env: List[str] = []
    notice = ""
    hostkey = ""
    if password:
        if shutil.which("sshpass"):
            ssh_argv = ["sshpass", "-e", *ssh_argv]
            extra_env.append(f"SSHPASS={password}")
            hostkey = _hostkey_handling(server, messages)
        else:
            log.warning("sshpass not found; cannot use the saved password for %s", server.name)
            notice = _print_line(messages.no_sshpass) + "\n"

    # The message becomes a printf format string: escape literal '%' and let
    # printf substitute the exit status so it is not stuck inside quotes.
    closed_format = (
        messages.closed.replace("\\", "\\\\")
        .replace("%", "%%")
        .format(name=server.name.replace("\\", "\\\\").replace("%", "%%"), status="%s")
    )
    reconnect_prompt = shlex.quote(messages.reconnect_prompt)
    gate = ""
    if deferred:
        restored = messages.restored.format(name=server.name)
        gate = (
            f"{_print_line(restored)}\n"
            f"printf '%s' {reconnect_prompt}\n"
            "read -r answer || exit 0\n"
            'case "$answer" in r|R) ;; *) exit 0 ;; esac\n'
        )
    script = (
        f"{notice}{gate}"
        "hostkey_asked=0\n"
        "while true; do\n"
        f"  {shlex.join(ssh_argv)}\n"
        "  status=$?\n"
        f"{hostkey}"
        f"  printf '\\n'{shlex.quote(closed_format)}'\\n' \"$status\"\n"
        f"  printf '%s' {reconnect_prompt}\n"
        "  read -r answer || exit $status\n"
        '  case "$answer" in r|R) hostkey_asked=0; continue ;; *) exit $status ;; esac\n'
        "done\n"
    )
    return [WRAPPER_SHELL, "-c", script], extra_env


def default_ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def parse_ssh_config(path: Optional[Path] = None) -> List[Server]:
    """Turn the ``Host`` entries of an OpenSSH client config into read-only
    :class:`Server` objects (wildcard/negated patterns are skipped). Only
    the handful of keywords Guake can map are read; everything else is left
    to ssh itself, which will still apply the config when connecting."""
    path = Path(path) if path else default_ssh_config_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        log.warning("Cannot read %s: %s", path, e)
        return []

    servers: List[Server] = []
    current: Optional[dict] = None

    def flush():
        if current and current.get("host"):
            try:
                servers.append(Server(**current))
            except ValueError as e:
                log.warning("Skipping ssh config host %s: %s", current.get("name"), e)

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace("=", " ", 1).split(None, 1)
        if len(parts) != 2:
            continue
        keyword, value = parts[0].lower(), parts[1].strip()
        if keyword == "host":
            flush()
            aliases = value.split()
            usable = [a for a in aliases if not any(c in a for c in "*?!")]
            if not usable:
                current = None
                continue
            alias = usable[0]
            current = {
                "name": alias,
                "host": alias,
                "group": SSH_CONFIG_GROUP,
                "id": f"{SSH_CONFIG_ID_PREFIX}{alias}",
            }
        elif keyword == "match":
            flush()
            current = None
        elif current is not None:
            if keyword == "user":
                current["user"] = value
            elif keyword == "port":
                try:
                    current["port"] = int(value)
                except ValueError:
                    pass
            elif keyword == "identityfile" and "identity_file" not in current:
                identity = value.strip('"')
                # ssh token paths (%d, %h...) cannot be expanded here.
                if "%" not in identity:
                    current["identity_file"] = identity
            elif keyword == "proxyjump":
                current["jump_host"] = value
    flush()
    return sorted(servers, key=sort_key)


def is_ssh_config_server(server: Server) -> bool:
    return server.id.startswith(SSH_CONFIG_ID_PREFIX)


def as_saved_server(server: Server) -> Server:
    """Copy of an ssh-config entry suitable for saving (fresh id, no group)."""
    return server.with_changes(id=uuid.uuid4().hex, group="")
