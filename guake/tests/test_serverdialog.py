# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

from unittest import mock

import pytest

from gi.repository import Gtk

from guake.menus import mk_servers_menu
from guake.notebook import TerminalNotebook
from guake.serverdialog import ServerEditDialog
from guake.serverdialog import ServersDialog
from guake.serverdialog import server_detail
from guake.servers import Server
from guake.servers import ServerStore


class FakeGuake:
    def __init__(self, tmp_path):
        self.window = Gtk.Window()
        self.servers = ServerStore(tmp_path / "servers.json")
        self.connect_to_server = mock.Mock()
        self.show_servers = mock.Mock()


@pytest.fixture
def guake(tmp_path, mocker):
    mocker.patch("guake.serverdialog.parse_ssh_config", return_value=[])
    mocker.patch("guake.menus.parse_ssh_config", return_value=[])
    return FakeGuake(tmp_path)


def menu_labels(menu):
    return [
        item.get_label()
        for item in menu.get_children()
        if not isinstance(item, Gtk.SeparatorMenuItem)
    ]


def first_path():
    return Gtk.TreePath.new_first()


# --- manager dialog --------------------------------------------------------------


def test_server_detail_shows_user_host_and_non_default_port():
    assert server_detail(Server(name="a", host="h")) == "h"
    assert server_detail(Server(name="a", host="h", user="u", port=2222)) == "u@h:2222"


def test_manager_lists_ungrouped_servers_then_groups(guake):
    guake.servers.add(Server(name="solo", host="h"))
    guake.servers.add(Server(name="web", host="h", user="u", port=2222, group="Prod"))
    dialog = ServersDialog(guake)

    top = [(row[1], row[2]) for row in dialog.model]
    assert top == [("solo", "h"), ("Prod", "")]
    group_row = dialog.model[1]
    assert [(child[1], child[2]) for child in group_row.iterchildren()] == [("web", "u@h:2222")]
    dialog.destroy()


def test_manager_buttons_follow_selection(guake):
    guake.servers.add(Server(name="solo", host="h"))
    dialog = ServersDialog(guake)
    assert not dialog.connect_button.get_sensitive()
    assert not dialog.edit_button.get_sensitive()
    assert not dialog.remove_button.get_sensitive()

    dialog.view.set_cursor(first_path(), None, False)
    assert dialog.connect_button.get_sensitive()
    assert dialog.edit_button.get_sensitive()
    assert dialog.remove_button.get_sensitive()
    dialog.destroy()


def test_manager_double_click_connects(guake):
    server = guake.servers.add(Server(name="solo", host="h"))[0]
    dialog = ServersDialog(guake)
    dialog.view.set_cursor(first_path(), None, False)

    dialog.on_row_activated(dialog.view, first_path(), None)

    guake.connect_to_server.assert_called_once_with(server)


def test_manager_remove_asks_then_clears_password(guake, mocker):
    clear = mocker.patch("guake.serverdialog.serversecrets.clear_password")
    ask = mocker.patch("guake.serverdialog._show_message", return_value=Gtk.ResponseType.YES)
    server = Server(name="solo", host="h", use_password=True)
    guake.servers.add(server)
    dialog = ServersDialog(guake)
    dialog.view.set_cursor(first_path(), None, False)

    dialog.on_remove()

    assert ask.called
    clear.assert_called_once_with(server.id)
    assert guake.servers.servers == []
    dialog.destroy()


def test_manager_remove_declined_keeps_server(guake, mocker):
    mocker.patch("guake.serverdialog._show_message", return_value=Gtk.ResponseType.NO)
    guake.servers.add(Server(name="solo", host="h"))
    dialog = ServersDialog(guake)
    dialog.view.set_cursor(first_path(), None, False)

    dialog.on_remove()

    assert len(guake.servers.servers) == 1
    dialog.destroy()


def test_manager_import_copies_ssh_config_hosts(guake, mocker):
    mocker.patch(
        "guake.serverdialog.parse_ssh_config",
        return_value=[Server(name="box", host="box", user="me", id="sshconfig:box", group="SSH")],
    )
    mocker.patch("guake.serverdialog._show_message")
    dialog = ServersDialog(guake)

    dialog.on_import()

    saved = guake.servers.servers
    assert [(s.name, s.user, s.group) for s in saved] == [("box", "me", "")]
    assert not saved[0].id.startswith("sshconfig:")
    dialog.destroy()


# --- edit dialog ---------------------------------------------------------------------


def test_edit_dialog_reads_form_into_server(guake):
    dialog = ServerEditDialog(Gtk.Window(), guake.servers)
    dialog.name_entry.set_text(" web ")
    dialog.group_entry.set_text("Prod")
    dialog.host_entry.set_text("10.0.0.5")
    dialog.port_spin.set_value(2200)
    dialog.user_entry.set_text("root")
    dialog.identity_entry.set_text("~/.ssh/key")
    dialog.jump_entry.set_text("bastion")
    dialog.options_entry.set_text("-C")
    dialog.command_entry.set_text("tmux attach || tmux")

    assert dialog.validation_error() is None
    server = dialog.build_server()
    assert server.name == "web"
    assert server.group == "Prod"
    assert server.host == "10.0.0.5"
    assert server.port == 2200
    assert server.user == "root"
    assert server.identity_file == "~/.ssh/key"
    assert server.jump_host == "bastion"
    assert server.options == "-C"
    assert server.command == "tmux attach || tmux"
    assert server.use_password is False
    dialog.destroy()


def test_edit_dialog_validation_messages(guake):
    dialog = ServerEditDialog(Gtk.Window(), guake.servers)
    assert "name" in dialog.validation_error()
    dialog.name_entry.set_text("x")
    assert "host" in dialog.validation_error()
    dialog.destroy()


def test_edit_dialog_rejects_unbalanced_ssh_options(guake):
    dialog = ServerEditDialog(Gtk.Window(), guake.servers)
    dialog.name_entry.set_text("a")
    dialog.host_entry.set_text("h")
    dialog.options_entry.set_text("-o 'oops")
    assert "SSH options" in dialog.validation_error()
    dialog.destroy()


def test_edit_dialog_keeps_id_and_password_flag_when_editing(guake):
    server = Server(name="a", host="h", id="abc", use_password=True)
    dialog = ServerEditDialog(Gtk.Window(), guake.servers, server)
    dialog.host_entry.set_text("h2")
    assert dialog.build_server() == server.with_changes(host="h2")
    dialog.destroy()


def test_edit_dialog_saves_new_server_and_password_in_keyring(guake, mocker):
    mocker.patch("guake.serverdialog.serversecrets.is_available", return_value=True)
    store_password = mocker.patch(
        "guake.serverdialog.serversecrets.store_password", return_value=True
    )
    dialog = ServerEditDialog(Gtk.Window(), guake.servers)
    dialog.name_entry.set_text("a")
    dialog.host_entry.set_text("h")
    dialog.password_entry.set_text("pw")
    mocker.patch.object(dialog, "run", return_value=Gtk.ResponseType.OK)

    server = dialog.run_and_save()

    assert server.use_password is True
    store_password.assert_called_once_with(server.id, "pw", "a")
    assert guake.servers.get(server.id) == server
    assert "pw" not in guake.servers.path.read_text(encoding="utf-8")
    dialog.destroy()


def test_edit_dialog_keyring_failure_does_not_enable_password(guake, mocker):
    mocker.patch("guake.serverdialog.serversecrets.is_available", return_value=True)
    mocker.patch("guake.serverdialog.serversecrets.store_password", return_value=False)
    mocker.patch("guake.serverdialog._show_message")
    dialog = ServerEditDialog(Gtk.Window(), guake.servers)
    dialog.name_entry.set_text("a")
    dialog.host_entry.set_text("h")
    dialog.password_entry.set_text("pw")
    mocker.patch.object(dialog, "run", return_value=Gtk.ResponseType.OK)

    server = dialog.run_and_save()

    assert server.use_password is False
    dialog.destroy()


def test_edit_dialog_forget_password(guake, mocker):
    mocker.patch("guake.serverdialog.serversecrets.is_available", return_value=True)
    clear = mocker.patch("guake.serverdialog.serversecrets.clear_password")
    server = Server(name="a", host="h", use_password=True)
    guake.servers.add(server)
    dialog = ServerEditDialog(Gtk.Window(), guake.servers, server)
    dialog.forget_password.set_active(True)
    mocker.patch.object(dialog, "run", return_value=Gtk.ResponseType.OK)

    saved = dialog.run_and_save()

    clear.assert_called_once_with(server.id)
    assert saved.use_password is False
    assert guake.servers.get(server.id).use_password is False
    dialog.destroy()


def test_edit_dialog_invalid_then_cancel_saves_nothing(guake, mocker):
    shown = mocker.patch("guake.serverdialog._show_message")
    dialog = ServerEditDialog(Gtk.Window(), guake.servers)
    mocker.patch.object(dialog, "run", side_effect=[Gtk.ResponseType.OK, Gtk.ResponseType.CANCEL])

    assert dialog.run_and_save() is None

    assert shown.call_count == 1
    assert guake.servers.servers == []
    dialog.destroy()


def test_edit_dialog_password_disabled_without_keyring(guake, mocker):
    mocker.patch("guake.serverdialog.serversecrets.is_available", return_value=False)
    dialog = ServerEditDialog(Gtk.Window(), guake.servers)
    assert not dialog.password_entry.get_sensitive()
    dialog.destroy()


# --- menu and tab bar button ------------------------------------------------------------


def test_servers_menu_lists_servers_groups_and_management_items(guake):
    guake.servers.add(Server(name="solo", host="h"))
    guake.servers.add(Server(name="web", host="h", group="Prod"))

    menu = mk_servers_menu(guake)

    assert menu_labels(menu) == ["solo", "Prod", "Add server...", "Manage servers..."]
    group_item = menu.get_children()[1]
    assert menu_labels(group_item.get_submenu()) == ["web"]

    menu.get_children()[0].activate()
    guake.connect_to_server.assert_called_once_with(guake.servers.find_by_name("solo"))

    menu.get_children()[-1].activate()
    guake.show_servers.assert_called_once_with()


def test_servers_menu_lists_ssh_config_hosts(guake, mocker):
    mocker.patch(
        "guake.menus.parse_ssh_config",
        return_value=[Server(name="box", host="box", id="sshconfig:box")],
    )
    menu = mk_servers_menu(guake)
    labels = menu_labels(menu)
    assert "Hosts from ~/.ssh/config" in labels
    hosts_item = menu.get_children()[labels.index("Hosts from ~/.ssh/config") + 1]
    assert menu_labels(hosts_item.get_submenu()) == ["box"]


def test_servers_menu_without_servers_shows_placeholder(guake):
    menu = mk_servers_menu(guake)
    first = menu.get_children()[0]
    assert not first.get_sensitive()
    assert menu_labels(menu)[-2:] == ["Add server...", "Manage servers..."]


def test_notebook_has_servers_button(mocker):
    for target in [
        "guake.notebook.TerminalNotebook.terminal_spawn",
        "guake.notebook.TerminalNotebook.terminal_attached",
        "guake.notebook.TerminalNotebook.guake",
        "guake.notebook.TerminalBox.set_terminal",
    ]:
        mocker.patch(target, create=True)
    nb = TerminalNotebook()
    assert nb.servers_button in nb.action_box.get_children()
