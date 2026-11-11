"""Tests for where images come from: every image a plan still has to run is
checked before anything stops, a registry that does not answer stops with what
can be done about it, and ``--use-cached-images`` only goes on when the images
can really run.

What would hurt if wrong: going on with an image that will not start, after
the cluster has stopped for it. The registry is stubbed here; on a cluster the
harness forces it with a host that cannot resolve (``harness.py --mirror``).
"""

from __future__ import annotations

import pytest

from tsupgrade import preflight as pf
from tsupgrade.apply import ApplyOptions, _route_images, apply_plan
from tsupgrade.images import (
    RegistryError,
    manifest_exists,
    mirrored,
    node_image_name,
    registry_problem,
)
from tsupgrade.plan import render_runbook, render_script
from tsupgrade.preflight import FAIL, PASS, UNKNOWN, PreflightOptions, RouteImage
from tsupgrade.steps import EnsureImage, MajorUpgrade, UpdateExtension
from test_apply_major import Clock, MajorKube, _apply, _plan
from test_majors import _plan as cross_plan

HUB = "timescale/timescaledb-ha:pg13.22-ts2.15.3@sha256:aa"
NAME = "docker.io/timescale/timescaledb-ha@sha256:aa"


# --- naming -------------------------------------------------------------------

def test_a_mirror_keeps_the_image_name_tag_and_digest():
    assert mirrored(HUB, "reg.example.com/dockerhub/timescale/") == (
        "reg.example.com/dockerhub/timescale/timescaledb-ha:pg13.22-ts2.15.3@sha256:aa")
    assert mirrored(HUB, None) == HUB


@pytest.mark.parametrize("value", ["https://reg.example.com/x", "reg.example.com/x@sha256:aa", "timescale"])
def test_a_registry_value_that_cannot_work_is_refused(value):
    assert registry_problem(value)


@pytest.mark.parametrize("value", ["reg.example.com/x", "localhost:5000", "mirror.invalid/timescale"])
def test_a_registry_value_is_a_host_and_a_path(value):
    assert registry_problem(value) is None


@pytest.mark.parametrize("reference,name", [
    (HUB, NAME),
    ("reg.example.com/x/timescaledb-ha:t@sha256:aa", "reg.example.com/x/timescaledb-ha@sha256:aa"),
    ("timescale/timescaledb-ha:t", "docker.io/timescale/timescaledb-ha:t"),
])
def test_an_image_is_named_as_a_node_lists_it(reference, name):
    """As measured on kind: ``host/repo@digest``, Docker Hub as docker.io."""
    assert node_image_name(reference) == name


def test_the_registry_is_asked_by_digest(monkeypatch):
    seen = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(request, timeout):
        seen.append(request.full_url)
        return Answer()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert manifest_exists("reg.example.com/x/timescaledb-ha:t@sha256:aa")[0] is True
    assert seen == ["https://reg.example.com/v2/x/timescaledb-ha/manifests/sha256:aa"]


# --- which images -------------------------------------------------------------

def test_every_image_still_to_run_is_checked_the_pg_upgrade_one_included():
    later = [
        MajorUpgrade(hop=1, from_pg=12, to_pg=13, image="a/i:up", ts="2.11.2", run_image="a/i:run",
                     digest="sha256:1", run_digest="sha256:2", link=False),
        EnsureImage(hop=2, image="a/i:run", digest="sha256:2"),
        UpdateExtension(hop=2, to="2.15.3", sql="x"),
        EnsureImage(hop=3, image="a/i:last", digest="sha256:3"),
    ]
    assert _route_images(later, ApplyOptions()) == (
        RouteImage("a/i:up@sha256:1", False),
        RouteImage("a/i:run@sha256:2", True),
        RouteImage("a/i:last@sha256:3", True),
    )
    mirror = _route_images(later[:1], ApplyOptions(image_registry="reg.example.com/x"))
    assert mirror[0].reference == "reg.example.com/x/i:up@sha256:1"


# --- the check ----------------------------------------------------------------

class Kube:
    def __init__(self, names=(NAME,), policy="IfNotPresent", nodes=1):
        self.names, self.policy, self.nodes = list(names), policy, nodes
        self.writes = []

    def json(self, args, namespaced=True):
        if args[:2] == ["get", "nodes"]:
            return {"items": [{"metadata": {"name": "node-{0}".format(i)},
                               "status": {"images": [{"names": self.names if i == 0 else []}]}}
                              for i in range(self.nodes)]}
        assert args[:2] == ["get", "statefulset"]
        return {"spec": {"template": {"spec": {"containers": [
            {"name": "timescaledb", "imagePullPolicy": self.policy}]}}}}


def _check(monkeypatch, kube, answer="silent", cached=False, on_statefulset=True):
    def registry(reference, timeout):
        if answer == "silent":
            raise RegistryError("cannot reach registry-1.docker.io: Name or service not known")
        return answer == "yes", "answered"

    monkeypatch.setattr(pf, "manifest_exists", registry)
    return pf.check_route_images(kube, PreflightOptions(
        target_image=HUB, route_images=(RouteImage(HUB, on_statefulset),),
        use_cached_images=cached, statefulset="tsdb"))


def test_a_registry_that_answers_is_enough(monkeypatch):
    result = _check(monkeypatch, Kube(names=()), answer="yes")
    assert result.status == PASS and result.notice is None


def test_a_missing_image_stops_even_with_the_option(monkeypatch):
    """The registry answered: this is not an outage."""
    result = _check(monkeypatch, Kube(), answer="no", cached=True)
    assert result.status == FAIL and "does not have" in result.summary


def test_a_silent_registry_stops_by_default_and_says_what_can_be_done(monkeypatch):
    result = _check(monkeypatch, Kube())
    text = "\n".join(result.details)
    assert result.status == FAIL
    assert "does not mean an image is missing" in text and "Nothing has been changed" in text
    assert "Running without the registry is possible" in text
    for option in ("try again", "--image-registry", "--use-cached-images"):
        assert option in text


def test_the_option_goes_on_when_the_images_can_run(monkeypatch):
    result = _check(monkeypatch, Kube(), cached=True)
    assert result.status == UNKNOWN and "going on" in result.summary
    assert "--use-cached-images" in result.notice


@pytest.mark.parametrize("kube,said", [
    (Kube(names=()), "is not on node-0"),
    (Kube(nodes=2), "is not on node-1"),
    (Kube(names=["docker.io/timescale/timescaledb-ha:pg13.22-ts2.15.3"]), "is not on node-0"),
    (Kube(policy="Always"), "imagePullPolicy Always"),
    (Kube(policy=None), "cannot read the pull policy"),
])
def test_the_option_does_not_override_an_image_that_cannot_run(monkeypatch, kube, said):
    """A tag on the node is not the verified image: the digest has to be."""
    result = _check(monkeypatch, kube, cached=True)
    assert result.status == FAIL
    assert said in "\n".join(result.details)


def test_the_message_says_how_to_change_the_pull_policy(monkeypatch):
    text = "\n".join(_check(monkeypatch, Kube(policy="Always")).details)
    assert "kubectl -n timescale patch statefulset tsdb" in text
    assert '"imagePullPolicy":"IfNotPresent"' in text
    assert "--use-cached-images, once the above is fixed" in text


def test_the_upgrade_pods_image_does_not_depend_on_the_statefulsets_policy(monkeypatch):
    """apply's one-off pod is always IfNotPresent."""
    result = _check(monkeypatch, Kube(policy="Always"), cached=True, on_statefulset=False)
    assert result.status == UNKNOWN


# --- apply --------------------------------------------------------------------

def test_apply_runs_every_image_from_the_named_registry():
    kube = MajorKube()
    report = _apply(_plan(), kube, image_registry="reg.example.com/x")
    assert report["result"] == "pass", report.get("failure")
    assert report["image_registry"] == "reg.example.com/x"
    images = [w for w in kube.writes if w.startswith(("run ", "set image"))]
    assert images and all("reg.example.com/x/" in w for w in images), images
    for pod in kube.pods:
        assert pod["image"].startswith("reg.example.com/x/")


def test_a_notice_reaches_the_operator_and_the_report(monkeypatch):
    class Result:
        name, status, summary = "route_images", UNKNOWN, "s"
        notice, details = "read this", []

    told, clock = [], Clock()
    plan = _plan()
    plan["steps"].insert(0, {"hop": 1, "action": "preflight", "image": plan["target"]["image"],
                             "target_pg": "13.22", "target_ts": "2.11.2"})
    report = apply_plan(
        plan,
        ApplyOptions(poll_interval=1, rollout_timeout=5, acknowledge_verification=True,
                     selector="application=patroni,cluster-name=tsdb", notify=told.append),
        kube=MajorKube(), preflight=lambda o: [Result()], sleep=clock.sleep, clock=clock)
    assert "read this" in told
    assert any("read this" in s["summary"] for s in report["steps"] if s["action"] == "preflight")


def test_the_registry_is_asked_once_before_the_first_hop():
    """A digest does not change: an outage at a later hop must not stop a run
    whose images were confirmed."""
    asked, clock = [], Clock()
    plan = _plan()
    target = plan["target"]["image"]
    plan["steps"][1:1] = [
        {"hop": 2, "action": "preflight", "image": target, "target_pg": "13.22", "target_ts": "2.11.2"}]
    plan["steps"].insert(0, {"hop": 1, "action": "preflight", "image": target,
                             "target_pg": "13.22", "target_ts": "2.11.2"})

    def preflight(options):
        asked.append((options.route_images, options.route_checked))
        return []

    report = apply_plan(
        plan, ApplyOptions(poll_interval=1, rollout_timeout=5, acknowledge_verification=True,
                           selector="application=patroni,cluster-name=tsdb"),
        kube=MajorKube(), preflight=preflight, sleep=clock.sleep, clock=clock)
    assert report["result"] == "pass", report.get("failure")
    assert asked[0][0] and asked[0][1] is False
    assert asked[1] == ((), True)


def test_a_later_hop_does_not_ask_the_registry(monkeypatch):
    called = []
    monkeypatch.setattr(pf, "manifest_exists", lambda *a, **k: called.append(a))
    monkeypatch.setattr(pf, "Kube", lambda *a: Kube())
    for name in ("check_context", "check_helm", "check_abi_break", "check_pg_minor_floor", "check_disk_headroom",
                 "check_extensions", "check_collation", "check_permissions", "check_drift",
                 "check_gates", "check_matrix"):
        monkeypatch.setattr(pf, name, lambda *a, **k: None)
    monkeypatch.setattr(pf, "discover_instances", lambda *a: ([], None))
    pf.run_preflight(PreflightOptions(target_image=HUB.split("@")[0], route_checked=True))
    assert called == []


def test_the_plan_file_keeps_the_verified_route_the_runbook_names_the_mirror():
    plan = cross_plan("pg16.15-ts2.30.1")
    before = str(plan)
    for text in (render_runbook(plan, "reg.example.com/x"), render_script(plan, "reg.example.com/x")):
        assert "reg.example.com/x/timescaledb-ha:" in text
    assert str(plan) == before and "reg.example.com" not in before
    assert "reg.example.com" not in render_runbook(plan)
