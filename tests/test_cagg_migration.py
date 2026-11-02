"""Tests for stopping on old continuous aggregates, and migrating them.

What would hurt if wrong: changing anything before the operator has seen what
was found, migrating without being told to, offering a migration known to fail
partway, or a call that leaves the old aggregate there (TimescaleDB's own hint
does). The migration's real behaviour is in ``test_cagg_migrate_container.py``.
"""

from __future__ import annotations

import pytest

from tsupgrade.apply import (
    ApplyOptions,
    ApplyRefused,
    _REFRESH_AFTER,
    _plan_cagg_migration,
    _step_migrate_caggs,
    _size,
)
from tsupgrade.gates import OldCagg, old_caggs
from tsupgrade.preflight import Instance
from tsupgrade.steps import (
    EnsureImage,
    MajorUpgrade,
    MigrateCaggs,
    Preflight,
    UpdateExtension,
    VerifyVersion,
)

CAGG = "\x1f".join(["public.fx_hourly", "public.fx_metrics", "311296", "2", "1", "", "reader:SELECT"])
WITH_VIEW = "\x1f".join(["public.fx_hourly", "public.fx_metrics", "311296", "2", "1", "public.fx_depends", ""])


def _route():
    """pg11 at 2.1.1 to pg15 at 2.27.2, as the plan lays it out."""
    return [
        Preflight(hop=1, image="i:12"),
        MajorUpgrade(hop=1, image="i:12a", from_pg=11, to_pg=12, ts="2.1.1", run_image="i:12", link=False),
        VerifyVersion(hop=1, expect="2.1.1"),
        Preflight(hop=2, image="i:12"),
        UpdateExtension(hop=2, to="2.11.2", sql="x"),
        VerifyVersion(hop=2, expect="2.11.2"),
        Preflight(hop=4, image="i:13"),
        UpdateExtension(hop=4, to="2.15.3", sql="x"),
        VerifyVersion(hop=4, expect="2.15.3"),
        Preflight(hop=6, image="i:15"),
        EnsureImage(hop=6, image="i:15"),
        UpdateExtension(hop=6, to="2.27.2", sql="x"),
        VerifyVersion(hop=6, expect="2.27.2"),
    ]


class Cluster:
    def __init__(self, aggregates=CAGG, ng=""):
        self.aggregates, self.ng = aggregates, ng
        self.sql = []
        self.writes = []

    def psql(self, pod, database, sql):
        self.sql.append(sql)
        if "information_schema.columns" in sql:
            exists = "'finalized'" in sql and False or "bucket_func" in sql
            return 0, "t" if exists else "f", ""
        if "time_bucket_ng" in sql:
            return 0, self.ng, ""
        if "FROM _timescaledb_catalog.continuous_agg" in sql:
            return 0, self.aggregates, ""
        raise AssertionError("not a read-only question: " + sql)


PRIMARY = Instance(pod="tsdb-0", role="primary", extensions={"verify": "2.1.1"})


def _decide(steps, cluster=None, source="2.1.1", **opts):
    _plan_cagg_migration(steps, {"source": {"pg": "11.11", "ts": source}}, cluster or Cluster(),
                         [PRIMARY], ["verify"], ApplyOptions(**opts))
    return steps


def _refusal(steps=None, cluster=None, **opts):
    with pytest.raises(ApplyRefused) as refused:
        _decide(steps or _route(), cluster, **opts)
    assert refused.value.plain
    return "\n".join(refused.value.reasons)


def test_it_stops_before_anything_and_says_what_it_found_and_where_from():
    text = _refusal()
    assert "read-only check" in text and "Nothing has been changed" in text
    for shown in ("verify: public.fx_hourly", "built on public.fx_metrics", "304.0 KiB", "2 chunk(s)",
                  "1 refresh or other job(s)", "views that depend on it: none", "reader:SELECT"):
        assert shown in text
    assert "Hop 6 of this plan updates to 2.27.2" in text and "--migrate-caggs" in text


def test_everything_it_asks_before_stopping_is_read_only():
    cluster = Cluster()
    _refusal(cluster=cluster)
    for sql in cluster.sql:
        assert sql.lstrip().upper().startswith("SELECT")
        assert "CALL" not in sql.upper().split("FROM")[0]


def test_it_says_what_agreeing_means_before_asking():
    asked = []
    _decide(_route(), confirm=lambda text: asked.append(text) or True)
    text = asked[0]
    assert "override => true, drop_old => true" in text and "cannot be undone" in text
    assert "at hop 4, when the database reaches 2.15.3" in text
    assert "backup" in text and "grants" in text


def test_no_means_it_stops():
    assert "--migrate-caggs" in _refusal(confirm=lambda text: False)


def test_the_flag_needs_no_question():
    def never(text):
        raise AssertionError("asked")

    assert any(isinstance(s, MigrateCaggs) for s in _decide(_route(), migrate_caggs=True, confirm=never))


def test_agreeing_adds_the_step_at_the_first_stop_it_is_known_to_work():
    """2.11.2 is on the way but cagg_migrate has only been run from 2.14."""
    steps = _decide(_route(), migrate_caggs=True)
    i = next(i for i, s in enumerate(steps) if isinstance(s, MigrateCaggs))
    assert steps[i] == MigrateCaggs(hop=4, expect="2.15.3")
    assert steps[i - 1] == VerifyVersion(hop=4, expect="2.15.3")
    assert steps.index(next(s for s in steps if isinstance(s, UpdateExtension) and s.to == "2.27.2")) > i


def test_a_source_already_in_the_window_migrates_after_the_first_preflight():
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.27.2", sql="x"),
             VerifyVersion(hop=1, expect="2.27.2")]
    primary = Instance(pod="tsdb-0", role="primary", extensions={"verify": "2.19.3"})
    _plan_cagg_migration(steps, {"source": {"ts": "2.19.3"}}, Cluster(), [primary], ["verify"],
                         ApplyOptions(migrate_caggs=True))
    assert steps[1] == MigrateCaggs(hop=1, expect="2.19.3")


@pytest.mark.parametrize("cluster,said", [
    (Cluster(aggregates=WITH_VIEW), "Drop them first and recreate them afterwards: public.fx_depends"),
    (Cluster(ng="public.fx_ng"), "public.fx_ng"),
])
def test_what_would_fail_stops_even_with_the_flag(cluster, said):
    """Measured with a view: the call fails partway and cannot be retried."""
    text = _refusal(cluster=cluster, migrate_caggs=True)
    assert said in text and "read-only check" in text


def test_a_route_with_no_stop_in_the_window_stops_even_with_the_flag():
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.27.2", sql="x")]
    assert "no release from 2.14 to 2.24" in _refusal(steps, migrate_caggs=True)


def test_nothing_old_changes_nothing():
    steps = _route()
    assert _decide(list(steps), Cluster(aggregates="")) == steps


def test_a_route_below_2_25_is_not_stopped_and_not_even_asked():
    steps = _route()[:9]
    cluster = Cluster()
    assert _decide(list(steps), cluster) == steps and cluster.sql == []


def test_a_question_that_fails_does_not_stop_it_and_is_told():
    """The gate at the hop still stops it; a guess here would be worse."""
    told = []
    cluster = Cluster()
    cluster.psql = lambda *a: (1, "", "connection lost")
    steps = _route()
    assert _decide(list(steps), cluster, notify=told.append) == steps
    assert "could not establish" in told[0]


# --- the step -----------------------------------------------------------------

class MigrateKube:
    def __init__(self, fail=None, warning=None):
        self.ran, self.fail, self.left = [], fail, True
        self.warning = warning or ('WARNING:  refresh the continuous aggregate after the migration executing '
                                   'this statement: "CALL public.refresh_continuous_aggregate(\'public.fx_hourly\', '
                                   "CAST('2026-01-08 00:00:00+00' AS timestamp with time zone), NULL);\"")

    def exec(self, pod, container, argv):
        sql = argv[-1]
        self.ran.append(sql)
        if self.fail and self.fail in sql:
            return 1, "", "ERROR:  it broke"
        if "information_schema.columns" in sql:
            return 0, "t", ""
        if sql.startswith("CALL cagg_migrate"):
            self.left = False
            return 0, "", self.warning
        if "aclexplode" in sql and sql.startswith("SELECT format('GRANT"):
            return 0, "GRANT SELECT ON fx_hourly TO reader\n", ""
        if "pg_get_userbyid" in sql:
            return 0, "alice\n", ""
        if "FROM _timescaledb_catalog.continuous_agg" in sql:
            return 0, CAGG if self.left else "", ""
        return 0, "", ""


class StepCluster:
    def __init__(self, kube, primary="tsdb-0"):
        self.kube, self._primary = kube, primary
        self.opts = ApplyOptions()

    def primary(self):
        return self._primary


def _migrate(kube):
    return _step_migrate_caggs(MigrateCaggs(hop=4, expect="2.15.3"), StepCluster(kube), ["verify"])


def test_the_call_replaces_the_aggregate_refreshes_and_restores_what_it_drops():
    kube = MigrateKube()
    outcome = _migrate(kube)
    call = next(i for i, s in enumerate(kube.ran) if s.startswith("CALL cagg_migrate"))
    assert kube.ran[call] == "CALL cagg_migrate('public.fx_hourly', override => true, drop_old => true)"
    after = kube.ran[call + 1:]
    assert after[0].startswith("CALL public.refresh_continuous_aggregate('public.fx_hourly', CAST(")
    assert "ALTER MATERIALIZED VIEW public.fx_hourly OWNER TO alice" in after
    assert "GRANT SELECT ON fx_hourly TO reader" in after
    assert "migrated 1 continuous aggregate(s) at 2.15.3" in outcome.summary


def test_grants_and_owner_are_read_before_the_call_drops_them():
    kube = MigrateKube()
    _migrate(kube)
    call = next(i for i, s in enumerate(kube.ran) if s.startswith("CALL cagg_migrate"))
    assert any("aclexplode" in s for s in kube.ran[:call]) and any("pg_get_userbyid" in s for s in kube.ran[:call])


def test_a_failed_call_says_it_may_be_half_done():
    with pytest.raises(Exception) as failed:
        _migrate(MigrateKube(fail="CALL cagg_migrate"))
    text = "\n".join(failed.value.output)
    assert "it broke" in text and "_old" in text and "continuous_agg_migrate_plan" in text


def test_an_aggregate_still_old_afterwards_fails_the_step():
    kube = MigrateKube()
    original = kube.exec

    def stays(pod, container, argv):
        result = original(pod, container, argv)
        kube.left = True
        return result

    kube.exec = stays
    with pytest.raises(Exception, match="still holds old-format"):
        _migrate(kube)


def test_only_a_refresh_statement_of_the_expected_shape_is_ever_run():
    """It comes from a server message; nothing else in one may be executed."""
    assert _REFRESH_AFTER.search(MigrateKube().warning)
    for bad in ('executing this statement: "DROP TABLE x;"',
                'executing this statement: "CALL public.refresh_continuous_aggregate(\'x\', NULL, NULL); DROP TABLE y;"'):
        assert _REFRESH_AFTER.search(bad) is None


def test_old_caggs_reads_the_report():
    def runner(database, sql):
        if "information_schema.columns" in sql:
            return 0, "t", ""
        assert sql.endswith("WHERE c.finalized IS FALSE")
        return 0, CAGG, ""

    assert old_caggs(runner, "verify", "2.15.3") == [OldCagg(
        "verify", "public.fx_hourly", "public.fx_metrics", 311296, 2, 1, (), ("reader:SELECT",))]


@pytest.mark.parametrize("size,text", [(512, "512 bytes"), (311296, "304.0 KiB"), (5 * 1024 ** 3, "5.0 GiB")])
def test_sizes_read_as_sizes(size, text):
    assert _size(size) == text


def test_the_flag_exists_on_apply():
    from tsupgrade import cli
    parser = cli.build_parser()
    assert parser.parse_args(["apply", "p", "--migrate-caggs"]).migrate_caggs is True
    assert parser.parse_args(["apply", "p"]).migrate_caggs is False
