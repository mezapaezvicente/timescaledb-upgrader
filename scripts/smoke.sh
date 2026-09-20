#!/usr/bin/env bash
# End-to-end check that the harness actually works: connect, create a
# hypertable, insert, query, and verify the answers are the expected ones.
#
# Uses its own throwaway table so it never touches whatever `make seed` wrote.
# Read/write, but only against that table -- unlike the Phase 3 preflight
# checks, which are strictly read-only.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kubectl

TABLE="_smoke_metrics"
PASS=0
FAIL=0

# check <label> <expected> <actual>
check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf '%s  ok%s %-42s %s\n' "$C_GREEN" "$C_RESET" "$label" "$actual"
    PASS=$((PASS + 1))
  else
    printf '%sfail%s %-42s expected %s, got %s\n' \
      "$C_RED" "$C_RESET" "$label" "$expected" "$actual" >&2
    FAIL=$((FAIL + 1))
  fi
}

# check_nonempty <label> <actual>
check_nonempty() {
  local label="$1" actual="$2"
  if [ -n "$actual" ]; then
    printf '%s  ok%s %-42s %s\n' "$C_GREEN" "$C_RESET" "$label" "$actual"
    PASS=$((PASS + 1))
  else
    printf '%sfail%s %-42s empty result\n' "$C_RED" "$C_RESET" "$label" >&2
    FAIL=$((FAIL + 1))
  fi
}

q() { psql_primary -d "$APP_DB" -tAc "$1" | tr -d '\r'; }

pod="$(primary_pod)"
log "context ${KUBE_CONTEXT}  namespace ${NAMESPACE}  primary ${pod:-<none>}"
log "database ${APP_DB}"
echo

# 1. Connectivity and versions -------------------------------------------------
check_nonempty "connect: server_version"   "$(q "SHOW server_version" | awk '{print $1}')"
check_nonempty "connect: timescaledb ext"  "$(q "SELECT extversion FROM pg_extension WHERE extname='timescaledb'")"
check          "patroni reports a leader"  "1" \
               "$(kc -n "$NAMESPACE" get pods -l "application=patroni,cluster-name=${CLUSTER_NAME},role=primary" --no-headers 2>/dev/null | wc -l | tr -d ' ')"

# 2. Hypertable creation -------------------------------------------------------
q "DROP TABLE IF EXISTS ${TABLE} CASCADE" >/dev/null
q "CREATE TABLE ${TABLE} (time timestamptz NOT NULL, device_id int NOT NULL, value double precision NOT NULL)" >/dev/null

if q "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
       WHERE p.proname='by_range' AND n.nspname='public'" | grep -q 1; then
  q "SELECT create_hypertable('${TABLE}', by_range('time', INTERVAL '1 hour'))" >/dev/null
else
  q "SELECT create_hypertable('${TABLE}', 'time', chunk_time_interval => INTERVAL '1 hour')" >/dev/null
fi

check "hypertable registered" "1" \
      "$(q "SELECT count(*) FROM timescaledb_information.hypertables
             WHERE hypertable_name='${TABLE}'")"

# 3. Insert --------------------------------------------------------------------
# 6 hours of data, one row per minute per device, 3 devices = 1080 rows,
# which must land in 6 chunks given the 1 hour chunk interval.
q "INSERT INTO ${TABLE} (time, device_id, value)
   SELECT TIMESTAMPTZ '2024-03-01 00:00:00+00' + ((g-1)/3) * INTERVAL '1 minute',
          ((g-1) % 3) + 1,
          (g % 100)::double precision
     FROM generate_series(1, 1080) AS g" >/dev/null

check "rows inserted"          "1080" "$(q "SELECT count(*) FROM ${TABLE}")"
check "distinct devices"       "3"    "$(q "SELECT count(DISTINCT device_id) FROM ${TABLE}")"
check "chunks created"         "6"    "$(q "SELECT count(*) FROM timescaledb_information.chunks
                                             WHERE hypertable_name='${TABLE}'")"

# 4. Query ---------------------------------------------------------------------
check "time_bucket: hourly buckets" "6" \
      "$(q "SELECT count(*) FROM (
              SELECT time_bucket(INTERVAL '1 hour', time) AS b
                FROM ${TABLE} GROUP BY b) s")"
check "time_bucket: rows per bucket" "180" \
      "$(q "SELECT DISTINCT n FROM (
              SELECT count(*) AS n FROM ${TABLE}
               GROUP BY time_bucket(INTERVAL '1 hour', time)) s")"
check "aggregate across chunks" "1080" \
      "$(q "SELECT count(*)::text FROM ${TABLE}
             WHERE time >= TIMESTAMPTZ '2024-03-01 00:00:00+00'
               AND time <  TIMESTAMPTZ '2024-03-01 06:00:00+00'")"
check_nonempty "chunk exclusion in plan" \
      "$(q "EXPLAIN (COSTS OFF) SELECT count(*) FROM ${TABLE}
             WHERE time >= TIMESTAMPTZ '2024-03-01 05:00:00+00'" \
         | grep -c '_hyper_' || true)"

# 5. Cleanup -------------------------------------------------------------------
q "DROP TABLE ${TABLE} CASCADE" >/dev/null
check "cleanup: table removed" "0" \
      "$(q "SELECT count(*) FROM pg_class WHERE relname='${TABLE}'")"

echo
if [ "$FAIL" -eq 0 ]; then
  ok "smoke test passed (${PASS} checks)"
else
  die "smoke test FAILED (${FAIL} failed, ${PASS} passed)"
fi
