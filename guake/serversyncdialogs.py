# -*- coding: utf-8; -*-
"""
GTK dialog to sync the saved servers with your other devices over
Tailscale. It fetches every device's list in the background, shows the
proposed changes with a check box each, and writes only the ones you keep.
Deletions and changes that deserve a closer look start unchecked and
need a second confirmation.

The protocol and the merge rules live in :mod:`guake.serversync`.
"""

import logging
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib
from gi.repository import Gio
from gi.repository import Gtk

from guake import addonstyle
from guake import serversecrets
from guake import serversync
from guake.serversync import ADD
from guake.serversync import DELETE
from guake.serversync import SHARE_SETTING
from guake.serversync import UPDATE
from guake.serversync import needs_confirmation
from guake.servers import DEFAULT_SSH_PORT

log = logging.getLogger(__name__)

COL_APPLY, COL_KIND, COL_TEXT, COL_INDEX = range(4)
WARNING_COLOR = "#c01c28"
# Fields worth naming when describing an update.
COMPARED_FIELDS = (
    "name",
    "host",
    "user",
    "port",
    "group",
    "identity_file",
    "jump_host",
    "options",
    "command",
    "use_password",
    "color",
)
# Translated when shown (gettext is not installed yet at import time).
FIELD_LABELS = {
    "name": "name",
    "host": "host",
    "user": "user",
    "port": "port",
    "group": "group",
    "identity_file": "key",
    "jump_host": "jump host",
    "options": "SSH options",
    "command": "remote command",
    "use_password": "saved password",
    "color": "colour",
}


def _escape(text):
    return GLib.markup_escape_text(str(text))


def time_ago(timestamp, now=None):
    """``"3 hours ago"`` style description, or ``""`` when unknown."""
    if not timestamp:
        return ""
    seconds = max(0, (now or time.time()) - timestamp)
    for unit_seconds, text in (
        (86400, _("{n} day(s) ago")),
        (3600, _("{n} hour(s) ago")),
        (60, _("{n} minute(s) ago")),
    ):
        if seconds >= unit_seconds:
            return text.format(n=int(seconds // unit_seconds))
    return _("just now")


def changed_fields(before, after):
    return [f for f in COMPARED_FIELDS if getattr(before, f) != getattr(after, f)]


def _value(value):
    if isinstance(value, bool):
        return _("yes") if value else _("no")
    return str(value) if value not in ("", None) else _("(none)")


def _change_lines(change):
    """Plain-text detail lines: every value an ADD brings that can run
    something, or old -> new for each field an UPDATE changes."""
    server = change.server
    if change.kind == ADD:
        target = (
            server.target if server.port == DEFAULT_SSH_PORT else f"{server.target}:{server.port}"
        )
        lines = [target]
        for field in ("jump_host", "options", "command", "identity_file"):
            if getattr(server, field):
                lines.append(f"{_(FIELD_LABELS[field])}: {getattr(server, field)}")
        return lines
    if change.kind == UPDATE:
        return [
            f"{_(FIELD_LABELS[f])}: {_value(getattr(change.previous, f))} → "
            f"{_value(getattr(server, f))}"
            for f in changed_fields(change.previous, server)
        ]
    return [server.target]


def describe_change(change, now=None):
    """Markup for the (kind, description) columns of a proposed change."""
    when = time_ago(change.when, now)
    if change.kind == DELETE:
        kind = f'<span foreground="{WARNING_COLOR}"><b>{_escape(_("Delete"))}</b></span>'
        origin = _("deleted on {device}").format(device=change.device)
    else:
        kind = _escape(_("Add") if change.kind == ADD else _("Update"))
        origin = _("from {device}").format(device=change.device)
    if when:
        origin = f"{origin}, {when}"
    details = "\n".join(_escape(line) for line in [*_change_lines(change), origin])
    text = (
        f'<b>{_escape(change.server.name)}</b>\n<small><span alpha="65%">{details}</span></small>'
    )
    note = change.warning
    if not note and change.kind == UPDATE and not change.recommended:
        note = _("Both copies were edited; check before applying.")
    if note:
        text += f'\n<small><span foreground="{WARNING_COLOR}">⚠ {_escape(note)}</span></small>'
    return kind, text


def device_summary(result):
    if result.snapshot is None:
        state = result.error
    else:
        state = _("{count} server(s)").format(count=len(result.snapshot.shared.servers))
        if result.snapshot.refused:
            state += " · " + _("{count} refused (SSH options that run local programs)").format(
                count=result.snapshot.refused
            )
    return f'<b>{_escape(result.device.name)}</b>  <span alpha="65%">{_escape(state)}</span>'


class SyncDialog(Gtk.Dialog):
    """Fetches the other devices' servers on open and lists the changes."""

    def __init__(self, parent, guake, collect=serversync.collect):
        super().__init__(title=_("Sync servers"), transient_for=parent, modal=True)
        addonstyle.mark(self)
        self.guake = guake
        self.store = guake.servers
        self.changes = []
        self.group_colors = {}
        self._collect = collect
        self._closed = False
        self.set_default_size(560, 460)
        self.add_button(_("_Close"), Gtk.ResponseType.CLOSE)
        self.apply_button = self.add_button(_("_Apply"), Gtk.ResponseType.APPLY)
        self.apply_button.get_style_context().add_class("suggested-action")
        self.apply_button.set_sensitive(False)
        self.connect("destroy", self._on_destroy)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=16)
        self.get_content_area().pack_start(box, True, True, 0)
        box.pack_start(self._build_share_row(), False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self._build_loading(), "loading")
        self.message_label = Gtk.Label(
            wrap=True, max_width_chars=60, justify=Gtk.Justification.CENTER
        )
        self.stack.add_named(self.message_label, "message")
        self.stack.add_named(self._build_results(), "results")
        box.pack_start(self.stack, True, True, 0)
        self.show_all()
        self.refresh()

    # -- layout ------------------------------------------------------------------

    def _build_share_row(self):
        row = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(xalign=0.0)
        title.set_markup(f"<b>{_escape(_('Share this computer’s servers'))}</b>")
        hint = Gtk.Label(
            label=_(
                "Lets your other devices sync from this one. Only devices signed in to "
                "your Tailscale account can connect. Names, hosts, users, key paths, SSH "
                "options and commands are shared; passwords and key files never are."
            ),
            xalign=0.0,
            wrap=True,
            max_width_chars=55,
        )
        hint.get_style_context().add_class("dim-label")
        text.pack_start(title, False, False, 0)
        text.pack_start(hint, False, False, 0)
        self.share_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        settings = getattr(getattr(self.guake, "settings", None), "general", None)
        if settings is not None:
            settings.bind(SHARE_SETTING, self.share_switch, "active", Gio.SettingsBindFlags.DEFAULT)
        row.pack_start(text, True, True, 0)
        row.pack_end(self.share_switch, False, False, 0)
        return row

    @staticmethod
    def _build_loading():
        box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(active=True)
        box.pack_start(spinner, False, False, 0)
        box.pack_start(
            Gtk.Label(label=_("Looking for your devices on Tailscale…")), False, False, 0
        )
        return box

    def _build_results(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.devices_label = Gtk.Label(xalign=0.0, wrap=True, selectable=True)
        box.pack_start(self.devices_label, False, False, 0)

        self.model = Gtk.ListStore(bool, str, str, int)
        self.view = Gtk.TreeView(model=self.model, headers_visible=False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_toggled)
        self.view.append_column(Gtk.TreeViewColumn("", toggle, active=COL_APPLY))
        self.view.append_column(
            Gtk.TreeViewColumn("", Gtk.CellRendererText(xpad=6), markup=COL_KIND)
        )
        text = Gtk.CellRendererText(ypad=4)
        column = Gtk.TreeViewColumn("", text, markup=COL_TEXT)
        column.set_expand(True)
        self.view.append_column(column)
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN, vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.view)
        self.changes_box = scrolled
        box.pack_start(scrolled, True, True, 0)

        # Ticked like a recommended change; shown when there is something to take.
        self.group_colors_check = Gtk.CheckButton(active=True, no_show_all=True)
        self.group_colors_check.connect("toggled", lambda *args: self._update_apply())
        box.pack_start(self.group_colors_check, False, False, 0)

        self.hint_label = Gtk.Label(xalign=0.0, wrap=True, max_width_chars=70)
        self.hint_label.get_style_context().add_class("dim-label")
        box.pack_start(self.hint_label, False, False, 0)
        return box

    # -- fetching ----------------------------------------------------------------

    def refresh(self):
        self.stack.set_visible_child_name("loading")
        self.apply_button.set_sensitive(False)
        threading.Thread(target=self._fetch, name="guake-server-sync-fetch", daemon=True).start()

    def _fetch(self):
        try:
            collected, error = self._collect(), None
        except serversync.SyncError as e:
            collected, error = None, str(e)
        except Exception as e:  # pylint: disable=broad-except
            log.exception("Server sync failed")
            collected, error = None, str(e)
        GLib.idle_add(self.show_collected, collected, error)

    def show_collected(self, collected, error):
        if self._closed:
            return False
        if error is not None:
            self._show_message(_("Cannot sync: {error}").format(error=error))
            return False
        if not collected.results:
            self._show_message(
                _(
                    "No other devices were found on your Tailscale account. Sign in to "
                    "Tailscale with the same account on your other computers."
                )
            )
            return False
        peers = {
            r.device.name: r.snapshot.shared for r in collected.results if r.snapshot is not None
        }
        self.changes = serversync.plan_sync(self.store.servers, self.store.deleted, peers)
        self.group_colors = serversync.plan_group_colors(self.store.group_colors, peers)
        self.group_colors_check.set_label(
            _("Use the group colors picked on the other devices: {groups}").format(
                groups=", ".join(sorted(self.group_colors))
            )
        )
        self.group_colors_check.set_visible(bool(self.group_colors))
        self.devices_label.set_markup("\n".join(device_summary(r) for r in collected.results))
        self._fill_changes(reachable=bool(peers))
        self.stack.set_visible_child_name("results")
        return False

    def _show_message(self, text):
        self.message_label.set_text(text)
        self.stack.set_visible_child_name("message")

    def _fill_changes(self, reachable=True):
        self.model.clear()
        now = time.time()
        for index, change in enumerate(self.changes):
            kind, text = describe_change(change, now)
            self.model.append([change.recommended, kind, text, index])
        self.changes_box.set_visible(bool(self.changes))
        if not reachable:
            self.hint_label.set_text(
                _(
                    "Could not get the servers of any device. Turn on sharing in the "
                    "Sync servers window of the other computers, with Guake running."
                )
            )
        elif not self.changes and self.group_colors:
            self.hint_label.set_text(_("The servers are already in sync."))
        elif not self.changes:
            self.hint_label.set_text(_("Everything is already in sync."))
        else:
            hints = [_("Nothing is changed until you press Apply.")]
            if any(needs_confirmation(c) for c in self.changes):
                hints.append(
                    _("Deletions and changes marked ⚠ are not ticked; tick them to confirm.")
                )
            if any(c.kind == ADD and c.server.use_password for c in self.changes):
                hints.append(_("Passwords are not synced; you will be asked for them on connect."))
            self.hint_label.set_text(" ".join(hints))
        self._update_apply()

    def on_toggled(self, renderer, path):
        self.model[path][COL_APPLY] = not self.model[path][COL_APPLY]
        self._update_apply()

    def _update_apply(self):
        self.apply_button.set_sensitive(
            bool(self.selected_changes() or self.selected_group_colors())
        )

    def selected_group_colors(self):
        return self.group_colors if self.group_colors_check.get_active() else {}

    def selected_changes(self):
        return [self.changes[row[COL_INDEX]] for row in self.model if row[COL_APPLY]]

    def _on_destroy(self, *args):
        self._closed = True

    # -- applying ----------------------------------------------------------------

    def confirm(self, changes):
        """Ask before deletions and changes marked with a warning."""
        deletions = [c for c in changes if c.kind == DELETE]
        flagged = [c for c in changes if c.kind != DELETE]
        if deletions and not flagged:
            title = _("Delete {count} server(s) from this computer?").format(count=len(deletions))
            button = _("_Delete")
        else:
            title = _("Apply {count} change(s) that need a closer look?").format(count=len(changes))
            button = _("_Apply")
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        addonstyle.mark(dialog)
        lines = [
            _("• Delete {name} (deleted on {device})").format(name=c.server.name, device=c.device)
            for c in deletions
        ] + [
            _("• {kind} {name}: {warning}").format(
                kind=_("Add") if c.kind == ADD else _("Update"),
                name=c.server.name,
                warning=c.warning or _("both copies were edited"),
            )
            for c in flagged
        ]
        if deletions:
            lines += ["", _("Saved passwords of deleted servers are removed from this computer.")]
        dialog.format_secondary_text("\n".join(lines))
        dialog.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(button, Gtk.ResponseType.YES).get_style_context().add_class(
            "destructive-action"
        )
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def apply(self):
        """Write the ticked changes. Returns False when the user backs out
        of the confirmation, so the dialog stays open."""
        selected = self.selected_changes()
        to_confirm = [c for c in selected if needs_confirmation(c) or not c.recommended]
        if to_confirm and not self.confirm(to_confirm):
            return False
        serversync.apply_changes(self.store, selected)
        self.store.apply_group_colors(self.selected_group_colors())
        for change in selected:
            if change.kind == DELETE and change.server.use_password:
                serversecrets.clear_password(change.server.id)
        log.info(
            "Server sync applied: %s",
            ", ".join(f"{c.kind} {c.server.name} from {c.device}" for c in selected),
        )
        return True


def sync_servers(parent, guake):
    """Run the sync dialog. Returns True when changes were written."""
    dialog = SyncDialog(parent, guake)
    try:
        while dialog.run() == Gtk.ResponseType.APPLY:
            if dialog.apply():
                return True
        return False
    finally:
        dialog.destroy()
