"""Tests for the 2.25 gate on old databases.

What would hurt if wrong: reporting all clear on a database from before 2.7,
whose catalog has no ``finalized`` column.
"""

from __future__ import annotations

from tsupgrade.gates import BLOCKED, CLEAR, INDETERMINATE, check_old_caggs


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
