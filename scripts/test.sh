#!/usr/bin/env bash
# Run the automated suites. The live end-to-end acceptance run is separate:
#   ./scripts/acceptance.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail=0
step() {
  echo
  echo "==> $*"
}

step "backend unit and integration tests (pytest)"
(cd backend && python3 -m pytest tests -q) || fail=1

step "desktop type checks (tsc)"
(cd desktop && npm run typecheck) || fail=1

step "desktop node tests (geometry + IPC surface)"
npm run build:electron --silent || fail=1
node --test desktop/tests/*.test.mjs || fail=1

if [ "$fail" -ne 0 ]; then
  echo
  echo "Some suites failed." >&2
  exit 1
fi

echo
echo "All automated suites passed."
