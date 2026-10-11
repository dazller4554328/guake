# -*- coding: utf-8; -*-
"""
Password storage for saved servers, backed by the desktop keyring through
libsecret (GNOME Keyring, KDE Wallet via the Secret Service API, ...).

libsecret is optional: when the ``Secret`` introspection data is missing
:func:`is_available` returns ``False`` and the password field is disabled
in the server editor. Passwords are never written to ``servers.json``.

When a master password is set, the passwords live in the vault instead
(:class:`guake.vault.VaultPasswords`, installed with :func:`use_backend`);
:data:`keyring` always is the desktop keyring.
"""

import logging
import types

log = logging.getLogger(__name__)

try:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ImportError, ValueError):  # pragma: no cover - depends on the host system
    Secret = None

_SCHEMA = None


def _schema():
    global _SCHEMA  # pylint: disable=global-statement
    if _SCHEMA is None and Secret is not None:
        _SCHEMA = Secret.Schema.new(
            "org.guake.Server",
            Secret.SchemaFlags.NONE,
            {"server-id": Secret.SchemaAttributeType.STRING},
        )
    return _SCHEMA


def _keyring_is_available() -> bool:
    return Secret is not None


def _keyring_store_password(server_id: str, password: str, label: str) -> bool:
    """Save ``password`` for ``server_id``. Returns ``True`` on success."""
    if not _keyring_is_available():
        return False
    try:
        return bool(
            Secret.password_store_sync(
                _schema(),
                {"server-id": server_id},
                Secret.COLLECTION_DEFAULT,
                f"Guake server: {label}",
                password,
                None,
            )
        )
    except Exception as e:  # pylint: disable=broad-except
        log.error("Cannot store password for server %s in the keyring: %s", server_id, e)
        return False


def _keyring_lookup_password(server_id: str):
    """Return the stored password, or ``None`` when there is none or the
    keyring cannot be reached."""
    if not _keyring_is_available():
        return None
    try:
        return Secret.password_lookup_sync(_schema(), {"server-id": server_id}, None)
    except Exception as e:  # pylint: disable=broad-except
        log.error("Cannot read password for server %s from the keyring: %s", server_id, e)
        return None


def _keyring_clear_password(server_id: str) -> bool:
    if not _keyring_is_available():
        return False
    try:
        return bool(Secret.password_clear_sync(_schema(), {"server-id": server_id}, None))
    except Exception as e:  # pylint: disable=broad-except
        log.error("Cannot remove password for server %s from the keyring: %s", server_id, e)
        return False


keyring = types.SimpleNamespace(
    is_available=_keyring_is_available,
    store_password=_keyring_store_password,
    lookup_password=_keyring_lookup_password,
    clear_password=_keyring_clear_password,
)

_backend = keyring


def use_backend(backend) -> None:
    """Keep the passwords in ``backend`` (the vault) from now on; ``None``
    goes back to the desktop keyring."""
    global _backend  # pylint: disable=global-statement
    _backend = backend or keyring


def uses_keyring() -> bool:
    return _backend is keyring


def is_available() -> bool:
    return _backend.is_available()


def store_password(server_id: str, password: str, label: str) -> bool:
    return _backend.store_password(server_id, password, label)


def lookup_password(server_id: str):
    return _backend.lookup_password(server_id)


def clear_password(server_id: str) -> bool:
    return _backend.clear_password(server_id)
