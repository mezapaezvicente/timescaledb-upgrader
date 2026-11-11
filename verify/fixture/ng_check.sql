-- Checks for the time_bucket_ng layer (ng.sql), after a run.
--
-- psql -v rebuilt=true after a run that had to rebuild them on time_bucket,
-- rebuilt=false after one that had to stop. Prints `check|<name>|pass|fail|<detail>`
-- like check.sql, and like it keeps going past a check that errors (the
-- caller treats a missing line as a failure).
--
--   ng_rows     every aggregate returns the rows it did at the source
--   ng_bucket   none is on time_bucket_ng any more, or after a stop all three are
--   ng_kept     the refresh policy, comment, owner and grant are still there
--   ng_refresh  (rebuilt only) a new row reaches an aggregate by refresh

\set ON_ERROR_STOP off

-- Rows match on everything exactly but the average, which matches within
-- 1e-9: the same rows summed in another order differ in the last digits.
SELECT 'check', 'ng_rows',
       CASE WHEN sum(bad) = 0 THEN 'pass' ELSE 'fail' END,
       string_agg(name || ' ' || CASE WHEN bad = 0 THEN 'same' ELSE bad || ' row(s) differ' END, ', ' ORDER BY name)
  FROM (
    SELECT 'fx_ng_month' AS name, count(*) AS bad
      FROM fx_ng_baseline_month b FULL JOIN fx_ng_month c USING (bucket, device, n)
     WHERE b.avg_value IS NULL OR c.avg_value IS NULL
        OR abs(b.avg_value - c.avg_value) > 1e-9 * greatest(1, abs(b.avg_value))
    UNION ALL
    SELECT 'fx_ng_week', count(*)
      FROM fx_ng_baseline_week b FULL JOIN fx_ng_week c USING (bucket, n)
     WHERE b.avg_value IS NULL OR c.avg_value IS NULL
        OR abs(b.avg_value - c.avg_value) > 1e-9 * greatest(1, abs(b.avg_value))
    UNION ALL
    SELECT 'fx_ng_day', count(*)
      FROM fx_ng_baseline_day b FULL JOIN fx_ng_day c USING (bucket, n)
     WHERE b.avg_value IS NULL OR c.avg_value IS NULL
        OR abs(b.avg_value - c.avg_value) > 1e-9 * greatest(1, abs(b.avg_value))) x;

SELECT 'check', 'ng_bucket',
       CASE WHEN count(*) = 3 AND count(*) FILTER (WHERE on_ng) = CASE WHEN :'rebuilt' THEN 0 ELSE 3 END
            THEN 'pass' ELSE 'fail' END,
       coalesce(string_agg(user_view_name || CASE WHEN on_ng THEN ' on time_bucket_ng' ELSE ' on time_bucket' END,
                           ', ' ORDER BY user_view_name), 'none found')
  FROM (SELECT user_view_name,
               pg_get_viewdef(to_regclass(format('%I.%I', direct_view_schema, direct_view_name)))
                 LIKE '%time_bucket_ng%' AS on_ng
          FROM _timescaledb_catalog.continuous_agg WHERE user_view_name LIKE 'fx\_ng\_%') a;

SELECT 'check', 'ng_kept',
       CASE WHEN policy AND comment = 'monthly, Madrid time' AND owner = 'fx_ng_owner' AND reader
            AND realtime THEN 'pass' ELSE 'fail' END,
       format('policy %s, comment %s, owner %s, reader %s, real-time %s', policy, comment, owner, reader, realtime)
  FROM (SELECT
          EXISTS (SELECT 1 FROM _timescaledb_config.bgw_job j JOIN _timescaledb_catalog.continuous_agg c
                    ON c.mat_hypertable_id = j.hypertable_id
                   WHERE c.user_view_name = 'fx_ng_month' AND j.proc_name = 'policy_refresh_continuous_aggregate'
                     AND j.schedule_interval = INTERVAL '1 day'
                     AND j.config->>'start_offset' = '1 year' AND j.config->>'end_offset' = '1 day') AS policy,
          obj_description('fx_ng_month'::regclass, 'pg_class') AS comment,
          (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'fx_ng_week'::regclass) AS owner,
          has_table_privilege('fx_ng_reader', 'fx_ng_day', 'SELECT') AS reader,
          (SELECT bool_and(NOT materialized_only) FROM _timescaledb_catalog.continuous_agg
            WHERE user_view_name LIKE 'fx\_ng\_%') AS realtime) k;

-- Read from the materialization: the real-time view shows the new row
-- whether or not the refresh worked.
\if :rebuilt
INSERT INTO fx_ng_local VALUES (TIMESTAMP '2026-03-10 12:00:00', 1);
CALL refresh_continuous_aggregate('fx_ng_day', '2026-03-01', '2026-04-01');
\set refreshed :ERROR
SELECT format('%I.%I', h.schema_name, h.table_name) AS mat
  FROM _timescaledb_catalog.continuous_agg c
  JOIN _timescaledb_catalog.hypertable h ON h.id = c.mat_hypertable_id
 WHERE c.user_view_name = 'fx_ng_day' \gset
SELECT 'check', 'ng_refresh',
       CASE WHEN count(*) = 1 AND NOT :refreshed THEN 'pass' ELSE 'fail' END,
       format('2026-03-10 has %s materialized row(s) after a refresh', count(*))
  FROM :mat WHERE bucket = TIMESTAMP '2026-03-10 00:00:00';
\endif
