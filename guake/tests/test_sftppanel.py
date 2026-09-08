# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name
"""GTK tests for the SFTP panel, driven against the local sftp-server (no
network, no ssh). Dialogs are replaced by stubs so nothing blocks."""

import os
import time

import gi
import pytest

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from guake import sftpdialogs
from guake.sftp import SftpSession
from guake.sftppanel import TCOL_STATUS
from guake.sftppanel import SftpPanel
from guake.tests.test_sftp import local_sftp_server
from guake.tests.test_sftp import needs_sftp

pytestmark = needs_sftp


def pump(predicate=lambda: False, timeout=10):
    """Run the main loop until ``predicate`` holds (or the timeout passes);
    returns whether it held."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / "remote"
    (root / "sub dir").mkdir(parents=True)
    (root / "sub dir" / "a file.txt").write_text("hi")
    (root / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    return root


@pytest.fixture
def dialogs(monkeypatch, tmp_path):
    """Stub every dialog: confirmations say yes, choosers pick tmp_path/dl,
    text prompts answer from ``answers``; errors are collected."""
    download_dir = tmp_path / "dl"
    download_dir.mkdir()
    state = {"answers": {}, "errors": [], "download_dir": str(download_dir), "confirm": True}
    monkeypatch.setattr(sftpdialogs, "confirm", lambda *a, **k: state["confirm"])
    monkeypatch.setattr(sftpdialogs, "choose_folder", lambda *a, **k: state["download_dir"])
    monkeypatch.setattr(
        sftpdialogs,
        "ask_text",
        lambda window, title, label, default="", ok_label=None: state["answers"].get(title),
    )
    monkeypatch.setattr(
        sftpdialogs, "show_error", lambda window, text, secondary=None: state["errors"].append(text)
    )
    return state


@pytest.fixture
def panel(remote, dialogs):
    window = Gtk.Window()
    closed = []
    panel = SftpPanel(
        window,
        "test server",
        lambda handler: SftpSession(["sftp", "-D", local_sftp_server()], prompt_handler=handler),
        closed.append,
    )
    panel.closed = closed
    window.add(panel)
    window.show_all()
    assert pump(lambda: panel.current_dir is not None)
    panel.load(str(remote))
    assert pump(lambda: panel.current_dir == str(remote))
    yield panel
    panel.shutdown()
    window.destroy()


def names(panel):
    return [row[1] for row in panel.model]


def select(panel, *wanted):
    selection = panel.view.get_selection()
    selection.unselect_all()
    for name in wanted:
        selection.select_path(Gtk.TreePath.new_from_indices([names(panel).index(name)]))


def test_listing_shows_directories_first_with_sizes(panel):
    assert names(panel) == ["sub dir", "big.bin"]
    assert panel.model[0][2] == ""
    assert panel.model[1][2] == "2.0 MB"
    assert panel.path_entry.get_text() == panel.current_dir
    assert panel.status.get_text() == "2 items"


def test_activating_a_directory_enters_it_and_up_goes_back(panel, remote):
    panel._on_row_activated(panel.view, Gtk.TreePath.new_first(), None)
    assert pump(lambda: panel.current_dir == str(remote / "sub dir"))
    assert names(panel) == ["a file.txt"]
    panel.go_up()
    assert pump(lambda: panel.current_dir == str(remote))


def test_bad_path_keeps_listing_and_reports_in_status(panel):
    panel.load("/nonexistent/guake-sftp-test")
    assert pump(lambda: "No such file" in panel.status.get_text())
    assert names(panel) == ["sub dir", "big.bin"]


def test_new_folder_and_rename_and_delete(panel, remote, dialogs):
    dialogs["answers"] = {"New folder": "made dir", "Rename": "renamed dir"}
    panel.new_folder()
    assert pump(lambda: "made dir" in names(panel))
    select(panel, "made dir")
    panel.rename_selected()
    assert pump(lambda: "renamed dir" in names(panel))
    assert (remote / "renamed dir").is_dir()
    select(panel, "renamed dir", "sub dir")
    panel.delete_selected()
    assert pump(lambda: names(panel) == ["big.bin"])
    assert not (remote / "sub dir").exists()
    assert dialogs["errors"] == []


def test_delete_is_skipped_when_not_confirmed(panel, remote, dialogs):
    dialogs["confirm"] = False
    select(panel, "big.bin")
    panel.delete_selected()
    pump(timeout=0.5)
    assert (remote / "big.bin").exists()


def test_download_selected_reports_progress_and_done(panel, remote, dialogs):
    select(panel, "big.bin", "sub dir")
    panel.download_selected()
    assert len(panel.transfer_model) == 2
    assert pump(lambda: all(row[TCOL_STATUS] == "Done" for row in panel.transfer_model))
    download_dir = dialogs["download_dir"]
    assert os.path.getsize(os.path.join(download_dir, "big.bin")) == 2 * 1024 * 1024
    assert open(os.path.join(download_dir, "sub dir", "a file.txt")).read() == "hi"
    assert panel.transfer_model[0][2] == 100


def test_upload_paths_refreshes_listing(panel, remote, tmp_path):
    local = tmp_path / "up load.txt"
    local.write_text("up")
    folder = tmp_path / "up dir"
    folder.mkdir()
    (folder / "inner.txt").write_text("in")
    panel.upload_paths([str(local), str(folder)])
    assert pump(lambda: all(row[TCOL_STATUS] == "Done" for row in panel.transfer_model))
    assert pump(lambda: "up load.txt" in names(panel) and "up dir" in names(panel))
    assert (remote / "up dir" / "inner.txt").read_text() == "in"


def test_clear_finished_removes_done_rows(panel, remote, dialogs):
    select(panel, "big.bin")
    panel.download_selected()
    assert pump(lambda: panel.transfer_model[0][TCOL_STATUS] == "Done")
    panel.clear_finished()
    assert len(panel.transfer_model) == 0


def test_close_calls_back_and_stops_sessions(panel):
    panel.close()
    assert panel.closed == [panel]
    panel.shutdown()
    assert pump(lambda: not panel.browser.session.is_alive, 5)


def test_prompt_from_worker_is_answered_through_dialog(monkeypatch, tmp_path):
    fake = tmp_path / "fake-sftp"
    fake.write_text(
        "#!/bin/sh\n"
        "printf 'Password: ' > /dev/tty\n"
        "read pw < /dev/tty\n"
        "while read line; do\n"
        "  printf 'sftp> %s\\n' \"$line\"\n"
        '  case "$line" in\n'
        "    -pwd) printf 'Remote working directory: /home/%s\\n' \"$pw\" ;;\n"
        "    lpwd) printf 'Local working directory: /x\\n' ;;\n"
        "    '-cd '*) ;;\n"
        "    '-ls '*) printf -- '-rw-r--r--    ? 0 0 3 Sep  8 20:18 hello.txt\\n' ;;\n"
        "    bye) exit 0 ;;\n"
        "  esac\n"
        "done\n"
    )
    fake.chmod(0o755)
    asked = []

    def fake_question(window, server_name, text, secret):
        asked.append((server_name, text, secret))
        return "hunter2"

    monkeypatch.setattr(sftpdialogs, "ask_question", fake_question)
    window = Gtk.Window()
    panel = SftpPanel(
        window,
        "fake",
        lambda handler: SftpSession([str(fake)], prompt_handler=handler),
        lambda p: None,
    )
    window.add(panel)
    window.show_all()
    assert pump(lambda: panel.current_dir == "/home/hunter2")
    assert asked == [("fake", "Password:", True)]
    assert names(panel) == ["hello.txt"]
    panel.shutdown()
    window.destroy()
