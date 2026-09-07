# -*- coding: utf-8; -*-
"""
GTK dialogs for the saved servers feature: the servers manager (a list with
connect / add / edit / remove / import actions) and the server editor form.

Storage and the ssh command line live in :mod:`guake.servers`; passwords
are kept in the desktop keyring through :mod:`guake.serversecrets`.
"""

import logging
import os
import shlex

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib
from gi.repository import Gtk
from gi.repository import Pango

from guake import serversecrets
from guake.servers import DEFAULT_SSH_PORT
from guake.servers import Server
from guake.servers import as_saved_server
from guake.servers import group_servers
from guake.servers import parse_ssh_config
from guake.utils import HidePrevention

log = logging.getLogger(__name__)

COLUMN_ID, COLUMN_NAME, COLUMN_DETAIL, COLUMN_WEIGHT = range(4)
ENTRY_WIDTH_CHARS = 40


def _show_message(parent, message_type, text, secondary=None, buttons=Gtk.ButtonsType.OK):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        destroy_with_parent=True,
        message_type=message_type,
        buttons=buttons,
        text=text,
    )
    if secondary:
        dialog.format_secondary_text(secondary)
    response = dialog.run()
    dialog.destroy()
    return response


def server_detail(server):
    """Short ``user@host:port`` description shown next to the server name."""
    detail = server.target
    if server.port != DEFAULT_SSH_PORT:
        detail = f"{detail}:{server.port}"
    return detail


class ServerEditDialog(Gtk.Dialog):
    """Form to add a new saved server or edit an existing one."""

    def __init__(self, parent, store, server=None):
        super().__init__(
            _("Edit server") if server else _("Add server"),
            parent,
            Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            (
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        self.store = store
        self.server = server
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)

        self._grid = Gtk.Grid(row_spacing=6, column_spacing=12, border_width=12)
        self._row = 0
        self.get_content_area().pack_start(self._grid, True, True, 0)

        self.name_entry = self._add_entry(_("Name"), server.name if server else "", _("e.g. web-1"))
        self.group_combo = Gtk.ComboBoxText.new_with_entry()
        for group in store.groups():
            self.group_combo.append_text(group)
        self.group_entry = self.group_combo.get_child()
        self.group_entry.set_text(server.group if server else "")
        self.group_entry.set_placeholder_text(_("Optional, e.g. Production"))
        self._add_row(_("Group"), self.group_combo)
        self.host_entry = self._add_entry(
            _("Host"), server.host if server else "", _("Host name or IP address")
        )
        self.port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port_spin.set_value(server.port if server else DEFAULT_SSH_PORT)
        self.port_spin.set_activates_default(True)
        self._add_row(_("Port"), self.port_spin, expand=False)
        self.user_entry = self._add_entry(
            _("Username"),
            server.user if server else "",
            _("Leave empty to use your local user name"),
        )

        self._add_section(_("Authentication"))
        self.identity_entry = self._add_file_entry(
            _("Private key"), server.identity_file if server else ""
        )
        self.password_entry = Gtk.Entry(
            visibility=False, input_purpose=Gtk.InputPurpose.PASSWORD, activates_default=True
        )
        self.forget_password = Gtk.CheckButton(label=_("Forget the saved password"))
        has_saved_password = bool(server and server.use_password)
        if not serversecrets.is_available():
            self.password_entry.set_sensitive(False)
            self.password_entry.set_placeholder_text(
                _("Unavailable: libsecret (gir1.2-secret-1) is not installed")
            )
        elif has_saved_password:
            self.password_entry.set_placeholder_text(
                _("Saved in your keyring. Leave empty to keep it.")
            )
        else:
            self.password_entry.set_placeholder_text(
                _("Optional. Kept in your keyring, used through sshpass.")
            )
        self._add_row(_("Password"), self.password_entry)
        if has_saved_password and serversecrets.is_available():
            self._add_row("", self.forget_password)

        self._add_section(_("Advanced"))
        self.jump_entry = self._add_entry(
            _("Jump host"),
            server.jump_host if server else "",
            _("Optional bastion, e.g. user@bastion (ssh -J)"),
        )
        self.options_entry = self._add_entry(
            _("SSH options"),
            server.options if server else "",
            _("Extra ssh arguments, e.g. -o ServerAliveInterval=30"),
        )
        self.command_entry = self._add_entry(
            _("Run after login"),
            server.command if server else "",
            _("Optional remote command, e.g. tmux attach || tmux"),
        )

        self.show_all()
        self.name_entry.grab_focus()

    # -- layout helpers ------------------------------------------------------

    def _add_row(self, label_text, widget, expand=True):
        label = Gtk.Label(label=label_text, xalign=1.0)
        self._grid.attach(label, 0, self._row, 1, 1)
        widget.set_hexpand(expand)
        self._grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return widget

    def _add_entry(self, label_text, text, placeholder):
        entry = Gtk.Entry(text=text, activates_default=True, width_chars=ENTRY_WIDTH_CHARS)
        entry.set_placeholder_text(placeholder)
        return self._add_row(label_text, entry)

    def _add_section(self, title):
        label = Gtk.Label(xalign=0.0, margin_top=8)
        label.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        self._grid.attach(label, 0, self._row, 2, 1)
        self._row += 1

    def _add_file_entry(self, label_text, text):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry(text=text, activates_default=True, hexpand=True)
        entry.set_placeholder_text(_("Optional, e.g. ~/.ssh/id_ed25519"))
        button = Gtk.Button(label=_("Browse..."))
        button.connect("clicked", self._on_browse_key, entry)
        box.pack_start(entry, True, True, 0)
        box.pack_start(button, False, False, 0)
        self._add_row(label_text, box)
        return entry

    def _on_browse_key(self, button, entry):
        chooser = Gtk.FileChooserDialog(
            title=_("Select a private key"),
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        ssh_dir = os.path.expanduser("~/.ssh")
        if os.path.isdir(ssh_dir):
            chooser.set_current_folder(ssh_dir)
        if chooser.run() == Gtk.ResponseType.OK:
            entry.set_text(chooser.get_filename())
        chooser.destroy()

    # -- reading the form ----------------------------------------------------

    def validation_error(self):
        """Return a user-facing message when the form cannot be saved, else None."""
        if not self.name_entry.get_text().strip():
            return _("Please give the server a name.")
        if not self.host_entry.get_text().strip():
            return _("Please enter the host name or IP address.")
        try:
            shlex.split(self.options_entry.get_text())
        except ValueError as e:
            return _("The SSH options cannot be parsed: {error}").format(error=e)
        return None

    def build_server(self, use_password=None):
        """Read the form into a :class:`Server` (keeps the id when editing)."""
        if use_password is None:
            use_password = bool(self.server and self.server.use_password)
        return Server(
            id=self.server.id if self.server else "",
            name=self.name_entry.get_text().strip(),
            host=self.host_entry.get_text().strip(),
            user=self.user_entry.get_text().strip(),
            port=self.port_spin.get_value_as_int(),
            group=self.group_entry.get_text().strip(),
            identity_file=self.identity_entry.get_text().strip(),
            jump_host=self.jump_entry.get_text().strip(),
            options=self.options_entry.get_text().strip(),
            command=self.command_entry.get_text().strip(),
            use_password=use_password,
        )

    def run_and_save(self):
        """Run the dialog until it is cancelled or a valid server is saved.
        Returns the saved :class:`Server`, or ``None`` when cancelled."""
        while self.run() == Gtk.ResponseType.OK:
            error = self.validation_error()
            if error:
                _show_message(self, Gtk.MessageType.ERROR, error)
                continue
            try:
                server = self.build_server()
            except ValueError as e:
                _show_message(self, Gtk.MessageType.ERROR, str(e))
                continue
            server = server.with_changes(use_password=self._persist_password(server))
            if self.server is None:
                self.store.add(server)
            else:
                self.store.update(server)
            return server
        return None

    def _persist_password(self, server):
        """Store / clear the password in the keyring as requested by the form.
        Returns whether a password is available for the server afterwards."""
        had_password = bool(self.server and self.server.use_password)
        if self.forget_password.get_active():
            serversecrets.clear_password(server.id)
            return False
        password = self.password_entry.get_text()
        if not password:
            return had_password
        if serversecrets.store_password(server.id, password, server.name):
            return True
        _show_message(
            self,
            Gtk.MessageType.WARNING,
            _("The password could not be saved in your keyring."),
            _("You will be asked for it when connecting."),
        )
        return had_password


class ServersDialog(Gtk.Dialog):
    """The servers manager: lists saved servers grouped by group name.
    Double-click (or Connect) opens a tab connected to the server."""

    def __init__(self, guake, add_new=False):
        super().__init__(
            _("Servers"),
            guake.window,
            Gtk.DialogFlags.DESTROY_WITH_PARENT,
            (Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE),
        )
        self.guake = guake
        self.store = guake.servers
        self.add_new = add_new
        self.set_default_size(560, 420)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, border_width=12)
        self.get_content_area().pack_start(content, True, True, 0)

        self.model = Gtk.TreeStore(str, str, str, int)
        self.view = Gtk.TreeView(model=self.model)
        self.view.set_enable_search(True)
        self.view.set_search_column(COLUMN_NAME)
        name_renderer = Gtk.CellRendererText()
        name_column = Gtk.TreeViewColumn(
            _("Name"), name_renderer, text=COLUMN_NAME, weight=COLUMN_WEIGHT
        )
        name_column.set_expand(True)
        self.view.append_column(name_column)
        self.view.append_column(
            Gtk.TreeViewColumn(_("Connection"), Gtk.CellRendererText(), text=COLUMN_DETAIL)
        )
        self.view.connect("row-activated", self.on_row_activated)
        self.view.get_selection().connect("changed", self.on_selection_changed)
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.view)
        content.pack_start(scrolled, True, True, 0)

        buttons = Gtk.ButtonBox(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        buttons.set_layout(Gtk.ButtonBoxStyle.START)
        self.connect_button = self._add_button(buttons, _("Connect"), self.on_connect)
        self.add_button_ = self._add_button(buttons, _("Add..."), self.on_add)
        self.edit_button = self._add_button(buttons, _("Edit..."), self.on_edit)
        self.remove_button = self._add_button(buttons, _("Remove"), self.on_remove)
        self.import_button = self._add_button(buttons, _("Import ~/.ssh/config"), self.on_import)
        self.import_button.set_tooltip_text(
            _("Save every host declared in your ssh client config as a server")
        )
        content.pack_start(buttons, False, False, 0)

        self.connect("response", lambda dialog, response: dialog.destroy())
        self.connect("destroy", self.on_destroy)
        self.refresh()

    @staticmethod
    def _add_button(box, label, callback):
        button = Gtk.Button(label=label)
        button.connect("clicked", callback)
        box.pack_start(button, False, False, 0)
        return button

    def present_dialog(self):
        """Show the manager on top of Guake without letting Guake auto-hide."""
        HidePrevention(self.guake.window).prevent()
        self.show_all()
        self.present()
        if self.add_new:
            self.add_new = False
            self.on_add()

    def on_destroy(self, *args):
        HidePrevention(self.guake.window).allow()

    # -- list handling -------------------------------------------------------

    def refresh(self, select_id=None):
        self.model.clear()
        select_path = None
        for group, members in group_servers(self.store.servers):
            parent = None
            if group:
                parent = self.model.append(None, ["", group, "", Pango.Weight.BOLD])
            for server in members:
                row = self.model.append(
                    parent, [server.id, server.name, server_detail(server), Pango.Weight.NORMAL]
                )
                if server.id == select_id:
                    select_path = self.model.get_path(row)
        self.view.expand_all()
        if select_path is not None:
            self.view.set_cursor(select_path, None, False)
        self.on_selection_changed(self.view.get_selection())
        self.import_button.set_sensitive(bool(parse_ssh_config()))

    def selected_server(self):
        model, tree_iter = self.view.get_selection().get_selected()
        if tree_iter is None:
            return None
        return self.store.get(model[tree_iter][COLUMN_ID])

    def on_selection_changed(self, selection):
        has_server = self.selected_server() is not None
        for button in (self.connect_button, self.edit_button, self.remove_button):
            button.set_sensitive(has_server)

    def on_row_activated(self, view, path, column):
        if self.selected_server() is not None:
            self.on_connect()
        elif view.row_expanded(path):
            view.collapse_row(path)
        else:
            view.expand_row(path, False)

    # -- actions ---------------------------------------------------------------

    def on_connect(self, *args):
        server = self.selected_server()
        if server is None:
            return
        self.guake.connect_to_server(server)
        self.response(Gtk.ResponseType.CLOSE)

    def on_add(self, *args):
        dialog = ServerEditDialog(self, self.store)
        server = dialog.run_and_save()
        dialog.destroy()
        if server is not None:
            self.refresh(select_id=server.id)

    def on_edit(self, *args):
        server = self.selected_server()
        if server is None:
            return
        dialog = ServerEditDialog(self, self.store, server)
        saved = dialog.run_and_save()
        dialog.destroy()
        self.refresh(select_id=saved.id if saved else server.id)

    def on_remove(self, *args):
        server = self.selected_server()
        if server is None:
            return
        answer = _show_message(
            self,
            Gtk.MessageType.QUESTION,
            _("Remove the server '{name}'?").format(name=server.name),
            _("Its saved password, if any, is removed from your keyring too."),
            buttons=Gtk.ButtonsType.YES_NO,
        )
        if answer != Gtk.ResponseType.YES:
            return
        if server.use_password:
            serversecrets.clear_password(server.id)
        self.store.remove(server.id)
        self.refresh()

    def on_import(self, *args):
        candidates = [as_saved_server(s) for s in parse_ssh_config()]
        added = self.store.import_servers(candidates)
        if added:
            _show_message(
                self,
                Gtk.MessageType.INFO,
                _("Imported {count} server(s) from ~/.ssh/config.").format(count=len(added)),
            )
        else:
            _show_message(
                self,
                Gtk.MessageType.INFO,
                _("Nothing new to import."),
                _("Every host in ~/.ssh/config is already saved."),
            )
        self.refresh(select_id=added[0].id if added else None)
