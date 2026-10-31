"""Tests for plan steps as types: the one place a step's JSON is read."""

from __future__ import annotations

import json

from tsupgrade.drift import ImageProfile
from tsupgrade.plan import build_plan
from tsupgrade.steps import (
    ACTIONS,
    EnsureImage,
    MajorUpgrade,
    PgUpgradeCheck,
    Preflight,
    UpdateExtension,
    parse_steps,
)

PG16 = ImageProfile("timescale/timescaledb-ha:pg16.15-ts2.30.1", (16,), "2.13.0", "2.30.1", "2.30.1")


def test_every_action_plan_writes_parses_into_its_type():
    plan = json.loads(json.dumps(build_plan("16", "2.13.0", PG16, verification_run=True)))
    steps, reasons = parse_steps(plan["steps"])
    assert reasons == []
    assert [type(s).__name__ for s in steps] == [
        "Preflight", "EnsureImage", "UpdateExtension", "CycleSessions", "VerifyVersion"]
    update = steps[2]
    assert isinstance(update, UpdateExtension)
    assert (update.from_version, update.to) == ("2.13.0", "2.30.1")


def test_a_major_upgrade_and_its_preflight_parse_with_every_field():
    raw = [
        {"hop": 1, "action": "preflight", "image": "x:run", "target_pg": "13.22", "target_ts": "2.11.2",
         "pg_upgrade": {"from_pg": 12, "to_pg": 13, "link": True, "image": "x:both"}},
        {"hop": 1, "action": "major_upgrade", "image": "x:both", "digest": "sha256:" + "a" * 64,
         "from_pg": 12, "to_pg": 13, "ts": "2.11.2", "run_image": "x:run", "link": True},
    ]
    steps, reasons = parse_steps(raw)
    assert reasons == []
    assert steps[0] == Preflight(1, "x:run", "13.22", "2.11.2", PgUpgradeCheck(12, 13, True, "x:both"))
    assert steps[1] == MajorUpgrade(1, "x:both", 12, 13, "2.11.2", "x:run", True, "sha256:" + "a" * 64)


def test_an_unknown_action_fails_by_name_and_no_steps_come_back():
    """apply used to run an unknown action as verify_version, in an else."""
    steps, reasons = parse_steps([{"action": "ensure_image", "image": "x"}, {"action": "drop_database"}])
    assert steps == []
    assert reasons == ["steps[1]: unknown action 'drop_database'"]
    assert "drop_database" not in ACTIONS


def test_every_problem_is_reported_not_only_the_first():
    steps, reasons = parse_steps([
        {"action": "ensure_image", "image": "x", "digest": "latest"},
        {"action": "update_extension", "to": "2.30.1", "sql": "DROP EXTENSION timescaledb;"},
        "not a step",
    ])
    assert steps == []
    assert len(reasons) == 3
    assert "digest is not a sha256 digest" in reasons[0]
    assert "sql is not the one statement" in reasons[1]


def test_steps_are_immutable_values():
    step = EnsureImage(1, "x")
    assert step == EnsureImage(1, "x", None)
    assert step.action == "ensure_image"
