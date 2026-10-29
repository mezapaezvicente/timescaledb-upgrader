"""Guards on the data files this repository actually ships.

The fixture-based tests prove the loader works. These prove the committed
``data/*.json`` are loadable and internally honest, so a bad hand-edit fails in
CI rather than in front of someone mid-maintenance-window.
"""

from __future__ import annotations

import pytest

from tsupgrade.images import load_catalog
from tsupgrade.matrix import VERDICTS, default_matrix_path, load_matrix

pytestmark = pytest.mark.skipif(
    not default_matrix_path().exists(),
    reason="data/upgrade-matrix.json has not been scaffolded yet",
)


def test_shipped_matrix_loads():
    matrix = load_matrix()
    assert len(matrix) > 0


def test_shipped_catalog_loads():
    catalog = load_catalog()
    assert len(catalog) > 0
    assert catalog.source_url


def test_every_verified_row_cites_a_source():
    """The invariant the loader enforces, restated against real data."""
    for entry in load_matrix():
        if entry.verified:
            assert entry.verdict in VERDICTS, entry.transition
            assert entry.source_url, entry.transition


def test_no_unverified_row_smuggles_in_a_verdict():
    """An unverified row must stay blank -- no verdict, no reason, no source.

    This is the rule that keeps guesses out of the matrix. If someone fills in a
    verdict, they have to flip `verified` and cite where it came from, and this
    test is what makes that non-optional.
    """
    for entry in load_matrix():
        if not entry.verified:
            assert entry.verdict is None, entry.transition
            assert entry.reason == "", entry.transition
            assert entry.source_url is None, entry.transition


def test_no_duplicate_transitions():
    seen = set()
    for entry in load_matrix():
        key = (entry.source_pg, entry.source_ts, entry.target_pg, entry.target_ts)
        assert key not in seen, "duplicate transition {0}".format(entry.transition)
        seen.add(key)


def test_no_row_describes_a_transition_to_itself():
    for entry in load_matrix():
        assert (entry.source_pg, entry.source_ts) != (
            entry.target_pg,
            entry.target_ts,
        ), entry.transition


def test_every_no_image_row_agrees_with_the_catalog():
    """A `no_image` verdict must still be true of the catalog we ship.

    If a refresh picks up a newly published image, this fails and the row has to
    be revisited rather than quietly going stale.
    """
    catalog = load_catalog()
    for entry in load_matrix():
        if entry.verdict == "no_image":
            assert not catalog.has_pair(entry.target_pg, entry.target_ts), (
                "matrix says no image for {0} but the catalog has one".format(
                    entry.transition
                )
            )


def test_every_other_verified_row_targets_an_image_that_exists():
    catalog = load_catalog()
    for entry in load_matrix():
        if entry.verified and entry.verdict in ("in_place", "dump_restore"):
            assert catalog.has_pair(entry.target_pg, entry.target_ts), (
                "{0} targets an image that does not exist".format(entry.transition)
            )


# --------------------------------------------------------------------------
# data/image-contents.json
# --------------------------------------------------------------------------

def _shipped_contents():
    from tsupgrade.contents import default_contents_path, load_contents

    if not default_contents_path().exists():
        pytest.skip("data/image-contents.json has not been measured yet")
    return load_contents()


def test_shipped_contents_agree_with_their_own_tags():
    """The tag names a PG minor and a TimescaleDB release; the measurement of
    that image's own major must contain both, or the entry is mislabelled."""
    from tsupgrade.images import parse_release_tag

    contents = _shipped_contents()
    assert len(contents) > 0
    for tag, entry in contents.images.items():
        parsed = parse_release_tag(tag)
        assert parsed is not None, tag
        major = parsed["pg"].split(".")[0]
        own = entry["pg"][major]
        assert own["server_version"] == parsed["pg"], tag
        assert own["default_version"] == parsed["ts"], tag
        assert parsed["ts"] in own["installable"], tag
        assert entry.get("digest", "").startswith("sha256:"), tag


def test_shipped_contents_are_sorted_and_unique():
    from tsupgrade.versions import parse_version

    for tag, entry in _shipped_contents().images.items():
        for major, found in entry["pg"].items():
            versions = found["installable"]
            keys = [parse_version(v) for v in versions]
            assert keys == sorted(set(keys)), (tag, major)
