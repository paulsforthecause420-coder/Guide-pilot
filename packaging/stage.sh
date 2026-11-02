#!/usr/bin/env bash
# Build the application tree that both the .deb and the AppImage wrap.
#
#   packaging/build/stage/
#     electron/                 bundled Electron runtime
#     resources/guidepilot/     application code (backend + built desktop)
#     guidepilot                launcher
#
# Usage: packaging/stage.sh [output-dir]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/packaging/build/stage}"
cd "$ROOT"

ELECTRON_SRC="$ROOT/desktop/node_modules/electron/dist"
if [ ! -x "$ELECTRON_SRC/electron" ]; then
  echo "Electron is not installed. Run: (cd desktop && npm install)" >&2
  exit 1
fi

echo "==> building renderer and main process"
npm run build --silent

echo "==> staging into $OUT"
rm -rf "$OUT"
mkdir -p "$OUT/resources/guidepilot/desktop" "$OUT/electron"

# Application code. Only what runs is copied: the Python package, the built
# desktop bundles and the Electron entry point.
# Keep the `backend/` directory: the Electron main process puts
# <app>/backend on PYTHONPATH exactly as it does in a source checkout.
mkdir -p "$OUT/resources/guidepilot/backend"
cp -r "$ROOT/backend/guidepilot" "$OUT/resources/guidepilot/backend/"
find "$OUT/resources/guidepilot" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$OUT/resources/guidepilot" -name '*.pyc' -delete
cp -r "$ROOT/desktop/dist" "$OUT/resources/guidepilot/desktop/dist"
cp "$ROOT/shared" -r "$OUT/resources/guidepilot/shared"
cp "$ROOT/workflows" -r "$OUT/resources/guidepilot/workflows"

cat > "$OUT/resources/guidepilot/package.json" <<'JSON'
{
  "name": "guidepilot",
  "productName": "GuidePilot",
  "version": "1.0.0",
  "description": "Screen-aware AI desktop instructor",
  "license": "MIT",
  "main": "desktop/dist/electron/main.js"
}
JSON

# Bundled runtime (the Electron version the app was built against).
cp -r "$ELECTRON_SRC/." "$OUT/electron/"
chmod +x "$OUT/electron/electron"

install -m 0755 "$ROOT/packaging/launcher.sh" "$OUT/guidepilot"

echo "==> stage ready: $(du -sh "$OUT" | cut -f1) at $OUT"
