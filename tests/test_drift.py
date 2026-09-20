"""Tests for the drift classifier and the bridge search.

No cluster and no network. These cover the decisions -- which of the four
states a database is in, what an image can really install, and the image
sequence that rescues a stranded one -- which is where a wrong answer would
send someone into an outage rather than out of one.

The live counterparts (a real container, a real registry) are exercised
separately; see ``docs/drift-verification.md``.
"""

from __future__ import annotations

import pytest

from tsupgrade.drift import (
    AHEAD,
    ALIGNED,
    INDETERMINATE,
    LAGGING,
    STRANDED,
    BridgeHop,
    ImageProfile,
    bridge,
    classify,
    classify_offline,
    effective_floor,
    installable_from_listings,
    plan_chain,
    parse_install_scripts,
    parse_shared_objects,
)
from tsupgrade.images import ImageCatalog


# --------------------------------------------------------------------------
# The four states
# --------------------------------------------------------------------------

INSTALLABLE = ["2.13.0", "2.15.3", "2.20.3", "2.28.3", "2.30.1"]


def test_aligned_when_at_image_default():
    assert classify("2.30.1", INSTALLABLE, "2.30.1").state == ALIGNED


def test_lagging_when_behind_but_installable():
    verdict = classify("2.20.3", INSTALLABLE, "2.30.1")
    assert verdict.state == LAGGING
    assert not verdict.is_outage


def test_stranded_when_below_the_image_floor():
    """The state nothing else reports, and the one that is an outage.

    A pg15 database at 2.9.2 on an image whose floor is 2.12.2 cannot load the
    extension at all. Rolling further forward does not help.
    """
    verdict = classify("2.9.2", ["2.12.2", "2.20.3", "2.28.3"], "2.28.3")
    assert verdict.state == STRANDED
    assert verdict.is_outage
    assert "roll back" in verdict.remedy


def test_ahead_when_image_rolled_back_under_the_catalog():
    verdict = classify("2.30.1", ["2.13.0", "2.20.3"], "2.20.3")
    assert verdict.state == AHEAD
    assert verdict.is_outage
    assert "roll the image forward" in verdict.remedy


def test_ahead_takes_precedence_over_stranded():
    """Both are outages, but the remedies point in opposite directions.

    A version newer than the default is also, normally, not installable. Saying
    'stranded' would advise rolling the image *back*, which is precisely wrong.
    """
    verdict = classify("2.30.1", ["2.13.0", "2.20.3"], "2.20.3")
    assert verdict.state == AHEAD


@pytest.mark.parametrize("installed", [None, "", "not-a-version"])
def test_unreadable_version_is_indeterminate_never_stranded(installed):
    """A stranded database and an unreachable one look alike from outside.

    Reporting the second as the first would invent an outage, which is the
    failure mode this whole toolkit is built to avoid.
    """
    verdict = classify(installed, INSTALLABLE, "2.30.1")
    assert verdict.state == INDETERMINATE
    assert verdict.state != STRANDED


def test_no_default_still_detects_stranding():
    """The strand test does not depend on knowing the default."""
    assert classify("2.9.2", ["2.12.2", "2.28.3"], None).state == STRANDED


def test_no_default_and_installable_is_indeterminate_not_aligned():
    assert classify("2.20.3", ["2.20.3"], None).state == INDETERMINATE


def test_empty_installable_set_does_not_strand():
    """An empty listing is a failed probe, not proof of absence."""
    assert classify("2.20.3", [], "2.30.1").state == LAGGING


# --------------------------------------------------------------------------
# What an image can really install
# --------------------------------------------------------------------------

def test_install_scripts_exclude_update_scripts():
    """Update scripts are why pg_available_extension_versions overstates.

    `timescaledb--2.13.0--2.30.1.sql` records a path between two versions; it
    does not mean 2.13.0 can be installed here.
    """
    names = [
        "timescaledb--2.13.0.sql",
        "timescaledb--2.30.1.sql",
        "timescaledb--2.13.0--2.30.1.sql",
        "timescaledb--2.12.2--2.13.0.sql",
        "timescaledb.control",
    ]
    assert parse_install_scripts(names) == ["2.13.0", "2.30.1"]


def test_shared_objects_exclude_loader_and_tsl():
    names = [
        "timescaledb-2.13.0.so",
        "timescaledb-2.30.1.so",
        "timescaledb.so",
        "timescaledb-tsl-2.30.1.so",
    ]
    assert parse_shared_objects(names) == ["2.13.0", "2.30.1"]


def test_installable_requires_both_artifacts():
    """A script without a shared object is a version that loads and then fails.

    This is the 2.12.2 case on pg16 images: listed, advertised, and absent.
    """
    scripts = ["timescaledb--2.12.2.sql", "timescaledb--2.13.0.sql"]
    objects = ["timescaledb-2.13.0.so"]
    assert installable_from_listings(scripts, objects) == ["2.13.0"]


def test_installable_sorts_numerically_not_lexically():
    scripts = ["timescaledb--2.9.0.sql", "timescaledb--2.13.0.sql"]
    objects = ["timescaledb-2.9.0.so", "timescaledb-2.13.0.so"]
    assert installable_from_listings(scripts, objects) == ["2.9.0", "2.13.0"]


# --------------------------------------------------------------------------
# Floor clamping
# --------------------------------------------------------------------------

def _catalog(pairs):
    return ImageCatalog(
        {"pairs": [{"pg": pg, "ts": ts, "tags": []} for pg, ts in pairs]}
    )


def test_floor_clamped_up_when_label_overstates():
    """pg16 images advertise 2.12.2; 2.12 never supported pg16."""
    catalog = _catalog([("16.15", "2.13.0"), ("16.15", "2.30.1")])
    floor, notes = effective_floor(16, "2.12.2", catalog)
    assert floor == "2.13"
    assert any("clamped" in n for n in notes)


def test_floor_left_alone_when_label_is_honest():
    catalog = _catalog([("15.13", "2.9.2"), ("15.18", "2.28.3")])
    floor, notes = effective_floor(15, "2.9.2", catalog)
    assert floor == "2.9.2"
    assert not any("clamped" in n for n in notes)


def test_pg15_floor_follows_the_registry_not_the_support_matrix():
    """The two sources disagree here, and the choice is load-bearing.

    The published support matrix starts pg15 at 2.10, but pg15/2.9 images exist
    and a 2.9 cluster is exactly the case the bridge search has to rescue.
    Clamping to 2.10 would make that rescue unrepresentable.
    """
    catalog = _catalog([("15.13", "2.9.2")])
    floor, notes = effective_floor(15, "2.8.1", catalog)
    assert floor == "2.9"
    assert any("support matrix starts" in n for n in notes)


# --------------------------------------------------------------------------
# Bridge search
# --------------------------------------------------------------------------

PG15_20 = ImageProfile("pg15-ts2.20", (15,), "2.8.1", "2.20.3", "2.20.3")
PG15_28 = ImageProfile("pg15-ts2.28", (15,), "2.12.2", "2.28.3", "2.28.3")
PG15_16 = ImageProfile("pg15-ts2.16", (15,), "2.8.1", "2.16.1", "2.16.1")


def test_two_hop_rescue_for_a_stranded_pg15_cluster():
    """The worked example: pg15 at 2.9.2 cannot reach the pg15-ts2.28 image.

    No documentation states this. It falls out of the label data: the newest
    pg15 image's floor (2.12.2) is above the cluster's version, so it strands,
    and an older image has to carry it up first.
    """
    hops, problem = bridge("2.9.2", PG15_28, [PG15_20, PG15_16], pg_major=15)
    assert problem is None
    assert hops == [
        BridgeHop("pg15-ts2.20", "2.9.2", "2.20.3"),
    ]


def test_bridge_is_empty_when_target_already_accepts():
    hops, problem = bridge("2.20.3", PG15_28, [PG15_20], pg_major=15)
    assert hops == []
    assert problem is None


def test_bridge_prefers_the_highest_ceiling():
    """Greedy on ceiling is what keeps the hop count minimal."""
    hops, _ = bridge("2.9.2", PG15_28, [PG15_16, PG15_20], pg_major=15)
    assert [h.image for h in hops] == ["pg15-ts2.20"]


def test_bridge_reports_a_version_it_cannot_lift():
    """Below every candidate's floor is a real answer, not a silent empty list."""
    hops, problem = bridge("2.1.0", PG15_28, [PG15_20, PG15_16], pg_major=15)
    assert hops == []
    assert problem is not None
    assert "no image accepts" in problem


def test_bridge_ignores_images_for_another_pg_major():
    pg16 = ImageProfile("pg16-ts2.30", (16,), "2.13.0", "2.30.1", "2.30.1")
    hops, problem = bridge("2.9.2", PG15_28, [pg16], pg_major=15)
    assert hops == []
    assert problem is not None


def test_bridge_does_not_loop_on_a_stalled_ceiling():
    """A candidate that accepts a version but cannot raise it must terminate."""
    flat = ImageProfile("flat", (15,), "2.9.0", "2.9.2", "2.9.2")
    hops, problem = bridge("2.9.2", PG15_28, [flat], pg_major=15)
    assert problem is not None
    assert len(hops) <= 1


def test_multi_hop_chain_is_built_in_order():
    low = ImageProfile("low", (15,), "2.1.0", "2.5.0", "2.5.0")
    mid = ImageProfile("mid", (15,), "2.4.0", "2.12.2", "2.12.2")
    hops, problem = bridge("2.1.0", PG15_28, [low, mid, PG15_20], pg_major=15)
    assert problem is None
    assert [h.from_version for h in hops] == ["2.1.0", "2.5.0"]
    assert [h.to_version for h in hops] == ["2.5.0", "2.12.2"]


# --------------------------------------------------------------------------
# Offline classification, which is the mode that runs before a rollout
# --------------------------------------------------------------------------

def test_offline_detects_stranding_before_the_image_is_applied():
    """The whole point of the offline mode.

    A stranded database may not be queryable, so the question has to be
    answerable from registry metadata alone -- and it is only useful before the
    rollout, because afterwards the answer is an incident.
    """
    verdict = classify_offline("2.9.2", PG15_28)
    assert verdict.state == STRANDED
    assert verdict.is_outage


def test_offline_reports_ahead_above_the_ceiling():
    assert classify_offline("2.30.1", PG15_28).state == AHEAD


def test_offline_aligned_and_lagging():
    assert classify_offline("2.28.3", PG15_28).state == ALIGNED
    assert classify_offline("2.20.3", PG15_28).state == LAGGING


@pytest.mark.parametrize("version", [None, "", "junk"])
def test_offline_refuses_to_guess(version):
    assert classify_offline(version, PG15_28).state == INDETERMINATE


# --------------------------------------------------------------------------
# The full rollout chain
# --------------------------------------------------------------------------

def test_plan_chain_ends_on_the_target_image():
    """bridge() answers what must happen first; plan_chain answers what to do.

    Two image rollouts and two ALTER EXTENSIONs is the operator-facing answer,
    and the final hop onto the target is part of it.
    """
    hops, problem = plan_chain("2.9.2", PG15_28, [PG15_20, PG15_16], pg_major=15)
    assert problem is None
    assert [h.image for h in hops] == ["pg15-ts2.20", "pg15-ts2.28"]
    assert hops[-1].to_version == "2.28.3"


def test_plan_chain_is_a_single_hop_when_no_bridging_is_needed():
    hops, problem = plan_chain("2.20.3", PG15_28, [PG15_20], pg_major=15)
    assert problem is None
    assert [h.image for h in hops] == ["pg15-ts2.28"]


def test_plan_chain_adds_nothing_when_already_at_the_ceiling():
    hops, problem = plan_chain("2.28.3", PG15_28, [PG15_20], pg_major=15)
    assert problem is None
    assert hops == []


def test_plan_chain_propagates_an_unbridgeable_version():
    hops, problem = plan_chain("2.1.0", PG15_28, [PG15_20], pg_major=15)
    assert problem is not None


# --------------------------------------------------------------------------
# Version comparison: the failure that would be invisible
# --------------------------------------------------------------------------

def test_versions_compare_numerically_not_lexically():
    """'2.9.2' sorts after '2.13.0' as a string, and that would invert everything.

    A database at 2.9.2 on a 2.13-default image is lagging. Compared as text it
    looks ahead, which would report an outage and advise rolling the image
    forward -- the opposite of the truth, in the one case that matters.
    """
    verdict = classify("2.9.2", ["2.9.2", "2.13.0"], "2.13.0")
    assert verdict.state == LAGGING


def test_double_digit_minors_outrank_single_digit():
    assert classify("2.9.0", ["2.9.0", "2.30.1"], "2.30.1").state == LAGGING
    assert classify("2.30.1", ["2.9.0", "2.30.1"], "2.9.0").state == AHEAD


def test_offline_comparison_is_also_numeric():
    profile = ImageProfile("img", (16,), "2.9.0", "2.13.0", "2.13.0")
    assert classify_offline("2.9.2", profile).state == LAGGING


# --------------------------------------------------------------------------
# Precedence when the signals conflict
# --------------------------------------------------------------------------

def test_default_that_is_not_installable_still_strands():
    """A broken image whose default has no shared object cannot load it either.

    Contrived, but the ordering matters: reporting ALIGNED because the version
    matches the default would hide a database that cannot start.
    """
    assert classify("2.30.1", ["2.13.0", "2.20.3"], "2.30.1").state == STRANDED


def test_profile_rejects_an_unparseable_version():
    assert ImageProfile("img", (16,), "2.13.0", "2.30.1").accepts("junk") is False


def test_profile_boundaries_are_inclusive():
    profile = ImageProfile("img", (16,), "2.13.0", "2.30.1")
    assert profile.accepts("2.13.0")
    assert profile.accepts("2.30.1")
    assert not profile.accepts("2.12.2")
    assert not profile.accepts("2.31.0")


# --------------------------------------------------------------------------
# Floor clamping without a catalog
# --------------------------------------------------------------------------

def test_floor_falls_back_to_the_support_matrix_without_a_catalog():
    """Offline with no images.json, the documented table is the only source."""
    floor, notes = effective_floor(16, "2.12.2", None)
    assert floor == "2.13"
    assert any("clamped" in n for n in notes)


def test_unknown_pg_major_leaves_the_floor_alone():
    floor, notes = effective_floor(99, "2.12.2", None)
    assert floor == "2.12.2"


def test_unparseable_floor_is_reported_not_silently_kept():
    floor, notes = effective_floor(16, "junk", None)
    assert floor == "junk"
    assert any("cannot parse" in n for n in notes)


# --------------------------------------------------------------------------
# Bridging edge cases
# --------------------------------------------------------------------------

def test_bridge_with_no_candidates_says_so():
    hops, problem = bridge("2.9.2", PG15_28, [], pg_major=15)
    assert hops == []
    assert "no candidate images" in problem


def test_bridge_does_not_offer_the_target_as_its_own_bridge():
    hops, problem = bridge("2.9.2", PG15_28, [PG15_28], pg_major=15)
    assert hops == []
    assert problem is not None


def test_bridging_is_not_attempted_for_a_version_above_the_ceiling():
    """An 'ahead' database is not stranded; rolling forward is the answer.

    Bridging upward from 2.30.1 toward a 2.28.3 ceiling is incoherent, so this
    must report a problem rather than invent a chain.
    """
    hops, problem = bridge("2.30.1", PG15_28, [PG15_20], pg_major=15)
    assert problem is not None


def test_plan_chain_does_not_go_backwards():
    hops, problem = plan_chain("2.30.1", PG15_28, [PG15_20], pg_major=15)
    assert problem is not None


def test_bridge_without_a_pg_major_still_works():
    """pg_major defaults to the target's own; profiles agreeing on it suffice."""
    hops, problem = bridge("2.9.2", PG15_28, [PG15_20])
    assert problem is None
    assert [h.image for h in hops] == ["pg15-ts2.20"]
