# -*- coding: utf-8; -*-
"""
GTK dialogs for the master password: the one asking for it when Guake
starts or was locked, and the one to set, change or remove it. The
encryption lives in :mod:`guake.vault`.
"""

import logging

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio
from gi.repository import Gtk

from guake import addonstyle
from guake import serversecrets
from guake import vault as vlt

log = logging.getLogger(__name__)

AUTO_LOCK_SETTING = "vault-auto-lock-minutes"
MAX_AUTO_LOCK_MINUTES = 1440
RESPONSE_REMOVE = 1


def _message(parent, message_type, text, secondary=None, buttons=Gtk.ButtonsType.OK):
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


def _label(text, dim=False):
    label = Gtk.Label(label=text, xalign=0.0, wrap=True, max_width_chars=50)
    if dim:
        label.get_style_context().add_class("dim-label")
    return label


def _password_entry(placeholder):
    entry = Gtk.Entry(
        visibility=False, input_purpose=Gtk.InputPurpose.PASSWORD, activates_default=True
    )
    entry.set_placeholder_text(placeholder)
    return entry


def new_password_error(password, confirm):
    """Why ``password`` cannot be the new master password, or None."""
    if len(password) < vlt.MIN_PASSWORD_LENGTH:
        return _("The master password needs at least {count} characters.").format(
            count=vlt.MIN_PASSWORD_LENGTH
        )
    if password != confirm:
        return _("The two passwords are different.")
    return None


class UnlockDialog(Gtk.Dialog):
    """Asks for the master password until it opens the vault."""

    def __init__(self, vault, parent=None, quitting=False):
        super().__init__(title=_("Guake is locked"), transient_for=parent, modal=True)
        addonstyle.mark(self)
        self.vault = vault
        self.add_button(_("_Quit") if quitting else _("_Cancel"), Gtk.ResponseType.CANCEL)
        self.add_button(_("_Unlock"), Gtk.ResponseType.OK).get_style_context().add_class(
            "suggested-action"
        )
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)
        if parent is None:
            # Guake's own window is hidden: stay where the user can see it.
            self.set_position(Gtk.WindowPosition.CENTER)
            self.set_keep_above(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=16)
        self.get_content_area().pack_start(box, True, True, 0)
        box.pack_start(_label(_("Enter your master password to open Guake.")), False, False, 0)
        self.password = _password_entry(_("Master password"))
        box.pack_start(self.password, False, False, 0)
        self.error = _label("", dim=True)
        self.error.set_no_show_all(True)
        box.pack_start(self.error, False, False, 0)
        self.show_all()
        self.password.grab_focus()

    def try_unlock(self):
        """Open the vault with what was typed. Returns whether it worked;
        otherwise the reason is shown and the field emptied."""
        password = self.password.get_text()
        self.password.set_text("")
        try:
            self.vault.unlock(password)
        except vlt.WrongPassword:
            self._show_error(_("That is not the master password. Try again."))
            return False
        except vlt.VaultError as e:
            log.error("Cannot open the vault: %s", e)
            self._show_error(str(e))
            return False
        return True

    def _show_error(self, text):
        self.error.set_text(text)
        self.error.show()
        self.password.grab_focus()

    def run_until_unlocked(self):
        while self.run() == Gtk.ResponseType.OK:
            if self.try_unlock():
                return True
        return False


def ask_unlock(vault, parent=None, quitting=False):
    """Ask for the master password. Returns False when the user gave up."""
    dialog = UnlockDialog(vault, parent, quitting)
    try:
        dialog.present()
        return dialog.run_until_unlocked()
    finally:
        dialog.destroy()


class MasterPasswordDialog(Gtk.Dialog):
    """Sets the master password, or changes and removes the current one."""

    def __init__(self, parent, vault, settings=None):
        super().__init__(title=_("Master password"), transient_for=parent, modal=True)
        addonstyle.mark(self)
        self.vault = vault
        self.has_vault = vault.exists
        if self.has_vault:
            remove = self.add_button(_("_Remove master password"), RESPONSE_REMOVE)
            remove.get_style_context().add_class("destructive-action")
        self.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
        apply_label = _("C_hange password") if self.has_vault else _("_Set master password")
        self.add_button(apply_label, Gtk.ResponseType.OK).get_style_context().add_class(
            "suggested-action"
        )
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=16)
        self.get_content_area().pack_start(box, True, True, 0)
        if self.has_vault:
            intro = _("Guake asks for the master password when it starts and after locking.")
        else:
            intro = _(
                "Guake will ask for this password when it starts. Your saved servers and "
                "their passwords are encrypted with it and cannot be read without it."
            )
        box.pack_start(_label(intro), False, False, 0)

        self.current = _password_entry(_("Current master password"))
        if self.has_vault:
            box.pack_start(self.current, False, False, 0)
        self.password = _password_entry(
            _("New master password (at least {count} characters)").format(
                count=vlt.MIN_PASSWORD_LENGTH
            )
        )
        self.confirm = _password_entry(_("Repeat the new master password"))
        for entry in (self.password, self.confirm):
            box.pack_start(entry, False, False, 0)
        if not self.has_vault:
            box.pack_start(
                _label(
                    _(
                        "There is no way to recover a forgotten master password. Keep an "
                        "exported backup of your servers somewhere safe."
                    ),
                    dim=True,
                ),
                False,
                False,
                0,
            )
        box.pack_start(self._build_auto_lock_row(settings), False, False, 0)
        self.show_all()
        (self.current if self.has_vault else self.password).grab_focus()

    def _build_auto_lock_row(self, settings):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=8)
        row.pack_start(_label(_("Lock after Guake was hidden for")), False, False, 0)
        self.auto_lock = Gtk.SpinButton.new_with_range(0, MAX_AUTO_LOCK_MINUTES, 1)
        self.auto_lock.set_tooltip_text(_("0 never locks Guake by itself"))
        row.pack_start(self.auto_lock, False, False, 0)
        row.pack_start(_label(_("minutes")), False, False, 0)
        if settings is not None:
            settings.bind(AUTO_LOCK_SETTING, self.auto_lock, "value", Gio.SettingsBindFlags.DEFAULT)
        else:
            row.set_sensitive(False)
        return row

    def current_password_error(self):
        if self.vault.check_password(self.current.get_text()):
            return None
        return _("That is not the current master password.")

    def clear(self):
        for entry in (self.current, self.password, self.confirm):
            entry.set_text("")


def _set_password(dialog, guake):
    error = new_password_error(dialog.password.get_text(), dialog.confirm.get_text())
    if error is None and dialog.has_vault:
        error = dialog.current_password_error()
    if error is not None:
        _message(dialog, Gtk.MessageType.ERROR, error)
        return False
    if dialog.has_vault:
        guake.vault.change_password(dialog.password.get_text())
        _message(dialog, Gtk.MessageType.INFO, _("The master password was changed."))
        return True
    vlt.enable(guake.vault, guake.servers, serversecrets.keyring, dialog.password.get_text())
    _message(
        dialog,
        Gtk.MessageType.INFO,
        _("The master password is set."),
        _(
            "Your servers and their passwords are now encrypted. Guake asks for the "
            "master password when it starts."
        ),
    )
    return True


def _remove_password(dialog, guake):
    error = dialog.current_password_error()
    if error is not None:
        _message(dialog, Gtk.MessageType.ERROR, error)
        return False
    if serversecrets.keyring.is_available():
        consequence = _(
            "Your servers go back to a plain file anyone using your account can read, "
            "and their passwords to your desktop keyring."
        )
    else:
        consequence = _(
            "Your servers go back to a plain file anyone using your account can read. "
            "There is no desktop keyring here, so their saved passwords will be forgotten."
        )
    confirmed = _message(
        dialog,
        Gtk.MessageType.WARNING,
        _("Remove the master password?"),
        consequence,
        Gtk.ButtonsType.OK_CANCEL,
    )
    if confirmed != Gtk.ResponseType.OK:
        return False
    lost = vlt.disable(guake.vault, guake.servers, serversecrets.keyring)
    if lost:
        _message(
            dialog,
            Gtk.MessageType.WARNING,
            _("The master password was removed."),
            _("The passwords of {count} server(s) could not be kept: {names}.").format(
                count=len(lost), names=", ".join(s.name for s in lost)
            ),
        )
    return True


def manage_master_password(parent, guake):
    """Let the user set, change or remove the master password."""
    if not vlt.is_available():
        _message(
            parent,
            Gtk.MessageType.INFO,
            _("A master password needs python3-cryptography."),
            _("Install it, then restart Guake."),
        )
        return
    settings = getattr(getattr(guake, "settings", None), "general", None)
    dialog = MasterPasswordDialog(parent, guake.vault, settings)
    try:
        while True:
            response = dialog.run()
            if response not in (Gtk.ResponseType.OK, RESPONSE_REMOVE):
                return
            action = _set_password if response == Gtk.ResponseType.OK else _remove_password
            try:
                if action(dialog, guake):
                    return
            except (vlt.VaultError, OSError) as e:
                log.error("Master password not changed: %s", e)
                _message(dialog, Gtk.MessageType.ERROR, _("That did not work."), str(e))
    finally:
        dialog.clear()
        dialog.destroy()
