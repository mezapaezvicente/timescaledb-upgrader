"""Aggregates on time_bucket_ng against real TimescaleDB, in a plain container:
created at 2.6.1, 2.8.0 and 2.12.2, rebuilt, then updated to 2.15.3. Skips
without docker or the image.

What would hurt if wrong: what ``test_ng_rebuild.py`` assumes. That the gate
finds every one (the old one missed a fixed width without a timezone) and the
update really fails on it, that the rebuild keeps every row and puts back what
it drops, that the update then goes through, and that whatever it refuses is
left exactly as it was.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from tsupgrade.apply import ApplyOptions, _step_rebuild_ng_caggs
from tsupgrade.gates import BLOCKED, CLEAR, check_time_bucket_ng, old_caggs, time_bucket_ng_caggs
from tsupgrade.ngrebuild import RebuildFailed, preview, rebuild
from tsupgrade.steps import RebuildNgCaggs

TAG = "timescale/timescaledb-ha:pg13.22-ts2.15.3"
NG = "timescaledb_experimental.time_bucket_ng"

# name -> (table, bucket expression, rebuilt or not). Measured: these are the
# shapes an aggregate takes time_bucket_ng in, and what time_bucket can match
# at 2.14 and below.
SHAPES = {
    "month_madrid": ("m_tz", NG + "('1 month', ts, 'Europe/Madrid')", True),
    "day_utc": ("m_tz", NG + "('1 day', ts, 'UTC')", True),
    "origin_ignored": ("m_tz", NG + "('1 day', ts, '2000-01-01 06:00+00'::timestamptz, 'UTC')", True),
    "week_madrid": ("m_tz", NG + "('7 days', ts, 'Europe/Madrid')", True),
    "ts_day": ("m_ts", NG + "('1 day', ts)", True),
    "ts_month": ("m_ts", NG + "('1 month', ts)", True),
    "date_month": ("m_date", NG + "('1 month', d)", True),
    "ts_week": ("m_ts", NG + "('7 days', ts)", False),
}
TABLES = """
CREATE TABLE m_tz (ts timestamptz NOT NULL, v double precision);
SELECT create_hypertable('m_tz', 'ts', chunk_time_interval => interval '7 days');
INSERT INTO m_tz SELECT t, extract(epoch FROM t)::bigint % 97
  FROM generate_series('2024-01-01 00:00+00'::timestamptz, '2024-03-31 23:00+00', '1 hour') t;
CREATE TABLE m_ts (ts timestamp NOT NULL, v double precision);
SELECT create_hypertable('m_ts', 'ts', chunk_time_interval => interval '7 days');
INSERT INTO m_ts SELECT t, extract(epoch FROM t)::bigint % 89
  FROM generate_series('2024-01-01 00:00'::timestamp, '2024-03-31 23:00', '1 hour') t;
CREATE TABLE m_date (d date NOT NULL, v double precision);
SELECT create_hypertable('m_date', 'd', chunk_time_interval => interval '30 days');
INSERT INTO m_date SELECT t::date, extract(epoch FROM t)::bigint % 83
  FROM generate_series('2023-01-01'::timestamp, '2024-03-31', '1 day') t;
DO $$ BEGIN CREATE ROLE reader; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE ROLE owner_r; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
GRANT SELECT ON m_tz TO owner_r;
"""


def _available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", TAG], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _available(), reason="needs docker and " + TAG)


class Box:
    def __init__(self, name):
        self.name = name

    def psql(self, database, sql=None, stdin=None):
        args = ["docker", "exec", "-i", self.name, "psql", "-U", "postgres", "-X", "-qtA", "-F", "|",
                "-v", "ON_ERROR_STOP=1", "-d", database] + (["-c", sql] if sql else [])
        done = subprocess.run(args, input=stdin.encode("utf-8") if stdin else None, capture_output=True)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def exec(self, pod, container, argv):
        done = subprocess.run(["docker", "exec", "-i", self.name] + argv, capture_output=True)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def one(self, database, sql):
        rc, out, err = self.psql(database, sql)
        assert rc == 0, err
        return out.strip()

    def database(self, name, version, shapes=SHAPES):
        assert self.psql("postgres", "CREATE DATABASE " + name)[0] == 0
        self.psql(name, "DROP EXTENSION IF EXISTS timescaledb CASCADE")
        assert self.psql(name, "CREATE EXTENSION timescaledb VERSION '{0}'".format(version))[0] == 0
        rc, _, err = self.psql(name, stdin=TABLES)
        assert rc == 0, err
        for agg in shapes:
            table, bucket, _ = SHAPES[agg]
            rc, _, err = self.psql(name, (
                "CREATE MATERIALIZED VIEW {0} WITH (timescaledb.continuous, timescaledb.materialized_only = false) "
                "AS SELECT {1} AS b, avg(v) AS a, count(*) AS n FROM {2} GROUP BY 1 WITH NO DATA").format(
                    agg, bucket, table))
            assert rc == 0, (agg, version, err)
            self.one(name, "CALL refresh_continuous_aggregate('{0}', '2023-01-01', '2024-03-01')".format(agg))

    def rows(self, database, agg):
        return self.one(database, "SELECT count(*) || ' ' || md5(string_agg(t::text, ';' ORDER BY t::text)) "
                                  "FROM {0} t".format(agg))


@pytest.fixture(scope="module")
def box():
    name = "tsu-ng-" + uuid.uuid4().hex[:8]
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=postgres", TAG],
                   check=True, capture_output=True)
    try:
        for _ in range(180):
            logs = subprocess.run(["docker", "logs", name], capture_output=True)
            text = logs.stdout.decode("utf-8", "replace") + logs.stderr.decode("utf-8", "replace")
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-q"]).returncode == 0
            if ready and "init process complete" in text:
                break
            subprocess.run(["docker", "exec", name, "sleep", "1"])
        yield Box(name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _runner(box):
    return lambda database, sql: box.psql(database, sql)


class Cluster:
    def __init__(self, box):
        self.kube, self.opts = box, ApplyOptions()

    def primary(self):
        return "tsdb-0"


def test_the_gate_finds_what_the_update_fails_on(box):
    """ts_day has no row in continuous_aggs_bucket_function at 2.12.2."""
    box.database("found", "2.12.2", ["ts_day"])
    assert box.one("found", "SELECT count(*) FROM _timescaledb_catalog.continuous_aggs_bucket_function") == "0"
    assert [c.name for c in time_bucket_ng_caggs(_runner(box), "found")] == ["public.ts_day"]
    assert check_time_bucket_ng(_runner(box), "found", "2.12.2", "2.15.3").status == BLOCKED
    rc, _, err = box.psql("found", "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")
    assert rc != 0 and "catalog_get when extension isn't loaded" in err
    assert box.one("found", "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'") == "2.12.2"


@pytest.mark.parametrize("version", ["2.8.0", "2.12.2"])
def test_the_step_rebuilds_keeps_every_row_and_the_update_then_goes_through(box, version):
    name = "step_" + version.replace(".", "")
    keep = [a for a, (_, _, ok) in SHAPES.items() if ok]
    box.database(name, version, keep)
    box.one(name, "SELECT add_continuous_aggregate_policy('month_madrid', start_offset => interval '6 months', "
                  "end_offset => interval '1 day', schedule_interval => interval '2 hours')")
    box.one(name, "ALTER MATERIALIZED VIEW month_madrid OWNER TO owner_r")
    box.one(name, "GRANT SELECT ON month_madrid, day_utc TO reader")
    box.one(name, "COMMENT ON VIEW month_madrid IS 'monthly; it''s kept'")
    before = {agg: box.rows(name, agg) for agg in keep}
    previews = {c.name: preview(_runner(box), name, c.name) for c in time_bucket_ng_caggs(_runner(box), name)}

    outcome = _step_rebuild_ng_caggs(RebuildNgCaggs(hop=1, expect=version), Cluster(box), [name])
    assert outcome.status != "failed", outcome.output
    assert "rebuilt {0} continuous aggregate(s)".format(len(keep)) in outcome.summary
    for agg in keep:
        assert box.rows(name, agg) == before[agg], agg
        assert "public.{0} on {1}".format(agg, previews["public." + agg]) in "\n".join(outcome.output)
    assert time_bucket_ng_caggs(_runner(box), name) == []
    assert box.one(name, "SELECT count(*) FROM pg_class WHERE relname LIKE '%tsu_rebuild%'") == "0"
    assert box.one(name, "SELECT pg_get_userbyid(relowner) || ' ' || relacl::text || ' ' || "
                         "obj_description(oid, 'pg_class') FROM pg_class WHERE oid = 'month_madrid'::regclass") == \
        "owner_r {owner_r=arwdDxt/owner_r,reader=r/owner_r} monthly; it's kept"
    assert box.one(name, "SELECT has_table_privilege('reader', 'day_utc', 'SELECT')") == "t"
    assert box.one(name, "SELECT j.schedule_interval || ' ' || (j.config->>'start_offset') || ' ' || "
                         "(j.config->>'end_offset') FROM _timescaledb_config.bgw_job j JOIN "
                         "_timescaledb_catalog.continuous_agg c ON c.mat_hypertable_id = j.hypertable_id "
                         "WHERE c.user_view_name = 'month_madrid'") == "02:00:00 6 mons 1 day"
    assert box.one(name, "SELECT bool_and(NOT materialized_only) FROM _timescaledb_catalog.continuous_agg") == "t"

    rc, _, err = box.psql(name, "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")
    assert rc == 0, err
    for agg in keep:
        assert box.rows(name, agg) == before[agg], agg
    box.one(name, "INSERT INTO m_tz VALUES ('2024-04-15 12:00+00', 5)")
    box.one(name, "CALL refresh_continuous_aggregate('day_utc', NULL, NULL)")
    assert box.one(name, "SELECT n FROM day_utc WHERE b = '2024-04-15 00:00+00'") == "1"


def test_an_old_format_aggregate_from_26_is_rebuilt_finalized(box):
    box.database("from26", "2.6.1", ["day_utc", "ts_day"])
    box.one("from26", "SELECT 1")
    assert box.psql("from26", "ALTER EXTENSION timescaledb UPDATE TO '2.12.2'")[0] == 0
    assert sorted(c.name for c in old_caggs(_runner(box), "from26", "2.12.2")) == ["public.day_utc", "public.ts_day"]
    before = {agg: box.rows("from26", agg) for agg in ("day_utc", "ts_day")}
    outcome = _step_rebuild_ng_caggs(RebuildNgCaggs(hop=1, expect="2.12.2"), Cluster(box), ["from26"])
    assert outcome.status != "failed", outcome.output
    assert old_caggs(_runner(box), "from26", "2.12.2") == []
    assert {agg: box.rows("from26", agg) for agg in before} == before
    assert box.psql("from26", "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")[0] == 0


def _left_as_it_was(box, database, agg, before):
    assert box.rows(database, agg) == before
    assert [c.name for c in time_bucket_ng_caggs(_runner(box), database)] == ["public." + agg]
    assert box.one(database, "SELECT count(*) FROM pg_class WHERE relname LIKE '%tsu_rebuild%'") == "0"


def test_no_matching_form_is_refused_by_the_preview_and_the_rebuild(box):
    box.database("weeks", "2.12.2", ["ts_week"])
    before = box.rows("weeks", "ts_week")
    with pytest.raises(RebuildFailed) as previewed:
        preview(_runner(box), "weeks", "public.ts_week")
    with pytest.raises(RebuildFailed) as refused:
        rebuild(_runner(box), "weeks", "public.ts_week")
    for exc in (previewed.value, refused.value):
        assert "no time_bucket form of public.ts_week buckets as it does" in exc.summary
        assert any("differs from time_bucket_ng" in line for line in exc.output)
    assert refused.value.summary.endswith("it is left as it was")
    _left_as_it_was(box, "weeks", "ts_week", before)


def test_history_the_raw_data_lost_is_refused(box):
    box.database("lost", "2.12.2", ["day_utc"])
    before = box.rows("lost", "day_utc")
    box.one("lost", "SELECT count(*) FROM drop_chunks('m_tz', older_than => '2024-01-20 00:00+00'::timestamptz)")
    with pytest.raises(RebuildFailed) as previewed:
        preview(_runner(box), "lost", "public.day_utc")
    with pytest.raises(RebuildFailed) as refused:
        rebuild(_runner(box), "lost", "public.day_utc")
    for exc in (previewed.value, refused.value):
        assert "the raw data no longer has" in exc.summary
    _left_as_it_was(box, "lost", "day_utc", before)


def test_an_index_of_its_own_is_refused_before_the_swap(box):
    box.database("indexed", "2.12.2", ["day_utc"])
    box.one("indexed", "CREATE INDEX day_utc_by_a ON day_utc (a)")
    before = box.rows("indexed", "day_utc")
    with pytest.raises(RebuildFailed) as refused:
        rebuild(_runner(box), "indexed", "public.day_utc")
    assert "indexes the rebuild would not create again" in refused.value.summary
    assert "day_utc_by_a" in refused.value.summary
    _left_as_it_was(box, "indexed", "day_utc", before)


def test_a_job_it_would_not_put_back_is_refused_before_anything(box):
    box.database("retained", "2.12.2", ["day_utc"])
    box.one("retained", "SELECT add_retention_policy('day_utc', interval '5 years')")
    before = box.rows("retained", "day_utc")
    with pytest.raises(RebuildFailed) as refused:
        rebuild(_runner(box), "retained", "public.day_utc")
    assert "policy_retention" in refused.value.summary
    _left_as_it_was(box, "retained", "day_utc", before)
