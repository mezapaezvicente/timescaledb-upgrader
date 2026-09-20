"""Tests for the update-script gates.

No cluster: the gates take their query runner as an argument precisely so the
decisions can be exercised against a scripted database. What is tested here is
the part that would hurt if it were wrong -- when a gate applies, what it does
when the catalog does not have the column it wants to read, and the difference
between "clear" and "could not tell".

The live counterparts are recorded in ``docs/drift-verification.md``.
"""

from __future__ import annotations

import pytest

from tsupgrade.gates import (
    BLOCKED,
    CLEAR,
    INDETERMINATE,
    NOT_APPLICABLE,
    check_information_views,
    check_multinode,
    check_old_caggs,
    run_gates,
)


class FakeDB:
    """A scripted database.

    ``columns`` is the set of ``(table, column)`` pairs that exist; everything
    else answers from ``rows``, keyed by a substring of the query. Anything
    unmatched returns an empty result, which is the benign answer -- a test
    that means to exercise a failure has to say so.
    """

    def __init__(self, columns=(), rows=None, fail_on=(), calls=None):
        self.columns = set(columns)
        self.rows = rows or {}
        self.fail_on = tuple(fail_on)
        self.calls = calls if calls is not None else []

    def __call__(self, database, sql):
        self.calls.append(sql)

        for needle in self.fail_on:
            if needle in sql:
                return 1, "", 'ERROR:  column "{0}" does not exist'.format(needle)

        if "information_schema.columns" in sql:
            table = _between(sql, "table_name='", "'")
            column = _between(sql, "column_name='", "'")
            return 0, "t" if (table, column) in self.columns else "f", ""

        for needle, value in self.rows.items():
            if needle in sql:
                return 0, value, ""
        return 0, "", ""


def _between(text, start, end):
    head = text.split(start, 1)[1]
    return head.split(end, 1)[0]


MODERN = [
    ("continuous_agg", "finalized"),
    ("continuous_aggs_bucket_function", "bucket_func"),
]
LEGACY = [
    ("continuous_agg", "finalized"),
    ("continuous_aggs_bucket_function", "name"),
]


# --------------------------------------------------------------------------
# Applicability
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source,target,applies",
    [
        ("2.13.0", "2.30.1", True),   # crosses the 2.14 removal
        ("2.13.1", "2.14.0", True),   # exactly the crossing
        ("2.14.0", "2.30.1", False),  # already past it
        ("2.20.3", "2.30.1", False),
        ("2.10.0", "2.13.1", False),  # target still below the removal
    ],
)
def test_multinode_applies_only_across_the_214_removal(source, target, applies):
    result = check_multinode(FakeDB(), "db", source, target)
    assert (result.status != NOT_APPLICABLE) is applies


@pytest.mark.parametrize(
    "target,applies",
    [("2.25.0", True), ("2.30.1", True), ("2.24.0", False), ("2.20.3", False)],
)
def test_cagg_gate_applies_only_when_crossing_225(target, applies):
    result = check_old_caggs(FakeDB(columns=MODERN), "db", "2.13.0", target)
    assert (result.status != NOT_APPLICABLE) is applies


@pytest.mark.parametrize(
    "source,applies",
    [
        ("2.24.0", True),   # the guard is strictly greater, so 2.24.0 is exposed
        ("2.13.0", True),
        ("2.24.1", False),
        ("2.28.0", False),
        ("2.30.1", False),
    ],
)
def test_information_views_gate_keys_off_the_source_version(source, applies):
    """The fix protects updates *from* a version past 2.24.0, not *to* one.

    sql/CMakeLists.txt guards it with VERSION_GREATER "2.24.0" -- strictly
    greater -- so a source at exactly 2.24.0 still drops the views for real.
    """
    result = check_information_views(FakeDB(), "db", source, "2.30.1")
    assert (result.status != NOT_APPLICABLE) is applies


def test_a_series_only_source_is_treated_as_exposed():
    """'2.24' pins less than '2.24.0' does, so assume the exposed reading."""
    result = check_information_views(FakeDB(), "db", "2.24", "2.30.1")
    assert result.status != NOT_APPLICABLE


# --------------------------------------------------------------------------
# Multi-node
# --------------------------------------------------------------------------

def test_multinode_clear_when_nothing_is_distributed():
    db = FakeDB(columns=[("hypertable", "replication_factor")], rows={"count(*)": "0"})
    assert check_multinode(db, "db", "2.13.0", "2.30.1").status == CLEAR


@pytest.mark.parametrize(
    "needle", ["replication_factor > 0", "timescaledb_fdw", "dist_uuid"]
)
def test_any_single_trigger_blocks(needle):
    """Three independent triggers; each aborts the update on its own."""
    db = FakeDB(
        columns=[("hypertable", "replication_factor")],
        rows={needle: "1", "count(*)": "0"},
    )
    assert check_multinode(db, "db", "2.13.0", "2.30.1").status == BLOCKED


def test_missing_replication_factor_column_is_clear_not_indeterminate():
    """The column is removed *by* 2.14, so its absence proves the gate is moot.

    The research flagged this probe for continuous_agg.finalized but not here,
    and without it the whole gate errors out on any database at 2.14 or later.
    """
    db = FakeDB(columns=[], rows={"count(*)": "0"})
    result = check_multinode(db, "db", "2.13.0", "2.30.1")
    assert result.status == CLEAR
    assert any("no replication_factor" in d or "already at 2.14" in d
               for d in result.details)


def test_missing_column_does_not_blank_the_other_two_triggers():
    """One absent column must not hide a foreign server that is really there."""
    db = FakeDB(columns=[], rows={"timescaledb_fdw": "1", "count(*)": "0"})
    assert check_multinode(db, "db", "2.13.0", "2.30.1").status == BLOCKED


def test_failed_probe_is_indeterminate_not_clear():
    db = FakeDB(fail_on=("information_schema.columns",), rows={"count(*)": "0"})
    assert check_multinode(db, "db", "2.13.0", "2.30.1").status == INDETERMINATE


def test_a_failing_query_never_reads_as_clear():
    db = FakeDB(
        columns=[("hypertable", "replication_factor")],
        fail_on=("replication_factor > 0",),
    )
    assert check_multinode(db, "db", "2.13.0", "2.30.1").status == INDETERMINATE


# --------------------------------------------------------------------------
# Continuous aggregates
# --------------------------------------------------------------------------

def test_caggs_clear_when_both_sub_checks_pass():
    assert check_old_caggs(FakeDB(columns=MODERN), "db", "2.13.0", "2.30.1").status == CLEAR


def test_unfinalized_aggregate_blocks():
    db = FakeDB(columns=MODERN, rows={"finalized IS FALSE": "public.old_cagg"})
    result = check_old_caggs(db, "db", "2.13.0", "2.30.1")
    assert result.status == BLOCKED
    assert any("cagg_migrate" in d for d in result.details)


def test_time_bucket_ng_blocks_independently_of_the_format():
    """A fully finalized aggregate still blocks if its bucket is time_bucket_ng.

    Two separate RAISE EXCEPTION blocks ship in 2.24.0--2.25.0.sql; the research
    named the second one but gave no query for it.
    """
    db = FakeDB(columns=MODERN, rows={"time_bucket_ng": "public.ng_cagg"})
    result = check_old_caggs(db, "db", "2.13.0", "2.30.1")
    assert result.status == BLOCKED


def test_missing_finalized_column_means_already_past_the_cutoff():
    """2.25 drops the column along with the format, so absence satisfies it."""
    db = FakeDB(columns=[("continuous_aggs_bucket_function", "bucket_func")])
    result = check_old_caggs(db, "db", "2.28.0", "2.30.1")
    assert result.status == CLEAR
    assert any("past the 2.25 cutoff" in d for d in result.details)


def test_legacy_bucket_schema_is_queried_by_name():
    """Before ~2.14 the catalog has `name` + `experimental`, not `bucket_func`.

    Without this branch the gate goes indeterminate on precisely the old
    databases it protects, since a 2.13 database reaching 2.30 passes straight
    through the 2.25 block.
    """
    db = FakeDB(columns=LEGACY, rows={"bf.name LIKE": "public.ng_cagg"})
    result = check_old_caggs(db, "db", "2.13.0", "2.30.1")
    assert result.status == BLOCKED
    assert any("bf.name LIKE" in call for call in db.calls)
    # Note the table is `continuous_aggs_bucket_function`, so a bare
    # "bucket_func" substring matches it too; check the column reference.
    assert not any("bf.bucket_func" in call for call in db.calls)


def test_neither_bucket_schema_means_nothing_can_use_it():
    db = FakeDB(columns=[("continuous_agg", "finalized")])
    assert check_old_caggs(db, "db", "2.13.0", "2.30.1").status == CLEAR


def test_blocked_beats_clear_across_sub_checks():
    db = FakeDB(columns=MODERN, rows={"time_bucket_ng": "public.ng"})
    result = check_old_caggs(db, "db", "2.13.0", "2.30.1")
    assert result.status == BLOCKED


# --------------------------------------------------------------------------
# Information views
# --------------------------------------------------------------------------

def test_information_views_clear_when_nothing_depends():
    assert check_information_views(FakeDB(), "db", "2.13.0", "2.30.1").status == CLEAR


def test_information_views_blocked_lists_every_offender():
    db = FakeDB(rows={"timescaledb_information": "public.a\npublic.b"})
    result = check_information_views(db, "db", "2.13.0", "2.30.1")
    assert result.status == BLOCKED
    assert "2 user view(s)" in result.summary
    assert any("public.a" in d for d in result.details)
    assert any("public.b" in d for d in result.details)


def test_information_views_query_excludes_extension_owned_views():
    """Without the deptype='e' exclusion every view reports itself."""
    db = FakeDB()
    check_information_views(db, "db", "2.13.0", "2.30.1")
    assert any("deptype = 'e'" in call for call in db.calls)


def test_information_views_failure_is_indeterminate():
    db = FakeDB(fail_on=("timescaledb_information",))
    assert check_information_views(db, "db", "2.13.0", "2.30.1").status == INDETERMINATE


# --------------------------------------------------------------------------
# The set
# --------------------------------------------------------------------------

def test_run_gates_returns_all_three():
    results = run_gates(FakeDB(columns=MODERN), "db", "2.13.0", "2.30.1")
    assert [r.name for r in results] == ["multinode", "old_caggs", "information_views"]


def test_blocks_property_only_true_for_blocked():
    db = FakeDB(columns=MODERN, rows={"timescaledb_information": "public.v"})
    results = run_gates(db, "db", "2.13.0", "2.30.1")
    blocking = [r for r in results if r.blocks]
    assert [r.name for r in blocking] == ["information_views"]


def test_every_gate_cites_a_source():
    for result in run_gates(FakeDB(columns=MODERN), "db", "2.13.0", "2.30.1"):
        assert result.source_url and result.source_url.startswith("https://")
