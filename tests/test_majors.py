"""Tests for routes across PostgreSQL majors, and the plans built on them.

What would hurt if wrong: a pg_upgrade at a TimescaleDB release one side does
not ship, a cluster run on a major its image's tag does not name, more
pg_upgrade runs than needed, and a known-bad update on the way that skips
its stop. The contents here are shaped like the measured images: few images
carry two majors, and none pairs pg12 with anything past pg13.
"""

from __future__ import annotations

import pytest

from tsupgrade.drift import ImageProfile
from tsupgrade.majors import MajorHop, find_route, tag_major
from tsupgrade.matrix import Matrix, MatrixEntry
from tsupgrade.plan import PlanRefused, build_plan, render_runbook, route_of

R = "timescale/timescaledb-ha:"


def _versions(low, high, extra=()):
    """Every x.y.z this test cares about between low and high, as shipped."""
    ladder = ["1.7.5", "2.1.0", "2.11.2", "2.13.0", "2.15.3", "2.19.3", "2.20.3",
              "2.27.2", "2.28.3", "2.30.1"]
    key = lambda v: tuple(int(p) for p in v.split("."))
    return tuple(sorted({v for v in ladder if key(low) <= key(v) <= key(high)} | set(extra), key=key))


def _p(tag, major, low, high):
    v = _versions(low, high)
    return ImageProfile(R + tag, (major,), v[0], v[-1], v[-1], from_filesystem=True, installable=v)


def _image(tag, **majors):
    return {R + tag: {int(m[2:]): _p(tag, int(m[2:]), *r) for m, r in majors.items()}}


IMAGES = {}
for chunk in (
    _image("pg12.22-ts2.11.2", pg12=("2.1.0", "2.11.2")),
    _image("pg13.12-ts2.11.2-all", pg12=("2.1.0", "2.11.2"), pg13=("2.1.0", "2.11.2")),
    _image("pg13.22-ts2.15.3", pg13=("2.1.0", "2.15.3")),
    _image("pg14.19-ts2.19.3", pg14=("2.11.2", "2.19.3")),
    _image("pg14.19-ts2.19.3-all", pg13=("2.1.0", "2.15.3"), pg14=("2.11.2", "2.19.3")),
    _image("pg15.13-ts2.19.3-all", pg13=("2.1.0", "2.15.3"), pg14=("2.11.2", "2.19.3"),
           pg15=("2.11.2", "2.19.3")),
    _image("pg15.18-ts2.28.3", pg15=("2.13.0", "2.28.3")),
    _image("pg15.18-ts2.28.3-all", pg15=("2.13.0", "2.28.3")),
    _image("pg16.15-ts2.30.1", pg16=("2.13.0", "2.30.1")),
    _image("pg16.15-ts2.30.1-all", pg15=("2.13.0", "2.28.3"), pg16=("2.13.0", "2.30.1")),
    _image("pg18.6-ts2.30.1-all", pg15=("2.13.0", "2.28.3"), pg16=("2.13.0", "2.30.1"),
           pg18=("2.27.2", "2.30.1")),
    _image("pg18.6-ts2.30.1", pg18=("2.27.2", "2.30.1")),
):
    IMAGES.update(chunk)


def _route(src_major, src_ts, tgt_major, tgt_ts, target):
    route, why = find_route(src_major, src_ts, tgt_major, tgt_ts, R + target, IMAGES)
    assert why is None, why
    return route


def _majors(route):
    return [s for s in route.steps if isinstance(s, MajorHop)]


# --- routes ------------------------------------------------------------------


def test_tag_major_is_the_major_the_tag_names():
    assert tag_major(R + "pg15.13-ts2.19.3-all") == 15
    assert tag_major("not a reference") is None


def test_one_major_up_upgrades_at_the_highest_release_both_sides_ship():
    route = _route(12, "2.11.2", 13, "2.15.3", "pg13.22-ts2.15.3")
    (hop,) = _majors(route)
    assert (hop.image, hop.from_pg, hop.to_pg, hop.ts) == (
        R + "pg13.12-ts2.11.2-all", 12, 13, "2.11.2")
    assert hop.run_image == R + "pg13.22-ts2.15.3"
    assert route.majors == (12, 13)


def test_the_fewest_pg_upgrades_skip_a_major_when_an_image_allows():
    route = _route(12, "2.11.2", 18, "2.30.1", "pg18.6-ts2.30.1")
    assert route.majors == (12, 13, 15, 18)


def test_the_old_major_is_updated_first_then_pg_upgrade_runs_there():
    route = _route(12, "2.11.2", 15, "2.28.3", "pg15.18-ts2.28.3")
    kinds = [(type(s).__name__, getattr(s, "to_version", getattr(s, "ts", None))) for s in route.steps]
    assert kinds == [
        ("MajorHop", "2.11.2"),       # 12 -> 13 at 2.11.2
        ("BridgeHop", "2.15.3"),      # on pg13, as far as 13 -> 15 can carry
        ("MajorHop", "2.15.3"),       # 13 -> 15
        ("BridgeHop", "2.28.3"),      # on pg15, to the target
    ]


def test_a_cluster_only_runs_on_its_tagged_major():
    """pg15.13-...-all carries pg13 and pg14 too, but runs only pg15."""
    route = _route(12, "2.11.2", 15, "2.28.3", "pg15.18-ts2.28.3")
    for step in route.steps:
        image = step.run_image if isinstance(step, MajorHop) else step.image
        major = step.to_pg if isinstance(step, MajorHop) else None
        if major is not None:
            assert tag_major(image) == major


def test_a_plain_tag_is_run_before_an_identical_all_tag():
    route = _route(12, "2.11.2", 16, "2.30.1", "pg16.15-ts2.30.1")
    images = {getattr(s, "run_image", None) or s.image for s in route.steps}
    assert R + "pg15.18-ts2.28.3" in images
    assert R + "pg15.18-ts2.28.3-all" not in images


def test_pg_upgrade_prefers_the_image_tagged_with_the_new_major():
    route = _route(15, "2.28.3", 16, "2.30.1", "pg16.15-ts2.30.1")
    assert _majors(route)[0].image == R + "pg16.15-ts2.30.1-all"


def test_no_shared_release_at_or_above_the_source_is_no_route():
    images = {**_image("pg12.22-ts2.11.2", pg12=("2.1.0", "2.11.2")),
              **_image("pg13.12-ts2.1.0-all", pg12=("2.1.0", "2.1.0"), pg13=("2.1.0", "2.1.0")),
              **_image("pg13.22-ts2.15.3", pg13=("2.1.0", "2.15.3"))}
    route, why = find_route(12, "2.11.2", 13, "2.15.3", R + "pg13.22-ts2.15.3", images)
    assert route is None and "at or above 2.11.2" in why


def test_no_image_carrying_both_majors_is_no_route():
    images = {**_image("pg12.22-ts2.11.2", pg12=("2.1.0", "2.11.2")),
              **_image("pg13.22-ts2.15.3", pg13=("2.1.0", "2.15.3"))}
    route, why = find_route(12, "2.11.2", 13, "2.15.3", R + "pg13.22-ts2.15.3", images)
    assert route is None and "carries a pg_upgrade" in why


# --- plans -------------------------------------------------------------------


def _plan(target, source_pg="12.22", source_ts="2.11.2", **kw):
    major = tag_major(R + target)
    return build_plan(source_pg, source_ts, IMAGES[R + target][major], matrix=Matrix([]),
                      verification_run=True, images=IMAGES, **kw)


def test_a_major_change_plans_pg_upgrade_steps_and_one_analyze():
    plan = _plan("pg16.15-ts2.30.1")
    actions = [s["action"] for s in plan["steps"]]
    upgrades = [s for s in plan["steps"] if s["action"] == "major_upgrade"]
    assert [(s["from_pg"], s["to_pg"], s["ts"]) for s in upgrades] == [
        (12, 13, "2.11.2"), (13, 15, "2.15.3"), (15, 16, "2.28.3")]
    assert all(s["link"] is False for s in upgrades)
    assert actions.count("analyze") == 1 and actions[-1] == "analyze"
    for i, step in enumerate(plan["steps"]):
        if step["action"] == "major_upgrade":
            assert plan["steps"][i + 1] == {"hop": step["hop"], "action": "verify_version",
                                            "expect": step["ts"]}


def test_a_known_bad_update_on_the_way_still_gets_its_stop():
    """The database crossed 2.15 on pg13, so pg15's 2.28.3 needs 2.27.2 first (#10272)."""
    plan = _plan("pg16.15-ts2.30.1")
    updates = [(s["from"], s["to"]) for s in plan["steps"] if s["action"] == "update_extension"]
    assert ("2.15.3", "2.27.2") in updates and ("2.27.2", "2.28.3") in updates


def test_link_is_carried_into_every_pg_upgrade_and_the_runbook():
    plan = _plan("pg18.6-ts2.30.1", link=True)
    assert all(s["link"] for s in plan["steps"] if s["action"] == "major_upgrade")
    assert "--link" in render_runbook(plan)
    assert "unusable" in render_runbook(plan)


def test_the_runbook_warns_off_pg_upgrades_own_extension_script():
    """pg_upgrade suggests ALTER EXTENSION timescaledb UPDATE, which would go
    13 -> 15 straight from 2.15.3 to 2.28.3, past the 2.27.2 stop (#10272)."""
    assert "do not run the update_extensions.sql" in render_runbook(_plan("pg16.15-ts2.30.1"))


DIGESTS = {ref: "sha256:" + str(i).rjust(64, "0") for i, ref in enumerate(sorted(IMAGES))}


def _verified(route_steps, start="pg12.22-ts2.11.2"):
    route = {"start": {"image": R + start, "digest": DIGESTS[R + start]}, "steps": route_steps}
    return Matrix([MatrixEntry("12", "2.11", "16", "2.30", verdict="in_place", verified=True,
                               evidence={"empty": "e", "seeded": "s", "link-empty": "le",
                                         "link-seeded": "ls"}, route=route)])


def test_a_cross_pg_plan_pins_both_images_of_every_pg_upgrade():
    plan = _plan("pg16.15-ts2.30.1", digests=DIGESTS)
    for step in (s for s in plan["steps"] if s["action"] == "major_upgrade"):
        assert step["digest"] == DIGESTS[step["image"]]
        assert step["run_digest"] == DIGESTS[step["run_image"]]


def test_the_reviews_reproduction_is_not_called_verified():
    """12 -> 13 -> 15 -> 16 was verified; offering pg18.6-all as well must
    not pass as that route if it changes which image runs a pg_upgrade."""
    verified = route_of(_plan("pg16.15-ts2.30.1", digests=DIGESTS)["steps"])
    images = {k: v for k, v in IMAGES.items() if k != R + "pg16.15-ts2.30.1-all"}
    major = 16
    with pytest.raises(PlanRefused, match="not the verified one"):
        build_plan("12.22", "2.11.2", IMAGES[R + "pg16.15-ts2.30.1"][major],
                   matrix=_verified(verified), images=images, digests=DIGESTS)


def test_an_older_patch_of_the_source_series_catches_up_first_and_says_so():
    """The route was recorded from 2.11.2; a cluster at 2.11.0 first updates
    to 2.11.2 on the pg12 image, then runs the same route."""
    verified = route_of(_plan("pg16.15-ts2.30.1", digests=DIGESTS)["steps"])
    images = dict(IMAGES)
    images[R + "pg12.22-ts2.11.2"] = {12: ImageProfile(
        R + "pg12.22-ts2.11.2", (12,), "2.1.0", "2.11.2", "2.11.2", from_filesystem=True,
        installable=_versions("2.1.0", "2.11.2", extra=("2.11.0",)))}
    plan = build_plan("12.22", "2.11.0", IMAGES[R + "pg16.15-ts2.30.1"][16],
                      matrix=_verified(verified), images=images, digests=DIGESTS)
    assert plan["matrix"]["route_verified"] is True
    assert route_of(plan["steps"])[0]["to"] == "2.11.2"
    assert any("before the verified route" in w and "2.11.2" in w for w in plan["warnings"])


def test_link_without_a_major_change_is_refused():
    with pytest.raises(PlanRefused, match="--link"):
        build_plan("16.15", "2.13.0", IMAGES[R + "pg16.15-ts2.30.1"][16], matrix=Matrix([]),
                   verification_run=True, link=True)


def test_a_major_change_without_measured_contents_is_refused():
    with pytest.raises(PlanRefused, match="measured"):
        build_plan("12.22", "2.11.2", IMAGES[R + "pg13.22-ts2.15.3"][13], matrix=Matrix([]),
                   verification_run=True)


def test_a_major_downgrade_is_refused():
    with pytest.raises(PlanRefused, match="downgrade"):
        build_plan("16.15", "2.13.0", IMAGES[R + "pg15.18-ts2.28.3"][15], matrix=Matrix([]),
                   verification_run=True, images=IMAGES)


def test_every_pg_upgrade_is_preceded_by_a_preflight_with_its_mode():
    """Nothing checked the cluster before the first stop; now each major hop
    starts with a preflight against the image it restarts on (R3)."""
    plan = _plan("pg16.15-ts2.30.1", link=True)
    assert plan["steps"][0]["action"] == "preflight"
    steps = plan["steps"]
    for i, step in enumerate(steps):
        if step["action"] == "major_upgrade":
            before = steps[i - 1]
            assert before["action"] == "preflight"
            assert before["image"] == step["run_image"] and before["target_ts"] == step["ts"]
            assert before["pg_upgrade"] == {"from_pg": step["from_pg"], "to_pg": step["to_pg"],
                                            "link": True, "image": step["image"]}
    assert "remove it once the result is accepted" in render_runbook(plan)


def test_the_runbook_says_what_a_pg_upgrade_does_to_backups():
    assert "stanza-upgrade" in render_runbook(_plan("pg13.22-ts2.15.3"))
