"""Tests for ``apply``'s major upgrade: the order of the writes, and what a
failure leaves behind.

The fake kubectl here models what a major upgrade touches: the StatefulSet's
scale and image, the pg_upgrade pod, the replicas' disks and Patroni's DCS
objects. The order matters more than anything else: the data has to be on
ordinal 0 before the cluster stops, nothing may be deleted until pg_upgrade
has succeeded, and the replicas' disks go before anything starts again.
"""

from __future__ import annotations

import json

import pytest

from tsupgrade.apply import _PG_UPGRADE_SCRIPT, ApplyOptions, apply_plan, check_plan
from test_apply import FakeKube, _all_pass

REPO = "timescale/timescaledb-ha:"
OLD = REPO + "pg12.22-ts2.11.2"
BOTH = REPO + "pg13.12-ts2.11.2-all"
NEW = REPO + "pg13.22-ts2.15.3"


class MajorKube(FakeKube):
    def __init__(self, primary=0, upgrade_phase="Succeeded", **kw):
        super().__init__(OLD, "12.22", {"verify": "2.11.2"}, replicas=1)
        for pod in self.pods:
            pod["role"] = "primary" if pod["name"] == "tsdb-{0}".format(primary) else "replica"
        self.replicas = 2
        self.major = 12
        self.sysid = "7000000000000000001"
        self.upgrade_phase = upgrade_phase
        self.upgrade_pod = None
        self.spec_seen = None

    def json(self, args, namespaced=True):
        if args[:2] == ["get", "statefulset"]:
            return {"spec": {
                "replicas": self.replicas,
                "template": {"spec": {
                    "securityContext": {"runAsUser": 1000, "fsGroup": 1000},
                    "containers": [{"name": "timescaledb", "image": self.template,
                                    "volumeMounts": [{"name": "pgdata",
                                                      "mountPath": "/home/postgres/pgdata"}]}]}},
                "volumeClaimTemplates": [{"metadata": {"name": "pgdata"}}]}}
        if args[:2] == ["get", "pod"] and args[2] == self.upgrade_pod:
            return {"status": {"phase": self.upgrade_phase}}
        return super().json(args, namespaced)

    def run(self, args, namespaced=True):
        if args[0] == "scale":
            n = int(args[-1].split("=")[1])
            self.writes.append("scale {0}".format(n))
            if n == 0:
                self.pods = []
            else:
                self.pods = [{"name": "tsdb-{0}".format(i), "role": "primary" if i == 0 else "replica",
                              "image": self.template, "uid": "new-{0}".format(i)} for i in range(n)]
                self.server = "13.12"
                self.major = 13
                self.sysid = "7000000000000000002"
            return 0, "", ""
        if args[0] == "run":
            self.upgrade_pod = args[1]
            self.spec_seen = json.loads(args[args.index("--overrides") + 1])
            self.writes.append("run {0} on {1}".format(args[1], args[args.index("--image") + 1]))
            return 0, "", ""
        if args[0] == "logs":
            if self.upgrade_phase != "Succeeded":
                return 0, "pg_upgrade --check failed:\nlocale mismatch", ""
            env = {e["name"]: e["value"] for e in self.spec_seen["spec"]["containers"][0]["env"]}
            said = "Linking" if env["LINK"] else "Copying"
            return 0, "\n".join(["{0} user relation files".format(said)] + ["ok"] * 20
                                + ["upgraded to PostgreSQL 13"]), ""
        if args[:2] == ["delete", "pod"] and args[2].startswith("tsdb-pg-upgrade-"):
            return 0, "", ""
        if args[:2] == ["delete", "pvc"]:
            self.writes.append("delete pvc " + args[2])
            return 0, "", ""
        if args[:2] == ["delete", "endpoints,configmaps"]:
            self.writes.append("delete dcs " + args[args.index("-l") + 1])
            return 0, "endpoints \"tsdb\" deleted", ""
        return super().run(args, namespaced)

    def exec(self, pod, container, argv):
        if argv[0] == "patronictl":
            self.writes.append("switchover to " + argv[argv.index("--candidate") + 1])
            for p in self.pods:
                p["role"] = "primary" if p["name"] == argv[argv.index("--candidate") + 1] else "replica"
            return 0, "", ""
        if argv[0] == "vacuumdb":
            self.writes.append("vacuumdb on " + pod)
            return 0, "", "vacuumdb: processing database \"verify\": Generating minimal optimizer statistics (1 target)"
        sql = argv[-1]
        if sql == "SELECT system_identifier FROM pg_control_system()":
            return 0, self.sysid, ""
        if sql.startswith("SELECT current_setting('server_version_num')"):
            return 0, "{0}|{1}".format(self.major, self.sysid), ""
        return super().exec(pod, container, argv)


def _plan(link=False):
    return {
        "plan_format": 1, "mode": "verification",
        "source": {"pg": "12.22", "ts": "2.11.2"},
        "target": {"image": NEW, "pg": "13.22", "ts": "2.11.2"},
        "steps": [
            {"hop": 1, "action": "major_upgrade", "image": BOTH, "from_pg": 12, "to_pg": 13,
             "ts": "2.11.2", "run_image": NEW, "link": link},
            {"hop": 1, "action": "verify_version", "expect": "2.11.2"},
            {"hop": 1, "action": "analyze", "why": "statistics"},
        ],
    }


def _apply(plan, kube):
    return apply_plan(plan, ApplyOptions(poll_interval=0, rollout_timeout=5,
                                         acknowledge_verification=True,
                                         selector="application=patroni,cluster-name=tsdb"),
                      kube=kube, preflight=_all_pass, sleep=lambda s: None)


def test_major_upgrade_writes_in_the_one_safe_order():
    kube = MajorKube()
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    assert kube.writes == [
        "scale 0",
        "run tsdb-pg-upgrade-12-13 on " + BOTH,
        "delete pvc pgdata-tsdb-1",
        "delete dcs application=patroni,cluster-name=tsdb",
        "set image " + NEW,
        "scale 2",
        "vacuumdb on tsdb-0",
    ]
    step = report["steps"][0]
    assert "PostgreSQL 12 -> 13" in step["summary"]
    assert any("system identifier 7000000000000000001" in l for l in step["output"])
    assert any("system identifier 7000000000000000002" in l for l in step["output"])


def test_the_upgrade_pod_runs_on_ordinal_0s_disk_as_the_statefulsets_user():
    kube = MajorKube()
    _apply(_plan(link=True), kube)
    spec = kube.spec_seen["spec"]
    assert spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == "pgdata-tsdb-0"
    assert spec["securityContext"]["runAsUser"] == 1000
    env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
    assert env == {"FROM": "12", "TO": "13", "PG_BIN_OLD": "/usr/lib/postgresql/12/bin",
                   "PG_BIN_NEW": "/usr/lib/postgresql/13/bin",
                   "DATA": "/home/postgres/pgdata/data", "LINK": "--link"}


@pytest.mark.parametrize("link,said", [(False, "Copying"), (True, "Linking")])
def test_pg_upgrades_word_on_copy_or_link_is_kept_from_mid_log(link, said):
    report = _apply(_plan(link=link), MajorKube())
    output = report["steps"][0]["output"]
    assert "{0} user relation files".format(said) in output
    assert "upgraded to PostgreSQL 13" in output


def test_the_script_counts_hard_links_to_prove_the_mode():
    assert "-links +1" in _PG_UPGRADE_SCRIPT


def test_a_leader_elsewhere_is_switched_to_ordinal_0_before_anything_stops():
    kube = MajorKube(primary=1)
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    assert kube.writes[:2] == ["switchover to tsdb-0", "scale 0"]


def test_a_failed_pg_upgrade_deletes_nothing_and_says_how_to_start_again():
    kube = MajorKube(upgrade_phase="Failed")
    report = _apply(_plan(), kube)
    assert report["result"] == "fail"
    assert kube.writes == ["scale 0", "run tsdb-pg-upgrade-12-13 on " + BOTH]
    failure = report["failure"]
    assert failure["action"] == "major_upgrade"
    assert "locale mismatch" in failure["error"]
    assert any("kubectl scale statefulset/tsdb --replicas=2" in l for l in failure["error"])


@pytest.mark.parametrize("mutate,reason", [
    (lambda s: s.update(to_pg=12), "from_pg below to_pg"),
    (lambda s: s.pop("run_image"), "image and run_image"),
    (lambda s: s.update(ts="2.11"), "'ts' is not a version"),
    (lambda s: s.update(link="yes"), "link true or false"),
])
def test_malformed_major_steps_are_refused(mutate, reason):
    plan = _plan()
    mutate(plan["steps"][0])
    reasons = check_plan(plan, ApplyOptions(acknowledge_verification=True))
    assert any(reason in r for r in reasons), reasons
