# -*- coding: utf-8 -*-

import pytest

from guake import tabcolors


def test_palette_colors_are_valid_hex():
    assert len(tabcolors.PALETTE) >= 8
    for _name, color in tabcolors.PALETTE:
        assert tabcolors.is_valid_color(color)


def test_auto_color_is_stable_and_from_the_palette():
    colors = {c for _n, c in tabcolors.PALETTE}
    assert tabcolors.auto_color("web-1") == tabcolors.auto_color("web-1")
    assert tabcolors.auto_color("web-1") in colors
    # Different servers spread over the palette.
    assert len({tabcolors.auto_color(f"server-{i}") for i in range(40)}) > 4


@pytest.mark.parametrize(
    "value,ok", [("#3584e4", True), ("#3584E4", True), ("", False), ("blue", False)]
)
def test_is_valid_color(value, ok):
    assert tabcolors.is_valid_color(value) is ok


def test_rgba_converts_hex_with_alpha():
    assert tabcolors.rgba("#ff8000", 0.5) == "rgba(255, 128, 0, 0.5)"


def test_tab_css_puts_the_accent_on_the_edge_facing_the_terminal():
    bottom = tabcolors.tab_css("#3584e4", active=True, tabs_at_bottom=True)
    top = tabcolors.tab_css("#3584e4", active=True, tabs_at_bottom=False)
    assert "border-top: 3px solid rgba(53, 132, 228, 1.0)" in bottom
    assert "border-bottom: 3px solid rgba(53, 132, 228, 1.0)" in top


def test_tab_css_inactive_is_quieter_than_active():
    active = tabcolors.tab_css("#3584e4", active=True, tabs_at_bottom=True)
    inactive = tabcolors.tab_css("#3584e4", active=False, tabs_at_bottom=True)
    assert "1.0)" in active and "1.0)" not in inactive


def test_tab_css_tints_the_whole_tab_more_strongly_when_active():
    active = tabcolors.tab_css("#3584e4", active=True, tabs_at_bottom=True)
    inactive = tabcolors.tab_css("#3584e4", active=False, tabs_at_bottom=True)
    assert f"background-color: rgba(53, 132, 228, {tabcolors.ACTIVE_TINT_ALPHA})" in active
    assert f"background-color: rgba(53, 132, 228, {tabcolors.INACTIVE_TINT_ALPHA})" in inactive
    assert tabcolors.ACTIVE_TINT_ALPHA > tabcolors.INACTIVE_TINT_ALPHA


@pytest.mark.parametrize(
    "color,expected",
    [
        ("#ffffff", "#000000"),
        ("#f6d32d", "#000000"),
        ("#000000", "#ffffff"),
        ("#9141ac", "#ffffff"),
    ],
)
def test_text_color_on_picks_the_readable_one(color, expected):
    assert tabcolors.text_color_on(color) == expected


def test_tab_css_without_color_keeps_a_transparent_edge():
    css = tabcolors.tab_css("", active=False, tabs_at_bottom=True)
    assert "border-top: 3px solid transparent" in css
    assert "background-color: transparent" in css
