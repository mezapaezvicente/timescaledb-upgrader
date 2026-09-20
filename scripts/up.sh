#!/usr/bin/env bash
# Bring up a local kind cluster running TimescaleDB under Patroni.
# Idempotent: safe to re-run against an existing cluster.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

READY_TIMEOUT="${READY_TIMEOUT:-900}"

need docker
need kind
need kubectl
need sed

docker info >/dev/null 2>&1 || die "docker does not appear to be running"

log "cluster    ${KIND_CLUSTER}  (context ${KUBE_CONTEXT})"
log "namespace  ${NAMESPACE}"
log "image      ${TSDB_IMAGE}"
log "replicas   ${REPLICAS}"

# --- 1. kind cluster ---------------------------------------------------------

if kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER}"; then
  ok "kind cluster '${KIND_CLUSTER}' already exists"
else
  log "creating kind cluster '${KIND_CLUSTER}' ..."
  kind create cluster --config "${REPO_ROOT}/cluster/kind-config.yaml" --wait 120s
  ok "kind cluster created"
fi

kc cluster-info >/dev/null 2>&1 || die "cannot reach cluster via context ${KUBE_CONTEXT}"

# --- 2. manifests ------------------------------------------------------------

log "applying manifests ..."
render_manifests | kc apply -f -

# --- 3. wait for Patroni -----------------------------------------------------
#
# `kubectl rollout status` is not usable here: the StatefulSet uses the OnDelete
# update strategy (a database should never be rolled by a controller without
# supervision), and rollout status only supports RollingUpdate.

log "waiting for ${REPLICAS} pod(s) to be created ..."
deadline=$(( $(date +%s) + READY_TIMEOUT ))
while :; do
  count="$(kc -n "$NAMESPACE" get pods \
    -l "application=patroni,cluster-name=${CLUSTER_NAME}" \
    --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  [ "${count:-0}" -ge "$REPLICAS" ] && break
  [ "$(date +%s)" -lt "$deadline" ] || die "timed out waiting for pods to appear"
  sleep 3
done

remaining=$(( deadline - $(date +%s) ))
[ "$remaining" -gt 30 ] || remaining=30
log "waiting for readiness (first run pulls a multi-GB image; up to ${remaining}s) ..."
if ! kc -n "$NAMESPACE" wait --for=condition=Ready pod \
      -l "application=patroni,cluster-name=${CLUSTER_NAME}" \
      --timeout="${remaining}s"; then
  warn "pods did not become ready; recent events:"
  kc -n "$NAMESPACE" get pods -o wide || true
  kc -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -20 || true
  die "cluster did not come up"
fi
ok "all members ready"

# --- 4. application database -------------------------------------------------
#
# Done here rather than in Patroni's post_bootstrap hook on purpose: a failing
# bootstrap hook makes Patroni wipe the data directory and retry, which turns a
# one-line SQL typo into an unreadable crash loop. Here it is re-runnable and
# the error lands in front of you.

log "ensuring database '${APP_DB}' and the timescaledb extension ..."
if ! psql_primary -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname = '${APP_DB}'" | grep -q 1; then
  psql_primary -d postgres -c "CREATE DATABASE \"${APP_DB}\""
fi
psql_primary -d "${APP_DB}" -c "CREATE EXTENSION IF NOT EXISTS timescaledb"

# --- 5. summary --------------------------------------------------------------

echo
ok "cluster is up"
psql_primary -d "${APP_DB}" -tAc \
  "SELECT 'postgres ' || current_setting('server_version') ||
          '  |  timescaledb ' || extversion
     FROM pg_extension WHERE extname = 'timescaledb'" | sed 's/^/     /'
echo
dim "  members:   make status"
dim "  psql:      make psql"
dim "  seed data: make seed ROWS=1000000"
dim "  tear down: make down"
