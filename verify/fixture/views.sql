-- Optional fixture layer: one user view on every timescaledb_information view.
--
-- Not part of the default seed. Most update scripts drop the information
-- views, and a user view on top blocks them with a dependency error, so in
-- the default seed this would block nearly every row. It exists to exercise
-- the information_views gate: preflight should refuse exactly the updates
-- whose scripts drop a view this layer depends on.
--
-- Run with psql -X after seed.sql (or on an empty database), at the version
-- the update starts from.

\set ON_ERROR_STOP on

DO $$
DECLARE
  v record;
BEGIN
  FOR v IN SELECT c.relname
             FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace
            WHERE s.nspname = 'timescaledb_information' AND c.relkind = 'v'
            ORDER BY 1 LOOP
    EXECUTE format('CREATE VIEW public.%I AS SELECT * FROM timescaledb_information.%I',
                   'fx_v_' || v.relname, v.relname);
  END LOOP;
END $$;

SELECT 'views' AS layer, count(*) AS created
  FROM pg_class WHERE relname LIKE 'fx\_v\_%' AND relkind = 'v';
