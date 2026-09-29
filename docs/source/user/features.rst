
Guake 3 Features
****************

TBD: Long description of each feature

Single Hotkey terminal
======================

TBD:

- Appears when you call and disappears once you are done by pressing a predefined hotkey (F12 by
  default)
- Customizable hotkeys for tab access, reorganization, background transparency, font size,...

Auto-start
==========

Guake can now (>=3.1) starts automatically on GNOME startup.

Advanced Tab Support
====================

Guake has several modes available to manage tab. You can let guake automatically rename the tab
or give you own name.

Color Palettes
==============

Thanks to the Guake community, a huge number of Terminal palettes are provided out of the box.

GTK Theme
=========

Guake allows you to choose a different GTK theme than your environment.

.. note:: You need to restart Guake after changing the GTK theme.

Quick Open, hyperlink and Search on web
=======================================

Guake automatically finds URL printed in your terminal and allow you to click on it using
``[Ctrl]+click``. Many other terminals, if not all, does that already.

Quick-Open
----------

.. image:: ../../../guake/data/pixmaps/quick-open.png
    :align: right

Guake is also able to find out file names and open it in your favorite code editor, such as
Visual Studio Code, Atom or SublimText.

Guake brings this to much more, by automatically parsing output of popular system commands such
as ``gcc``, Python's traceback or ``pytest`` report, and allowing you to automatically open the
file at the correct line number. Guake is even able to find the Python function name automatically
when used with ``pytest``.

.. image:: ../../../guake/data/pixmaps/quick-open-python-exception.png
    :align: center

Even if Guake cannot parse the output, you can still ask him to open a wanted file path displayed
in your terminal, provided the file exists at this path. Simply select the full path and click
using the ``[Ctrl]+click``, or with the contextual menu on right click.

.. image:: ../../../guake/data/pixmaps/quick-open-selection.png
    :align: center

Contextual menu
---------------

Right click also displays "Search on web" (if you have selected a text) and "Open link" (if the
text under the cursor is a URL or if the selected text is a URL).

Guake also supports
`HTML-like anchor <https://gist.github.com/egmontkob/eb114294efbcd5adb1944c9f3cb5feda>`_ with
special characters such as::

    echo -e '\e]8;;http://example.com\aThis is a link\e]8;;\a'

HTML-like anchors
-----------------

You may need a recent version of the VTE (Virtual Terminal Emulator) component in you system
(vte >= 0.50).

Multi Monitor
=============

TBD: Multi-monitor support (open on a specified monitor, open on mouse monitor)

Hook points
===========

TBD: Configure Guake startup by running a bash script when Guake starts

Save Terminal Content
=====================
TBD: Save terminal content to file

Custom Commands
===============

TBD

DBus commands
=============

The major features of guake are available on DBus.

Tab UUID
========

Tabs are uniquely identified with a UUID. Each terminal receives this UUID in the following
environment varialbe: ``GUAKE_TAB_UUID``. It can be used to rename the tab from the command line
using ``--tab-index 3c542bc1-7c99-4e73-8d37-e08281bd592c``.


Per-directory `.guake.yml` file
=============================

If there is a file named `.guake.yml` in the current working directory of the shell associated with a tab, Guake will try to read the title from there. The current format is very simple and it will probably change in the future::

    title: "My Great Project"

Saved servers
=============

Guake can remember the SSH servers you use, like the connection manager in
Tabby. Open the list with the server button on the tab bar, the ``Servers``
entry of the right-click menu, or ``<Control><Shift>s``, then click a server:
a new tab opens and logs you in.

Each saved server has a name, an optional group (shown as a submenu), a host,
a port and a user name, plus optionally:

- a private key (``ssh -i``),
- a password, kept in your desktop keyring through libsecret and handed to
  ``sshpass`` when connecting (it is never written to the servers file; both
  ``gir1.2-secret-1`` and ``sshpass`` must be installed),
- a jump host (``ssh -J``),
- extra ssh options,
- a command to run once logged in, for example ``tmux attach || tmux``.

*Manage servers...* lists the servers with a search field and adds, edits,
removes and connects to them (*Files* opens the SFTP panel instead). Hosts
declared in ``~/.ssh/config`` are listed automatically in the servers menu,
and *Import hosts from ~/.ssh/config* in the manager's menu copies them into
the saved list so they can be edited.

Each server has a tab colour: pick one in the editor, or leave it on *Auto*
for a colour derived from the server. Server tabs show that colour and a
server icon; the current tab is painted more strongly. Any tab, local or not,
can be given a colour from its right-click *Tab Color* menu, and that choice
is kept with the saved tab session.

The first time you connect to a server with a saved password, ssh does not
know its host key yet and ``sshpass`` refuses to answer that question for
you. The tab then shows ssh's own prompt with the key fingerprint: type
``yes`` to trust it and the login continues with the saved password. If the
host key has *changed*, the password is not sent and the tab explains how to
remove the old key (``ssh-keygen -R``) if the change is expected.

Backup and restore
------------------

*Export backup...* in the manager's menu writes every saved server to a JSON
file (readable only by you), to set Guake up on another machine or keep a
copy. With *Include saved passwords and private keys* the keyring passwords
and the private key files the servers use are added too, encrypted with
AES-256-GCM under a key derived from a passphrase you choose (scrypt). This
needs ``python3-cryptography``; without it only the server list is exported.

*Import backup...* reads such a file (or a plain ``servers.json``). A server
with the same id or name as a saved one updates it, the others are added.
When the file holds secrets you are asked for the passphrase, or you can
import without them and type passwords when connecting. Passwords go to the
keyring; private keys go where the server expects them inside ``~/.ssh`` or,
when a different key already has that name or the path is outside
``~/.ssh``, to ``~/.ssh/guake-imported/``. Existing files are never
overwritten.

When a connection ends the tab stays open, shows why, and offers to reconnect
(press ``r`` then Enter) or close (Enter). Server tabs are part of the saved
tab session too: after a Guake restart they come back, but wait for you to
press ``r`` before connecting, so Guake never dials every server at startup.

Servers are stored in ``~/.config/guake/servers.json``. From the command line,
``guake --server NAME`` opens a tab connected to the saved server ``NAME`` and
``guake --servers`` opens the manager.

SFTP file transfer
==================

Files and folders can be uploaded to and downloaded from a saved server with
the SFTP panel, which opens next to the terminal like Tabby's. Show it with
the SFTP button on the tab bar, the *SFTP file transfer* entry of the
terminal's right-click menu, or ``<Control><Shift>u``. In a tab connected to
a saved server the panel opens for that server; in any other tab you pick the
server from a menu first.

The panel lists the remote directory (double-click a folder to enter it,
click a folder in the path bar, type a path after clicking the pencil or
pressing ``<Control>l``, use the parent and home buttons, or Backspace) and
offers:

- **Download**: select files or folders, then *Download...* from the
  right-click menu, or double-click a file. You choose the local folder;
  folders are downloaded recursively.
- **Upload**: the upload buttons pick local files or a folder, and files or
  folders dragged from your file manager onto the list are uploaded into the
  current directory. You are asked before an existing file is replaced.
- **New folder**, **Rename** (F2), **Delete** (Delete key; folders are
  removed with their content) and **Copy path**.

Transfers queue up at the bottom of the panel with their progress and can be
cancelled. Uploads and downloads run on their own connection, so browsing
stays responsive while a large file is being copied.

The panel drives OpenSSH's own ``sftp`` client, so it needs nothing beyond
``openssh-client`` and honours everything the terminal connection does:
``~/.ssh/config``, keys and the agent, the jump host and the extra options
of the saved server (options that only make sense for an interactive
session, such as ``-t`` or ``-X``, are left out). A password saved in the
keyring is used automatically; any other question ssh asks (password,
passphrase, a new host key) is shown in a dialog.
