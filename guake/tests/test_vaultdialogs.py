# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

from unittest import mock

import pytest

from gi.repository import Gtk

from guake import serversecrets
from guake import vault as vlt
from guake import vaultdialogs
from guake.menus import mk_servers_menu
from guake.serverdialog import ServersDialog
from guake.servers import Server
from guake.servers import ServerStore
from guake.vaultdialogs import RESPONSE_REMOVE
from guake.vaultdialogs import MasterPasswordDialog
from guake.vaultdialogs import UnlockDialog
from guake.vaultdialogs import manage_master_password
from guake.vaultdialogs import new_password_error

pytestmark = pytest.mark.skipif(not vlt.is_available(), reason="needs python3-cryptography")

PASSWORD = "correct horse battery"


class FakeGuake:
    def __init__(self, tmp_path):
        self.window = Gtk.Window()
        self.servers = ServerStore(tmp_path / "servers.json")
        self.vault = vlt.Vault(tmp_path / vlt.VAULT_FILENAME, scrypt_n=2**14)
        self.show_servers = mock.Mock()
        self.connect_to_server = mock.Mock()
        self.lock = mock.Mock()


@pytest.fixture
def keyring(mocker):
    passwords = {"web1": "hunter2"}
    fake = mock.Mock()
    fake.is_available.return_value = True
    fake.lookup_password.side_effect = passwords.get
    fake.store_password.side_effect = lambda sid, pw, label: passwords.update({sid: pw}) or True
    fake.clear_password.side_effect = lambda sid: passwords.pop(sid, None) is not None
    fake.passwords = passwords
    mocker.patch.object(serversecrets, "keyring", fake)
    yield fake
    serversecrets.use_backend(None)


@pytest.fixture
def guake(tmp_path, mocker, keyring):  # pylint: disable=unused-argument
    mocker.patch("guake.serverdialog.parse_ssh_config", return_value=[])
    mocker.patch("guake.menus.parse_ssh_config", return_value=[])
    guake = FakeGuake(tmp_path)
    guake.servers.add(Server(name="web-1", host="10.0.0.5", id="web1", use_password=True))
    return guake


@pytest.fixture
def messages(mocker):
    return mocker.patch("guake.vaultdialogs._message", return_value=Gtk.ResponseType.OK)


def answer(mocker, responses, **fields):
    """Make the master password dialog answer ``responses`` in turn, with
    ``fields`` typed in."""

    def run(dialog):
        for name, text in fields.items():
            getattr(dialog, name).set_text(text)
        return responses.pop(0)

    mocker.patch.object(MasterPasswordDialog, "run", run)


def menu_labels(menu):
    return [item.get_label() for item in menu.get_children()]


# --- validation ----------------------------------------------------------------


def test_new_password_must_be_long_enough():
    assert "at least" in new_password_error("short", "short")


def test_new_password_must_be_typed_twice_the_same():
    assert "different" in new_password_error(PASSWORD, PASSWORD + "x")


def test_new_password_accepted():
    assert new_password_error(PASSWORD, PASSWORD) is None


# --- unlocking -----------------------------------------------------------------


def test_unlock_dialog_opens_the_vault_with_the_password(guake):
    guake.vault.create(PASSWORD, {})
    guake.vault.lock()
    dialog = UnlockDialog(guake.vault)
    dialog.password.set_text(PASSWORD)

    assert dialog.try_unlock()

    assert guake.vault.is_unlocked
    assert dialog.password.get_text() == ""
    dialog.destroy()


def test_unlock_dialog_explains_a_wrong_password(guake):
    guake.vault.create(PASSWORD, {})
    guake.vault.lock()
    dialog = UnlockDialog(guake.vault)
    dialog.password.set_text("wrong")

    assert not dialog.try_unlock()

    assert guake.vault.is_locked
    assert "not the master password" in dialog.error.get_text()
    assert dialog.error.get_visible()
    assert dialog.password.get_text() == ""
    dialog.destroy()


def test_ask_unlock_keeps_asking_until_cancelled(guake, mocker):
    guake.vault.create(PASSWORD, {})
    guake.vault.lock()
    responses = [Gtk.ResponseType.OK, Gtk.ResponseType.OK, Gtk.ResponseType.CANCEL]
    mocker.patch.object(UnlockDialog, "run", lambda dialog: responses.pop(0))

    assert not vaultdialogs.ask_unlock(guake.vault)

    assert responses == []
    assert guake.vault.is_locked


# --- setting, changing, removing -------------------------------------------------


def test_setting_a_master_password_encrypts_servers_and_passwords(guake, keyring, messages, mocker):
    answer(mocker, [Gtk.ResponseType.OK], password=PASSWORD, confirm=PASSWORD)

    manage_master_password(guake.window, guake)

    assert guake.vault.is_unlocked
    assert not guake.servers.path.exists()
    assert keyring.passwords == {}
    assert serversecrets.lookup_password("web1") == "hunter2"
    assert "10.0.0.5" not in guake.vault.path.read_text(encoding="utf-8")
    assert messages.call_args[0][1] == Gtk.MessageType.INFO


def test_mismatched_passwords_set_nothing(guake, messages, mocker):
    answer(
        mocker,
        [Gtk.ResponseType.OK, Gtk.ResponseType.CANCEL],
        password=PASSWORD,
        confirm="something else",
    )

    manage_master_password(guake.window, guake)

    assert not guake.vault.exists
    assert guake.servers.path.exists()
    assert messages.call_args[0][1] == Gtk.MessageType.ERROR


def test_changing_needs_the_current_password(guake, keyring, messages, mocker):
    vlt.enable(guake.vault, guake.servers, keyring, PASSWORD)
    answer(
        mocker,
        [Gtk.ResponseType.OK, Gtk.ResponseType.CANCEL],
        current="wrong",
        password="a brand new password",
        confirm="a brand new password",
    )

    manage_master_password(guake.window, guake)

    assert guake.vault.check_password(PASSWORD)
    assert messages.call_args[0][1] == Gtk.MessageType.ERROR


def test_changing_the_master_password(guake, keyring, messages, mocker):  # pylint: disable=W0613
    vlt.enable(guake.vault, guake.servers, keyring, PASSWORD)
    answer(
        mocker,
        [Gtk.ResponseType.OK],
        current=PASSWORD,
        password="a brand new password",
        confirm="a brand new password",
    )

    manage_master_password(guake.window, guake)

    assert guake.vault.check_password("a brand new password")
    assert [s.name for s in guake.servers.servers] == ["web-1"]


def test_removing_the_master_password_puts_everything_back(guake, keyring, messages, mocker):
    vlt.enable(guake.vault, guake.servers, keyring, PASSWORD)
    answer(mocker, [RESPONSE_REMOVE], current=PASSWORD)

    manage_master_password(guake.window, guake)

    assert not guake.vault.exists
    assert guake.servers.path.exists()
    assert keyring.passwords == {"web1": "hunter2"}
    assert serversecrets.uses_keyring()
    assert messages.call_args[0][1] == Gtk.MessageType.WARNING


def test_removing_needs_the_current_password(
    guake, keyring, messages, mocker
):  # pylint: disable=W0613
    vlt.enable(guake.vault, guake.servers, keyring, PASSWORD)
    answer(mocker, [RESPONSE_REMOVE, Gtk.ResponseType.CANCEL], current="wrong")

    manage_master_password(guake.window, guake)

    assert guake.vault.exists
    assert not guake.servers.path.exists()


def test_removing_can_be_cancelled_at_the_warning(guake, keyring, mocker):
    vlt.enable(guake.vault, guake.servers, keyring, PASSWORD)
    mocker.patch("guake.vaultdialogs._message", return_value=Gtk.ResponseType.CANCEL)
    answer(mocker, [RESPONSE_REMOVE, Gtk.ResponseType.CANCEL], current=PASSWORD)

    manage_master_password(guake.window, guake)

    assert guake.vault.exists


def test_master_password_needs_cryptography(guake, messages, mocker):
    mocker.patch("guake.vaultdialogs.vlt.is_available", return_value=False)

    manage_master_password(guake.window, guake)

    assert not guake.vault.exists
    assert "python3-cryptography" in messages.call_args[0][2]


# --- entry points ----------------------------------------------------------------


def test_servers_menu_offers_to_lock_only_with_an_open_vault(guake):
    assert _("Lock Guake") not in menu_labels(mk_servers_menu(guake))

    guake.vault.create(PASSWORD, {})
    menu = mk_servers_menu(guake)

    assert _("Lock Guake") in menu_labels(menu)
    next(i for i in menu.get_children() if i.get_label() == _("Lock Guake")).activate()
    guake.lock.assert_called_once()


def test_servers_dialog_opens_the_master_password_dialog(guake, mocker):
    manage = mocker.patch("guake.serverdialog.manage_master_password")
    dialog = ServersDialog(guake)

    dialog.master_password_item.activate()

    manage.assert_called_once_with(dialog, guake)
    dialog.destroy()
