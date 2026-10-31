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
import shutil
import subprocess

import pytest

from tsupgrade.apply import _PG_UPGRADE_SCRIPT, ApplyOptions, apply_plan, check_plan
from tsupgrade.plan import PLAN_FORMAT
from test_apply import FakeKube, _all_pass

REPO = "timescale/timescaledb-ha:"
OLD = REPO + "pg12.22-ts2.11.2"
BOTH = REPO + "pg13.12-ts2.11.2-all"
NEW = REPO + "pg13.22-ts2.15.3"


class MajorKube(FakeKube):
    """``phases``: what the upgrade pod reports on each look, the last one
    repeating; ``leftover``: the phase of an upgrade pod already there."""

    def __init__(self, primary=0, upgrade_phase="Succeeded", phases=None, leftover=None,
                 fail_pvc=False, **kw):
        super().__init__(OLD, "12.22", {"verify": "2.11.2"}, replicas=1)
        for pod in self.pods:
            pod["role"] = "primary" if pod["name"] == "tsdb-{0}".format(primary) else "replica"
        self.replicas = 2
        self.major = 12
        self.sysid = "7000000000000000001"
        self.upgrade_phase = upgrade_phase
        self.phases = list(phases or [upgrade_phase])
        self.upgrade_pod = "tsdb-pg-upgrade-12-13" if leftover else None
        self.leftover = leftover
        self.fail_pvc = fail_pvc
        self.dcs_extra = []
        self.spec_seen = None

    def json(self, args, namespaced=True):
        if args[:2] == ["get", "statefulset"]:
            return {"spec": {
                "replicas": self.replicas,
                "updateStrategy": {"type": "OnDelete"},
                "template": {"spec": {
                    "securityContext": {"runAsUser": 1000, "fsGroup": 1000},
                    "containers": [{"name": "timescaledb", "image": self.template,
                                    "volumeMounts": [{"name": "pgdata",
                                                      "mountPath": "/home/postgres/pgdata"}]}]}},
                "volumeClaimTemplates": [{"metadata": {"name": "pgdata"}}]}}
        if args[:2] == ["get", "endpoints,configmaps"]:
            names = [("Endpoints", "tsdb"), ("Endpoints", "tsdb-config")] + self.dcs_extra
            return {"items": [{"kind": k, "metadata": {"name": n}} for k, n in names]}
        return super().json(args, namespaced)

    def _upgrade_status(self):
        if self.leftover:
            return {"phase": self.leftover}
        phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        self.upgrade_phase = phase
        status = {"phase": phase}
        if phase == "Pending":
            status["containerStatuses"] = [{"state": {"waiting": {
                "reason": "ErrImagePull", "message": "pull access denied"}}}]
        return status

    def run(self, args, namespaced=True):
        if args[:2] == ["get", "pod"] and args[2].startswith("tsdb-pg-upgrade-"):
            if args[2] != self.upgrade_pod:
                return 0, "", ""
            return 0, json.dumps({"status": self._upgrade_status()}), ""
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
            if getattr(self, "failed_log", None):
                return 0, self.failed_log, ""
            if self.leftover:
                return 0, "upgraded to PostgreSQL 13 (an earlier run)", ""
            if self.upgrade_phase != "Succeeded":
                return 0, "pg_upgrade --check failed:\nlocale mismatch", ""
            env = {e["name"]: e["value"] for e in self.spec_seen["spec"]["containers"][0]["env"]}
            said = "Linking" if env["LINK"] else "Copying"
            return 0, "\n".join(["{0} user relation files".format(said)] + ["ok"] * 20
                                + ["upgraded to PostgreSQL 13"]), ""
        if args[:2] == ["delete", "pod"] and args[2].startswith("tsdb-pg-upgrade-"):
            self.writes.append("delete pod " + args[2])
            return 0, "", ""
        if args[:2] == ["delete", "pvc"]:
            if self.fail_pvc:
                return 1, "", "persistentvolumeclaims is forbidden"
            self.writes.append("delete pvc " + args[2])
            return 0, "", ""
        if args[0] == "delete" and any(a.startswith(("endpoints/", "configmap/")) for a in args):
            self.writes.append("delete dcs " + " ".join(a for a in args[1:] if "/" in a))
            return 0, "", ""
        return super().run(args, namespaced)

    def exec(self, pod, container, argv):
        if argv[:2] == ["patronictl", "list"]:
            return 0, json.dumps([{"Cluster": "tsdb", "Member": p["name"]} for p in self.pods]), ""
        if argv[0] == "patronictl":
            self.writes.append("switchover to " + argv[argv.index("--candidate") + 1])
            for p in self.pods:
                p["role"] = "primary" if p["name"] == argv[argv.index("--candidate") + 1] else "replica"
            return 0, "", ""
        if argv[0] == "vacuumdb":
            self.writes.append("vacuumdb on " + pod)
            return 0, "", "vacuumdb: processing database \"verify\": Generating minimal optimizer statistics (1 target)"
        sql = argv[-1]
        if "WHERE datname = 'template0'" in sql:
            self.locale_sql = sql
            return 0, "UTF8|en_US.UTF-8|en_US.UTF-8|c|", ""
        if sql == "SELECT system_identifier FROM pg_control_system()":
            return 0, self.sysid, ""
        if sql.startswith("SELECT current_setting('server_version_num')"):
            return 0, "{0}|{1}".format(self.major, self.sysid), ""
        return super().exec(pod, container, argv)


def _plan(link=False):
    return {
        "plan_format": PLAN_FORMAT, "mode": "verification",
        "source": {"pg": "12.22", "ts": "2.11.2"},
        "target": {"image": NEW, "pg": "13.22", "ts": "2.11.2"},
        "steps": [
            {"hop": 1, "action": "major_upgrade", "image": BOTH, "from_pg": 12, "to_pg": 13,
             "ts": "2.11.2", "run_image": NEW, "link": link},
            {"hop": 1, "action": "verify_version", "expect": "2.11.2"},
            {"hop": 1, "action": "analyze", "why": "statistics"},
        ],
    }


class Clock:
    """Time that moves only when apply sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1.0)


def _apply(plan, kube, **opts):
    clock = Clock()
    opts = dict(dict(poll_interval=1, rollout_timeout=5, acknowledge_verification=True,
                     selector="application=patroni,cluster-name=tsdb"), **opts)
    return apply_plan(plan, ApplyOptions(**opts), kube=kube, preflight=_all_pass,
                      sleep=clock.sleep, clock=clock)


def test_major_upgrade_writes_in_the_one_safe_order():
    kube = MajorKube()
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    assert kube.writes == [
        "scale 0",
        "run tsdb-pg-upgrade-12-13 on " + BOTH,
        "delete pod tsdb-pg-upgrade-12-13",
        "delete pvc pgdata-tsdb-1",
        "delete dcs endpoints/tsdb endpoints/tsdb-config",
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
                   "DATA": "/home/postgres/pgdata/data", "LINK": "--link",
                   "ENCODING": "UTF8", "LC_COLLATE": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8",
                   "LOCALE_PROVIDER": "libc", "PROVIDER_LOCALE": ""}


@pytest.mark.parametrize("link,said", [(False, "Copying"), (True, "Linking")])
def test_pg_upgrades_word_on_copy_or_link_is_kept_from_mid_log(link, said):
    report = _apply(_plan(link=link), MajorKube())
    output = report["steps"][0]["output"]
    assert "{0} user relation files".format(said) in output
    assert "upgraded to PostgreSQL 13" in output


def test_the_script_counts_hard_links_to_prove_the_mode():
    assert "-links +1" in _PG_UPGRADE_SCRIPT


def test_the_script_never_runs_pg_upgrades_extension_script_and_removes_it():
    assert "rm -f update_extensions.sql" in _PG_UPGRADE_SCRIPT
    assert "psql" not in _PG_UPGRADE_SCRIPT


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
    assert any("kubectl -n timescale scale statefulset/tsdb --replicas=2" in l for l in failure["error"])


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


# --- the wait, the leftover pod, and what every failure says (R2) ------------

def test_a_long_pg_upgrade_is_waited_for_without_a_limit_by_default():
    """Copy mode on a real database outlasts any rollout timeout."""
    kube = MajorKube(phases=["Pending"] + ["Running"] * 50 + ["Succeeded"])
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")


def test_running_past_the_pg_upgrade_timeout_keeps_the_pod_and_says_how_to_go_on():
    kube = MajorKube(phases=["Running"])
    report = _apply(_plan(), kube, pg_upgrade_timeout=10)
    assert report["result"] == "fail"
    assert not any(w.startswith("delete") for w in kube.writes)
    failure = report["failure"]
    assert "still running in pod tsdb-pg-upgrade-12-13" in failure["summary"]
    error = "\n".join(failure["error"])
    assert "do not start the cluster while it runs" in error
    assert "logs -f tsdb-pg-upgrade-12-13" in error
    assert "delete pvc pgdata-tsdb-1" in error and "--replicas=2" in error


def test_a_pod_that_never_starts_fails_after_the_rollout_timeout_and_is_left():
    kube = MajorKube(phases=["Pending"])
    report = _apply(_plan(), kube)
    assert report["result"] == "fail"
    failure = report["failure"]
    assert "has not started" in failure["summary"] and "ErrImagePull" in failure["summary"]
    assert "data is untouched" in failure["summary"]
    error = "\n".join(failure["error"])
    assert "delete pod tsdb-pg-upgrade-12-13" in error
    assert "scale statefulset/tsdb --replicas=2" in error
    assert "delete pod tsdb-pg-upgrade-12-13" not in kube.writes


@pytest.mark.parametrize("phase", ["Running", "Pending"])
def test_an_upgrade_pod_already_running_is_refused_not_deleted(phase):
    kube = MajorKube(leftover=phase)
    report = _apply(_plan(), kube)
    assert report["result"] == "fail"
    assert kube.writes == []
    assert "already exists and is {0}".format(phase) in report["failure"]["summary"]


@pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
def test_a_finished_pod_from_an_earlier_run_is_shown_and_refused(phase):
    kube = MajorKube(leftover=phase)
    report = _apply(_plan(), kube)
    assert kube.writes == []
    failure = report["failure"]
    assert "left from an earlier run ({0})".format(phase) in failure["summary"]
    assert "upgraded to PostgreSQL 13 (an earlier run)" in failure["error"]


def test_a_failure_after_pg_upgrade_says_the_data_is_upgraded_and_how_to_finish():
    kube = MajorKube(fail_pvc=True)
    report = _apply(_plan(), kube)
    assert report["result"] == "fail"
    error = "\n".join(report["failure"]["error"])
    assert "upgraded to PostgreSQL 13" in error and "do not start it on PostgreSQL 12" in error
    assert "/home/postgres/pgdata/data.pg12" in error
    assert "set image statefulset/tsdb timescaledb=" + NEW in error
    assert ("delete endpoints/tsdb endpoints/tsdb-config endpoints/tsdb-failover "
            "--ignore-not-found") in error
    assert "data untouched" not in error


def test_every_failure_after_the_scale_to_0_says_how_to_start_again():
    """A failed pg_upgrade and a pod that cannot be created both leave the
    cluster stopped; the report must say so."""
    for kube in (MajorKube(upgrade_phase="Failed"), MajorKube(phases=["Pending"])):
        report = _apply(_plan(), kube)
        assert any("scale statefulset/tsdb --replicas=2" in l for l in report["failure"]["error"])


# --- a failed --link: what the old cluster needs before it starts (R4) -------

def test_a_failed_link_after_linking_began_says_to_rename_pg_control_back():
    """pg_upgrade's docs: once linking starts it appends .old to the old
    cluster's global/pg_control; while the new cluster has never started,
    removing the suffix makes the old one usable again."""
    kube = MajorKube(upgrade_phase="Failed")
    kube.failed_log = ("pg_upgrade  failed:\nold cluster disabled: "
                       "/home/postgres/pgdata/data/global/pg_control.old")
    report = _apply(_plan(link=True), kube)
    error = "\n".join(report["failure"]["error"])
    assert "will not start" in error
    assert ("mv /home/postgres/pgdata/data/global/pg_control.old "
            "/home/postgres/pgdata/data/global/pg_control") in error
    assert "never started" in error
    assert "data untouched" not in error
    assert "scale statefulset/tsdb --replicas=2" in error


def test_a_failed_link_before_linking_began_leaves_the_old_cluster_as_it_was():
    report = _apply(_plan(link=True), MajorKube(upgrade_phase="Failed"))
    error = "\n".join(report["failure"]["error"])
    assert "data untouched" in error and "pg_control" not in error


def test_after_a_successful_link_the_report_says_when_the_old_directory_is_reusable():
    kube = MajorKube(fail_pvc=True)
    report = _apply(_plan(link=True), kube)
    error = "\n".join(report["failure"]["error"])
    assert "reusable only while the new cluster has never started" in error


# --- the script itself, on fake binaries -------------------------------------

BASH = shutil.which("bash")
FAKE_PG_UPGRADE = r"""#!/bin/bash
# Fake pg_upgrade: $FAKE decides how it fails. -d is the old data directory.
while [ $# -gt 0 ]; do case "$1" in -d) old="$2"; shift;; -D) new="$2"; shift;; --check) check=1;; esac; shift; done
if [ -n "${check:-}" ]; then [ "$FAKE" = check ] && { echo "check failed"; exit 1; }; exit 0; fi
case "$FAKE" in
  link) mv "$old/global/pg_control" "$old/global/pg_control.old"; echo "Linking user relation files"; exit 1;;
  ok) mkdir -p "$new/base/1"; touch "$new/base/1/1234"; echo "Copying user relation files ok"; exit 0;;
esac
"""


def _fake_cluster(tmp_path):
    old_bin, new_bin = tmp_path / "old", tmp_path / "new"
    for d in (old_bin, new_bin):
        d.mkdir()
    (old_bin / "pg_controldata").write_text("#!/bin/bash\necho 'Data page checksum version: 1'\n",
                                           newline="\n")
    (old_bin / "postgres").write_text("#!/bin/bash\n", newline="\n")
    (new_bin / "initdb").write_text(
        "#!/bin/bash\n[ \"$1\" = --help ] && exit 0\necho \"$@\" > \"$(dirname \"$0\")/initdb.args\"\n"
        "while [ $# -gt 0 ]; do [ \"$1\" = -D ] && mkdir -p \"$2\"; shift; done\n", newline="\n")
    (new_bin / "pg_upgrade").write_text(FAKE_PG_UPGRADE, newline="\n")
    data = tmp_path / "pgdata" / "data"
    (data / "global").mkdir(parents=True)
    (data / "global" / "pg_control").write_text("x")
    return old_bin, new_bin, data


def _script(tmp_path, fake, link="--link", locale_provider="libc"):
    old_bin, new_bin, data = _fake_cluster(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "FROM": "12", "TO": "13", "PG_BIN_OLD": old_bin.as_posix(),
           "PG_BIN_NEW": new_bin.as_posix(), "DATA": data.as_posix(), "LINK": link, "FAKE": fake,
           "ENCODING": "UTF8", "LC_COLLATE": "C.UTF-8", "LC_CTYPE": "C.UTF-8",
           "LOCALE_PROVIDER": locale_provider, "PROVIDER_LOCALE": "und-x-icu"}
    done = subprocess.run([BASH, "-c", _PG_UPGRADE_SCRIPT], env=env, capture_output=True, text=True)
    return done.returncode, done.stdout, data


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_the_script_reports_a_disabled_old_cluster_when_link_fails_mid_way(tmp_path):
    rc, out, data = _script(tmp_path, "link")
    assert rc == 1
    assert "old cluster disabled: {0}/global/pg_control.old".format(data.as_posix()) in out


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_the_script_reports_nothing_changed_when_the_check_fails(tmp_path):
    rc, out, data = _script(tmp_path, "check")
    assert rc == 1
    assert "old cluster disabled" not in out and "moved" not in out
    assert (data / "global" / "pg_control").exists()


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_the_script_swaps_the_directories_on_success(tmp_path):
    rc, out, data = _script(tmp_path, "ok", link="")
    assert rc == 0, out
    assert (data / "base" / "1" / "1234").exists()
    assert (data.parent / "data.pg12" / "global" / "pg_control").exists()
    assert "upgraded to PostgreSQL 13" in out


def test_the_preflight_before_a_pg_upgrade_checks_its_disk_in_the_planned_mode():
    seen = []
    plan = _plan(link=True)
    plan["steps"].insert(0, {"hop": 1, "action": "preflight", "image": NEW, "target_pg": "13.22",
                             "target_ts": "2.11.2",
                             "pg_upgrade": {"from_pg": 12, "to_pg": 13, "link": True}})
    clock = Clock()
    apply_plan(plan, ApplyOptions(poll_interval=1, rollout_timeout=5, acknowledge_verification=True),
               kube=MajorKube(), preflight=lambda o: seen.append(o) or _all_pass(o),
               sleep=clock.sleep, clock=clock)
    assert [o.pg_upgrade_link for o in seen] == [True]


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_the_script_lists_the_old_directories_left_on_the_disk(tmp_path):
    rc, out, data = _script(tmp_path, "ok", link="")
    assert "to remove once the result is accepted: {0}.pg12".format(data.as_posix()) in out


def test_the_upgrade_pod_is_scheduled_like_the_database_pods():
    """R6: on tainted database nodes, behind a node selector or a private
    registry, a pod without these would sit Pending."""
    kube = MajorKube()
    template = {
        "tolerations": [{"key": "db", "operator": "Exists", "effect": "NoSchedule"}],
        "nodeSelector": {"pool": "db"},
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {}}},
        "imagePullSecrets": [{"name": "registry"}],
        "serviceAccountName": "tsdb-patroni",
    }
    resources = {"requests": {"memory": "2Gi"}, "limits": {"memory": "4Gi"}}
    original = kube.json

    def json_(args, namespaced=True):
        doc = original(args, namespaced)
        if args[:2] == ["get", "statefulset"]:
            doc["spec"]["template"]["spec"].update(template)
            doc["spec"]["template"]["spec"]["containers"][0]["resources"] = resources
        return doc
    kube.json = json_
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    spec = kube.spec_seen["spec"]
    for key, value in template.items():
        assert spec[key] == value, key
    assert spec["containers"][0]["resources"] == resources
    assert spec["securityContext"]["runAsUser"] == 1000


def test_a_finished_pg_upgrade_reports_what_it_means_for_backups():
    report = _apply(_plan(), MajorKube())
    assert any(l.startswith("backups: ") and "stanza-upgrade" in l
               for l in report["steps"][0]["output"])


# --- initdb takes the old cluster's encoding and locale (R8) -----------------

def test_initdb_gets_template0s_encoding_and_locale_read_before_anything_stops():
    kube = MajorKube()
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    assert "datname = 'template0'" in kube.locale_sql
    assert "datlocprovider" not in kube.locale_sql  # PostgreSQL 12 has no provider column
    assert any("lc_collate=en_US.UTF-8" in l for l in report["steps"][0]["output"])


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("provider,expect", [
    ("libc", "-E UTF8 --lc-collate=C.UTF-8 --lc-ctype=C.UTF-8 -D"),
    ("icu", "--locale-provider=icu --icu-locale=und-x-icu -D"),
])
def test_the_script_passes_the_old_locale_to_initdb(tmp_path, provider, expect):
    rc, out, data = _script(tmp_path, "ok", link="", locale_provider=provider)
    assert rc == 0, out
    assert expect in (tmp_path / "new" / "initdb.args").read_text()


# --- Patroni's DCS state, by name (R7) ---------------------------------------

def test_patronis_state_is_deleted_by_name_for_its_scope_failover_included():
    """A switchover leaves <scope>-failover behind (measured); it goes too."""
    kube = MajorKube()
    kube.dcs_extra = [("Endpoints", "tsdb-failover")]
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")
    assert "delete dcs endpoints/tsdb endpoints/tsdb-config endpoints/tsdb-failover" in kube.writes
    assert any("scope tsdb" in l for l in report["steps"][0]["output"])


def test_a_selector_that_also_matches_other_objects_is_refused_before_anything_stops():
    kube = MajorKube()
    kube.dcs_extra = [("ConfigMap", "tsdb-chart-settings")]
    report = _apply(_plan(), kube)
    assert report["result"] == "fail"
    assert kube.writes == []
    assert "configmap/tsdb-chart-settings" in report["failure"]["summary"]
    assert "will not guess" in report["failure"]["summary"]


def test_the_scope_falls_back_to_patronis_rest_api():
    kube = MajorKube()
    exec_ = kube.exec

    def older_patronictl(pod, container, argv):
        if argv[:2] == ["patronictl", "list"]:
            return 2, "", "no such option: -f"
        if argv[0] == "curl":
            return 0, json.dumps({"patroni": {"version": "2.0.2", "scope": "tsdb"}}), ""
        return exec_(pod, container, argv)
    kube.exec = older_patronictl
    report = _apply(_plan(), kube)
    assert report["result"] == "pass", report.get("failure")


def test_a_finished_major_upgrade_reports_what_a_rollback_needs():
    """R14: structured facts in the report, not text to parse back."""
    report = _apply(_plan(), MajorKube())
    detail = report["steps"][0]["detail"]
    assert detail == {
        "from_pg": 12, "to_pg": 13, "link": False, "old_image": OLD, "run_image": NEW,
        "data": "/home/postgres/pgdata/data", "kept": "/home/postgres/pgdata/data.pg12",
        "replicas": 2, "claim": "pgdata", "scope": "tsdb",
        "system_identifier_before": "7000000000000000001",
    }
    assert "detail" not in report["steps"][1]
