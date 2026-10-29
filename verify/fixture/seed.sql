-- The seeded fixture for matrix verification runs.
--
-- Creates, at whatever TimescaleDB version is installed in the current
-- database, the objects an upgrade has to carry across:
--
--   fx_metrics   hypertable, 7 daily chunks, 5 devices, one row per minute
--   fx_hourly    continuous aggregate (hourly avg and count per device)
--   compression  enabled, the oldest 3 chunks compressed
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
-- Deliberately not covered: time_bucket_ng aggregates (refused from 2.13, and
-- a gate rather than a fixture concern), retention, background policies. No
-- policy is added, so no background job changes the data between seed and
-- check. Values are deterministic, so the same seed is byte-identical.
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

INSERT INTO fx_metrics (time, device, value)
SELECT TIMESTAMPTZ '2026-01-01 00:00:00+00' + (m * INTERVAL '1 minute'),
       d,
       ((m * 7919 + d * 104729) % 10007)::double precision / 100.0
  FROM generate_series(0, 7 * 1440 - 1) AS m,
       generate_series(1, 5) AS d;

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

CREATE TABLE fx_baseline AS
SELECT :'extversion'::text                          AS seeded_at,
       (SELECT count(*) FROM fx_metrics)            AS rows,
       (SELECT sum(value::numeric) FROM fx_metrics) AS total,
       (SELECT count(*) FROM fx_hourly)             AS cagg_rows,
       :compressed::bigint                          AS compressed;

SELECT 'seeded' AS fixture, * FROM fx_baseline;
