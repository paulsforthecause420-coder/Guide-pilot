#!/usr/bin/env bash
# Live end-to-end acceptance run.
#
# Starts the deterministic demo application and GuidePilot on the real X11
# desktop, then drives the scenario with real mouse/keyboard input through
# tests/acceptance.desktop.mjs. Requires an X11 session and xdotool.
#
# Usage: ./scripts/acceptance.sh [debug-port]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${1:-9223}"
STATE_FILE="/tmp/guidepilot-acceptance-state.json"
ELECTRON="$ROOT/desktop/node_modules/electron/dist/electron"
LOG_DIR="${TMPDIR:-/tmp}/guidepilot-acceptance"
mkdir -p "$LOG_DIR"

if [ -z "${DISPLAY:-}" ]; then
  echo "DISPLAY is not set: the acceptance run needs a real X11 session." >&2
  exit 1
fi
for tool in xdotool python3; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
[ -x "$ELECTRON" ] || { echo "Electron is not installed. Run: (cd desktop && npm install)" >&2; exit 1; }

echo "==> building"
npm run build --silent

# Two instances of the demo (a leftover from an earlier run, say) share a window
# title and an accessibility application name: the pointer would land in one
# while the assistant read the other, and every verification would be measured
# against the wrong window. That ambiguity is not a product failure, so it is
# rejected up front instead of producing a misleading result.
# xdotool exits non-zero when nothing matches, and this script runs under
# pipefail, so the count is taken defensively.
STRAY=$(xdotool search --name '^GuidePilot Demo Application$' 2>/dev/null | wc -l || true)
if [ "$STRAY" -gt 0 ]; then
  echo "A GuidePilot Demo Application window is already open ($STRAY window(s))." >&2
  echo "Close it first: ./scripts/demo-pid.sh stop" >&2
  exit 1
fi

echo "==> starting demo application"
# A stale state file from an earlier run would make a failed run look correct.
rm -f "$STATE_FILE"
GUIDEPILOT_DEMO_STATE="$STATE_FILE" "$ELECTRON" --no-sandbox --force-renderer-accessibility demo-app \
  >"$LOG_DIR/demo.log" 2>&1 &
DEMO_PID=$!

echo "==> starting GuidePilot (debug port $PORT)"
(cd desktop && "$ELECTRON" . --remote-debugging-port="$PORT") >"$LOG_DIR/app.log" 2>&1 &
APP_PID=$!

cleanup() {
  echo
  echo "==> stopping (logs: $LOG_DIR)"
  kill "$APP_PID" "$DEMO_PID" 2>/dev/null || true
  # The Electron main process starts the Python service as a child; stop it too.
  pkill -P "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
  wait "$DEMO_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> waiting for both applications to come up"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/json/list" >/dev/null 2>&1 \
    && xdotool search --name 'GuidePilot Demo Application' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
sleep 3

node tests/acceptance.desktop.mjs 'GuidePilot Demo Application' "$PORT"
status=$?

echo
echo "demo state:"
cat "$STATE_FILE" 2>/dev/null || true
exit "$status"
