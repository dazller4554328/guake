import itertools
import logging
import os

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib
from gi.repository import Gdk
from gi.repository import Gtk
from textwrap import dedent

from guake.paths import GUAKE_THEME_DIR

log = logging.getLogger(__name__)

# Reference:
# https://gitlab.gnome.org/GNOME/gnome-tweaks/blob/master/gtweak/utils.py (GPL)


def get_resource_dirs(resource):
    """Returns a list of all known resource dirs for a given resource.

    :param str resource:
        Name of the resource (e.g. "themes")
    :return:
        A list of resource dirs
    """
    dirs = [
        os.path.join(dir, resource)
        for dir in itertools.chain(
            GLib.get_system_data_dirs(), GUAKE_THEME_DIR, GLib.get_user_data_dir()
        )
    ]
    dirs += [os.path.join(os.path.expanduser("~"), f".{resource}")]

    return [Path(dir) for dir in dirs if os.path.isdir(dir)]


def list_all_themes():
    return sorted(
        {
            x.name
            for theme_dir in get_resource_dirs("themes")
            for x in theme_dir.iterdir()
            if x.is_dir()
        }
    )


def select_gtk_theme(settings):
    gtk_settings = Gtk.Settings.get_default()
    if settings.general.get_boolean("gtk-use-system-default-theme"):
        log.debug("Using system default theme")
        gtk_settings.reset_property("gtk-theme-name")
        gtk_settings.set_property("gtk-application-prefer-dark-theme", False)
        return

    gtk_theme_name = settings.general.get_string("gtk-theme-name")
    log.debug("Wanted GTK theme: %r", gtk_theme_name)
    gtk_settings.set_property("gtk-theme-name", gtk_theme_name)

    prefer_dark_theme = settings.general.get_boolean("gtk-prefer-dark-theme")
    log.debug("Prefer dark theme: %r", prefer_dark_theme)
    gtk_settings.set_property("gtk-application-prefer-dark-theme", prefer_dark_theme)


def get_gtk_theme(settings):
    gtk_theme_name = settings.general.get_string("gtk-theme-name")
    prefer_dark_theme = settings.general.get_boolean("gtk-prefer-dark-theme")
    return (gtk_theme_name, "dark" if prefer_dark_theme else None)


# One provider is reloaded on appearance changes, rather than stacking overrides.
_style_provider = None
_theme_notifications_connected = False


def appearance_css(dark):
    """Visual Studio inspired chrome; terminal colours remain user controlled."""
    colors = (
        ("#1e1e1e", "#252526", "#2d2d30", "#3e3e42", "#f0f0f0", "#b8b8bf", "#37373d", "#094771")
        if dark
        else (
            "#ffffff",
            "#f3f3f3",
            "#e8e8ec",
            "#ccccd2",
            "#202024",
            "#5f5f67",
            "#e0e0e5",
            "#cce8ff",
        )
    )
    base, panel, bar, border, fg, muted, hover, selection = colors
    return dedent(
        f"""
        window.background, dialog.background {{ background-color: {panel}; color: {fg}; }}
        headerbar, toolbar, menubar {{
            background-image: none; background-color: {bar}; color: {fg};
            border-color: {border}; box-shadow: none;
        }}
        button {{
            background-image: none; background-color: {panel}; color: {fg};
            border: 1px solid {border}; border-radius: 2px; box-shadow: none;
            text-shadow: none;
        }}
        button:hover {{ background-color: {hover}; }}
        button:checked, button:active {{ background-color: {selection}; }}
        button:disabled {{ color: {muted}; }}
        button.suggested-action {{
            background-color: #007acc; color: #ffffff; border-color: #007acc;
        }}
        button.suggested-action:hover {{ background-color: #008be5; }}
        entry, spinbutton, textview text {{
            background-color: {base}; color: {fg}; border-color: {border};
            border-radius: 2px; box-shadow: none;
        }}
        entry:focus, spinbutton:focus {{ border-color: #007acc; }}
        treeview, list, .guake-sftp-panel {{ background-color: {panel}; color: {fg}; }}
        treeview:selected, list row:selected {{ background-color: {selection}; color: {fg}; }}
        treeview header button {{ background-color: {bar}; color: {muted}; border-radius: 0; }}
        .dim-label {{ color: {muted}; opacity: 1; }}
        menu, popover, popover.background {{
            background-color: {panel}; color: {fg}; border-color: {border};
        }}
        menuitem:hover, modelbutton:hover {{ background-color: {selection}; color: {fg}; }}
        separator, paned > separator {{ background-color: {border}; }}
        scrollbar {{ background-color: {panel}; }}
        scrollbar slider {{ background-color: {border}; border: none; border-radius: 2px; }}
        scrollbar slider:hover {{ background-color: {muted}; }}
        #notebook-teminals > header {{
            background-image: none; background-color: {bar}; color: {muted};
            border: none; padding: 0; margin: 0; box-shadow: none;
        }}
        #notebook-teminals > header > tabs {{ padding: 0; margin: 0; }}
        #notebook-teminals > header > tabs > tab {{
            background-image: none; background-color: transparent; color: {muted};
            border: none; border-right: 1px solid {border}; border-radius: 0;
            padding: 0; margin: 0; box-shadow: none;
        }}
        #notebook-teminals > header > tabs > tab:hover {{
            background-color: {hover}; color: {fg};
        }}
        #notebook-teminals > header > tabs > tab:checked {{
            background-color: {base}; color: {fg};
        }}
        #notebook-teminals > header.top > tabs > tab:checked {{
            box-shadow: inset 0 2px #007acc;
        }}
        #notebook-teminals > header.bottom > tabs > tab:checked {{
            box-shadow: inset 0 -2px #007acc;
        }}
        #notebook-teminals > header button, .guake-tab-label button {{
            background-image: none; background-color: transparent; border: none;
            border-radius: 2px; box-shadow: none; padding: 4px; color: {muted};
        }}
        #notebook-teminals > header button:hover, .guake-tab-label button:hover {{
            background-color: {hover}; color: {fg};
        }}
        .guake-sftp-header {{
            background-color: {bar}; color: {fg}; border-radius: 0;
            padding: 10px 6px 10px 12px;
        }}
    """
    )


def patch_gtk_theme(style_context, settings):
    """Install and refresh chrome when preferences or the system theme change."""
    global _style_provider, _theme_notifications_connected
    gtk_settings = Gtk.Settings.get_default()
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    if _style_provider is None:
        _style_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            screen, _style_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def refresh(*_args):
        if settings.general.get_boolean("gtk-use-system-default-theme"):
            found, background = style_context.lookup_color("theme_bg_color")
            dark = found and (background.red + background.green + background.blue) / 3 < 0.5
        else:
            dark = settings.general.get_boolean("gtk-prefer-dark-theme")
        _style_provider.load_from_data(appearance_css(dark).encode())

    if not _theme_notifications_connected:
        for prop in ("gtk-theme-name", "gtk-application-prefer-dark-theme"):
            gtk_settings.connect("notify::" + prop, refresh)
        for key in ("gtk-use-system-default-theme", "gtk-prefer-dark-theme", "gtk-theme-name"):
            settings.general.connect("changed::" + key, refresh)
        _theme_notifications_connected = True
    refresh()
