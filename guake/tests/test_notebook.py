# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name

import pytest

from guake.notebook import TerminalNotebook


@pytest.fixture
def nb(mocker):
    targets = [
        "guake.notebook.TerminalNotebook.terminal_spawn",
        "guake.notebook.TerminalNotebook.terminal_attached",
        "guake.notebook.TerminalNotebook.guake",
        "guake.notebook.TerminalBox.set_terminal",
    ]
    for target in targets:
        mocker.patch(target, create=True)
    return TerminalNotebook()


def test_zero_page_notebook(nb):
    assert nb.get_n_pages() == 0


def test_add_one_page_to_notebook(nb):
    nb.new_page()
    assert nb.get_n_pages() == 1


def test_add_two_pages_to_notebook(nb):
    nb.new_page()
    nb.new_page()
    assert nb.get_n_pages() == 2


def test_remove_page_in_notebook(nb):
    nb.new_page()
    nb.new_page()
    assert nb.get_n_pages() == 2
    nb.remove_page(0)
    assert nb.get_n_pages() == 1
    nb.remove_page(0)
    assert nb.get_n_pages() == 0


def test_rename_page(nb):
    t1 = "foo"
    t2 = "bar"
    nb.new_page()
    nb.rename_page(0, t1, True)
    assert nb.get_tab_text_index(0) == t1
    nb.rename_page(0, t2, False)
    assert nb.get_tab_text_index(0) == t1
    nb.rename_page(0, t2, True)
    assert nb.get_tab_text_index(0) == t2


def test_add_new_page_with_focus_with_label(nb):
    t = "test_this_label"
    nb.new_page_with_focus(label=t)
    assert nb.get_n_pages() == 1
    assert nb.get_tab_text_index(0) == t


# --- tab colours -------------------------------------------------------------


def tab_label(nb, index):
    return nb.get_tab_label(nb.get_nth_page(index))


def test_plain_tab_has_no_colour_or_server_icon(nb):
    nb.new_page_with_focus(label="local")
    label = tab_label(nb, 0)
    assert label.color == ""
    assert not label.icon.get_visible()


def test_server_tab_gets_icon_tooltip_and_automatic_colour(nb):
    from guake.servers import Server
    from guake.tabcolors import auto_color

    nb.new_page_with_focus(label="web")
    label = tab_label(nb, 0)
    server = Server(name="web", host="h", user="u", id="abc")
    label.set_server(server)
    assert label.icon.get_visible()
    assert label.color == auto_color("abc")
    assert "u@h" in label.get_tooltip_text()

    label.set_server(server.with_changes(color="#e62d42"))
    assert label.color == "#e62d42"


def test_user_colour_wins_and_can_be_reset(nb):
    from guake.servers import Server

    nb.new_page_with_focus(label="web")
    label = tab_label(nb, 0)
    label.set_server(Server(name="web", host="h", color="#e62d42"))
    label.set_user_color("#3584e4")
    assert label.color == "#3584e4"
    label.set_user_color("")
    assert label.color == "#e62d42"


def test_only_the_current_tab_is_marked_active(nb):
    nb.new_page_with_focus(label="one")
    nb.new_page_with_focus(label="two")
    assert [tab_label(nb, i)._active for i in range(2)] == [False, True]
    nb.set_current_page(0)
    assert [tab_label(nb, i)._active for i in range(2)] == [True, False]
