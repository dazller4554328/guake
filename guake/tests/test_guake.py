# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

import json
import os
import time

from pathlib import Path

import gi
import pytest

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import guake.guake_app

from guake.common import pixmapfile
from guake.guake_app import Guake
from guake.servers import Server


@pytest.fixture
def g(mocker, fs):
    mocker.patch("guake.guake_app.Guake.get_xdg_config_directory", return_value=Path("/foobar"))
    mocker.patch("guake.guake_app.shutil.copy", create=True)
    mocker.patch("guake.guake_app.notifier.showMessage", create=True)
    mocker.patch("guake.guake_app.traceback.print_exc", create=True)
    fs.pause()
    g = Guake()
    fs.add_real_file(pixmapfile("guake-notification.png"))
    fs.resume()
    return g


# Accel Test


def test_accel_search_terminal(g):
    nb = g.get_notebook()
    page = nb.get_nth_page(0)
    assert not page.search_revealer.get_reveal_child()

    g.accel_search_terminal()
    assert page.search_revealer.get_reveal_child()


def test_accel_search_terminal_debounce(g):
    nb = g.get_notebook()
    page = nb.get_nth_page(0)
    assert not page.search_revealer.get_reveal_child()

    g.prev_accel_search_terminal_time = time.time()
    g.accel_search_terminal()
    assert not page.search_revealer.get_reveal_child()


def test_accel_quit_without_prompt(mocker, g):
    # Disable quit prompt
    mocker.patch.object(g.settings.general, "get_boolean", return_value=False)
    mocker.patch("guake.guake_app.Gtk.main_quit")

    g.accel_quit()
    assert guake.guake_app.Gtk.main_quit.call_count == 1


def test_accel_quit_with_prompt(mocker, g):
    # Enable quit prompt
    mocker.patch.object(g.settings.general, "get_boolean", return_value=True)
    mocker.patch("guake.guake_app.PromptQuitDialog")
    mocker.patch("guake.guake_app.Gtk.main_quit")

    g.accel_quit()
    assert guake.guake_app.Gtk.main_quit.call_count == 1


# Save/Restore Tabs


def test_guake_restore_tabs(g, fs):
    d1 = fs.create_dir("/foobar/foo")
    d2 = fs.create_dir("/foobar/bar")
    d3 = fs.create_dir("/foobar/foo/foo")
    d4 = fs.create_dir("/foobar/foo/bar")
    session = {
        "schema_version": 1,
        "timestamp": 1556092197,
        "workspace": {
            "0": [
                [
                    {"directory": d1.path, "label": "1", "custom_label_set": True},
                    {"directory": d2.path, "label": "2", "custom_label_set": True},
                    {"directory": d3.path, "label": d3.path, "custom_label_set": False},
                ]
            ],
            "1": [[{"directory": d4.path, "label": "4", "custom_label_set": True}]],
        },
    }

    fn = fs.create_file("/foobar/session.json")
    with open(fn.path, "w", encoding="utf-8") as f:
        f.write(json.dumps(session))

    g.restore_tabs(fn.name)
    nb = g.notebook_manager.get_notebook(0)
    assert nb.get_n_pages() == 3
    assert nb.get_tab_text_index(0) == "1"
    assert nb.get_tab_text_index(1) == "2"

    nb = g.notebook_manager.get_notebook(1)
    assert nb.get_n_pages() == 1
    assert nb.get_tab_text_index(0) == "4"


def test_guake_restore_tabs_json_without_schema_version(g, fs):
    guake.guake_app.notifier.showMessage.reset_mock()

    fn = fs.create_file("/foobar/bar.json")
    with open(fn.path, "w", encoding="utf-8") as f:
        f.write("{}")

    g.restore_tabs(fn.name)
    assert guake.guake_app.notifier.showMessage.call_count == 1


def test_guake_restore_tabs_with_higher_schema_version(g, fs):
    guake.guake_app.notifier.showMessage.reset_mock()

    fn = fs.create_file("/foobar/bar.json")
    with open(fn.path, "w", encoding="utf-8") as f:
        f.write('{"schema_version": 2147483647}')

    g.restore_tabs(fn.name)
    assert guake.guake_app.notifier.showMessage.call_count == 1


def test_guake_restore_tabs_json_broken_session_file(g, fs):
    guake.guake_app.notifier.showMessage.reset_mock()
    fn = fs.create_file("/foobar/foobar.json")
    with open(fn.path, "w", encoding="utf-8") as f:
        f.write("{")

    g.restore_tabs(fn.name)
    assert guake.guake_app.shutil.copy.call_count == 1
    assert guake.guake_app.notifier.showMessage.call_count == 1


def test_guake_restore_tabs_schema_broken_session_file(g, fs):
    guake.guake_app.notifier.showMessage.reset_mock()

    fn = fs.create_file("/foobar/bar.json")
    d = fs.create_dir("/foobar/foo")
    with open(fn.path, "w", encoding="utf-8") as f:
        f.write(f'{{"schema_version": 1, "workspace": {{"0": [[{{"directory": "{d.path}"}}]]}}}}')

    g.restore_tabs(fn.name)
    assert guake.guake_app.shutil.copy.call_count == 1
    assert guake.guake_app.traceback.print_exc.call_count == 1


def test_guake_save_tabs_and_restore(mocker, g, fs):
    # Disable auto save
    mocker.patch.object(g.settings.general, "get_boolean", return_value=False)

    # Save
    assert not os.path.exists("/foobar/session.json")
    g.add_tab()
    g.rename_current_tab("foobar", True)
    g.add_tab()
    g.rename_current_tab("python", True)
    assert g.get_notebook().get_n_pages() == 3

    g.save_tabs()
    assert os.path.exists("/foobar")
    assert os.path.exists("/foobar/session.json")

    # Restore prepare
    g.close_tab()
    g.close_tab()
    assert g.get_notebook().get_n_pages() == 1

    # Restore
    g.restore_tabs()
    nb = g.get_notebook()
    assert nb.get_n_pages() == 3
    assert nb.get_tab_text_index(1) == "foobar"
    assert nb.get_tab_text_index(2) == "python"


def test_guake_hide_tab_bar_if_one_tab(mocker, g, fs):
    # Set hide-tabs-if-one-tab to True
    mocker.patch.object(g.settings.general, "get_boolean", return_value=True)

    g.settings.general.set_boolean("hide-tabs-if-one-tab", True)
    assert g.get_notebook().get_n_pages() == 1
    assert g.get_notebook().get_property("show-tabs") is False


def test_load_cwd_guake_yml_not_found_error(g):
    vte = g.get_notebook().get_current_terminal()
    assert g.fm.read_yaml("/foo/.guake.yml") is None
    assert g.load_cwd_guake_yaml(vte) == {}


def test_load_cwd_guake_yml_encoding_error(g, mocker, fs):
    vte = g.get_notebook().get_current_terminal()
    mocker.patch.object(vte, "get_current_directory", return_value="/foo/")
    fs.create_file("/foo/.guake.yml", contents=b"\xfe\xf0[\xb1\x0b\xc1\x18\xda")
    assert g.fm.read_yaml("/foo/.guake.yml") is None
    assert g.load_cwd_guake_yaml(vte) == {}


def test_load_cwd_guake_yml_format_error(g, mocker, fs):
    vte = g.get_notebook().get_current_terminal()
    mocker.patch.object(vte, "get_current_directory", return_value="/foo/")
    fs.create_file("/foo/.guake.yml", contents=b"[[as]")
    assert g.fm.read_yaml("/foo/.guake.yml") is None
    assert g.load_cwd_guake_yaml(vte) == {}


def test_load_cwd_guake_yml(mocker, g, fs):
    vte = g.get_notebook().get_current_terminal()
    mocker.patch.object(vte, "get_current_directory", return_value="/foo/")

    f = fs.create_file("/foo/.guake.yml", contents="title: bar")
    assert g.load_cwd_guake_yaml(vte) == {"title": "bar"}

    # Cache in action.
    f.set_contents("title: foo")
    assert g.load_cwd_guake_yaml(vte) == {"title": "bar"}
    g.fm.clear()
    assert g.load_cwd_guake_yaml(vte) == {"title": "foo"}


def test_guake_compute_tab_title(mocker, g, fs):
    vte = g.get_notebook().get_current_terminal()
    mocker.patch.object(vte, "get_current_directory", return_value="/foo/")

    # Original title.
    assert g.compute_tab_title(vte) == "Terminal"

    # Change title.
    fs.create_file("/foo/.guake.yml", contents="title: bar")
    assert g.compute_tab_title(vte) == "bar"

    # Avoid loading the guake.yml
    mocker.patch.object(g.settings.general, "get_boolean", return_value=False)
    assert g.compute_tab_title(vte) == "Terminal"


# Saved servers


def test_connect_to_server_opens_tab_named_after_server(g):
    from guake.servers import Server

    server = Server(name="test-box", host="127.0.0.1", port=1, user="nobody")
    nb = g.get_notebook()
    before = nb.get_n_pages()

    g.connect_to_server(server)

    assert nb.get_n_pages() == before + 1
    assert nb.get_tab_text_index(nb.get_current_page()) == "test-box"
    assert nb.get_current_terminal().server_id == server.id


def test_connect_to_server_by_name_reports_unknown_server(g):
    from guake.servers import Server

    assert g.connect_to_server_by_name("nope") is False
    g.servers.add(Server(name="box", host="127.0.0.1", port=1))
    assert g.connect_to_server_by_name("BOX") is True


def test_open_server_tab_reports_invalid_options_instead_of_failing_silently(g, mocker):
    from guake.servers import Server

    shown = mocker.patch.object(g, "show_server_error")
    nb = g.get_notebook()
    before = nb.get_n_pages()

    assert g.open_server_tab(Server(name="bad", host="h", options="-o 'oops")) is None

    assert nb.get_n_pages() == before
    assert shown.called


def test_server_tab_survives_save_and_restore(g):
    from guake.servers import Server

    server = g.servers.add(Server(name="test-box", host="127.0.0.1", port=1))[0]
    nb = g.get_notebook()
    g.connect_to_server(server)

    g.save_tabs()
    session = json.loads((Path("/foobar") / "session.json").read_text(encoding="utf-8"))
    saved_ids = [
        pane.get("server_id")
        for frames in session["workspace"].values()
        for tabs in frames
        for tab in tabs
        for pane in tab["panes"]
    ]
    assert server.id in saved_ids

    g.restore_tabs()

    labels = [nb.get_tab_text_index(i) for i in range(nb.get_n_pages())]
    assert "test-box" in labels
    restored = nb.get_terminals_for_page(labels.index("test-box"))[0]
    assert restored.server_id == server.id


# SFTP panel


class StubSftpPanel(Gtk.Box):
    """Stands in for the real panel: no sftp process, just the wiring."""

    def __init__(self, window, server_name, session_factory, on_close):
        super().__init__()
        self.server_name = server_name
        self.session_factory = session_factory
        self.on_close = on_close
        self.view = Gtk.TreeView()
        self.add(self.view)

    def close(self):
        self.on_close(self)


@pytest.fixture
def stub_panel(mocker):
    mocker.patch("guake.boxes.SftpPanel", StubSftpPanel)
    return StubSftpPanel


def test_open_sftp_panel_places_panel_next_to_terminal(g, stub_panel):
    server = Server(name="web", host="10.0.0.5", user="root", id="web")
    g.servers.add(server)
    root = g.current_root_box()
    assert root.sftp_panel is None
    assert root.paned.get_child1() is root.get_child()

    panel = g.open_sftp_panel(server)
    assert isinstance(panel, stub_panel)
    assert root.sftp_panel is panel
    assert root.paned.get_child2() is panel
    assert panel.server_id == "web"
    assert panel.server_name == "web"

    # Opening the same server again keeps the panel; closing removes it.
    assert g.open_sftp_panel(server) is panel
    panel.close()
    assert root.sftp_panel is None
    assert root.paned.get_child2() is None


def test_toggle_sftp_panel_needs_a_server_tab(g, stub_panel):
    assert g.toggle_sftp_panel() is False
    server = Server(name="web", host="10.0.0.5", id="web")
    g.servers.add(server)
    g.get_notebook().get_current_terminal().server_id = "web"
    assert g.toggle_sftp_panel() is True
    assert g.current_root_box().sftp_panel is not None
    assert g.toggle_sftp_panel() is True
    assert g.current_root_box().sftp_panel is None


def test_open_sftp_panel_rejects_broken_options(g, stub_panel, mocker):
    mocker.patch.object(g, "show_server_error")
    server = Server(name="bad", host="h", options='-o "unterminated')
    assert g.open_sftp_panel(server) is None
    assert g.show_server_error.call_count == 1
    assert g.current_root_box().sftp_panel is None


def test_sftp_session_factory_uses_saved_password(g, mocker):
    mocker.patch("guake.guake_app.serversecrets.lookup_password", return_value="pw")
    server = Server(name="web", host="10.0.0.5", user="root", port=2200, use_password=True)
    session = g.sftp_session_factory(server)(lambda text, secret: None)
    assert session.argv == ["sftp", "-P", "2200", "root@10.0.0.5"]
    assert session._password == "pw"
    assert not session.is_alive
