# -*- coding: utf-8; -*-
"""
The vault: saved servers and their passwords, encrypted with a master
password.

Without a master password the servers are in ``servers.json`` and their
passwords in the desktop keyring. Setting one moves both into
``~/.config/guake/servers.vault``::

    {
        "format": "guake-vault",
        "version": 1,
        "kdf": "scrypt", "n": 131072, "r": 8, "p": 1,
        "cipher": "aes-256-gcm",
        "salt": "<base64>", "nonce": "<base64>", "data": "<base64>"
    }

``data`` is a JSON object of sections (``servers``: what ``servers.json``
held, ``passwords``: server id -> password), encrypted with a key derived
from the master password. The key is derived once, when the vault is
unlocked, and kept in memory until it is locked; every save encrypts the
sections again under a new nonce. Nothing else is written to disk.

Locking drops the key and the decrypted sections. Python cannot wipe
them from memory, so this protects the file, not a running Guake against
someone who can read its memory.

No GTK here, so it can be unit tested without a display. The dialogs live
in :mod:`guake.vaultdialogs`.
"""

import copy
import hmac
import json
import logging
import os
import tempfile
import threading

from pathlib import Path
from typing import List
from typing import Optional

from guake import serversecrets
from guake.serverbackup import CIPHER_NAME
from guake.serverbackup import KDF_NAME
from guake.serverbackup import NONCE_BYTES
from guake.serverbackup import SALT_BYTES
from guake.serverbackup import SCRYPT_N
from guake.serverbackup import SCRYPT_P
from guake.serverbackup import SCRYPT_R
from guake.serverbackup import BackupError
from guake.serverbackup import _b64
from guake.serverbackup import _derive_key
from guake.serverbackup import _kdf_parameters
from guake.serverbackup import _unb64
from guake.serverbackup import is_encryption_available
from guake.servers import SERVERS_SCHEMA_VERSION
from guake.servers import FileStorage
from guake.servers import Server
from guake.servers import ServersFile
from guake.servers import StoreLocked
from guake.servers import parse_servers_data
from guake.servers import servers_payload

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - depends on the host system
    AESGCM = None

log = logging.getLogger(__name__)

VAULT_FORMAT = "guake-vault"
VAULT_VERSION = 1
VAULT_FILENAME = "servers.vault"
MAX_VAULT_BYTES = 32 * 1024 * 1024
MIN_PASSWORD_LENGTH = 8

SERVERS_SECTION = "servers"
PASSWORDS_SECTION = "passwords"
SESSION_SECTION = "session"
VERIFIER_LABEL = b"guake-vault-locked"


class VaultError(Exception):
    """The vault cannot be read or written."""


class WrongPassword(VaultError):
    """The master password does not open the vault."""


class VaultLocked(VaultError):
    """The vault has to be unlocked first."""


def is_available() -> bool:
    return is_encryption_available()


def _associated_data(n: int, r: int, p: int) -> bytes:
    """Authenticate the parameters along with the ciphertext, so they
    cannot be weakened in the file without the decryption failing."""
    header = {"format": VAULT_FORMAT, "version": VAULT_VERSION, "kdf": KDF_NAME}
    header.update(n=n, r=r, p=p, cipher=CIPHER_NAME)
    return json.dumps(header, sort_keys=True).encode("ascii")


class _Key:
    """The derived key and what it was derived with."""

    def __init__(self, password: str, salt: bytes, n: int, r: int, p: int):
        self.salt, self.n, self.r, self.p = salt, n, r, p
        self.key = _derive_key(password, salt, n, r, p)

    def matches(self, password: str) -> bool:
        candidate = _derive_key(password, self.salt, self.n, self.r, self.p)
        return hmac.compare_digest(candidate, self.key)

    @property
    def associated_data(self) -> bytes:
        return _associated_data(self.n, self.r, self.p)

    def verifier(self) -> "_Verifier":
        return _Verifier(self)


class _Verifier:
    """Remembers, without keeping the key, which master password a locked
    vault was opened with: the vault only reopens with that one, whatever
    file is on disk by then."""

    def __init__(self, key: _Key):
        self.salt, self.n, self.r, self.p = key.salt, key.n, key.r, key.p
        self.digest = self._digest(key.key)

    @staticmethod
    def _digest(key: bytes) -> bytes:
        return hmac.new(key, VERIFIER_LABEL, "sha256").digest()

    def matches(self, password: str) -> bool:
        candidate = _derive_key(password, self.salt, self.n, self.r, self.p)
        return hmac.compare_digest(self._digest(candidate), self.digest)


class Vault:
    """The encrypted file at ``path`` and, while unlocked, its content.

    Once locked, the vault stays locked until the same master password is
    given again, even if the file was deleted or replaced meanwhile.

    Safe to use from several threads."""

    def __init__(self, path: Path, scrypt_n: int = SCRYPT_N):
        self.path = Path(path)
        self._scrypt_n = scrypt_n
        self._lock = threading.RLock()
        self._key: Optional[_Key] = None
        self._locked_with: Optional[_Verifier] = None
        self._sections: dict = {}

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def is_unlocked(self) -> bool:
        return self._key is not None

    @property
    def is_locked(self) -> bool:
        """There is a vault and it is closed (no vault is not locked)."""
        return not self.is_unlocked and (self._locked_with is not None or self.exists)

    def create(self, password: str, sections: dict) -> None:
        """Start a vault holding ``sections``, opened by ``password``."""
        self._check_new_password(password)
        with self._lock:
            if self.exists:
                raise VaultError(f"{self.path} already exists.")
            key = self._new_key(password)
            sections = copy.deepcopy(sections)
            self._write(key, sections)
            self._key, self._sections = key, sections
        log.info("Created the vault %s", self.path)

    def unlock(self, password: str) -> None:
        """Open the vault. Raises WrongPassword, or VaultError when the
        file cannot be used."""
        if not is_available():
            raise VaultError("python3-cryptography is needed to open the vault.")
        with self._lock:
            if self._locked_with is not None and not self._locked_with.matches(password):
                raise WrongPassword("The master password is not correct.")
            envelope = self._read_envelope()
            try:
                salt, nonce, n, r, p = _kdf_parameters(envelope)
            except BackupError as e:
                raise VaultError(
                    "The vault is damaged or uses settings Guake does not accept."
                ) from e
            try:
                key = _Key(password, salt, n, r, p)
                plain = AESGCM(key.key).decrypt(
                    nonce, _unb64(envelope.get("data", "")), key.associated_data
                )
            except InvalidTag as e:
                raise WrongPassword("The master password is not correct.") from e
            except (MemoryError, TypeError, ValueError, AttributeError) as e:
                raise VaultError(f"The vault is damaged: {e}") from e
            try:
                sections = json.loads(plain.decode("utf-8"))
            except ValueError as e:
                raise VaultError(f"The vault is damaged: {e}") from e
            if not isinstance(sections, dict):
                raise VaultError("The vault is damaged.")
            self._key, self._sections, self._locked_with = key, sections, None

    def lock(self) -> None:
        with self._lock:
            if self._key is not None:
                self._locked_with = self._key.verifier()
            self._key, self._sections = None, {}

    def check_password(self, password: str) -> bool:
        """Whether ``password`` is the master password of the open vault."""
        with self._lock:
            return self._open_key().matches(password)

    def change_password(self, password: str) -> None:
        self._check_new_password(password)
        with self._lock:
            self._open_key()
            key = self._new_key(password)
            self._write(key, self._sections)
            self._key = key
        log.info("Changed the master password of %s", self.path)

    def get(self, section: str):
        """A copy of ``section``; an empty dict when it was never saved."""
        with self._lock:
            self._open_key()
            return copy.deepcopy(self._sections.get(section, {}))

    def put(self, section: str, value) -> None:
        """Replace ``section`` and save the vault."""
        with self._lock:
            key = self._open_key()
            sections = {**self._sections, section: copy.deepcopy(value)}
            self._write(key, sections)
            self._sections = sections

    def remove(self) -> None:
        """Delete the vault file and forget its content."""
        with self._lock:
            self._key, self._sections, self._locked_with = None, {}, None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        log.info("Removed the vault %s", self.path)

    @staticmethod
    def _check_new_password(password: str) -> None:
        if not password:
            raise ValueError("A master password is needed")
        if not is_available():
            raise VaultError("python3-cryptography is needed for a master password.")

    def _open_key(self) -> _Key:
        if self._key is None:
            raise VaultLocked("The vault is locked.")
        return self._key

    def _new_key(self, password: str) -> _Key:
        return _Key(password, os.urandom(SALT_BYTES), self._scrypt_n, SCRYPT_R, SCRYPT_P)

    def _read_envelope(self) -> dict:
        try:
            if self.path.stat().st_size > MAX_VAULT_BYTES:
                raise VaultError("The vault file is too large.")
            with self.path.open(encoding="utf-8") as f:
                envelope = json.load(f)
        except (OSError, ValueError) as e:
            raise VaultError(f"Cannot read {self.path}: {e}") from e
        if not isinstance(envelope, dict) or envelope.get("format") != VAULT_FORMAT:
            raise VaultError(f"{self.path} is not a Guake vault.")
        version = envelope.get("version")
        if not isinstance(version, int) or version > VAULT_VERSION:
            raise VaultError("The vault was made by a newer Guake. Please update Guake first.")
        return envelope

    def _write(self, key: _Key, sections: dict) -> None:
        """Encrypt ``sections`` under a new nonce and replace the file
        atomically; it is readable by the user only."""
        nonce = os.urandom(NONCE_BYTES)
        plain = json.dumps(sections, ensure_ascii=False).encode("utf-8")
        envelope = {
            "format": VAULT_FORMAT,
            "version": VAULT_VERSION,
            "kdf": KDF_NAME,
            "n": key.n,
            "r": key.r,
            "p": key.p,
            "cipher": CIPHER_NAME,
            "salt": _b64(key.salt),
            "nonce": _b64(nonce),
            "data": _b64(AESGCM(key.key).encrypt(nonce, plain, key.associated_data)),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp: a fresh, unpredictable, 0600 file (never an existing symlink).
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".guake-vault-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(envelope, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class VaultServers:
    """Keeps the servers of a :class:`guake.servers.ServerStore` in the
    vault instead of ``servers.json``."""

    def __init__(self, vault: Vault):
        self.vault = vault

    def load(self) -> Optional[ServersFile]:
        """The saved servers, or None while the vault is locked."""
        try:
            data = self.vault.get(SERVERS_SECTION)
        except VaultLocked:
            return None
        if not data:
            return ServersFile(servers=[], deleted={}, groups={})
        # Never an empty list for servers Guake cannot read: the next save
        # would replace them for good.
        if not isinstance(data, dict) or "servers" not in data:
            raise VaultError("The servers in the vault are damaged.")
        if data.get("schema_version", 0) > SERVERS_SCHEMA_VERSION:
            raise VaultError("The vault was made by a newer Guake. Please update Guake first.")
        return parse_servers_data(data, "The vault")

    def save(self, servers, deleted, groups) -> None:
        try:
            self.vault.put(SERVERS_SECTION, servers_payload(servers, deleted, groups))
        except VaultLocked as e:
            raise StoreLocked(str(e)) from e


class VaultPasswords:
    """The server passwords, kept in the vault. Stands in for the desktop
    keyring in :mod:`guake.serversecrets`."""

    def __init__(self, vault: Vault):
        self.vault = vault

    @staticmethod
    def is_available() -> bool:
        return True

    def lookup_password(self, server_id: str):
        try:
            return self.vault.get(PASSWORDS_SECTION).get(server_id)
        except VaultLocked:
            log.warning("No password for server %s: the vault is locked", server_id)
            return None

    def store_password(self, server_id: str, password: str, label: str) -> bool:
        # pylint: disable=unused-argument
        return self._change(lambda passwords: {**passwords, server_id: password})

    def clear_password(self, server_id: str) -> bool:
        try:
            if server_id not in self.vault.get(PASSWORDS_SECTION):
                return False
        except VaultLocked:
            return False
        return self._change(
            lambda passwords: {k: v for k, v in passwords.items() if k != server_id}
        )

    def _change(self, change) -> bool:
        try:
            self.vault.put(PASSWORDS_SECTION, change(self.vault.get(PASSWORDS_SECTION)))
        except (VaultError, OSError) as e:
            log.error("Cannot save the passwords in the vault: %s", e)
            return False
        return True


def attach(vault: Vault, store) -> None:
    """Make ``store`` and :mod:`guake.serversecrets` use the open vault.
    Raises VaultError when its servers cannot be used."""
    plain = store.storage
    store.storage = VaultServers(vault)
    try:
        store.reload()
    except VaultError:
        store.storage = plain
        store.reload()
        raise
    serversecrets.use_backend(VaultPasswords(vault))
    _remove_leftover(FileStorage(store.path), store)


def _remove_leftover(plain: FileStorage, store) -> None:
    """Delete a ``servers.json`` left next to the vault (Guake was stopped
    while setting or removing the master password), unless it holds servers
    the vault does not have."""
    if not plain.path.exists():
        return
    known = {s.id for s in store.servers}
    if all(s.id in known for s in plain.load().servers):
        plain.remove()
        log.info("Removed %s: its servers are in the vault", plain.path)
    else:
        log.warning("%s is not encrypted and holds servers the vault does not have", plain.path)


def enable(vault: Vault, store, keyring, password: str) -> None:
    """Set a master password: move the servers of ``store`` and their
    ``keyring`` passwords into a new vault, then delete the plain copies."""
    passwords = {}
    for server in store.servers:
        saved = keyring.lookup_password(server.id)
        if saved:
            passwords[server.id] = saved
    snapshot = store.snapshot()
    if snapshot is None:
        raise StoreLocked("The servers are locked")
    # One write holds everything before any plain copy is deleted.
    vault.create(
        password, {SERVERS_SECTION: servers_payload(*snapshot), PASSWORDS_SECTION: passwords}
    )
    store.storage = VaultServers(vault)
    serversecrets.use_backend(VaultPasswords(vault))
    FileStorage(store.path).remove()
    for server_id in passwords:
        if not keyring.clear_password(server_id):
            log.warning("The password of server %s is still in the keyring", server_id)


def disable(vault: Vault, store, keyring) -> List[Server]:
    """Remove the master password: the servers go back to ``servers.json``
    and their passwords to ``keyring``. Returns the servers whose password
    could not be saved there; they will ask for it when connecting."""
    passwords = vault.get(PASSWORDS_SECTION)
    store.set_storage(FileStorage(store.path))
    serversecrets.use_backend(None)
    lost = []
    for server in store.servers:
        saved = passwords.get(server.id)
        if saved is None or keyring.store_password(server.id, saved, server.name):
            continue
        lost.append(server)
        if server.use_password:
            store.update(server.with_changes(use_password=False))
    vault.remove()
    return lost
