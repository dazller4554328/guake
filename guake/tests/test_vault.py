# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

import json
import stat

import pytest

from guake import serversecrets
from guake import vault as vlt
from guake.servers import FileStorage
from guake.servers import Server
from guake.servers import ServerStore
from guake.servers import StoreLocked
from guake.vault import Vault
from guake.vault import VaultPasswords
from guake.vault import VaultServers

pytestmark = pytest.mark.skipif(not vlt.is_available(), reason="needs python3-cryptography")

PASSWORD = "correct horse battery"
FAST_SCRYPT_N = 2**14


class FakeKeyring:
    def __init__(self, passwords=None, available=True):
        self.passwords = dict(passwords or {})
        self.available = available

    def is_available(self):
        return self.available

    def lookup_password(self, server_id):
        return self.passwords.get(server_id)

    def store_password(self, server_id, password, label):  # pylint: disable=unused-argument
        if not self.available:
            return False
        self.passwords[server_id] = password
        return True

    def clear_password(self, server_id):
        return self.passwords.pop(server_id, None) is not None


@pytest.fixture(autouse=True)
def keyring_backend():
    yield
    serversecrets.use_backend(None)


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / vlt.VAULT_FILENAME, scrypt_n=FAST_SCRYPT_N)


@pytest.fixture
def web():
    return Server(name="web-1", host="10.0.0.5", user="root", id="web1", use_password=True)


@pytest.fixture
def store(tmp_path, web):
    store = ServerStore(tmp_path / "servers.json")
    store.add(web)
    return store


# --- the vault file ----------------------------------------------------------


def test_new_vault_does_not_exist_and_is_not_locked(vault):
    assert not vault.exists
    assert not vault.is_unlocked
    assert not vault.is_locked


def test_create_writes_a_file_without_the_plain_data(vault):
    vault.create(PASSWORD, {"servers": {"servers": [{"host": "secret.example.org"}]}})

    raw = vault.path.read_text(encoding="utf-8")

    assert vault.is_unlocked
    assert json.loads(raw)["format"] == vlt.VAULT_FORMAT
    assert "secret.example.org" not in raw
    assert PASSWORD not in raw


def test_vault_file_is_readable_by_the_user_only(vault):
    vault.create(PASSWORD, {})

    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600


def test_unlock_with_the_password_gives_the_data_back(vault):
    vault.create(PASSWORD, {"passwords": {"web1": "hunter2"}})
    reopened = Vault(vault.path)

    reopened.unlock(PASSWORD)

    assert reopened.get("passwords") == {"web1": "hunter2"}


def test_unlock_with_a_wrong_password_is_refused(vault):
    vault.create(PASSWORD, {})
    reopened = Vault(vault.path)

    with pytest.raises(vlt.WrongPassword):
        reopened.unlock("not the password")

    assert reopened.is_locked


def test_lock_forgets_the_data(vault):
    vault.create(PASSWORD, {"passwords": {"web1": "hunter2"}})

    vault.lock()

    assert vault.is_locked
    with pytest.raises(vlt.VaultLocked):
        vault.get("passwords")
    with pytest.raises(vlt.VaultLocked):
        vault.put("passwords", {})


def test_put_is_saved_and_survives_a_restart(vault):
    vault.create(PASSWORD, {})

    vault.put("passwords", {"web1": "new"})

    reopened = Vault(vault.path)
    reopened.unlock(PASSWORD)
    assert reopened.get("passwords") == {"web1": "new"}


def test_get_returns_a_copy(vault):
    vault.create(PASSWORD, {"passwords": {"web1": "a"}})

    vault.get("passwords")["web1"] = "changed"

    assert vault.get("passwords") == {"web1": "a"}
    assert vault.get("missing") == {}


def test_each_save_uses_a_new_nonce(vault):
    vault.create(PASSWORD, {})
    first = json.loads(vault.path.read_text(encoding="utf-8"))

    vault.put("passwords", {})

    second = json.loads(vault.path.read_text(encoding="utf-8"))
    assert first["nonce"] != second["nonce"]
    assert first["salt"] == second["salt"]


def test_change_password_replaces_the_old_one(vault):
    vault.create(PASSWORD, {"passwords": {"web1": "a"}})

    vault.change_password("another long password")

    reopened = Vault(vault.path)
    with pytest.raises(vlt.WrongPassword):
        reopened.unlock(PASSWORD)
    reopened.unlock("another long password")
    assert reopened.get("passwords") == {"web1": "a"}


def test_check_password(vault):
    vault.create(PASSWORD, {})

    assert vault.check_password(PASSWORD)
    assert not vault.check_password("nope")


def test_empty_password_is_refused(vault):
    with pytest.raises(ValueError):
        vault.create("", {})
    assert not vault.exists


def test_create_does_not_replace_an_existing_vault(vault):
    vault.create(PASSWORD, {})

    with pytest.raises(vlt.VaultError):
        Vault(vault.path).create("other password", {})


def test_tampered_file_is_refused(vault):
    vault.create(PASSWORD, {})
    data = json.loads(vault.path.read_text(encoding="utf-8"))
    data["n"] = 2**15
    vault.path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(vlt.WrongPassword):
        Vault(vault.path).unlock(PASSWORD)


@pytest.mark.parametrize("content", ["not json", "[]", '{"format": "something else"}'])
def test_damaged_file_is_reported(vault, content):
    vault.path.write_text(content, encoding="utf-8")

    with pytest.raises(vlt.VaultError) as error:
        vault.unlock(PASSWORD)

    assert not isinstance(error.value, vlt.WrongPassword)


def test_vault_from_a_newer_guake_is_refused(vault):
    vault.create(PASSWORD, {})
    data = json.loads(vault.path.read_text(encoding="utf-8"))
    data["version"] = vlt.VAULT_VERSION + 1
    vault.path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(vlt.VaultError, match="newer"):
        Vault(vault.path).unlock(PASSWORD)


def test_hostile_kdf_settings_are_refused(vault):
    vault.create(PASSWORD, {})
    data = json.loads(vault.path.read_text(encoding="utf-8"))
    data["n"] = 2**30
    vault.path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(vlt.VaultError):
        Vault(vault.path).unlock(PASSWORD)


def test_remove_deletes_the_file(vault):
    vault.create(PASSWORD, {})

    vault.remove()

    assert not vault.exists
    assert not vault.is_unlocked


# --- servers kept in the vault -------------------------------------------------


def test_store_on_a_vault_keeps_servers_out_of_plain_files(vault, tmp_path, web):
    vault.create(PASSWORD, {})
    store = ServerStore(tmp_path / "servers.json", storage=VaultServers(vault))

    store.add(web)

    assert not (tmp_path / "servers.json").exists()
    assert "10.0.0.5" not in vault.path.read_text(encoding="utf-8")
    reopened = Vault(vault.path)
    reopened.unlock(PASSWORD)
    assert ServerStore(tmp_path / "servers.json", storage=VaultServers(reopened)).servers == (
        store.servers
    )


def test_store_is_empty_and_read_only_while_the_vault_is_locked(vault, tmp_path, web):
    vault.create(PASSWORD, {})
    store = ServerStore(tmp_path / "servers.json", storage=VaultServers(vault))
    store.add(web)

    vault.lock()
    store.reload()

    assert store.locked
    assert store.servers == []
    assert store.snapshot() is None
    with pytest.raises(StoreLocked):
        store.add(Server(name="other", host="h"))


def test_store_comes_back_after_unlocking(vault, tmp_path, web):
    vault.create(PASSWORD, {})
    store = ServerStore(tmp_path / "servers.json", storage=VaultServers(vault))
    store.add(web)
    vault.lock()
    store.reload()

    vault.unlock(PASSWORD)
    store.reload()

    assert not store.locked
    assert [s.name for s in store.servers] == ["web-1"]
    assert [s.name for s in store.snapshot().servers] == ["web-1"]


# --- passwords kept in the vault -----------------------------------------------


def test_vault_passwords_store_lookup_and_clear(vault):
    vault.create(PASSWORD, {})
    passwords = VaultPasswords(vault)

    assert passwords.store_password("web1", "hunter2", "web-1")
    assert passwords.lookup_password("web1") == "hunter2"
    assert passwords.clear_password("web1")
    assert passwords.lookup_password("web1") is None
    assert not passwords.clear_password("web1")


def test_vault_passwords_are_unavailable_while_locked(vault):
    vault.create(PASSWORD, {})
    passwords = VaultPasswords(vault)
    passwords.store_password("web1", "hunter2", "web-1")

    vault.lock()

    assert passwords.lookup_password("web1") is None
    assert not passwords.store_password("web1", "x", "web-1")
    assert not passwords.clear_password("web1")


def test_serversecrets_goes_through_the_vault_when_one_is_set(vault):
    vault.create(PASSWORD, {})
    try:
        serversecrets.use_backend(VaultPasswords(vault))

        assert serversecrets.is_available()
        assert serversecrets.store_password("web1", "hunter2", "web-1")
        assert serversecrets.lookup_password("web1") == "hunter2"
        assert vault.get("passwords") == {"web1": "hunter2"}
    finally:
        serversecrets.use_backend(None)


# --- turning the master password on and off ------------------------------------


def test_enable_moves_servers_and_passwords_into_the_vault(vault, store):
    keyring = FakeKeyring({"web1": "hunter2"})

    vlt.enable(vault, store, keyring, PASSWORD)

    assert not store.path.exists()
    assert keyring.passwords == {}
    assert [s.name for s in store.servers] == ["web-1"]
    reopened = Vault(vault.path)
    reopened.unlock(PASSWORD)
    assert reopened.get("passwords") == {"web1": "hunter2"}
    assert [s["name"] for s in reopened.get("servers")["servers"]] == ["web-1"]


def test_enable_leaves_everything_alone_when_the_vault_cannot_be_written(vault, store, mocker):
    keyring = FakeKeyring({"web1": "hunter2"})
    mocker.patch("guake.vault.os.replace", side_effect=OSError("disk full"))

    with pytest.raises(OSError):
        vlt.enable(vault, store, keyring, PASSWORD)

    assert store.path.exists()
    assert not vault.exists
    assert not vault.is_unlocked
    assert keyring.passwords == {"web1": "hunter2"}
    assert isinstance(store.storage, FileStorage)
    assert serversecrets.uses_keyring()


def test_enable_writes_servers_and_passwords_in_one_go(vault, store, mocker):
    write = mocker.spy(vault, "_write")

    vlt.enable(vault, store, FakeKeyring({"web1": "hunter2"}), PASSWORD)

    assert write.call_count == 1


# --- staying locked ------------------------------------------------------------


def test_locked_vault_stays_locked_when_its_file_is_deleted(vault):
    vault.create(PASSWORD, {})
    vault.lock()

    vault.path.unlink()

    assert vault.is_locked
    with pytest.raises(vlt.VaultError):
        vault.unlock(PASSWORD)
    assert vault.is_locked


def test_locked_vault_does_not_open_with_the_password_of_a_swapped_file(vault, tmp_path):
    vault.create(PASSWORD, {})
    vault.lock()
    other = Vault(tmp_path / "other.vault", scrypt_n=FAST_SCRYPT_N)
    other.create("the password of someone else", {})
    other.path.replace(vault.path)

    with pytest.raises(vlt.WrongPassword):
        vault.unlock("the password of someone else")

    assert vault.is_locked


def test_locked_vault_reopens_with_its_own_password(vault):
    vault.create(PASSWORD, {"passwords": {"web1": "a"}})
    vault.lock()

    vault.unlock(PASSWORD)

    assert vault.get("passwords") == {"web1": "a"}


# --- starting with a vault -----------------------------------------------------


def test_attach_removes_a_servers_file_left_by_an_interrupted_change(vault, store, tmp_path):
    vlt.enable(vault, store, FakeKeyring(), PASSWORD)
    store.path.write_text(json.dumps(vault.get("servers")), encoding="utf-8")
    restarted = ServerStore(tmp_path / "servers.json")

    vlt.attach(vault, restarted)

    assert not store.path.exists()
    assert [s.name for s in restarted.servers] == ["web-1"]


def test_attach_keeps_a_servers_file_with_servers_the_vault_lacks(vault, store, tmp_path):
    vlt.enable(vault, store, FakeKeyring(), PASSWORD)
    plain = ServerStore(tmp_path / "servers.json")
    plain.add(Server(name="only-here", host="h"))

    vlt.attach(vault, ServerStore(tmp_path / "servers.json"))

    assert [s.name for s in ServerStore(tmp_path / "servers.json").servers] == ["only-here"]


@pytest.mark.parametrize("section", [{"schema_version": 99, "servers": []}, {"bogus": 1}, [1]])
def test_servers_guake_cannot_read_are_never_replaced_by_an_empty_list(vault, tmp_path, section):
    vault.create(PASSWORD, {"servers": section})
    store = ServerStore(tmp_path / "servers.json")

    with pytest.raises(vlt.VaultError):
        vlt.attach(vault, store)

    assert isinstance(store.storage, FileStorage)
    assert vault.get("servers") == section


def test_disable_moves_everything_back(vault, store):
    keyring = FakeKeyring({"web1": "hunter2"})
    vlt.enable(vault, store, keyring, PASSWORD)

    lost = vlt.disable(vault, store, keyring)

    assert lost == []
    assert not vault.exists
    assert keyring.passwords == {"web1": "hunter2"}
    assert [s.name for s in ServerStore(store.path).servers] == ["web-1"]
    assert store.get("web1").use_password


def test_disable_without_a_keyring_reports_the_lost_passwords(vault, store):
    vlt.enable(vault, store, FakeKeyring({"web1": "hunter2"}), PASSWORD)

    lost = vlt.disable(vault, store, FakeKeyring(available=False))

    assert [s.name for s in lost] == ["web-1"]
    assert not store.get("web1").use_password
    assert not vault.exists
