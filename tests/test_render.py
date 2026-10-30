"""Tests for the Markdown rendering of the matrix.

What these defend: a verified row shows the runs that prove it -- both
records, their results and the PostgreSQL minor -- and a record that has gone
missing shows up as missing instead of quietly vanishing from the table.
"""

from __future__ import annotations

import json

from tsupgrade.matrix import Matrix, MatrixEntry
from tsupgrade.render import reference_label, render_markdown

EVIDENCE = {"empty": "ev/2.20-to-2.28.empty.json", "seeded": "ev/2.20-to-2.28.seeded.json"}


def _record(mode, result, stage="done"):
    doc = {
        "evidence_format": 1,
        "transition": {"source_pg": "15", "source_ts": "2.20", "target_pg": "15", "target_ts": "2.28"},
        "mode": mode,
        "images": [{"reference": "timescale/timescaledb-ha:pg15.18-ts2.28.3", "digest": "sha256:" + "a" * 64}],
        "pg_version": "15.18",
        "versions": {"source": "2.20.3", "target": "2.28.3", "reached": "2.28.3"},
        "upgrader": {"commit": "0" * 40, "dirty": False},
        "result": result,
        "log": "verify/out/x.log",
        "recorded_at": "2026-10-30T00:00:00Z",
        "stage": stage,
    }
    if result == "fail":
        doc["failure"] = {"explanation": "fixture checks failed", "error": "check|x|fail|"}
    return doc


def _write(tmp_path, mode, doc):
    path = tmp_path / EVIDENCE[mode]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _entry(**kwargs):
    base = dict(verdict="in_place", verified=True, evidence=EVIDENCE,
                references=("https://github.com/timescale/timescaledb/issues/10272",))
    base.update(kwargs)
    return MatrixEntry("15", "2.20", "15", "2.28", **base)


def _row(text):
    return next(line for line in text.splitlines() if line.startswith("| `15` | `2.20`"))


def test_a_verified_row_shows_its_pg_minor_records_and_references(tmp_path):
    _write(tmp_path, "empty", _record("empty", "pass"))
    _write(tmp_path, "seeded", _record("seeded", "pass"))
    row = _row(render_markdown(Matrix([_entry()]), root=tmp_path))
    assert "| 15.18 |" in row
    assert "[empty: pass](../ev/2.20-to-2.28.empty.json)" in row
    assert "[seeded: pass](../ev/2.20-to-2.28.seeded.json)" in row
    assert "[#10272](https://github.com/timescale/timescaledb/issues/10272)" in row


def test_a_failed_and_a_missing_record_are_both_shown(tmp_path):
    _write(tmp_path, "empty", _record("empty", "fail", stage="check"))
    row = _row(render_markdown(Matrix([_entry()]), root=tmp_path))
    assert "empty: fail" in row
    assert "seeded: missing" in row


def test_an_unverified_row_has_no_evidence_cells():
    entry = MatrixEntry("15", "2.20", "15", "2.28")
    row = _row(render_markdown(Matrix([entry])))
    assert "unknown" in row
    assert row.count("—") >= 3


def test_steps_are_listed_under_their_transition(tmp_path):
    text = render_markdown(Matrix([_entry(steps=("stop at 2.27.2",))]), root=tmp_path)
    assert "### pg15/ts2.20 -> pg15/ts2.28" in text
    assert "1. stop at 2.27.2" in text


def test_reference_labels():
    assert reference_label("https://github.com/timescale/timescaledb/pull/10278") == "#10278"
    assert reference_label(
        "https://github.com/timescale/timescaledb/releases/tag/2.29.0") == "2.29.0 release"
    assert reference_label(
        "https://registry-1.docker.io/v2/timescale/timescaledb-ha/tags/list?n=10000") == "registry"
    assert reference_label("https://www.tigerdata.com/docs/x") == "docs"
