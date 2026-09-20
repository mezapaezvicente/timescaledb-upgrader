#!/usr/bin/env bash
# Tail the primary's Patroni + PostgreSQL log stream.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kubectl
pod="${1:-$(primary_pod)}"
[ -n "$pod" ] || die "no pod is currently the Patroni primary in ns/${NAMESPACE}"

log "tailing logs from ${pod} (ctrl-c to stop)"
exec kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" logs -f "$pod" -c timescaledb --tail=100
