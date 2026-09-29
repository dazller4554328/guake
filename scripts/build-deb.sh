#!/usr/bin/env bash
#
# Build a Debian/Ubuntu package (dist/guake_<version>_all.deb) for this fork
# using only dpkg-deb, no debhelper required.
#
# Usage:
#   scripts/build-deb.sh [VERSION]
#
# VERSION defaults to `git describe --tags` without its leading "v". When the
# repository has no tags it falls back to
#   <BASE_VERSION>+fork.<commit date>.<short sha>
# e.g. 3.10.1+fork.20260908.609e108. That string is both a valid Debian
# version and a valid PEP 440 version (local segment), so the same value is
# used for the package and for guake's own `guake --version`.
#
# Environment overrides:
#   BASE_VERSION    upstream version the fork is based on (default 3.10.1)
#   DEB_MAINTAINER  Maintainer field (default: dazller4554328 <darrenmalawi13@gmail.com>)
#   PYTHON          python interpreter used for staging (default /usr/bin/python3)
#   DEB_EPOCH       Debian epoch (default 1), see below
#
# The build works on a snapshot copy of the source tree so the Makefile's
# in-place rewrite of guake/paths.py never touches the working copy.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
BASE_VERSION="${BASE_VERSION:-3.10.1}"
DEB_MAINTAINER="${DEB_MAINTAINER:-dazller4554328 <darrenmalawi13@gmail.com>}"
PYTHON="${PYTHON:-/usr/bin/python3}"
PKG_NAME="guake"
# The package is called "guake" like the distribution's, so the Debian version
# carries an epoch: "1:3.10.1+fork..." always sorts above the distribution's
# "3.x", and `apt upgrade` never swaps the fork for upstream Guake.
DEB_EPOCH="${DEB_EPOCH:-1}"
# Architecture-independent location imported by every python3 minor version.
PY_DEST="usr/lib/python3/dist-packages"

log() { printf '\033[1;34m[build-deb]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[build-deb] error:\033[0m %s\n' "$*" >&2; exit 1; }

for tool in dpkg-deb make msgfmt glib-compile-schemas "$PYTHON"; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' is required but not installed"
done


# shellcheck source=scripts/lib/stage-guake.sh
. "$ROOT_DIR/scripts/lib/stage-guake.sh"

VERSION="${1:-$(derive_version)}"
VERSION="${VERSION#v}"
# Debian: must start with a digit, then alphanumerics and . + ~ - only.
[[ "$VERSION" =~ ^[0-9][A-Za-z0-9.+~-]*$ ]] || die "'$VERSION' is not a valid Debian version"
PY_VERSION="$(pep440_version "$VERSION")"
log "Debian version: $VERSION (python version: $PY_VERSION)"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/guake-deb.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
SRC_DIR="$WORK_DIR/src"
STAGE_DIR="$WORK_DIR/stage"
mkdir -p "$STAGE_DIR"
snapshot_source "$SRC_DIR"
stage_guake "$SRC_DIR" "$STAGE_DIR" "$PY_DEST" "$PY_VERSION" dpkg

# --------------------------------------------------------------------------
# Debian metadata
# --------------------------------------------------------------------------
DEBIAN_DIR="$STAGE_DIR/DEBIAN"
mkdir -p "$DEBIAN_DIR" "$STAGE_DIR/usr/share/doc/$PKG_NAME"
install -m644 "$SRC_DIR/COPYING" "$STAGE_DIR/usr/share/doc/$PKG_NAME/copyright"

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE_DIR" | cut -f1)"

cat >"$DEBIAN_DIR/control" <<EOF
Package: $PKG_NAME
Version: ${DEB_EPOCH}:$VERSION
Architecture: all
Maintainer: $DEB_MAINTAINER
Installed-Size: $INSTALLED_SIZE
Section: x11
Priority: optional
Homepage: https://github.com/dazller4554328/guake
Depends: python3 (>= 3.8), python3-gi (>= 3.26.1), python3-gi-cairo, python3-cairo, python3-dbus, python3-yaml, gir1.2-glib-2.0, gir1.2-gtk-3.0 (>= 3.22), gir1.2-vte-2.91 (>= 0.50), gir1.2-keybinder-3.0, gir1.2-notify-0.7, gir1.2-wnck-3.0, gir1.2-pango-1.0, libglib2.0-bin, libutempter0, default-dbus-session-bus | dbus-session-bus, dconf-gsettings-backend | gsettings-backend
Recommends: openssh-client, sshpass, gir1.2-secret-1, python3-cryptography
Suggests: gir1.2-ayatanaappindicator3-0.1, python3-colorlog
Description: Drop-down terminal for GNOME (dazller4554328 fork)
 Guake is a top-down "Quake style" terminal for GNOME: press a hotkey and a
 terminal slides down from the top of the screen.
 .
 This fork adds a saved SSH servers manager (passwords kept in the desktop
 keyring and passed through sshpass, coloured server tabs, encrypted backup
 and restore) and a Tabby-style SFTP panel for uploading and downloading
 files next to a terminal tab.
EOF

cat >"$DEBIAN_DIR/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    if command -v glib-compile-schemas >/dev/null 2>&1; then
        glib-compile-schemas /usr/share/glib-2.0/schemas || true
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
fi
exit 0
EOF

cat >"$DEBIAN_DIR/postrm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
    remove|purge)
        if command -v glib-compile-schemas >/dev/null 2>&1 \
            && [ -d /usr/share/glib-2.0/schemas ]; then
            glib-compile-schemas /usr/share/glib-2.0/schemas || true
        fi
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        ;;
esac
exit 0
EOF

# --------------------------------------------------------------------------
# Permissions and build
# --------------------------------------------------------------------------
find "$STAGE_DIR" -type d -exec chmod 0755 {} +
find "$STAGE_DIR" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGE_DIR"/usr/bin/* "$DEBIAN_DIR/postinst" "$DEBIAN_DIR/postrm"

mkdir -p "$DIST_DIR"
DEB_FILE="$DIST_DIR/${PKG_NAME}_${VERSION}_all.deb"
log "Building $DEB_FILE"
# xz keeps the package installable on older dpkg that lacks zstd support.
dpkg-deb --root-owner-group -Zxz --build "$STAGE_DIR" "$DEB_FILE" >/dev/null

log "Done: $DEB_FILE"
