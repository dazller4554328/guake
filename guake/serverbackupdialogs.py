# -*- coding: utf-8; -*-
"""
GTK dialogs to export the saved servers to a backup file and to import one,
typically to set Guake up on another machine. The file format and the
merge logic live in :mod:`guake.serverbackup`.
"""

import datetime
import logging
import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from guake import serverbackup
from guake import serversecrets

log = logging.getLogger(__name__)

MIN_PASSPHRASE_LENGTH = 12
JSON_PATTERN = "*.json"


def _message(parent, message_type, text, secondary=None):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        destroy_with_parent=True,
        message_type=message_type,
        buttons=Gtk.ButtonsType.OK,
        text=text,
    )
    if secondary:
        dialog.format_secondary_text(secondary)
    dialog.run()
    dialog.destroy()


def _dim_label(text):
    label = Gtk.Label(label=text, xalign=0.0, wrap=True, max_width_chars=50)
    label.get_style_context().add_class("dim-label")
    return label


def _password_entry(placeholder):
    entry = Gtk.Entry(
        visibility=False, input_purpose=Gtk.InputPurpose.PASSWORD, activates_default=True
    )
    entry.set_placeholder_text(placeholder)
    return entry


def _json_filter():
    json_filter = Gtk.FileFilter()
    json_filter.set_name(_("Guake server backups (*.json)"))
    json_filter.add_pattern(JSON_PATTERN)
    return json_filter


class ExportDialog(Gtk.Dialog):
    """Asks whether to include passwords and keys, and the passphrase that
    encrypts them."""

    def __init__(self, parent, count):
        super().__init__(title=_("Export servers"), transient_for=parent, modal=True)
        self.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
        self.add_button(_("_Export..."), Gtk.ResponseType.OK).get_style_context().add_class(
            "suggested-action"
        )
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=16)
        self.get_content_area().pack_start(box, True, True, 0)
        box.pack_start(
            Gtk.Label(
                label=_(
                    "Save your {count} server(s) to a file, then use Import backup... "
                    "in Guake on the other machine."
                ).format(count=count),
                xalign=0.0,
                wrap=True,
                max_width_chars=50,
            ),
            False,
            False,
            0,
        )

        self.include_secrets = Gtk.CheckButton(
            label=_("Include saved passwords and private keys"), margin_top=8
        )
        box.pack_start(self.include_secrets, False, False, 0)
        if serverbackup.is_encryption_available():
            hint = _(
                "They are encrypted with a passphrase you choose here. You will need it "
                "to import the file."
            )
        else:
            hint = _("Install python3-cryptography to include passwords and keys.")
            self.include_secrets.set_sensitive(False)
        box.pack_start(_dim_label(hint), False, False, 0)

        self.passphrase = _password_entry(
            _("Passphrase (at least {count} characters)").format(count=MIN_PASSPHRASE_LENGTH)
        )
        self.confirm = _password_entry(_("Repeat the passphrase"))
        for entry in (self.passphrase, self.confirm):
            box.pack_start(entry, False, False, 0)
        self.include_secrets.connect("toggled", self._on_toggled)
        self._on_toggled(self.include_secrets)
        self.show_all()

    def _on_toggled(self, check):
        for entry in (self.passphrase, self.confirm):
            entry.set_sensitive(check.get_active())
        if check.get_active():
            self.passphrase.grab_focus()

    def validation_error(self):
        if not self.include_secrets.get_active():
            return None
        if len(self.passphrase.get_text()) < MIN_PASSPHRASE_LENGTH:
            return _("The passphrase needs at least {count} characters.").format(
                count=MIN_PASSPHRASE_LENGTH
            )
        if self.passphrase.get_text() != self.confirm.get_text():
            return _("The two passphrases are different.")
        return None

    def run_for_options(self):
        """``(include_secrets, passphrase)``, or None when cancelled."""
        while self.run() == Gtk.ResponseType.OK:
            error = self.validation_error()
            if error is None:
                include = self.include_secrets.get_active()
                passphrase = self.passphrase.get_text() if include else ""
                self.passphrase.set_text("")
                self.confirm.set_text("")
                return include, passphrase
            _message(self, Gtk.MessageType.ERROR, error)
        return None


def _choose_file(parent, action, title, current_name=None):
    chooser = Gtk.FileChooserDialog(title=title, transient_for=parent, action=action)
    accept = _("_Save") if action == Gtk.FileChooserAction.SAVE else _("_Open")
    chooser.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL, accept, Gtk.ResponseType.OK)
    chooser.set_do_overwrite_confirmation(True)
    chooser.add_filter(_json_filter())
    chooser.set_current_folder(os.path.expanduser("~"))
    if current_name:
        chooser.set_current_name(current_name)
    filename = chooser.get_filename() if chooser.run() == Gtk.ResponseType.OK else None
    chooser.destroy()
    return filename


def export_servers(parent, servers):
    """Run the whole export: options, file name, writing. Returns the path
    written, or None."""
    dialog = ExportDialog(parent, len(servers))
    options = dialog.run_for_options()
    dialog.destroy()
    if options is None:
        return None
    include_secrets, passphrase = options
    today = datetime.date.today().isoformat()
    path = _choose_file(
        parent,
        Gtk.FileChooserAction.SAVE,
        _("Export servers to"),
        current_name=f"guake-servers-{today}{serverbackup.BACKUP_SUFFIX}",
    )
    if path is None:
        return None
    secrets = serverbackup.collect_secrets(servers, serversecrets) if include_secrets else None
    try:
        serverbackup.write_backup(
            path, serverbackup.build_backup(servers, secrets, passphrase=passphrase)
        )
    except (OSError, ValueError) as e:
        log.error("Cannot export servers to %s: %s", path, e)
        _message(parent, Gtk.MessageType.ERROR, _("The backup could not be written."), str(e))
        return None
    details = ""
    if secrets is not None:
        details = _("Includes {passwords} password(s) and {keys} private key(s).").format(
            passwords=len(secrets.passwords), keys=len(secrets.keys)
        )
    _message(
        parent,
        Gtk.MessageType.INFO,
        _("Exported {count} server(s) to {path}.").format(count=len(servers), path=path),
        details or None,
    )
    return path


class PassphraseDialog(Gtk.Dialog):
    """Asks for the passphrase of a backup that contains secrets."""

    SKIP = 1

    def __init__(self, parent):
        super().__init__(title=_("Import servers"), transient_for=parent, modal=True)
        self.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
        self.add_button(_("Import _without passwords"), self.SKIP)
        self.add_button(_("_Import"), Gtk.ResponseType.OK).get_style_context().add_class(
            "suggested-action"
        )
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=16)
        self.get_content_area().pack_start(box, True, True, 0)
        box.pack_start(
            Gtk.Label(
                label=_("This backup contains encrypted passwords and private keys."),
                xalign=0.0,
            ),
            False,
            False,
            0,
        )
        box.pack_start(
            _dim_label(_("Enter the passphrase chosen when it was exported.")), False, False, 0
        )
        self.passphrase = _password_entry(_("Passphrase"))
        box.pack_start(self.passphrase, False, False, 0)
        self.error = Gtk.Label(xalign=0.0, no_show_all=True)
        self.error.get_style_context().add_class("error")
        box.pack_start(self.error, False, False, 0)
        self.show_all()

    def run_for_secrets(self, backup):
        """Decrypted Secrets, an empty Secrets when skipped, None when cancelled."""
        while True:
            response = self.run()
            if response == self.SKIP:
                return serverbackup.Secrets()
            if response != Gtk.ResponseType.OK:
                return None
            try:
                secrets = serverbackup.decrypt_secrets(backup, self.passphrase.get_text())
                self.passphrase.set_text("")
                return secrets
            except serverbackup.WrongPassphrase:
                self.error.set_text(_("Wrong passphrase, please try again."))
                self.error.show()
                self.passphrase.select_region(0, -1)
                self.passphrase.grab_focus()


def _risky_details(server):
    lines = [f"\u2022 {server.name} ({server.target})"]
    for label, value in (
        (_("ssh options"), server.options),
        (_("jump host"), server.jump_host),
        (_("command after login"), server.command),
    ):
        if value:
            lines.append(f"    {label}: {value}")
    return "\n".join(lines)


def confirm_risky_servers(parent, servers):
    """Show the settings of imported servers that make ssh run commands or
    connect through another host, and let the user refuse the import. A
    backup from someone else could otherwise hide e.g. a ProxyCommand."""
    risky = serverbackup.risky_servers(servers)
    if not risky:
        return True
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.WARNING,
        buttons=Gtk.ButtonsType.NONE,
        text=_("Check these server settings before importing"),
    )
    dialog.format_secondary_text(
        _(
            "They run commands or route the connection when you connect. Only import "
            "them if you trust where this backup comes from."
        )
    )
    text = Gtk.TextView(
        editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR
    )
    text.get_buffer().set_text("\n".join(_risky_details(s) for s in risky))
    scrolled = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN, min_content_height=120)
    scrolled.set_max_content_height(260)
    scrolled.set_propagate_natural_height(True)
    scrolled.add(text)
    dialog.get_message_area().pack_start(scrolled, True, True, 0)
    dialog.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
    dialog.add_button(_("_Import"), Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.CANCEL)
    scrolled.show_all()
    accepted = dialog.run() == Gtk.ResponseType.OK
    dialog.destroy()
    return accepted


def import_servers(parent, store):
    """Run the whole import: choose the file, decrypt, merge. Returns the
    :class:`guake.serverbackup.ImportSummary`, or None."""
    path = _choose_file(parent, Gtk.FileChooserAction.OPEN, _("Import servers from"))
    if path is None:
        return None
    try:
        backup = serverbackup.load_backup(path)
        if not confirm_risky_servers(parent, backup.servers):
            return None
        secrets = serverbackup.Secrets()
        if backup.has_secrets:
            dialog = PassphraseDialog(parent)
            secrets = dialog.run_for_secrets(backup)
            dialog.destroy()
            if secrets is None:
                return None
        summary = serverbackup.import_backup(store, backup.servers, secrets, serversecrets)
    except (serverbackup.BackupError, OSError, ValueError) as e:
        log.error("Cannot import servers from %s: %s", path, e)
        _message(parent, Gtk.MessageType.ERROR, _("The backup could not be imported."), str(e))
        return None
    _message(
        parent,
        Gtk.MessageType.INFO,
        _(
            "Imported {added} new server(s), updated {updated}, {unchanged} already up to date."
        ).format(
            added=len(summary.added), updated=len(summary.updated), unchanged=len(summary.unchanged)
        ),
        _("Restored {passwords} password(s) and {keys} private key(s).").format(
            passwords=summary.passwords, keys=summary.keys
        ),
    )
    return summary
