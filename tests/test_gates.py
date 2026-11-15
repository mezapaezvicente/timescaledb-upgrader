"""Tests for the update-script gates.

No cluster: the gates take their query runner as an argument precisely so the
decisions can be exercised against a scripted database. What is tested here is
the part that would hurt if it were wrong -- when a gate applies, what it does
when the catalog does not have the column it wants to read, and the difference
between "clear" and "could not tell".

The live counterparts are recorded in ``docs/drift-verification.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tsupgrade.gates import (
    BLOCKED,
    CLEAR,
    INDETERMINATE,
    NOT_APPLICABLE,
    check_information_views,
    check_multinode,
    check_old_caggs,
    check_time_bucket_ng,
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


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _between(text, start, end):
    head = text.split(start, 1)[1]
    return head.split(end, 1)[0]


MODERN = [
    ("continuous_agg", "finalized"),
    ("continuous_aggs_bucket_function", "bucket_func"),
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


def _boundaries():
    """The measured per-target boundaries in fixtures/view_drop_boundaries.tsv."""
    rows = []
    for line in (FIXTURES / "view_drop_boundaries.tsv").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            target, last_dropping, first_keeping, repair_only = line.split("	")
            rows.append((target, last_dropping, first_keeping, repair_only))
    return rows


@pytest.mark.parametrize("target,last_dropping,first_keeping,repair_only", _boundaries())
def test_information_views_gate_matches_every_measured_update_script(
    target, last_dropping, first_keeping, repair_only
):
    """The gate's floors agree with what each shipped script does.

    A script that drops the views must get the full check, one that keeps them
    must not, and one that drops them only in the #10272 repair gets the
    narrow check.
    """
    if last_dropping != "-":
        assert check_information_views(FakeDB(), "db", last_dropping, target).status != NOT_APPLICABLE
    if first_keeping != "-":
        db = FakeDB()
        result = check_information_views(db, "db", first_keeping, target)
        if repair_only == "-":
            assert result.status == NOT_APPLICABLE
        else:
            assert first_keeping == repair_only.split("..")[0]
            assert result.status != NOT_APPLICABLE
            assert "job_history" in db.calls[-1]


@pytest.mark.parametrize(
    "source,target,applies",
    [
        # Measured on pg15.18 and pg16.15 with a user view on every information view.
        ("2.25.0", "2.27.2", True),   # cannot drop view ...hypertables: 2.25 is not enough below 2.28
        ("2.25.2", "2.27.2", True),
        ("2.26.4", "2.27.2", False),
        ("2.26.0", "2.27.2", False),
        ("2.27.2", "2.28.0", False),
        ("2.25.0", "2.28.0", False),
        ("2.24.0", "2.30.1", True),   # views_detached.sql is built only past 2.24.0
        ("2.13.1", "2.28.3", True),
        # Measured on pg14.19 and pg15.18.
        ("2.15.3", "2.19.3", False),
        ("2.14.2", "2.15.3", True),
        ("2.10.2", "2.11.2", False),
        ("2.10.1", "2.11.2", True),
        ("2.20.0", "2.21.4", False),
        ("2.21.4", "2.22.1", True),
        ("2.5.0", "2.5.2", False),
        ("2.6.1", "2.7.0", True),
        # Later than anything measured: the 2.28 floor holds.
        ("2.29.0", "2.31.0", False),
    ],
)
def test_information_views_gate_keys_off_both_versions(source, target, applies):
    result = check_information_views(FakeDB(), "db", source, target)
    assert (result.status != NOT_APPLICABLE) is applies


def test_a_series_only_version_is_treated_as_exposed():
    """'2.24' pins less than '2.24.0' does, so assume the exposed reading."""
    assert check_information_views(FakeDB(), "db", "2.24", "2.30.1").status != NOT_APPLICABLE
    assert check_information_views(FakeDB(), "db", "2.26.0", "2.27").status != NOT_APPLICABLE


@pytest.mark.parametrize("source", ["2.25.0", "2.26.4", "2.27.2", "2.28.0"])
def test_history_repair_window_checks_only_the_views_it_drops(source):
    """2.25.0-2.28.0 to 2.28.1+ keeps the views unless the #10272 repair runs.

    Measured on pg15.18: created at 2.13.1, updated to 2.25.0, 2.26.4 or
    2.27.2, a user view on job_history blocks 2.28.3; created fresh at 2.25.0,
    the same view does not. Only the bad id column tells them apart.
    """
    db = FakeDB()
    check_information_views(db, "db", source, "2.28.3")
    query = db.calls[-1]
    assert "'job_history', 'job_errors'" in query
    assert "atttypid <> 'bigint'::regtype" in query


def test_history_repair_does_not_run_past_its_window():
    assert check_information_views(FakeDB(), "db", "2.28.1", "2.30.1").status == NOT_APPLICABLE
    assert check_information_views(FakeDB(), "db", "2.25.0", "2.28.0").status == NOT_APPLICABLE


def test_history_repair_blocks_with_its_own_explanation():
    db = FakeDB(rows={"timescaledb_information": "public.fx_v_job_history"})
    result = check_information_views(db, "db", "2.25.0", "2.28.3")
    assert result.status == BLOCKED
    assert any("#10272" in d for d in result.details)


def test_a_full_drop_uses_the_full_query():
    db = FakeDB()
    check_information_views(db, "db", "2.24.0", "2.28.3")
    assert "job_history" not in db.calls[-1]


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


def test_missing_finalized_column_means_already_past_the_cutoff():
    """2.25 drops the column along with the format, so absence satisfies it."""
    db = FakeDB(columns=[("continuous_aggs_bucket_function", "bucket_func")])
    result = check_old_caggs(db, "db", "2.28.0", "2.30.1")
    assert result.status == CLEAR
    assert any("past the 2.25 cutoff" in d for d in result.details)


def test_the_format_gate_leaves_time_bucket_ng_to_its_own():
    """No database with one reaches 2.25: every update past 2.14 fails first."""
    db = FakeDB(columns=MODERN, rows={"time_bucket_ng": NG_ROW})
    assert check_old_caggs(db, "db", "2.13.0", "2.30.1").status == CLEAR
    assert not any("time_bucket_ng" in call for call in db.calls)


# --------------------------------------------------------------------------
# Aggregates on time_bucket_ng
# --------------------------------------------------------------------------

NG_ROW = "\x1f".join(["public.ng_cagg", "public.metrics", "8192", "1", "0", "", "", "end"])


@pytest.mark.parametrize(
    "source,target,applies",
    [
        ("2.12.2", "2.15.0", True),   # the first update that fails
        ("2.14.2", "2.30.1", True),
        ("2.4.0", "2.20.3", True),    # the first release that could create one
        ("2.13.0", "2.14.2", False),  # never leaves 2.14
        ("2.15.3", "2.30.1", False),  # cannot hold one: no database with one gets there
    ],
)
def test_time_bucket_ng_gate_applies_only_past_214(source, target, applies):
    result = check_time_bucket_ng(FakeDB(), "db", source, target)
    assert (result.status != NOT_APPLICABLE) is applies


def test_time_bucket_ng_is_found_by_the_definition_not_the_bucket_catalog():
    """Measured: before 2.15 a fixed-width bucket without a timezone has no
    row in continuous_aggs_bucket_function, and the old gate called it clear."""
    db = FakeDB(rows={"time_bucket_ng": NG_ROW})
    result = check_time_bucket_ng(db, "db", "2.12.2", "2.28.3")
    assert result.status == BLOCKED
    query = next(call for call in db.calls if "time_bucket_ng" in call)
    assert "pg_get_viewdef" in query and "direct_view_name" in query
    assert "continuous_aggs_bucket_function" not in query


def test_time_bucket_ng_points_at_the_rebuild_not_cagg_migrate():
    """cagg_migrate refuses them (measured), whatever the 2.25 hint says."""
    result = check_time_bucket_ng(FakeDB(rows={"time_bucket_ng": NG_ROW}), "db", "2.12.2", "2.28.3")
    assert "1 continuous aggregate(s)" in result.summary
    assert any("public.ng_cagg" in d for d in result.details)
    assert any("--migrate-caggs" in d and "refuses" in d for d in result.details)
    assert not any("CALL cagg_migrate" in d for d in result.details)


def test_time_bucket_ng_clear_when_none():
    assert check_time_bucket_ng(FakeDB(), "db", "2.12.2", "2.28.3").status == CLEAR


def test_time_bucket_ng_failure_is_indeterminate():
    db = FakeDB(fail_on=("pg_get_viewdef",))
    assert check_time_bucket_ng(db, "db", "2.12.2", "2.28.3").status == INDETERMINATE


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

def test_run_gates_returns_all_four():
    results = run_gates(FakeDB(columns=MODERN), "db", "2.13.0", "2.30.1")
    assert [r.name for r in results] == ["multinode", "old_caggs", "time_bucket_ng", "information_views"]


def test_blocks_property_only_true_for_blocked():
    db = FakeDB(columns=MODERN, rows={"timescaledb_information": "public.v"})
    results = run_gates(db, "db", "2.13.0", "2.30.1")
    blocking = [r for r in results if r.blocks]
    assert [r.name for r in blocking] == ["information_views"]


def test_every_gate_cites_a_source():
    for result in run_gates(FakeDB(columns=MODERN), "db", "2.13.0", "2.30.1"):
        assert result.source_url and result.source_url.startswith("https://")
