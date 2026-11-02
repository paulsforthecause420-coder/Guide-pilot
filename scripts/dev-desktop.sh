#!/usr/bin/env bash
# Build the renderer and main process, then start GuidePilot from source.
#
# The Electron main process starts the Python service itself (loopback HTTP with
# a per-run token), so there is nothing else to launch.
#
# Usage:
#   ./scripts/dev-desktop.sh                 # normal run
#   ./scripts/dev-desktop.sh --remote-debugging-port=9223
#   GUIDEPILOT_DEVTOOLS=1 ./scripts/dev-desktop.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x "desktop/node_modules/electron/dist/electron" ]; then
  echo "Electron is not installed. Run: (cd desktop && npm install)" >&2
  exit 1
fi

echo "==> building renderer and main process"
npm run build

echo "==> starting GuidePilot"
cd desktop
exec ./node_modules/electron/dist/electron . "$@"
