#!/usr/bin/env bash
# Interactive psql shell on the current Patroni primary.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kubectl
pod="$(primary_pod)"
[ -n "$pod" ] || die "no pod is currently the Patroni primary in ns/${NAMESPACE}"

log "psql on ${pod} (database ${APP_DB})"
exec kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" exec -it "$pod" -c timescaledb -- \
  psql -U postgres -d "${APP_DB}"
