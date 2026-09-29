# shellcheck shell=bash
#
# Shared by scripts/build-deb.sh and scripts/build-rpm.sh: version numbers,
# a snapshot of the source tree, and a staged `make install` of Guake.
#
# The caller sets ROOT_DIR, PYTHON, BASE_VERSION and defines log() and die().

# Version from `git describe --tags` without its leading "v", or
# <BASE_VERSION>+fork.<commit date>.<short sha> when there is no tag.
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

# PEP 440 form of $1 for setuptools_scm: kept when valid, otherwise its
# leading release number plus the rest as a local label.
pep440_version() {
    "$PYTHON" - "$1" <<'PYEOF'
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
PYEOF
}

# Copy tracked and untracked (non-ignored) files to $1, so the Makefile's
# in-place rewrite of guake/paths.py never touches the working copy.
snapshot_source() {
    local dest="$1" file
    if [ -n "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null)" ]; then
        log "warning: working tree has uncommitted changes; they will be packaged"
    fi
    log "Copying source tree to $dest"
    mkdir -p "$dest"
    # Files deleted in the working tree are skipped; any other copy error aborts.
    while IFS= read -r -d '' file; do
        [ -e "$ROOT_DIR/$file" ] || [ -L "$ROOT_DIR/$file" ] || continue
        (cd "$ROOT_DIR" && cp -P --parents -- "$file" "$dest") || die "cannot copy $file"
    done < <(git -C "$ROOT_DIR" ls-files -z --cached --others --exclude-standard)
    [ -f "$dest/setup.py" ] || die "source snapshot failed"
}

# stage_guake SRC_DIR STAGE_DIR PY_DEST PY_VERSION INSTALLER
# Install Guake from SRC_DIR into STAGE_DIR (prefix /usr) with the python
# package under STAGE_DIR/PY_DEST, /usr/bin/guake, guake-toggle and
# guake-prefs. INSTALLER is written to the dist-info (dpkg, rpm) so that
# `pip uninstall` leaves the package alone.
stage_guake() {
    local src_dir="$1" stage_dir="$2" py_dest="$3" py_version="$4" installer="$5"
    export SETUPTOOLS_SCM_PRETEND_VERSION="$py_version"
    export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_GUAKE="$py_version"
    # The system python may be marked externally managed (PEP 668); we only
    # install into a --root staging dir, so this is safe.
    export PIP_BREAK_SYSTEM_PACKAGES=1
    # Never let pip "upgrade" (i.e. uninstall) a guake already installed on the
    # build host, and never pull dependencies into the package.
    export PIP_IGNORE_INSTALLED=1
    export PIP_NO_DEPS=1
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    export PIP_NO_WARN_SCRIPT_LOCATION=1

    local make_args=(PYTHON="$PYTHON" PREFIX=/usr DESTDIR="$stage_dir" COMPILE_SCHEMA=0)

    log "Generating desktop files, paths, translations (make)"
    make -C "$src_dir" PYTHON="$PYTHON" prepare-install >/dev/null

    log "Installing data files and translations into staging"
    make -C "$src_dir" "${make_args[@]}" install-schemas install-locale >/dev/null

    # install-guake skips its `pip install -r requirements.txt` step because
    # DESTDIR is set; its update-desktop-database call is non-fatal. Its pip
    # failure is also swallowed, so the result is checked explicitly below.
    log "Installing the python package into staging (pip --root)"
    make -C "$src_dir" "${make_args[@]}" install-guake

    # pip may pick /usr/local/lib/python3.X/dist-packages,
    # /usr/lib/python3.X/site-packages... depending on the distribution.
    # Move whatever it produced to PY_DEST.
    local pkg_init site_dir
    pkg_init="$(find "$stage_dir" -path '*-packages/guake/__init__.py' -print -quit)"
    [ -n "$pkg_init" ] || die "pip did not install the guake package into staging"
    site_dir="$(dirname "$(dirname "$pkg_init")")"
    if [ "$site_dir" != "$stage_dir/$py_dest" ]; then
        log "Relocating ${site_dir#"$stage_dir"} -> /$py_dest"
        mkdir -p "$stage_dir/$py_dest"
        cp -a "$site_dir"/. "$stage_dir/$py_dest/"
        rm -rf "$site_dir"
    fi
    # Entry-point scripts land in <prefix>/bin; move them if pip used /usr/local.
    if [ -d "$stage_dir/usr/local/bin" ]; then
        mkdir -p "$stage_dir/usr/bin"
        mv "$stage_dir/usr/local/bin/"* "$stage_dir/usr/bin/"
    fi
    # Drop now-empty leftovers (usr/local, usr/lib/python3.X, ...).
    find "$stage_dir/usr" -depth -type d -empty -delete

    # Byte code, tests and pip bookkeeping that points at the temp build dir.
    local py_pkg="$stage_dir/$py_dest/guake" dist_info
    find "$stage_dir" -name __pycache__ -type d -prune -exec rm -rf {} +
    find "$stage_dir" -name '*.py[co]' -delete
    rm -rf "$py_pkg/tests"
    for dist_info in "$stage_dir/$py_dest"/guake-*.dist-info; do
        rm -f "$dist_info/direct_url.json" "$dist_info/RECORD"
        echo "$installer" >"$dist_info/INSTALLER"
    done

    # paths.py must point at /usr/share, not the dev tree.
    grep -q '"/usr/share/guake"' "$py_pkg/paths.py" \
        || die "staged guake/paths.py does not point at /usr/share/guake"

    local exe
    for exe in guake guake-toggle; do
        [ -f "$stage_dir/usr/bin/$exe" ] || die "missing /usr/bin/$exe in staging"
        # pip writes the interpreter it ran with; force the system python3.
        sed -i -e '1s|^#!.*python.*$|#!/usr/bin/python3|' "$stage_dir/usr/bin/$exe"
    done
    # guake-prefs is not a setuptools entry point; provide the classic launcher.
    cat >"$stage_dir/usr/bin/guake-prefs" <<'SHEOF'
#!/bin/sh
# Open the Guake preferences window.
exec /usr/bin/guake --preferences "$@"
SHEOF
}
