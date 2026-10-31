"""Tests for measured image contents and what they overrule.

No cluster, no network, no docker: the probe's output is fed in as text and
the runner is faked. Each test below corresponds to a way the label-only
profiles were wrong against a real image, measured 2026-10-29:

* ``pg15.18-ts2.28.3`` is labelled from 2.12.2 but installs from 2.13.0, so a
  2.12.2 database was planned as a single hop straight into a stranded state.
* ``pg12.22-ts2.11.2`` ships 1.7.5 and 2.1.0+, nothing of 2.0. A range accepts
  2.0.1 there; the image cannot load it.
* Landing on each image's ceiling routes a pg12 1.7 database to 2.0.1, which
  the next image cannot load, when 1.7.5 would have carried it across.
"""

from __future__ import annotations

import json

import pytest

from tsupgrade import cli
from tsupgrade.contents import (
    ContentsError,
    ImageContents,
    extensions_from_listing,
    libraries_from_listing,
    load_contents,
    measure_image,
    merge_measurements,
    parse_measurement,
)
from tsupgrade.drift import (
    INDETERMINATE,
    LAGGING,
    STRANDED,
    BridgeHop,
    ImageProfile,
    bridge,
    classify_offline,
    plan_chain,
)
from tsupgrade.plan import PlanRefused, build_plan

REPO = "timescale/timescaledb-ha:"

PG12_22_SET = ["1.7.5"] + [
    "2.{0}.{1}".format(minor, patch)
    for minor, patches in [(1, 2), (2, 2), (3, 2), (4, 3), (5, 3), (6, 2),
                           (7, 3), (8, 2), (9, 4), (10, 4), (11, 3)]
    for patch in range(patches)
]
PG12_6_SET = ["1.7.0", "1.7.1", "1.7.2", "1.7.3", "1.7.4", "1.7.5", "2.0.0", "2.0.1"]


def _contents():
    return ImageContents({
        "repository": "timescale/timescaledb-ha",
        "images": {
            "pg12.6-ts2.0.1-p0": {
                "digest": "sha256:aaa", "measured_at": "2026-10-29",
                "pg": {
                    "11": {"server_version": "11.11", "default_version": "2.0.1",
                           "installable": ["1.6.0", "1.7.5", "2.0.1"]},
                    "12": {"server_version": "12.6", "default_version": "2.0.1",
                           "installable": PG12_6_SET},
                },
            },
            "pg12.22-ts2.11.2": {
                "digest": "sha256:bbb", "measured_at": "2026-10-29",
                "pg": {"12": {"server_version": "12.22", "default_version": "2.11.2",
                              "installable": PG12_22_SET}},
            },
            "pg15.18-ts2.28.3": {
                "digest": "sha256:ccc", "measured_at": "2026-10-29",
                "pg": {"15": {"server_version": "15.18", "default_version": "2.28.3",
                              "installable": ["2.13.0", "2.20.3", "2.28.3"]}},
            },
        },
    })


def test_pg12_set_has_the_measured_shape():
    """Guard on the fixture itself: 31 versions, 1.7.5 then a hole at 2.0."""
    assert len(PG12_22_SET) == 31
    assert PG12_22_SET[:2] == ["1.7.5", "2.1.0"]
    assert PG12_22_SET[-1] == "2.11.2"


# --------------------------------------------------------------------------
# Profiles from measured contents
# --------------------------------------------------------------------------

def test_measured_profile_uses_the_real_floor_not_the_label():
    profile = _contents().profile(REPO + "pg15.18-ts2.28.3", 15)
    assert profile.from_filesystem
    assert profile.ts_floor == "2.13.0"
    assert not profile.accepts("2.12.2")


@pytest.mark.parametrize("reference", [
    "timescale/timescaledb-ha:pg15.18-ts2.28.3",
    "docker.io/timescale/timescaledb-ha:pg15.18-ts2.28.3",
    "registry-1.docker.io/timescale/timescaledb-ha:pg15.18-ts2.28.3",
])
def test_lookup_accepts_every_spelling_of_a_docker_hub_reference(reference):
    assert _contents().profile(reference, 15) is not None


@pytest.mark.parametrize("reference", [
    "ghcr.io/timescale/timescaledb-ha:pg15.18-ts2.28.3",  # another registry
    "timescale/timescaledb:pg15.18-ts2.28.3",  # another repository
    REPO + "pg15.19-ts2.28.3",  # never measured
    "timescale/timescaledb-ha@sha256:ccc",  # unparseable here, not a crash
])
def test_lookup_does_not_borrow_another_images_measurement(reference):
    assert _contents().profile(reference, 15) is None


def test_multi_pg_image_picks_the_requested_major():
    contents = _contents()
    assert contents.profile(REPO + "pg12.6-ts2.0.1-p0", 11).installable == ("1.6.0", "1.7.5", "2.0.1")
    assert contents.profile(REPO + "pg12.6-ts2.0.1-p0", 12).installable == tuple(PG12_6_SET)
    # No major asked for: the highest it ships.
    assert contents.profile(REPO + "pg12.6-ts2.0.1-p0").ts_floor == "1.7.0"


def test_a_major_the_image_lacks_yields_a_profile_that_does_not_serve_it():
    profile = _contents().profile(REPO + "pg12.22-ts2.11.2", 15)
    assert profile is not None
    assert not profile.serves(15)


def test_measured_profile_accepts_only_exact_members():
    profile = _contents().profile(REPO + "pg12.22-ts2.11.2", 12)
    assert profile.accepts("1.7.5")
    assert profile.accepts("2.1.0")
    assert not profile.accepts("2.0.1")  # inside the range, not on disk
    assert not profile.accepts("1.7.4")
    assert not profile.accepts("2.11")  # not an exact release


# --------------------------------------------------------------------------
# What the measured sets change downstream
# --------------------------------------------------------------------------

def test_offline_drift_reports_a_version_in_the_hole_as_stranded():
    profile = _contents().profile(REPO + "pg12.22-ts2.11.2", 12)
    verdict = classify_offline("2.0.1", profile)
    assert verdict.state == STRANDED
    assert "does not ship it" in verdict.summary


def test_offline_drift_will_not_guess_an_inexact_version_against_a_set():
    profile = _contents().profile(REPO + "pg12.22-ts2.11.2", 12)
    assert classify_offline("2.5", profile).state == INDETERMINATE
    assert classify_offline("2.5.0", profile).state == LAGGING


def test_bridge_stops_below_the_hole_instead_of_at_the_ceiling():
    """Landing on the ceiling (2.0.1) strands; 1.7.5 crosses."""
    contents = _contents()
    target = contents.profile(REPO + "pg12.22-ts2.11.2", 12)
    bridge_image = contents.profile(REPO + "pg12.6-ts2.0.1-p0", 12)
    hops, problem = plan_chain("1.7.0", target, [bridge_image], pg_major=12)
    assert problem is None
    assert hops == [
        BridgeHop(REPO + "pg12.6-ts2.0.1-p0", "1.7.0", "1.7.5"),
        BridgeHop(REPO + "pg12.22-ts2.11.2", "1.7.5", "2.11.2"),
    ]


def test_a_database_on_2_0_cannot_cross_the_hole_with_these_two_images():
    """Recorded because TODO.md assumed it could (the pg12 2.0 -> 2.11 row)."""
    contents = _contents()
    target = contents.profile(REPO + "pg12.22-ts2.11.2", 12)
    bridge_image = contents.profile(REPO + "pg12.6-ts2.0.1-p0", 12)
    hops, problem = bridge("2.0.1", target, [bridge_image], pg_major=12)
    assert hops == []
    assert "stalls at ts2.0.1" in problem


def test_bridge_prefers_fewer_hops_over_a_higher_intermediate_landing():
    a = ImageProfile("a", (15,), "2.9.0", "2.12.0", installable=("2.9.0", "2.10.0", "2.12.0"))
    b = ImageProfile("b", (15,), "2.10.0", "2.13.0", installable=("2.10.0", "2.13.0"))
    target = ImageProfile("t", (15,), "2.13.0", "2.28.3", installable=("2.13.0", "2.28.3"))
    hops, problem = bridge("2.9.0", target, [a, b], pg_major=15)
    assert problem is None
    assert [(h.image, h.to_version) for h in hops] == [("a", "2.10.0"), ("b", "2.13.0")]


def test_plan_no_longer_sends_2_12_2_straight_onto_the_2_28_image():
    """The reported bug. The label said 2.12.2 was in range; it is not."""
    target = _contents().profile(REPO + "pg15.18-ts2.28.3", 15)
    with pytest.raises(PlanRefused) as caught:
        build_plan("15.18", "2.12.2", target, [], verification_run=True)
    assert any("bridge" in r.reason for r in caught.value.refusals)


# --------------------------------------------------------------------------
# The CLI prefers measurement and needs no registry for a measured image
# --------------------------------------------------------------------------

def test_cli_profile_prefers_the_measurement_and_skips_the_registry(monkeypatch):
    def no_network(*_a, **_k):
        raise AssertionError("labels fetched for a measured image")

    monkeypatch.setattr(cli, "fetch_image_labels", no_network)
    profile = cli._profile(REPO + "pg15.18-ts2.28.3", 15, None, 1.0, _contents())
    assert profile.ts_floor == "2.13.0"


def test_cli_profile_falls_back_to_labels_for_an_unmeasured_image(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "fetch_image_labels", lambda ref, timeout: calls.append(ref))
    monkeypatch.setattr(cli, "profile_from_labels", lambda labels, major, catalog: "labels")
    assert cli._profile(REPO + "pg16.16-ts2.31.0", 16, None, 1.0, _contents()) == "labels"
    assert calls == [REPO + "pg16.16-ts2.31.0"]


def test_explicit_contents_path_that_is_missing_is_an_error(tmp_path):
    with pytest.raises(ContentsError):
        cli._load_contents(str(tmp_path / "nope.json"))


# --------------------------------------------------------------------------
# Measuring
# --------------------------------------------------------------------------

PROBE_OUTPUT = """\
== 12
-- server_version
postgres (PostgreSQL) 12.22 (Ubuntu 12.22-1.pgdg22.04+1)
-- default_version
2.11.2
-- sharedir
timescaledb--1.7.5.sql
timescaledb--1.7.5--2.0.0.sql
timescaledb--2.0.0--2.1.0.sql
timescaledb--2.1.0.sql
timescaledb--2.11.2.sql
timescaledb.control
-- pkglibdir
timescaledb-1.7.5.so
timescaledb-2.0.1.so
timescaledb-2.1.0.so
timescaledb-2.11.2.so
timescaledb-tsl-2.11.2.so
timescaledb.so
== 13
-- server_version
postgres (PostgreSQL) 13.22
-- default_version
-- sharedir
-- pkglibdir
"""


def test_parse_measurement_needs_both_script_and_object():
    result = parse_measurement(PROBE_OUTPUT)
    assert list(result) == ["12"]  # 13 ships nothing installable
    major = dict(result["12"])
    major.pop("extensions"), major.pop("libraries")
    assert major == {
        "server_version": "12.22",
        "default_version": "2.11.2",
        # 2.0.1 has a .so but no install script; update scripts don't count.
        "installable": ["1.7.5", "2.1.0", "2.11.2"],
    }


def test_the_listings_also_say_what_other_extensions_and_libraries_a_major_ships():
    """R9: what pg_upgrade --check would look for, known before the outage."""
    share = ["postgis.control", "postgis--3.4.2.sql", "postgis--3.4.1--3.4.2.sql",
             "postgis-3.control", "postgis-3--3.4.2.sql", "plpgsql.control", "plpgsql--1.0.sql",
             "pg_stat_statements.control", "pg_stat_statements--1.4.sql",
             "pg_stat_statements--1.4--1.5.sql", "orphan--1.0.sql", "README"]
    assert extensions_from_listing(share) == {
        "pg_stat_statements": ["1.4"], "plpgsql": ["1.0"], "postgis": ["3.4.2"],
        "postgis-3": ["3.4.2"]}
    assert libraries_from_listing(["postgis-3.so", "plpgsql.so", "bitcode", "x.so.1"]) == [
        "plpgsql", "postgis-3"]


def test_measure_image_records_the_digest():
    def run(args):
        if args[0] == "image":
            return json.dumps(["timescale/timescaledb-ha@sha256:abc"])
        assert args[:4] == ["run", "--rm", "--entrypoint", "sh"]
        return PROBE_OUTPUT

    entry = measure_image(REPO + "pg12.22-ts2.11.2", run=run)
    assert entry["digest"] == "sha256:abc"
    assert entry["pg"]["12"]["installable"][0] == "1.7.5"


def test_measure_image_refuses_an_image_with_nothing_installable():
    with pytest.raises(ContentsError):
        measure_image("x:y", run=lambda args: "[]" if args[0] == "image" else "== 16\n")


def test_merge_keeps_other_images_and_orders_by_version():
    first = merge_measurements(None, {"pg16.15-ts2.30.1": {"pg": {}}})
    both = merge_measurements(first, {"pg12.22-ts2.11.2": {"pg": {}}})
    assert list(both["images"]) == ["pg12.22-ts2.11.2", "pg16.15-ts2.30.1"]


def test_ranges_shows_the_hole():
    assert cli._ranges(PG12_22_SET) == "1.7.5, 2.1.0 .. 2.11.2"
    assert cli._ranges(PG12_6_SET) == "1.7.0 .. 2.0.1"


def test_load_contents_rejects_a_malformed_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("[]")
    with pytest.raises(ContentsError):
        load_contents(path)
