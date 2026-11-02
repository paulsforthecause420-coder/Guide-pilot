#!/usr/bin/env bash
# GuidePilot launcher.
#
# Layout (identical in the .deb and the AppImage):
#   <prefix>/resources/guidepilot/   application code (backend + built desktop)
#   <prefix>/electron/               bundled Electron runtime
#
# The Electron main process reads the app code from resources/guidepilot and
# starts the Python service itself; nothing here needs network access.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_CODE="$SELF_DIR/resources/guidepilot"
ELECTRON_BIN="$SELF_DIR/electron/electron"

if [ ! -x "$ELECTRON_BIN" ]; then
  echo "GuidePilot: bundled Electron runtime missing at $ELECTRON_BIN" >&2
  exit 1
fi
if [ ! -f "$APP_CODE/package.json" ]; then
  echo "GuidePilot: application code missing at $APP_CODE" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "GuidePilot needs python3 on PATH (it runs the local guidance service)." >&2
  exit 1
fi

export PYTHONPATH="$APP_CODE/backend${PYTHONPATH:+:$PYTHONPATH}"
exec "$ELECTRON_BIN" "$APP_CODE" "$@"
