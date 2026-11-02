"""Tests for ``apply``, the only command that writes.

No cluster. A fake kubectl holds the state -- pods, roles, images, extension
versions per database -- and records every write, so each test can assert not
only what happened but what did *not*: a refused plan must leave the write log
empty.

What these defend:

* a verification-mode plan needs its own acknowledgement;
* a plan only runs against the state it was made from;
* a plan file cannot smuggle in SQL other than the one update statement;
* a rollout goes replicas first, primary last;
* the first failing step stops the run, and its error is kept verbatim.
"""

from __future__ import annotations

import json
from typing import Dict, List

import pytest

from tsupgrade import apply as apply_mod
from tsupgrade.apply import ApplyOptions, ApplyRefused, apply_plan, check_plan, render_report
from tsupgrade.drift import ImageProfile
from tsupgrade.plan import build_plan
from tsupgrade.preflight import FAIL, PASS, UNKNOWN, CheckResult

REPO = "timescale/timescaledb-ha:"
PG15_13 = REPO + "pg15.13-ts2.20.3"
PG15_18 = REPO + "pg15.18-ts2.28.3"


class FakeKube:
    """Just enough kubectl for apply, with the cluster's state behind it."""

    def __init__(self, image: str, server: str, dbs: Dict[str, str], replicas: int = 0):
        self.old_caggs = ""
        self.template = image
        self.server = server
        self.ext = dict(dbs)
        self.pods = []
        for i in range(replicas + 1):
            self.pods.append({
                "name": "tsdb-{0}".format(i),
                "role": "primary" if i == 0 else "replica",
                "image": image,
                "uid": "uid-{0}-0".format(i),
            })
        self.writes: List[str] = []
        self.alter_error = None
        self.strategy = "OnDelete"

    def _pod_json(self, pod):
        return {
            "metadata": {"name": pod["name"], "uid": pod["uid"], "labels": {"role": pod["role"]}},
            "spec": {"containers": [{"name": "timescaledb", "image": pod["image"]}]},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": "timescaledb",
                                       "imageID": "docker.io/" + pod["image"] + "@sha256:feed"}],
            },
        }

    def _pod(self, name):
        return next(p for p in self.pods if p["name"] == name)

    # -- the Kube interface -------------------------------------------------

    def json(self, args, namespaced=True):
        if args[:2] == ["get", "pods"]:
            return {"items": [self._pod_json(p) for p in self.pods]}
        if args[:2] == ["get", "statefulset"]:
            return {"spec": {"updateStrategy": {"type": self.strategy},
                             "template": {"spec": {"containers": [
                                 {"name": "timescaledb", "image": self.template}]}}}}
        raise AssertionError("unexpected kubectl json {0}".format(args))

    def run(self, args, namespaced=True):
        if args[0] == "set":
            self.template = args[-1].split("=", 1)[1]
            self.writes.append("set image " + self.template)
            return 0, "", ""
        if args[0] == "delete":
            pod = self._pod(args[2])
            pod["image"] = self.template
            pod["uid"] = pod["uid"] + "+"
            self.writes.append("delete " + pod["name"])
            return 0, "", ""
        if args[:2] == ["get", "pod"]:
            return 0, json.dumps(self._pod_json(self._pod(args[2]))), ""
        raise AssertionError("unexpected kubectl {0}".format(args))

    def exec(self, pod, container, argv):
        if argv[0] == "env":  # preflight's read-only psql
            argv = argv[2:]
        db, sql = argv[argv.index("-d") + 1], argv[-1]
        if sql.startswith("SELECT current_setting('server_version')"):
            dbs = ",".join(["postgres"] + sorted(self.ext))
            return 0, "{0}|f|/data|1000|{1}|2.28.3".format(self.server, dbs), ""
        if sql.startswith("SELECT coalesce((SELECT extversion"):
            return 0, self.ext.get(db, ""), ""
        if sql == "SELECT 1":
            return 0, "1", ""
        if "information_schema.columns" in sql:  # the check for old aggregates
            return 0, "t", ""
        if "time_bucket_ng" in sql:
            return 0, "", ""
        if "FROM _timescaledb_catalog.continuous_agg" in sql:
            return 0, self.old_caggs, ""
        if sql == "SHOW server_version":
            return 0, self.server + " (Ubuntu)", ""
        if sql.startswith("ALTER EXTENSION"):
            if self.alter_error:
                return 1, "", self.alter_error
            self.ext[db] = sql.split("'")[1]
            self.writes.append("{0}@{1}: {2}".format(db, pod, sql))
            return 0, "", ""
        if "pg_terminate_backend" in sql:
            self.writes.append("terminate on " + pod)
            return 0, "2", ""
        raise AssertionError("unexpected SQL {0!r}".format(sql))

    def psql(self, pod, container, database, user, sql):
        return self.exec(pod, container, ["psql", "-d", database, "-c", sql])


def _profile(ref, floor, ceiling):
    return ImageProfile(ref, (15,), floor, ceiling, ceiling)


def _plan(source_ts="2.20.3", verification=True, candidates=()):
    target = _profile(PG15_18, "2.13.0", "2.28.3")
    return build_plan("15.18", source_ts, target, list(candidates), None,
                      verification_run=verification)


def _all_pass(_opts):
    return [CheckResult("drift", "Drift", PASS, "ok"), CheckResult("matrix", "Matrix", UNKNOWN, "unverified")]


def _run(plan, kube, **opts):
    opts.setdefault("acknowledge_verification", True)
    return apply_plan(plan, ApplyOptions(poll_interval=0, rollout_timeout=5, **opts),
                      kube=kube, preflight=_all_pass, sleep=lambda s: None)


# --------------------------------------------------------------------------
# Refusals: nothing is written
# --------------------------------------------------------------------------

def test_a_verification_plan_needs_its_own_acknowledgement():
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})
    with pytest.raises(ApplyRefused, match="acknowledge-verification-run"):
        _run(_plan(), kube, acknowledge_verification=False)
    assert kube.writes == []


def test_a_cluster_at_another_version_is_refused():
    kube = FakeKube(PG15_18, "15.18", {"app": "2.22.0"})
    with pytest.raises(ApplyRefused, match="has timescaledb 2.22.0|not at the source"):
        _run(_plan(), kube)
    assert kube.writes == []


def test_another_postgres_minor_is_refused():
    kube = FakeKube(PG15_18, "15.17", {"app": "2.20.3"})
    with pytest.raises(ApplyRefused, match="PostgreSQL 15.18"):
        _run(_plan(), kube)
    assert kube.writes == []


def test_databases_that_disagree_must_be_scoped_explicitly():
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3", "other": "2.13.0"})
    with pytest.raises(ApplyRefused, match="--database"):
        _run(_plan(), kube)
    report = _run(_plan(), kube, databases=("app",))
    assert report["result"] == "pass"
    assert kube.ext == {"app": "2.28.3", "other": "2.13.0"}


def test_a_plan_cannot_carry_arbitrary_sql():
    plan = _plan()
    step = next(s for s in plan["steps"] if s["action"] == "update_extension")
    step["sql"] = "ALTER EXTENSION timescaledb UPDATE TO '2.28.3'; DROP TABLE app;"
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})
    with pytest.raises(ApplyRefused, match="one statement"):
        _run(plan, kube)
    assert kube.writes == []


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(plan_format=99),
    lambda p: p["steps"].append({"action": "drop_database"}),
    lambda p: p.update(steps="all of them"),
])
def test_malformed_plans_are_refused(mutate):
    plan = _plan()
    mutate(plan)
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})
    with pytest.raises(ApplyRefused):
        _run(plan, kube)
    assert kube.writes == []


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def test_single_image_upgrade_touches_no_pods():
    # 2.20.3 -> 2.28.3 stops at 2.27.2 (#10272): two updates, still no rollout.
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"}, replicas=1)
    report = _run(_plan(), kube)
    assert report["result"] == "pass"
    assert kube.writes == [
        "app@tsdb-0: ALTER EXTENSION timescaledb UPDATE TO '2.27.2';",
        "terminate on tsdb-0",
        "terminate on tsdb-1",
        "app@tsdb-0: ALTER EXTENSION timescaledb UPDATE TO '2.28.3';",
        "terminate on tsdb-0",
        "terminate on tsdb-1",
    ]
    statuses = [(s["hop"], s["action"], s["status"]) for s in report["steps"]]
    assert (1, "ensure_image", "skipped") in statuses
    assert (2, "ensure_image", "skipped") in statuses
    assert report["final"]["databases"] == {"app": "2.28.3"}
    assert report["final"]["instances"][0]["server_version"] == "15.18"
    assert report["final"]["instances"][0]["image_id"].endswith("@sha256:feed")


def test_two_image_upgrade_rolls_replicas_first_and_updates_after_each_rollout():
    bridge = _profile(PG15_13, "2.9.0", "2.20.3")
    plan = _plan(source_ts="2.12.2", candidates=[bridge])
    assert plan["hops"] == 3
    kube = FakeKube(PG15_13, "15.18", {"app": "2.12.2"}, replicas=1)
    report = _run(plan, kube)
    assert report["result"] == "pass", report
    assert kube.writes == [
        # hop 1: already on the bridge image
        "app@tsdb-0: ALTER EXTENSION timescaledb UPDATE TO '2.20.3';",
        "terminate on tsdb-0", "terminate on tsdb-1",
        # hop 2: roll, replica first, then update to the #10272 stop
        "set image " + PG15_18,
        "delete tsdb-1", "delete tsdb-0",
        "app@tsdb-0: ALTER EXTENSION timescaledb UPDATE TO '2.27.2';",
        "terminate on tsdb-0", "terminate on tsdb-1",
        # hop 3: same image, no rollout
        "app@tsdb-0: ALTER EXTENSION timescaledb UPDATE TO '2.28.3';",
        "terminate on tsdb-0", "terminate on tsdb-1",
    ]


def test_old_patroni_master_label_is_the_primary():
    # Patroni 2.0.2 (the pg11 image) labels its leader `master` even with
    # leader_label_value: primary set; apply refused "found 0" primaries.
    bridge = _profile(PG15_13, "2.9.0", "2.20.3")
    plan = _plan(source_ts="2.12.2", candidates=[bridge])
    kube = FakeKube(PG15_13, "15.18", {"app": "2.12.2"}, replicas=1)
    kube.pods[0]["role"] = "master"
    report = _run(plan, kube)
    assert report["result"] == "pass", report
    # The primary is still rolled last and is where the update runs.
    assert kube.writes[3:6] == ["set image " + PG15_18, "delete tsdb-1", "delete tsdb-0"]
    assert kube.writes[6] == "app@tsdb-0: ALTER EXTENSION timescaledb UPDATE TO '2.27.2';"
    assert report["final"]["instances"][0]["role"] == "primary"


def test_a_pinned_step_rolls_out_the_digest_reference():
    """What runs is what the plan names, even if the tag was pushed again."""
    digest = "sha256:" + "a" * 64
    plan = _plan()
    for step in plan["steps"]:
        if step["action"] == "ensure_image":
            step["digest"] = digest
    kube = FakeKube(PG15_13, "15.18", {"verify": "2.20.3"})
    report = _run(plan, kube)
    assert report["result"] == "pass", report.get("failure")
    assert "set image {0}@{1}".format(PG15_18, digest) in kube.writes


def test_a_malformed_digest_is_refused_before_anything_is_written():
    plan = _plan()
    next(s for s in plan["steps"] if s["action"] == "ensure_image")["digest"] = "latest"
    assert any("not a sha256 digest" in r for r in check_plan(plan, ApplyOptions(
        acknowledge_verification=True)))


def test_a_failed_rollout_reports_what_it_had_already_done():
    """Found live: a pod that never comes back must not erase the set image."""
    bridge = _profile(PG15_13, "2.9.0", "2.20.3")
    plan = _plan(source_ts="2.12.2", candidates=[bridge])
    kube = FakeKube(PG15_13, "15.18", {"app": "2.12.2"}, replicas=1)
    original = kube.run

    def stuck(args, namespaced=True):
        if args[:2] == ["get", "pod"]:
            return 1, "", "pods \"{0}\" not found".format(args[2])
        return original(args, namespaced)

    kube.run = stuck
    clock = iter(range(0, 10000, 10))
    report = apply_plan(plan, ApplyOptions(acknowledge_verification=True, poll_interval=0,
                                           rollout_timeout=30),
                        kube=kube, preflight=_all_pass, sleep=lambda s: None,
                        clock=lambda: next(clock))
    assert report["failure"]["action"] == "ensure_image"
    assert "timed out" in report["failure"]["summary"]
    assert report["failure"]["error"][:2] == [
        "statefulset/tsdb: {0} -> {1}".format(PG15_13, PG15_18), "tsdb-1 deleted"]


def test_pod_deletion_does_not_block_on_graceful_shutdown():
    """Found live: --wait=true outlasted kubectl's per-call timeout."""
    bridge = _profile(PG15_13, "2.9.0", "2.20.3")
    kube = FakeKube(PG15_13, "15.18", {"app": "2.12.2"})
    calls = []
    original = kube.run
    kube.run = lambda args, namespaced=True: (calls.append(args), original(args, namespaced))[1]
    _run(_plan(source_ts="2.12.2", candidates=[bridge]), kube)
    deletes = [c for c in calls if c[0] == "delete"]
    assert deletes and all("--wait=false" in c for c in deletes)


def test_old_aggregates_stop_the_run_before_any_write():
    """The stop comes from reading alone: no preflight step, no pod, no ALTER."""
    kube = FakeKube(PG15_18, "15.18", {"verify": "2.20.3"})
    kube.old_caggs = chr(31).join(["public.ca", "public.t", "1024", "1", "0", "", "", "end"])
    with pytest.raises(ApplyRefused) as refused:
        _run(_plan(), kube)
    assert kube.writes == [] and refused.value.plain
    assert "read-only check" in " ".join(refused.value.reasons)


def test_agreeing_migrates_in_the_order_the_stop_said():
    kube = FakeKube(PG15_18, "15.18", {"verify": "2.20.3"})
    kube.old_caggs = chr(31).join(["public.ca", "public.t", "1024", "1", "0", "", "", "end"])
    calls = []
    original = kube.exec

    def exec_(pod, container, argv):
        sql = argv[-1]
        if sql.startswith("CALL cagg_migrate"):
            kube.old_caggs = ""
            calls.append(sql)
            kube.writes.append("cagg_migrate")
            return 0, "", ""
        if "aclexplode" in sql and sql.startswith("SELECT format") or "pg_get_userbyid" in sql:
            return 0, "", ""
        return original(pod, container, argv)

    kube.exec = exec_
    report = _run(_plan(), kube, migrate_caggs=True)
    assert report["result"] == "pass", report.get("failure")
    assert calls == ["CALL cagg_migrate('public.ca', override => true, drop_old => true)"]
    order = [w for w in kube.writes if w == "cagg_migrate" or w.startswith("verify@") and "ALTER" in w]
    assert order[0] == "cagg_migrate"
    assert [s["action"] for s in report["steps"]].count("migrate_caggs") == 1


def test_a_failing_preflight_stops_before_any_write():
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})

    def failing(_opts):
        return [CheckResult("gates", "Gates", FAIL, "non-finalized aggregate", ["app: fx_hourly"])]

    report = apply_plan(_plan(), ApplyOptions(acknowledge_verification=True, poll_interval=0),
                        kube=kube, preflight=failing, sleep=lambda s: None)
    assert report["result"] == "fail"
    assert report["failure"]["action"] == "preflight"
    assert "  app: fx_hourly" in report["failure"]["error"]
    assert kube.writes == []


def test_a_failing_alter_is_kept_verbatim_and_stops_the_run():
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})
    kube.alter_error = "ERROR:  function _timescaledb_internal.ping_data_node(name) does not exist"
    report = _run(_plan(), kube)
    assert report["result"] == "fail"
    assert report["failure"]["action"] == "update_extension"
    assert report["failure"]["error"] == [kube.alter_error]
    assert [s["action"] for s in report["steps"]][-1] == "update_extension"
    assert not any(w.startswith("terminate") for w in kube.writes)
    assert report["final"]["databases"] == {"app": "2.20.3"}


def test_verify_fails_when_a_database_never_reports_the_target(monkeypatch):
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})
    original = kube.exec

    def stale_read(pod, container, argv):
        if argv[-1].startswith("SELECT coalesce((SELECT extversion") and kube.writes:
            return 0, "2.20.3", ""
        return original(pod, container, argv)

    monkeypatch.setattr(kube, "exec", stale_read)
    clock = iter(range(0, 1000, 10))
    report = apply_plan(_plan(), ApplyOptions(acknowledge_verification=True, poll_interval=0,
                                              rollout_timeout=30),
                        kube=kube, preflight=_all_pass, sleep=lambda s: None,
                        clock=lambda: next(clock))
    assert report["result"] == "fail"
    assert report["failure"]["action"] == "verify_version"
    assert any("2.20.3" in line for line in report["failure"]["error"])


def test_report_renders_the_failure():
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})
    kube.alter_error = "ERROR:  boom"
    text = render_report(_run(_plan(), kube))
    assert "VERIFICATION RUN" in text
    assert "[FAIL] hop 1 update_extension" in text
    assert "ERROR:  boom" in text
    assert text.rstrip().endswith("FAIL")


def test_cli_apply_exit_codes(tmp_path, monkeypatch, capsys):
    from tsupgrade import cli

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()))
    kube = FakeKube(PG15_18, "15.18", {"app": "2.20.3"})
    monkeypatch.setattr(apply_mod, "Kube", lambda *a, **k: kube)
    monkeypatch.setattr(apply_mod, "run_preflight", _all_pass)

    assert cli.main(["apply", str(plan_path)]) == cli.EXIT_CHECK_FAILED
    assert "acknowledge" in capsys.readouterr().err
    assert kube.writes == []

    report_path = tmp_path / "report.json"
    code = cli.main(["apply", str(plan_path), "--acknowledge-verification-run",
                     "--report", str(report_path)])
    assert code == cli.EXIT_OK
    assert json.loads(report_path.read_text())["result"] == "pass"


@pytest.mark.parametrize("strategy", ["RollingUpdate", None])
def test_a_statefulset_the_controller_rolls_itself_is_refused(strategy):
    """R10: with RollingUpdate (also the default when unset), set image makes
    the controller replace pods while apply deletes them."""
    kube = FakeKube(PG15_13, "15.18", {"verify": "2.20.3"})
    kube.strategy = strategy
    with pytest.raises(ApplyRefused) as info:
        _run(_plan(), kube)
    assert kube.writes == []
    assert "updates with RollingUpdate" in str(info.value)
    assert '"type":"OnDelete"' in str(info.value)


def test_an_on_delete_statefulset_is_accepted():
    kube = FakeKube(PG15_13, "15.18", {"verify": "2.20.3"})
    assert apply_mod.check_statefulset(kube, ApplyOptions()) == []
