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
                "use_password": false
            }
        ]
    }

Passwords are never written to this file. When ``use_password`` is true the
password is stored in the desktop keyring (see :mod:`guake.serversecrets`)
and handed to ``sshpass`` through the environment at connection time.
"""

import json
import logging
import os
import shlex
import shutil
import uuid

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple

log = logging.getLogger(__name__)

SERVERS_SCHEMA_VERSION = 1
SERVERS_FILENAME = "servers.json"
DEFAULT_SSH_PORT = 22
SSH_CONFIG_GROUP = "SSH config"
SSH_CONFIG_ID_PREFIX = "sshconfig:"

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

    def __post_init__(self):
        if not self.id:
            object.__setattr__(self, "id", uuid.uuid4().hex)
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError(f"Invalid port for server {self.name!r}: {self.port!r}")
        if not self.name.strip():
            raise ValueError("A server needs a name")
        if not self.host.strip():
            raise ValueError(f"Server {self.name!r} needs a host")

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
        return cls(**known)


def sort_key(server: Server) -> Tuple[str, str]:
    return (server.group.lower(), server.name.lower())


def group_servers(servers: Iterable[Server]) -> List[Tuple[str, List[Server]]]:
    """Return ``[(group_name, [servers...]), ...]`` sorted by group then name.
    Ungrouped servers come first under the empty group name."""
    grouped = {}
    for server in sorted(servers, key=sort_key):
        grouped.setdefault(server.group, []).append(server)
    return sorted(grouped.items(), key=lambda item: (item[0] != "", item[0].lower()))


def load_servers(path: Path) -> List[Server]:
    """Read the servers file. A missing file yields an empty list. A broken
    file is logged and also yields an empty list, so Guake keeps working."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.error("Cannot read servers file %s: %s", path, e)
        return []
    if not isinstance(data, dict) or "servers" not in data:
        log.error("Servers file %s has an unexpected layout", path)
        return []
    if data.get("schema_version", 0) > SERVERS_SCHEMA_VERSION:
        log.error(
            "Servers file %s was written by a newer Guake (schema %s > %s)",
            path,
            data.get("schema_version"),
            SERVERS_SCHEMA_VERSION,
        )
        return []
    servers = []
    for raw in data["servers"]:
        try:
            servers.append(Server.from_dict(raw))
        except (TypeError, ValueError) as e:
            log.error("Skipping invalid server entry %r: %s", raw, e)
    return servers


def save_servers(path: Path, servers: Iterable[Server]) -> None:
    """Write the servers file atomically (write to a temp file, then rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SERVERS_SCHEMA_VERSION,
        "servers": [s.to_dict() for s in sorted(servers, key=sort_key)],
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, path)
    log.info("Saved %d server(s) to %s", len(payload["servers"]), path)


class ServerStore:
    """In-memory list of servers backed by a JSON file.

    Every mutating call returns the new list and writes it to disk; the
    stored list itself is never mutated in place.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._servers: List[Server] = load_servers(self.path)

    @property
    def servers(self) -> List[Server]:
        return list(self._servers)

    def reload(self) -> List[Server]:
        self._servers = load_servers(self.path)
        return self.servers

    def get(self, server_id: str) -> Optional[Server]:
        return next((s for s in self._servers if s.id == server_id), None)

    def find_by_name(self, name: str) -> Optional[Server]:
        wanted = name.strip().lower()
        return next((s for s in self._servers if s.name.lower() == wanted), None)

    def groups(self) -> List[str]:
        return sorted({s.group for s in self._servers if s.group}, key=str.lower)

    def add(self, server: Server) -> List[Server]:
        return self._commit([*self._servers, server])

    def update(self, server: Server) -> List[Server]:
        if self.get(server.id) is None:
            raise KeyError(f"Unknown server id {server.id}")
        return self._commit([server if s.id == server.id else s for s in self._servers])

    def remove(self, server_id: str) -> List[Server]:
        return self._commit([s for s in self._servers if s.id != server_id])

    def import_servers(self, candidates: Iterable[Server]) -> List[Server]:
        """Add every candidate whose name is not already saved. Returns the
        list of servers that were actually added."""
        existing = {s.name.lower() for s in self._servers}
        added = [c for c in candidates if c.name.lower() not in existing]
        if added:
            self._commit([*self._servers, *added])
        return added

    def _commit(self, servers: List[Server]) -> List[Server]:
        save_servers(self.path, servers)
        self._servers = sorted(servers, key=sort_key)
        return self.servers


def build_ssh_argv(server: Server) -> List[str]:
    """The plain ``ssh`` command line for a server (no wrapper, no sshpass).

    Entries that come straight from ``~/.ssh/config`` are launched as a bare
    ``ssh <alias>`` so that ssh applies the whole config stanza itself.
    """
    argv = ["ssh"]
    if is_ssh_config_server(server):
        if server.command:
            argv += ["-t", server.host, server.command]
        else:
            argv.append(server.host)
        return argv
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
    if server.command:
        # Force a tty so interactive remote commands (tmux, htop...) work.
        argv.append("-t")
    argv.append(server.target)
    if server.command:
        argv.append(server.command)
    return argv


CLOSED_MESSAGE = "Connection to {name} closed (exit status {status})."
RESTORED_MESSAGE = "Tab for {name} restored, not connected yet."
RECONNECT_PROMPT = "Press r then Enter to reconnect, or Enter to close this tab: "
NO_SSHPASS_MESSAGE = (
    "[Guake] 'sshpass' is not installed, so the saved password cannot be used. "
    "Enter it manually or install sshpass."
)


def build_launch(
    server: Server,
    password: Optional[str] = None,
    closed_message: str = CLOSED_MESSAGE,
    reconnect_prompt: str = RECONNECT_PROMPT,
    no_sshpass_message: str = NO_SSHPASS_MESSAGE,
    deferred: bool = False,
    restored_message: str = RESTORED_MESSAGE,
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
    ``sshpass -e`` invocation.
    """
    ssh_argv = build_ssh_argv(server)
    extra_env: List[str] = []
    notice = ""
    if password:
        if shutil.which("sshpass"):
            ssh_argv = ["sshpass", "-e", *ssh_argv]
            extra_env.append(f"SSHPASS={password}")
        else:
            log.warning("sshpass not found; cannot use the saved password for %s", server.name)
            notice = f"printf '%s\\n' {shlex.quote(no_sshpass_message)}\n"

    # The message becomes a printf format string: escape literal '%' and let
    # printf substitute the exit status so it is not stuck inside quotes.
    closed_format = closed_message.replace("%", "%%").format(
        name=server.name.replace("%", "%%"), status="%s"
    )
    gate = ""
    if deferred:
        restored = restored_message.format(name=server.name)
        gate = (
            f"printf '%s\\n' {shlex.quote(restored)}\n"
            f"printf '%s' {shlex.quote(reconnect_prompt)}\n"
            "read -r answer || exit 0\n"
            'case "$answer" in r|R) ;; *) exit 0 ;; esac\n'
        )
    script = (
        f"{notice}{gate}"
        "while true; do\n"
        f"  {shlex.join(ssh_argv)}\n"
        "  status=$?\n"
        f"  printf '\\n'{shlex.quote(closed_format)}'\\n' \"$status\"\n"
        f"  printf '%s' {shlex.quote(reconnect_prompt)}\n"
        "  read -r answer || exit $status\n"
        '  case "$answer" in r|R) continue ;; *) exit $status ;; esac\n'
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
