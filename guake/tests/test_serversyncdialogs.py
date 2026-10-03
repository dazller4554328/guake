# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

from unittest import mock

import pytest

from gi.repository import Gtk

from guake import serversync as sync
from guake import serversyncdialogs as dialogs
from guake.servers import GroupColor
from guake.servers import Server
from guake.servers import ServerStore
from guake.servers import ServersFile
from guake.servers import Tombstone
from guake.serversyncdialogs import SyncDialog

NOW = 2e9


class FakeGuake:
    def __init__(self, tmp_path):
        self.window = Gtk.Window()
        self.servers = ServerStore(tmp_path / "servers.json")


def collected(*results):
    me = sync.TailscaleSelf("me", "100.1.1.1", 1)
    return sync.Collected(me=me, results=list(results))


def reachable(name, servers=(), deleted=None, groups=None):
    shared = ServersFile(servers=list(servers), deleted=deleted or {}, groups=groups or {})
    snapshot = sync.Snapshot(shared, 0)
    return sync.PeerResult(sync.Device(name, "100.1.1.2", True), snapshot, "")


@pytest.fixture
def guake(tmp_path):
    guake = FakeGuake(tmp_path)
    guake.servers.apply_sync(
        [
            Server(name="web", host="web.lan", id="web", updated_at=NOW - 100),
            Server(name="db", host="db.lan", id="db", updated_at=NOW - 100, use_password=True),
        ],
        {},
    )
    return guake


def open_dialog(guake, result=None, error=None):
    """A dialog showing ``result`` without running the background fetch."""
    with mock.patch.object(SyncDialog, "refresh"):
        dialog = SyncDialog(guake.window, guake)
    dialog.show_collected(result, error)
    return dialog


def test_time_ago():
    assert dialogs.time_ago(0) == ""
    assert dialogs.time_ago(NOW - 30, NOW) == "just now"
    assert dialogs.time_ago(NOW - 7200, NOW) == "2 hour(s) ago"
    assert dialogs.time_ago(NOW - 3 * 86400, NOW) == "3 day(s) ago"


def test_describe_update_names_changed_fields():
    before = Server(name="web", host="a", id="w")
    change = sync.Change(sync.UPDATE, before.with_changes(host="b", port=2), "laptop", 0, before)
    kind, text = dialogs.describe_change(change)
    assert kind == "Update" and "from laptop" in text
    assert "host: a → b" in text and "port: 22 → 2" in text


def test_describe_escapes_markup():
    change = sync.Change(sync.ADD, Server(name="<b>x&", host="h"), "lap<top>", 0)
    _, text = dialogs.describe_change(change)
    assert "&lt;b&gt;x&amp;" in text and "lap&lt;top&gt;" in text


def test_error_is_shown(guake):
    dialog = open_dialog(guake, error="Tailscale is not installed.")
    assert dialog.stack.get_visible_child_name() == "message"
    assert "not installed" in dialog.message_label.get_text()
    assert not dialog.apply_button.get_sensitive()


def test_no_devices_found(guake):
    dialog = open_dialog(guake, collected())
    assert "No other devices" in dialog.message_label.get_text()


def test_unreachable_device_is_listed(guake):
    down = sync.PeerResult(sync.Device("old", "100.1.1.3", False), None, "offline")
    dialog = open_dialog(guake, collected(down))
    assert "old" in dialog.devices_label.get_text() and "offline" in dialog.devices_label.get_text()
    assert "Could not get the servers of any device" in dialog.hint_label.get_text()


def test_nothing_to_do(guake):
    dialog = open_dialog(guake, collected(reachable("laptop", guake.servers.servers)))
    assert "already in sync" in dialog.hint_label.get_text()


def test_group_colors_picked_elsewhere_can_be_applied_on_their_own(guake):
    guake.servers.update(guake.servers.get("web").with_changes(group="Prod"))
    picked = {"Prod": GroupColor("#e62d42", NOW)}
    dialog = open_dialog(
        guake, collected(reachable("laptop", guake.servers.servers, groups=picked))
    )
    assert not dialog.changes
    assert "Prod" in dialog.group_colors_check.get_label()
    assert dialog.group_colors_check.get_visible() and dialog.apply_button.get_sensitive()

    # Unticked, nothing is left to apply and the colour stays as it was.
    dialog.group_colors_check.set_active(False)
    assert not dialog.apply_button.get_sensitive()
    assert dialog.apply() and not guake.servers.has_group_color("Prod")

    dialog.group_colors_check.set_active(True)
    assert dialog.apply()
    assert guake.servers.group_color("Prod") == "#e62d42"


def test_group_colors_row_is_hidden_when_there_are_none(guake):
    dialog = open_dialog(guake, collected(reachable("laptop", guake.servers.servers)))
    assert not dialog.group_colors_check.get_visible()


def test_adds_are_ticked_and_deletions_are_not(guake):
    new = Server(name="new", host="new.lan", id="new", updated_at=NOW)
    dialog = open_dialog(
        guake, collected(reachable("laptop", [new], {"web": Tombstone(at=NOW, name="web")}))
    )
    assert [(c.kind, row[dialogs.COL_APPLY]) for c, row in zip(dialog.changes, dialog.model)] == [
        ("add", True),
        ("delete", False),
    ]
    assert "not ticked" in dialog.hint_label.get_text()
    assert dialog.apply_button.get_sensitive()


def test_apply_without_deletions_does_not_ask(guake, mocker):
    new = Server(name="new", host="new.lan", id="new", updated_at=NOW)
    dialog = open_dialog(guake, collected(reachable("laptop", [new])))
    confirm = mocker.patch.object(dialog, "confirm")
    assert dialog.apply()
    confirm.assert_not_called()
    assert guake.servers.get("new") is not None


def _dialog_with_ticked_deletion(guake):
    dialog = open_dialog(
        guake, collected(reachable("laptop", deleted={"db": Tombstone(at=NOW, name="db")}))
    )
    dialog.on_toggled(None, "0")
    return dialog


def test_declined_deletion_changes_nothing(guake, mocker):
    dialog = _dialog_with_ticked_deletion(guake)
    mocker.patch.object(dialog, "confirm", return_value=False)
    assert not dialog.apply()
    assert guake.servers.get("db") is not None


def test_confirmed_deletion_removes_server_and_password(guake, mocker):
    dialog = _dialog_with_ticked_deletion(guake)
    confirm = mocker.patch.object(dialog, "confirm", return_value=True)
    clear = mocker.patch("guake.serversyncdialogs.serversecrets.clear_password")
    assert dialog.apply()
    assert [c.server.name for c in confirm.call_args[0][0]] == ["db"]
    assert guake.servers.get("db") is None
    clear.assert_called_once_with("db")


def test_untick_everything_disables_apply(guake):
    new = Server(name="new", host="new.lan", id="new", updated_at=NOW)
    dialog = open_dialog(guake, collected(reachable("laptop", [new])))
    dialog.on_toggled(None, "0")
    assert not dialog.apply_button.get_sensitive()


def test_result_arriving_after_close_is_ignored(guake):
    with mock.patch.object(SyncDialog, "refresh"):
        dialog = SyncDialog(guake.window, guake)
    dialog.destroy()
    assert dialog.show_collected(collected(), None) is False


def test_background_fetch_reports_errors(guake, mocker):
    def failing():
        raise sync.SyncError("boom")

    idle = mocker.patch("guake.serversyncdialogs.GLib.idle_add")
    with mock.patch.object(SyncDialog, "refresh"):
        dialog = SyncDialog(guake.window, guake, collect=failing)
    dialog._fetch()  # pylint: disable=protected-access
    idle.assert_called_once_with(dialog.show_collected, None, "boom")


def test_add_shows_command_and_warning():
    change = sync.plan_sync(
        [],
        {},
        {"laptop": ServersFile([Server(name="w", host="h", command="htop", updated_at=1)], {})},
    )[0]
    _, text = dialogs.describe_change(change)
    assert "remote command: htop" in text and "⚠" in text


def test_ticked_flagged_change_must_be_confirmed(guake, mocker):
    evil = Server(name="db", host="evil.lan", id="db", updated_at=NOW, use_password=True)
    dialog = open_dialog(guake, collected(reachable("laptop", [evil])))
    assert [row[dialogs.COL_APPLY] for row in dialog.model] == [False]
    dialog.on_toggled(None, "0")
    confirm = mocker.patch.object(dialog, "confirm", return_value=False)
    assert not dialog.apply()
    assert "password" in confirm.call_args[0][0][0].warning
    assert guake.servers.get("db").host == "db.lan"


def test_refused_servers_are_reported():
    result = sync.PeerResult(
        sync.Device("laptop", "100.1.1.2", True), sync.Snapshot(ServersFile([], {}), 2), ""
    )
    assert "2 refused" in dialogs.device_summary(result)
