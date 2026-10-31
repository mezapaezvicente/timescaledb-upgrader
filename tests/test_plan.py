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


def _digest(profile):
    return "sha256:" + format(abs(hash(profile.reference)), "x").rjust(64, "0")[:64]


DIGESTS = {p.reference: _digest(p) for p in (PG16, PG15_20, PG15_28)}


def _update(profile, to):
    return {"action": "update", "image": profile.reference, "digest": DIGESTS[profile.reference],
            "to": to}


def _entry(src_pg, src_ts, tgt_pg, tgt_ts, verdict=None, verified=False, route=None):
    return MatrixEntry(
        src_pg, src_ts, tgt_pg, tgt_ts,
        verdict=verdict,
        verified=verified,
        evidence={"empty": "e.json", "seeded": "s.json"} if verified else None,
        route=route,
    )


PG16_ROUTE = {"start": {"image": PG16.reference, "digest": DIGESTS[PG16.reference]},
              "steps": [_update(PG16, "2.30.1")]}
KNOWN = Matrix([_entry("16", "2.13", "16", "2.30", "in_place", True, PG16_ROUTE)])
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
    plan = build_plan("16", "2.13.0", PG16, matrix=KNOWN, digests=DIGESTS)
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
    plan = build_plan("16", "2.13.0", PG16, matrix=KNOWN, digests=DIGESTS)
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
    normal = render_runbook(build_plan("16", "2.13.0", PG16, matrix=KNOWN, digests=DIGESTS))
    verifying = render_runbook(build_plan("16", "2.13.0", PG16, matrix=EMPTY, verification_run=True))
    assert "VERIFICATION RUN" not in normal
    assert verifying.startswith("!")
    assert "VERIFICATION RUN" in verifying


# --------------------------------------------------------------------------
# The verified route, pinned (R1)
# --------------------------------------------------------------------------

# The pg15 bridge row as verified: 2.20.3 on the 2.20 image, then the 2.27.2
# stop and 2.28.3 on the 2.28 image.
BRIDGE_ROUTE = {
    "start": {"image": PG15_20.reference, "digest": DIGESTS[PG15_20.reference]},
    "steps": [_update(PG15_20, "2.20.3"), _update(PG15_28, "2.27.2"), _update(PG15_28, "2.28.3")],
}
BRIDGE = Matrix([_entry("15", "2.9", "15", "2.28", "in_place", True, BRIDGE_ROUTE)])
PG15_19 = ImageProfile(REPO + "pg15.13-ts2.19.3", (15,), "2.9.0", "2.19.3", "2.19.3")


def test_the_verified_route_is_planned_pinned_and_says_so():
    plan = build_plan("15", "2.9.0", PG15_28, [PG15_20], matrix=BRIDGE, digests=DIGESTS)
    assert plan["matrix"]["route_verified"] is True
    rollouts = [s for s in plan["steps"] if s["action"] == "ensure_image"]
    assert [s["digest"] for s in rollouts] == [DIGESTS[PG15_20.reference]] + [DIGESTS[PG15_28.reference]] * 2
    runbook = render_runbook(plan)
    assert "this is the route its records ran" in runbook
    assert "{0}@{1}".format(PG15_20.reference, DIGESTS[PG15_20.reference]) in runbook


def test_a_route_through_other_images_is_not_reported_as_verified():
    """The review's reproduction, same-major: the endpoints match the row,
    the images in between do not."""
    digests = dict(DIGESTS, **{PG15_19.reference: _digest(PG15_19)})
    refused = _refusal(source_pg="15", source_ts="2.9.0", target_profile=PG15_28,
                       candidates=[PG15_19], matrix=BRIDGE, digests=digests)
    assert refused.only_unestablished
    assert "not the verified one" in str(refused) and "pg15.13-ts2.19.3" in str(refused)


def test_verification_run_off_the_verified_route_warns_and_says_unverified():
    digests = dict(DIGESTS, **{PG15_19.reference: _digest(PG15_19)})
    plan = build_plan("15", "2.9.0", PG15_28, [PG15_19], matrix=BRIDGE, digests=digests,
                      verification_run=True)
    assert plan["matrix"]["route_verified"] is False
    assert any("not the verified one" in w for w in plan["warnings"])
    assert "verified, but not for this route" in render_runbook(plan)


def test_a_different_digest_is_a_different_route():
    """A re-pushed tag: same name, other content, not what was verified."""
    repushed = dict(DIGESTS, **{PG15_28.reference: "sha256:" + "f" * 64})
    refused = _refusal(source_pg="15", source_ts="2.9.0", target_profile=PG15_28,
                       candidates=[PG15_20], matrix=BRIDGE, digests=repushed)
    assert refused.only_unestablished
    assert "sha256:" + "f" * 64 in str(refused)


def test_a_verified_row_without_a_recorded_route_is_unestablished():
    matrix = Matrix([_entry("16", "2.13", "16", "2.30", "in_place", True)])
    refused = _refusal(source_pg="16", source_ts="2.13.0", target_profile=PG16,
                       matrix=matrix, digests=DIGESTS)
    assert refused.only_unestablished
    assert "before routes were recorded" in str(refused)


def test_an_image_without_a_measured_digest_runs_unpinned_with_a_warning():
    plan = build_plan("16", "2.13.0", PG16, matrix=EMPTY, verification_run=True)
    assert [s.get("digest") for s in plan["steps"] if s["action"] == "ensure_image"] == [None]
    assert any("no measured digest" in w for w in plan["warnings"])


# --------------------------------------------------------------------------
# Two-image rows
# --------------------------------------------------------------------------

def test_stranded_source_is_bridged_through_an_older_image():
    """The pg15 2.9 -> 2.28 row: the 2.28 image cannot load 2.9 at all."""
    plan = build_plan(
        "15", "2.9.0", PG15_28, [PG15_20], matrix=EMPTY, verification_run=True
    )
    assert plan["hops"] == 3
    updates = [s for s in plan["steps"] if s["action"] == "update_extension"]
    assert [(u["from"], u["to"]) for u in updates] == [
        ("2.9.0", "2.20.3"), ("2.20.3", "2.27.2"), ("2.27.2", "2.28.3"),
    ]
    images = [s["image"] for s in plan["steps"] if s["action"] == "ensure_image"]
    assert images == [PG15_20.reference, PG15_28.reference, PG15_28.reference]


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
        ("15.13", "2.20.3"), ("15.18", "2.27.2"), ("15.18", "2.28.3"),
    ]


# --------------------------------------------------------------------------
# Known-bad updates: stop short inside the same image
# --------------------------------------------------------------------------

PG12_11 = ImageProfile(
    REPO + "pg12.22-ts2.11.2", (12,), "1.7.5", "2.11.2", "2.11.2",
    installable=("1.7.5",) + tuple("2.{0}.0".format(m) for m in range(1, 11))
    + ("2.1.1", "2.10.1", "2.10.2", "2.10.3", "2.11.0", "2.11.1", "2.11.2"),
)


def _updates(plan):
    return [(s["from"], s["to"]) for s in plan["steps"] if s["action"] == "update_extension"]


@pytest.mark.parametrize("source", ["2.15.0", "2.20.3", "2.24.0"])
def test_unknown_history_source_stops_at_2_27_2_before_2_28_3(source):
    """#10272: the planner cannot see whether the source was updated across 2.15."""
    plan = build_plan("15", source, PG15_28, matrix=EMPTY, verification_run=True)
    assert _updates(plan) == [(source, "2.27.2"), ("2.27.2", "2.28.3")]
    images = [s["image"] for s in plan["steps"] if s["action"] == "ensure_image"]
    assert images == [PG15_28.reference, PG15_28.reference]
    assert any("10272" in w and "2.27.2" in w for w in plan["warnings"])


@pytest.mark.parametrize("source", ["2.13.0", "2.14.2", "2.25.0", "2.27.2"])
def test_sources_outside_the_bad_range_go_straight_to_2_28_3(source):
    plan = build_plan("15", source, PG15_28, matrix=EMPTY, verification_run=True)
    assert _updates(plan) == [(source, "2.28.3")]


def test_1x_to_2_10_2_or_later_stops_at_2_1_1():
    """#5789: 1.7.5 -> 2.11.2 in one ALTER fails on ping_data_node."""
    plan = build_plan("12", "1.7.5", PG12_11, matrix=EMPTY, verification_run=True)
    assert _updates(plan) == [("1.7.5", "2.1.1"), ("2.1.1", "2.11.2")]


def test_1x_below_2_10_2_needs_no_stop():
    plan = build_plan(
        "12", "1.7.5", PG12_11, matrix=EMPTY, target_ts="2.10.1", verification_run=True
    )
    assert _updates(plan) == [("1.7.5", "2.10.1")]


def test_stop_the_image_does_not_ship_blocks_the_plan():
    no_stop = ImageProfile(
        REPO + "pg15.18-ts2.28.3", (15,), "2.13.0", "2.28.3", "2.28.3",
        installable=("2.20.3", "2.28.3"),
    )
    refused = _refusal(
        source_pg="15", source_ts="2.20.3", target_profile=no_stop,
        matrix=EMPTY, verification_run=True,
    )
    assert refused.refusals[0].kind == BLOCKED
    assert "2.27.2" in str(refused) and "10272" in str(refused)


def test_the_stop_says_why_in_the_plan_and_the_runbook():
    plan = build_plan("15", "2.20.3", PG15_28, matrix=EMPTY, verification_run=True)
    first, second = [s for s in plan["steps"] if s["action"] == "update_extension"]
    assert "issues/10272" in first["why"]
    assert "why" not in second
    assert "stops here because 2.20.3 -> 2.28.3 directly" in render_runbook(plan)


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


def test_cli_refuses_via_outside_a_verification_run(offline_profiles, tmp_path, capsys):
    code = cli.main(["plan", "--from-pg", "15", "--from-ts", "2.9.0",
                     "--target-image", PG15_28.reference, "--via", PG15_20.reference,
                     "--matrix", _matrix_file(tmp_path, BRIDGE.entries)])
    assert code == cli.EXIT_ERROR
    assert "--verification-run" in capsys.readouterr().err


def test_cli_plans_a_verified_row_over_its_recorded_route(offline_profiles, tmp_path, monkeypatch, capsys):
    """No --via: the images come from the row's route, the digests from the
    measured contents."""
    class Contents:
        def entry(self, ref):
            return {"digest": DIGESTS[ref]} if ref in DIGESTS else None
    monkeypatch.setattr(cli, "_load_contents", lambda path: Contents())
    code = cli.main(["plan", "--from-pg", "15", "--from-ts", "2.9.0", "--json",
                     "--target-image", PG15_28.reference,
                     "--matrix", _matrix_file(tmp_path, BRIDGE.entries)])
    assert code == cli.EXIT_OK
    plan = json.loads(capsys.readouterr().out)
    assert plan["matrix"]["route_verified"] is True
    assert plan["steps"][1] == {"hop": 1, "action": "ensure_image", "image": PG15_20.reference,
                                "digest": DIGESTS[PG15_20.reference]}


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
