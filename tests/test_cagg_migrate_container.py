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

    def database(self, name, view=False):
        assert self.psql("postgres", "CREATE DATABASE " + name)[0] == 0
        self.psql(name, "DROP EXTENSION IF EXISTS timescaledb CASCADE")
        assert self.psql(name, "CREATE EXTENSION timescaledb VERSION '2.1.1'")[0] == 0
        rc, _, err = self.psql(name, stdin=SEED.read_text(encoding="utf-8"))
        assert rc == 0, err
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


def test_a_view_on_the_aggregate_is_found_at_the_oldest_release(box):
    box.database("viewed", view=True)
    assert old_caggs(_runner(box), "viewed", "2.1.1")[0].dependents == ("public.fx_depends",)
