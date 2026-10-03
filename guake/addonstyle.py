# -*- coding: utf-8; -*-
"""
Look of the servers and SFTP add-on: a Visual Studio inspired palette, the
bundled Codicons and the style sheet that applies them.

The style sheet only reaches widgets marked with :func:`mark` (the servers
window, its dialogs and the SFTP panel) and the tab strip, so the rest of
Guake keeps the GTK theme chosen in the preferences.

The icons are the Visual Studio Code Codicons (CC BY 4.0, Microsoft),
installed with Guake's pixmaps as ``guake-<name>-symbolic.svg``.
"""

import logging

from string import Template
from typing import NamedTuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk
from gi.repository import Gtk

from guake.paths import IMAGE_DIR

log = logging.getLogger(__name__)

ADDON_CLASS = "guake-addon"
ICON_PREFIX = "guake-"
ICON_SUFFIX = "-symbolic"
UI_FONT = '"Segoe UI", "Inter", "Noto Sans", "Ubuntu", "Cantarell", sans-serif'
MONO_FONT = (
    '"Cascadia Code", "Cascadia Mono", "JetBrains Mono", "Fira Code", '
    '"DejaVu Sans Mono", monospace'
)

# Stock icons used when a bundled one cannot be found (a broken install).
FALLBACK_ICONS = {
    "server": "network-server-symbolic",
    "remote-explorer": "folder-remote-symbolic",
    "folder": "folder-symbolic",
    "file": "text-x-generic-symbolic",
    "close": "window-close-symbolic",
    "add": "list-add-symbolic",
    "ellipsis": "open-menu-symbolic",
}
DEFAULT_FALLBACK_ICON = "image-missing"
LOOKUP_SIZE = 16


class Palette(NamedTuple):
    surface: str  # lists and inputs
    panel: str  # window and panel background
    bar: str  # title bars, tool bars, section headers
    border: str
    text: str
    strong: str
    muted: str
    hover: str
    selection: str
    selection_text: str
    selection_unfocused: str
    input: str
    input_border: str
    button: str
    button_hover: str
    accent: str
    accent_hover: str
    danger: str
    scrollbar: str


DARK = Palette(
    surface="#1e1e1e",
    panel="#252526",
    bar="#2d2d30",
    border="#3f3f46",
    text="#cccccc",
    strong="#f1f1f1",
    muted="#9d9d9d",
    hover="#2a2d2e",
    selection="#04395e",
    selection_text="#ffffff",
    selection_unfocused="#37373d",
    input="#313131",
    input_border="#3c3c3c",
    button="#3a3d41",
    button_hover="#45494e",
    accent="#0078d4",
    accent_hover="#1a86dc",
    danger="#c42b1c",
    scrollbar="rgba(121, 121, 121, 0.4)",
)

LIGHT = Palette(
    surface="#ffffff",
    panel="#f8f8f8",
    bar="#eeeef2",
    border="#d4d4dc",
    text="#3b3b3b",
    strong="#1f1f1f",
    muted="#6c6c6c",
    hover="#e8e8e8",
    selection="#0060c0",
    selection_text="#ffffff",
    selection_unfocused="#e4e6f1",
    input="#ffffff",
    input_border="#cecece",
    button="#e5e5e5",
    button_hover="#d6d6d6",
    accent="#005fb8",
    accent_hover="#0258a8",
    danger="#c42b1c",
    scrollbar="rgba(100, 100, 100, 0.4)",
)

_CSS = Template(
    """
.guake-addon { font-family: $ui_font; }
.guake-addon.background, .guake-addon .guake-panel {
    background-color: $panel; color: $text;
}
.guake-addon label.dim-label, .guake-addon .dim-label { color: $muted; opacity: 1; }
.guake-addon .guake-mono { font-family: $mono_font; font-size: 0.92em; }
.guake-addon .guake-heading { color: $strong; font-weight: 600; }

.guake-addon headerbar {
    background-image: none; background-color: $bar; color: $strong;
    border: none; border-bottom: 1px solid $border; border-radius: 0;
    box-shadow: none; min-height: 38px; padding: 0 6px;
}
.guake-addon headerbar .title { font-weight: 600; color: $strong; }
.guake-addon headerbar .subtitle { color: $muted; }

.guake-addon button {
    background-image: none; background-color: $button; color: $text;
    border: 1px solid transparent; border-radius: 2px; box-shadow: none;
    text-shadow: none; -gtk-icon-shadow: none; padding: 4px 12px; min-height: 20px;
}
.guake-addon button.text-button.image-button image { margin-right: 5px; }
.guake-addon button:hover { background-color: $button_hover; color: $strong; }
.guake-addon button:focus { border-color: $accent; }
.guake-addon button:disabled { color: $muted; opacity: 0.55; }
.guake-addon button.suggested-action {
    background-color: $accent; color: #ffffff; border-color: $accent;
}
.guake-addon button.suggested-action:hover { background-color: $accent_hover; color: #ffffff; }
.guake-addon button.destructive-action {
    background-color: $danger; color: #ffffff; border-color: $danger;
}
.guake-addon button.flat, .guake-addon headerbar button,
.guake-addon spinbutton button {
    background-color: transparent; border-color: transparent; padding: 4px 6px;
}
.guake-addon button.flat:hover, .guake-addon headerbar button:hover,
.guake-addon spinbutton button:hover {
    background-color: alpha($text, 0.14); color: $strong;
}
.guake-addon button.flat:checked, .guake-addon button.flat:active,
.guake-addon headerbar button:checked, .guake-addon headerbar button:active {
    background-color: alpha($text, 0.22);
}
.guake-addon headerbar button.titlebutton { padding: 4px; }

.guake-addon entry, .guake-addon spinbutton {
    background-image: none; background-color: $input; color: $text;
    border: 1px solid $input_border; border-radius: 2px; box-shadow: none;
    caret-color: $strong; min-height: 26px;
}
.guake-addon spinbutton entry { border: none; min-height: 24px; }
.guake-addon entry:focus, .guake-addon spinbutton:focus { border-color: $accent; }
.guake-addon entry:disabled { color: $muted; }
.guake-addon entry image { color: $muted; }
.guake-addon entry selection, .guake-addon label selection {
    background-color: $selection; color: $selection_text;
}

.guake-addon scrolledwindow, .guake-addon frame > border {
    border-color: $border; border-radius: 0;
}
.guake-addon treeview.view {
    background-color: $surface; color: $text; border-color: $border;
}
.guake-addon treeview.view:hover { background-color: $hover; }
.guake-addon treeview.view:selected { background-color: $selection_unfocused; color: $strong; }
.guake-addon treeview.view:selected:focus {
    background-color: $selection; color: $selection_text;
}
.guake-addon treeview.view header button {
    background-color: $bar; color: $muted; border: none;
    border-bottom: 1px solid $border; border-radius: 0;
    font-size: 0.85em; font-weight: 600; padding: 3px 8px;
}
.guake-addon treeview.view header button:hover { color: $strong; }
.guake-addon treeview.view.trough { background-color: alpha($text, 0.12); border-radius: 0; }
.guake-addon treeview.view.progressbar {
    background-image: none; background-color: $accent; color: #ffffff;
    border-radius: 0; box-shadow: none; border: none;
}

.guake-addon .dialog-action-area { padding: 4px 16px 14px 16px; }
.guake-addon .dialog-action-area button { margin-left: 8px; min-width: 64px; }
.guake-addon separator { background-color: $border; min-height: 1px; min-width: 1px; }
.guake-addon scrollbar { background-color: transparent; border: none; }
.guake-addon scrollbar slider {
    background-color: $scrollbar; border: 2px solid transparent; border-radius: 0;
    min-width: 8px; min-height: 8px;
}
.guake-addon scrollbar slider:hover { background-color: alpha($text, 0.5); }

.guake-addon .guake-toolbar {
    background-color: $bar; border-bottom: 1px solid $border; padding: 3px 6px;
}
.guake-addon .guake-panel-title {
    background-color: $bar; border-bottom: 1px solid $border;
    border-left: 3px solid transparent; padding: 5px 4px 5px 9px;
}
.guake-addon .guake-search { background-color: $panel; padding: 8px 10px; }
.guake-addon .guake-section {
    background-color: $bar; border-top: 1px solid $border;
    border-bottom: 1px solid $border; padding: 2px 4px 2px 12px;
}
.guake-addon .guake-section > label, .guake-addon label.guake-section-title {
    color: $strong; font-size: 0.82em; font-weight: 700; letter-spacing: 0.6px;
}
.guake-addon label.guake-section-title {
    border-bottom: 1px solid $border; padding: 10px 0 4px 0;
}
.guake-addon .guake-crumbs { background-color: $panel; padding: 1px 6px; }
.guake-addon .guake-crumbs button {
    background-color: transparent; border: none; color: $muted;
    padding: 2px 4px; min-height: 18px;
}
.guake-addon .guake-crumbs button:hover { background-color: transparent; color: $strong; }
.guake-addon .guake-crumbs button.guake-current { color: $strong; }
.guake-addon .guake-crumbs image { color: $muted; }
.guake-addon .guake-statusbar {
    background-color: $accent; color: #ffffff; padding: 2px 10px; font-size: 0.88em;
}
.guake-addon .guake-hint { color: $muted; padding: 14px 12px; }
.guake-addon actionbar > revealer > box {
    background-color: $bar; border-top: 1px solid $border; padding: 6px 10px;
}
.guake-addon button.guake-swatch {
    background-color: transparent; border: 2px solid transparent; border-radius: 3px;
    padding: 3px; min-height: 0; min-width: 0;
}
.guake-addon button.guake-swatch:hover { background-color: alpha($text, 0.14); }
.guake-addon button.guake-swatch:checked { border-color: $strong; }

.guake-sftp-panel { border-left: 1px solid $border; }

#notebook-teminals > header {
    background-image: none; background-color: $bar; color: $muted;
    border: none; padding: 0; margin: 0; box-shadow: none;
}
#notebook-teminals > header > tabs { padding: 0; margin: 0; }
#notebook-teminals > header > tabs > tab {
    background-image: none; background-color: transparent; color: $muted;
    border: none; border-right: 1px solid $border; border-radius: 0;
    padding: 0; margin: 0; box-shadow: none; font-family: $ui_font;
}
#notebook-teminals > header > tabs > tab:hover { background-color: $hover; color: $text; }
#notebook-teminals > header > tabs > tab:checked { background-color: $surface; color: $strong; }
#notebook-teminals > header button, .guake-tab-label button {
    background-image: none; background-color: transparent; border: none;
    border-radius: 2px; box-shadow: none; padding: 4px; color: $muted;
}
#notebook-teminals > header button:hover, .guake-tab-label button:hover {
    background-color: alpha($text, 0.14); color: $strong;
}
"""
)


def palette(dark):
    return DARK if dark else LIGHT


def addon_css(dark):
    """The add-on style sheet for a dark or a light GTK theme."""
    return _CSS.substitute(ui_font=UI_FONT, mono_font=MONO_FONT, **palette(dark)._asdict())


def mark(widget):
    """Give ``widget`` (a window or a panel) the add-on look. Returns it."""
    widget.get_style_context().add_class(ADDON_CLASS)
    return widget


# -- icons ---------------------------------------------------------------------

_icons_registered = False


def register_icons():
    """Let the icon theme find the bundled icons. Safe to call repeatedly."""
    global _icons_registered
    if _icons_registered:
        return
    theme = Gtk.IconTheme.get_default()
    if theme is None:
        return
    theme.append_search_path(IMAGE_DIR)
    _icons_registered = True


def icon_name(name):
    """Icon theme name of the bundled icon ``name`` (e.g. ``"server"``),
    or of a stock icon when it is missing."""
    register_icons()
    bundled = f"{ICON_PREFIX}{name}{ICON_SUFFIX}"
    theme = Gtk.IconTheme.get_default()
    # has_icon() ignores icons that sit directly in a search path.
    if theme is None or theme.lookup_icon(bundled, LOOKUP_SIZE, 0) is not None:
        return bundled
    log.warning("Bundled icon %s not found in %s", bundled, IMAGE_DIR)
    return FALLBACK_ICONS.get(name, DEFAULT_FALLBACK_ICON)


def image(name, size=Gtk.IconSize.MENU):
    return Gtk.Image.new_from_icon_name(icon_name(name), size)


def colored_icon(name, color, size, scale=1):
    """The bundled icon ``name`` painted in ``color`` (``#rrggbb``), as a
    cairo surface for a cell renderer. None when it cannot be loaded."""
    theme = Gtk.IconTheme.get_default()
    if theme is None:
        return None
    info = theme.lookup_icon_for_scale(icon_name(name), size, scale, Gtk.IconLookupFlags.FORCE_SIZE)
    if info is None:
        return None
    rgba = Gdk.RGBA()
    if not rgba.parse(color):
        return None
    try:
        pixbuf, _was_symbolic = info.load_symbolic(rgba, None, None, None)
    except gi.repository.GLib.Error as e:
        log.warning("Cannot load icon %s: %s", name, e)
        return None
    return Gdk.cairo_surface_create_from_pixbuf(pixbuf, scale, None)


# -- installing ----------------------------------------------------------------

_provider = None
_connected = False


def is_dark(style_context, settings):
    """Whether Guake currently shows a dark GTK theme."""
    if settings.general.get_boolean("gtk-use-system-default-theme"):
        found, background = style_context.lookup_color("theme_bg_color")
        return bool(found) and (background.red + background.green + background.blue) / 3 < 0.5
    return settings.general.get_boolean("gtk-prefer-dark-theme")


def install(style_context, settings):
    """Load the add-on style sheet and keep it in step with the GTK theme
    (light or dark) chosen in the preferences."""
    global _provider, _connected
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    register_icons()
    if _provider is None:
        _provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            screen, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def refresh(*_args):
        _provider.load_from_data(addon_css(is_dark(style_context, settings)).encode())

    if not _connected:
        gtk_settings = Gtk.Settings.get_default()
        for prop in ("gtk-theme-name", "gtk-application-prefer-dark-theme"):
            gtk_settings.connect("notify::" + prop, refresh)
        for key in ("gtk-use-system-default-theme", "gtk-prefer-dark-theme", "gtk-theme-name"):
            settings.general.connect("changed::" + key, refresh)
        _connected = True
    refresh()
