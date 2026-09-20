#!/usr/bin/env bash
# Shared configuration and helpers. Sourced by every script in this directory.
#
# Every value below can be overridden from the environment or the make command
# line, e.g.  make up REPLICAS=3 TSDB_IMAGE=timescale/timescaledb-ha:pg17-ts2.30

set -euo pipefail

# --- configuration -----------------------------------------------------------

# Name of the kind cluster and, by derivation, the kubectl context.
KIND_CLUSTER="${KIND_CLUSTER:-tsdb-upgrader}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-${KIND_CLUSTER}}"

# Patroni scope / StatefulSet name, and the namespace it lives in.
CLUSTER_NAME="${CLUSTER_NAME:-tsdb}"
NAMESPACE="${NAMESPACE:-timescale}"

# The TimescaleDB HA image under test. Pinned to an exact pg/ts pair on purpose:
# floating tags make "which versions did this run against?" unanswerable.
TSDB_IMAGE="${TSDB_IMAGE:-timescale/timescaledb-ha:pg16.15-ts2.30.1}"

REPLICAS="${REPLICAS:-1}"
STORAGE="${STORAGE:-5Gi}"

# Application database seeded with time-series data.
APP_DB="${APP_DB:-bench}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- output ------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'
  C_RED=$'\033[31m';  C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_BLUE=""; C_GREEN=""; C_RED=""; C_YELLOW=""; C_DIM=""
fi

log()  { printf '%s==>%s %s\n' "$C_BLUE"  "$C_RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%serr %s %s\n' "$C_RED"   "$C_RESET" "$*" >&2; exit 1; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

# --- tooling -----------------------------------------------------------------

need() {
  command -v "$1" >/dev/null 2>&1 || die "required tool not found on PATH: $1"
}

# Every kubectl call in this repo goes through here, so it is impossible to
# accidentally operate on whatever context happens to be current.
kc() {
  kubectl --context "$KUBE_CONTEXT" "$@"
}

# --- manifest rendering ------------------------------------------------------

# Emit every manifest in deploy/ as one YAML stream with placeholders filled in.
# Written to stdout and piped into `kubectl apply -f -`: passing file paths
# would break under Git Bash on Windows, where kubectl.exe cannot read POSIX
# paths. stdin sidesteps path translation entirely.
render_manifests() {
  local f first=1
  for f in "$REPO_ROOT"/deploy/*.yaml; do
    if [ "$first" -eq 0 ]; then printf -- '---\n'; fi
    first=0
    sed \
      -e "s|__NAMESPACE__|${NAMESPACE}|g" \
      -e "s|__CLUSTER_NAME__|${CLUSTER_NAME}|g" \
      -e "s|__IMAGE__|${TSDB_IMAGE}|g" \
      -e "s|__REPLICAS__|${REPLICAS}|g" \
      -e "s|__STORAGE__|${STORAGE}|g" \
      "$f"
  done
}

# --- cluster helpers ---------------------------------------------------------

# Name of the pod currently holding the Patroni leader lock.
primary_pod() {
  kc -n "$NAMESPACE" get pods \
    -l "application=patroni,cluster-name=${CLUSTER_NAME},role=primary" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
}

# Run psql inside the primary. Extra args are passed straight to psql.
# Uses the local unix socket as the postgres superuser, which pg_hba trusts.
psql_primary() {
  local pod
  pod="$(primary_pod)"
  [ -n "$pod" ] || die "no pod is currently the Patroni primary in ns/${NAMESPACE}"
  kc -n "$NAMESPACE" exec -i "$pod" -c timescaledb -- \
    psql -U postgres -v ON_ERROR_STOP=1 "$@"
}
