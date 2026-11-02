#!/usr/bin/env bash
# Start or stop the demo application by recorded PID.
#
# Pattern-based kills (pkill -f) are avoided on purpose: the pattern would also
# match the shell that runs the kill. This helper records the PID instead.
#
#   ./scripts/demo-pid.sh start [state-file]
#   ./scripts/demo-pid.sh stop
#   ./scripts/demo-pid.sh status
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="/tmp/guidepilot-demo.pid"
ELECTRON="$ROOT/desktop/node_modules/electron/dist/electron"

case "${1:-status}" in
  start)
    STATE="${2:-/tmp/guidepilot-demo-state.json}"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "demo already running: $(cat "$PIDFILE")"
      exit 0
    fi
    setsid nohup env GUIDEPILOT_DEMO_STATE="$STATE" "$ELECTRON" \
      --no-sandbox --force-renderer-accessibility "$ROOT/demo-app" \
      > /tmp/guidepilot-demo.log 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    echo "demo started: $(cat "$PIDFILE") state=$STATE"
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      PID="$(cat "$PIDFILE")"
      kill "$PID" 2>/dev/null || true
      pkill -P "$PID" 2>/dev/null || true
      rm -f "$PIDFILE"
      echo "demo stopped: $PID"
    else
      echo "no pidfile"
    fi
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running: $(cat "$PIDFILE")"
    else
      echo "not running"
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|status} [state-file]" >&2
    exit 2
    ;;
esac
