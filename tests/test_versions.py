"""Tests for version parsing and the matching rules the matrix lookup uses."""

from __future__ import annotations

import pytest

from tsupgrade.versions import (
    InvalidVersion,
    format_version,
    parse_version,
    pg_matches,
    ts_matches,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("16", (16,)),
        ("16.15", (16, 15)),
        ("2.30.1", (2, 30, 1)),
        (17, (17,)),
        ("  16.4  ", (16, 4)),
        # What `SHOW server_version` actually returns on a Debian build. The
        # packaging suffix is not part of any upgrade decision.
        ("16.15-1.pgdg22.04+2", (16, 15)),
        ("15.4 (Ubuntu 15.4-1)", (15, 4)),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize("bad", [None, "", "   ", "abc", "v", True, False])
def test_unparseable_versions_raise(bad):
    with pytest.raises(InvalidVersion):
        parse_version(bad)


def test_format_version_round_trips():
    assert format_version(parse_version("2.30.1")) == "2.30.1"


# --- PostgreSQL: the unit of comparison is the major -----------------------

@pytest.mark.parametrize(
    "entry,query,expected",
    [
        ("16", "16", True),
        ("16", "16.15", True),
        ("16", "17.2", False),
        # An entry that pins a minor means exactly that minor.
        ("16.15", "16.15", True),
        ("16.15", "16.15.1", True),
        ("16.15", "16.9", False),
        ("16.15", "16", False),
    ],
)
def test_pg_matches(entry, query, expected):
    assert pg_matches(entry, query) is expected


# --- TimescaleDB: the unit of comparison is the series ---------------------

@pytest.mark.parametrize(
    "entry,query,expected",
    [
        ("2.30", "2.30", True),
        ("2.30", "2.30.1", True),
        ("2.30", "2.28.3", False),
        ("2.30", "3.30.1", False),
        # An entry that pins a patch means exactly that patch.
        ("2.30.1", "2.30.1", True),
        ("2.30.1", "2.30.0", False),
        ("2.30.1", "2.30", False),
    ],
)
def test_ts_matches(entry, query, expected):
    assert ts_matches(entry, query) is expected


def test_matching_is_not_symmetric():
    """A series-wide entry covers a patch release, but not the reverse.

    Worth pinning down: the arguments are an entry and a discovered version,
    and swapping them is a bug, not a convenience.
    """
    assert ts_matches("2.30", "2.30.1") is True
    assert ts_matches("2.30.1", "2.30") is False
