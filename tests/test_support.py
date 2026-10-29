"""Tests for the vendor-stated version facts.

These are transcriptions, so the tests are mostly about the *shape* of an
honest transcription: a table that does not cover a case says so, rather than
implying a negative answer.
"""

from __future__ import annotations

import pytest

from tsupgrade.support import (
    ABI_BROKEN,
    PG_MINOR_RULES,
    SUPPORT_MATRIX,
    abi_break_for,
    documented_pg_majors,
    documented_ts_floor,
    floor_disagreements,
    pg_minor_refusal,
    pg_minor_rules_for,
    supports,
)


# --------------------------------------------------------------------------
# The ABI break
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "version,fixed",
    [
        ("17.1", "17.2"),
        ("16.5", "16.6"),
        ("15.9", "15.10"),
        ("14.14", "14.15"),
        ("13.17", "13.18"),
        ("12.21", "12.22"),
    ],
)
def test_abi_broken_releases_are_named_with_their_fix(version, fixed):
    assert abi_break_for(version) == fixed


@pytest.mark.parametrize("version", ["16.15", "17.2", "16.6", "18.6", "15.10", "15.8"])
def test_healthy_releases_are_not_flagged(version):
    assert abi_break_for(version) is None


def test_abi_check_tolerates_a_full_patch_version():
    """Real servers report '16.15 (Ubuntu 16.15-1.pgdg22.04+2)'."""
    assert abi_break_for("16.5 (Ubuntu 16.5-1.pgdg22.04+1)") == "16.6"


@pytest.mark.parametrize("version", ["not a version", "", None, "16"])
def test_abi_check_is_silent_on_what_it_cannot_parse(version):
    """A major with no minor cannot be matched against a patch-level table.

    Returning None here means 'this table does not answer', which is the only
    honest answer -- it is not a clean bill of health.
    """
    assert abi_break_for(version) is None


def test_abi_table_covers_exactly_the_six_documented_releases():
    assert len(ABI_BROKEN) == 6


# --------------------------------------------------------------------------
# The PostgreSQL minors each TimescaleDB series refuses at load time
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ts,pg",
    [
        ("2.20.0", "15.9"),
        ("2.30.1", "16.5"),
        ("2.28.3", "17.1"),
        ("2.20.3", "16.0"),
        ("2.1.0", "13.1"),
        ("2.15.3", "13.0"),
        ("2.18.0", "14.14"),   # the ABI break, excluded by exact version
        ("2.19.3", "17.1"),
    ],
)
def test_target_refuses_the_minor(ts, pg):
    assert pg_minor_refusal(ts, pg) is not None


@pytest.mark.parametrize(
    "ts,pg",
    [
        ("2.20.0", "15.10"),   # the floor itself is accepted
        ("2.30.1", "16.6"),
        ("2.28.3", "17.2"),
        ("2.30.1", "18.0"),    # pg18 has no floor
        ("2.1.0", "13.2"),
        ("2.18.0", "15.8"),    # 2.18-2.19 exclude, they do not floor
        ("2.19.3", "14.15"),
        ("2.17.2", "17.1"),    # before the exclusions existed
        ("2.9.0", "15.0"),
        ("2.0.1", "12.0"),
    ],
)
def test_target_accepts_the_minor(ts, pg):
    assert pg_minor_refusal(ts, pg) is None


def test_refusal_names_the_floor_and_its_source_tag():
    reason = pg_minor_refusal("2.20.3", "15.9")
    assert "15.10" in reason
    assert "2.20.0" in reason


def test_refusal_reads_a_full_server_version_string():
    assert pg_minor_refusal("2.30.1", "16.5 (Ubuntu 16.5-1.pgdg22.04+1)") is not None


def test_series_beyond_the_transcription_has_no_rules():
    """A newer release may have raised a floor; silence, not permission."""
    assert pg_minor_rules_for("2.31.0") is None
    assert pg_minor_rules_for("1.7.5") is None


def test_series_with_no_minor_conditions_is_known_and_empty():
    """2.16-2.17 were read and impose nothing, which differs from unread."""
    assert pg_minor_rules_for("2.17.2") == ("2.16.0", {})


@pytest.mark.parametrize("pg", ["16", "not a version", "", None])
def test_refusal_is_silent_without_a_minor(pg):
    assert pg_minor_refusal("2.30.1", pg) is None


def test_minor_rule_bands_are_contiguous_from_2_0():
    """Every 2.x tag up to the last band was read, so no gaps are allowed."""
    expected_low = (2, 0)
    for low, high, _tag, _rules in PG_MINOR_RULES:
        assert low == expected_low
        assert low <= high
        expected_low = (high[0], high[1] + 1)


# --------------------------------------------------------------------------
# The support matrix
# --------------------------------------------------------------------------

def test_support_matrix_gap_is_reported_as_silence_not_denial():
    """The published table has no row for the 2.11 series.

    Inventing one to close the gap would be the exact kind of guess the matrix
    loader exists to prevent, so the answer is None -- not an empty tuple, and
    certainly not False.
    """
    assert documented_pg_majors("2.11") is None
    assert supports(12, "2.11") is None


def test_series_beyond_the_transcription_is_also_silence():
    """A release newer than the table is unknown, not unsupported."""
    assert documented_pg_majors("2.99") is None
    assert supports(18, "2.99") is None


@pytest.mark.parametrize(
    "pg_major,floor",
    [(16, (2, 13)), (17, (2, 17)), (18, (2, 23)), (14, (2, 5)), (13, (2, 1))],
)
def test_documented_floors_match_the_registry_where_they_agree(pg_major, floor):
    assert documented_ts_floor(pg_major) == floor


def test_unknown_pg_major_has_no_documented_floor():
    assert documented_ts_floor(99) is None


@pytest.mark.parametrize(
    "pg_major,ts,expected",
    [
        (18, "2.30", True),
        (15, "2.30", False),   # 2.29-2.30 dropped pg15
        (15, "2.28", True),
        (16, "2.12", False),   # the phantom: 2.12 never served pg16
        (15, "2.12", True),
    ],
)
def test_matrix_answers_where_it_speaks(pg_major, ts, expected):
    assert supports(pg_major, ts) is expected


def test_matrix_bands_do_not_overlap():
    """Overlapping bands would make lookup order significant, and it is not."""
    seen = []
    for low, high, _ in SUPPORT_MATRIX:
        assert low <= high
        for other_low, other_high in seen:
            assert high < other_low or low > other_high
        seen.append((low, high))


# --------------------------------------------------------------------------
# Where the registry and the table disagree
# --------------------------------------------------------------------------

def test_pg15_floor_disagreement_is_surfaced():
    """Known live discrepancy: registry publishes pg15/2.9, the table says 2.10.

    Surfaced rather than reconciled. Whichever is wrong, hiding it makes the
    other harder to trust -- and the choice is load-bearing for bridging.
    """
    assert floor_disagreements({15: (2, 9), 16: (2, 13)}) == [(15, (2, 9), (2, 10))]


def test_no_disagreement_when_registry_matches_the_table():
    assert floor_disagreements({16: (2, 13), 17: (2, 17)}) == []


def test_registry_floor_above_the_table_is_not_a_disagreement():
    """Only the registry reaching *further back* than the table matters.

    A table that documents support the registry never published is the vendor's
    business, not a clamping problem.
    """
    assert floor_disagreements({13: (2, 5)}) == []


def test_unknown_major_is_skipped_rather_than_guessed():
    assert floor_disagreements({99: (1, 0)}) == []
