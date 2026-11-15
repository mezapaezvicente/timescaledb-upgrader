"""The migration step against real TimescaleDB: an aggregate created at 2.1.1,
updated to 2.15.3, in a plain container. Skips without docker or the image.

What would hurt if wrong: what the fake in ``test_cagg_migration.py`` assumes.
That the report reads an old catalog, that the call clears the gate and the data
is intact, that grants come back, and that a refused call leaves things as said.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tsupgrade.apply import ApplyOptions, _step_migrate_caggs
from tsupgrade.gates import BLOCKED, CLEAR, check_old_caggs, old_caggs
from tsupgrade.steps import MigrateCaggs

TAG = "timescale/timescaledb-ha:pg13.22-ts2.15.3"
SEED = Path(__file__).resolve().parents[1] / "verify" / "fixture" / "seed.sql"
CHECK = SEED.parent / "check.sql"


def _available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", TAG], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _available(), reason="needs docker and " + TAG)


class Box:
    def __init__(self, name):
        self.name = name

    def psql(self, database, sql=None, stdin=None, *flags):
        args = ["docker", "exec", "-i", self.name, "psql", "-U", "postgres", "-X", "-qtA", "-F", "|",
                "-v", "ON_ERROR_STOP=1", "-d", database] + list(flags) + (["-c", sql] if sql else [])
        done = subprocess.run(args, input=stdin.encode("utf-8") if stdin else None, capture_output=True)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def exec(self, pod, container, argv):
        done = subprocess.run(["docker", "exec", "-i", self.name] + argv, capture_output=True)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def database(self, name, view=False, grant=True):
        assert self.psql("postgres", "CREATE DATABASE " + name)[0] == 0
        self.psql(name, "DROP EXTENSION IF EXISTS timescaledb CASCADE")
        assert self.psql(name, "CREATE EXTENSION timescaledb VERSION '2.1.1'")[0] == 0
        rc, _, err = self.psql(name, stdin=SEED.read_text(encoding="utf-8"))
        assert rc == 0, err
        if grant:
            self.psql(name, "DO $$ BEGIN CREATE ROLE reader; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            assert self.psql(name, "GRANT SELECT ON fx_hourly TO reader")[0] == 0
        if view:
            assert self.psql(name, "CREATE VIEW fx_depends AS SELECT device FROM fx_hourly")[0] == 0


@pytest.fixture(scope="module")
def box():
    name = "tsu-cagg-" + uuid.uuid4().hex[:8]
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=postgres", TAG],
                   check=True, capture_output=True)
    try:
        for _ in range(180):
            logs = subprocess.run(["docker", "logs", name], capture_output=True).stderr.decode("utf-8", "replace")
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-q"]).returncode == 0
            if ready and "init process complete" in logs + subprocess.run(
                    ["docker", "logs", name], capture_output=True).stdout.decode("utf-8", "replace"):
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


def test_the_report_reads_the_oldest_catalog(box):
    box.database("oldest")
    found = old_caggs(_runner(box), "oldest", "2.1.1")
    assert [c.name for c in found] == ["public.fx_hourly"]
    assert found[0].built_on == "public.fx_metrics" and found[0].size_bytes > 0 and found[0].chunks > 0
    assert "reader:SELECT" in found[0].grants


def test_the_step_clears_the_gate_and_keeps_data_and_grants(box):
    box.database("migrate")
    assert box.psql("migrate", "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")[0] == 0
    assert check_old_caggs(_runner(box), "migrate", "2.15.3", "2.27.2").status == BLOCKED
    outcome = _step_migrate_caggs(MigrateCaggs(hop=4, expect="2.15.3"), Cluster(box), ["migrate"])
    assert outcome.status != "failed", outcome.output
    assert check_old_caggs(_runner(box), "migrate", "2.15.3", "2.27.2").status == CLEAR
    assert box.psql("migrate", "SELECT has_table_privilege('reader', 'public.fx_hourly', 'SELECT')")[1].strip() == "t"
    rc, out, err = box.psql("migrate", None, CHECK.read_text(encoding="utf-8").replace("\\set ON_ERROR_STOP off", ""))
    assert rc == 0, err
    lines = [l.split("|") for l in out.splitlines() if l.startswith("check|")]
    assert len(lines) >= 7 and all(l[2] == "pass" for l in lines), lines


def test_an_aggregate_with_no_views_and_no_grants_is_read_through_stripped_output(box):
    """As kubectl hands it over: this is what got past the check on pg11."""
    box.database("bare", grant=False)
    def runner(database, sql):
        rc, out, err = box.psql(database, sql)
        return rc, out.strip(), err.strip()

    found = old_caggs(runner, "bare", "2.1.1")
    assert [c.name for c in found] == ["public.fx_hourly"] and found[0].grants == ()


def test_a_view_on_the_aggregate_is_found_at_the_oldest_release(box):
    box.database("viewed", view=True)
    assert old_caggs(_runner(box), "viewed", "2.1.1")[0].dependents == ("public.fx_depends",)


@pytest.mark.parametrize("kind", ["VIEW", "MATERIALIZED VIEW"])
def test_the_view_advice_works_as_written(box, kind):
    """What the refusal tells the operator to run: save, drop, migrate, load."""
    name = "advice_" + kind.split()[0].lower()
    box.database(name)
    assert box.psql(name, "CREATE {0} fx_depends AS SELECT device, count(*) AS n FROM fx_hourly GROUP BY device".format(kind))[0] == 0
    assert box.psql(name, "GRANT SELECT ON fx_depends TO reader")[0] == 0
    assert box.psql(name, "COMMENT ON {0} fx_depends IS 'kept'".format(kind))[0] == 0
    before = box.psql(name, "SELECT * FROM fx_depends ORDER BY 1")[1]
    assert box.psql(name, "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")[0] == 0

    rc, saved, err = box.exec(None, None, ["pg_dump", "-U", "postgres", "-d", name, "--schema-only",
                                           "-t", '"public"."fx_depends"'])
    assert rc == 0 and "fx_depends" in saved, err
    assert box.psql(name, 'DROP {0} "public"."fx_depends"'.format(kind))[0] == 0
    outcome = _step_migrate_caggs(MigrateCaggs(hop=4, expect="2.15.3"), Cluster(box), [name])
    assert outcome.status != "failed", outcome.output
    rc, _, err = box.psql(name, None, saved)
    assert rc == 0, err
    if kind != "VIEW":
        assert box.psql(name, "REFRESH MATERIALIZED VIEW public.fx_depends")[0] == 0
    assert box.psql(name, "SELECT * FROM fx_depends ORDER BY 1")[1] == before
    assert box.psql(name, "SELECT has_table_privilege('reader', 'public.fx_depends', 'SELECT')")[1].strip() == "t"
    assert box.psql(name, "SELECT obj_description('public.fx_depends'::regclass)")[1].strip() == "kept"



# --- R30: beyond one aggregate per database ---------------------------------------
# Measured overnight 2026-11-11 on this image; OVERNIGHT-BLOCKERS-3.md has the
# transcripts. Each case is an aggregate created at 2.1.1 (the old format),
# updated to 2.15.3 and migrated by apply's own step.

DAILY = ("CREATE MATERIALIZED VIEW fx_daily WITH (timescaledb.continuous) AS "
         "SELECT time_bucket(INTERVAL '1 day', time) AS day, device, max(value) AS top "
         "FROM fx_metrics GROUP BY 1, 2 WITH NO DATA")
JOBS = ("SELECT proc_name || ' ' || schedule_interval || ' ' || coalesce(config - 'mat_hypertable_id' "
        "- 'hypertable_id', '{{}}')::text FROM timescaledb_information.jobs "
        "WHERE proc_name LIKE 'policy_%' AND hypertable_name = (SELECT materialization_hypertable_name "
        "FROM timescaledb_information.continuous_aggregates WHERE view_name = '{0}') ORDER BY 1")


def _migrate(box, name):
    assert box.psql(name, "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")[0] == 0
    outcome = _step_migrate_caggs(MigrateCaggs(hop=4, expect="2.15.3"), Cluster(box), [name])
    assert outcome.status != "failed", outcome.output
    assert check_old_caggs(_runner(box), name, "2.15.3", "2.27.2").status == CLEAR
    return outcome


def test_two_aggregates_in_one_database_both_migrate_with_their_data(box):
    box.database("two")
    assert box.psql("two", DAILY)[0] == 0
    assert box.psql("two", "CALL refresh_continuous_aggregate('fx_daily', NULL, NULL)")[0] == 0
    assert box.psql("two", "CREATE TABLE before_h AS SELECT * FROM fx_hourly")[0] == 0
    assert box.psql("two", "CREATE TABLE before_d AS SELECT * FROM fx_daily")[0] == 0
    assert sorted(c.name for c in old_caggs(_runner(box), "two", "2.1.1")) == [
        "public.fx_daily", "public.fx_hourly"]
    outcome = _migrate(box, "two")
    assert "migrated 2 continuous aggregate(s)" in outcome.summary
    # max() and count() come back exactly; avg() differs in the last bits
    # (measured: at most 2.8e-14), summed in another order by the new format.
    same = ("SELECT count(*) FROM {0} a FULL JOIN {1} b USING ({2}, device) "
            "WHERE a.device IS NULL OR b.device IS NULL OR {3}")
    assert box.psql("two", same.format("fx_daily", "before_d", "day", "a.top <> b.top"))[1].strip() == "0"
    assert box.psql("two", same.format("fx_hourly", "before_h", "bucket",
                                        "a.n <> b.n OR abs(a.avg_value - b.avg_value) > 1e-9"))[1].strip() == "0"


def test_no_aggregate_can_be_built_on_an_old_format_one(box):
    """Hierarchical aggregates arrive in 2.9, after the old format stopped
    being created (2.7), and refuse an old-format base: the case cannot arise."""
    box.database("tiered")
    on_hourly = ("CREATE MATERIALIZED VIEW fx_2h WITH (timescaledb.continuous) AS SELECT "
                 "time_bucket(INTERVAL '2 hours', bucket) AS b2, device, sum(n) AS n FROM fx_hourly "
                 "GROUP BY 1, 2 WITH NO DATA")
    rc, _, err = box.psql("tiered", on_hourly)
    assert rc != 0 and "invalid continuous aggregate view" in err
    assert box.psql("tiered", "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")[0] == 0
    rc, _, err = box.psql("tiered", on_hourly)
    assert rc != 0 and "old format of continuous aggregate is not supported" in err


def test_compression_and_its_policy_survive_but_compressed_chunks_come_back_uncompressed(box):
    box.database("squeezed")
    assert box.psql("squeezed", "SELECT add_continuous_aggregate_policy('fx_hourly', start_offset => "
                                "INTERVAL '30 days', end_offset => INTERVAL '1 hour', schedule_interval "
                                "=> INTERVAL '1 hour')")[0] == 0
    assert box.psql("squeezed", "ALTER EXTENSION timescaledb UPDATE TO '2.15.3'")[0] == 0
    # Compression on an aggregate arrives in 2.6; the old format accepts it.
    assert box.psql("squeezed", "ALTER MATERIALIZED VIEW fx_hourly SET (timescaledb.compress = true)")[0] == 0
    assert box.psql("squeezed", "SELECT add_compression_policy('fx_hourly', compress_after => "
                                "INTERVAL '45 days')")[0] == 0
    assert box.psql("squeezed", "SELECT count(*) FROM (SELECT compress_chunk(c) FROM "
                                "show_chunks('fx_hourly') c) s")[1].strip() == "2"
    before = box.psql("squeezed", JOBS.format("fx_hourly"))[1]
    outcome = _step_migrate_caggs(MigrateCaggs(hop=4, expect="2.15.3"), Cluster(box), ["squeezed"])
    assert outcome.status != "failed", outcome.output
    assert box.psql("squeezed", JOBS.format("fx_hourly"))[1] == before
    assert "policy_compression" in before and '"compress_after": "45 days"' in before
    assert box.psql("squeezed", "SELECT compression_enabled FROM timescaledb_information."
                                "continuous_aggregates WHERE view_name = 'fx_hourly'")[1].strip() == "t"
    assert box.psql("squeezed", "SELECT count(*) FILTER (WHERE is_compressed) || '/' || count(*) FROM "
                                "timescaledb_information.chunks WHERE hypertable_name = (SELECT "
                                "materialization_hypertable_name FROM timescaledb_information."
                                "continuous_aggregates WHERE view_name = 'fx_hourly')")[1].strip() == "0/2"


def test_a_refresh_policy_with_its_own_settings_survives_under_a_new_job(box):
    box.database("policy")
    assert box.psql("policy", "SELECT add_continuous_aggregate_policy('fx_hourly', start_offset => "
                              "INTERVAL '30 days', end_offset => INTERVAL '2 hours', schedule_interval "
                              "=> INTERVAL '15 minutes')")[0] == 0
    ids = "SELECT string_agg(job_id::text, ',') FROM timescaledb_information.jobs WHERE proc_name = " \
          "'policy_refresh_continuous_aggregate'"
    old_id = box.psql("policy", ids)[1].strip()
    before = box.psql("policy", JOBS.format("fx_hourly"))[1]
    assert before.strip() == ('policy_refresh_continuous_aggregate 00:15:00 '
                              '{"end_offset": "02:00:00", "start_offset": "30 days"}')
    _migrate(box, "policy")
    assert box.psql("policy", JOBS.format("fx_hourly"))[1] == before
    assert box.psql("policy", ids)[1].strip() not in ("", old_id)
