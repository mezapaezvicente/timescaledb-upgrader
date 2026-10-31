#!/usr/bin/env bash
# Run the unit test suite.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3 python py; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)" || {
  echo "err  no usable Python 3.8+ found on PATH (set PYTHON=/path/to/python)" >&2
  exit 1
}

if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
  echo "err  pytest is not installed: $PY -m pip install -r requirements-dev.txt" >&2
  exit 1
fi

# The scripts apply and rollback run in pods: lint them when shellcheck is here.
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck tools/tsupgrade/scripts/*.sh || exit 1
else
  echo "note: shellcheck not installed; tools/tsupgrade/scripts/*.sh not linted" >&2
fi

exec "$PY" -m pytest "$@"
