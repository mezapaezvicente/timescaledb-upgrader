#!/usr/bin/env bash
# Optional accelerator: copy TSDB_IMAGE from the local Docker daemon into the
# kind node, instead of having the node pull it from Docker Hub.
#
# Worth it when you already have the image locally or are switching between
# several of them. `make up` does not call this -- the default path pulls inside
# the node, so the harness has no dependency on local Docker state.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need docker
need kind

if ! docker image inspect "$TSDB_IMAGE" >/dev/null 2>&1; then
  log "pulling ${TSDB_IMAGE} into local Docker ..."
  docker pull "$TSDB_IMAGE"
fi

log "side-loading ${TSDB_IMAGE} into kind cluster '${KIND_CLUSTER}' (slow; multi-GB) ..."
kind load docker-image "$TSDB_IMAGE" --name "$KIND_CLUSTER"
ok "image available inside the cluster"
