# -*- coding: utf-8; -*-
"""
The SFTP panel: a remote file browser plus a transfer queue, shown next to
the terminal of a tab (see :meth:`guake.boxes.RootTerminalBox.open_sftp_panel`).

Two sftp sessions back the panel, each on its own worker thread: one for
browsing (so the listing stays responsive) and one for transfers, created
on the first upload or download. Everything that touches widgets runs on
the GTK main loop; worker results are marshalled with ``GLib.idle_add``.
"""

import logging
import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gio
from gi.repository import Gtk
from gi.repository import Pango

from guake import sftpdialogs
from guake.sftp import SftpWorker
from guake.sftp import format_size
from guake.sftp import remote_join
from guake.sftp import remote_parent
from guake.utils import HidePrevention

log = logging.getLogger(__name__)

PANEL_WIDTH = 440
URI_TARGET = "text/uri-list"
COL_ICON, COL_NAME, COL_SIZE, COL_MODIFIED, COL_PERMS, COL_ENTRY = range(6)
TCOL_ICON, TCOL_NAME, TCOL_PROGRESS, TCOL_STATUS, TCOL_TRANSFER = range(5)
ICON_BY_KIND = {
    "dir": "folder-symbolic",
    "file": "text-x-generic-symbolic",
    "link": "emblem-symbolic-link",
    "other": "text-x-generic-symbolic",
}
DOWNLOAD = "download"
UPLOAD = "upload"


class Transfer:
    """One queued, running or finished upload/download shown in the queue."""

    def __init__(self, direction, name, local, remote, is_dir):
        self.direction = direction
        self.name = name
        self.local = local  # file/dir to send, or the folder to receive into
        self.remote = remote  # file/dir to fetch, or the folder to receive into
        self.is_dir = is_dir
        self.job = None
        self.row = None
        self.finished = False

    @property
    def icon(self):
        return "document-save-symbolic" if self.direction == DOWNLOAD else "document-send-symbolic"

    def run(self, session, progress_cb):
        if self.direction == DOWNLOAD:
            session.download(self.remote, self.local, self.is_dir, progress_cb)
        else:
            session.upload(self.local, self.remote, self.is_dir, progress_cb)


class SftpPanel(Gtk.Box):
    """Remote file browser for one server.

    ``session_factory(prompt_handler)`` returns a fresh, not yet started
    :class:`guake.sftp.SftpSession`; ``on_close`` is called when the user
    closes the panel (the owner removes and destroys it).
    """

    last_download_dir = None
    last_upload_dir = None

    def __init__(self, window, server_name, session_factory, on_close):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.window = window
        self.server_name = server_name
        self.session_factory = session_factory
        self.on_close = on_close
        self.current_dir = None
        self.entries = []
        self.context_menu = None
        self._closed = False
        self.set_size_request(PANEL_WIDTH, -1)
        self.set_border_width(4)

        self.prompter = sftpdialogs.MainLoopPrompter(window, server_name)
        self.browser = SftpWorker(session_factory(self.prompter), "sftp-browse")
        self.browser.start()
        self.transfers = None

        self._build_header()
        self._build_toolbar()
        self._build_file_view()
        self._build_transfer_view()
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("dim-label")
        self.pack_start(self.status, False, False, 0)
        self.connect("destroy", self._on_destroy)
        self.load()

    # -- widgets ---------------------------------------------------------------

    def _build_header(self):
        header = Gtk.Box(spacing=6)
        header.pack_start(
            Gtk.Image.new_from_icon_name("folder-remote-symbolic", Gtk.IconSize.MENU),
            False,
            False,
            0,
        )
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.set_markup(f"<b>{GLib.markup_escape_text(self.server_name)}</b>")
        header.pack_start(title, True, True, 0)
        close = self._icon_button("window-close-symbolic", _("Close the SFTP panel"), self.close)
        header.pack_end(close, False, False, 0)
        self.pack_start(header, False, False, 0)

    def _build_toolbar(self):
        bar = Gtk.Box(spacing=2)
        bar.pack_start(self._icon_button("go-up-symbolic", _("Parent folder"), self.go_up), 0, 0, 0)
        bar.pack_start(
            self._icon_button("go-home-symbolic", _("Home folder"), self.go_home), 0, 0, 0
        )
        bar.pack_start(
            self._icon_button("view-refresh-symbolic", _("Refresh"), self.refresh), False, False, 0
        )
        self.path_entry = Gtk.Entry(placeholder_text=_("Remote path"))
        self.path_entry.connect("activate", lambda entry: self.load(entry.get_text().strip()))
        bar.pack_start(self.path_entry, True, True, 0)
        bar.pack_start(
            self._icon_button("document-send-symbolic", _("Upload files..."), self.upload_files),
            False,
            False,
            0,
        )
        bar.pack_start(
            self._icon_button("folder-open-symbolic", _("Upload a folder..."), self.upload_folder),
            False,
            False,
            0,
        )
        bar.pack_start(
            self._icon_button("folder-new-symbolic", _("New folder..."), self.new_folder),
            False,
            False,
            0,
        )
        self.pack_start(bar, False, False, 0)

    @staticmethod
    def _icon_button(icon, tooltip, callback):
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_tooltip_text(tooltip)
        button.set_can_focus(False)
        button.connect("clicked", lambda *args: callback())
        return button

    def _build_file_view(self):
        self.model = Gtk.ListStore(str, str, str, str, str, GObject.TYPE_PYOBJECT)
        self.view = Gtk.TreeView(model=self.model)
        self.view.set_tooltip_column(COL_PERMS)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        name_column = Gtk.TreeViewColumn(_("Name"))
        icon = Gtk.CellRendererPixbuf()
        name = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
        name_column.pack_start(icon, False)
        name_column.pack_start(name, True)
        name_column.add_attribute(icon, "icon-name", COL_ICON)
        name_column.add_attribute(name, "text", COL_NAME)
        name_column.set_expand(True)
        self.view.append_column(name_column)
        size = Gtk.CellRendererText(xalign=1.0)
        self.view.append_column(Gtk.TreeViewColumn(_("Size"), size, text=COL_SIZE))
        self.view.append_column(
            Gtk.TreeViewColumn(_("Modified"), Gtk.CellRendererText(), text=COL_MODIFIED)
        )
        self.view.connect("row-activated", self._on_row_activated)
        self.view.connect("button-press-event", self._on_button_press)
        self.view.connect("key-press-event", self._on_key_press)
        self.view.drag_dest_set(
            Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new(URI_TARGET, 0, 0)], Gdk.DragAction.COPY
        )
        self.view.connect("drag-data-received", self._on_drag_data_received)
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.view)
        self.pack_start(scrolled, True, True, 0)

    def _build_transfer_view(self):
        self.transfer_model = Gtk.ListStore(str, str, int, str, GObject.TYPE_PYOBJECT)
        self.transfer_view = Gtk.TreeView(model=self.transfer_model, headers_visible=False)
        self.transfer_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        column = Gtk.TreeViewColumn(_("Transfers"))
        icon = Gtk.CellRendererPixbuf()
        name = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
        column.pack_start(icon, False)
        column.pack_start(name, True)
        column.add_attribute(icon, "icon-name", TCOL_ICON)
        column.add_attribute(name, "text", TCOL_NAME)
        column.set_expand(True)
        self.transfer_view.append_column(column)
        progress = Gtk.CellRendererProgress()
        self.transfer_view.append_column(
            Gtk.TreeViewColumn(_("Progress"), progress, value=TCOL_PROGRESS, text=TCOL_STATUS)
        )
        scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(90)
        scrolled.add(self.transfer_view)

        bar = Gtk.Box(spacing=2)
        label = Gtk.Label(label=_("Transfers"), xalign=0)
        label.get_style_context().add_class("dim-label")
        bar.pack_start(label, True, True, 0)
        bar.pack_end(
            self._icon_button("edit-clear-all-symbolic", _("Clear finished"), self.clear_finished),
            False,
            False,
            0,
        )
        bar.pack_end(
            self._icon_button("process-stop-symbolic", _("Cancel selected"), self.cancel_selected),
            False,
            False,
            0,
        )
        self.pack_start(bar, False, False, 0)
        self.pack_start(scrolled, False, False, 0)

    # -- threading helpers -------------------------------------------------------

    def _on_main(self, callback):
        """Wrap a job callback so it runs on the GTK main loop, and not at
        all once the panel is gone."""

        def done(result, error):
            GLib.idle_add(self._dispatch, callback, result, error)

        return done

    def _dispatch(self, callback, result, error):
        if not self._closed:
            callback(result, error)
        return False

    def _set_status(self, text):
        self.status.set_text(text)
        self.status.set_tooltip_text(text)

    # -- browsing ----------------------------------------------------------------

    def load(self, path=None):
        """List ``path`` (the remote home directory when None)."""
        self._set_status(_("Loading...") if self.current_dir else _("Connecting..."))
        self.browser.submit(
            lambda session: session.listdir(path or session.home or "."),
            self._on_main(self._on_listing),
            "list",
        )

    def _on_listing(self, result, error):
        if error is not None:
            self._set_status(str(error))
            return
        self.current_dir, self.entries = result
        self.path_entry.set_text(self.current_dir)
        self.model.clear()
        for entry in self.entries:
            self.model.append(
                [
                    ICON_BY_KIND.get(entry.kind, ICON_BY_KIND["other"]),
                    entry.name,
                    "" if entry.is_dir else format_size(entry.size),
                    entry.modified,
                    entry.permissions,
                    entry,
                ]
            )
        count = len(self.entries)
        self._set_status(_("{count} items").format(count=count) if count != 1 else _("1 item"))

    def refresh(self):
        if self.current_dir:
            self.load(self.current_dir)

    def go_up(self):
        if self.current_dir:
            self.load(remote_parent(self.current_dir))

    def go_home(self):
        self.load(None)

    def _selected_entries(self):
        model, paths = self.view.get_selection().get_selected_rows()
        return [model[path][COL_ENTRY] for path in paths]

    def _on_row_activated(self, view, path, column):
        entry = self.model[path][COL_ENTRY]
        if entry.is_dir or entry.is_link:
            self.load(remote_join(self.current_dir, entry.name))
        else:
            self.download_entries([entry])

    def _on_key_press(self, view, event):
        if event.keyval == Gdk.KEY_BackSpace:
            self.go_up()
        elif event.keyval == Gdk.KEY_Delete:
            self.delete_selected()
        elif event.keyval == Gdk.KEY_F2:
            self.rename_selected()
        elif event.keyval == Gdk.KEY_F5:
            self.refresh()
        else:
            return False
        return True

    def _on_button_press(self, view, event):
        if event.button != 3:
            return False
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        if hit is not None and not view.get_selection().path_is_selected(hit[0]):
            view.get_selection().unselect_all()
            view.get_selection().select_path(hit[0])
        self._popup_context_menu(event)
        return True

    def _popup_context_menu(self, event):
        entries = self._selected_entries()
        menu = Gtk.Menu()
        one = len(entries) == 1
        items = [
            (_("Download..."), self.download_selected, bool(entries)),
            (
                _("Open"),
                lambda: self._on_row_activated(self.view, self._selected_path(), None),
                one and entries[0].is_dir,
            ),
            (_("Rename..."), self.rename_selected, one),
            (_("Delete"), self.delete_selected, bool(entries)),
            (None, None, None),
            (_("Upload files..."), self.upload_files, True),
            (_("Upload a folder..."), self.upload_folder, True),
            (_("New folder..."), self.new_folder, True),
            (None, None, None),
            (_("Copy path"), self.copy_path, one),
            (_("Refresh"), self.refresh, True),
        ]
        for label, callback, sensitive in items:
            if label is None:
                menu.add(Gtk.SeparatorMenuItem())
                continue
            item = Gtk.MenuItem(label=label)
            item.set_sensitive(sensitive)
            item.connect("activate", lambda _item, cb=callback: cb())
            menu.add(item)
        menu.show_all()
        # Keep a reference or the menu is collected while shown.
        self.context_menu = menu
        HidePrevention(self.window).prevent()
        menu.connect("hide", lambda *args: HidePrevention(self.window).allow())
        try:
            menu.popup_at_pointer(event)
        except AttributeError:  # Gtk < 3.22
            menu.popup(None, None, None, None, event.button, event.time)

    def _selected_path(self):
        _model, paths = self.view.get_selection().get_selected_rows()
        return paths[0] if paths else None

    def copy_path(self):
        entries = self._selected_entries()
        if entries:
            path = remote_join(self.current_dir, entries[0].name)
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(path, -1)
            self._set_status(_("Copied {path}").format(path=path))

    # -- file operations -----------------------------------------------------

    def _run_then_refresh(self, func, description):
        """Run ``func(session)`` on the browsing session, then reload the
        listing; errors are shown in a dialog."""

        def done(_result, error):
            if error is not None:
                sftpdialogs.show_error(self.window, description, str(error))
            self.refresh()

        self.browser.submit(func, self._on_main(done), description)

    def new_folder(self):
        name = sftpdialogs.ask_text(
            self.window, _("New folder"), _("Folder name:"), ok_label=_("Create")
        )
        if name and self.current_dir:
            path = remote_join(self.current_dir, name)
            self._run_then_refresh(lambda s: s.mkdir(path), _("Cannot create folder"))

    def rename_selected(self):
        entries = self._selected_entries()
        if len(entries) != 1:
            return
        old_name = entries[0].name
        name = sftpdialogs.ask_text(
            self.window, _("Rename"), _("New name for {name}:").format(name=old_name), old_name
        )
        if name and name != old_name:
            old = remote_join(self.current_dir, old_name)
            new = remote_join(self.current_dir, name)
            self._run_then_refresh(lambda s: s.rename(old, new), _("Cannot rename"))

    def delete_selected(self):
        entries = self._selected_entries()
        if not entries:
            return
        names = [e.name for e in entries]
        ok = sftpdialogs.confirm(
            self.window,
            _("Delete {count} item(s) from {server}?").format(
                count=len(names), server=self.server_name
            ),
            _("Folders are deleted with everything inside them. This cannot be undone.\n\n")
            + sftpdialogs.list_names(names),
            ok_label=_("Delete"),
            destructive=True,
        )
        if not ok:
            return
        targets = [(remote_join(self.current_dir, e.name), e.is_dir) for e in entries]

        def remove_all(session):
            for path, is_dir in targets:
                if is_dir:
                    session.remove_dir(path)
                else:
                    session.remove_file(path)

        self._run_then_refresh(remove_all, _("Cannot delete"))

    # -- transfers -----------------------------------------------------------

    def _transfer_worker(self):
        if self.transfers is None:
            self.transfers = SftpWorker(self.session_factory(self.prompter), "sftp-transfer")
            self.transfers.start()
        return self.transfers

    def download_selected(self):
        self.download_entries(self._selected_entries())

    def download_entries(self, entries):
        if not entries or not self.current_dir:
            return
        folder = sftpdialogs.choose_folder(
            self.window,
            _("Download to"),
            SftpPanel.last_download_dir or sftpdialogs.default_download_dir(),
            _("Download here"),
        )
        if not folder:
            return
        SftpPanel.last_download_dir = folder
        for entry in entries:
            local = os.path.join(folder, entry.name)
            if os.path.exists(local) and not sftpdialogs.confirm(
                self.window,
                _("Replace {name}?").format(name=entry.name),
                _("{path} already exists.").format(path=local),
                ok_label=_("Replace"),
            ):
                continue
            remote = remote_join(self.current_dir, entry.name)
            self._queue(Transfer(DOWNLOAD, entry.name, folder, remote, entry.is_dir))

    def upload_files(self):
        paths = sftpdialogs.choose_files(
            self.window,
            _("Upload to {path}").format(path=self.current_dir or ""),
            SftpPanel.last_upload_dir,
        )
        self.upload_paths(paths)

    def upload_folder(self):
        folder = sftpdialogs.choose_folder(
            self.window,
            _("Upload a folder to {path}").format(path=self.current_dir or ""),
            SftpPanel.last_upload_dir,
            _("Upload"),
        )
        if folder:
            self.upload_paths([folder])

    def upload_paths(self, paths):
        """Queue an upload of each local path into the current directory."""
        if not paths or not self.current_dir:
            return
        SftpPanel.last_upload_dir = os.path.dirname(paths[0])
        existing = {entry.name for entry in self.entries}
        for path in paths:
            name = os.path.basename(path.rstrip("/"))
            if name in existing and not sftpdialogs.confirm(
                self.window,
                _("Replace {name}?").format(name=name),
                _("{name} already exists in {path}.").format(name=name, path=self.current_dir),
                ok_label=_("Replace"),
            ):
                continue
            self._queue(Transfer(UPLOAD, name, path, self.current_dir, os.path.isdir(path)))

    def _on_drag_data_received(self, widget, context, x, y, data, info, time):
        paths = []
        for uri in data.get_uris():
            path = Gio.File.new_for_uri(uri).get_path()
            if path:
                paths.append(path)
        self.upload_paths(paths)

    def _queue(self, transfer):
        transfer.row = self.transfer_model.append(
            [transfer.icon, transfer.name, 0, _("Queued"), transfer]
        )

        def progress(update):
            GLib.idle_add(self._on_progress, transfer, update)

        def done(_result, error):
            self._on_transfer_done(transfer, error)

        transfer.job = self._transfer_worker().submit(
            lambda session: transfer.run(session, progress), self._on_main(done), transfer.name
        )

    def _on_progress(self, transfer, update):
        if self._closed or transfer.finished:
            return False
        status = f"{update.percent}%  {update.done}  {update.rate}"
        if update.eta:
            status += f"  {update.eta}"
        if transfer.is_dir and update.name:
            status = f"{update.name}  {status}"
        self.transfer_model[transfer.row][TCOL_PROGRESS] = update.percent
        self.transfer_model[transfer.row][TCOL_STATUS] = status
        return False

    def _on_transfer_done(self, transfer, error):
        transfer.finished = True
        row = self.transfer_model[transfer.row]
        if error is None:
            row[TCOL_PROGRESS] = 100
            row[TCOL_STATUS] = _("Done")
        else:
            row[TCOL_STATUS] = str(error)
        if transfer.direction == UPLOAD and transfer.remote == self.current_dir:
            self.refresh()

    def _selected_transfers(self):
        model, paths = self.transfer_view.get_selection().get_selected_rows()
        return [model[path][TCOL_TRANSFER] for path in paths]

    def cancel_selected(self):
        for transfer in self._selected_transfers():
            if not transfer.finished and self.transfers is not None:
                self.transfers.cancel(transfer.job)
                self.transfer_model[transfer.row][TCOL_STATUS] = _("Cancelling...")

    def clear_finished(self):
        row = self.transfer_model.get_iter_first()
        while row is not None:
            next_row = self.transfer_model.iter_next(row)
            if self.transfer_model[row][TCOL_TRANSFER].finished:
                self.transfer_model.remove(row)
            row = next_row

    def active_transfer_count(self):
        return sum(1 for row in self.transfer_model if not row[TCOL_TRANSFER].finished)

    # -- lifecycle -----------------------------------------------------------

    def close(self):
        active = self.active_transfer_count()
        if active and not sftpdialogs.confirm(
            self.window,
            _("{count} transfer(s) still running. Close the panel anyway?").format(count=active),
            _("Running transfers are stopped; queued ones are dropped."),
            ok_label=_("Close"),
            destructive=True,
        ):
            return
        self.on_close(self)

    def _on_destroy(self, *args):
        self.shutdown()

    def shutdown(self):
        """Stop both sessions. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self.browser.stop()
        if self.transfers is not None:
            self.transfers.stop()
