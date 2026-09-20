#!/usr/bin/env bash
# Read-only preflight checks against the running cluster.
#
#   make preflight TARGET_IMAGE=timescale/timescaledb-ha:pg17.11-ts2.30.1
#
# Nothing this runs mutates cluster or database state.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET_IMAGE="${TARGET_IMAGE:-}"
if [ -z "$TARGET_IMAGE" ]; then
  die "set TARGET_IMAGE, e.g. make preflight TARGET_IMAGE=timescale/timescaledb-ha:pg17.11-ts2.30.1"
fi

need kubectl

exec bash "${REPO_ROOT}/scripts/tsupgrade.sh" preflight \
  --target-image "$TARGET_IMAGE" \
  --context "$KUBE_CONTEXT" \
  --namespace "$NAMESPACE" \
  --selector "application=patroni,cluster-name=${CLUSTER_NAME}" \
  --container timescaledb \
  --psql-user postgres \
  "$@"
