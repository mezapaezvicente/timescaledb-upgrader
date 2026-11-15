"""Tests for ``rollback``: going back from a copy-mode major upgrade.

What would hurt if wrong: going back after ``--link`` (the old directory is
then unusable), going back from an upgrade that never finished, starting the
old cluster without its writes being declared lost, and doing anything at all
before the refusals have been checked. The fake cluster starts where a
finished 12 -> 13 upgrade left it.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tsupgrade.apply import ApplyOptions
from tsupgrade.rollback import _SWAP_SCRIPT, LOST_WRITES, RollbackRefused, rollback
from test_apply_major import BASH, NEW, OLD, Clock, MajorKube, _apply, _plan


class UpgradedKube(MajorKube):
    """A cluster on PostgreSQL 13 after apply; going back puts 12 on again."""

    def __init__(self, swap_phase="Succeeded", **kw):
        super().__init__(**kw)
        self.template = NEW
        for pod in self.pods:
            pod["image"] = NEW
        self.server, self.major, self.sysid = "13.22", 13, "7000000000000000002"
        self.swap_phase = swap_phase
        self.swap_pod = None

    def run(self, args, namespaced=True):
        if args[:2] == ["get", "pod"] and args[2].startswith("tsdb-rollback-"):
            if args[2] != self.swap_pod:
                return 0, "", ""
            return 0, json.dumps({"status": {"phase": self.swap_phase}}), ""
        if args[0] == "run" and args[1].startswith("tsdb-rollback-"):
            self.swap_pod = args[1]
            self.spec_seen = json.loads(args[args.index("--overrides") + 1])
            self.writes.append("run {0} on {1}".format(args[1], args[args.index("--image") + 1]))
            return 0, "", ""
        if args[0] == "logs" and args[1].startswith("tsdb-rollback-"):
            return 0, ("PostgreSQL 13's data directory is now /home/postgres/pgdata/data.rolled-back-pg13-7000000000000000002"
                       if self.swap_phase == "Succeeded" else "data.pg12 is not there"), ""
        if args[:2] == ["delete", "pod"] and args[2].startswith("tsdb-rollback-"):
            self.writes.append("delete pod " + args[2])
            return 0, "", ""
        if args[0] == "scale" and int(args[-1].split("=")[1]) > 0:
            n = int(args[-1].split("=")[1])
            self.writes.append("scale {0}".format(n))
            self.pods = [{"name": "tsdb-{0}".format(i), "role": "primary" if i == 0 else "replica",
                          "image": self.template, "uid": "back-{0}".format(i)} for i in range(n)]
            self.server, self.major, self.sysid = "12.22", 12, "7000000000000000001"
            return 0, "", ""
        return super().run(args, namespaced)


def _report(**detail):
    base = {"from_pg": 12, "to_pg": 13, "link": False, "old_image": OLD, "run_image": NEW,
            "data": "/home/postgres/pgdata/data", "kept": "/home/postgres/pgdata/data.pg12",
            "replicas": 2, "claim": "pgdata", "scope": "tsdb",
            "system_identifier_before": "7000000000000000001"}
    base.update(detail)
    return {"steps": [
        {"hop": 1, "action": "major_upgrade", "status": "done", "summary": "", "output": [],
         "detail": base},
        {"hop": 2, "action": "update_extension", "status": "done", "summary": "", "output": []}]}


def _rollback(report, kube, data_dir=None, **kw):
    clock = Clock()
    return rollback(report, ApplyOptions(poll_interval=1, rollout_timeout=5, data_dir=data_dir,
                                         selector="application=patroni,cluster-name=tsdb"),
                    kube=kube, sleep=clock.sleep, clock=clock, **kw)


def test_rollback_writes_in_order_and_ends_on_the_old_cluster():
    kube = UpgradedKube()
    result = _rollback(_report(), kube, discard_writes=True)
    assert result["result"] == "pass", result.get("failure")
    assert kube.writes == [
        "scale 0",
        "run tsdb-rollback-13-12 on " + OLD,
        "delete pod tsdb-rollback-13-12",
        "delete pvc pgdata-tsdb-1",
        "delete dcs endpoints/tsdb endpoints/tsdb-config",
        "set image " + OLD,
        "scale 2",
    ]
    env = {e["name"]: e["value"] for e in kube.spec_seen["spec"]["containers"][0]["env"]}
    assert env == {"FROM": "12", "TO": "13", "DATA": "/home/postgres/pgdata/data",
                   "KEPT": "/home/postgres/pgdata/data.pg12", "SYSID": "7000000000000000002"}
    assert kube.spec_seen["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "pgdata-tsdb-0"
    assert result["lost"] == LOST_WRITES
    assert any("system identifier 7000000000000000001 (the old cluster's)" in l for l in result["output"])


def test_rollback_needs_the_lost_writes_said_out_loud():
    kube = UpgradedKube()
    with pytest.raises(RollbackRefused, match="--discard-writes-since-upgrade"):
        _rollback(_report(), kube)
    assert kube.writes == []


@pytest.mark.parametrize("report,reason", [
    (_report(link=True), "used --link"),
    ({"steps": [{"action": "major_upgrade", "status": "failed"}]}, "did not finish"),
    ({"steps": [{"action": "update_extension", "status": "done"}]}, "no major upgrade"),
    ({"steps": [{"action": "major_upgrade", "status": "done"}]}, "predates rollback"),
])
def test_rollback_refuses_what_it_cannot_undo(report, reason):
    kube = UpgradedKube()
    with pytest.raises(RollbackRefused, match=reason):
        _rollback(report, kube, discard_writes=True)
    assert kube.writes == []


def test_rollback_refuses_a_cluster_that_is_not_on_the_new_major():
    kube = UpgradedKube()
    kube.major = 12
    with pytest.raises(RollbackRefused, match="not where the report's last upgrade left it"):
        _rollback(_report(), kube, discard_writes=True)
    assert kube.writes == []


def test_a_failed_swap_leaves_the_new_cluster_stopped_and_says_how_to_restart_it():
    kube = UpgradedKube(swap_phase="Failed")
    result = _rollback(_report(), kube, discard_writes=True)
    assert result["result"] == "fail"
    assert not any(w.startswith(("delete pvc", "delete dcs", "set image")) for w in kube.writes)
    error = "\n".join(result["failure"]["error"])
    assert "data.pg12 is not there" in error
    assert "PostgreSQL 13's data, untouched" in error and "--replicas=2" in error


def test_apply_then_rollback_round_trips_the_report():
    """The report apply writes is what rollback reads."""
    report = _apply(_plan(), MajorKube())
    kube = UpgradedKube()
    assert _rollback(json.loads(json.dumps(report)), kube, discard_writes=True)["result"] == "pass"


def _no_data(report):
    for key in ("data", "kept"):
        report["steps"][0]["detail"].pop(key)
    return report


def test_rollback_takes_the_data_directory_from_the_report():
    kube = UpgradedKube()
    kube.mounts = [{"name": "storage-volume", "mountPath": "/var/lib/postgresql"}]
    report = _report(data="/var/lib/postgresql/data", kept="/var/lib/postgresql/data.pg12",
                     claim="storage-volume")
    result = _rollback(report, kube, discard_writes=True)
    assert result["result"] == "pass", result.get("failure")
    env = {e["name"]: e["value"] for e in kube.spec_seen["spec"]["containers"][0]["env"]}
    assert env["DATA"] == "/var/lib/postgresql/data" and env["KEPT"] == "/var/lib/postgresql/data.pg12"
    assert kube.spec_seen["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == (
        "storage-volume-tsdb-0")
    assert "delete pvc storage-volume-tsdb-1" in kube.writes


def test_rollback_mounts_every_claim_and_deletes_every_replica_claim():
    kube = UpgradedKube()
    kube.mounts = [{"name": "storage-volume", "mountPath": "/var/lib/postgresql"},
                   {"name": "wal-volume", "mountPath": "/var/lib/postgresql/wal"}]
    report = _report(data="/var/lib/postgresql/data", kept="/var/lib/postgresql/data.pg12")
    result = _rollback(report, kube, discard_writes=True)
    assert result["result"] == "pass", result.get("failure")
    assert [v["persistentVolumeClaim"]["claimName"] for v in kube.spec_seen["spec"]["volumes"]] == [
        "storage-volume-tsdb-0", "wal-volume-tsdb-0"]
    assert [w for w in kube.writes if w.startswith("delete pvc")] == [
        "delete pvc storage-volume-tsdb-1", "delete pvc wal-volume-tsdb-1"]


def test_rollback_refuses_a_report_without_a_data_directory():
    kube = UpgradedKube()
    with pytest.raises(RollbackRefused, match="give it with --data-dir"):
        _rollback(_no_data(_report()), kube, discard_writes=True)
    assert kube.writes == []


def test_data_dir_stands_in_for_a_report_without_one_and_overrides_one_that_has_it():
    for report in (_no_data(_report()), _report(data="/home/postgres/pgdata/wrong")):
        kube = UpgradedKube()
        result = _rollback(report, kube, data_dir="/home/postgres/pgdata/pg", discard_writes=True)
        assert result["result"] == "pass", result.get("failure")
        env = {e["name"]: e["value"] for e in kube.spec_seen["spec"]["containers"][0]["env"]}
        assert env["DATA"] == "/home/postgres/pgdata/pg" and env["KEPT"] == "/home/postgres/pgdata/pg.pg12"


def test_rollback_refuses_a_data_directory_outside_every_claim():
    kube = UpgradedKube()
    with pytest.raises(RollbackRefused, match="not below the mount"):
        _rollback(_report(data="/var/lib/postgresql/data"), kube, discard_writes=True)
    assert kube.writes == []


# --- the swap script, on a real disk ------------------------------------------

def _upgraded(tmp_path):
    """The disk as a copy-mode pg_upgrade 12 -> 13 leaves it."""
    data = tmp_path / "pgdata" / "data"
    keep = tmp_path / "pgdata" / "data.pg12"
    (data / "global").mkdir(parents=True)
    (keep / "global").mkdir(parents=True)
    (data / "PG_VERSION").write_text("13")
    (keep / "PG_VERSION").write_text("12")
    return data, keep


def _swap(tmp_path, linked=False, aside=False, sysid="7000000000000000002", fresh=True):
    if fresh:
        data, keep = _upgraded(tmp_path)
    else:
        data, keep = tmp_path / "pgdata" / "data", tmp_path / "pgdata" / "data.pg12"
    if linked:
        (keep / "global" / "pg_control.old").write_text("x")
    if aside:
        (tmp_path / "pgdata" / ("data.rolled-back-pg13-" + sysid)).mkdir()
    env = {"PATH": "/usr/bin:/bin", "FROM": "12", "TO": "13", "DATA": data.as_posix(),
           "KEPT": keep.as_posix(), "SYSID": sysid}
    done = subprocess.run([BASH, "-c", _SWAP_SCRIPT], env=env, capture_output=True, text=True)
    return done.returncode, done.stdout, data


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_the_swap_puts_the_old_directory_back(tmp_path):
    rc, out, data = _swap(tmp_path)
    assert rc == 0, out
    assert (data / "PG_VERSION").read_text() == "12"
    assert (data.parent / "data.rolled-back-pg13-7000000000000000002" / "global").is_dir()


@pytest.mark.skipif(BASH is None, reason="no bash")
def test_a_second_rollback_on_the_same_disk_finds_its_name_free(tmp_path):
    """Upgrade, go back, upgrade again, go back again: each upgraded cluster
    has its own system identifier, so the second set-aside does not collide
    with the first (measured: with one fixed name the second swap refused)."""
    rc, out, data = _swap(tmp_path, sysid="7000000000000000002")
    assert rc == 0, out
    # pg_upgrade again: the old directory moves to data.pg12, a new 13 takes its place
    keep = data.parent / "data.pg12"
    data.rename(keep)
    (data / "global").mkdir(parents=True)
    (data / "PG_VERSION").write_text("13")
    rc, out, data = _swap(tmp_path, sysid="7000000000000000003", fresh=False)
    assert rc == 0, out
    assert (data / "PG_VERSION").read_text() == "12"
    for sysid in ("7000000000000000002", "7000000000000000003"):
        assert (data.parent / ("data.rolled-back-pg13-" + sysid) / "PG_VERSION").read_text() == "13"



@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("kw,said", [({"linked": True}, "--link"), ({"aside": True}, "already exists")])
def test_the_swap_refuses_a_linked_directory_or_one_in_the_way(tmp_path, kw, said):
    rc, out, data = _swap(tmp_path, **kw)
    assert rc == 2 and said in out
    assert (data / "PG_VERSION").read_text() == "13"
