# -*- coding: utf-8; -*-
"""
Backup files for saved servers, to move them to another machine.

A backup is a JSON file::

    {
        "format": "guake-servers-backup",
        "version": 1,
        "exported_at": "2026-09-29T12:00:00+00:00",
        "servers": [ ...same entries as servers.json... ],
        "secrets": {                      # optional
            "kdf": "scrypt", "n": 32768, "r": 8, "p": 1,
            "cipher": "aes-256-gcm",
            "salt": "<base64>", "nonce": "<base64>", "data": "<base64>"
        }
    }

``secrets`` holds the keyring passwords and the private key files of the
servers, encrypted with a key derived from a passphrase the user chooses.
Nothing secret is ever written in clear text; without python3-cryptography
only the server list can be exported. A plain ``servers.json`` can be
imported as well.

No GTK here, so it can be unit tested without a display. The dialogs live in
:mod:`guake.serverdialog`.
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
import uuid

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple

from guake.servers import SERVERS_SCHEMA_VERSION
from guake.servers import SSH_CONFIG_ID_PREFIX
from guake.servers import GroupColor
from guake.servers import Server
from guake.servers import dump_group_colors
from guake.servers import parse_group_colors

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
except ImportError:  # pragma: no cover - depends on the host system
    AESGCM = None

log = logging.getLogger(__name__)

BACKUP_FORMAT = "guake-servers-backup"
BACKUP_VERSION = 1
BACKUP_SUFFIX = ".guake-servers.json"

SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
KDF_NAME = "scrypt"
CIPHER_NAME = "aes-256-gcm"
# Limits on what a (possibly hostile) backup may ask the key derivation for.
SCRYPT_N_RANGE = (2**14, 2**20)
SCRYPT_MAX_R = 8
SCRYPT_MAX_P = 4
SCRYPT_MAX_MEMORY = 256 * 1024 * 1024

MAX_BACKUP_BYTES = 10 * 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
PRIVATE_KEY_HEADER = re.compile(rb"^\s*-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
# Only file names like id_ed25519 may be restored where the server expects them.
KEY_FILE_NAME = re.compile(r"^id_[A-Za-z0-9_.-]+$")

SSH_DIR = ".ssh"
IMPORTED_KEYS_DIR = "guake-imported"


class BackupError(Exception):
    """The file is not a backup Guake can read."""


class WrongPassphrase(BackupError):
    """The passphrase does not decrypt the backup's secrets."""


@dataclass(frozen=True)
class Secrets:
    passwords: Dict[str, str] = field(default_factory=dict)  # server id -> password
    keys: Dict[str, bytes] = field(default_factory=dict)  # identity_file -> content

    def is_empty(self) -> bool:
        return not self.passwords and not self.keys


@dataclass(frozen=True)
class Backup:
    servers: List[Server]
    encrypted_secrets: Optional[dict] = None
    groups: Dict[str, GroupColor] = field(default_factory=dict)

    @property
    def has_secrets(self) -> bool:
        return self.encrypted_secrets is not None


def is_encryption_available() -> bool:
    return AESGCM is not None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    normalized = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    return Scrypt(salt=salt, length=KEY_BYTES, n=n, r=r, p=p).derive(normalized)


def _associated_data(n: int, r: int, p: int) -> bytes:
    """Authenticate the parameters along with the ciphertext, so they
    cannot be weakened in the file without the decryption failing."""
    header = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION, "kdf": KDF_NAME}
    header.update(n=n, r=r, p=p, cipher=CIPHER_NAME)
    return json.dumps(header, sort_keys=True).encode("ascii")


def looks_like_private_key(content: bytes) -> bool:
    return len(content) <= MAX_KEY_BYTES and bool(PRIVATE_KEY_HEADER.match(content))


def _encrypt(secrets: Secrets, passphrase: str) -> dict:
    plain = json.dumps(
        {
            "passwords": secrets.passwords,
            "keys": {path: _b64(content) for path, content in secrets.keys.items()},
        }
    ).encode("utf-8")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = _derive_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return {
        "kdf": KDF_NAME,
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "cipher": CIPHER_NAME,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "data": _b64(
            AESGCM(key).encrypt(nonce, plain, _associated_data(SCRYPT_N, SCRYPT_R, SCRYPT_P))
        ),
    }


def build_backup(
    servers: Iterable[Server],
    secrets: Optional[Secrets] = None,
    passphrase: str = "",
    groups: Optional[Dict[str, GroupColor]] = None,
) -> dict:
    """The backup document for ``servers``; ``secrets`` are encrypted with
    ``passphrase`` (required when there are secrets to include)."""
    data = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "servers": [s.to_dict() for s in servers],
        "groups": dump_group_colors(groups or {}),
    }
    if secrets is not None and not secrets.is_empty():
        if not passphrase:
            raise ValueError("A passphrase is needed to export passwords and keys")
        if not is_encryption_available():
            raise ValueError("python3-cryptography is needed to export passwords and keys")
        data["secrets"] = _encrypt(secrets, passphrase)
    return data


def write_backup(path: Path, data: dict) -> None:
    """Write ``data`` atomically to ``path``, readable by the user only."""
    path = Path(path)
    # mkstemp: a fresh, unpredictable, 0600 file (never an existing symlink).
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".guake-backup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    log.info("Exported %d server(s) to %s", len(data["servers"]), path)


def _parse_servers(raw_servers) -> List[Server]:
    if not isinstance(raw_servers, list):
        raise BackupError("The file has no list of servers.")
    servers = []
    for raw in raw_servers:
        try:
            servers.append(Server.from_dict(raw))
        except (TypeError, ValueError, AttributeError) as e:
            log.warning("Skipping invalid server entry %r in backup: %s", raw, e)
    return servers


def load_backup(path: Path) -> Backup:
    """Read a backup (or a plain ``servers.json``). Raises BackupError."""
    try:
        if Path(path).stat().st_size > MAX_BACKUP_BYTES:
            raise BackupError("The file is too large to be a Guake servers backup.")
        with Path(path).open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise BackupError(f"Cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise BackupError("This is not a Guake servers backup.")
    if data.get("format") == BACKUP_FORMAT:
        version = data.get("version", 0)
    elif isinstance(data.get("schema_version"), int) and "servers" in data:
        # A servers.json copied from another machine.
        version = BACKUP_VERSION if data["schema_version"] <= SERVERS_SCHEMA_VERSION else 99
    else:
        raise BackupError("This is not a Guake servers backup.")
    if not isinstance(version, int) or version > BACKUP_VERSION:
        raise BackupError("This backup was made by a newer Guake. Please update Guake first.")
    secrets = data.get("secrets")
    if secrets is not None and not isinstance(secrets, dict):
        raise BackupError("The encrypted part of the backup is damaged.")
    return Backup(
        servers=_parse_servers(data.get("servers")),
        encrypted_secrets=secrets,
        groups=parse_group_colors(data.get("groups", {})),
    )


def _kdf_parameters(enc: dict):
    """``(salt, nonce, n, r, p)`` from the file, refusing anything outside
    what Guake writes (a hostile file could ask for gigabytes of memory)."""
    try:
        if enc.get("kdf") != KDF_NAME or enc.get("cipher") != CIPHER_NAME:
            raise ValueError("unknown encryption method")
        n, r, p = (enc[k] for k in ("n", "r", "p"))
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (n, r, p)):
            raise ValueError("invalid key derivation parameters")
        salt, nonce = _unb64(enc["salt"]), _unb64(enc["nonce"])
    except (KeyError, TypeError, ValueError) as e:
        raise BackupError(f"The encrypted part of the backup is damaged: {e}") from e
    low, high = SCRYPT_N_RANGE
    if (
        not low <= n <= high
        or n & (n - 1)
        or not 1 <= r <= SCRYPT_MAX_R
        or not 1 <= p <= SCRYPT_MAX_P
        or 128 * n * r > SCRYPT_MAX_MEMORY
        or len(salt) != SALT_BYTES
        or len(nonce) != NONCE_BYTES
    ):
        raise BackupError("The backup uses encryption settings Guake does not accept.")
    return salt, nonce, n, r, p


def decrypt_secrets(backup: Backup, passphrase: str) -> Secrets:
    """Decrypt the passwords and keys of ``backup``. Raises WrongPassphrase
    when ``passphrase`` is not the one used for the export."""
    if not backup.has_secrets:
        return Secrets()
    if not is_encryption_available():
        raise BackupError("python3-cryptography is needed to import passwords and keys.")
    salt, nonce, n, r, p = _kdf_parameters(backup.encrypted_secrets)
    try:
        key = _derive_key(passphrase, salt, n, r, p)
        plain = AESGCM(key).decrypt(
            nonce, _unb64(backup.encrypted_secrets.get("data", "")), _associated_data(n, r, p)
        )
    except InvalidTag as e:
        raise WrongPassphrase("The passphrase is not correct.") from e
    except (MemoryError, TypeError, ValueError, AttributeError) as e:
        raise BackupError(f"The encrypted part of the backup is damaged: {e}") from e
    try:
        data = json.loads(plain.decode("utf-8"))
        return Secrets(
            passwords={str(k): str(v) for k, v in data.get("passwords", {}).items()},
            keys={str(k): _unb64(v) for k, v in data.get("keys", {}).items()},
        )
    except (ValueError, AttributeError) as e:
        raise BackupError(f"The encrypted part of the backup is damaged: {e}") from e


def collect_keys(servers: Iterable[Server]) -> Dict[str, bytes]:
    """Contents of the private key files the servers use, by identity_file.
    Missing or unreadable files are skipped (and logged)."""
    keys = {}
    for server in servers:
        if not server.identity_file or server.identity_file in keys:
            continue
        path = Path(os.path.expanduser(server.identity_file))
        try:
            if path.stat().st_size > MAX_KEY_BYTES:
                raise OSError("too large for a private key")
            content = path.read_bytes()
        except OSError as e:
            log.warning("Cannot read key %s of server %s: %s", server.identity_file, server.name, e)
            continue
        if looks_like_private_key(content):
            keys[server.identity_file] = content
        else:
            log.warning("Not exporting %s: it is not a private key file", server.identity_file)
    return keys


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)


def restore_key(identity_file: str, content: bytes, home: Optional[Path] = None) -> str:
    """Put an imported private key on disk and return the identity_file the
    server should use from now on. Raises BackupError when ``content`` is not
    a private key or cannot be written.

    Only a private key called like ``id_*`` directly in ~/.ssh is restored
    where the server expects it, and only when that name is free or already
    holds the same key. Everything else (another existing key, config,
    authorized_keys, paths outside ~/.ssh...) goes to ~/.ssh/guake-imported/,
    so a backup can never plant or replace ssh's own files.
    """
    if not looks_like_private_key(content):
        raise BackupError(f"The key for {identity_file} is not a private key file.")
    home = Path(home) if home else Path.home()
    ssh_dir = home / SSH_DIR
    if identity_file.startswith("~"):
        expanded = identity_file.replace("~", str(home), 1)
    else:
        expanded = identity_file
    target = Path(os.path.normpath(expanded))
    try:
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            target.parent == ssh_dir
            and KEY_FILE_NAME.match(target.name)
            and not target.name.endswith(".pub")
        ):
            if not os.path.lexists(target):
                _write_private(target, content)
                return identity_file
            if target.is_file() and not target.is_symlink() and target.read_bytes() == content:
                return identity_file
        digest = hashlib.sha256(content).hexdigest()[:8]
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", target.name).lstrip(".") or "key"
        name = f"{base}-{digest}"
        fallback = ssh_dir / IMPORTED_KEYS_DIR / name
        if not os.path.lexists(fallback):
            _write_private(fallback, content)
        elif fallback.is_symlink() or fallback.read_bytes() != content:
            raise BackupError(f"{fallback} already exists with other content.")
    except OSError as e:
        raise BackupError(f"Cannot restore the key {identity_file}: {e}") from e
    return f"~/{SSH_DIR}/{IMPORTED_KEYS_DIR}/{name}"


def collect_secrets(servers: Iterable[Server], keyring) -> Secrets:
    """Passwords (from ``keyring``, see :mod:`guake.serversecrets`) and key
    files of ``servers``, ready to be encrypted into a backup."""
    servers = list(servers)
    passwords = {}
    for server in servers:
        if server.use_password:
            password = keyring.lookup_password(server.id)
            if password:
                passwords[server.id] = password
    return Secrets(passwords=passwords, keys=collect_keys(servers))


@dataclass(frozen=True)
class ImportSummary:
    added: List[Server]
    updated: List[Server]
    unchanged: List[Server]
    passwords: int = 0
    keys: int = 0


CONNECTION_FIELDS = ("host", "user", "port", "jump_host", "options")


def risky_servers(servers: Iterable[Server]) -> List[Server]:
    """Servers whose settings run commands or change where ssh connects
    through (ssh options such as ProxyCommand, a remote command, a jump
    host): the import asks before accepting those from a file."""
    return [s for s in servers if s.options or s.command or s.jump_host]


def _prepare_incoming(servers: Iterable[Server]) -> List[Server]:
    """One server per name (the last wins) and no ids pretending to be
    ~/.ssh/config entries."""
    by_name = {}
    for server in servers:
        if server.id.startswith(SSH_CONFIG_ID_PREFIX):
            server = server.with_changes(id=uuid.uuid4().hex)
        by_name[server.name.lower()] = server
    return list(by_name.values())


def _restore_keys(servers: List[Server], secrets: Secrets, home) -> Tuple[List[Server], int]:
    restored, count = [], 0
    for server in servers:
        content = secrets.keys.get(server.identity_file) if server.identity_file else None
        if content is not None:
            try:
                server = server.with_changes(
                    identity_file=restore_key(server.identity_file, content, home)
                )
                count += 1
            except BackupError as e:
                log.warning("Key of server %s not restored: %s", server.name, e)
        restored.append(server)
    return restored, count


def import_backup(store, servers: Iterable[Server], secrets: Secrets, keyring, home=None):
    """Merge ``servers`` into ``store``: restore their private keys, save
    their passwords in ``keyring`` and keep ``use_password`` truthful for
    this machine (a server whose password did not come along asks for it).

    A saved server whose host, user, port, jump host or options change
    loses its keyring password unless the backup brings one, so a backup
    cannot redirect a saved password to another machine."""
    incoming, keys = _restore_keys(_prepare_incoming(servers), secrets, home)
    before = {s.id: s for s in store.servers}
    result = store.merge(incoming)

    original_id = {s.name.lower(): s.id for s in incoming}
    passwords = 0
    for server in [*result.added, *result.updated, *result.unchanged]:
        password = secrets.passwords.get(original_id.get(server.name.lower(), server.id))
        old = before.get(server.id)
        retargeted = old is not None and any(
            getattr(old, f) != getattr(server, f) for f in CONNECTION_FIELDS
        )
        if password and keyring.store_password(server.id, password, server.name):
            passwords += 1
            has_password = True
        elif retargeted:
            keyring.clear_password(server.id)
            has_password = False
        else:
            has_password = keyring.lookup_password(server.id) is not None
        if server.use_password != has_password:
            store.update(server.with_changes(use_password=has_password))
    return ImportSummary(
        added=[store.get(s.id) for s in result.added],
        updated=[store.get(s.id) for s in result.updated],
        unchanged=result.unchanged,
        passwords=passwords,
        keys=keys,
    )
