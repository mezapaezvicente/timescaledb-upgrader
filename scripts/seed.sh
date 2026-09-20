#!/usr/bin/env bash
# Write a configurable amount of time-series data into a hypertable.
#
# Knobs (environment or `make seed ROWS=... CHUNK_INTERVAL=...`):
#   ROWS            total rows to insert                     (default 100000)
#   CHUNK_INTERVAL  hypertable chunk_time_interval           (default '1 day')
#   DEVICES         distinct device_id values                (default 10)
#   STEP            time between consecutive ticks           (default '10 seconds')
#   START_TS        first timestamp                          (default 2024-01-01Z)
#   TABLE           table name                               (default metrics)
#   BATCH_ROWS      rows per INSERT                          (default 2000000)
#
# The generated values are deterministic, not random: seeding the same ROWS
# twice produces byte-identical data. That is what makes it possible to compare
# a database before and after an upgrade and claim the contents are unchanged.
#
# The table is dropped and recreated on every run, so ROWS is exact.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ROWS="${ROWS:-100000}"
CHUNK_INTERVAL="${CHUNK_INTERVAL:-1 day}"
DEVICES="${DEVICES:-10}"
STEP="${STEP:-10 seconds}"
START_TS="${START_TS:-2024-01-01 00:00:00+00}"
TABLE="${TABLE:-metrics}"
BATCH_ROWS="${BATCH_ROWS:-2000000}"

need kubectl

case "$ROWS"    in ''|*[!0-9]*) die "ROWS must be a positive integer, got '${ROWS}'" ;; esac
case "$DEVICES" in ''|*[!0-9]*) die "DEVICES must be a positive integer, got '${DEVICES}'" ;; esac
[ "$ROWS" -gt 0 ]    || die "ROWS must be > 0"
[ "$DEVICES" -gt 0 ] || die "DEVICES must be > 0"

log "seeding ${ROWS} rows into ${APP_DB}.${TABLE}"
dim "     chunk_time_interval = ${CHUNK_INTERVAL}   devices = ${DEVICES}   step = ${STEP}"

# TimescaleDB 2.13 introduced the create_hypertable(rel, by_range(...)) form and
# deprecated the positional one. Probe for it rather than assuming: this repo is
# expected to seed old versions too, and guessing here would be a version fact
# invented from memory.
if psql_primary -d "$APP_DB" -tAc \
     "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
       WHERE p.proname = 'by_range' AND n.nspname = 'public'" | grep -q 1; then
  HT_CALL="SELECT create_hypertable('@TABLE@', by_range('time', INTERVAL '@CHUNK@'));"
  dim "     hypertable API: by_range (TimescaleDB >= 2.13)"
else
  HT_CALL="SELECT create_hypertable('@TABLE@', 'time', chunk_time_interval => INTERVAL '@CHUNK@');"
  dim "     hypertable API: positional (TimescaleDB < 2.13)"
fi

sql_template() {
cat <<'SQLEOF'
\set ON_ERROR_STOP on

DROP TABLE IF EXISTS @TABLE@ CASCADE;

CREATE TABLE @TABLE@ (
  time       timestamptz      NOT NULL,
  device_id  integer          NOT NULL,
  value      double precision NOT NULL
);

@HT_CALL@

DO $seed$
DECLARE
  total   bigint := @ROWS@;
  batch   bigint := @BATCH@;
  lo      bigint := 1;
  hi      bigint;
BEGIN
  WHILE lo <= total LOOP
    hi := LEAST(lo + batch - 1, total);
    INSERT INTO @TABLE@ (time, device_id, value)
    SELECT
      TIMESTAMPTZ '@START@' + (((g - 1) / @DEVICES@)::double precision * INTERVAL '@STEP@'),
      ((g - 1) % @DEVICES@)::integer + 1,
      ((g * 7919) % 10007)::double precision / 100.0
    FROM generate_series(lo, hi) AS g;
    RAISE NOTICE 'inserted %/% rows', hi, total;
    lo := hi + 1;
  END LOOP;
END
$seed$;

ANALYZE @TABLE@;
SQLEOF
}

started=$(date +%s)
sql_template \
  | sed -e "s|@HT_CALL@|${HT_CALL}|g" \
  | sed -e "s|@TABLE@|${TABLE}|g" \
        -e "s|@CHUNK@|${CHUNK_INTERVAL}|g" \
        -e "s|@ROWS@|${ROWS}|g" \
        -e "s|@BATCH@|${BATCH_ROWS}|g" \
        -e "s|@DEVICES@|${DEVICES}|g" \
        -e "s|@STEP@|${STEP}|g" \
        -e "s|@START@|${START_TS}|g" \
  | psql_primary -d "$APP_DB" -f -
elapsed=$(( $(date +%s) - started ))

ok "seeded in ${elapsed}s"

# Report what actually landed, rather than trusting the parameters.
psql_primary -d "$APP_DB" -tA -F'  ' -c "
  SELECT 'rows          ' || count(*)                      FROM ${TABLE}
  UNION ALL
  SELECT 'distinct devs ' || count(DISTINCT device_id)     FROM ${TABLE}
  UNION ALL
  SELECT 'time span     ' || min(time) || ' .. ' || max(time) FROM ${TABLE}
  UNION ALL
  SELECT 'chunks        ' || count(*)
    FROM timescaledb_information.chunks
   WHERE hypertable_name = '${TABLE}'
  UNION ALL
  SELECT 'total size    ' || pg_size_pretty(hypertable_size('${TABLE}'::regclass));
" | sed 's/^/     /'
