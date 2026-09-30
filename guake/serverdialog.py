# -*- coding: utf-8; -*-
"""
GTK dialogs for the saved servers feature: the servers manager (a list with
connect / add / edit / remove / import actions) and the server editor form.

Storage and the ssh command line live in :mod:`guake.servers`; passwords
are kept in the desktop keyring through :mod:`guake.serversecrets`.
"""

import logging
import math
import os
import shlex

import cairo
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib
from gi.repository import Gdk
from gi.repository import Gtk
from gi.repository import Pango

from guake import serversecrets
from guake.menus import color_swatch
from guake.serverbackupdialogs import export_servers
from guake.serverbackupdialogs import import_servers
from guake.servers import DEFAULT_SSH_PORT
from guake.servers import Server
from guake.servers import as_saved_server
from guake.servers import group_servers
from guake.servers import parse_ssh_config
from guake.serversyncdialogs import sync_servers
from guake.tabcolors import PALETTE
from guake.tabcolors import auto_color
from guake.utils import HidePrevention

log = logging.getLogger(__name__)

COLUMN_ID, COLUMN_NAME, COLUMN_DETAIL, COLUMN_WEIGHT, COLUMN_COLOR, COLUMN_SUBTITLE = range(6)
ENTRY_WIDTH_CHARS = 40
SWATCH_SIZE = 12
COLOR_SWATCH_SIZE = 18
_SWATCH_CACHE = {}


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
        self._add_row(_("Tab color"), self._build_color_picker(server.color if server else ""))
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

    def _build_color_picker(self, current):
        """A row of round colour buttons; "Auto" picks a colour from the name."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.color_buttons = {}
        group = None
        choices = [("", _("Automatic"))] + [(c, _(n)) for n, c in PALETTE]
        if current and current not in {c for _n, c in PALETTE}:
            # A colour set outside the palette (servers.json, a backup) stays pickable.
            choices.append((current, _("Custom {color}").format(color=current)))
        for color, name in choices:
            button = Gtk.RadioButton(group=group, draw_indicator=False, relief=Gtk.ReliefStyle.NONE)
            group = group or button
            if color:
                button.add(color_swatch(color, size=COLOR_SWATCH_SIZE))
            else:
                button.set_label(_("Auto"))
            button.set_tooltip_text(name)
            button.set_active(color == current)
            box.pack_start(button, False, False, 0)
            self.color_buttons[color] = button
        return box

    def selected_color(self):
        return next((c for c, b in self.color_buttons.items() if b.get_active()), "")

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
            color=self.selected_color(),
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


def _swatch_pixbuf(color, size=SWATCH_SIZE):
    """A round colour dot for tree views (cached per colour)."""
    if color not in _SWATCH_CACHE:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        rgba = Gdk.RGBA()
        rgba.parse(color)
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
        cr.arc(size / 2, size / 2, size / 2 - 1, 0, 2 * math.pi)
        cr.fill()
        _SWATCH_CACHE[color] = Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)
    return _SWATCH_CACHE[color]


def _icon_button(icon_name, tooltip, callback, label=None):
    button = Gtk.Button(label=label, always_show_image=bool(label))
    button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
    button.set_tooltip_text(tooltip)
    button.connect("clicked", callback)
    return button


def server_subtitle(server):
    """Second line of a server row: where it connects and how it logs in."""
    parts = [server_detail(server)]
    if server.use_password:
        parts.append(_("password"))
    if server.identity_file:
        parts.append(_("key {name}").format(name=os.path.basename(server.identity_file)))
    if server.jump_host:
        parts.append(_("via {host}").format(host=server.jump_host))
    return " \u00b7 ".join(parts)


class ServersDialog(Gtk.Dialog):
    """The servers manager: lists saved servers grouped by group name, with
    a search field. Double-click (or Connect) opens a tab connected to the
    server. The header bar menu imports and exports backups."""

    def __init__(self, guake, add_new=False):
        super().__init__(
            title=_("Servers"),
            transient_for=guake.window,
            destroy_with_parent=True,
            use_header_bar=True,
        )
        self.guake = guake
        self.store = guake.servers
        self.add_new = add_new
        self.set_default_size(660, 520)
        self._build_header_bar()

        content = self.get_content_area()
        content.set_spacing(0)
        self.search_entry = Gtk.SearchEntry(
            placeholder_text=_("Search by name, host, user or group"), margin=12, margin_bottom=6
        )
        self.search_entry.connect("search-changed", self.on_search_changed)
        content.pack_start(self.search_entry, False, False, 0)

        self.model = Gtk.TreeStore(str, str, str, int, str, str)
        self.filter = self.model.filter_new()
        self.filter.set_visible_func(self._row_visible)
        self.view = Gtk.TreeView(model=self.filter, headers_visible=False, enable_search=False)
        self.view.get_style_context().add_class("guake-servers")
        self.view.set_search_column(COLUMN_NAME)
        column = Gtk.TreeViewColumn()
        swatch = Gtk.CellRendererPixbuf(xpad=12)
        column.pack_start(swatch, False)
        column.set_cell_data_func(swatch, self._render_swatch)
        server_icon = Gtk.CellRendererPixbuf(xpad=6)
        column.pack_start(server_icon, False)
        column.set_cell_data_func(server_icon, self._render_server_icon)
        text = Gtk.CellRendererText(ypad=12, ellipsize=Pango.EllipsizeMode.END)
        column.pack_start(text, True)
        column.set_cell_data_func(text, self._render_text)
        column.set_expand(True)
        self.view.append_column(column)
        self.view.connect("row-activated", self.on_row_activated)
        self.view.get_selection().connect("changed", self.on_selection_changed)
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN, margin_start=12, margin_end=12)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.view)

        # A stack only switches to visible children, so show them up front.
        empty_state = self._build_empty_state()
        scrolled.show_all()
        empty_state.show_all()
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(scrolled, "list")
        self.stack.add_named(empty_state, "empty")
        content.pack_start(self.stack, True, True, 0)
        content.pack_start(self._build_action_bar(), False, False, 0)

        self.connect("response", lambda dialog, response: dialog.destroy())
        self.connect("destroy", self.on_destroy)
        self.refresh()

    # -- layout ------------------------------------------------------------------

    def _build_header_bar(self):
        header = self.get_header_bar()
        self.add_button_ = _icon_button("list-add-symbolic", _("Add a server"), self.on_add)
        header.pack_start(self.add_button_)
        self.sync_button = _icon_button(
            "emblem-synchronizing-symbolic",
            _("Sync servers with your other devices over Tailscale"),
            self.on_sync,
        )
        header.pack_start(self.sync_button)

        menu = Gtk.Menu()
        self.import_button = self._menu_item(
            menu, _("Import hosts from ~/.ssh/config"), self.on_import
        )
        self.import_button.set_tooltip_text(
            _("Save every host declared in your ssh client config as a server")
        )
        menu.append(Gtk.SeparatorMenuItem())
        self.import_backup_item = self._menu_item(
            menu, _("Import backup..."), self.on_import_backup
        )
        self.export_backup_item = self._menu_item(
            menu, _("Export backup..."), self.on_export_backup
        )
        menu.show_all()
        self.more_button = Gtk.MenuButton(popup=menu, tooltip_text=_("Import and export"))
        self.more_button.set_image(
            Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
        )
        header.pack_end(self.more_button)

    @staticmethod
    def _menu_item(menu, label, callback):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        menu.append(item)
        return item

    def _build_action_bar(self):
        bar = Gtk.ActionBar()
        self.edit_button = _icon_button(
            "document-edit-symbolic", _("Edit the server"), self.on_edit, _("Edit")
        )
        self.remove_button = _icon_button(
            "user-trash-symbolic", _("Remove the server"), self.on_remove, _("Remove")
        )
        self.sftp_button = _icon_button(
            "folder-remote-symbolic", _("Browse files over SFTP"), self.on_sftp, _("Files")
        )
        self.connect_button = _icon_button(
            "utilities-terminal-symbolic",
            _("Open a tab logged into the server"),
            self.on_connect,
            _("Connect"),
        )
        self.connect_button.get_style_context().add_class("suggested-action")
        bar.pack_start(self.edit_button)
        bar.pack_start(self.remove_button)
        bar.pack_end(self.connect_button)
        bar.pack_end(self.sftp_button)
        return bar

    def _build_empty_state(self):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, margin=24
        )
        icon = Gtk.Image.new_from_icon_name("network-server-symbolic", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(64)
        icon.get_style_context().add_class("dim-label")
        title = Gtk.Label()
        title.set_markup(f"<big><b>{GLib.markup_escape_text(_('No saved servers yet'))}</b></big>")
        hint = Gtk.Label(
            label=_(
                "Add a server, or bring your servers over from another machine with "
                "Import backup... in the menu at the top right."
            ),
            wrap=True,
            max_width_chars=48,
            justify=Gtk.Justification.CENTER,
        )
        hint.get_style_context().add_class("dim-label")
        add = Gtk.Button(label=_("Add a server"), halign=Gtk.Align.CENTER)
        add.get_style_context().add_class("suggested-action")
        add.connect("clicked", self.on_add)
        for widget in (icon, title, hint, add):
            box.pack_start(widget, False, False, 0)
        return box

    # -- rendering -------------------------------------------------------------

    def _render_server_icon(self, column, cell, model, tree_iter, data):
        cell.set_property(
            "icon-name", "network-server-symbolic" if model[tree_iter][COLUMN_ID] else None
        )

    def _render_swatch(self, column, cell, model, tree_iter, data):
        if model[tree_iter][COLUMN_ID]:
            cell.set_property("icon-name", None)
            cell.set_property("pixbuf", _swatch_pixbuf(model[tree_iter][COLUMN_COLOR]))
        else:
            cell.set_property("pixbuf", None)
            cell.set_property("icon-name", "folder-symbolic")

    def _render_text(self, column, cell, model, tree_iter, data):
        row = model[tree_iter]
        name = GLib.markup_escape_text(row[COLUMN_NAME])
        if row[COLUMN_ID]:
            subtitle = GLib.markup_escape_text(row[COLUMN_SUBTITLE])
            markup = f'<b>{name}</b>\n<small><span alpha="65%">{subtitle}</span></small>'
        else:
            count = model.iter_n_children(tree_iter)
            markup = f'<b>{name}</b>  <small><span alpha="65%">{count}</span></small>'
        cell.set_property("markup", markup)

    def _row_visible(self, model, tree_iter, data):
        query = (
            self.search_entry.get_text().strip().lower() if hasattr(self, "search_entry") else ""
        )
        if not query:
            return True
        row = model[tree_iter]
        if not row[COLUMN_ID]:
            if query in row[COLUMN_NAME].lower():
                return True
            return any(self._matches(child, query) for child in row.iterchildren())
        parent = model.iter_parent(tree_iter)
        if parent is not None and query in model[parent][COLUMN_NAME].lower():
            return True
        return self._matches(row, query)

    @staticmethod
    def _matches(row, query):
        return query in row[COLUMN_NAME].lower() or query in row[COLUMN_SUBTITLE].lower()

    def on_search_changed(self, entry):
        self.filter.refilter()
        self.view.expand_all()
        first = self.filter.get_iter_first()
        if first is not None and entry.get_text():
            while self.filter.iter_has_child(first) and not self.filter[first][COLUMN_ID]:
                first = self.filter.iter_children(first)
            self.view.set_cursor(self.filter.get_path(first), None, False)

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
        select_iter = None
        servers = self.store.servers
        for group, members in group_servers(servers):
            parent = None
            if group:
                parent = self.model.append(None, ["", group, "", Pango.Weight.BOLD, "", ""])
            for server in members:
                row = self.model.append(
                    parent,
                    [
                        server.id,
                        server.name,
                        server_detail(server),
                        Pango.Weight.NORMAL,
                        server.color or auto_color(server.id),
                        server_subtitle(server),
                    ],
                )
                if server.id == select_id:
                    select_iter = row
        self.filter.refilter()
        self.view.expand_all()
        if select_iter is not None:
            path = self.filter.convert_child_path_to_path(self.model.get_path(select_iter))
            if path is not None:
                self.view.set_cursor(path, None, False)
        self.stack.set_visible_child_name("list" if servers else "empty")
        self.search_entry.set_sensitive(bool(servers))
        self.export_backup_item.set_sensitive(bool(servers))
        self.on_selection_changed(self.view.get_selection())
        self.import_button.set_sensitive(bool(parse_ssh_config()))

    def selected_server(self):
        model, tree_iter = self.view.get_selection().get_selected()
        if tree_iter is None:
            return None
        return self.store.get(model[tree_iter][COLUMN_ID])

    def on_selection_changed(self, selection):
        has_server = self.selected_server() is not None
        for button in (self.connect_button, self.sftp_button, self.edit_button, self.remove_button):
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

    def on_sftp(self, *args):
        server = self.selected_server()
        if server is None:
            return
        self.response(Gtk.ResponseType.CLOSE)
        self.guake.open_sftp_panel(server)

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
        if saved is not None and hasattr(self.guake, "refresh_server_tabs"):
            self.guake.refresh_server_tabs()

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

    def on_export_backup(self, *args):
        export_servers(self, self.store.servers)

    def on_sync(self, *args):
        if sync_servers(self, self.guake):
            self.refresh()
            if hasattr(self.guake, "refresh_server_tabs"):
                self.guake.refresh_server_tabs()

    def on_import_backup(self, *args):
        summary = import_servers(self, self.store)
        if summary is not None:
            changed = [*summary.added, *summary.updated]
            self.refresh(select_id=changed[0].id if changed else None)
            if hasattr(self.guake, "refresh_server_tabs"):
                self.guake.refresh_server_tabs()
