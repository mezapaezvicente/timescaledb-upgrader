#!/usr/bin/env bash
# Read-only snapshot of the harness: Patroni members, pods, services, volumes.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kubectl

log "context ${KUBE_CONTEXT}  namespace ${NAMESPACE}"
echo

pod="$(primary_pod)"
if [ -n "$pod" ]; then
  echo "Patroni members:"
  kc -n "$NAMESPACE" exec "$pod" -c timescaledb -- patronictl list 2>/dev/null || \
    warn "patronictl failed"
  echo
else
  warn "no primary elected yet"
fi

echo "Pods:"
kc -n "$NAMESPACE" get pods -o wide -L role 2>/dev/null || true
echo
echo "Services:"
kc -n "$NAMESPACE" get svc 2>/dev/null || true
echo
echo "Volumes:"
kc -n "$NAMESPACE" get pvc 2>/dev/null || true
