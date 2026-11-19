-- Integrity checks for a database seeded by seed.sql, run after the upgrade.
--
-- Prints one line per check, `check|<name>|pass|fail|<detail>`, so the
-- caller can parse it with psql -qtA. A check that errors prints no line at
-- all; the caller treats a missing line as a failure, so ON_ERROR_STOP stays
-- off and one broken check does not hide the others.
--
-- Runs at the *target* version, which is always 2.x, so only 2.x syntax is
-- used here. The order matters: the read-only checks come first, then the
-- ones that write (refresh, insert, decompress).
--
-- Optional: psql -v expect=<version> adds a check on extversion.
--
-- A step a check depends on (a refresh, an insert) is tracked with psql's
-- :ERROR, and its failure fails the check. Otherwise a real-time aggregate
-- "matches" a fresh query with no refresh having worked at all, because the
-- view computes unmaterialized buckets on the fly. For the same reason every
-- aggregate check reads only what was actually materialized: the aggregate is
-- switched to materialized_only first, or, when it cannot be, read below its
-- watermark (see below).

\set ON_ERROR_STOP off

\if :{?expect}
SELECT 'check', 'version',
       CASE WHEN extversion = :'expect' THEN 'pass' ELSE 'fail' END,
       'extversion ' || extversion || ', expected ' || :'expect'
  FROM pg_extension WHERE extname = 'timescaledb';
\endif

-- Every row reads back, compressed chunks included.
SELECT 'check', 'rows',
       CASE WHEN n = b.rows AND t = b.total THEN 'pass' ELSE 'fail' END,
       format('%s rows, sum %s; seeded %s rows, sum %s at %s', n, t, b.rows, b.total, b.seeded_at)
  FROM (SELECT count(*) AS n, sum(value::numeric) AS t FROM fx_metrics) m, fx_baseline b;

-- The jobs are still registered and their stats still show the success and
-- the failure. job_errors is not compared: from 2.15 it reads a table the
-- update itself rebuilds.
SELECT 'check', 'jobs',
       CASE WHEN b.jobs = 0 OR (registered = b.jobs AND ok_runs >= 1 AND failures >= 1)
            THEN 'pass' ELSE 'fail' END,
       CASE WHEN b.jobs = 0 THEN format('none seeded at %s', b.seeded_at)
            ELSE format('%s jobs registered, seeded %s; fx_job_ok %s runs, fx_job_fail %s failures',
                        registered, b.jobs, ok_runs, failures) END
  FROM fx_baseline b,
       (SELECT count(*) AS registered,
               coalesce(sum(s.total_runs) FILTER (WHERE j.proc_name = 'fx_job_ok'), 0) AS ok_runs,
               coalesce(sum(s.total_failures) FILTER (WHERE j.proc_name = 'fx_job_fail'), 0) AS failures
          FROM timescaledb_information.jobs j
          LEFT JOIN timescaledb_information.job_stats s USING (job_id)
         WHERE j.proc_name IN ('fx_job_ok', 'fx_job_fail')) j;

-- The upgrade must not decompress anything on its own.
SELECT 'check', 'still_compressed',
       CASE WHEN n = b.compressed THEN 'pass' ELSE 'fail' END,
       format('%s compressed chunks; seeded %s', n, b.compressed)
  FROM (SELECT count(*) AS n FROM timescaledb_information.chunks
         WHERE hypertable_name = 'fx_metrics' AND is_compressed) c, fx_baseline b;

-- From here on the aggregate checks read only what is materialized.
--
-- An aggregate in the old format (created before 2.7, never migrated) cannot
-- be altered from 2.14.0: "Continuous Aggregates with
-- partials is not supported anymore". Its view stays real-time, and a
-- real-time view takes buckets below the watermark from the materialization
-- and computes only those at or above it from the hypertable. So :materialized
-- is a filter that keeps the materialized rows either way, read again at each
-- query because a refresh moves the watermark. The watermark functions are in
-- _timescaledb_functions from 2.12, below every release that refuses the ALTER.
ALTER MATERIALIZED VIEW fx_hourly SET (timescaledb.materialized_only = true);
\if :ERROR
SELECT mat_hypertable_id AS fx_mat FROM _timescaledb_catalog.continuous_agg
 WHERE user_view_name = 'fx_hourly' \gset
\set materialized 'bucket < _timescaledb_functions.to_timestamp(_timescaledb_functions.cagg_watermark(' :fx_mat '))'
\set how ' (old format, cannot be altered: read below the watermark)'
\else
\set materialized true
\set how ''
\endif

-- The materialization survived, before anything refreshes it.
SELECT 'check', 'cagg_survived',
       CASE WHEN n = b.cagg_rows THEN 'pass' ELSE 'fail' END,
       format('%s materialized rows; seeded %s%s', n, b.cagg_rows, :'how')
  FROM (SELECT count(*) AS n FROM fx_hourly WHERE :materialized) c, fx_baseline b;

-- The aggregate still refreshes, and matches the same query run fresh.
CALL refresh_continuous_aggregate('fx_hourly', NULL, NULL);
\if :ERROR
\set refreshed false
\else
\set refreshed true
\endif

WITH fresh AS (
  SELECT time_bucket(INTERVAL '1 hour', time) AS bucket, device,
         round(avg(value)::numeric, 6) AS a, count(*) AS n
    FROM fx_metrics GROUP BY 1, 2
), agg AS (
  SELECT bucket, device, round(avg_value::numeric, 6) AS a, n FROM fx_hourly WHERE :materialized
), diff AS (
  (SELECT * FROM fresh EXCEPT SELECT * FROM agg)
  UNION ALL
  (SELECT * FROM agg EXCEPT SELECT * FROM fresh)
)
SELECT 'check', 'cagg_matches',
       CASE WHEN :'refreshed'::boolean AND count(*) = 0 THEN 'pass' ELSE 'fail' END,
       format('%s differing buckets; refresh %s%s', count(*),
              CASE WHEN :'refreshed'::boolean THEN 'ran' ELSE 'FAILED' END, :'how')
  FROM diff;

-- New writes land, and a refresh picks them up.
INSERT INTO fx_metrics (time, device, value)
SELECT TIMESTAMPTZ '2026-01-08 00:00:00+00' + (m * INTERVAL '1 minute'), 1, 1.0
  FROM generate_series(0, 59) AS m;
\if :ERROR
\set wrote false
\else
\set wrote true
\endif

CALL refresh_continuous_aggregate('fx_hourly', TIMESTAMPTZ '2026-01-08 00:00:00+00',
                                  TIMESTAMPTZ '2026-01-08 01:00:00+00');
\if :ERROR
\set refreshed_window false
\else
\set refreshed_window true
\endif

SELECT 'check', 'write_and_refresh',
       CASE WHEN :'wrote'::boolean AND :'refreshed_window'::boolean
                 AND count(*) = 1 AND min(n) = 60
            THEN 'pass' ELSE 'fail' END,
       format('insert %s, refresh %s; %s new buckets, %s rows in them%s',
              CASE WHEN :'wrote'::boolean THEN 'ok' ELSE 'FAILED' END,
              CASE WHEN :'refreshed_window'::boolean THEN 'ok' ELSE 'FAILED' END,
              count(*), coalesce(sum(n), 0), :'how')
  FROM fx_hourly WHERE bucket = TIMESTAMPTZ '2026-01-08 00:00:00+00' AND :materialized;

-- Compressed chunks still decompress, and give back exactly what went in.
SELECT count(*) AS decompressed
  FROM (SELECT decompress_chunk(c, if_compressed => true)
          FROM show_chunks('fx_metrics', older_than => TIMESTAMPTZ '2026-01-04 00:00:00+00') AS c) s
\gset

SELECT 'check', 'decompress',
       CASE WHEN :decompressed = b.compressed AND n = b.rows AND t = b.total
            THEN 'pass' ELSE 'fail' END,
       format('%s chunks decompressed; %s rows, sum %s before the new writes',
              :decompressed, n, t)
  FROM (SELECT count(*) AS n, sum(value::numeric) AS t FROM fx_metrics
         WHERE time < TIMESTAMPTZ '2026-01-08 00:00:00+00') m, fx_baseline b;
