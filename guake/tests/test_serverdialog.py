# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

from unittest import mock

import pytest

from gi.repository import Gtk

from guake import addonstyle
from guake.menus import mk_servers_menu
from guake.notebook import TerminalNotebook
from guake.serverdialog import COLUMN_COLOR
from guake.serverdialog import ServerEditDialog
from guake.serverdialog import ServersDialog
from guake.serverdialog import count_summary
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
    """Text of each menu item; for server items (swatch, name, detail) the name."""
    labels = []
    for item in menu.get_children():
        if isinstance(item, Gtk.SeparatorMenuItem):
            continue
        label = item.get_label()
        if label is None:
            box = item.get_child()
            label = next(w.get_text() for w in box.get_children() if isinstance(w, Gtk.Label))
        labels.append(label)
    return labels


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


# --- polish: search, colours, empty state, backups ---------------------------------


def visible_names(dialog):
    names = []

    def walk(rows):
        for row in rows:
            names.append(row[1])
            walk(row.iterchildren())

    walk(dialog.filter)
    return names


def test_manager_search_filters_by_name_host_and_group(guake):
    guake.servers.add(Server(name="solo", host="alpha.example"))
    guake.servers.add(Server(name="web", host="h", group="Prod"))
    guake.servers.add(Server(name="db", host="h", group="Prod"))
    dialog = ServersDialog(guake)

    dialog.search_entry.set_text("alpha")
    dialog.on_search_changed(dialog.search_entry)
    assert visible_names(dialog) == ["solo"]
    assert dialog.selected_server().name == "solo"

    dialog.search_entry.set_text("prod")
    dialog.on_search_changed(dialog.search_entry)
    assert visible_names(dialog) == ["Prod", "db", "web"]

    dialog.search_entry.set_text("")
    dialog.on_search_changed(dialog.search_entry)
    assert len(visible_names(dialog)) == 4
    dialog.destroy()


def test_manager_shows_empty_state_without_servers(guake):
    dialog = ServersDialog(guake)
    assert dialog.stack.get_visible_child_name() == "empty"
    assert not dialog.export_backup_item.get_sensitive()
    guake.servers.add(Server(name="solo", host="h"))
    dialog.refresh()
    assert dialog.stack.get_visible_child_name() == "list"
    assert dialog.export_backup_item.get_sensitive()
    dialog.destroy()


def test_manager_sftp_button_opens_panel(guake):
    guake.open_sftp_panel = mock.Mock()
    server = guake.servers.add(Server(name="solo", host="h"))[0]
    dialog = ServersDialog(guake)
    dialog.view.set_cursor(first_path(), None, False)
    dialog.on_sftp()
    guake.open_sftp_panel.assert_called_once_with(server)


def test_edit_dialog_color_picker_round_trips(guake):
    server = Server(name="a", host="h", color="#e62d42")
    dialog = ServerEditDialog(Gtk.Window(), guake.servers, server)
    assert dialog.selected_color() == "#e62d42"
    dialog.color_buttons[""].set_active(True)
    assert dialog.build_server().color == ""
    dialog.color_buttons["#3584e4"].set_active(True)
    assert dialog.build_server().color == "#3584e4"
    dialog.destroy()


def test_export_dialog_validates_passphrase():
    from guake.serverbackupdialogs import ExportDialog

    dialog = ExportDialog(Gtk.Window(), 3)
    assert dialog.validation_error() is None  # servers only
    dialog.include_secrets.set_active(True)
    dialog.passphrase.set_text("short")
    dialog.confirm.set_text("short")
    assert "12" in dialog.validation_error()
    dialog.passphrase.set_text("long enough pass")
    dialog.confirm.set_text("different pass!!")
    assert dialog.validation_error() == "The two passphrases are different."
    dialog.confirm.set_text("long enough pass")
    assert dialog.validation_error() is None
    dialog.destroy()


def test_passphrase_dialog_retries_on_wrong_passphrase(mocker):
    from guake import serverbackup
    from guake.serverbackupdialogs import PassphraseDialog

    dialog = PassphraseDialog(Gtk.Window())
    mocker.patch.object(dialog, "run", side_effect=[Gtk.ResponseType.OK, Gtk.ResponseType.OK])
    secrets = serverbackup.Secrets(passwords={"a": "b"})
    decrypt = mocker.patch(
        "guake.serverbackupdialogs.serverbackup.decrypt_secrets",
        side_effect=[serverbackup.WrongPassphrase("no"), secrets],
    )
    assert dialog.run_for_secrets(object()) == secrets
    assert decrypt.call_count == 2
    assert dialog.error.get_visible()
    dialog.destroy()


def test_passphrase_dialog_skip_imports_without_secrets(mocker):
    from guake import serverbackup
    from guake.serverbackupdialogs import PassphraseDialog

    dialog = PassphraseDialog(Gtk.Window())
    mocker.patch.object(dialog, "run", return_value=PassphraseDialog.SKIP)
    assert dialog.run_for_secrets(object()) == serverbackup.Secrets()
    dialog.destroy()


def test_edit_dialog_keeps_a_custom_colour(guake):
    server = Server(name="a", host="h", color="#123456")
    dialog = ServerEditDialog(Gtk.Window(), guake.servers, server)
    assert dialog.build_server().color == "#123456"
    dialog.destroy()


def test_import_asks_before_accepting_servers_that_run_commands(guake, mocker, tmp_path):
    from guake import serverbackup
    from guake import serverbackupdialogs as dialogs

    path = tmp_path / "b.json"
    evil = Server(name="evil", host="h", options="-o ProxyCommand=touch /tmp/pwn")
    serverbackup.write_backup(path, serverbackup.build_backup([evil]))
    mocker.patch.object(dialogs, "_choose_file", return_value=str(path))
    confirm = mocker.patch.object(dialogs, "confirm_risky_servers", return_value=False)

    assert dialogs.import_servers(Gtk.Window(), guake.servers) is None
    assert confirm.call_args[0][1] == [evil]
    assert guake.servers.servers == []


# --- group colours -----------------------------------------------------------------


def select_row(dialog, path):
    dialog.view.set_cursor(Gtk.TreePath.new_from_string(path), None, False)


def test_manager_rows_are_painted_in_the_group_color(guake):
    guake.servers.add(Server(name="web", host="h", group="Prod"))
    guake.servers.add(Server(name="db", host="h", group="Prod", color="#3584e4"))
    guake.servers.set_group_color("Prod", "#e62d42")
    dialog = ServersDialog(guake)

    group_row = dialog.model[0]
    assert group_row[COLUMN_COLOR] == "#e62d42"
    colors = {child[1]: child[COLUMN_COLOR] for child in group_row.iterchildren()}
    assert colors == {"web": "#e62d42", "db": "#3584e4"}
    dialog.destroy()


def test_group_color_button_follows_the_selection(guake):
    guake.servers.add(Server(name="solo", host="h"))
    guake.servers.add(Server(name="web", host="h", group="Prod"))
    dialog = ServersDialog(guake)

    select_row(dialog, "0")
    assert dialog.selected_group() == "" and not dialog.group_color_button.get_sensitive()
    select_row(dialog, "1")
    assert dialog.selected_group() == "Prod" and dialog.group_color_button.get_sensitive()
    assert not dialog.connect_button.get_sensitive()
    select_row(dialog, "1:0")
    assert dialog.selected_group() == "Prod" and dialog.group_color_button.get_sensitive()
    dialog.destroy()


def test_picking_a_group_color_repaints_rows_and_tabs(guake):
    guake.refresh_server_tabs = mock.Mock()
    guake.servers.add(Server(name="web", host="h", group="Prod", id="web"))
    dialog = ServersDialog(guake)
    select_row(dialog, "0:0")

    dialog.set_group_color("Prod", "#3a944a")

    assert guake.servers.group_color("Prod") == "#3a944a"
    assert dialog.model[0][COLUMN_COLOR] == "#3a944a"
    assert dialog.selected_server().id == "web"
    guake.refresh_server_tabs.assert_called_once_with()

    # The menu ticks the picked colour, and "Automatic" once it is reset.
    menu = dialog._group_color_menu("Prod")  # pylint: disable=protected-access
    assert [i.get_active() for i in menu.get_children()].index(True) == 3
    dialog.set_group_color("Prod", "")
    menu = dialog._group_color_menu("Prod")  # pylint: disable=protected-access
    assert menu.get_children()[0].get_active()
    dialog.destroy()


def test_context_menu_offers_server_actions_and_the_group_color(guake):
    guake.servers.add(Server(name="solo", host="h"))
    guake.servers.add(Server(name="web", host="h", group="Prod"))
    dialog = ServersDialog(guake)

    select_row(dialog, "0")
    labels = menu_labels(dialog._build_context_menu())  # pylint: disable=protected-access
    assert labels == ["Connect", "Browse files over SFTP", "Edit...", "Remove"]
    select_row(dialog, "1")
    labels = menu_labels(dialog._build_context_menu())  # pylint: disable=protected-access
    assert labels == ["Color of group Prod"]
    dialog.destroy()


def test_title_bar_counts_servers_and_groups(guake):
    assert count_summary([]) == ""
    assert count_summary([Server(name="a", host="h")]) == "1 server"
    servers = [Server(name="a", host="h", group="x"), Server(name="b", host="h", group="y")]
    assert count_summary(servers) == "2 servers · 2 groups"


def test_servers_menu_marks_groups_and_servers_with_their_color(guake):
    guake.servers.add(Server(name="web", host="h", group="Prod"))
    guake.servers.set_group_color("Prod", "#e62d42")
    menu = mk_servers_menu(guake)
    assert menu_labels(menu)[0] == "Prod"
    assert menu_labels(menu.get_children()[0].get_submenu()) == ["web"]


# --- look ---------------------------------------------------------------------


@pytest.mark.parametrize("dark", [True, False])
def test_addon_css_is_valid_and_scoped_to_the_addon(dark):
    css = addonstyle.addon_css(dark)
    Gtk.CssProvider().load_from_data(css.encode())  # raises on a syntax error
    assert "$" not in css
    selectors = [
        line.split("{")[0].strip()
        for line in css.splitlines()
        if "{" in line and not line.startswith(" ")
    ]
    assert selectors
    for selector in selectors:
        for part in selector.split(","):
            if part.strip():
                assert part.strip().startswith((".guake-", "#notebook-teminals")), selector


def test_bundled_icons_are_found_and_unknown_ones_fall_back():
    assert addonstyle.icon_name("server") == "guake-server-symbolic"
    assert addonstyle.icon_name("no-such-icon") == addonstyle.DEFAULT_FALLBACK_ICON
    assert addonstyle.colored_icon("server", "#e62d42", 16) is not None
    assert addonstyle.colored_icon("server", "not a colour", 16) is None


def test_every_icon_the_addon_uses_is_bundled():
    import re

    from pathlib import Path

    import guake

    root = Path(guake.__file__).parent
    used = set()
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        used.update(
            re.findall(r'addonstyle\.(?:image|icon_name|colored_icon)\(\s*"([a-z-]+)"', text)
        )
        used.update(re.findall(r'_icon_button\(\s*"([a-z-]+)"', text))
    used.update(["folder", "file", "file-media", "file-pdf", "file-zip", "file-code"])
    used.update(["file-binary", "file-symlink-file", "arrow-up", "arrow-down"])
    missing = [
        n for n in sorted(used) if not (root / "data/pixmaps" / f"guake-{n}-symbolic.svg").exists()
    ]
    assert not missing
