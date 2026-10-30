"""Tests for the matrix loader and lookup.

The behaviour these pin down is what keeps the toolkit honest:

* a lookup that finds nothing must say so, rather than falling back to
  something plausible;
* an entry with no established verdict must not read as an approval;
* a row that claims to be verified must name the runs that verified it.
"""

from __future__ import annotations

import pytest

from tsupgrade.matrix import (
    VERDICTS,
    Matrix,
    MatrixEntry,
    MatrixError,
    load_matrix,
)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def test_loads_every_entry_from_the_fixture(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    assert len(matrix) == 6
    assert all(isinstance(e, MatrixEntry) for e in matrix)


def test_preserves_top_level_metadata(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    assert matrix.meta["schema_version"] == 1
    assert matrix.meta["generated_at"] == "2026-01-01T00:00:00Z"
    assert "entries" not in matrix.meta


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(MatrixError, match="not found"):
        load_matrix(tmp_path / "nope.json")


def test_malformed_json_is_an_error(write_matrix):
    path = write_matrix("{ this is not json", name="broken.json")
    with pytest.raises(MatrixError, match="not valid JSON"):
        load_matrix(path)


def test_top_level_must_be_an_object(write_matrix):
    with pytest.raises(MatrixError, match="JSON object"):
        load_matrix(write_matrix([]))


def test_entries_key_is_required(write_matrix):
    with pytest.raises(MatrixError, match="missing top-level 'entries'"):
        load_matrix(write_matrix({"schema_version": 1}))


@pytest.mark.parametrize(
    "field", ["source_pg", "source_ts", "target_pg", "target_ts"]
)
def test_required_fields_must_be_present(write_matrix, valid_entry, field):
    del valid_entry[field]
    with pytest.raises(MatrixError, match="missing required field"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


@pytest.mark.parametrize(
    "field", ["source_pg", "source_ts", "target_pg", "target_ts"]
)
def test_required_fields_must_not_be_empty(write_matrix, valid_entry, field):
    valid_entry[field] = "  "
    with pytest.raises(MatrixError, match="must not be empty"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_unknown_verdict_is_rejected(write_matrix, valid_entry):
    valid_entry["verdict"] = "probably_fine"
    with pytest.raises(MatrixError, match="is not one of"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_a_verified_entry_must_name_its_evidence(write_matrix, valid_entry):
    valid_entry["evidence"] = None
    with pytest.raises(MatrixError, match="must carry evidence"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_references_are_not_evidence(write_matrix, valid_entry):
    """Upstream links explain a verdict; only the upgrader's own runs prove it."""
    valid_entry.update(evidence=None, references=["https://example.invalid/issue"])
    with pytest.raises(MatrixError, match="must carry evidence"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


@pytest.mark.parametrize(
    "evidence",
    [
        {"empty": "a.json"},
        {"empty": "a.json", "seeded": "b.json", "other": "c.json"},
        {"empty": "a.json", "seeded": ""},
        ["a.json", "b.json"],
    ],
)
def test_evidence_must_name_exactly_the_two_runs(write_matrix, valid_entry, evidence):
    valid_entry["evidence"] = evidence
    with pytest.raises(MatrixError, match="must map exactly empty and seeded"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_a_no_image_entry_may_cite_the_registry_instead(write_matrix, valid_entry):
    valid_entry.update(verdict="no_image", evidence=None,
                       references=["https://example.invalid/tags"])
    assert load_matrix(write_matrix({"entries": [valid_entry]})).entries[0].is_known


def test_a_no_image_entry_needs_the_registry_listing(write_matrix, valid_entry):
    valid_entry.update(verdict="no_image", references=[])
    with pytest.raises(MatrixError, match="the registry listing"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_an_unverified_entry_must_not_carry_evidence(write_matrix, valid_entry):
    valid_entry.update(verified=False, verdict=None)
    with pytest.raises(MatrixError, match="must not carry evidence"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_references_must_be_a_list_of_strings(write_matrix, valid_entry):
    valid_entry["references"] = "https://example.invalid/x"
    with pytest.raises(MatrixError, match="'references' must be a list"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_a_leftover_source_url_fails_loudly(write_matrix, valid_entry):
    valid_entry["source_url"] = "https://example.invalid/x"
    with pytest.raises(MatrixError, match="no longer a field"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_a_verified_entry_must_have_a_verdict(write_matrix, valid_entry):
    valid_entry["verdict"] = None
    with pytest.raises(MatrixError, match="must carry a verdict"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_an_unverified_entry_needs_neither(write_matrix, valid_entry):
    valid_entry.update(verified=False, verdict=None, evidence=None, references=[], reason="")
    matrix = load_matrix(write_matrix({"entries": [valid_entry]}))
    assert len(matrix) == 1
    assert matrix.entries[0].is_known is False


def test_verified_flag_must_be_a_boolean(write_matrix, valid_entry):
    valid_entry["verified"] = "yes"
    with pytest.raises(MatrixError, match="must be a boolean"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_steps_must_be_a_list_of_strings(write_matrix, valid_entry):
    valid_entry["steps"] = [{"do": "something"}]
    with pytest.raises(MatrixError, match="list of strings"):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_unparseable_version_fails_at_load_time(write_matrix, valid_entry):
    valid_entry["source_pg"] = "sixteen"
    with pytest.raises(MatrixError):
        load_matrix(write_matrix({"entries": [valid_entry]}))


def test_error_message_names_the_offending_entry(write_matrix, valid_entry):
    bad = dict(valid_entry)
    del bad["target_ts"]
    with pytest.raises(MatrixError, match=r"entries\[1\]"):
        load_matrix(write_matrix({"entries": [valid_entry, bad]}))


# --------------------------------------------------------------------------
# Lookup: exact match
# --------------------------------------------------------------------------

def test_exact_match_returns_the_entry(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    entry = matrix.lookup("15", "2.20", "15", "2.21")

    assert entry is not None
    assert entry.verdict == "in_place"
    assert entry.verified is True
    assert entry.is_known is True
    assert entry.evidence["seeded"] == "verify/evidence/fixture-in-place.seeded.json"
    assert entry.references == ("https://example.invalid/fixture-in-place",)
    assert entry.steps == ("ALTER EXTENSION timescaledb UPDATE;",)


def test_patch_versions_resolve_to_their_series(basic_matrix_path):
    """A discovered 15.4 / 2.20.2 must find the row written as 15 / 2.20."""
    matrix = load_matrix(basic_matrix_path)
    entry = matrix.lookup("15.4", "2.20.2", "15.8", "2.21.0")

    assert entry is not None
    assert entry.verdict == "in_place"


def test_lookup_accepts_integer_versions(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    assert matrix.lookup(15, "2.20", 15, "2.21") is not None


# --------------------------------------------------------------------------
# Lookup: no matching entry
# --------------------------------------------------------------------------

def test_no_matching_entry_returns_none(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    assert matrix.lookup("99", "9.9", "99", "9.9") is None


def test_right_source_but_wrong_target_does_not_match(basic_matrix_path):
    """Partial matches must not leak through as a result."""
    matrix = load_matrix(basic_matrix_path)
    assert matrix.lookup("15", "2.20", "15", "2.29") is None


def test_wrong_series_does_not_match_neighbouring_series(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    assert matrix.lookup("15", "2.19", "15", "2.21") is None


def test_lookup_on_an_empty_matrix_returns_none():
    assert Matrix([]).lookup("16", "2.28", "17", "2.30") is None


def test_unparseable_lookup_version_raises(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    with pytest.raises(MatrixError, match="cannot compare versions"):
        matrix.lookup("sixteen", "2.28", "17", "2.30")


# --------------------------------------------------------------------------
# Lookup: unverified entries
# --------------------------------------------------------------------------

def test_unverified_entry_is_returned_but_not_known(basic_matrix_path):
    """The row exists, so lookup finds it -- but it carries no verdict.

    This is the distinction the whole design rests on: 'we have not established
    this' is a different answer from 'there is no such transition', and neither
    one is permission to proceed.
    """
    matrix = load_matrix(basic_matrix_path)
    entry = matrix.lookup("15", "2.21", "16", "2.21")

    assert entry is not None
    assert entry.verdict is None
    assert entry.verified is False
    assert entry.is_known is False
    assert entry.reason == ""
    assert entry.evidence is None


def test_unverified_entry_is_distinguishable_from_a_miss(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    unverified = matrix.lookup("15", "2.21", "16", "2.21")
    missing = matrix.lookup("15", "2.21", "18", "2.30")

    assert unverified is not None
    assert missing is None


def test_no_image_verdict_is_verified_against_the_registry(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    entry = matrix.lookup("13", "2.15", "13", "2.30")

    assert entry is not None
    assert entry.verdict == "no_image"
    assert entry.is_known is True


# --------------------------------------------------------------------------
# Lookup: specificity
# --------------------------------------------------------------------------

def test_a_pinned_row_wins_over_the_series_row_it_sits_inside(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    entry = matrix.lookup("16.15", "2.28.3", "17", "2.30")

    assert entry is not None
    assert entry.verdict == "dump_restore"
    assert entry.reason == "fixture: row pinned to one patch release"


def test_the_series_row_still_applies_to_other_patch_releases(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    entry = matrix.lookup("16.4", "2.28.1", "17", "2.30")

    assert entry is not None
    assert entry.verdict == "in_place"
    assert entry.reason == "fixture: series-wide row"


# --------------------------------------------------------------------------
# Entry helpers
# --------------------------------------------------------------------------

def test_transition_renders_readably(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    entry = matrix.lookup("12", "2.11", "16", "2.30")
    assert entry is not None
    assert entry.transition == "pg12/ts2.11 -> pg16/ts2.30"


def test_to_dict_round_trips_through_the_loader(basic_matrix_path, write_matrix):
    original = load_matrix(basic_matrix_path)
    doc = {"entries": [e.to_dict() for e in original]}
    reloaded = load_matrix(write_matrix(doc))

    assert [e.to_dict() for e in reloaded] == [e.to_dict() for e in original]


def test_declared_verdicts_are_the_documented_set():
    assert VERDICTS == {"in_place", "dump_restore", "unsupported", "no_image"}


# --------------------------------------------------------------------------
# from_source: turning a miss into something actionable
# --------------------------------------------------------------------------

def test_from_source_lists_recorded_transitions(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    found = matrix.from_source("15", "2.21")

    assert len(found) == 1
    assert found[0].target_pg == "16"


def test_from_source_normalises_patch_versions(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    assert matrix.from_source("15.4", "2.20.2")


def test_from_source_returns_empty_for_an_unknown_start(basic_matrix_path):
    matrix = load_matrix(basic_matrix_path)
    assert matrix.from_source("99", "9.9") == []


def test_from_source_ignores_unparseable_entries_rather_than_raising():
    """A bad row must not take out the whole 'what can I do from here' listing."""
    good = MatrixEntry("16", "2.28", "17", "2.30")
    matrix = Matrix([good])
    assert matrix.from_source("16", "2.28") == [good]
    assert matrix.from_source("16", "2.27") == []
