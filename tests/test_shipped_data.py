"""Guards on the data files this repository actually ships.

The fixture-based tests prove the loader works. These prove the committed
``data/*.json`` are loadable and internally honest, so a bad hand-edit fails in
CI rather than in front of someone mid-maintenance-window.
"""

from __future__ import annotations

import pytest

from tsupgrade.evidence import load_record
from tsupgrade.images import load_catalog
from tsupgrade.matrix import VERDICTS, default_matrix_path, load_matrix

REPO = default_matrix_path().parents[1]

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


def test_every_verified_row_names_its_proof():
    """The invariant the loader enforces, restated against real data."""
    for entry in load_matrix():
        if entry.verified:
            assert entry.verdict in VERDICTS, entry.transition
            if entry.verdict == "no_image":
                assert entry.references, entry.transition
            else:
                assert entry.evidence, entry.transition


def test_no_unverified_row_smuggles_in_a_verdict():
    """An unverified row must stay blank -- no verdict, reason or evidence.

    This is the rule that keeps guesses out of the matrix. If someone fills in a
    verdict, they have to flip `verified` and name the runs that established
    it, and this test is what makes that non-optional.
    """
    for entry in load_matrix():
        if not entry.verified:
            assert entry.verdict is None, entry.transition
            assert entry.reason == "", entry.transition
            assert entry.evidence is None, entry.transition
            assert not entry.steps, entry.transition


def _records(entry):
    """Every record the row cites, keyed as cited; each must be what its key says."""
    records = {key: load_record(REPO / path) for key, path in entry.evidence.items()}
    for key, doc in records.items():
        control, _, mode = key.partition("-") if key.startswith("direct-") else (None, "", key)
        assert doc["mode"] == mode, (entry.transition, key)
        assert doc.get("control") == control, (entry.transition, key)
        assert doc["transition"] == {
            "source_pg": entry.source_pg, "source_ts": entry.source_ts,
            "target_pg": entry.target_pg, "target_ts": entry.target_ts,
        }, (entry.transition, mode)
    return records


def test_every_verified_row_agrees_with_its_evidence():
    """A verdict says what the runs showed, and nothing they did not.

    * ``in_place``: the upgrade itself completed in both runs and the empty run
      passed. A seeded run may fail only in the fixture checks after the
      upgrade, and only with the caveat written in ``reason``.
    * ``unsupported``: some run failed before the upgrade completed, or the
      planner refused it outright.
    """
    for entry in load_matrix():
        if not entry.verified or not entry.evidence:
            continue
        records = _records(entry)
        runs = {k: d for k, d in records.items() if "control" not in d}
        controls = {k: d for k, d in records.items() if "control" in d}
        where = entry.transition
        if entry.verdict == "in_place":
            for doc in runs.values():
                assert doc["versions"]["reached"] == doc["versions"]["target"], where
            for prefix in ("", "history-"):
                empty, seeded = runs.get(prefix + "empty"), runs.get(prefix + "seeded")
                if empty is not None:
                    assert empty["result"] == "pass", (where, prefix)
                if seeded is not None and seeded["result"] != "pass":
                    assert seeded["stage"] == "check", (where, prefix)
                    assert entry.reason, where
        elif entry.verdict == "unsupported":
            assert any(d["result"] == "fail" and d["stage"] in ("plan", "apply")
                       for d in runs.values()), where
        else:
            pytest.fail("no evidence rule for verdict {0!r} ({1})".format(entry.verdict, where))


def test_a_cited_control_shows_the_failure_it_controls_for():
    """A row cites a direct control to show that a planner's step is needed.

    The control never counts toward the verdict; it has to have failed in the
    update itself, or it shows nothing.
    """
    for entry in load_matrix():
        if not entry.evidence:
            continue
        for key, doc in _records(entry).items():
            if "control" in doc:
                assert doc["result"] == "fail" and doc["stage"] == "apply", (entry.transition, key)
                assert entry.steps, (entry.transition, key)


def test_every_same_pg_row_is_settled():
    """Only the cross-PG rows, out of scope, may still be unverified."""
    for entry in load_matrix():
        if entry.source_pg == entry.target_pg:
            assert entry.verified, entry.transition


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
