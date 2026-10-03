# -*- coding: utf-8; -*-
"""
Colours for notebook tabs: the palette offered in the tab and server menus,
a stable automatic colour per server, and the CSS that paints a tab.

The tab itself is a node of the GtkNotebook and cannot be styled one by one,
so the colour goes on the tab's label box (see
:class:`guake.boxes.TabLabelEventBox`): a flat transparent label with a bar on the edge
facing the terminal, bolder on the current tab.
"""

import hashlib
import re

from typing import List
from typing import Tuple

COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

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
INACTIVE_BAR_ALPHA = 0.55


def is_valid_color(value: str) -> bool:
    return bool(value) and bool(COLOR_PATTERN.match(value))


def auto_color(key: str) -> str:
    """A palette colour picked from ``key`` (a server id or name), the same
    one on every run and every machine."""
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)][1]


def rgba(color: str, alpha: float) -> str:
    red, green, blue = bytes.fromhex(color[1:])
    return f"rgba({red}, {green}, {blue}, {alpha})"


def tab_css(color: str, active: bool, tabs_at_bottom: bool) -> str:
    """CSS for a tab label box. The bar sits on the edge next to the
    terminal. Without a colour the edge stays transparent so every tab keeps
    the same height."""
    edge = "top" if tabs_at_bottom else "bottom"
    if is_valid_color(color):
        bar = rgba(color, ACTIVE_BAR_ALPHA if active else INACTIVE_BAR_ALPHA)
    else:
        bar = "transparent"
    return (
        ".guake-tab-label {"
        f" border-{edge}: {ACCENT_WIDTH_PX}px solid {bar};"
        " background-color: transparent;"
        " border-radius: 0;"
        " padding: 6px 12px;"
        " }"
    )
