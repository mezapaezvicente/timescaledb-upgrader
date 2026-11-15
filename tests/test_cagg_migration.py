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

CAGG = "\x1f".join(["public.fx_hourly", "public.fx_metrics", "311296", "2", "1", "", "reader:SELECT", "end"])
WITH_VIEW = "\x1f".join(["public.fx_hourly", "public.fx_metrics", "311296", "2", "1", "public.fx_depends", "", "end"])
BARE = "\x1f".join(["public.fx_hourly", "public.fx_metrics", "311296", "2", "0", "", "", "end"])


def _route():
    """pg11 at 2.1.1 to pg15 at 2.27.2, as the plan lays it out."""
    return [
        Preflight(hop=1, image="i:12"),
        MajorUpgrade(hop=1, image="i:12a", from_pg=11, to_pg=12, ts="2.1.1", run_image="i:12", link=False),
        VerifyVersion(hop=1, expect="2.1.1"),
        Preflight(hop=2, image="i:12"),
        UpdateExtension(hop=2, to="2.11.2", sql="x"),
        VerifyVersion(hop=2, expect="2.11.2"),
        MajorUpgrade(hop=3, image="i:13a", from_pg=12, to_pg=13, ts="2.11.2", run_image="i:13", link=False),
        Preflight(hop=4, image="i:13"),
        UpdateExtension(hop=4, to="2.15.3", sql="x"),
        VerifyVersion(hop=4, expect="2.15.3"),
        MajorUpgrade(hop=5, image="i:15a", from_pg=13, to_pg=15, ts="2.15.3", run_image="i:15", link=False),
        Preflight(hop=6, image="i:15"),
        EnsureImage(hop=6, image="i:15"),
        UpdateExtension(hop=6, to="2.27.2", sql="x"),
        VerifyVersion(hop=6, expect="2.27.2"),
    ]


class Cluster:
    def __init__(self, aggregates=CAGG, ng="", kind="v"):
        self.aggregates, self.ng, self.kind = aggregates, ng, kind
        self.sql = []
        self.writes = []
        self.kube, self.opts = None, ApplyOptions()

    def psql(self, pod, database, sql):
        self.sql.append(sql)
        if sql.startswith("SELECT relkind FROM pg_class"):
            return 0, self.kind, ""
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
    assert text.startswith("apply did not start: TimescaleDB 2.25 and later refuse continuous "
                           "aggregates created before 2.7, and this plan")
    assert "Created before TimescaleDB 2.7 (non-finalized, the old format):" in text
    assert "this plan updates TimescaleDB to 2.27.2 on PostgreSQL 15" in text and "--migrate-caggs" in text
    assert text.endswith("This came from a read-only check. Nothing has been changed.")
    assert "hop" not in text.lower()


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
    assert "when TimescaleDB reaches 2.15.3 on PostgreSQL 13" in text
    assert text.endswith("Nothing has been changed.") and "hop" not in text.lower()
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


def test_what_would_fail_stops_even_with_the_flag():
    """Measured with a view: the call fails partway and cannot be retried.
    What stops a rebuild on time_bucket_ng is in test_ng_rebuild.py."""
    text = _refusal(cluster=Cluster(aggregates=WITH_VIEW), migrate_caggs=True)
    assert "verify: public.fx_depends, on public.fx_hourly" in text
    assert text.endswith("This came from a read-only check. Nothing has been changed.")


def test_a_dependent_view_comes_with_what_to_run_for_it():
    text = _refusal(cluster=Cluster(aggregates=WITH_VIEW), migrate_caggs=True)
    exec_ = "kubectl -n timescale exec -i tsdb-0 -c timescaledb --"
    assert exec_ + " pg_dump -U postgres -d verify --schema-only -t '\"public\".\"fx_depends\"' > verify.public.fx_depends.sql" in text
    assert exec_ + " psql -U postgres -d verify -c 'DROP VIEW \"public\".\"fx_depends\"'" in text
    assert exec_ + " psql -U postgres -d verify -v ON_ERROR_STOP=1 < verify.public.fx_depends.sql" in text
    assert "REFRESH" not in text and "postgresql.org/docs/current/sql-dropview.html" in text


def test_a_dependent_materialized_view_is_dropped_and_refreshed_as_one():
    text = _refusal(cluster=Cluster(aggregates=WITH_VIEW, kind="m"), migrate_caggs=True)
    assert "public.fx_depends (materialized)" in text
    assert "DROP MATERIALIZED VIEW" in text and "REFRESH MATERIALIZED VIEW" in text


def test_the_commands_reach_the_cluster_apply_reached():
    cluster = Cluster(aggregates=WITH_VIEW)
    cluster.opts = ApplyOptions(context="kind-prod", namespace="db", container="pg")
    assert "kubectl --context kind-prod -n db exec -i tsdb-0 -c pg --" in _refusal(
        cluster=cluster, migrate_caggs=True)


def test_a_route_with_no_stop_in_the_window_stops_even_with_the_flag():
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.27.2", sql="x")]
    assert "never stops at a TimescaleDB release from 2.14 to 2.24" in _refusal(steps, migrate_caggs=True)


def test_nothing_old_changes_nothing():
    steps = _route()
    assert _decide(list(steps), Cluster(aggregates="")) == steps


def test_a_route_below_2_25_asks_only_about_time_bucket_ng():
    """It goes past 2.14, where those fail; the old format is fine below 2.25."""
    steps = _route()[:10]
    assert steps[-1] == VerifyVersion(hop=4, expect="2.15.3")
    cluster = Cluster()
    assert _decide(list(steps), cluster) == steps
    assert cluster.sql and all("time_bucket_ng" in sql for sql in cluster.sql)


def test_a_route_that_stays_at_214_or_below_is_not_even_asked():
    steps = _route()[:6]
    assert steps[-1] == VerifyVersion(hop=2, expect="2.11.2")
    cluster = Cluster()
    assert _decide(list(steps), cluster) == steps and cluster.sql == []


def test_a_question_that_fails_stops_it_before_anything():
    """Measured: going on left it to the gate at hop 6, after three pg_upgrades."""
    cluster = Cluster()
    cluster.psql = lambda *a: (1, "", "psql: error: connection lost" + chr(10) + "more")
    text = _refusal(cluster=cluster, migrate_caggs=True)
    assert text.startswith("apply did not start: it could not check every database")
    assert "  verify: psql: error: connection lost" + chr(10) in text and "psql -U postgres" in text
    # The first update it would find out at is the one past 2.14, after 11 to 13.
    assert "no update past 2.14 gets through" in text
    assert "after upgrading PostgreSQL from 11 to 13" in text and "hop" not in text.lower()
    assert text.endswith("This came from a read-only check. Nothing has been changed.")


def test_no_primary_stops_it_before_anything():
    steps = _route()
    replica = Instance(pod="tsdb-1", role="replica", extensions={"verify": "2.1.1"})
    down = Instance(pod="tsdb-0", role="unknown", error="connection refused")
    with pytest.raises(ApplyRefused) as refused:
        _plan_cagg_migration(steps, {"source": {"ts": "2.1.1"}}, Cluster(), [down, replica], ["verify"],
                             ApplyOptions())
    text = chr(10).join(refused.value.reasons)
    assert "no primary could be queried: tsdb-0 is unknown, connection refused; tsdb-1 is replica" in text
    assert "Nothing has been changed" in text


def test_empty_last_fields_survive_output_being_stripped():
    """Measured on pg11: kubectl output is stripped, chr(31) counts as whitespace
    to Python, and an aggregate with no views and no grants looked malformed."""
    def runner(database, sql):
        if "information_schema.columns" in sql:
            return 0, "f", ""
        return 0, BARE.strip(), ""

    found = old_caggs(runner, "verify", "2.1.1")
    assert found and found[0].dependents == () and found[0].grants == ()


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
        assert "'end')" in sql
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
