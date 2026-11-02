"""Tests for looking ahead at the 2.25 refusal of old continuous aggregates.

What would hurt if wrong: reporting all clear on a database from before 2.7,
whose catalog has no ``finalized`` column, or stopping a hop on an aggregate
that release cannot migrate yet.
"""

from __future__ import annotations

from tsupgrade.gates import BLOCKED, CLEAR, INDETERMINATE, check_old_caggs
from tsupgrade.preflight import PASS, UNKNOWN, Instance, PreflightOptions, check_later_gates


def _runner(finalized_column, aggregates="public.fx_hourly", broken=False):
    """A catalog: ``finalized_column`` says whether continuous_agg has that
    column; ``aggregates`` is what a query for old-format ones returns."""
    def run(database, sql):
        if broken:
            return 1, "", "connection lost"
        if "information_schema.columns" in sql or "pg_attribute" in sql:
            if "'finalized'" in sql:
                return 0, "t" if finalized_column else "f", ""
            return 0, "f", ""
        if "FROM _timescaledb_catalog.continuous_agg" in sql:
            return 0, aggregates, ""
        return 0, "", ""
    return run


def test_a_database_from_before_2_7_is_not_past_the_cutoff():
    """Measured: 2.1.1's install script has no finalized column. Every
    aggregate there is in the old format, and that used to read as all clear."""
    gate = check_old_caggs(_runner(False), "verify", "2.1.1", "2.27.2")
    assert gate.status == BLOCKED
    assert "old format : public.fx_hourly" in gate.details


def test_without_aggregates_a_database_from_before_2_7_is_clear():
    assert check_old_caggs(_runner(False, aggregates=""), "verify", "2.1.1", "2.27.2").status == CLEAR


def test_past_the_cutoff_the_missing_column_still_means_clear():
    gate = check_old_caggs(_runner(False), "verify", "2.25.0", "2.28.3")
    assert gate.status == CLEAR
    assert any("already past the 2.25 cutoff" in d for d in gate.details)


def test_a_version_that_says_nothing_is_not_clear():
    assert check_old_caggs(_runner(False), "verify", "unknown", "2.27.2").status == INDETERMINATE


class Kube:
    def __init__(self, run):
        self.run = run

    def psql(self, pod, container, database, user, sql):
        return self.run(database, sql)


def _check(run, target="2.15.3", later=((6, "2.27.2"), (7, "2.28.3")), version="2.1.1"):
    return check_later_gates(
        Kube(run), PreflightOptions(target_image="x", later_updates=tuple(later)),
        [Instance(pod="tsdb-0", extensions={"verify": version}),
         Instance(pod="tsdb-1", extensions={"verify": version})], target)


def test_an_old_aggregate_is_told_about_at_the_start_and_does_not_stop_the_hop():
    result = _check(_runner(False), target="2.1.1")
    assert result.status == UNKNOWN
    assert "public.fx_hourly" in result.notice
    assert "Hop 6 of this plan updates to 2.27.2" in result.notice
    assert "cagg_migrate()" in result.notice


def test_each_database_is_asked_once_replicas_hold_the_same_data():
    calls = []
    run = _runner(False)

    def counted(database, sql):
        calls.append(sql)
        return run(database, sql)

    _check(counted, target="2.1.1")
    assert sum("FROM _timescaledb_catalog.continuous_agg" in c and "JOIN" not in c for c in calls) == 1


def test_nothing_old_is_a_pass_with_nothing_to_read():
    result = _check(_runner(True, aggregates=""), version="2.11.2")
    assert result.status == PASS and result.notice is None


def test_a_failed_question_is_unknown_not_clear():
    result = _check(_runner(True, broken=True), version="2.11.2")
    assert result.status == UNKNOWN and result.notice is None


def test_no_later_update_reaches_2_25_so_nothing_is_asked():
    assert _check(_runner(False), later=[(4, "2.15.3")]) is None


def test_this_hop_reaching_2_25_is_the_gates_check_not_this_one():
    assert _check(_runner(False), target="2.27.2") is None
