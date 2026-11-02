#!/usr/bin/env bash
# Start the deterministic demo application used for manual trials and for the
# end-to-end acceptance scenario.
#
# The demo app mirrors its own state to $GUIDEPILOT_DEMO_STATE so automated
# tests can assert what really happened inside it. GuidePilot itself never reads
# that file: guidance comes only from the accessibility tree and pixels.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ELECTRON="$ROOT/desktop/node_modules/electron/dist/electron"
if [ ! -x "$ELECTRON" ]; then
  echo "Electron is not installed. Run: (cd desktop && npm install)" >&2
  exit 1
fi

export GUIDEPILOT_DEMO_STATE="${GUIDEPILOT_DEMO_STATE:-/tmp/guidepilot-demo-state.json}"
echo "demo state file: $GUIDEPILOT_DEMO_STATE"

exec "$ELECTRON" --no-sandbox --force-renderer-accessibility demo-app
