"""Go back from the last PostgreSQL major upgrade an ``apply`` run made.

A copy-mode ``pg_upgrade`` keeps the old data directory beside the new one
(``<data>.pg<old major>``), but by then the replicas' disks and Patroni's
stored state are gone, so going back by hand is several steps. This does
them, for the *last* major upgrade in an ``apply`` report, in this order:

1. the StatefulSet is scaled to 0;
2. a pod on ordinal 0's disk moves the new directory aside
   (``<data>.rolled-back-pg<new major>-<its system identifier>``) and the
   old one back;
3. the other ordinals' disks and Patroni's objects for the scope are
   deleted: they belong to the upgraded cluster, whose system identifier is
   not the old one's;
4. the StatefulSet is set back to the image it ran before and scaled up;
   Patroni starts ordinal 0 on the old directory and re-clones the rest.

Every write made after the upgrade is lost: the old directory is the cluster
as it was when ``pg_upgrade`` ran. That includes the extension updates the
plan made on the new major. So the caller has to say so
(``discard_writes``), and the report repeats it.

It refuses, having written nothing, when:

* the report has no major upgrade, or its last one did not finish (a failed
  one carries its own note on how to go back);
* that upgrade used ``--link``: the old directory shares its files with the
  new cluster, which has started since, and ``pg_upgrade``'s documentation
  says such an old cluster has to be restored from backup;
* the cluster does not run the new major, or no primary answers;
* the pod selector matches endpoints or configmaps that are not Patroni's.

The pod checks again before moving anything: the old directory has to be
there, not linked (no ``global/pg_control.old``), and nothing may already
sit where the new one would go.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from .apply import (
    ApplyOptions,
    _Cluster,
    _data_mount,
    _dcs_objects,
    _kubectl,
    _patroni_scope,
    _pod_phase,
    _replica_pvcs,
    _run_on_ordinal_0,
    _StepFailed,
    load_script,
)
from .preflight import Kube, PreflightError

__all__ = ["ROLLBACK_FORMAT", "RollbackRefused", "last_major_upgrade", "rollback"]

ROLLBACK_FORMAT = 1

LOST_WRITES = ("every write made since the upgrade is lost: the old data directory is the "
               "cluster as it was when pg_upgrade ran, extension updates included")

_SWAP_SCRIPT = load_script("rollback_swap.sh")


class RollbackRefused(Exception):
    """Nothing was written."""

    def __init__(self, reasons: List[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


def last_major_upgrade(report: Any) -> Dict[str, Any]:
    """The detail of the report's last major upgrade, or :class:`RollbackRefused`."""
    steps = (report or {}).get("steps") if isinstance(report, dict) else None
    majors = [s for s in steps or [] if s.get("action") == "major_upgrade"]
    if not majors:
        raise RollbackRefused(["the report has no major upgrade to go back from"])
    last = majors[-1]
    if last.get("status") != "done":
        raise RollbackRefused([
            "the last major upgrade in the report did not finish ({0}); its failure says what "
            "it left and how to start the old cluster again".format(last.get("status"))])
    detail = last.get("detail")
    if not isinstance(detail, dict) or not detail.get("old_image"):
        raise RollbackRefused(["the report does not say which image ran before; it predates "
                               "rollback (apply it again with this version to get one)"])
    if detail.get("link"):
        raise RollbackRefused([
            "PostgreSQL {0} -> {1} used --link: the old directory shares its files with the "
            "new cluster, which has started since, so it is not usable; pg_upgrade's "
            "documentation says to restore the old cluster from backup".format(
                detail.get("from_pg"), detail.get("to_pg"))])
    return detail


def rollback(
    report: Dict[str, Any],
    opts: ApplyOptions,
    discard_writes: bool = False,
    kube: Optional[Kube] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Undo the report's last major upgrade; returns a report of its own."""
    if not discard_writes:
        raise RollbackRefused([LOST_WRITES + "; say so with --discard-writes-since-upgrade"])
    detail = last_major_upgrade(report)
    old, new = int(detail["from_pg"]), int(detail["to_pg"])
    # The report's data directory, unless --data-dir says otherwise; never assumed.
    data = opts.data_dir or detail.get("data")
    if not data:
        raise RollbackRefused(["the report does not say where the data directory is; give it "
                               "with --data-dir"])
    kept = (detail.get("kept") if not opts.data_dir and detail.get("kept")
            else "{0}.pg{1}".format(data, old))
    opts = replace(opts, data_dir=data)
    kube = kube or Kube(opts.context, opts.namespace, opts.timeout)
    cluster = _Cluster(kube, opts, sleep, clock)

    primary = cluster.primary()
    if primary is None:
        raise RollbackRefused(["no ready primary"])
    rc, out, _ = cluster.psql(primary, opts.admin_db,
                              "SELECT current_setting('server_version_num')::int / 10000, "
                              "system_identifier FROM pg_control_system()")
    running, _, sysid = out.strip().partition("|")
    if rc != 0 or running != str(new):
        raise RollbackRefused(["the cluster runs PostgreSQL {0}, not {1}: it is not where the "
                               "report's last upgrade left it".format(running or "?", new)])
    try:
        scope = _patroni_scope(kube, primary, opts.container)
    except _StepFailed as exc:
        raise RollbackRefused([exc.summary]) from exc
    _, others = _dcs_objects(kube, opts, scope)
    if others:
        raise RollbackRefused(["the pod selector {0} also matches {1}, which are not Patroni's "
                               "objects for scope {2}".format(opts.selector, ", ".join(others), scope)])
    sts = kube.json(["get", "statefulset", opts.statefulset])
    if _data_mount(sts, opts, data) is None:
        raise RollbackRefused(["the data directory {0} is not below the mount of any volume claim "
                               "template of statefulset/{1}".format(data, opts.statefulset)])

    output: List[str] = ["going back from PostgreSQL {0} (system identifier {1}) to {2}: {3}".format(
        new, sysid, old, LOST_WRITES)]
    result: Dict[str, Any] = {"rollback_format": ROLLBACK_FORMAT, "from_pg": new, "to_pg": old,
                              "lost": LOST_WRITES, "output": output}
    try:
        _go_back(cluster, kube, opts, sts, dict(detail, data=data, kept=kept), scope, sysid,
                 output)
        result["result"] = "pass"
    except (_StepFailed, PreflightError) as exc:
        result["result"] = "fail"
        result["failure"] = {
            "summary": exc.summary if isinstance(exc, _StepFailed) else str(exc),
            "error": exc.output if isinstance(exc, _StepFailed) else []}
    return result


def _go_back(cluster: _Cluster, kube: Kube, opts: ApplyOptions, sts: Dict[str, Any],
             detail: Dict[str, Any], scope: str, sysid: str, output: List[str]) -> None:
    old, new, replicas = int(detail["from_pg"]), int(detail["to_pg"]), int(detail["replicas"])
    pod = "{0}-rollback-{1}-{2}".format(opts.statefulset, new, old)
    if _pod_phase(kube, pod) is not None:
        raise _StepFailed("pod {0} is left from an earlier run; read its log, delete it ({1}) "
                          "and try again".format(pod, _kubectl(opts, "delete", "pod", pod)))

    rc, out, err = kube.run(["scale", "statefulset/" + opts.statefulset, "--replicas=0"])
    if rc != 0:
        raise _StepFailed("kubectl scale to 0 failed", [err or out])
    cluster.wait_for("every instance to stop", lambda: not cluster.pods())
    output.append("scaled statefulset/{0} to 0".format(opts.statefulset))
    stopped = ["the cluster is stopped on PostgreSQL {0}'s data, untouched; to start it again: "
               "{1}".format(new, _kubectl(opts, "scale", "statefulset/" + opts.statefulset,
                                          "--replicas={0}".format(replicas)))]

    rc, out, err = _run_on_ordinal_0(kube, sts, opts, pod, detail["old_image"], _SWAP_SCRIPT, {
        "FROM": str(old), "TO": str(new), "DATA": detail["data"], "KEPT": detail["kept"],
        "SYSID": sysid})
    if rc != 0:
        raise _StepFailed("cannot start pod {0}: {1}".format(pod, err or out), stopped)
    cluster.wait_for("pod {0} to finish".format(pod), lambda: (_pod_phase(kube, pod) or {}).get(
        "phase") in ("Succeeded", "Failed"))
    phase = (_pod_phase(kube, pod) or {}).get("phase")
    _, log, _ = kube.run(["logs", pod])
    lines = [l for l in log.splitlines() if l.strip()]
    if phase != "Succeeded":
        raise _StepFailed("swapping the data directories failed", lines[-20:] + stopped)
    output.extend(lines[-3:])
    kube.run(["delete", "pod", pod, "--wait=false"])

    for name in _replica_pvcs(sts, opts, replicas):
        rc, out, err = kube.run(["delete", "pvc", name, "--ignore-not-found", "--wait=true"])
        if rc != 0:
            raise _StepFailed("cannot delete {0}".format(name), [err or out])
        output.append("deleted {0}".format(name))
    dcs, _ = _dcs_objects(kube, opts, scope)
    if dcs:
        rc, out, err = kube.run(["delete"] + dcs + ["--ignore-not-found"])
        if rc != 0:
            raise _StepFailed("cannot reset Patroni's state", [err or out])
    output.append("reset Patroni's state for scope {0}: {1}".format(scope, ", ".join(dcs) or "nothing found"))

    image = detail["old_image"]
    for args in (["set", "image", "statefulset/" + opts.statefulset,
                  "{0}={1}".format(opts.container, image)],
                 ["scale", "statefulset/" + opts.statefulset, "--replicas={0}".format(replicas)]):
        rc, out, err = kube.run(args)
        if rc != 0:
            raise _StepFailed("kubectl {0} failed".format(args[0]), [err or out])

    def back() -> bool:
        pods = cluster.pods()
        return (len(pods) == replicas
                and all(cluster.ready(p) and cluster.pod_image(p) == image for p in pods)
                and cluster.primary() is not None)

    cluster.wait_for("{0} instance(s) ready on {1}".format(replicas, image), back)
    primary = cluster.primary()
    rc, out, _ = cluster.psql(primary, opts.admin_db,
                              "SELECT current_setting('server_version_num')::int / 10000, "
                              "system_identifier FROM pg_control_system()")
    major, _, sysid = out.strip().partition("|")
    if rc != 0 or major != str(old):
        raise _StepFailed("the primary runs PostgreSQL {0}, not {1}".format(major or "?", old))
    before = detail.get("system_identifier_before")
    if before and sysid != before:
        raise _StepFailed("the primary's system identifier is {0}, not the {1} the old cluster "
                          "had".format(sysid, before))
    output.append("back: primary {0} on PostgreSQL {1}, system identifier {2}{3}".format(
        primary, major, sysid, " (the old cluster's)" if before else ""))
