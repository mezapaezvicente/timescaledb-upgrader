#!/usr/bin/env bash
# Delete the namespace (and with it the StatefulSet, PVCs and data) while
# leaving the kind cluster in place. Faster than a full down/up when you only
# want to start from an empty database.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kubectl

if ! kc get namespace "$NAMESPACE" >/dev/null 2>&1; then
  ok "namespace ${NAMESPACE} does not exist; nothing to do"
  exit 0
fi

log "deleting namespace ${NAMESPACE} (this destroys the database) ..."
kc delete namespace "$NAMESPACE" --wait=true
ok "namespace deleted; run 'make up' to recreate"
