"""Tests for verification-run evidence records.

What these defend: a record cannot claim a pass it did not reach, a failure
cannot be recorded without its explanation and verbatim error, and the
version facts that decide whether a row is proven (exact PG minor, exact
releases, image digests) cannot be left vague. Everything else is left loose
on purpose -- the failure shapes are not known yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tsupgrade.evidence import (
    EvidenceError,
    build_record,
    evidence_key,
    load_record,
    record_path,
    stage_seconds,
    validate_record,
    write_record,
)

ROW = {"source_pg": "15", "source_ts": "2.13", "target_pg": "15", "target_ts": "2.28"}
IMAGE = {"reference": "timescale/timescaledb-ha:pg15.18-ts2.28.3", "digest": "sha256:f49b"}
REV = {"commit": "abc123", "dirty": False}


def _record(**overrides):
    args = dict(
        transition=ROW, mode="seeded", images=[IMAGE], pg_version="15.18",
        source="2.13.1", target="2.28.3", reached="2.28.3", passed=True,
        log="verify/out/runs/x.log", upgrader=REV,
    )
    args.update(overrides)
    return build_record(**args)


def test_a_passing_run_builds_and_validates():
    doc = _record(checks=["check|rows|pass|..."])
    assert doc["result"] == "pass"
    assert "failure" not in doc
    assert doc["checks"] == ["check|rows|pass|..."]  # extra fields pass through
    assert validate_record(doc) == []


def test_a_failure_needs_its_explanation_and_the_verbatim_error():
    with pytest.raises(EvidenceError, match="failure.explanation"):
        _record(passed=False, reached="2.24.0")
    doc = _record(
        passed=False, reached="2.24.0",
        explanation="the 2.24 -> 2.25 script refused a non-finalized aggregate",
        error="ERROR:  cannot upgrade because of non-finalized continuous aggregates",
    )
    assert doc["failure"]["error"].startswith("ERROR:")


def test_a_pass_must_actually_reach_the_target():
    with pytest.raises(EvidenceError, match="reach its target"):
        _record(reached="2.27.1")


@pytest.mark.parametrize("pg_version", ["15", "", None, "fifteen"])
def test_the_pg_minor_is_mandatory(pg_version):
    with pytest.raises(EvidenceError, match="pg_version"):
        _record(pg_version=pg_version)


@pytest.mark.parametrize("field,value", [("source", "2.13"), ("target", "2.28")])
def test_versions_must_be_exact_releases(field, value):
    with pytest.raises(EvidenceError, match="versions." + field):
        _record(**{field: value})


def test_reached_may_be_null_on_a_failure_that_never_read_it():
    doc = _record(passed=False, reached=None, explanation="plan refused", error="refused: ...")
    assert doc["versions"]["reached"] is None


def test_every_image_needs_a_digest():
    with pytest.raises(EvidenceError, match="digest"):
        _record(images=[IMAGE, {"reference": "timescale/timescaledb-ha:pg15.13-ts2.20.3"}])


def test_mode_is_empty_or_seeded():
    with pytest.raises(EvidenceError, match="mode"):
        _record(mode="both")


def test_a_history_run_names_the_release_it_was_created_at():
    with pytest.raises(EvidenceError, match="history"):
        _record(mode="history-seeded")
    with pytest.raises(EvidenceError, match="history"):
        _record(mode="history-seeded", history="2.13")
    assert _record(mode="history-seeded", history="2.13.1")["history"] == "2.13.1"


def test_a_control_is_marked_and_keyed_apart():
    doc = _record(mode="history-empty", history="2.13.1", control="direct", passed=False,
                  reached="2.20.3", explanation="direct update failed", error="ERROR: ...")
    assert doc["control"] == "direct"
    assert evidence_key(doc["mode"], doc["control"]) == "direct-history-empty"
    assert evidence_key("seeded") == "seeded"
    with pytest.raises(EvidenceError, match="control"):
        _record(control="skip-stops")


CROSS = {"source_pg": "12", "source_ts": "2.11", "target_pg": "13", "target_ts": "2.15"}


def _cross(mode, *hops, **kw):
    return _record(transition=CROSS, mode=mode, pg_version="13.22", source="2.11.2",
                   target="2.15.3", reached="2.15.3", major_upgrades=list(hops), **kw)


def _hop(files, shared):
    return {"from_pg": 12, "to_pg": 13, "files": files, "shared_inodes": shared}


def test_a_passing_major_change_shows_pg_upgrade_copied_or_linked_as_its_mode_says():
    assert _cross("seeded", _hop("copy", 0))["result"] == "pass"
    assert _cross("link-seeded", _hop("link", 159))["result"] == "pass"
    with pytest.raises(EvidenceError, match="lists its pg_upgrade runs"):
        _cross("seeded")
    with pytest.raises(EvidenceError, match="link of the relation files"):
        _cross("link-empty", _hop("copy", 0))
    with pytest.raises(EvidenceError, match="hard-link count"):
        _cross("empty", _hop("copy", None))


def test_the_hard_link_count_must_fit_the_mode():
    with pytest.raises(EvidenceError, match="does not fit a link"):
        _cross("link-empty", _hop("link", 0))
    with pytest.raises(EvidenceError, match="does not fit a copy"):
        _cross("empty", _hop("copy", 3))


def test_a_failed_major_change_need_not_show_its_pg_upgrades():
    doc = _record(transition=CROSS, mode="empty", passed=False, reached="2.11.2",
                  source="2.11.2", target="2.15.3", explanation="pg_upgrade failed",
                  error="locale mismatch")
    assert doc["result"] == "fail"


def test_a_pass_with_a_failure_block_is_rejected():
    doc = _record()
    doc["failure"] = {"explanation": "x", "error": "y"}
    assert any("carries no failure" in p for p in validate_record(doc))


def test_unknown_fields_are_not_an_error():
    doc = _record()
    doc["something_learned_later"] = {"any": "shape"}
    assert validate_record(doc) == []


def test_record_path_is_one_file_per_row_and_mode(tmp_path):
    assert record_path(tmp_path, ROW, "empty") == tmp_path / "pg15" / "2.13-to-2.28.empty.json"


def test_a_major_change_names_its_target_major_in_the_path(tmp_path):
    row = {"source_pg": "12", "source_ts": "2.11", "target_pg": "16", "target_ts": "2.30"}
    assert record_path(tmp_path, row, "link-seeded") == (
        tmp_path / "pg12" / "2.11-to-pg16-2.30.link-seeded.json")


def test_write_and_load_round_trip(tmp_path):
    path = record_path(tmp_path, ROW, "seeded")
    write_record(path, _record())
    assert load_record(path)["versions"]["reached"] == "2.28.3"
    assert path.read_bytes().endswith(b"}\n")  # LF, like every generated file


def test_write_refuses_an_invalid_record(tmp_path):
    doc = _record()
    del doc["log"]
    with pytest.raises(EvidenceError, match="log"):
        write_record(tmp_path / "x.json", doc)
    assert not (tmp_path / "x.json").exists()


def test_upgrader_revision_reads_this_repository():
    from tsupgrade.evidence import upgrader_revision

    rev = upgrader_revision(Path(__file__).resolve().parents[1])
    assert set(rev) == {"commit", "dirty"}


def test_records_themselves_do_not_make_the_tree_dirty(tmp_path):
    import shutil
    import subprocess

    from tsupgrade.evidence import upgrader_revision

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path)] + list(args), check=True,
                       capture_output=True)

    git("init", "-q")
    (tmp_path / "a.txt").write_text("a")
    git("add", "a.txt")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    assert upgrader_revision(tmp_path)["dirty"] is False

    (tmp_path / "verify" / "evidence" / "pg15").mkdir(parents=True)
    (tmp_path / "verify" / "evidence" / "pg15" / "x.empty.json").write_text("{}")
    assert upgrader_revision(tmp_path)["dirty"] is False

    (tmp_path / "a.txt").write_text("changed")
    assert upgrader_revision(tmp_path)["dirty"] is True


def test_a_route_when_present_must_be_pinned():
    doc = _record()
    doc["route"] = {"start": {"image": "x:pg16.15-ts2.13.0", "digest": "sha256:" + "0" * 64},
                    "steps": [{"action": "update", "image": "x:pg16.15-ts2.30.1",
                               "digest": None, "to": "2.30.1"}]}
    assert any("pinned image" in p for p in validate_record(doc))
    doc["route"]["steps"][0]["digest"] = "sha256:" + "1" * 64
    assert validate_record(doc) == []


def test_timings_when_present_can_be_computed_again():
    ok = {"started_at": "2026-11-25T10:00:00Z", "finished_at": "2026-11-25T10:02:24Z"}
    doc = _record(timings={"forward": ok, "rollback": ok})
    assert validate_record(doc) == []
    assert stage_seconds(ok) == 144
    doc["timings"] = {"apply": ok}
    assert any("timings.apply is not one of" in p for p in validate_record(doc))
    doc["timings"] = {"forward": {"started_at": "2026-11-25 10:00", "finished_at": ok["finished_at"]}}
    assert any("needs started_at and finished_at" in p for p in validate_record(doc))
    doc["timings"] = {"forward": {"started_at": ok["finished_at"], "finished_at": ok["started_at"]}}
    assert any("finished before it started" in p for p in validate_record(doc))


def test_a_size_run_names_its_seed_and_files_apart_per_size():
    with pytest.raises(EvidenceError, match="names its seed"):
        _record(mode="scale-seeded")
    doc = _record(mode="scale-seeded", seed={"devices": 500, "rows": 5040000})
    assert validate_record(doc) == []
    assert evidence_key("scale-seeded", rows=5040000) == "scale-seeded-5040000"
    assert evidence_key("seeded") == "seeded"


def test_a_size_run_with_link_must_have_linked():
    row = dict(ROW, target_pg="16", target_ts="2.30")
    copy = [{"from_pg": 15, "to_pg": 16, "files": "copy", "shared_inodes": 0}]
    link = [{"from_pg": 15, "to_pg": 16, "files": "link", "shared_inodes": 40}]
    seed = {"devices": 500, "rows": 5040000}
    assert _record(transition=row, mode="scale-link-seeded", seed=seed, major_upgrades=link)
    with pytest.raises(EvidenceError, match="link of the relation"):
        _record(transition=row, mode="scale-link-seeded", seed=seed, major_upgrades=copy)
    assert _record(transition=row, mode="scale-seeded", seed=seed, major_upgrades=copy)
