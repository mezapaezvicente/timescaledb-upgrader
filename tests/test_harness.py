"""Tests for verify/harness.py: the parts that decide, not the parts that run.

Which images and exact releases a row resolves to, and how a run is judged,
are where a harness can quietly verify the wrong thing. The cluster plumbing
is exercised by running it.
"""

from __future__ import annotations

import importlib.util
import json
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


def test_a_cross_pg_row_needs_a_target_major_image():
    skip = harness.resolve(_row("15", "2.28", "2.28", target_pg="16"), PG15)
    assert isinstance(skip, harness.Unrunnable)
    assert "no measured pg16 image" in skip.reason


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


def test_judge_failed_check_quotes_what_the_server_said():
    checks = dict(ALL_PASS)
    checks["cagg_survived"] = ("fail", "840 rows (could not set materialized_only)")
    stderr = ("psql:<stdin>:44: ERROR:  invalid continuous aggregate query\n"
              "DETAIL:  Continuous Aggregates with partials is not supported anymore.\n"
              "psql:<stdin>:60: NOTICE:  continuous aggregate \"fx_hourly\" is already up-to-date\n")
    _, _, error, _ = harness.judge(RUN, "seeded", 0, "", {"result": "pass"}, "2.28.3",
                                   checks, stderr)
    assert error.splitlines() == [
        "check|cagg_survived|fail|840 rows (could not set materialized_only)",
        "psql:<stdin>:44: ERROR:  invalid continuous aggregate query",
        "DETAIL:  Continuous Aggregates with partials is not supported anymore."]


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


def test_major_upgrades_reads_what_pg_upgrade_did_from_the_report():
    report = {"steps": [
        {"action": "update_extension", "output": ["Copying user relation files"]},
        {"action": "major_upgrade", "summary": "PostgreSQL 12 -> 13; 2 instance(s) on x",
         "output": ["Linking user relation files",
                    "relation files sharing an inode with /d.pg12: 159", "Upgrade Complete"]},
        {"action": "major_upgrade", "summary": "PostgreSQL 13 -> 15; 2 instance(s) on y",
         "output": ["Copying user relation files                                 ok",
                    "relation files sharing an inode with /d.pg13: 0"]},
        {"action": "major_upgrade", "summary": "PostgreSQL 15 -> 16; 2 instance(s) on z",
         "output": ["Upgrade Complete"]}]}
    assert harness.major_upgrades(report) == [
        {"from_pg": 12, "to_pg": 13, "files": "link", "shared_inodes": 159},
        {"from_pg": 13, "to_pg": 15, "files": "copy", "shared_inodes": 0},
        {"from_pg": 15, "to_pg": 16, "files": None, "shared_inodes": None}]
    assert harness.major_upgrades(None) == []


def test_the_make_up_cluster_is_refused():
    with pytest.raises(harness.HarnessError, match="long-lived"):
        harness.Cluster("tsdb-upgrader", 2, PG15)


def test_a_run_after_a_major_change_gets_a_fresh_namespace(monkeypatch):
    """A rollback ends healthy on the start image, but its disk holds the
    set-aside directory; the next mode must not inherit it (measured: the
    rollback-seeded run after rollback-empty found it and failed)."""
    monkeypatch.setenv("HARNESS_BASH", "bash")
    cluster = harness.Cluster("tsdb-verify", 2, PG15)
    calls = []
    monkeypatch.setattr(cluster, "exists", lambda: True)
    monkeypatch.setattr(cluster, "on", lambda image: True)
    monkeypatch.setattr(cluster, "isolate", lambda log: calls.append("isolate"))
    monkeypatch.setattr(cluster, "load", lambda image, log: calls.append("load"))
    monkeypatch.setattr(cluster, "kubectl", lambda *a, **k: (calls.append(a[0]), (0, "", ""))[1])
    monkeypatch.setattr(harness, "_run", lambda *a, **k: (calls.append("up.sh"), (0, "", ""))[1])

    class Log:
        def section(self, text):
            calls.append("reset")

    cluster.ensure("img", Log())
    assert calls == ["isolate"]
    calls.clear()
    cluster.used = True
    cluster.ensure("img", Log())
    assert calls[:3] == ["reset", "load", "delete"] and "up.sh" in calls
    assert cluster.used is False


def test_a_command_never_gets_the_terminal_as_its_stdin(monkeypatch, tmp_path):
    """apply asks before migrating aggregates when stdin is a terminal. A run
    launched from a console handed it that terminal, and both refusal runs
    waited for an answer until they timed out (measured: caggs-refused-seeded
    and ng-refused-empty, 3600s each)."""
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        return harness.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(harness.subprocess, "run", run)
    harness.tsupgrade(["apply", "plan.json"], harness.Log(tmp_path / "log"))
    harness._run(["psql"], input="SELECT 1")
    assert seen[0]["stdin"] is harness.subprocess.DEVNULL
    assert seen[1]["input"] == "SELECT 1" and seen[1]["stdin"] is None


CROSS = harness.resolve(_row("12", "2.11", "2.15", target_pg="13"), harness.load_contents())


def test_a_run_goes_on_from_cached_images_unless_it_has_to_stop():
    """A registry that does not answer is the network, not the row's result."""
    normal = harness.apply_arguments(CROSS, "p", "r", "ctx")
    assert "--use-cached-images" in normal and "--image-registry" not in normal
    mirror = harness.apply_arguments(harness.with_mirror(CROSS), "p", "r", "ctx")
    assert "--use-cached-images" in mirror
    assert mirror[mirror.index("--image-registry") + 1] == harness.MIRROR
    refused = harness.apply_arguments(harness.with_mirror(CROSS, refused=True), "p", "r", "ctx")
    assert "--use-cached-images" not in refused and "--image-registry" in refused


def test_the_mirror_cannot_ever_answer():
    assert harness.MIRROR.split("/")[0].endswith(".invalid")


def test_mirror_runs_name_their_records_and_never_a_rows():
    run = harness.with_mirror(CROSS)
    assert (run.mode("seeded"), run.end("seeded")) == ("mirror-seeded", CROSS.target)
    refused = harness.with_mirror(CROSS, refused=True)
    assert (refused.mode("empty"), refused.end("empty")) == ("mirror-refused-empty", CROSS.source)
    assert isinstance(harness.with_mirror(RUN), harness.Unrunnable)
    assert isinstance(harness.with_mirror(harness.with_link(CROSS)), harness.Unrunnable)


SAID = ["This does not mean an image is missing.", "Running without the registry is possible",
        "--image-registry <host/path>", "--use-cached-images"]
STOPPED = {"result": "fail", "failure": {"hop": 1, "action": "preflight", "summary":
                                         "preflight failed: route_images", "error": SAID}}


def test_a_run_that_had_to_stop_passes_only_if_it_stopped_as_it_should():
    run = harness.with_mirror(CROSS, refused=True)
    args = (run, "empty", 3, "")
    assert harness.judge(*args, STOPPED, run.source, None, pg_version=run.start_pg)[0] is True
    # It ran.
    assert harness.judge(*args, {"result": "pass"}, run.source, None, pg_version=run.start_pg)[0] is False
    # It stopped later, or on something else.
    later = {"failure": dict(STOPPED["failure"], hop=2)}
    assert harness.judge(*args, later, run.source, None, pg_version=run.start_pg)[0] is False
    other = {"failure": dict(STOPPED["failure"], summary="preflight failed: gates")}
    assert harness.judge(*args, other, run.source, None, pg_version=run.start_pg)[0] is False
    # It did not say what can be done.
    mute = {"failure": dict(STOPPED["failure"], error=SAID[:1])}
    assert harness.judge(*args, mute, run.source, None, pg_version=run.start_pg)[0] is False
    # It changed something.
    assert harness.judge(*args, STOPPED, run.source, None, pg_version="13.22")[0] is False
    assert harness.judge(*args, STOPPED, "2.15.3", None, pg_version=run.start_pg)[0] is False


def test_the_node_listing_is_waited_for(monkeypatch):
    monkeypatch.setenv("HARNESS_BASH", "bash")
    cluster = harness.Cluster("tsdb-verify", 2, PG15)
    answers = [[], ["docker.io/timescale/timescaledb-ha@sha256:aa"]]
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    monkeypatch.setattr(cluster, "kubectl", lambda *a, **k: (0, json.dumps(
        {"items": [{"status": {"images": [{"names": answers.pop(0)}]}}]}), ""))
    cluster.await_listed(["timescale/timescaledb-ha:t@sha256:aa"])
    assert answers == []
    monkeypatch.setattr(cluster, "kubectl", lambda *a, **k: (1, "", "down"))
    with pytest.raises(harness.HarnessError, match="does not list"):
        cluster.await_listed(["timescale/timescaledb-ha:t@sha256:aa"], timeout=-1)


def test_resume_skips_only_clean_valid_records(tmp_path):
    from tsupgrade.evidence import build_record, record_path, write_record

    def record(dirty):
        return build_record(
            transition=RUN.transition, mode="empty",
            images=[{"reference": RUN.target_image, "digest": "sha256:bb"}],
            pg_version="15.18", source=RUN.source, target=RUN.target, reached=RUN.target,
            passed=True, log="verify/out/x.log", upgrader={"commit": "abc", "dirty": dirty})

    unchanged = lambda *args: ""  # noqa: E731 - git log: nothing since
    assert not harness.recorded(RUN, "empty", tmp_path, git=unchanged)
    write_record(record_path(tmp_path, RUN.transition, "empty"), record(True))
    assert not harness.recorded(RUN, "empty", tmp_path, git=unchanged)
    write_record(record_path(tmp_path, RUN.transition, "empty"), record(False))
    assert harness.recorded(RUN, "empty", tmp_path, git=unchanged)
    assert not harness.recorded(RUN, "seeded", tmp_path, git=unchanged)
    # A clean record from before a change to apply.py does not count.
    assert not harness.recorded(RUN, "empty", tmp_path, git=lambda *args: "abc1234")


# --- history and controls ---------------------------------------------------

LATE = harness.resolve(_row("15", "2.27", "2.28"), PG15)       # one image
BRIDGED = harness.resolve(_row("15", "2.12", "2.28"), PG15)    # two images


def test_history_keeps_the_row_and_names_its_records():
    run = harness.with_history(LATE, "2.13.1", PG15)
    assert isinstance(run, harness.Run)
    assert (run.history, run.source, run.target) == ("2.13.1", "2.27.2", "2.28.3")
    assert (run.mode("empty"), run.key("seeded")) == ("history-empty", "history-seeded")
    assert (LATE.mode("seeded"), LATE.key("seeded")) == ("seeded", "seeded")


@pytest.mark.parametrize("version,reason", [
    ("2.13", "exact release"),          # a series pins nothing
    ("2.27.2", "not below source"),     # no history at all
    ("2.28.0", "not below source"),
    ("2.12.1", "does not ship"),       # below the image's floor
])
def test_history_must_be_an_older_release_the_start_image_ships(version, reason):
    assert reason in harness.with_history(LATE, version, PG15).reason


def test_history_is_staged_on_the_start_image():
    # The bridged row starts on pg15.13, which ships 2.9.0 but not 2.10.0.
    assert isinstance(harness.with_history(BRIDGED, "2.9.0", PG15), harness.Run)
    assert "does not ship 2.10.0" in harness.with_history(BRIDGED, "2.10.0", PG15).reason


def test_a_direct_control_needs_a_history_and_one_image():
    assert "only recorded with --history" in harness.as_control(LATE, "direct").reason
    run = harness.as_control(harness.with_history(LATE, "2.13.1", PG15), "direct")
    assert run.control == "direct"
    assert run.key("seeded") == "direct-history-seeded"
    assert run.mode("seeded") == "history-seeded"
    bridged = harness.as_control(harness.with_history(BRIDGED, "2.9.0", PG15), "direct")
    assert "needs one image" in bridged.reason


def test_a_failed_direct_update_is_judged_as_a_failure_in_the_update():
    run = harness.as_control(harness.with_history(LATE, "2.13.1", PG15), "direct")
    passed, _, error, stage = harness.judge(
        run, "empty", 3, "ERROR:  cannot drop table _timescaledb_internal.bgw_job_stat_history",
        None, "2.27.2", None)
    assert (passed, stage) == (False, "apply")
    assert "bgw_job_stat_history" in error


def test_resume_finds_history_and_control_records_by_their_key(tmp_path):
    from tsupgrade.evidence import build_record, record_path, write_record

    run = harness.as_control(harness.with_history(LATE, "2.13.1", PG15), "direct")
    doc = build_record(
        transition=run.transition, mode="history-empty", history="2.13.1", control="direct",
        images=[{"reference": run.target_image, "digest": "sha256:bb"}],
        pg_version="15.18", source=run.source, target=run.target, reached=run.source,
        passed=False, explanation="the direct update failed", error="ERROR: x",
        log="verify/out/x.log", upgrader={"commit": "abc", "dirty": False})
    write_record(record_path(tmp_path, run.transition, "direct-history-empty"), doc)
    assert harness.recorded(run, "empty", tmp_path, git=lambda *a: "")
    assert not harness.recorded(harness.with_history(LATE, "2.13.1", PG15), "empty", tmp_path, git=lambda *a: "")


# --- major changes ----------------------------------------------------------

XPG = ImageContents({"repository": REPO, "images": {
    "pg12.22-ts2.11.2": {"digest": "sha256:a1", "pg": {"12": {
        "server_version": "12.22", "installable": ["2.1.0", "2.11.2"]}}},
    "pg13.12-ts2.11.2-all": {"digest": "sha256:a2", "pg": {
        "12": {"server_version": "12.16", "installable": ["2.1.0", "2.11.2"]},
        "13": {"server_version": "13.12", "installable": ["2.1.0", "2.11.2"]}}},
    "pg13.22-ts2.15.3": {"digest": "sha256:a3", "pg": {"13": {
        "server_version": "13.22", "installable": ["2.1.0", "2.11.2", "2.15.3"]}}},
    "pg16.15-ts2.30.1": {"digest": "sha256:a4", "pg": {"16": {
        "server_version": "16.15", "installable": ["2.13.0", "2.30.1"]}}},
}})


def test_a_major_change_starts_on_the_source_major_and_offers_every_image_between():
    run = harness.resolve(_row("12", "2.11", "2.15", target_pg="13"), XPG)
    assert isinstance(run, harness.Run) and run.cross
    assert (run.start_image, run.source) == (REPO + ":pg12.22-ts2.11.2", "2.11.2")
    assert (run.target_image, run.target) == (REPO + ":pg13.22-ts2.15.3", "2.15.3")
    assert REPO + ":pg13.12-ts2.11.2-all" in run.candidates
    assert REPO + ":pg16.15-ts2.30.1" not in run.candidates  # outside 12..13
    assert run.slug == "pg12/2.11-to-pg13-2.15"


def test_select_rows_takes_major_changes_only_when_asked():
    rows = [_row("12", "2.11", "2.11"), _row("12", "2.11", "2.15", target_pg="13"),
            _row("12", "2.11", "2.30", target_pg="16")]
    assert [r.target_pg for r in harness.select_rows(rows)] == ["12"]
    assert [r.target_pg for r in harness.select_rows(rows, to_pgs=["13", "16"])] == ["13", "16"]


def test_link_is_only_for_a_major_change_and_names_its_records():
    run = harness.with_link(harness.resolve(_row("12", "2.11", "2.15", target_pg="13"), XPG))
    assert run.link and run.key("seeded") == "link-seeded"
    assert "major change" in harness.with_link(RUN).reason


def test_images_used_includes_the_pg_upgrade_and_restart_images():
    run = harness.resolve(_row("12", "2.11", "2.15", target_pg="13"), XPG)
    plan = {"steps": [
        {"action": "major_upgrade", "image": REPO + ":pg13.12-ts2.11.2-all",
         "run_image": REPO + ":pg13.22-ts2.15.3"},
        {"action": "ensure_image", "image": REPO + ":pg13.22-ts2.15.3"}]}
    assert harness.images_used(run, plan) == [
        REPO + ":pg12.22-ts2.11.2", REPO + ":pg13.12-ts2.11.2-all", REPO + ":pg13.22-ts2.15.3"]


def test_containerd_spells_out_docker_hubs_registry_and_library():
    assert harness.containerd_name("timescale/timescaledb-ha:pg16") == "docker.io/timescale/timescaledb-ha:pg16"
    assert harness.containerd_name("postgres:16") == "docker.io/library/postgres:16"
    assert harness.containerd_name("ghcr.io/x/y:1") == "ghcr.io/x/y:1"


def test_a_record_names_the_route_its_plan_took_pinned():
    digest = "sha256:" + "0" * 64

    class Contents:
        def entry(self, ref):
            return {"digest": digest}
    plan = {"steps": [
        {"action": "preflight", "image": RUN.target_image},
        {"action": "ensure_image", "image": RUN.target_image, "digest": digest},
        {"action": "update_extension", "from": RUN.source, "to": RUN.target}]}
    assert harness.record_route(RUN, plan, Contents()) == {
        "start": {"image": RUN.start_image, "digest": digest},
        "steps": [{"action": "update", "image": RUN.target_image, "digest": digest, "to": RUN.target}]}
    # Unpinned somewhere: no route, rather than one plan would never reproduce.
    plan["steps"][1]["digest"] = None
    assert harness.record_route(RUN, plan, Contents()) is None
    assert harness.record_route(RUN, None, Contents()) is None


def _record_doc():
    from tsupgrade.evidence import build_record
    return build_record(
        transition=RUN.transition, mode="empty",
        images=[{"reference": RUN.target_image, "digest": "sha256:bb"}],
        pg_version="15.18", source=RUN.source, target=RUN.target, reached=RUN.target,
        passed=True, log="verify/out/x.log", upgrader={"commit": "abc", "dirty": False})


def test_routes_are_written_only_where_every_record_agrees(tmp_path):
    import json
    digest = "sha256:" + "0" * 64
    route = {"start": {"image": "x:a", "digest": digest},
             "steps": [{"action": "update", "image": "x:a", "digest": digest, "to": "2.30.1"}]}
    other = dict(route, start={"image": "x:b", "digest": digest})

    def entry(name, routes):
        evidence = {}
        for key, r in zip(("empty", "seeded"), routes):
            path = tmp_path / "{0}.{1}.json".format(name, key)
            path.write_text(json.dumps(dict(_record_doc(), route=r) if r else _record_doc()))
            evidence[key] = path.name
        return MatrixEntry("16", name, "16", "2.30", verdict="in_place", verified=True,
                           evidence=evidence)
    rows = [entry("2.13", [route, route]), entry("2.14", [route, other]), entry("2.15", [route, None]),
            MatrixEntry("16", "2.16", "16", "2.30")]
    routes, problems = harness.routes_for(rows, root=tmp_path)
    assert routes == {0: route}
    assert any("different routes" in p for p in problems)
    assert any("not every record" in p and "seeded" in p for p in problems)


# --- forward, then rollback --------------------------------------------------

CROSS = harness.resolve(_row("12", "2.11", "2.15", target_pg="13"), XPG)


def test_rollback_runs_are_copy_mode_major_changes_that_end_at_the_source():
    run = harness.with_rollback(CROSS)
    assert run.rollback and run.key("empty") == "rollback-empty"
    assert run.end("seeded") == run.source and CROSS.end("seeded") == CROSS.target
    assert "major change" in harness.with_rollback(RUN).reason
    assert "copy" in harness.with_rollback(harness.with_link(CROSS)).reason


def test_a_rollback_run_is_judged_on_the_rollback_and_the_source():
    run = harness.with_rollback(CROSS)
    report = {"result": "pass"}
    ok = harness.judge(run, "empty", 0, "", report, run.source, None, back={"result": "pass"})
    assert ok[0] is True
    failed = harness.judge(run, "empty", 0, "", report, run.target, None,
                           back={"result": "fail", "failure": {"summary": "swap failed",
                                                               "error": ["ERROR: x"]}})
    assert failed[0] is False and failed[3] == "rollback" and "swap failed" in failed[1]
    still_new = harness.judge(run, "empty", 0, "", report, run.target, None, back={"result": "pass"})
    assert still_new[0] is False and "not {0}".format(run.source) in still_new[1]


def test_a_route_runs_migrating_old_aggregates_unless_it_has_to_stop():
    assert "--migrate-caggs" in harness.apply_arguments(CROSS, "p", "r", "ctx")
    run = harness.with_caggs_refused(CROSS)
    assert "--migrate-caggs" not in harness.apply_arguments(run, "p", "r", "ctx")
    assert (run.mode("seeded"), run.end("seeded")) == ("caggs-refused-seeded", CROSS.source)
    assert isinstance(harness.with_caggs_refused(harness.with_link(CROSS)), harness.Unrunnable)


def test_without_migrate_caggs_an_empty_database_has_nothing_to_stop_for():
    run = harness.with_caggs_refused(CROSS)
    assert (run.must_stop("empty"), run.must_stop("seeded")) == (False, True)
    assert (run.mode("empty"), run.end("empty")) == ("caggs-refused-empty", CROSS.target)
    assert harness.judge(run, "empty", 0, "", {"result": "pass"}, run.target, None)[0] is True
    # Stopping where there was nothing to stop for is the failure here.
    stopped = harness.judge(run, "empty", 2, "refused: nothing was written\n", None, run.source, None)
    assert stopped[0] is False and stopped[3] == "apply"


def test_a_cagg_refusal_passes_only_if_it_stopped_and_said_what_it_found():
    run = harness.with_caggs_refused(CROSS)
    said = ("refused: nothing was written\nThis comes from a read-only check. Nothing has been changed.\n"
            "  verify: public.fx_hourly\n    built on public.fx_metrics\nrun apply again with --migrate-caggs")
    args = (run, "seeded", 3)
    assert harness.judge(*args, said, None, run.source, ALL_PASS, pg_version=run.start_pg)[0] is True
    assert harness.judge(*args, "refused", None, run.source, ALL_PASS, pg_version=run.start_pg)[0] is False
    assert harness.judge(*args, said, {"result": "fail"}, run.source, ALL_PASS, pg_version=run.start_pg)[0] is False
    assert harness.judge(*args, said, None, run.source, ALL_PASS, pg_version="13.22")[0] is False


# --- measured times ---------------------------------------------------------

def test_a_stage_is_timed_even_when_it_raises(monkeypatch):
    clock = iter(["2026-11-25T10:00:00Z", "2026-11-25T10:00:16Z"])
    monkeypatch.setattr(harness, "_now", lambda: next(clock))
    timings = {}
    with pytest.raises(RuntimeError):
        with harness.timed(timings, "forward"):
            raise RuntimeError("apply crashed")
    assert timings == {"forward": {"started_at": "2026-11-25T10:00:00Z",
                                   "finished_at": "2026-11-25T10:00:16Z"}}


def test_measured_times_come_from_passing_timed_records_only(tmp_path):
    from tsupgrade.evidence import build_record, record_path, write_record

    def record(row, mode, seconds, passed=True, rollback=None, **extra):
        timings = {"forward": {"started_at": "2026-11-25T10:00:00Z",
                               "finished_at": "2026-11-25T10:{0:02d}:{1:02d}Z".format(*divmod(seconds, 60))}}
        if rollback is not None:
            timings["rollback"] = {"started_at": "2026-11-25T11:00:00Z",
                                   "finished_at": "2026-11-25T11:00:{0:02d}Z".format(rollback)}
        doc = build_record(
            transition=row.transition, mode=mode,
            images=[{"reference": row.target_image, "digest": "sha256:bb"}],
            pg_version="15.18", source=row.source, target=row.target,
            reached=row.target if passed else None, passed=passed, log="verify/out/x.log",
            explanation=None if passed else "apply failed", error=None if passed else "boom",
            upgrader={"commit": "abc", "dirty": False}, timings=timings, **extra)
        write_record(record_path(tmp_path, row.transition, mode), doc)

    record(RUN, "seeded", 16)
    record(LATE, "seeded", 11)
    record(BRIDGED, "seeded", 600, passed=False)          # a failure is no measure
    record(BRIDGED, "empty", 20)
    write_record(record_path(tmp_path, LATE.transition, "empty"), build_record(  # no timings
        transition=LATE.transition, mode="empty",
        images=[{"reference": LATE.target_image, "digest": "sha256:bb"}], pg_version="15.18",
        source=LATE.source, target=LATE.target, reached=LATE.target, passed=True,
        log="verify/out/x.log", upgrader={"commit": "abc", "dirty": False}))
    assert harness.measured_times(tmp_path) == [
        ("empty, same major", 1, "20 (20-20)", "-"),
        ("seeded, same major", 2, "13.5 (11-16)", "-"),
    ]


def test_the_readme_times_are_computed_from_the_records():
    """Every row of the README's measured-times tables is one --times prints
    from verify/evidence/ as it is now."""
    readme = (_PATH.parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Measured times", 1)[1].split("\n## ", 1)[0]
    rows = [l for l in section.splitlines() if l.startswith("| ") and not l.startswith("| Run |")]
    assert rows, "no measured-times table in the README"
    printed = set(harness.times_lines())
    assert [r for r in rows if r not in printed] == []


def test_route_shape_counts_images_on_one_major_and_pg_upgrades_across():
    start = {"image": "x:pg15.13-ts2.20.3", "digest": "sha256:aa"}
    update = lambda image: {"action": "update", "image": image, "to": "2.28.3"}  # noqa: E731
    hop = {"action": "major_upgrade", "image": "x:pg16.15-ts2.30.1"}
    assert harness.route_shape({"route": {"start": start, "steps": [update(start["image"])]}})[1] == "one image"
    assert harness.route_shape({"route": {"start": start, "steps": [update("x:pg15.18-ts2.28.3")]}})[1] == "2 images"
    assert harness.route_shape({"route": {"start": start, "steps": [hop, hop]}})[1] == "2 pg_upgrades"
    assert harness.route_shape({})[1] == "route not recorded"


def test_an_import_is_waited_for_until_the_node_sees_it(monkeypatch):
    monkeypatch.setenv("HARNESS_BASH", "bash")
    cluster = harness.Cluster("tsdb-verify", 2, PG15)
    clock = iter(range(0, 10000, 5))
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    answers = iter([False, False, True])
    monkeypatch.setattr(cluster, "loaded", lambda image: next(answers))
    assert cluster.await_loaded("x:pg15.18-ts2.28.3")          # seen on the third look
    monkeypatch.setattr(cluster, "loaded", lambda image: False)
    assert not cluster.await_loaded("x:pg15.18-ts2.28.3", timeout=60)  # never: still a failure


def test_a_size_run_is_a_plain_seeded_run_keyed_by_its_rows():
    big = harness.with_devices(LATE, 500)
    assert big.rows == 5040000
    assert big.mode("seeded") == "scale-seeded" and big.key("seeded") == "scale-seeded-5040000"
    assert isinstance(harness.with_devices(LATE, 5), harness.Unrunnable)
    assert isinstance(harness.with_devices(harness.replace(LATE, history="2.27.0"), 500), harness.Unrunnable)
    assert isinstance(harness.with_devices(harness.replace(LATE, rollback=True), 500), harness.Unrunnable)
    assert harness.psql_timeout(LATE) == 600
    assert harness.psql_timeout(big) > harness.psql_timeout(LATE)


def test_the_size_table_sets_a_rows_size_runs_beside_its_usual_one(tmp_path):
    from tsupgrade.evidence import build_record, record_path, write_record

    def record(run, mode, seconds, seed=None):
        extra = {"seed": seed} if seed else {}
        doc = build_record(
            transition=run.transition, mode=mode,
            images=[{"reference": run.target_image, "digest": "sha256:bb"}],
            pg_version="15.18", source=run.source, target=run.target, reached=run.target,
            passed=True, log="verify/out/x.log", upgrader={"commit": "abc", "dirty": False},
            timings={"forward": {"started_at": "2026-11-25T10:00:00Z",
                                 "finished_at": "2026-11-25T10:{0:02d}:{1:02d}Z".format(*divmod(seconds, 60))}},
            route={"start": {"image": run.target_image, "digest": "sha256:" + "b" * 64},
                   "steps": [{"action": "update", "image": run.target_image,
                              "digest": "sha256:" + "b" * 64, "to": run.target}]},
            **extra)
        key = harness.evidence_key(mode, rows=(seed or {}).get("rows"))
        write_record(record_path(tmp_path, run.transition, key), doc)

    record(LATE, "seeded", 15)
    record(LATE, "scale-seeded", 40, {"devices": 500, "rows": 5040000})
    record(RUN, "seeded", 16)                      # no size run: not in the table
    assert harness.size_times(tmp_path) == [
        ("pg15 2.27 -> 2.28", "one image", "-", "50,400", 15),
        ("pg15 2.27 -> 2.28", "one image", "-", "5,040,000", 40),
    ]
