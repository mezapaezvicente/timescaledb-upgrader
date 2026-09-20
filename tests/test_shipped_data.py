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
