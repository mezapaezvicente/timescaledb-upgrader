"""Tests for verify/harness.py: the parts that decide, not the parts that run.

Which images and exact releases a row resolves to, and how a run is judged,
are where a harness can quietly verify the wrong thing. The cluster plumbing
is exercised by running it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tsupgrade.contents import ImageContents
from tsupgrade.matrix import MatrixEntry

_PATH = Path(__file__).resolve().parents[1] / "verify" / "harness.py"
_spec = importlib.util.spec_from_file_location("harness", _PATH)
harness = importlib.util.module_from_spec(_spec)
sys.modules["harness"] = harness
_spec.loader.exec_module(harness)

REPO = "timescale/timescaledb-ha"


PG15 = ImageContents({"repository": REPO, "images": {
    "pg15.13-ts2.20.3": {"digest": "sha256:aa", "pg": {"15": {
        "server_version": "15.13",
        "installable": ["2.9.0", "2.9.3", "2.12.1", "2.12.2", "2.13.0", "2.20.3"]}}},
    "pg15.18-ts2.28.3": {"digest": "sha256:bb", "pg": {"15": {
        "server_version": "15.18",
        "installable": ["2.13.0", "2.13.1", "2.27.2", "2.28.0", "2.28.3"]}}},
    # A multi-PG image tagged for 12: never used for a pg11 row.
    "pg12.6-ts2.0.1-p0": {"digest": "sha256:cc", "pg": {
        "11": {"server_version": "11.11", "installable": ["2.0.1"]},
        "12": {"server_version": "12.6", "installable": ["1.7.5", "2.0.1"]}}},
}})


def _row(pg, src, tgt, target_pg=None):
    return MatrixEntry(source_pg=pg, source_ts=src, target_pg=target_pg or pg, target_ts=tgt)


# --- resolve ---------------------------------------------------------------


def test_single_image_row_uses_newest_patches_on_the_target_image():
    run = harness.resolve(_row("15", "2.13", "2.28"), PG15)
    assert isinstance(run, harness.Run)
    assert run.start_image == run.target_image == REPO + ":pg15.18-ts2.28.3"
    assert (run.source, run.target) == ("2.13.1", "2.28.3")
    assert run.start_pg == run.target_pg == "15.18"
    assert run.candidates == (REPO + ":pg15.13-ts2.20.3",)


def test_source_below_the_target_images_floor_starts_on_an_older_image():
    run = harness.resolve(_row("15", "2.12", "2.28"), PG15)
    assert run.start_image == REPO + ":pg15.13-ts2.20.3"
    assert run.target_image == REPO + ":pg15.18-ts2.28.3"
    assert (run.source, run.start_pg, run.target_pg) == ("2.12.2", "15.13", "15.18")


def test_start_image_prefers_the_one_reaching_furthest():
    contents = ImageContents({"repository": REPO, "images": {
        "pg15.1-ts2.10.0": {"pg": {"15": {"server_version": "15.1",
                                          "installable": ["2.9.0", "2.10.0"]}}},
        "pg15.13-ts2.20.3": {"pg": {"15": {"server_version": "15.13",
                                           "installable": ["2.9.0", "2.9.3", "2.20.3"]}}},
        "pg15.18-ts2.28.3": {"pg": {"15": {"server_version": "15.18",
                                           "installable": ["2.13.0", "2.28.3"]}}},
    }})
    run = harness.resolve(_row("15", "2.9", "2.28"), contents)
    assert run.start_image == REPO + ":pg15.13-ts2.20.3"
    assert run.source == "2.9.3"


def test_multi_pg_image_is_only_used_for_its_own_tagged_major():
    skip = harness.resolve(_row("11", "2.0", "2.1"), PG15)
    assert isinstance(skip, harness.Unrunnable)
    assert "pg11" in skip.reason


def test_target_series_nobody_ships_is_unrunnable():
    skip = harness.resolve(_row("15", "2.28", "2.30"), PG15)
    assert isinstance(skip, harness.Unrunnable)
    assert "2.30.x" in skip.reason


def test_source_series_nobody_ships_is_unrunnable():
    skip = harness.resolve(_row("15", "2.10", "2.28"), PG15)
    assert isinstance(skip, harness.Unrunnable)
    assert "2.10.x" in skip.reason


def test_cross_pg_rows_are_out_of_scope():
    skip = harness.resolve(_row("15", "2.28", "2.28", target_pg="16"), PG15)
    assert isinstance(skip, harness.Unrunnable)
    assert "crosses" in skip.reason


def test_select_rows_keeps_same_pg_and_filters():
    rows = [_row("15", "2.13", "2.28"), _row("15", "2.14", "2.28"),
            _row("16", "2.13", "2.30"), _row("15", "2.28", "2.30", target_pg="16")]
    assert len(harness.select_rows(rows)) == 3
    assert [r.source_ts for r in harness.select_rows(rows, ["15"])] == ["2.13", "2.14"]
    assert [r.source_pg for r in harness.select_rows(rows, sources=["2.13"])] == ["15", "16"]


def test_run_order_groups_by_image_and_puts_bridged_rows_last():
    runs = [harness.resolve(_row("15", s, "2.28"), PG15) for s in ("2.12", "2.13", "2.9")]
    ordered = harness.run_order(runs)
    assert [r.entry.source_ts for r in ordered] == ["2.9", "2.12", "2.13"]
    # Different start images: grouped by image, not interleaved by version.
    assert ordered[0].start_image == ordered[1].start_image


def test_shipped_contents_resolve_every_same_pg_row_but_the_no_image_ones():
    from tsupgrade.contents import load_contents
    from tsupgrade.matrix import load_matrix

    rows = harness.select_rows(load_matrix().entries)
    resolved = [harness.resolve(e, load_contents()) for e in rows]
    runs = [r for r in resolved if isinstance(r, harness.Run)]
    skipped = [r for r in resolved if isinstance(r, harness.Unrunnable)]
    assert len(runs) == 98
    assert {(s.entry.source_pg, s.entry.target_ts) for s in skipped} == {
        ("11", "2.30"), ("12", "2.30"), ("13", "2.30"), ("14", "2.30"), ("15", "2.30")}
    for run in runs:
        assert harness._key(run.source)[:2] == harness._key(run.entry.source_ts)
        assert harness._key(run.target)[:2] == harness._key(run.entry.target_ts)


# --- judge -----------------------------------------------------------------


RUN = harness.resolve(_row("15", "2.13", "2.28"), PG15)
ALL_PASS = {c: ("pass", "") for c in harness.CHECKS}


def test_judge_pass_empty_and_seeded():
    report = {"result": "pass"}
    assert harness.judge(RUN, "empty", 0, "", report, "2.28.3", None)[0] is True
    assert harness.judge(RUN, "seeded", 0, "", report, "2.28.3", ALL_PASS)[0] is True


def test_judge_refusal_without_report():
    passed, why, error, stage = harness.judge(
        RUN, "empty", 2, "VERIFICATION RUN\nrefused: nothing was written\n  - source is 2.13.0\n",
        None, "2.13.1", None)
    assert (passed, stage) == (False, "apply")
    assert why == "apply refused before writing anything"
    assert "  - source is 2.13.0" in error


def test_judge_crash_without_report():
    passed, why, error, _ = harness.judge(RUN, "empty", 1, "Traceback\nKeyError: x\n",
                                          None, None, None)
    assert not passed and "exited 1" in why
    assert "KeyError" in error


def test_judge_failed_step_quotes_the_error_lines_verbatim():
    report = {"result": "fail", "failure": {
        "hop": 2, "action": "update_extension", "summary": "ALTER EXTENSION failed",
        "error": ["WARNING:  job history configuration modified",
                  "ERROR:  cannot drop table x because other objects depend on it",
                  "DETAIL:  view y depends on table x",
                  "command terminated with exit code 1"]}}
    passed, why, error, stage = harness.judge(RUN, "seeded", 2, "", report, "2.20.3", ALL_PASS)
    assert not passed and stage == "apply"
    assert why == "hop 2 update_extension failed: ALTER EXTENSION failed"
    assert error.splitlines() == [
        "ERROR:  cannot drop table x because other objects depend on it",
        "DETAIL:  view y depends on table x"]


def test_judge_pass_report_but_wrong_version_fails():
    passed, why, _, stage = harness.judge(RUN, "empty", 0, "", {"result": "pass"}, "2.27.2", None)
    assert not passed and stage == "verify"
    assert "2.27.2" in why


def test_judge_seeded_needs_every_check_and_a_missing_line_fails():
    checks = dict(ALL_PASS)
    checks["cagg_matches"] = ("fail", "3 differing buckets")
    del checks["decompress"]
    passed, why, error, stage = harness.judge(RUN, "seeded", 0, "", {"result": "pass"},
                                              "2.28.3", checks)
    assert not passed and stage == "check"
    assert why.endswith("cagg_matches, decompress")
    assert "check|cagg_matches|fail|3 differing buckets" in error
    assert "check|decompress|missing" in error


def test_judge_empty_ignores_checks():
    assert harness.judge(RUN, "empty", 0, "", {"result": "pass"}, "2.28.3", {})[0] is True


# --- small helpers ---------------------------------------------------------


def test_parse_checks():
    out = "noise\ncheck|rows|pass|50400 rows\ncheck|version|fail|extversion 2.20.3, expected 2.28.3\n"
    assert harness.parse_checks(out) == {
        "rows": ("pass", "50400 rows"),
        "version": ("fail", "extversion 2.20.3, expected 2.28.3")}


def test_error_lines_falls_back_to_everything_when_nothing_looks_like_an_error():
    assert harness.error_lines(["", "something odd", "more"]) == "something odd\nmore"


def test_images_used_follows_the_plan():
    bridged = harness.resolve(_row("15", "2.12", "2.28"), PG15)
    plan = {"steps": [
        {"action": "ensure_image", "image": REPO + ":pg15.13-ts2.20.3"},
        {"action": "update_extension"},
        {"action": "ensure_image", "image": REPO + ":pg15.18-ts2.28.3"}]}
    assert harness.images_used(bridged, plan) == [
        REPO + ":pg15.13-ts2.20.3", REPO + ":pg15.18-ts2.28.3"]
    # No plan (refused): the images the row would have used.
    assert harness.images_used(bridged, None) == [
        REPO + ":pg15.13-ts2.20.3", REPO + ":pg15.18-ts2.28.3"]
    assert harness.images_used(RUN, None) == [REPO + ":pg15.18-ts2.28.3"]


def test_the_make_up_cluster_is_refused():
    with pytest.raises(harness.HarnessError, match="long-lived"):
        harness.Cluster("tsdb-upgrader", 2, PG15)
