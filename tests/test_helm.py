"""A Helm-owned StatefulSet is reported and warned about, never refused.

The next ``helm upgrade`` puts the chart's image back over whatever ``apply``
set, so every output that changes the image ends with what to put in the
chart's values: preflight and ``apply`` when they see Helm's marks, the
runbook and the script always (``plan`` is offline and cannot look).
"""

from __future__ import annotations

import pytest

from tsupgrade.apply import render_report
from tsupgrade.helm import HELM_HEADING, helm_owner, split_image, values_note
from tsupgrade.plan import final_image, render_runbook, render_script
from tsupgrade.preflight import PASS, UNKNOWN, PreflightError, PreflightOptions, check_helm
from test_apply import PG15_18, FakeKube, _plan, _run
from test_apply_major import MajorKube, _apply, _plan as major_plan
from test_majors import _plan as cross_plan

RELEASE = {"annotations": {"meta.helm.sh/release-name": "tsdb"},
           "labels": {"app.kubernetes.io/managed-by": "Helm"}}


@pytest.mark.parametrize("meta,owner", [
    (RELEASE, "tsdb"),
    ({"labels": {"app.kubernetes.io/managed-by": "Helm"}}, "Helm"),
    ({"labels": {"app.kubernetes.io/managed-by": "kustomize"}}, None),
    ({}, None),
])
def test_helm_owner_reads_the_release_annotation_or_the_label(meta, owner):
    assert helm_owner({"metadata": meta}) == owner


@pytest.mark.parametrize("ref,parts", [
    ("timescale/timescaledb-ha:pg16.15-ts2.30.1@sha256:abc",
     ("timescale/timescaledb-ha", "pg16.15-ts2.30.1", "sha256:abc")),
    ("reg.example.com:5000/x/timescaledb-ha:pg16.15-ts2.30.1",
     ("reg.example.com:5000/x/timescaledb-ha", "pg16.15-ts2.30.1", None)),
    ("reg.example.com:5000/timescaledb-ha@sha256:abc", ("reg.example.com:5000/timescaledb-ha", None, "sha256:abc")),
])
def test_split_image_keeps_a_registry_port_out_of_the_tag(ref, parts):
    assert split_image(ref) == parts


def test_the_note_names_repository_tag_and_digest():
    note = values_note("timescale/timescaledb-ha:pg16.15-ts2.30.1@sha256:abc", "tsdb", major=True,
                       statefulset="tsdb")
    assert note[0] == HELM_HEADING + " (statefulset/tsdb is managed by Helm release tsdb):"
    text = "\n".join(note)
    assert "timescale/timescaledb-ha" in text and "pg16.15-ts2.30.1" in text and "sha256:abc" in text
    assert "puts the old image back" in text and "will not start" in text


# --- preflight -----------------------------------------------------------------

class StsKube:
    def __init__(self, meta, owners=("tsdb",), fail=False):
        self.meta, self.owners, self.fail = meta, owners, fail

    def json(self, args, namespaced=True):
        if self.fail:
            raise PreflightError("forbidden")
        if args[:2] == ["get", "pods"]:
            return {"items": [{"metadata": {"ownerReferences": [{"kind": "StatefulSet", "name": o}]}}
                              for o in self.owners]}
        assert args[:3] == ["get", "statefulset", "tsdb"], args
        return {"metadata": self.meta}


def test_preflight_reports_a_helm_owned_statefulset_as_a_pass_with_the_values():
    result = check_helm(StsKube(RELEASE), PreflightOptions("timescale/timescaledb-ha:pg16.15-ts2.30.1"))
    assert result.status == PASS
    assert result.summary == "statefulset/tsdb is managed by Helm release tsdb"
    assert any("pg16.15-ts2.30.1" in d for d in result.details)
    assert result.notice and "helm upgrade" in result.notice


def test_preflight_finds_the_statefulset_from_the_pods_owner_when_not_named():
    kube = StsKube({})
    result = check_helm(kube, PreflightOptions("x"))
    assert (result.status, result.summary) == (PASS, "statefulset/tsdb is not managed by Helm")


@pytest.mark.parametrize("kube", [StsKube({}, owners=()), StsKube({}, owners=("a", "b")),
                                  StsKube({}, fail=True)])
def test_preflight_says_unknown_when_it_cannot_tell(kube):
    assert check_helm(kube, PreflightOptions("x")).status == UNKNOWN


def test_the_notice_is_given_once_per_apply_run_not_at_every_hop():
    later = PreflightOptions("x", statefulset="tsdb", route_checked=True)
    assert check_helm(StsKube(RELEASE), later).notice is None


# --- plan ----------------------------------------------------------------------

def test_the_runbook_and_the_script_always_end_with_the_conditional_note():
    plan = cross_plan("pg16.15-ts2.30.1")
    image = final_image(plan)
    repository, tag, _ = split_image(image)
    runbook = render_runbook(plan)
    tail = runbook[runbook.index(HELM_HEADING):]
    assert "if a Helm chart manages this StatefulSet" in tail
    assert repository in tail and tag in tail and "will not start" in tail
    script = render_script(plan)
    assert "# " + HELM_HEADING + " (if a Helm chart manages this StatefulSet):" in script


def test_the_final_image_is_the_last_one_rolled_out_by_digest():
    plan = cross_plan("pg16.15-ts2.30.1")
    last = [s for s in plan["steps"] if s["action"] in ("ensure_image", "major_upgrade")][-1]
    expect = (last.get("run_image") or last["image"])
    assert final_image(plan).startswith(expect)
    assert final_image(plan, "reg.example.com/x").startswith("reg.example.com/x/timescaledb-ha:")


# --- apply ---------------------------------------------------------------------

class HelmKube(FakeKube):
    def json(self, args, namespaced=True):
        doc = super().json(args, namespaced)
        if args[:2] == ["get", "statefulset"]:
            doc["metadata"] = RELEASE
        return doc


def test_apply_ends_with_the_values_to_change_when_helm_owns_the_statefulset():
    kube = HelmKube("timescale/timescaledb-ha:pg15.13-ts2.20.3", "15.18", {"app": "2.20.3"})
    report = _run(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    assert report["helm"]["release"] == "tsdb"
    assert report["helm"]["image"] == kube.template
    assert HELM_HEADING in render_report(report)


def test_apply_says_nothing_of_helm_when_the_image_did_not_change_or_helm_is_absent():
    kube = HelmKube(PG15_18, "15.18", {"app": "2.20.3"})
    assert "helm" not in _run(_plan(), kube)       # already on the image
    kube = FakeKube("timescale/timescaledb-ha:pg15.13-ts2.20.3", "15.18", {"app": "2.20.3"})
    assert "helm" not in _run(_plan(), kube)       # no Helm marks


def test_a_major_upgrade_on_a_helm_statefulset_warns_the_pods_would_not_start():
    kube = MajorKube()
    orig = kube.json

    def json(args, namespaced=True):
        doc = orig(args, namespaced)
        if args[:2] == ["get", "statefulset"]:
            doc["metadata"] = RELEASE
        return doc

    kube.json = json
    report = _apply(major_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    assert any("will not start" in l for l in report["helm"]["note"])
