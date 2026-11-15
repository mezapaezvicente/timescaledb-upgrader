-- The time_bucket_ng layer, for harness runs with --ng or --ng-refused.
--
-- Run after seed.sql (or on an empty database), at the source release, which
-- must be from 2.8 to 2.14: time_bucket_ng aggregates of these shapes can be
-- created from 2.7 to 2.14.2, and none can be at 2.15 or later (measured).
-- A database holding one fails every update past 2.14; apply rebuilds them
-- on time_bucket with --migrate-caggs, and stops without it.
--
--   fx_ng_metrics  hypertable (timestamptz), 60 days hourly, 3 devices
--   fx_ng_local    hypertable (timestamp), 60 days hourly
--   fx_ng_month    month in Europe/Madrid, per device; a refresh policy and a
--                  comment
--   fx_ng_week     7 days in Europe/Madrid: time_bucket_ng and time_bucket
--                  start weeks on different days; owned by fx_ng_owner
--   fx_ng_day      1 day on timestamp, no timezone: no row in
--                  continuous_aggs_bucket_function before 2.15, which is what
--                  the old gate missed; fx_ng_reader may SELECT it
--   fx_ng_baseline_month, _week, _day
--                  each aggregate's rows, what ng_check.sql compares against
--
-- All real-time, refreshed over a fixed window (a NULL start fails at 2.8 for
-- a month with a timezone, measured), so the second half of the data is read
-- live. Values are deterministic.

\set ON_ERROR_STOP on

DO $$ BEGIN CREATE ROLE fx_ng_reader; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE fx_ng_owner; EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE fx_ng_metrics (time timestamptz NOT NULL, device integer NOT NULL, value double precision NOT NULL);
SELECT create_hypertable('fx_ng_metrics', 'time', chunk_time_interval => INTERVAL '7 days');
INSERT INTO fx_ng_metrics
SELECT TIMESTAMPTZ '2026-01-01 00:00:00+00' + h * INTERVAL '1 hour', d, ((h * 7919 + d * 104729) % 10007) / 100.0
  FROM generate_series(0, 60 * 24 - 1) AS h, generate_series(1, 3) AS d;

CREATE TABLE fx_ng_local (time timestamp NOT NULL, value double precision NOT NULL);
SELECT create_hypertable('fx_ng_local', 'time', chunk_time_interval => INTERVAL '7 days');
INSERT INTO fx_ng_local
SELECT TIMESTAMP '2026-01-01 00:00:00' + h * INTERVAL '1 hour', ((h * 7919) % 10007) / 100.0
  FROM generate_series(0, 60 * 24 - 1) AS h;

CREATE MATERIALIZED VIEW fx_ng_month
  WITH (timescaledb.continuous, timescaledb.materialized_only = false)
AS SELECT timescaledb_experimental.time_bucket_ng('1 month', time, 'Europe/Madrid') AS bucket,
          device, avg(value) AS avg_value, count(*) AS n
     FROM fx_ng_metrics GROUP BY 1, 2
WITH NO DATA;

CREATE MATERIALIZED VIEW fx_ng_week
  WITH (timescaledb.continuous, timescaledb.materialized_only = false)
AS SELECT timescaledb_experimental.time_bucket_ng('7 days', time, 'Europe/Madrid') AS bucket,
          avg(value) AS avg_value, count(*) AS n
     FROM fx_ng_metrics GROUP BY 1
WITH NO DATA;

CREATE MATERIALIZED VIEW fx_ng_day
  WITH (timescaledb.continuous, timescaledb.materialized_only = false)
AS SELECT timescaledb_experimental.time_bucket_ng('1 day', time) AS bucket,
          avg(value) AS avg_value, count(*) AS n
     FROM fx_ng_local GROUP BY 1
WITH NO DATA;

CALL refresh_continuous_aggregate('fx_ng_month', '2025-12-01', '2026-02-01');
CALL refresh_continuous_aggregate('fx_ng_week', '2025-12-01', '2026-02-01');
CALL refresh_continuous_aggregate('fx_ng_day', '2025-12-01', '2026-02-01');

-- Daily from now on: it runs once when added, and not again during a run.
SELECT add_continuous_aggregate_policy('fx_ng_month', start_offset => INTERVAL '1 year',
                                       end_offset => INTERVAL '1 day', schedule_interval => INTERVAL '1 day');
COMMENT ON VIEW fx_ng_month IS 'monthly, Madrid time';
GRANT SELECT ON fx_ng_metrics TO fx_ng_owner;
ALTER MATERIALIZED VIEW fx_ng_week OWNER TO fx_ng_owner;
GRANT SELECT ON fx_ng_day TO fx_ng_reader;

-- The rows themselves: an average summed in another order differs in the
-- last digits (measured), so ng_check.sql compares them within 1e-9.
CREATE TABLE fx_ng_baseline_month AS SELECT * FROM fx_ng_month;
CREATE TABLE fx_ng_baseline_week AS SELECT * FROM fx_ng_week;
CREATE TABLE fx_ng_baseline_day AS SELECT * FROM fx_ng_day;

SELECT 'ng' AS fixture,
       (SELECT count(*) FROM fx_ng_baseline_month) AS month_rows,
       (SELECT count(*) FROM fx_ng_baseline_week) AS week_rows,
       (SELECT count(*) FROM fx_ng_baseline_day) AS day_rows;
