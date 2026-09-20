"""Tests for tag parsing and the image catalog.

No network access: ``fetch_tags`` is the only function that talks to a
registry, and these tests feed ``build_catalog`` a tag list directly.
"""

from __future__ import annotations

import json

import pytest

from tsupgrade.images import (
    ImageCatalog,
    RegistryError,
    build_catalog,
    load_catalog,
    parse_release_tag,
)

# A slice of real tag shapes seen in timescale/timescaledb-ha, including the
# ones that must NOT be treated as release tags.
SAMPLE_TAGS = [
    "pg16.15-ts2.30.1",
    "pg16.15-ts2.30.1-all",
    "pg16.15-ts2.30.1-oss",
    "pg16.15-ts2.30.1-all-oss",
    "pg15.18-ts2.28.3",
    "pg13.4-ts2.5.0",
    "pg11.10-ts1.7.4-p0",
    # Not release tags:
    "pg16-ts2.30",          # floating, pins no patch
    "pg17",                 # floating
    "cicd-006ec76",         # CI build
    "pg16.15-all-oss-builder",
    "pg17.6-ts2.24.0-dev",
    "pg11-latest",
    "pg18-cicheck-all-amd64",
]


@pytest.mark.parametrize(
    "tag,pg,ts",
    [
        ("pg16.15-ts2.30.1", "16.15", "2.30.1"),
        ("pg16.15-ts2.30.1-all", "16.15", "2.30.1"),
        ("pg16.15-ts2.30.1-all-oss", "16.15", "2.30.1"),
        ("pg11.10-ts1.7.4-p2", "11.10", "1.7.4"),
    ],
)
def test_parses_release_tags(tag, pg, ts):
    parsed = parse_release_tag(tag)
    assert parsed is not None
    assert parsed["pg"] == pg
    assert parsed["ts"] == ts


@pytest.mark.parametrize(
    "tag",
    [
        "pg16-ts2.30",
        "pg17",
        "cicd-006ec76",
        "pg16.15-all-oss-builder",
        "pg17.6-ts2.24.0-dev",
        "pg11-latest",
        "pg18-cicheck-all-amd64",
        "latest",
        "",
    ],
)
def test_rejects_non_release_tags(tag):
    """Floating, CI and builder tags pin no (pg, ts) pair and must be ignored."""
    assert parse_release_tag(tag) is None


def test_build_catalog_groups_variants_under_one_pair():
    doc = build_catalog(SAMPLE_TAGS)
    pairs = {(p["pg"], p["ts"]): p for p in doc["pairs"]}

    assert ("16.15", "2.30.1") in pairs
    assert len(pairs[("16.15", "2.30.1")]["tags"]) == 4


def test_build_catalog_counts_only_release_tags():
    doc = build_catalog(SAMPLE_TAGS)
    assert doc["counts"]["tags_total"] == len(SAMPLE_TAGS)
    assert doc["counts"]["release_tags"] == 7
    assert doc["counts"]["pairs"] == 4


def test_build_catalog_records_where_it_came_from():
    doc = build_catalog(SAMPLE_TAGS)
    assert doc["repository"] == "timescale/timescaledb-ha"
    assert "registry-1.docker.io" in doc["source_url"]
    assert doc["queried_at"].endswith("Z")


def test_catalog_queries():
    catalog = ImageCatalog(build_catalog(SAMPLE_TAGS))

    assert catalog.pg_majors() == [11, 13, 15, 16]
    assert catalog.latest_ts_series(16) == (2, 30)
    assert catalog.latest_ts_series(13) == (2, 5)
    assert catalog.latest_ts_series(99) is None


def test_has_pair_matches_on_series_not_patch():
    catalog = ImageCatalog(build_catalog(SAMPLE_TAGS))

    assert catalog.has_pair("16", "2.30") is True
    assert catalog.has_pair("16.15", "2.30.1") is True
    # No image was ever published pairing pg13 with TimescaleDB 2.30.
    assert catalog.has_pair("13", "2.30") is False
    assert catalog.has_pair("nonsense", "2.30") is False


def test_tags_for_returns_every_variant():
    catalog = ImageCatalog(build_catalog(SAMPLE_TAGS))
    tags = catalog.tags_for("16", "2.30")

    assert "pg16.15-ts2.30.1" in tags
    assert "pg16.15-ts2.30.1-all-oss" in tags
    assert catalog.tags_for("13", "2.30") == []


def test_missing_catalog_is_a_loud_error(tmp_path):
    with pytest.raises(RegistryError, match="images-refresh"):
        load_catalog(tmp_path / "absent.json")


def test_malformed_catalog_is_a_loud_error(tmp_path):
    path = tmp_path / "images.json"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_catalog(path)


def test_catalog_round_trips_through_disk(tmp_path):
    path = tmp_path / "images.json"
    path.write_text(json.dumps(build_catalog(SAMPLE_TAGS)), encoding="utf-8")

    catalog = load_catalog(path)
    assert len(catalog) == 4
    assert catalog.has_pair("15", "2.28") is True


# --------------------------------------------------------------------------
# Image labels: what a tag declares about its own contents
# --------------------------------------------------------------------------

from tsupgrade.images import ImageLabels, _split_versions  # noqa: E402


def _labels(ts=(), pg=(), ts_default=None):
    return ImageLabels(
        reference="test:tag",
        pg_versions=tuple(pg),
        ts_versions=tuple(ts),
        pg_default=None,
        ts_default=ts_default,
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2.13.0 2.30.1", ("2.13.0", "2.30.1")),
        ("2.13.0,2.30.1", ("2.13.0", "2.30.1")),
        ("  2.13.0   2.30.1  ", ("2.13.0", "2.30.1")),
        ("2.30.1", ("2.30.1",)),
        ("", ()),
        (None, ()),
    ],
)
def test_label_version_lists_split_on_whitespace_or_commas(raw, expected):
    assert _split_versions(raw) == expected


def test_label_floor_and_ceiling_sort_numerically_not_lexically():
    """Lexically '2.9.2' sorts after '2.13.0', which would inverse the range.

    This is the whole point of the floor/ceiling pair, so getting it wrong
    would make every bridging decision wrong in the same direction.
    """
    labels = _labels(ts=["2.13.0", "2.9.2", "2.30.1", "2.20.3"])
    assert labels.ts_floor == "2.9.2"
    assert labels.ts_ceiling == "2.30.1"


def test_empty_label_has_no_floor_or_ceiling():
    labels = _labels()
    assert labels.ts_floor is None
    assert labels.ts_ceiling is None


def test_pg_majors_are_deduplicated_and_sorted():
    labels = _labels(pg=["15.18", "16.15", "17.11", "15.13"])
    assert labels.pg_majors == (15, 16, 17)


def test_pg_majors_skips_what_it_cannot_parse():
    """A label format change should lose one entry, not the whole list."""
    labels = _labels(pg=["15.18", "junk", "16.15"])
    assert labels.pg_majors == (15, 16)
