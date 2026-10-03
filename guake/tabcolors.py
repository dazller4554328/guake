# -*- coding: utf-8; -*-
"""
Colours for notebook tabs: the palette offered in the tab, server and group
menus, a stable automatic colour per server or group, and the CSS that paints
a tab.

The tab itself is a node of the GtkNotebook and cannot be styled one by one,
so the colour goes on the tab's label box (see
:class:`guake.boxes.TabLabelEventBox`), which fills the tab: a tint of the
colour over the whole tab and a solid bar on the edge facing the terminal,
both stronger on the current tab.
"""

import hashlib
import re

from typing import List
from typing import Tuple

# \Z, not $: a trailing newline must not get through to the style sheet.
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}\Z")

# GNOME palette tones that stay readable on light and dark themes.
PALETTE: List[Tuple[str, str]] = [
    ("Blue", "#3584e4"),
    ("Teal", "#2190a4"),
    ("Green", "#3a944a"),
    ("Yellow", "#c88800"),
    ("Orange", "#ed5b00"),
    ("Red", "#e62d42"),
    ("Pink", "#d56199"),
    ("Purple", "#9141ac"),
    ("Slate", "#6f8396"),
    ("Brown", "#986a44"),
]

ACCENT_WIDTH_PX = 3
ACTIVE_BAR_ALPHA = 1.0
INACTIVE_BAR_ALPHA = 0.75
ACTIVE_TINT_ALPHA = 0.45
INACTIVE_TINT_ALPHA = 0.22
# Relative luminance above which black text reads better than white.
LIGHT_COLOR_LUMINANCE = 0.45
TAB_PADDING = "5px 10px 5px 12px"


def is_valid_color(value: str) -> bool:
    return bool(value) and bool(COLOR_PATTERN.match(value))


def auto_color(key: str) -> str:
    """A palette colour picked from ``key`` (a server id or a group name),
    the same one on every run and every machine."""
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)][1]


def rgba(color: str, alpha: float) -> str:
    red, green, blue = bytes.fromhex(color[1:])
    return f"rgba({red}, {green}, {blue}, {alpha})"


def luminance(color: str) -> float:
    """Relative luminance of ``color`` (WCAG), from 0 (black) to 1 (white)."""

    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(c) for c in bytes.fromhex(color[1:]))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def text_color_on(color: str) -> str:
    """Black or white, whichever reads better on a ``color`` background."""
    return "#000000" if luminance(color) > LIGHT_COLOR_LUMINANCE else "#ffffff"


def tab_css(color: str, active: bool, tabs_at_bottom: bool) -> str:
    """CSS for a tab label box. A coloured tab is tinted all over and gets a
    bar on the edge next to the terminal. Without a colour the edge stays
    transparent so every tab keeps the same height."""
    edge = "top" if tabs_at_bottom else "bottom"
    if is_valid_color(color):
        bar = rgba(color, ACTIVE_BAR_ALPHA if active else INACTIVE_BAR_ALPHA)
        tint = rgba(color, ACTIVE_TINT_ALPHA if active else INACTIVE_TINT_ALPHA)
    else:
        bar = tint = "transparent"
    return (
        ".guake-tab-label {"
        f" border-{edge}: {ACCENT_WIDTH_PX}px solid {bar};"
        f" background-color: {tint};"
        " border-radius: 0;"
        f" padding: {TAB_PADDING};"
        " }"
    )
