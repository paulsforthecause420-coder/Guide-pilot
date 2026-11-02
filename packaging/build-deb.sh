#!/usr/bin/env bash
# Build a Debian package for GuidePilot (no network access required).
#
#   packaging/build-deb.sh            -> packaging/build/guidepilot_1.0.0_amd64.deb
#
# Install with:  sudo apt install ./guidepilot_1.0.0_amd64.deb
# Remove with:   sudo apt remove guidepilot
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c "import json,sys;print(json.load(open('$ROOT/package.json'))['version'])")"
ARCH="$(dpkg --print-architecture)"
BUILD="$ROOT/packaging/build"
STAGE="$BUILD/stage"
PKG="$BUILD/deb"
OUT="$BUILD/guidepilot_${VERSION}_${ARCH}.deb"

command -v dpkg-deb >/dev/null || { echo "dpkg-deb is required" >&2; exit 1; }

"$ROOT/packaging/stage.sh" "$STAGE"
python3 "$ROOT/packaging/make-icon.py" "$ROOT/assets/guidepilot.png" >/dev/null

echo "==> assembling package tree"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt"
cp -r "$STAGE" "$PKG/opt/guidepilot"
mkdir -p "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/256x256/apps" "$PKG/usr/share/doc/guidepilot"

cat > "$PKG/usr/bin/guidepilot" <<'SH'
#!/bin/sh
exec /opt/guidepilot/guidepilot "$@"
SH
chmod 0755 "$PKG/usr/bin/guidepilot"

install -m 0644 "$ROOT/packaging/guidepilot.desktop" "$PKG/usr/share/applications/guidepilot.desktop"
install -m 0644 "$ROOT/assets/guidepilot.png" "$PKG/usr/share/icons/hicolor/256x256/apps/guidepilot.png"
[ -f "$ROOT/README.md" ] && install -m 0644 "$ROOT/README.md" "$PKG/usr/share/doc/guidepilot/README.md" || true

INSTALLED_SIZE="$(du -sk "$PKG" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: guidepilot
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= 3.10), python3-gi, libgtk-3-0, libnss3, libxss1, libasound2
Recommends: xdotool, at-spi2-core, tesseract-ocr, xdg-utils, espeak-ng
Maintainer: GuidePilot maintainers <maintainers@guidepilot.invalid>
Description: screen-aware step-by-step desktop guide
 GuidePilot floats above your desktop applications, reads the accessibility
 tree and screen pixels of the window you are working in, highlights the exact
 control to use next, tells you what to type, and verifies that the step
 actually took effect before moving on.
 .
 Everything runs locally: screen capture, accessibility, planning and
 verification. An AI provider is optional and only used when you configure one.
CONTROL

cat > "$PKG/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
if [ -x /usr/bin/update-desktop-database ]; then
  update-desktop-database -q /usr/share/applications || true
fi
if [ -x /usr/bin/gtk-update-icon-cache ]; then
  gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
fi
exit 0
SH
chmod 0755 "$PKG/DEBIAN/postinst"

cat > "$PKG/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
exit 0
SH
chmod 0755 "$PKG/DEBIAN/prerm"

# Keep the package honest: no byte-code caches or build leftovers.
find "$PKG" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -name '*.pyc' -delete 2>/dev/null || true

echo "==> building $OUT"
dpkg-deb --root-owner-group --build "$PKG" "$OUT" >/dev/null
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
echo "inspect: dpkg-deb --info '$OUT'"
echo "install: sudo apt install '$OUT'"
