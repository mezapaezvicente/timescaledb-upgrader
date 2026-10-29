#!/usr/bin/env bash
# Thin wrapper around `python -m tsupgrade`.
#
# Exists so that interpreter discovery and PYTHONPATH live in exactly one
# place. The package is not installed -- it runs straight from tools/ in the
# clone, because an upgrade tool that needs its own install step first is one
# more thing to go wrong during a maintenance window.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3 python py; do
    [ -n "$candidate" ] || continue
    # `python3` on Windows is often a Microsoft Store stub that exits non-zero.
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

TOOLS="${REPO_ROOT}/tools"
# Git Bash normally rewrites PYTHONPATH for a Windows Python, but not under
# MSYS_NO_PATHCONV=1 (which docker/kubectl exec calls need), so do it here.
SEP=":"
if command -v cygpath >/dev/null 2>&1; then TOOLS="$(cygpath -w "$TOOLS")"; SEP=";"; fi
export PYTHONPATH="${TOOLS}${PYTHONPATH:+${SEP}${PYTHONPATH}}"
exec "$PY" -m tsupgrade "$@"
