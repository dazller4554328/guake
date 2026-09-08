# -*- coding: utf-8; -*-
"""
Small GTK dialogs used by the SFTP panel: questions asked by ssh while
connecting, name prompts (rename, new folder), confirmations and the file
choosers for uploads and downloads.

Every dialog keeps Guake from auto-hiding while it is open.
"""

import os
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib
from gi.repository import Gtk

from guake.utils import HidePrevention

ENTRY_WIDTH_CHARS = 40
MAX_LISTED_NAMES = 8


def _run(window, dialog):
    """Run a modal dialog on top of Guake and return its response."""
    HidePrevention(window).prevent()
    try:
        return dialog.run()
    finally:
        dialog.destroy()
        HidePrevention(window).allow()


def show_error(window, text, secondary=None):
    dialog = Gtk.MessageDialog(
        transient_for=window,
        modal=True,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text=text,
    )
    if secondary:
        dialog.format_secondary_text(secondary)
    _run(window, dialog)


def confirm(window, text, secondary=None, ok_label=None, destructive=False):
    """Yes/no question; returns True when the user confirmed."""
    dialog = Gtk.MessageDialog(
        transient_for=window,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.NONE,
        text=text,
    )
    if secondary:
        dialog.format_secondary_text(secondary)
    dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    button = dialog.add_button(ok_label or _("OK"), Gtk.ResponseType.OK)
    if destructive:
        button.get_style_context().add_class("destructive-action")
    dialog.set_default_response(Gtk.ResponseType.OK)
    return _run(window, dialog) == Gtk.ResponseType.OK


def list_names(names):
    """A short bulleted list of names for confirmation messages."""
    shown = [f"• {name}" for name in names[:MAX_LISTED_NAMES]]
    if len(names) > MAX_LISTED_NAMES:
        shown.append(_("… and {count} more").format(count=len(names) - MAX_LISTED_NAMES))
    return "\n".join(shown)


def ask_text(window, title, label, default="", ok_label=None):
    """Prompt for one line of text; returns it, or None when cancelled."""
    dialog = Gtk.Dialog(title=title, transient_for=window, modal=True)
    dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    dialog.add_button(ok_label or _("OK"), Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    box.set_spacing(6)
    box.set_border_width(12)
    box.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
    entry = Gtk.Entry(text=default, width_chars=ENTRY_WIDTH_CHARS, activates_default=True)
    box.pack_start(entry, False, False, 0)
    dialog.show_all()
    entry.grab_focus()
    if "." in default and not default.startswith("."):
        entry.select_region(0, default.rfind("."))
    HidePrevention(window).prevent()
    response = dialog.run()
    text = entry.get_text().strip()
    dialog.destroy()
    HidePrevention(window).allow()
    return text if response == Gtk.ResponseType.OK and text else None


def ask_question(window, server_name, text, secret):
    """Show a question from ssh (password, passphrase, host key...) and
    return the answer, or None when the user cancelled."""
    dialog = Gtk.Dialog(
        title=_("SFTP: {name}").format(name=server_name), transient_for=window, modal=True
    )
    dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    dialog.add_button(_("OK"), Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    box.set_spacing(6)
    box.set_border_width(12)
    label = Gtk.Label(label=text, xalign=0, selectable=True)
    label.set_line_wrap(True)
    label.set_max_width_chars(70)
    box.pack_start(label, False, False, 0)
    entry = Gtk.Entry(width_chars=ENTRY_WIDTH_CHARS, activates_default=True)
    if secret:
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    box.pack_start(entry, False, False, 0)
    dialog.show_all()
    entry.grab_focus()
    HidePrevention(window).prevent()
    response = dialog.run()
    answer = entry.get_text()
    dialog.destroy()
    HidePrevention(window).allow()
    return answer if response == Gtk.ResponseType.OK else None


class MainLoopPrompter:
    """Adapter that lets a worker thread ask the user a question through the
    GTK main loop and wait for the answer."""

    def __init__(self, window, server_name):
        self.window = window
        self.server_name = server_name

    def __call__(self, text, secret):
        answered = threading.Event()
        result = {}

        def show():
            result["answer"] = ask_question(self.window, self.server_name, text, secret)
            answered.set()
            return False

        GLib.idle_add(show)
        answered.wait()
        return result.get("answer")


def default_download_dir():
    return GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser(
        "~"
    )


def choose_folder(window, title, start_dir=None, ok_label=None):
    """Pick a local folder; returns its path or None."""
    dialog = Gtk.FileChooserDialog(
        title=title,
        transient_for=window,
        action=Gtk.FileChooserAction.SELECT_FOLDER,
        modal=True,
    )
    dialog.add_buttons(
        _("Cancel"), Gtk.ResponseType.CANCEL, ok_label or _("Select"), Gtk.ResponseType.OK
    )
    if start_dir and os.path.isdir(start_dir):
        dialog.set_current_folder(start_dir)
    HidePrevention(window).prevent()
    response = dialog.run()
    path = dialog.get_filename()
    dialog.destroy()
    HidePrevention(window).allow()
    return path if response == Gtk.ResponseType.OK else None


def choose_files(window, title, start_dir=None):
    """Pick one or more local files to upload; returns a list of paths."""
    dialog = Gtk.FileChooserDialog(
        title=title, transient_for=window, action=Gtk.FileChooserAction.OPEN, modal=True
    )
    dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Upload"), Gtk.ResponseType.OK)
    dialog.set_select_multiple(True)
    if start_dir and os.path.isdir(start_dir):
        dialog.set_current_folder(start_dir)
    HidePrevention(window).prevent()
    response = dialog.run()
    paths = dialog.get_filenames()
    dialog.destroy()
    HidePrevention(window).allow()
    return paths if response == Gtk.ResponseType.OK else []
