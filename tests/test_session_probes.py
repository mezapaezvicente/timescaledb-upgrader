"""The session rules the tool prints rest on verify/probes/sessions/.

Each record there is one run of verify/probe_sessions.py. These tests fail if
a record stops showing what the messages in gates.py and plan.py say.
"""

import json
from pathlib import Path

import pytest

from tsupgrade.gates import SESSION_RULES

PROBES = Path(__file__).resolve().parents[1] / "verify" / "probes" / "sessions"
RECORDS = sorted(PROBES.glob("*.json"))
ENDED = '"timescaledb" already loaded with a different version'
REFUSED = "cannot be updated after the old version has already been loaded"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _steps(session):
    return {s["step"]: s["output"] for s in session["steps"]}


def test_there_are_records_for_both_old_and_new_releases():
    sources = {_load(p)["from"] for p in RECORDS}
    assert any(s.startswith("2.5.") for s in sources)
    assert any(s.startswith("2.30.") for s in sources)


@pytest.mark.parametrize("path", RECORDS, ids=[p.stem for p in RECORDS])
def test_an_open_session_keeps_the_old_library_and_is_ended_at_its_next_statement(path):
    record = _load(path)
    assert record["update"]["exit"] == 0
    assert record["replica_caught_up"] is True
    roles = set()
    for session in record["old_sessions"].values():
        roles.add(session["role"])
        old = "timescaledb-{0}.so".format(record["from"])
        assert old in session["libraries_before_update"]["libraries"]
        assert old in session["libraries_after_update"]["libraries"]
        steps = _steps(session)
        assert ENDED in steps["r1.pid"]
        # Ended, not failing on: nothing after the first statement ran.
        assert not any(name.startswith("r2.") for name in steps)
        assert session["exit"] != 0
    assert roles == {"primary", "replica"}


@pytest.mark.parametrize("path", RECORDS, ids=[p.stem for p in RECORDS])
def test_a_fresh_session_works_on_every_instance(path):
    record = _load(path)
    for session in record["fresh_sessions"].values():
        assert session["exit"] == 0
        steps = _steps(session)
        assert steps["extversion"] == record["to"]
        assert not any("ERROR" in out or "FATAL" in out for out in steps.values())


@pytest.mark.parametrize("path", RECORDS, ids=[p.stem for p in RECORDS])
def test_an_update_after_the_library_loaded_is_refused(path):
    record = _load(path)
    same = record["same_session_update"]
    assert REFUSED in _steps(same)["update"]
    assert same["extversion_after"] == record["from"]


def test_the_printed_rules_quote_what_the_records_show():
    assert REFUSED in SESSION_RULES[0]
    assert ENDED in SESSION_RULES[1]
