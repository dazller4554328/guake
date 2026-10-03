# -*- coding: utf-8; -*-
"""
GTK dialogs for the saved servers feature: the servers manager (a list with
connect / add / edit / remove / import actions, where a group's colour is
picked) and the server editor form.

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

from guake import addonstyle
from guake import serversecrets
from guake.menus import color_swatch
from guake.menus import mk_color_menu
from guake.serverbackupdialogs import export_servers
from guake.serverbackupdialogs import import_servers
from guake.servers import DEFAULT_SSH_PORT
from guake.servers import Server
from guake.servers import as_saved_server
from guake.servers import group_servers
from guake.servers import parse_ssh_config
from guake.serversyncdialogs import sync_servers
from guake.tabcolors import PALETTE
from guake.utils import HidePrevention

log = logging.getLogger(__name__)

COLUMN_ID, COLUMN_NAME, COLUMN_DETAIL, COLUMN_WEIGHT, COLUMN_COLOR, COLUMN_SUBTITLE = range(6)
ENTRY_WIDTH_CHARS = 40
ICON_SIZE = 16
CHIP_SIZE = 10
CHIP_RADIUS = 2
COLOR_SWATCH_SIZE = 18
ROW_PADDING = 5
DIM_ALPHA = "62%"
_ICON_CACHE = {}


def _show_message(parent, message_type, text, secondary=None, buttons=Gtk.ButtonsType.OK):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        destroy_with_parent=True,
        message_type=message_type,
        buttons=buttons,
        text=text,
    )
    addonstyle.mark(dialog)
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
        addonstyle.mark(self)
        self.store = store
        self.server = server
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)
        ok_button = self.get_widget_for_response(Gtk.ResponseType.OK)
        ok_button.get_style_context().add_class("suggested-action")

        self._grid = Gtk.Grid(row_spacing=8, column_spacing=12, border_width=16)
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
        label = Gtk.Label(label=label_text, xalign=0.0)
        label.get_style_context().add_class("dim-label")
        self._grid.attach(label, 0, self._row, 1, 1)
        widget.set_hexpand(expand)
        self._grid.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return widget

    def _build_color_picker(self, current):
        """A row of colour buttons; "Auto" follows the server's group."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.color_buttons = {}
        group = None
        automatic = _("Automatic: the color of the server's group")
        choices = [("", automatic)] + [(c, _(n)) for n, c in PALETTE]
        if current and current not in {c for _n, c in PALETTE}:
            # A colour set outside the palette (servers.json, a backup) stays pickable.
            choices.append((current, _("Custom {color}").format(color=current)))
        for color, name in choices:
            button = Gtk.RadioButton(group=group, draw_indicator=False, relief=Gtk.ReliefStyle.NONE)
            group = group or button
            button.get_style_context().add_class("guake-swatch")
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
        label = Gtk.Label(label=title.upper(), xalign=0.0)
        label.get_style_context().add_class("guake-section-title")
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


def _chip_surface(color, scale):
    """A small rounded square in ``color``: the mark of a group."""
    size = ICON_SIZE * scale
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    surface.set_device_scale(scale, scale)
    cr = cairo.Context(surface)
    rgba = Gdk.RGBA()
    rgba.parse(color)
    cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
    left = top = (ICON_SIZE - CHIP_SIZE) / 2
    right = bottom = left + CHIP_SIZE
    cr.new_sub_path()
    cr.arc(right - CHIP_RADIUS, top + CHIP_RADIUS, CHIP_RADIUS, -math.pi / 2, 0)
    cr.arc(right - CHIP_RADIUS, bottom - CHIP_RADIUS, CHIP_RADIUS, 0, math.pi / 2)
    cr.arc(left + CHIP_RADIUS, bottom - CHIP_RADIUS, CHIP_RADIUS, math.pi / 2, math.pi)
    cr.arc(left + CHIP_RADIUS, top + CHIP_RADIUS, CHIP_RADIUS, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.fill()
    return surface


def row_icon(color, is_group, scale=1):
    """Icon of a list row, in the row's colour: a chip for a group, a
    server for a server (cached)."""
    key = (color, is_group, scale)
    if key not in _ICON_CACHE:
        surface = (
            _chip_surface(color, scale)
            if is_group
            else addonstyle.colored_icon("server", color, ICON_SIZE, scale)
        )
        if surface is None:
            return None
        _ICON_CACHE[key] = surface
    return _ICON_CACHE[key]


def _icon_button(icon, tooltip, callback, label=None):
    button = Gtk.Button(label=label, always_show_image=bool(label))
    button.set_image(addonstyle.image(icon, Gtk.IconSize.BUTTON))
    button.set_tooltip_text(tooltip)
    button.connect("clicked", callback)
    return button


def count_summary(servers):
    """``"5 servers in 2 groups"`` for the title bar."""
    groups = {s.group for s in servers if s.group}
    if not servers:
        return ""
    text = _("1 server") if len(servers) == 1 else _("{count} servers").format(count=len(servers))
    if groups:
        text += " \u00b7 " + (
            _("1 group") if len(groups) == 1 else _("{count} groups").format(count=len(groups))
        )
    return text


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
    """The servers manager: lists saved servers under their group, with a
    search field. Double-click (or Connect) opens a tab connected to the
    server. A group's colour, shared by the tabs of all its servers, is
    picked here. The header bar menu imports and exports backups."""

    def __init__(self, guake, add_new=False):
        super().__init__(
            title=_("Servers"),
            transient_for=guake.window,
            destroy_with_parent=True,
            use_header_bar=True,
        )
        addonstyle.mark(self)
        self.guake = guake
        self.store = guake.servers
        self.add_new = add_new
        self.color_menu = None
        self.context_menu = None
        self.set_default_size(680, 540)
        self._build_header_bar()

        content = self.get_content_area()
        content.set_spacing(0)
        content.set_border_width(0)
        self.search_entry = Gtk.SearchEntry(
            placeholder_text=_("Search by name, host, user or group")
        )
        self.search_entry.connect("search-changed", self.on_search_changed)
        search_box = Gtk.Box()
        search_box.get_style_context().add_class("guake-search")
        search_box.pack_start(self.search_entry, True, True, 0)
        content.pack_start(search_box, False, False, 0)
        content.pack_start(Gtk.Separator(), False, False, 0)

        self.model = Gtk.TreeStore(str, str, str, int, str, str)
        self.filter = self.model.filter_new()
        self.filter.set_visible_func(self._row_visible)
        self.view = Gtk.TreeView(model=self.filter, headers_visible=False, enable_search=False)
        self.view.get_style_context().add_class("guake-servers")
        self.view.set_search_column(COLUMN_NAME)
        self.view.set_level_indentation(4)
        column = Gtk.TreeViewColumn()
        icon = Gtk.CellRendererPixbuf(xpad=4)
        column.pack_start(icon, False)
        column.set_cell_data_func(icon, self._render_icon)
        text = Gtk.CellRendererText(ypad=ROW_PADDING, ellipsize=Pango.EllipsizeMode.END)
        column.pack_start(text, True)
        column.set_cell_data_func(text, self._render_text)
        column.set_expand(True)
        self.view.append_column(column)
        self.view.connect("row-activated", self.on_row_activated)
        self.view.connect("button-press-event", self.on_button_press)
        self.view.get_selection().connect("changed", self.on_selection_changed)
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.NONE)
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
        self.add_button_ = _icon_button("add", _("Add a server"), self.on_add)
        header.pack_start(self.add_button_)
        self.sync_button = _icon_button(
            "sync", _("Sync servers with your other devices over Tailscale"), self.on_sync
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
        self.more_button.set_image(addonstyle.image("ellipsis", Gtk.IconSize.BUTTON))
        header.pack_end(self.more_button)

    @staticmethod
    def _menu_item(menu, label, callback):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        menu.append(item)
        return item

    def _build_action_bar(self):
        bar = Gtk.ActionBar()
        self.edit_button = _icon_button("edit", _("Edit the server"), self.on_edit, _("Edit"))
        self.remove_button = _icon_button(
            "trash", _("Remove the server"), self.on_remove, _("Remove")
        )
        self.group_color_button = _icon_button(
            "symbol-color",
            _("Pick the color shared by the tabs of every server in the group"),
            self.on_group_color,
            _("Group color"),
        )
        self.sftp_button = _icon_button(
            "remote-explorer", _("Browse files over SFTP"), self.on_sftp, _("Files")
        )
        self.connect_button = _icon_button(
            "terminal", _("Open a tab logged into the server"), self.on_connect, _("Connect")
        )
        self.connect_button.get_style_context().add_class("suggested-action")
        bar.pack_start(self.edit_button)
        bar.pack_start(self.remove_button)
        bar.pack_start(self.group_color_button)
        bar.pack_end(self.connect_button)
        bar.pack_end(self.sftp_button)
        return bar

    def _build_empty_state(self):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, margin=24
        )
        icon = addonstyle.image("server-environment", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(56)
        icon.get_style_context().add_class("dim-label")
        title = Gtk.Label()
        title.get_style_context().add_class("guake-heading")
        title.set_markup(f"<big>{GLib.markup_escape_text(_('No saved servers yet'))}</big>")
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

    def _render_icon(self, column, cell, model, tree_iter, data):
        row = model[tree_iter]
        cell.set_property(
            "surface", row_icon(row[COLUMN_COLOR], not row[COLUMN_ID], self.get_scale_factor())
        )

    def _render_text(self, column, cell, model, tree_iter, data):
        row = model[tree_iter]
        name = GLib.markup_escape_text(row[COLUMN_NAME])
        if row[COLUMN_ID]:
            dimmed = GLib.markup_escape_text(row[COLUMN_SUBTITLE])
            markup = f'{name}   <span alpha="{DIM_ALPHA}" size="small">{dimmed}</span>'
        else:
            count = model.iter_n_children(tree_iter)
            markup = f'<b>{name}</b>   <span alpha="{DIM_ALPHA}" size="small">{count}</span>'
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

    def refresh(self, select_id=None, select_group=None):
        self.model.clear()
        select_iter = None
        servers = self.store.servers
        for group, members in group_servers(servers):
            parent = None
            if group:
                parent = self.model.append(
                    None, ["", group, "", Pango.Weight.BOLD, self.store.group_color(group), ""]
                )
                if group == select_group and select_id is None:
                    select_iter = parent
            for server in members:
                row = self.model.append(
                    parent,
                    [
                        server.id,
                        server.name,
                        server_detail(server),
                        Pango.Weight.NORMAL,
                        self.store.color_for(server),
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
        self.get_header_bar().set_subtitle(count_summary(servers))
        self.search_entry.set_sensitive(bool(servers))
        self.export_backup_item.set_sensitive(bool(servers))
        self.on_selection_changed(self.view.get_selection())
        self.import_button.set_sensitive(bool(parse_ssh_config()))

    def selected_server(self):
        model, tree_iter = self.view.get_selection().get_selected()
        if tree_iter is None:
            return None
        return self.store.get(model[tree_iter][COLUMN_ID])

    def selected_group(self):
        """The group of the selected row: the group itself, or the one the
        selected server belongs to. Empty when there is none."""
        model, tree_iter = self.view.get_selection().get_selected()
        if tree_iter is None:
            return ""
        if not model[tree_iter][COLUMN_ID]:
            return model[tree_iter][COLUMN_NAME]
        server = self.store.get(model[tree_iter][COLUMN_ID])
        return server.group if server is not None else ""

    def on_selection_changed(self, selection):
        has_server = self.selected_server() is not None
        for button in (self.connect_button, self.sftp_button, self.edit_button, self.remove_button):
            button.set_sensitive(has_server)
        self.group_color_button.set_sensitive(bool(self.selected_group()))

    def on_button_press(self, view, event):
        if event.button != Gdk.BUTTON_SECONDARY:
            return False
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        if hit is None:
            return False
        view.set_cursor(hit[0], None, False)
        self.context_menu = self._build_context_menu()
        self.context_menu.popup_at_pointer(event)
        return True

    def _build_context_menu(self):
        menu = Gtk.Menu()
        if self.selected_server() is not None:
            self._menu_item(menu, _("Connect"), self.on_connect)
            self._menu_item(menu, _("Browse files over SFTP"), self.on_sftp)
            menu.append(Gtk.SeparatorMenuItem())
            self._menu_item(menu, _("Edit..."), self.on_edit)
            self._menu_item(menu, _("Remove"), self.on_remove)
        group = self.selected_group()
        if group:
            if menu.get_children():
                menu.append(Gtk.SeparatorMenuItem())
            item = Gtk.MenuItem(label=_("Color of group {group}").format(group=group))
            item.set_submenu(self._group_color_menu(group))
            menu.append(item)
        menu.show_all()
        return menu

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

    # -- group colour ----------------------------------------------------------

    def _group_color_menu(self, group):
        current = self.store.group_color(group) if self.store.has_group_color(group) else ""
        return mk_color_menu(current, lambda item, color: self.set_group_color(group, color))

    def on_group_color(self, button):
        group = self.selected_group()
        if not group:
            return
        # Keep a reference or the menu is collected while shown.
        self.color_menu = self._group_color_menu(group)
        self.color_menu.popup_at_widget(
            button, Gdk.Gravity.NORTH_WEST, Gdk.Gravity.SOUTH_WEST, None
        )

    def set_group_color(self, group, color):
        """Paint every server of ``group`` (list, tabs) in ``color``; an
        empty colour goes back to the automatic one."""
        selected = self.selected_server()
        self.store.set_group_color(group, color)
        self.refresh(select_id=selected.id if selected else None, select_group=group)
        self._refresh_tabs()

    def _refresh_tabs(self):
        if hasattr(self.guake, "refresh_server_tabs"):
            self.guake.refresh_server_tabs()

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
        if saved is not None:
            self._refresh_tabs()

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
        export_servers(self, self.store.servers, self.store.group_colors)

    def on_sync(self, *args):
        if sync_servers(self, self.guake):
            self.refresh()
            self._refresh_tabs()

    def on_import_backup(self, *args):
        summary = import_servers(self, self.store)
        if summary is not None:
            changed = [*summary.added, *summary.updated]
            self.refresh(select_id=changed[0].id if changed else None)
            self._refresh_tabs()
