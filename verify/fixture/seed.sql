-- The seeded fixture for matrix verification runs.
--
-- Creates, at whatever TimescaleDB version is installed in the current
-- database, the objects an upgrade has to carry across:
--
--   fx_metrics   hypertable, 7 daily chunks, 5 devices (or :devices), one row
--                per minute
--   fx_hourly    continuous aggregate (hourly avg and count per device)
--   compression  enabled, the oldest 3 chunks compressed
--   jobs         fx_job_ok and fx_job_fail, each run once by the scheduler,
--                so job stats (and from 2.9 job_errors) hold rows; from 2.0,
--                where add_job arrives
--   fx_baseline  what check.sql compares against after the upgrade
--
-- One script for every source version from 1.7 to 2.29. The syntax that
-- changed is chosen by probing the installed extension, never assumed:
--
--   * continuous aggregates: 1.x is CREATE VIEW ... WITH (timescaledb.continuous)
--     refreshed by REFRESH MATERIALIZED VIEW; 2.x is CREATE MATERIALIZED VIEW ...
--     WITH NO DATA refreshed by refresh_continuous_aggregate().
--   * 1.x materializes at most max_interval_per_job per refresh (20 buckets by
--     default), so it is raised to cover the whole range in one refresh.
--   * create_hypertable: by_range() where it exists (2.13+), positional before.
--
-- Deliberately not covered: time_bucket_ng aggregates (ng.sql, an optional
-- layer for the --ng runs: they stop every update past 2.14), retention,
-- background policies, and
-- user views on timescaledb_information (views.sql, an optional layer: they
-- block most updates by design, which is the information_views gate's job).
-- No policy is added and the jobs touch no table, so nothing changes the data
-- between seed and check. Values are deterministic, so the data is too.
--
-- Run with psql -X against a database where the extension is already at the
-- source version.

\set ON_ERROR_STOP on

SELECT extversion,
       string_to_array(extversion, '.')::int[] < '{2,0}' AS ts1,
       EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE p.proname = 'by_range' AND n.nspname = 'public') AS has_by_range
  FROM pg_extension WHERE extname = 'timescaledb' \gset

CREATE TABLE fx_metrics (
  time    timestamptz      NOT NULL,
  device  integer          NOT NULL,
  value   double precision NOT NULL
);

\if :has_by_range
SELECT create_hypertable('fx_metrics', by_range('time', INTERVAL '1 day'));
\else
SELECT create_hypertable('fx_metrics', 'time', chunk_time_interval => INTERVAL '1 day');
\endif

-- devices: how many series, each 10,080 rows. 5 (50,400 rows) unless a size
-- run passes -v devices=N (harness --devices); the first 5 are the same rows.
\if :{?devices}
\else
\set devices 5
\endif

INSERT INTO fx_metrics (time, device, value)
SELECT TIMESTAMPTZ '2026-01-01 00:00:00+00' + (m * INTERVAL '1 minute'),
       d,
       ((m * 7919 + d * 104729) % 10007)::double precision / 100.0
  FROM generate_series(0, 7 * 1440 - 1) AS m,
       generate_series(1, :devices) AS d;

\if :ts1
CREATE VIEW fx_hourly
  WITH (timescaledb.continuous,
        timescaledb.max_interval_per_job = '30 days')
AS SELECT time_bucket(INTERVAL '1 hour', time) AS bucket,
          device,
          avg(value) AS avg_value,
          count(*)   AS n
     FROM fx_metrics
    GROUP BY 1, 2;
REFRESH MATERIALIZED VIEW fx_hourly;
\else
CREATE MATERIALIZED VIEW fx_hourly
  WITH (timescaledb.continuous)
AS SELECT time_bucket(INTERVAL '1 hour', time) AS bucket,
          device,
          avg(value) AS avg_value,
          count(*)   AS n
     FROM fx_metrics
    GROUP BY 1, 2
WITH NO DATA;
CALL refresh_continuous_aggregate('fx_hourly', NULL, NULL);
\endif

ALTER TABLE fx_metrics SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'device',
  timescaledb.compress_orderby   = 'time DESC'
);

SELECT count(*) AS compressed
  FROM (SELECT compress_chunk(c)
          FROM show_chunks('fx_metrics', older_than => TIMESTAMPTZ '2026-01-04 00:00:00+00') AS c) s
\gset

-- Jobs with a history: one success and one failure, run by the scheduler
-- rather than run_job(), because only the scheduler records stats and errors.
-- Daily, so neither runs again during a verification run. The wait is for the
-- recorded failure, not for a number of runs: a failing job can have started
-- and not yet finished.
SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE p.proname = 'add_job' AND n.nspname = 'public') AS has_jobs \gset

\if :has_jobs
CREATE PROCEDURE fx_job_ok(job_id int, config jsonb)
  LANGUAGE plpgsql AS $$ BEGIN PERFORM 1; END $$;
CREATE PROCEDURE fx_job_fail(job_id int, config jsonb)
  LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fx_job_fail: deliberate failure'; END $$;
SELECT add_job('fx_job_ok', INTERVAL '1 day') AS job_ok,
       add_job('fx_job_fail', INTERVAL '1 day') AS job_fail \gset

-- psql does not substitute variables in a dollar-quoted body, so the block
-- finds the jobs by name.
DO $$
DECLARE
  waited int := 0;
  ok_runs bigint;
  failures bigint;
  errors bigint;
BEGIN
  LOOP
    SELECT s.total_runs INTO ok_runs
      FROM timescaledb_information.jobs j JOIN timescaledb_information.job_stats s USING (job_id)
     WHERE j.proc_name = 'fx_job_ok';
    SELECT s.total_failures INTO failures
      FROM timescaledb_information.jobs j JOIN timescaledb_information.job_stats s USING (job_id)
     WHERE j.proc_name = 'fx_job_fail';
    errors := 1;
    IF EXISTS (SELECT 1 FROM pg_views WHERE schemaname = 'timescaledb_information'
                  AND viewname = 'job_errors') THEN
      EXECUTE 'SELECT count(*) FROM timescaledb_information.job_errors WHERE proc_name = ''fx_job_fail'''
         INTO errors;
    END IF;
    EXIT WHEN coalesce(ok_runs, 0) >= 1 AND coalesce(failures, 0) >= 1 AND errors >= 1;
    IF waited >= 120 THEN
      RAISE EXCEPTION 'fixture jobs did not run within 120s (ok runs %, failures %, errors %)',
        ok_runs, failures, errors;
    END IF;
    PERFORM pg_sleep(1);
    waited := waited + 1;
  END LOOP;
END $$;
\set jobs 2
\else
\set jobs 0
\endif

CREATE TABLE fx_baseline AS
SELECT :'extversion'::text                          AS seeded_at,
       (SELECT count(*) FROM fx_metrics)            AS rows,
       (SELECT sum(value::numeric) FROM fx_metrics) AS total,
       (SELECT count(*) FROM fx_hourly)             AS cagg_rows,
       :compressed::bigint                          AS compressed,
       :jobs::int                                   AS jobs;

SELECT 'seeded' AS fixture, * FROM fx_baseline;
