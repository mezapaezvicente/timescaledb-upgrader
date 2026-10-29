"""Tests for the planner.

No cluster and no network. Profiles are built by hand with real tag shapes, so
the PostgreSQL-minor check reads the minor from the tag the way it would live.

What these defend: a transition with no established verdict is refused unless
the verification mode is asked for, that mode lifts *only* that refusal, and it
is impossible to miss in the output.
"""

from __future__ import annotations

import json

import pytest

from tsupgrade import cli
from tsupgrade.drift import ImageProfile
from tsupgrade.matrix import Matrix, MatrixEntry
from tsupgrade.plan import (
    BLOCKED,
    NORMAL,
    UNESTABLISHED,
    VERIFICATION,
    PlanRefused,
    build_plan,
    render_runbook,
)

REPO = "timescale/timescaledb-ha:"

PG16 = ImageProfile(REPO + "pg16.15-ts2.30.1", (16,), "2.13.0", "2.30.1", "2.30.1")
PG15_20 = ImageProfile(REPO + "pg15.13-ts2.20.3", (15,), "2.9.0", "2.20.3", "2.20.3")
PG15_28 = ImageProfile(REPO + "pg15.18-ts2.28.3", (15,), "2.13.0", "2.28.3", "2.28.3")


def _entry(src_pg, src_ts, tgt_pg, tgt_ts, verdict=None, verified=False):
    return MatrixEntry(
        src_pg, src_ts, tgt_pg, tgt_ts,
        verdict=verdict,
        verified=verified,
        source_url="https://example.invalid/x" if verified else None,
    )


KNOWN = Matrix([_entry("16", "2.13", "16", "2.30", "in_place", True)])
UNKNOWN_ROW = Matrix([_entry("16", "2.13", "16", "2.30")])
EMPTY = Matrix([])


def _refusal(**kwargs):
    with pytest.raises(PlanRefused) as info:
        build_plan(**kwargs)
    return info.value


# --------------------------------------------------------------------------
# Normal mode
# --------------------------------------------------------------------------

def test_established_in_place_row_is_planned_as_one_hop():
    plan = build_plan("16", "2.13.0", PG16, matrix=KNOWN)
    assert plan["mode"] == NORMAL
    assert plan["hops"] == 1
    assert [s["action"] for s in plan["steps"]] == [
        "preflight", "ensure_image", "update_extension", "cycle_sessions", "verify_version",
    ]
    update = plan["steps"][2]
    assert (update["from"], update["to"]) == ("2.13.0", "2.30.1")
    assert update["sql"] == "ALTER EXTENSION timescaledb UPDATE TO '2.30.1';"


def test_plan_is_plain_json():
    """apply reads it back from disk, so it must round-trip."""
    plan = build_plan("16", "2.13.0", PG16, matrix=KNOWN)
    assert json.loads(json.dumps(plan)) == plan


def test_unverified_row_is_refused_as_unestablished():
    refused = _refusal(source_pg="16", source_ts="2.13.0", target_profile=PG16, matrix=UNKNOWN_ROW)
    assert refused.only_unestablished


def test_missing_row_is_refused_as_unestablished_not_unsupported():
    refused = _refusal(source_pg="16", source_ts="2.13.0", target_profile=PG16, matrix=EMPTY)
    assert refused.only_unestablished
    assert "not the same as unsupported" in str(refused)


@pytest.mark.parametrize("verdict", ["dump_restore", "unsupported", "no_image"])
def test_established_negative_verdict_is_blocked(verdict):
    matrix = Matrix([_entry("16", "2.13", "16", "2.30", verdict, True)])
    refused = _refusal(source_pg="16", source_ts="2.13.0", target_profile=PG16, matrix=matrix)
    assert not refused.only_unestablished


def test_source_must_be_an_exact_release():
    """Whether an image loads the source depends on its patch release."""
    refused = _refusal(source_pg="16", source_ts="2.13", target_profile=PG16, matrix=KNOWN)
    assert refused.refusals[0].kind == BLOCKED


def test_downgrade_is_blocked():
    refused = _refusal(source_pg="16", source_ts="2.31.0", target_profile=PG16, matrix=KNOWN)
    assert "downgrade" in str(refused)


def test_every_reason_is_reported_not_just_the_first():
    old_pg = ImageProfile(REPO + "pg16.4-ts2.30.1", (16,), "2.13.0", "2.30.1", "2.30.1")
    refused = _refusal(source_pg="16", source_ts="2.13.0", target_profile=old_pg, matrix=EMPTY)
    kinds = sorted(r.kind for r in refused.refusals)
    assert kinds == [BLOCKED, UNESTABLISHED]


# --------------------------------------------------------------------------
# Verification mode
# --------------------------------------------------------------------------

@pytest.mark.parametrize("matrix", [UNKNOWN_ROW, EMPTY, None])
def test_verification_run_plans_what_normal_mode_refuses(matrix):
    plan = build_plan("16", "2.13.0", PG16, matrix=matrix, verification_run=True)
    assert plan["mode"] == VERIFICATION
    assert plan["hops"] == 1
    assert any(w.startswith("verification run:") for w in plan["warnings"])


def test_verification_run_can_recheck_a_settled_row_but_says_what_it_was():
    matrix = Matrix([_entry("16", "2.13", "16", "2.30", "dump_restore", True)])
    plan = build_plan("16", "2.13.0", PG16, matrix=matrix, verification_run=True)
    assert any("dump_restore" in w for w in plan["warnings"])


def test_verification_run_covers_a_release_newer_than_the_repository():
    """The case a user hits before this repo is updated for a new release."""
    newer = ImageProfile(REPO + "pg16.16-ts2.31.0", (16,), "2.13.0", "2.31.0", "2.31.0")
    with pytest.raises(PlanRefused):
        build_plan("16", "2.30.1", newer, matrix=KNOWN)
    plan = build_plan("16", "2.30.1", newer, matrix=KNOWN, verification_run=True)
    assert plan["steps"][2]["to"] == "2.31.0"
    assert any("compat.h" in w for w in plan["warnings"])


def test_verification_run_does_not_lift_a_major_change():
    pg17 = ImageProfile(REPO + "pg17.11-ts2.30.1", (17,), "2.17.0", "2.30.1", "2.30.1")
    refused = _refusal(
        source_pg="16", source_ts="2.30.1", target_profile=pg17,
        matrix=EMPTY, verification_run=True,
    )
    assert "PostgreSQL major" in str(refused)


def test_verification_run_does_not_lift_a_refused_pg_minor():
    """The extension would not load; no run can turn that into a result."""
    old_pg = ImageProfile(REPO + "pg16.5-ts2.30.1", (16,), "2.13.0", "2.30.1", "2.30.1")
    refused = _refusal(
        source_pg="16", source_ts="2.13.0", target_profile=old_pg,
        matrix=EMPTY, verification_run=True,
    )
    assert not refused.only_unestablished
    assert "16.6" in str(refused)


def test_runbook_is_loud_in_verification_mode_only():
    normal = render_runbook(build_plan("16", "2.13.0", PG16, matrix=KNOWN))
    verifying = render_runbook(build_plan("16", "2.13.0", PG16, matrix=EMPTY, verification_run=True))
    assert "VERIFICATION RUN" not in normal
    assert verifying.startswith("!")
    assert "VERIFICATION RUN" in verifying


# --------------------------------------------------------------------------
# Two-image rows
# --------------------------------------------------------------------------

def test_stranded_source_is_bridged_through_an_older_image():
    """The pg15 2.9 -> 2.28 row: the 2.28 image cannot load 2.9 at all."""
    plan = build_plan(
        "15", "2.9.0", PG15_28, [PG15_20], matrix=EMPTY, verification_run=True
    )
    assert plan["hops"] == 2
    updates = [s for s in plan["steps"] if s["action"] == "update_extension"]
    assert [(u["from"], u["to"]) for u in updates] == [("2.9.0", "2.20.3"), ("2.20.3", "2.28.3")]
    images = [s["image"] for s in plan["steps"] if s["action"] == "ensure_image"]
    assert images == [PG15_20.reference, PG15_28.reference]


def test_stranded_source_without_candidates_is_blocked():
    refused = _refusal(
        source_pg="15", source_ts="2.9.0", target_profile=PG15_28,
        matrix=EMPTY, verification_run=True,
    )
    assert refused.refusals[0].kind == BLOCKED


def test_each_hop_is_checked_against_its_own_pg_minor():
    """A bridge image on a refused minor blocks the plan even if the target is fine."""
    bad_bridge = ImageProfile(REPO + "pg15.9-ts2.20.3", (15,), "2.9.0", "2.20.3", "2.20.3")
    refused = _refusal(
        source_pg="15", source_ts="2.9.0", target_profile=PG15_28,
        candidates=[bad_bridge], matrix=EMPTY, verification_run=True,
    )
    assert "15.10" in str(refused)


def test_hop_preflight_targets_the_hop_not_the_final_version():
    plan = build_plan("15", "2.9.0", PG15_28, [PG15_20], matrix=EMPTY, verification_run=True)
    preflights = [s for s in plan["steps"] if s["action"] == "preflight"]
    assert [(p["target_pg"], p["target_ts"]) for p in preflights] == [
        ("15.13", "2.20.3"), ("15.18", "2.28.3"),
    ]


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

@pytest.fixture
def offline_profiles(monkeypatch):
    profiles = {p.reference: p for p in (PG16, PG15_20, PG15_28)}
    monkeypatch.setattr(cli, "_profile", lambda ref, *a, **k: profiles[ref])


def _matrix_file(tmp_path, entries):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"entries": [e.to_dict() for e in entries]}))
    return str(path)


def test_cli_exit_code_distinguishes_unestablished_from_blocked(offline_profiles, tmp_path, capsys):
    base = ["plan", "--from-pg", "16", "--target-image", PG16.reference,
            "--matrix", _matrix_file(tmp_path, [])]
    assert cli.main(base + ["--from-ts", "2.13.0"]) == cli.EXIT_UNKNOWN
    assert "--verification-run" in capsys.readouterr().err
    assert cli.main(base + ["--from-ts", "2.13"]) == cli.EXIT_CHECK_FAILED


def test_cli_verification_run_writes_the_plan_and_announces_itself(offline_profiles, tmp_path, capsys):
    out = tmp_path / "plan.json"
    code = cli.main([
        "plan", "--from-pg", "16", "--from-ts", "2.13.0",
        "--target-image", PG16.reference, "--matrix", _matrix_file(tmp_path, []),
        "--verification-run", "--output", str(out),
    ])
    assert code == cli.EXIT_OK
    assert json.loads(out.read_text())["mode"] == VERIFICATION
    captured = capsys.readouterr()
    assert "VERIFICATION RUN" in captured.err
    assert "VERIFICATION RUN" in captured.out
