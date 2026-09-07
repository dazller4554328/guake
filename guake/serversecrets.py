# -*- coding: utf-8; -*-
"""
Password storage for saved servers, backed by the desktop keyring through
libsecret (GNOME Keyring, KDE Wallet via the Secret Service API, ...).

libsecret is optional: when the ``Secret`` introspection data is missing
:func:`is_available` returns ``False`` and the password field is disabled
in the server editor. Passwords are never written to ``servers.json``.
"""

import logging

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


def is_available() -> bool:
    return Secret is not None


def store_password(server_id: str, password: str, label: str) -> bool:
    """Save ``password`` for ``server_id``. Returns ``True`` on success."""
    if not is_available():
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


def lookup_password(server_id: str):
    """Return the stored password, or ``None`` when there is none or the
    keyring cannot be reached."""
    if not is_available():
        return None
    try:
        return Secret.password_lookup_sync(_schema(), {"server-id": server_id}, None)
    except Exception as e:  # pylint: disable=broad-except
        log.error("Cannot read password for server %s from the keyring: %s", server_id, e)
        return None


def clear_password(server_id: str) -> bool:
    if not is_available():
        return False
    try:
        return bool(Secret.password_clear_sync(_schema(), {"server-id": server_id}, None))
    except Exception as e:  # pylint: disable=broad-except
        log.error("Cannot remove password for server %s from the keyring: %s", server_id, e)
        return False
