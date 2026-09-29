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

# --------------------------------------------------------------------------
# Version
# --------------------------------------------------------------------------
derive_version() {
    local described
    if described="$(git -C "$ROOT_DIR" describe --tags 2>/dev/null)"; then
        # v3.10.2 -> 3.10.2 ; v3.10.2-4-gabc123 -> 3.10.2+4.gabc123
        described="${described#v}"
        echo "$described" | sed -E 's/-([0-9]+)-g([0-9a-f]+)$/+\1.g\2/'
        return
    fi
    local date sha
    date="$(git -C "$ROOT_DIR" log -1 --format=%cd --date=format:%Y%m%d 2>/dev/null || date +%Y%m%d)"
    sha="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo nogit)"
    echo "${BASE_VERSION}+fork.${date}.${sha}"
}

VERSION="${1:-$(derive_version)}"
VERSION="${VERSION#v}"
# Debian: must start with a digit, then alphanumerics and . + ~ - only.
[[ "$VERSION" =~ ^[0-9][A-Za-z0-9.+~-]*$ ]] || die "'$VERSION' is not a valid Debian version"

# PEP 440 version handed to setuptools_scm. Use VERSION when it is valid,
# otherwise keep its leading release number and push the rest to a local label.
PY_VERSION="$("$PYTHON" - "$VERSION" <<'EOF'
import re, sys
v = sys.argv[1]
try:
    from packaging.version import Version, InvalidVersion
    try:
        print(Version(v)); sys.exit(0)
    except InvalidVersion:
        pass
except ImportError:
    if re.fullmatch(r"\d+(\.\d+)*(\+[a-z0-9]+(\.[a-z0-9]+)*)?", v, re.I):
        print(v); sys.exit(0)
m = re.match(r"(\d+(?:\.\d+)*)(.*)", v)
rest = re.sub(r"[^A-Za-z0-9]+", ".", m.group(2)).strip(".")
print(m.group(1) + ("+" + rest if rest else ""))
EOF
)"

log "Debian version: $VERSION (python version: $PY_VERSION)"

# --------------------------------------------------------------------------
# Snapshot the source tree (tracked + untracked, non-ignored files)
# --------------------------------------------------------------------------
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/guake-deb.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
SRC_DIR="$WORK_DIR/src"
STAGE_DIR="$WORK_DIR/stage"
mkdir -p "$SRC_DIR" "$STAGE_DIR"

if [ -n "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null)" ]; then
    log "warning: working tree has uncommitted changes; they will be packaged"
fi
log "Copying source tree to $SRC_DIR"
# Files deleted in the working tree are skipped; any other copy error aborts.
while IFS= read -r -d '' file; do
    [ -e "$ROOT_DIR/$file" ] || [ -L "$ROOT_DIR/$file" ] || continue
    (cd "$ROOT_DIR" && cp -P --parents -- "$file" "$SRC_DIR") || die "cannot copy $file"
done < <(git -C "$ROOT_DIR" ls-files -z --cached --others --exclude-standard)
[ -f "$SRC_DIR/setup.py" ] || die "source snapshot failed"

# --------------------------------------------------------------------------
# Stage with the Makefile
# --------------------------------------------------------------------------
export SETUPTOOLS_SCM_PRETEND_VERSION="$PY_VERSION"
export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_GUAKE="$PY_VERSION"
# Debian/Ubuntu mark the system python as externally managed (PEP 668); we only
# install into a --root staging dir, so this is safe.
export PIP_BREAK_SYSTEM_PACKAGES=1
# Never let pip "upgrade" (i.e. uninstall) a guake already installed on the
# build host, and never pull dependencies into the package.
export PIP_IGNORE_INSTALLED=1
export PIP_NO_DEPS=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_WARN_SCRIPT_LOCATION=1

MAKE_ARGS=(PYTHON="$PYTHON" PREFIX=/usr DESTDIR="$STAGE_DIR" COMPILE_SCHEMA=0)

log "Generating desktop files, paths, translations (make)"
make -C "$SRC_DIR" PYTHON="$PYTHON" prepare-install >/dev/null

log "Installing data files and translations into staging"
make -C "$SRC_DIR" "${MAKE_ARGS[@]}" install-schemas install-locale >/dev/null

# install-guake skips its `pip install -r requirements.txt` step because DESTDIR
# is set; its update-desktop-database call is non-fatal. Its pip failure is
# also swallowed, so the result is checked explicitly below.
log "Installing the python package into staging (pip --root)"
make -C "$SRC_DIR" "${MAKE_ARGS[@]}" install-guake

# pip/Debian may pick /usr/local/lib/python3.X/dist-packages or
# /usr/lib/python3.X/site-packages depending on the pip build. Move whatever it
# produced to /usr/lib/python3/dist-packages.
PKG_INIT="$(find "$STAGE_DIR" -path '*-packages/guake/__init__.py' -print -quit)"
[ -n "$PKG_INIT" ] || die "pip did not install the guake package into staging"
SITE_DIR="$(dirname "$(dirname "$PKG_INIT")")"
if [ "$SITE_DIR" != "$STAGE_DIR/$PY_DEST" ]; then
    log "Relocating ${SITE_DIR#"$STAGE_DIR"} -> /$PY_DEST"
    mkdir -p "$STAGE_DIR/$PY_DEST"
    cp -a "$SITE_DIR"/. "$STAGE_DIR/$PY_DEST/"
    rm -rf "$SITE_DIR"
fi
# Entry-point scripts land in <prefix>/bin; move them if pip used /usr/local.
if [ -d "$STAGE_DIR/usr/local/bin" ]; then
    mkdir -p "$STAGE_DIR/usr/bin"
    mv "$STAGE_DIR/usr/local/bin/"* "$STAGE_DIR/usr/bin/"
fi
# Drop now-empty leftovers (usr/local, usr/lib/python3.X, ...).
find "$STAGE_DIR/usr" -depth -type d -empty -delete

# Byte code, tests and pip bookkeeping that points at the temp build dir.
PY_PKG="$STAGE_DIR/$PY_DEST/guake"
find "$STAGE_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$STAGE_DIR" -name '*.py[co]' -delete
rm -rf "$PY_PKG/tests"
rm -f "$STAGE_DIR/$PY_DEST"/guake-*.dist-info/direct_url.json
rm -f "$STAGE_DIR/$PY_DEST"/guake-*.dist-info/RECORD
# Mark the distribution as dpkg-owned so `pip uninstall` leaves it alone.
for installer in "$STAGE_DIR/$PY_DEST"/guake-*.dist-info/INSTALLER; do
    echo dpkg >"$installer"
done

# paths.py must point at /usr/share, not the dev tree.
grep -q '"/usr/share/guake"' "$PY_PKG/paths.py" \
    || die "staged guake/paths.py does not point at /usr/share/guake"

# --------------------------------------------------------------------------
# Executables
# --------------------------------------------------------------------------
for exe in guake guake-toggle; do
    [ -f "$STAGE_DIR/usr/bin/$exe" ] || die "missing /usr/bin/$exe in staging"
    # pip writes the interpreter it ran with; force the system python3.
    sed -i -e '1s|^#!.*python.*$|#!/usr/bin/python3|' "$STAGE_DIR/usr/bin/$exe"
done
# guake-prefs is not a setuptools entry point; provide the classic launcher.
cat >"$STAGE_DIR/usr/bin/guake-prefs" <<'EOF'
#!/bin/sh
# Open the Guake preferences window.
exec /usr/bin/guake --preferences "$@"
EOF

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
