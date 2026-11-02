#!/usr/bin/env bash
# Build a portable AppImage (or, without appimagetool, a relocatable directory
# tarball that runs from anywhere).
#
#   packaging/build-appimage.sh
#
# appimagetool is intentionally not vendored: if it is not installed the script
# still produces a working portable build and tells you how to finish the
# AppImage step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c "import json,sys;print(json.load(open('$ROOT/package.json'))['version'])")"
ARCH="$(uname -m)"
BUILD="$ROOT/packaging/build"
APPDIR="$BUILD/GuidePilot.AppDir"

"$ROOT/packaging/stage.sh" "$APPDIR/usr"
python3 "$ROOT/packaging/make-icon.py" "$ROOT/assets/guidepilot.png" >/dev/null

echo "==> assembling AppDir"
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
install -m 0644 "$ROOT/packaging/guidepilot.desktop" "$APPDIR/usr/share/applications/guidepilot.desktop"
install -m 0644 "$ROOT/packaging/guidepilot.desktop" "$APPDIR/guidepilot.desktop"
install -m 0644 "$ROOT/assets/guidepilot.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/guidepilot.png"
install -m 0644 "$ROOT/assets/guidepilot.png" "$APPDIR/guidepilot.png"

cat > "$APPDIR/AppRun" <<'SH'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/guidepilot" "$@"
SH
chmod 0755 "$APPDIR/AppRun"

if command -v appimagetool >/dev/null 2>&1; then
  OUT="$BUILD/GuidePilot-${VERSION}-${ARCH}.AppImage"
  echo "==> packaging AppImage with appimagetool"
  ARCH="$ARCH" appimagetool "$APPDIR" "$OUT"
  echo "built $OUT ($(du -h "$OUT" | cut -f1))"
  echo "run: chmod +x '$OUT' && '$OUT'"
else
  OUT="$BUILD/GuidePilot-${VERSION}-${ARCH}.tar.gz"
  echo "==> appimagetool not found: producing a portable directory archive instead"
  echo "    (install appimagetool from https://github.com/AppImage/appimagetool to get a single-file AppImage)"
  tar -C "$BUILD" -czf "$OUT" "$(basename "$APPDIR")"
  echo "built $OUT ($(du -h "$OUT" | cut -f1))"
  echo "run: tar xzf '$OUT' && ./GuidePilot.AppDir/AppRun"
fi
