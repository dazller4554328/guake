=====================================
Guake (fork with SSH + SFTP features)
=====================================

|actions-badge|_ |docs-badge|_

.. |actions-badge| image:: https://github.com/dazller4554328/guake/actions/workflows/ci.yml/badge.svg
.. _actions-badge: https://github.com/dazller4554328/guake/actions

.. |docs-badge| image:: https://readthedocs.org/projects/guake/badge/?version=stable
.. _docs-badge: https://guake.readthedocs.io/en/stable/?badge=stable

This is a fork of `Guake <https://github.com/Guake/guake>`_, the drop-down terminal for
GNOME, that adds a Tabby-style SSH connection manager and an SFTP file transfer panel on
top of upstream ``master``. Everything upstream Guake does still works exactly the same;
this fork only adds features.

What this fork adds
===================

- **Saved SSH servers** – an SSH connection manager like the one in Tabby. Save a server
  (host, port, user, private key, jump host, extra ssh options, a command to run after
  login), group servers into submenus, and open a tab logged into one with a single click
  from the tab bar button, the right-click ``Servers`` menu, or ``Ctrl+Shift+S``.
  Hosts from ``~/.ssh/config`` are listed automatically and can be imported. Passwords
  are kept in your desktop keyring, never in a plain file. When a connection drops the
  tab stays open and offers to reconnect with one key press. Server tabs are restored
  with the rest of your session after a restart.
- **SFTP file transfer panel** – upload and download files and folders to any saved
  server from a panel that opens next to the terminal (tab bar button, right-click
  ``SFTP file transfer``, or ``Ctrl+Shift+U``). Browse the remote directory, drag and
  drop from your file manager to upload, download by double-click, create folders,
  rename, delete, copy paths, and watch a transfer queue with progress and cancel. It
  drives OpenSSH's own ``sftp`` client, so keys, agent, ``~/.ssh/config`` and jump hosts
  all work with no extra setup.
- **Word characters preference** – choose which characters count as part of a word when
  double-clicking to select text (upstream issue #2304).
- New command line options: ``guake --server NAME`` opens a tab connected to a saved
  server, ``guake --servers`` opens the server manager.

Full details are in ``docs/source/user/features.rst`` under *Saved servers* and *SFTP file
transfer*.

Installation
============

This fork is not in any distribution's package repository, so it has to be installed from
source. It takes about five minutes.

1. Remove a packaged Guake if you have one
------------------------------------------

Two copies of Guake installed side by side will fight over the D-Bus name and the F12
hotkey. Uninstall the distribution package first:

.. code-block:: bash

   sudo apt remove guake        # Debian / Ubuntu
   sudo dnf remove guake        # Fedora
   sudo pacman -R guake         # Arch / Manjaro

2. Get the source
-----------------

Do **NOT** use the ZIP or tarball that GitHub offers on the Releases or Code pages. The
build uses PBR, which needs the full Git history to work out the version. Clone instead:

.. code-block:: bash

   git clone https://github.com/dazller4554328/guake.git
   cd guake

3. Install the system dependencies
----------------------------------

The repository ships a script per distribution that installs the GTK, VTE and Python
packages Guake needs:

.. code-block:: bash

   ./scripts/bootstrap-dev-debian.sh run make    # Debian, Ubuntu, Mint, Pop!_OS
   ./scripts/bootstrap-dev-fedora.sh run make    # Fedora
   ./scripts/bootstrap-dev-arch.sh run make      # Arch, Manjaro

Then install the packages the new features use. They are all optional, but without them
the matching feature is greyed out or falls back to asking you interactively:

.. list-table::
   :header-rows: 1

   * - Feature
     - Debian / Ubuntu
     - Fedora
     - Arch
   * - SSH connections and SFTP panel
     - ``openssh-client``
     - ``openssh-clients``
     - ``openssh``
   * - Saved passwords in the keyring
     - ``gir1.2-secret-1`` ``sshpass``
     - ``libsecret`` ``sshpass``
     - ``libsecret`` ``sshpass``

For example on Ubuntu:

.. code-block:: bash

   sudo apt install openssh-client gir1.2-secret-1 sshpass

4. Build and install
--------------------

.. code-block:: bash

   make
   sudo make install

If ``sudo make install`` complains about a "dubious ownership" Git error, run
``sudo git config --global --add safe.directory '*'`` once and try again.

5. Run it
---------

.. code-block:: bash

   guake

Press **F12** to drop the terminal down. On Wayland the global hotkey has to be set in
your desktop's keyboard settings: add a custom shortcut that runs ``guake-toggle`` and bind
it to F12 (GNOME: Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts).

To have Guake start with your session, tick *Start Guake at login* in
Preferences → General.

Updating
--------

.. code-block:: bash

   cd guake
   git pull
   make reinstall      # runs uninstall + build + install, do not sudo it

Uninstalling
------------

.. code-block:: bash

   cd guake
   sudo make uninstall

Quick tour of the new features
==============================

**Add a server**: click the server icon on the tab bar (or press ``Ctrl+Shift+S``) and
choose *Manage servers...*, then *Add*. Fill in a name, host and user. Pick a private key
file if you use one, or type a password to store it in the keyring. Click the server's
name in the menu to open a tab connected to it.

**Import from ~/.ssh/config**: in *Manage servers...* click *Import ~/.ssh/config*. Hosts
you already have there are listed automatically even without importing.

**Transfer files**: in a tab connected to a saved server press ``Ctrl+Shift+U``. The SFTP
panel opens on the right. Drag files from your file manager onto it to upload, double-click
a file to download it, right-click for the rest.

**From the command line**: ``guake --server work`` opens a tab connected to the saved
server named ``work``.

Where things are stored
-----------------------

- Saved servers: ``~/.config/guake/servers.json`` (passwords are **not** in this file).
- Passwords: your desktop keyring (GNOME Keyring, KDE Wallet, ...) via libsecret.
- Everything else: the same GSettings keys upstream Guake uses.

Upstream Guake
==============

- Upstream source: https://github.com/Guake/guake
- Homepage: https://guake.github.io
- Documentation: http://guake.readthedocs.io/
- Translations: https://hosted.weblate.org/projects/guake/guake/

Bugs in the SSH server manager, the SFTP panel or the word-characters preference belong
in `this fork's issue tracker <https://github.com/dazller4554328/guake/issues>`_. Anything
else is most likely an upstream matter.

**Important note**: Do **NOT** use the domain guake.org, it has been registered by someone
outside the team. Nobody involved with Guake is responsible for the content on that site.

Guake was originally created by Gabriel Falcão, see:
https://sourceforge.net/projects/guake-gnome-vte/
