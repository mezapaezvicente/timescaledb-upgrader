#!/usr/bin/env bash
# Destroy the local test cluster. Deletes the kind cluster outright, which takes
# the PersistentVolumes (backed by directories inside the node container) with
# it -- no orphaned local-path data left behind on the host.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kind

if kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER}"; then
  log "deleting kind cluster '${KIND_CLUSTER}' ..."
  kind delete cluster --name "${KIND_CLUSTER}"
  ok "cluster deleted"
else
  ok "kind cluster '${KIND_CLUSTER}' does not exist; nothing to do"
fi
