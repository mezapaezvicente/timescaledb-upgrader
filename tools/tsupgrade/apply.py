"""Execute a plan. The only part of this tool that writes.

``plan`` decides and ``apply`` acts. ``apply`` reads a plan document and does
exactly what its steps say, in order, and nothing else:

``preflight``         the read-only checks against the hop's image; any FAIL stops
``ensure_image``      roll the StatefulSet onto the hop's image, if it is not there
``update_extension``  ``ALTER EXTENSION timescaledb UPDATE TO '<v>'`` in each database
``cycle_sessions``    terminate client sessions so none keeps the old library loaded
``verify_version``    every database, on every instance, reports the new version
``major_upgrade``     ``pg_upgrade`` the leader's data to a new PostgreSQL major, then
                      restart every instance on the new major's image
``analyze``           ``vacuumdb --all --analyze-in-stages`` on the primary

It stops at the first step that fails and says which one, with the error
verbatim. It never retries, rolls back or improvises: a half-finished upgrade
is reported as exactly that, and the operator decides what happens next.

Before the first step it refuses outright, having written nothing, when:

* the plan is in ``verification`` mode and the caller did not acknowledge it
  separately (``acknowledge_verification``) -- such a plan was built without an
  established verdict and is for disposable databases only;
* the cluster does not match ``plan.source``: a different PostgreSQL version,
  or a database whose extension is not at the source version. A plan is only
  valid for the state it was made from;
* an ``update_extension`` step carries SQL other than the one statement its own
  ``to`` field implies. The plan is a file and files get edited; ``apply``
  executes upgrades, not whatever SQL a document contains;
* a digest in the plan is not a ``sha256:`` digest;
* the StatefulSet does not update ``OnDelete``. With ``RollingUpdate`` the
  controller replaces pods itself, the primary included, as soon as
  ``kubectl set image`` runs, racing apply's own deletions.

Images run by digest. A step's ``digest`` (and a major upgrade's
``run_digest``) turns its image into ``image@sha256:...`` for every rollout,
the ``pg_upgrade`` pod and the restart, so what runs is what the plan names
even if the tag has since been pushed again. A step without one runs by tag.

Preflight stays read-only and its module is untouched. The writes live here:
``kubectl set image``, ``kubectl delete pod`` (the StatefulSet is ``OnDelete``,
so a pod picks up a new image only when deleted), the ``ALTER EXTENSION`` and
``pg_terminate_backend``.

A major upgrade, under Patroni, in this order:

1. the leader moves to ordinal 0 (``patronictl switchover``), because the
   StatefulSet is ``OrderedReady``: ordinal 0 starts first and alone, so its
   disk is the one that must hold the data;
2. the StatefulSet is scaled to 0, which shuts every instance down cleanly;
3. a pod on the image carrying both majors mounts ordinal 0's disk and runs
   ``initdb`` (with the old cluster's checksum setting), ``pg_upgrade
   --check``, ``pg_upgrade``, and swaps the directories. The old one stays
   beside it as ``<data>.pg<old major>``; after ``--link`` it is unusable;
4. the other ordinals' disks and Patroni's objects in the DCS are deleted:
   ``pg_upgrade`` gives the cluster a new system identifier, so the stored
   state and the replicas' copies belong to a cluster that no longer exists.
   Patroni's objects are deleted by name, for the scope Patroni reports, and
   only those that carry the pod selector's labels; before anything stops,
   apply refuses if that selector matches any other endpoints or configmap;
5. the StatefulSet is set to the new major's image and scaled back up.
   Patroni starts ordinal 0 on the upgraded directory and re-clones the rest.

Every failure after step 2 carries what it left behind and how to go on:
before ``pg_upgrade`` has succeeded, the cluster is stopped with its old data
intact and the report says how to start it again; after, the report lists
the commands that finish the upgrade by hand. ``apply`` does neither on its
own: whether to go back is the operator's call.

The wait for ``pg_upgrade`` has its own limit (``pg_upgrade_timeout``, none by
default), since a copy takes as long as the database is large; a pod that has
not started at all fails after ``rollout_timeout``. When apply stops waiting,
the pod keeps running and the report says how to watch it. An upgrade pod
already there (running, or left from an earlier run) is refused, never
deleted: it may be working on the disk, or hold the only account of what
happened to it.

Pod order on an image rollout: replicas first, the primary last. Patroni
releases the leader key when its pod is stopped, so with replicas the primary's
deletion is a fast failover rather than an outage; with one instance it is a
restart. Either way the rollout waits for a primary to be back before going on.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .helm import helm_owner, values_note
from .gates import OldCagg, old_caggs, reaches_old_cagg_removal, time_bucket_ng_caggs
from .images import mirrored
from .plan import PLAN_FORMAT, VERIFICATION, pinned
from .steps import (
    EnsureImage,
    MajorUpgrade,
    MigrateCaggs,
    Preflight,
    UpdateExtension,
    VerifyVersion,
    parse_steps,
)
from .preflight import (
    BACKUPS_AT_A_MAJOR_UPGRADE,
    FAIL,
    Instance,
    Kube,
    PreflightError,
    PreflightOptions,
    RouteImage,
    discover_instances,
    patroni_role,
    run_preflight,
)
from .versions import InvalidVersion, parse_version

__all__ = [
    "APPLY_FORMAT",
    "ApplyOptions",
    "ApplyRefused",
    "apply_plan",
    "render_report",
]

APPLY_FORMAT = 1

# Where the timescale/timescaledb-ha images keep the data directory. Used only
# when no server can say where it is, and the report says so.
DEFAULT_DATA_DIR = "/home/postgres/pgdata/data"

DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"



class ApplyRefused(Exception):
    """Nothing was written: the plan, the options or the cluster disqualify it."""

    def __init__(self, reasons: Sequence[str], plain: bool = False) -> None:
        # plain: the reasons are one message, to print as it stands.
        self.plain = plain
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass
class ApplyOptions:
    context: Optional[str] = None
    namespace: str = "timescale"
    selector: str = "application=patroni"
    container: str = "timescaledb"
    statefulset: str = "tsdb"
    psql_user: str = "postgres"
    admin_db: str = "postgres"
    # Empty: every database with timescaledb installed, which is what a real
    # upgrade must cover. Naming databases scopes the source check, the update
    # and the verification to them; the image rollout still affects them all.
    databases: Tuple[str, ...] = ()
    acknowledge_verification: bool = False
    matrix_path: Optional[str] = None
    timeout: float = 30.0
    rollout_timeout: float = 600.0
    # How long a running pg_upgrade pod is waited for; 0 is no limit. A pod
    # that has not started is bounded by rollout_timeout instead.
    pg_upgrade_timeout: float = 0.0
    # The data directory, for a major upgrade. None: read from the servers
    # before anything stops (resolve_data_dir); set (--data-dir), this path.
    data_dir: Optional[str] = None
    # Where each major's binaries are, as the timescale/timescaledb-ha images
    # lay them out.
    pg_bin: str = "/usr/lib/postgresql/{0}/bin"
    registry_timeout: float = 15.0
    poll_interval: float = 2.0
    # Run the plan's images from here instead (images.mirrored); the plan and
    # the route it was verified on do not change.
    image_registry: Optional[str] = None
    # Go on when a registry does not answer, if the images can run without it.
    use_cached_images: bool = False
    # Migrate old continuous aggregates if the plan needs it; else ``confirm``
    # is asked, and with neither apply stops before changing anything.
    migrate_caggs: bool = False
    confirm: Optional[Callable[[str], bool]] = None
    # What tsupgrade.discover found when the names were not given.
    discovered: Tuple[str, ...] = ()
    # Called as soon as something is worth telling the operator, since a run
    # can take hours and the report comes at the end.
    notify: Optional[Callable[[str], None]] = None


@dataclass
class StepResult:
    hop: int
    action: str
    status: str
    summary: str
    output: List[str] = field(default_factory=list)
    detail: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        doc = {
            "hop": self.hop,
            "action": self.action,
            "status": self.status,
            "summary": self.summary,
            "output": list(self.output),
        }
        if self.detail is not None:
            doc["detail"] = dict(self.detail)
        return doc


@dataclass
class _Outcome:
    """What a step function returns: its own status, not one read from its text."""

    summary: str
    output: List[str] = field(default_factory=list)
    status: str = DONE
    # Structured facts a later command needs (rollback reads a major upgrade's).
    detail: Optional[Dict[str, Any]] = None


class _StepFailed(Exception):
    def __init__(self, summary: str, output: Sequence[str] = ()) -> None:
        self.summary = summary
        self.output = list(output)
        super().__init__(summary)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Checks made before anything is written
# ---------------------------------------------------------------------------


def check_plan(plan: Any, opts: ApplyOptions) -> List[str]:
    """Reasons the plan document itself cannot be applied."""
    if not isinstance(plan, dict):
        return ["the plan is not a JSON object"]
    reasons: List[str] = []
    if plan.get("plan_format") != PLAN_FORMAT:
        reasons.append(
            "plan_format {0!r} is not {1}; re-run `tsupgrade plan`".format(
                plan.get("plan_format"), PLAN_FORMAT
            )
        )
    if plan.get("mode") == VERIFICATION and not opts.acknowledge_verification:
        reasons.append(
            "this is a verification-run plan, built without an established "
            "verdict; applying it needs --acknowledge-verification-run, and only "
            "against a disposable database"
        )
    source = plan.get("source") or {}
    if not source.get("pg") or not source.get("ts"):
        reasons.append("plan.source needs pg and ts")
    return reasons + parse_steps(plan.get("steps"))[1]


def check_statefulset(kube: Kube, opts: ApplyOptions) -> List[str]:
    """apply rolls pods itself, so the controller must not: ``OnDelete`` only."""
    try:
        sts = kube.json(["get", "statefulset", opts.statefulset])
    except PreflightError as exc:
        return ["cannot read statefulset/{0}: {1}".format(opts.statefulset, exc)]
    # Kubernetes' default, when the field is absent, is RollingUpdate.
    kind = ((sts.get("spec") or {}).get("updateStrategy") or {}).get("type") or "RollingUpdate"
    if kind == "OnDelete":
        return []
    return ["statefulset/{0} updates with {1}: kubectl set image would make the controller "
            "replace pods itself, the primary included, while apply deletes them in its own "
            "order. Switch it first: kubectl -n {2} patch statefulset {0} --type merge -p "
            "'{{\"spec\":{{\"updateStrategy\":{{\"type\":\"OnDelete\"}}}}}}'".format(
                opts.statefulset, kind, opts.namespace)]


def _claim_mounts(sts: Dict[str, Any], opts: ApplyOptions) -> List[Dict[str, Any]]:
    """The database container's mounts of the StatefulSet's volume claim
    templates, in the templates' order."""
    template = sts.get("spec", {}).get("template", {}).get("spec", {})
    container = next((c for c in template.get("containers") or []
                      if c.get("name") == opts.container), {})
    mounts = {m.get("name"): m for m in container.get("volumeMounts") or []}
    claims = [c.get("metadata", {}).get("name")
              for c in sts.get("spec", {}).get("volumeClaimTemplates") or []]
    return [mounts[c] for c in claims if c in mounts]


def _under(path: str, mount: str) -> bool:
    """Strictly below: pg_upgrade writes the data directory's siblings, which
    must be on the same volume."""
    path, mount = posixpath.normpath(path), posixpath.normpath(mount)
    return path.startswith(mount.rstrip("/") + "/")


def _data_mount(sts: Dict[str, Any], opts: ApplyOptions, data: str) -> Optional[Dict[str, Any]]:
    """The claim template's mount that holds ``data``: the deepest one."""
    found = [m for m in _claim_mounts(sts, opts) if _under(data, m.get("mountPath") or "")]
    return max(found, key=lambda m: len(m["mountPath"])) if found else None


def _claims(sts: Dict[str, Any]) -> List[str]:
    """Every volume claim template's name: each ordinal has a PVC of each."""
    return [c.get("metadata", {}).get("name")
            for c in sts.get("spec", {}).get("volumeClaimTemplates") or []]


def _replica_pvcs(sts: Dict[str, Any], opts: ApplyOptions, replicas: int) -> List[str]:
    """The PVCs of ordinals 1 and up, every claim's: WAL and data alike belong
    to the cluster a pg_upgrade or a rollback replaces."""
    return ["{0}-{1}-{2}".format(claim, opts.statefulset, i)
            for i in range(1, replicas) for claim in _claims(sts)]


def resolve_data_dir(instances: Sequence[Instance], sts: Dict[str, Any],
                     opts: ApplyOptions) -> Tuple[List[str], str, str]:
    """``(reasons, path, how)``: the data directory a major upgrade works on.

    ``--data-dir`` if given. Otherwise what the servers report, ordinal 0's
    first; they have to agree. :data:`DEFAULT_DATA_DIR` only when none can
    say, and ``how`` says so. Either way the path has to sit below the mount
    of one of the StatefulSet's volume claim templates, or the upgrade pod
    could not reach it.
    """
    first = "{0}-0".format(opts.statefulset)
    if opts.data_dir:
        path, how = opts.data_dir, "given with --data-dir"
    else:
        read = {i.pod: posixpath.normpath(i.data_directory)
                for i in instances if not i.error and i.data_directory}
        if len(set(read.values())) > 1:
            return (["the instances report different data directories ({0}); a major upgrade "
                     "works on one path on every disk. Nothing was done".format(", ".join(
                         "{0}={1}".format(p, d) for p, d in sorted(read.items())))], "", "")
        if read:
            pod = first if first in read else sorted(read)[0]
            path, how = read[pod], "read from {0}'s server".format(pod)
        else:
            path, how = DEFAULT_DATA_DIR, ("no server reported it, so the timescaledb-ha "
                                           "images' default; --data-dir sets it")
    if _data_mount(sts, opts, path) is None:
        mounts = ", ".join("{0} at {1}".format(m.get("name"), m.get("mountPath"))
                           for m in _claim_mounts(sts, opts)) or "none"
        return (["the data directory {0} ({1}) is not below the mount of any volume claim "
                 "template of statefulset/{2} (container {3} mounts: {4}); the pg_upgrade pod "
                 "could not reach it. Nothing was done".format(
                     path, how, opts.statefulset, opts.container, mounts)], path, how)
    return [], path, how


_TABLESPACES_SQL = ("SELECT coalesce(string_agg(spcname, ',' ORDER BY spcname), '') FROM pg_tablespace "
                    "WHERE spcname NOT IN ('pg_default', 'pg_global')")


def check_major_layout(cluster: "_Cluster", sts: Dict[str, Any],
                       primary: str) -> Tuple[List[str], Optional[str]]:
    """``(reasons, wal)``: what a major upgrade cannot handle, read before
    anything is written, and the WAL directory when ``pg_wal`` is a symlink.

    Tablespaces are refused: each lives on its own path, often its own
    volume, which the upgrade pod does not mount, and ``--link`` cannot
    cross volumes. WAL on another volume is handled (the script puts the new
    cluster's beside it), but only on a volume claim the upgrade pod mounts.
    """
    opts = cluster.opts
    reasons: List[str] = []
    rc, out, err = cluster.psql(primary, opts.admin_db, _TABLESPACES_SQL)
    if rc != 0:
        reasons.append("cannot read pg_tablespace on {0}: {1}".format(primary, (err or out).strip()))
    elif out.strip():
        reasons.append("tablespaces {0}: a major upgrade with tablespaces is not supported (the "
                       "pg_upgrade pod does not mount their paths, and --link cannot cross "
                       "volumes). Nothing was done".format(", ".join(out.strip().split(","))))
    # Not a symlink, or unreadable: the script looks again on the disk itself.
    rc, out, _ = cluster.kube.exec("{0}-0".format(opts.statefulset), opts.container,
                                   ["readlink", (opts.data_dir or "") + "/pg_wal"])
    wal = None
    if rc == 0 and out.strip():
        wal = posixpath.normpath(posixpath.join(opts.data_dir or "", out.strip()))
        if _data_mount(sts, opts, wal) is None:
            reasons.append("pg_wal is a symlink to {0}, which is not below the mount of any volume "
                           "claim template of statefulset/{1}; the pg_upgrade pod could not reach "
                           "it. Nothing was done".format(wal, opts.statefulset))
    return reasons, wal


def _same_pg(planned: str, running: Optional[str]) -> bool:
    """Major always; the minor too when the plan names one."""
    if not running:
        return False
    try:
        want, have = parse_version(planned), parse_version(running.split()[0])
    except InvalidVersion:
        return False
    return have[: len(want)] == want


def check_source(
    plan: Dict[str, Any], instances: Sequence[Instance], opts: ApplyOptions
) -> Tuple[List[str], List[str]]:
    """``(reasons, databases)``: does the cluster match ``plan.source``?

    Returns the databases the run will act on. Every one must be at exactly
    the source version, on a primary running the source PostgreSQL.
    """
    source_pg, source_ts = str(plan["source"]["pg"]), str(plan["source"]["ts"])
    primaries = [i for i in instances if i.role == "primary" and not i.error]
    if len(primaries) != 1:
        return ["expected exactly one reachable primary, found {0}".format(len(primaries))], []
    primary = primaries[0]

    reasons: List[str] = []
    if not _same_pg(source_pg, primary.server_version):
        reasons.append(
            "the plan is for PostgreSQL {0}, the primary runs {1}".format(
                source_pg, primary.server_version
            )
        )

    installed = {d: v for d, v in primary.extensions.items() if v}
    if opts.databases:
        databases = list(opts.databases)
        for db in databases:
            if db not in primary.extensions:
                reasons.append("database {0} does not exist".format(db))
            elif installed.get(db) != source_ts:
                reasons.append(
                    "database {0} has timescaledb {1}, the plan starts from {2}".format(
                        db, installed.get(db) or "not installed", source_ts
                    )
                )
    else:
        databases = sorted(installed)
        if not databases:
            reasons.append("no database has timescaledb installed")
        others = {d: v for d, v in installed.items() if v != source_ts}
        if others:
            reasons.append(
                "databases not at the source version {0}: {1}; every database "
                "has to be upgraded, so either bring them to {0} or scope the "
                "run with --database".format(
                    source_ts, ", ".join("{0}={1}".format(d, v) for d, v in sorted(others.items()))
                )
            )
    return reasons, databases


# ---------------------------------------------------------------------------
# The cluster, for writing
# ---------------------------------------------------------------------------


class _Cluster:
    def __init__(
        self,
        kube: Kube,
        opts: ApplyOptions,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        data_how: str = "",
        wal: Optional[str] = None,
    ) -> None:
        self.kube = kube
        self.opts = opts
        self.sleep = sleep
        self.clock = clock
        # Where opts.data_dir came from (resolve_data_dir), for the report.
        self.data_how = data_how
        # Where pg_wal points when it is a symlink (check_major_layout).
        self.wal = wal

    def pods(self) -> List[Dict[str, Any]]:
        return self.kube.json(["get", "pods", "-l", self.opts.selector]).get("items", [])

    def _container(self, pod: Dict[str, Any], status: bool = False) -> Dict[str, Any]:
        key = "containerStatuses" if status else "containers"
        section = pod.get("status" if status else "spec", {}).get(key) or []
        for c in section:
            if c.get("name") == self.opts.container:
                return c
        return {}

    def pod_image(self, pod: Dict[str, Any]) -> Optional[str]:
        return self._container(pod).get("image")

    def pod_image_id(self, pod: Dict[str, Any]) -> Optional[str]:
        return self._container(pod, status=True).get("imageID")

    @staticmethod
    def role(pod: Dict[str, Any]) -> str:
        return patroni_role(pod.get("metadata", {}).get("labels"))

    @staticmethod
    def ready(pod: Dict[str, Any]) -> bool:
        for cond in pod.get("status", {}).get("conditions") or []:
            if cond.get("type") == "Ready":
                return cond.get("status") == "True"
        return False

    def primary(self) -> Optional[str]:
        for pod in self.pods():
            if self.role(pod) == "primary" and self.ready(pod):
                return pod["metadata"]["name"]
        return None

    def psql(self, pod: str, database: str, sql: str) -> Tuple[int, str, str]:
        """One statement in a fresh session, writes allowed, no .psqlrc."""
        return self.kube.exec(
            pod,
            self.opts.container,
            ["psql", "-U", self.opts.psql_user, "-d", database, "-X", "-qtAF", "|",
             "-v", "ON_ERROR_STOP=1", "-c", sql],
        )

    def wait_for(self, what: str, test: Callable[[], bool]) -> None:
        deadline = self.clock() + self.opts.rollout_timeout
        while True:
            try:
                if test():
                    return
            except PreflightError:
                pass  # the API or a pod may be briefly unavailable mid-rollout
            if self.clock() >= deadline:
                raise _StepFailed(
                    "timed out after {0:.0f}s waiting for {1}".format(
                        self.opts.rollout_timeout, what
                    )
                )
            self.sleep(self.opts.poll_interval)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _route_images(later: Sequence[Any], opts: ApplyOptions) -> Tuple[RouteImage, ...]:
    """Each image the steps still have to run, as it will be asked for."""
    found: Dict[str, bool] = {}

    def add(image: str, digest: Optional[str], on_statefulset: bool) -> None:
        reference = mirrored(pinned(image, digest), opts.image_registry)
        found[reference] = found.get(reference, False) or on_statefulset

    for step in later:
        if isinstance(step, EnsureImage):
            add(step.image, step.digest, True)
        elif isinstance(step, MajorUpgrade):
            add(step.image, step.digest, False)
            add(step.run_image, step.run_digest, True)
    return tuple(RouteImage(reference, on) for reference, on in found.items())


def _step_preflight(
    step: Preflight,
    opts: ApplyOptions,
    preflight: Callable[[PreflightOptions], Sequence[Any]],
    later: Sequence[Any] = (),
    first: bool = True,
) -> _Outcome:
    results = preflight(
        PreflightOptions(
            target_image=step.image,
            context=opts.context,
            namespace=opts.namespace,
            selector=opts.selector,
            container=opts.container,
            psql_user=opts.psql_user,
            admin_db=opts.admin_db,
            target_pg=step.target_pg,
            target_ts=step.target_ts,
            matrix_path=opts.matrix_path,
            timeout=opts.timeout,
            registry_timeout=opts.registry_timeout,
            pg_upgrade_link=step.pg_upgrade.link if step.pg_upgrade else None,
            pg_upgrade_image=step.pg_upgrade.image if step.pg_upgrade else None,
            pg_upgrade_to=step.pg_upgrade.to_pg if step.pg_upgrade else None,
            route_images=_route_images(later, opts) if first else (),
            route_checked=not first,
            use_cached_images=opts.use_cached_images,
            statefulset=opts.statefulset,
            discovered=opts.discovered,
        )
    )
    lines = ["{0}: {1} - {2}".format(r.name, r.status, r.summary) for r in results]
    notices = [r.notice for r in results if getattr(r, "notice", None)]
    for notice in notices:
        if opts.notify:
            opts.notify(notice)
    failed = [r for r in results if r.status == FAIL]
    if failed:
        detail = []
        for r in failed:
            detail.append("{0}: {1}".format(r.name, r.summary))
            detail.extend("  " + d for d in r.details)
        raise _StepFailed(
            "preflight failed: {0}".format(", ".join(r.name for r in failed)), detail
        )
    return _Outcome("; ".join(["{0} checks, none failed".format(len(results))] + notices), lines)


def _step_ensure_image(step: EnsureImage, cluster: _Cluster) -> _Outcome:
    opts = cluster.opts
    image = mirrored(pinned(step.image, step.digest), opts.image_registry)
    sts = cluster.kube.json(["get", "statefulset", opts.statefulset])
    containers = sts.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    template_image = next(
        (c.get("image") for c in containers if c.get("name") == opts.container), None
    )
    pods = cluster.pods()
    stale = [p for p in pods if cluster.pod_image(p) != image]
    if template_image == image and not stale:
        return _Outcome("already running {0}".format(image), status=SKIPPED)

    output: List[str] = []
    try:
        _roll(cluster, image, template_image, stale, output)
    except (_StepFailed, PreflightError) as exc:
        # Keep what was already done: a half-finished rollout is the state
        # the operator has to act on.
        summary = exc.summary if isinstance(exc, _StepFailed) else str(exc)
        extra = exc.output if isinstance(exc, _StepFailed) else []
        raise _StepFailed(summary, output + extra) from exc
    return _Outcome("rolled {0} pod(s) onto {1}".format(len(stale), image), output)


def _roll(
    cluster: _Cluster,
    image: str,
    template_image: Optional[str],
    stale: List[Dict[str, Any]],
    output: List[str],
) -> None:
    opts = cluster.opts
    if template_image != image:
        rc, out, err = cluster.kube.run(
            ["set", "image", "statefulset/" + opts.statefulset,
             "{0}={1}".format(opts.container, image)]
        )
        if rc != 0:
            raise _StepFailed("kubectl set image failed", [err or out])
        output.append("statefulset/{0}: {1} -> {2}".format(opts.statefulset, template_image, image))

    # Replicas first, the primary last.
    stale.sort(key=lambda p: (cluster.role(p) == "primary", p["metadata"]["name"]))
    for pod in stale:
        name = pod["metadata"]["name"]
        old_uid = pod["metadata"].get("uid")
        # Not --wait: Patroni gets the pod's whole grace period to shut down,
        # which outlasts a single kubectl call's timeout. The poll below waits
        # instead, under the rollout timeout.
        rc, out, err = cluster.kube.run(["delete", "pod", name, "--wait=false"])
        if rc != 0:
            raise _StepFailed("kubectl delete pod {0} failed".format(name), [err or out])
        output.append("{0} deleted".format(name))

        def recreated() -> bool:
            rc, out, _ = cluster.kube.run(["get", "pod", name, "-o", "json"])
            if rc != 0:
                return False
            fresh = json.loads(out)
            return (
                fresh["metadata"].get("uid") != old_uid
                and cluster.pod_image(fresh) == image
                and cluster.ready(fresh)
            )

        cluster.wait_for("{0} to come back on {1}".format(name, image), recreated)
        output.append("{0} recreated on {1}".format(name, image))

    cluster.wait_for("a ready primary", lambda: cluster.primary() is not None)

    def accepting() -> bool:
        primary = cluster.primary()
        return primary is not None and cluster.psql(primary, opts.admin_db, "SELECT 1")[0] == 0

    cluster.wait_for("the primary to accept connections", accepting)
    output.append("primary is {0}".format(cluster.primary()))


def _step_update(
    step: UpdateExtension, cluster: _Cluster, databases: Sequence[str]
) -> _Outcome:
    primary = cluster.primary()
    if primary is None:
        raise _StepFailed("no ready primary to run ALTER EXTENSION on")
    output: List[str] = []
    for db in databases:
        rc, out, err = cluster.psql(primary, db, step.sql)
        if rc != 0:
            done = ", ".join(output) or "none"
            raise _StepFailed(
                "ALTER EXTENSION failed in database {0} (already updated: {1})".format(db, done),
                [line for line in (err or out).splitlines() if line.strip()],
            )
        output.append(db)
    return _Outcome("updated {0} database(s) to {1}".format(len(databases), step.to), [
        "{0}: {1}".format(db, step.sql) for db in output
    ])


# Below 2.14 cagg_migrate has not been run here, and from 2.25 it is gone.
_MIGRATE_FROM, _MIGRATE_BELOW = (2, 14), (2, 25)
_REFRESH_AFTER = re.compile(
    r'executing this statement: "(CALL [\w.]*refresh_continuous_aggregate\(\'[^\']*\', '
    r"(?:CAST\('[^']*' AS [a-z ]+\)|NULL), (?:CAST\('[^']*' AS [a-z ]+\)|NULL)\);)\"")
# What cagg_migrate drops without a word (measured: the grants; the owner is
# the role that ran it).
_GRANTS_SQL = (
    "SELECT format('GRANT %s ON %s TO %s', e.privilege_type, v.oid::regclass, "
    "CASE e.grantee WHEN 0 THEN 'PUBLIC' ELSE quote_ident(g.rolname) END) "
    "FROM pg_class v, aclexplode(v.relacl) e LEFT JOIN pg_roles g ON g.oid = e.grantee "
    "WHERE v.oid = to_regclass({0}) AND e.grantee <> v.relowner"
)
_OWNER_SQL = "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = to_regclass({0})"


def _in_migration_window(version: str) -> bool:
    key = parse_version(version)
    return _MIGRATE_FROM <= key[:2] < _MIGRATE_BELOW


def _size(size: int) -> str:
    value, unit = float(size), "bytes"
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            break
        value /= 1024
    return "{0:.0f} {1}".format(value, unit) if unit == "bytes" else "{0:.1f} {1}".format(value, unit)


def _describe_cagg(cagg: OldCagg) -> List[str]:
    return [
        "  {0}: {1}".format(cagg.database, cagg.name),
        "    built on {0}; {1} in {2} chunk(s); {3} refresh or other job(s)".format(
            cagg.built_on, _size(cagg.size_bytes), cagg.chunks, cagg.jobs),
        "    views that depend on it: {0}".format(", ".join(cagg.dependents) or "none"),
        "    grants: {0}".format(", ".join(cagg.grants) or "none"),
    ]


def _quoted_name(name: str) -> str:
    """schema.name as an exact identifier, for pg_dump -t and DROP."""
    return ".".join('"' + part.replace('"', '""') + '"' for part in name.split(".", 1))


# Measured in tests/test_cagg_migrate_container.py: save, drop, migrate, load.
NL = chr(10)

_VIEW_DOCS = [
    "  https://www.postgresql.org/docs/current/app-pgdump.html",
    "  https://www.postgresql.org/docs/current/sql-dropview.html",
]


def _view_guidance(cluster: _Cluster, pod: str, found: Sequence[OldCagg],
                   runner: Callable[[str, str], Tuple[int, str, str]]) -> List[str]:
    """What to run, view by view, to take the dependent views out of the way and
    put them back: the operator runs it; apply only reads."""
    opts = cluster.opts
    user = opts.psql_user

    def in_pod(*words: str) -> str:
        return _kubectl(opts, "exec", "-i", pod, "-c", opts.container, "--") + " " + " ".join(
            shlex.quote(w) for w in words)

    lines = [
        "Views depend on them, and cagg_migrate fails partway with a view in place, leaving the "
        "old aggregate behind. Save and drop each view, run apply, then recreate it from its file:",
    ]
    for cagg in found:
        for view in cagg.dependents:
            rc, kind, _ = runner(cagg.database, "SELECT relkind FROM pg_class WHERE oid = to_regclass({0})".format(
                _literal(_quoted_name(view))))
            materialized = rc == 0 and kind.strip() == "m"
            what = "MATERIALIZED VIEW" if materialized else "VIEW"
            exact = _quoted_name(view)
            saved = "{0}.{1}.sql".format(cagg.database, view)
            lines += [
                "  {0}: {1}{2}, on {3}".format(cagg.database, view, " (materialized)" if materialized else "",
                                              cagg.name),
                "    save:     " + in_pod("pg_dump", "-U", user, "-d", cagg.database, "--schema-only",
                                          "-t", exact) + " > " + shlex.quote(saved),
                "    drop:     " + in_pod("psql", "-U", user, "-d", cagg.database, "-c",
                                          "DROP {0} {1}".format(what, exact)),
                "    recreate, after apply: " + in_pod("psql", "-U", user, "-d", cagg.database, "-v",
                                                       "ON_ERROR_STOP=1") + " < " + shlex.quote(saved),
            ]
            if materialized:
                lines.append("    then refresh it: " + in_pod("psql", "-U", user, "-d", cagg.database, "-c",
                                                             "REFRESH MATERIALIZED VIEW " + exact))
    lines += [
        "The file keeps the view's definition, owner, grants and comment. A view on another "
        "view is dropped first and recreated last.",
    ] + _VIEW_DOCS
    return lines


def _on_pg(steps: Sequence[Any], index: int, plan: Dict[str, Any]) -> str:
    """" on PostgreSQL N" at that point of the plan, or nothing if unknown."""
    majors = [s for s in steps[:index] if isinstance(s, MajorUpgrade)]
    if majors:
        return " on PostgreSQL {0}".format(majors[-1].to_pg)
    source = str((plan.get("source") or {}).get("pg") or "")
    return " on PostgreSQL {0}".format(source.split(".")[0]) if source else ""


def _plan_cagg_migration(
    steps: List[Any], plan: Dict[str, Any], cluster: _Cluster, instances: Sequence[Instance],
    databases: Sequence[str], opts: ApplyOptions,
) -> None:
    """Before anything changes: does this route reach the 2.25 refusal with
    aggregates it will refuse? If so stop, and say what they are, unless the
    operator agrees to migrate them; then add the step. Read-only until then."""
    index, crossing = next(((i, s) for i, s in enumerate(steps) if isinstance(s, UpdateExtension)
                            and reaches_old_cagg_removal(s.to)), (None, None))
    if crossing is None:
        return
    primary = next((i for i in instances if i.role == "primary" and not i.error), None)
    unread: List[str] = [] if primary else ["no primary could be queried: " + "; ".join(
        "{0} is {1}{2}".format(i.pod, i.role, ", " + i.error if i.error else "") for i in instances
    ) if instances else "no primary could be queried: no instances were found"]
    failed: Dict[str, str] = {}

    def runner(database: str, sql: str) -> Tuple[int, str, str]:
        rc, out, err = cluster.psql(primary.pod, database, sql)
        if rc != 0:
            said = [line.strip() for line in (err or out).splitlines() if line.strip()]
            failed.setdefault(database, said[0] if said else "psql exited {0}".format(rc))
        return rc, out, err

    found: List[OldCagg] = []
    bucket: List[str] = []
    for database in databases if primary else ():
        version = primary.extensions.get(database)
        caggs = old_caggs(runner, database, version)
        ng = time_bucket_ng_caggs(runner, database)
        if caggs is None or ng is None:
            unread.append("{0}: {1}".format(database, failed.get(
                database, "the catalog answered in a form this version does not read")))
            continue
        found.extend(caggs)
        if ng:
            bucket.append("  {0}: {1}".format(database, ng))
    footer = ["", "This came from a read-only check. Nothing has been changed."]
    target = "this plan updates TimescaleDB to {0}{1}".format(crossing.to, _on_pg(steps, index, plan))
    if unread:
        # Not going on to find out at that hop, after every pg_upgrade before it.
        majors = [s for s in steps[:index] if isinstance(s, MajorUpgrade)]
        later = (" Finding them only then would be after upgrading PostgreSQL from {0} to {1}.".format(
            majors[0].from_pg, majors[-1].to_pg) if majors else "")
        raise ApplyRefused([
            "apply did not start: it could not check every database for continuous aggregates "
            "created before TimescaleDB 2.7.",
        ] + ["  " + line for line in unread] + [
            "Check that the primary is running and that `psql -U {0}` can read "
            "_timescaledb_catalog in each database, then run apply again.".format(cluster.opts.psql_user),
            "",
            "Why it checks first: {0}, and TimescaleDB 2.25 and later refuse those "
            "aggregates.{1}".format(target, later),
        ] + footer, plain=True)
    if not found and not bucket:
        return

    source = str(plan["source"]["ts"])
    stop: Optional[int] = 1 if _in_migration_window(source) else None
    release = source
    if stop is None:
        for i, step in enumerate(steps[:index]):
            if isinstance(step, VerifyVersion) and _in_migration_window(step.expect):
                stop, release = i + 1, step.expect
                break
    hop = steps[stop - 1].hop if stop else None

    which = ("continuous aggregates created before 2.7" if not bucket
             else "continuous aggregates built on time_bucket_ng" if not found
             else "some continuous aggregates here")
    lines = [
        "apply did not start: TimescaleDB 2.25 and later refuse {0}, and {1}.".format(which, target),
    ]
    if found:
        lines += ["", "Created before TimescaleDB 2.7 (non-finalized, the old format):"]
        for cagg in found:
            lines += _describe_cagg(cagg)
    blocked: List[str] = []
    if bucket:
        blocked += ["Built on time_bucket_ng, which no known migration fixes; rebuild these "
                    "without it first:"] + bucket
    if any(cagg.dependents for cagg in found):
        blocked += _view_guidance(cluster, primary.pod, found, runner)
    if found and stop is None:
        blocked.append("This plan never stops at a TimescaleDB release from 2.14 to 2.24, where "
                       "cagg_migrate is known to work. Plan to one first, migrate, then plan on.")
    if blocked:
        raise ApplyRefused(lines + [""] + blocked + footer, plain=True)

    lines += [
        "",
        "They have to be migrated before 2.25, which also removes cagg_migrate. If you agree, "
        "apply does it partway through the plan, when TimescaleDB reaches {0}{1}:".format(
            release, _on_pg(steps, stop, plan)),
        "  CALL cagg_migrate('<aggregate>', override => true, drop_old => true)",
        "This drops the old aggregate and cannot be undone, so have a backup. It needs about "
        "the size above free again and takes as long as rebuilding that data. apply then puts "
        "back the grants and owner the call removes.",
    ]
    if not opts.migrate_caggs and not (opts.confirm and opts.confirm(NL.join(lines + footer))):
        raise ApplyRefused(lines + ["", "To go on: run apply again with --migrate-caggs, or answer "
                                    "y at its prompt. Or plan to a target below 2.25."] + footer, plain=True)
    steps.insert(stop, MigrateCaggs(hop=hop, expect=release))


def _step_migrate_caggs(step: MigrateCaggs, cluster: _Cluster, databases: Sequence[str]) -> _Outcome:
    primary = cluster.primary()
    if primary is None:
        raise _StepFailed("no ready primary to run cagg_migrate on")
    # Rebuilding an aggregate takes as long as its data does.
    kube = cluster.kube
    if isinstance(kube, Kube):
        kube = Kube(kube.context, kube.namespace, 7 * 24 * 3600.0)

    def run(database: str, sql: str) -> Tuple[int, str, str]:
        return kube.exec(primary, cluster.opts.container, [
            "psql", "-U", cluster.opts.psql_user, "-d", database, "-X", "-qtAF", "|",
            "-v", "ON_ERROR_STOP=1", "-c", sql])

    output: List[str] = []
    migrated = 0
    for database in databases:
        caggs = old_caggs(lambda d, q: run(d, q), database, step.expect)
        if caggs is None:
            raise _StepFailed("cannot read the continuous aggregates of {0}".format(database), output)
        for cagg in caggs:
            quoted = _literal(cagg.name)
            _, grants, _ = run(database, _GRANTS_SQL.format(quoted))
            _, owner, _ = run(database, _OWNER_SQL.format(quoted))
            rc, out, err = run(database, "CALL cagg_migrate({0}, override => true, drop_old => true)".format(quoted))
            if rc != 0:
                raise _StepFailed("cagg_migrate failed for {0} in {1}".format(cagg.name, database), output + [
                    line for line in (err or out).splitlines() if line.strip()] + [
                    "it may be half done: look for {0}_new and {0}_old, and at "
                    "_timescaledb_catalog.continuous_agg_migrate_plan".format(cagg.name)])
            output.append("{0}: migrated {1}".format(database, cagg.name))
            refresh = _REFRESH_AFTER.search(err or "")
            followups = [refresh.group(1)] if refresh else []
            if owner.strip():
                followups.append("ALTER MATERIALIZED VIEW {0} OWNER TO {1}".format(cagg.name, owner.strip()))
            followups.extend(g for g in grants.splitlines() if g.strip())
            for sql in followups:
                rc, out, err = run(database, sql)
                if rc != 0:
                    raise _StepFailed("after migrating {0} in {1}: {2}".format(cagg.name, database, sql),
                                      output + [line for line in (err or out).splitlines() if line.strip()])
                output.append("{0}: {1}".format(database, sql))
            migrated += 1
        left = old_caggs(lambda d, q: run(d, q), database, step.expect)
        if left:
            raise _StepFailed("{0} still holds old-format aggregates: {1}".format(
                database, ", ".join(c.name for c in left)), output)
    return _Outcome("migrated {0} continuous aggregate(s) at {1}".format(migrated, step.expect), output)


def _step_cycle(cluster: _Cluster, databases: Sequence[str]) -> _Outcome:
    names = "ARRAY[{0}]::name[]".format(", ".join(_literal(d) for d in databases))
    sql = (
        "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
        "WHERE datname = ANY({0}) AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend'".format(names)
    )
    output = []
    for pod in cluster.pods():
        name = pod["metadata"]["name"]
        rc, out, err = cluster.psql(name, cluster.opts.admin_db, sql)
        if rc != 0:
            raise _StepFailed("cannot terminate sessions on {0}".format(name), [err or out])
        output.append("{0}: {1} session(s) terminated".format(name, out.strip() or "0"))
    return _Outcome("sessions cycled on {0} instance(s)".format(len(output)), output)


def _step_verify(
    step: VerifyVersion, cluster: _Cluster, databases: Sequence[str]
) -> _Outcome:
    expect = step.expect
    sql = "SELECT coalesce((SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'), '')"
    pods = [p["metadata"]["name"] for p in cluster.pods()]
    seen: Dict[Tuple[str, str], str] = {}

    def all_match() -> bool:
        for pod in pods:
            for db in databases:
                if seen.get((pod, db)) == expect:
                    continue
                rc, out, err = cluster.psql(pod, db, sql)
                seen[(pod, db)] = out.strip() if rc == 0 else "error: " + (err or out)
        return all(v == expect for v in seen.values())

    # Replicas report the new version only once they have replayed it.
    try:
        cluster.wait_for("every instance to report {0}".format(expect), all_match)
    except _StepFailed as exc:
        wrong = ["{0}/{1}: {2}".format(p, d, v) for (p, d), v in sorted(seen.items()) if v != expect]
        raise _StepFailed("not every database reports {0}".format(expect), wrong) from exc
    return _Outcome("{0} database(s) x {1} instance(s) report {2}".format(
        len(databases), len(pods), expect), [
        "{0}/{1}: {2}".format(p, d, v) for (p, d), v in sorted(seen.items())
    ])


def load_script(name: str) -> str:
    """A script from ``tsupgrade/scripts``, read as package data."""
    try:
        from importlib.resources import files
    except ImportError:  # Python 3.8
        from importlib.resources import read_text
        return read_text("tsupgrade.scripts", name, encoding="utf-8")
    return (files("tsupgrade") / "scripts" / name).read_text(encoding="utf-8")


# Runs in the pg_upgrade pod, as the StatefulSet's user, on ordinal 0's disk.
_PG_UPGRADE_SCRIPT = load_script("pg_upgrade.sh")


def _step_major_upgrade(step: MajorUpgrade, cluster: _Cluster) -> _Outcome:
    output: List[str] = []
    # What a failure from here on leaves behind and how to go on; replaced as
    # the upgrade moves through its stages, and attached to every failure.
    state: List[str] = []
    try:
        return _major_upgrade(step, cluster, output, state)
    except (_StepFailed, PreflightError) as exc:
        summary = exc.summary if isinstance(exc, _StepFailed) else str(exc)
        extra = exc.output if isinstance(exc, _StepFailed) else []
        raise _StepFailed(summary, output + extra + [l for l in state if l not in extra]) from exc


def _kubectl(opts: ApplyOptions, *args: str) -> str:
    """A kubectl command line for the operator, in this run's context and namespace."""
    context = ["--context", opts.context] if opts.context else []
    return " ".join(["kubectl"] + context + ["-n", opts.namespace] + list(args))


def _pod_phase(kube: Kube, pod: str) -> Optional[Dict[str, Any]]:
    """The pod's status, or None when there is no such pod."""
    rc, out, err = kube.run(["get", "pod", pod, "--ignore-not-found", "-o", "json"])
    if rc != 0:
        raise _StepFailed("cannot look for pod {0}".format(pod), [err or out])
    return (json.loads(out).get("status") or {}) if out.strip() else None


def _why_pending(status: Dict[str, Any]) -> str:
    for c in status.get("containerStatuses") or []:
        waiting = (c.get("state") or {}).get("waiting") or {}
        if waiting.get("reason"):
            return " ".join(filter(None, [waiting["reason"], waiting.get("message")]))
    for cond in status.get("conditions") or []:
        if cond.get("status") == "False" and cond.get("message"):
            return cond["message"]
    return "no reason given"


# Patroni's objects in a Kubernetes DCS, by scope. Measured on Patroni 4.0.5
# (2026-10-31): with use_endpoints, endpoints <scope> and <scope>-config;
# without, configmaps <scope>-config and <scope>-leader; either way
# <scope>-failover once a switchover has run. <scope>-sync (synchronous mode)
# was not measured; it is Patroni's name for that state.
_DCS_SUFFIXES = ("", "-config", "-leader", "-failover", "-sync")


def _patroni_scope(kube: Kube, pod: str, container: str) -> str:
    """The Patroni scope, as Patroni itself reports it. Both sources were
    checked on Patroni 2.0.2 (the pg11 image) and 4.0.5: ``patronictl list
    -f json`` names it per member, REST ``/patroni`` as ``patroni.scope``."""
    rc, out, _ = kube.exec(pod, container, ["patronictl", "list", "-f", "json"])
    try:
        clusters = {m.get("Cluster") for m in json.loads(out)} if rc == 0 else set()
    except (ValueError, AttributeError):
        clusters = set()
    if len(clusters) == 1 and None not in clusters:
        return clusters.pop()
    rc, out, err = kube.exec(pod, container, ["curl", "-s", "localhost:8008/patroni"])
    try:
        scope = json.loads(out).get("patroni", {}).get("scope") if rc == 0 else None
    except ValueError:
        scope = None
    if not scope:
        raise _StepFailed("cannot read Patroni's scope from {0} (patronictl list, REST "
                          "/patroni); nothing was done".format(pod), [err or out])
    return scope


def _dcs_objects(kube: Kube, opts: ApplyOptions, scope: str) -> Tuple[List[str], List[str]]:
    """``(patroni, others)``: the endpoints and configmaps the pod selector
    matches, as ``kind/name``, split by whether the name is Patroni's."""
    names = {scope + suffix for suffix in _DCS_SUFFIXES}
    items = kube.json(["get", "endpoints,configmaps", "-l", opts.selector]).get("items", [])
    patroni, others = [], []
    for item in items:
        ref = "{0}/{1}".format(item.get("kind", "").lower() or "object",
                               item.get("metadata", {}).get("name"))
        (patroni if item.get("metadata", {}).get("name") in names else others).append(ref)
    return sorted(patroni), sorted(others)


def _finish_by_hand(opts: ApplyOptions, pvcs: Sequence[str], replicas: int, run_image: str,
                    dcs: Sequence[str]) -> List[str]:
    """What apply does after a successful pg_upgrade, as commands."""
    lines = [_kubectl(opts, "delete", "pvc", pvc) for pvc in pvcs]
    return lines + [
        _kubectl(opts, "delete", *dcs, "--ignore-not-found") if dcs
        else "(Patroni had no objects in the DCS to delete)",
        _kubectl(opts, "set", "image", "statefulset/" + opts.statefulset,
                 "{0}={1}".format(opts.container, run_image)),
        _kubectl(opts, "scale", "statefulset/" + opts.statefulset, "--replicas={0}".format(replicas)),
    ]


_PROVIDERS = {"c": "libc", "i": "icu", "b": "builtin"}


def _old_locale(cluster: _Cluster, primary: str, old: int) -> Dict[str, str]:
    """The old cluster's template0 encoding, locale and provider, as the
    pg_upgrade script's initdb takes them. template0 is what pg_upgrade
    compares (13-15) or copies (16+)."""
    provider = ("datlocprovider" if old >= 15 else "'c'")
    provider_locale = ("coalesce(datlocale, '')" if old >= 17
                       else "coalesce(daticulocale, '')" if old >= 15 else "''")
    rc, out, err = cluster.psql(primary, cluster.opts.admin_db, (
        "SELECT pg_encoding_to_char(encoding), datcollate, datctype, {0}, {1} "
        "FROM pg_database WHERE datname = 'template0'").format(provider, provider_locale))
    parts = out.strip().split("|")
    if rc != 0 or len(parts) != 5 or not all(parts[:3]):
        raise _StepFailed("cannot read template0's encoding and locale", [err or out])
    encoding, collate, ctype, kind, provider_value = parts
    return {"ENCODING": encoding, "LC_COLLATE": collate, "LC_CTYPE": ctype,
            "LOCALE_PROVIDER": _PROVIDERS.get(kind, kind), "PROVIDER_LOCALE": provider_value}


def _after_failed_upgrade(lines: Sequence[str], opts: ApplyOptions, old: int, replicas: int,
                          claim: str, wal: Optional[str] = None) -> List[str]:
    """What a failed pg_upgrade left, from the script's exit lines; empty when
    the old cluster is as it was (a copy, or --link before linking began)."""
    def said(prefix: str) -> List[str]:
        return [l.split(": ", 1)[1] for l in lines if l.startswith(prefix + ": ")]

    disabled, moved = said("old cluster disabled"), said("old data directory moved")
    swapped, wal_moved = said("data directories swapped"), said("old WAL directory moved")
    if not (disabled or moved or swapped or wal_moved):
        return []
    disk = "{0}-{1}-0".format(claim, opts.statefulset)
    notes = ["the cluster is stopped, and PostgreSQL {0} will not start on ordinal 0's data "
             "as it is now:".format(old)]
    if moved:
        notes.append("    the old data directory was moved to {0}; move it back to {1}".format(
            moved[0], opts.data_dir))
    if swapped:
        notes.append("    pg_upgrade had finished and the directories were swapped: {0} is the "
                     "upgraded cluster and the old one is {0}.pg{1}; move {0} aside and "
                     "{0}.pg{1} back to {0}".format(opts.data_dir, old))
    if wal_moved:
        notes.append("    the old WAL directory was moved to {0}; move what is at {1} aside, {0} "
                     "back to {1}, and point {2}/pg_wal at it (ln -sfn {1} {2}/pg_wal)".format(
                         wal_moved[0], wal or wal_moved[0].rsplit(".pg", 1)[0], opts.data_dir))
    for path in disabled:
        notes.append("    pg_upgrade --link had started linking and renamed global/pg_control "
                     "to {0}. The new cluster was never started, so the old one is unmodified "
                     "otherwise: rename it back (mv {0} {1})".format(path, path[:-len(".old")]))
    notes.append("do that in a pod that mounts {0} (the upgrade pod's image will do), then: "
                 "{1}".format(disk, _kubectl(opts, "scale", "statefulset/" + opts.statefulset,
                                             "--replicas={0}".format(replicas))))
    return notes


def _await_pg_upgrade(cluster: _Cluster, pod: str, waiting: List[str]) -> str:
    """Wait for the pod to finish; returns its phase.

    A pod that never starts is bounded by ``rollout_timeout``: nothing has run
    yet. Once it runs, ``pg_upgrade_timeout`` (0: no limit), because a copy of
    a real database takes as long as it takes. Either way the pod is left
    alone: apply stops waiting, it does not stop the upgrade.
    """
    opts, kube = cluster.opts, cluster.kube
    started = cluster.clock()
    running_since: Optional[float] = None
    while True:
        status = _pod_phase(kube, pod) or {}
        phase = status.get("phase")
        if phase in ("Succeeded", "Failed"):
            return phase
        now = cluster.clock()
        if phase == "Running":
            running_since = now if running_since is None else running_since
            if opts.pg_upgrade_timeout and now - running_since >= opts.pg_upgrade_timeout:
                raise _StepFailed(
                    "pg_upgrade is still running in pod {0} after {1:.0f}s; apply stopped "
                    "waiting, the upgrade goes on".format(pod, now - running_since),
                    ["do not start the cluster while it runs: PostgreSQL must not open "
                     "ordinal 0's data directory under pg_upgrade",
                     "watch it: " + _kubectl(opts, "logs", "-f", pod),
                     "when it shows Succeeded (" + _kubectl(opts, "get", "pod", pod)
                     + "), finish by hand:"] + ["    " + l for l in waiting]
                    + ["if it shows Failed instead, its log says why, and the data is as a "
                       "failed pg_upgrade leaves it"])
        elif now - started >= opts.rollout_timeout:
            raise _StepFailed(
                "the pg_upgrade pod {0} has not started after {1:.0f}s ({2}: {3}); "
                "nothing has run, the data is untouched".format(
                    pod, now - started, phase or "no phase", _why_pending(status)),
                ["the pod is left as it is; if it starts later it runs pg_upgrade, so delete "
                 "it before starting the cluster: " + _kubectl(opts, "delete", "pod", pod)])
        cluster.sleep(opts.poll_interval)


# What the pg_upgrade pod takes from the StatefulSet's pod template, so it
# can be scheduled, and pull its image, wherever the database pods can.
_SCHEDULING = ("tolerations", "nodeSelector", "affinity", "imagePullSecrets",
               "serviceAccountName", "priorityClassName")


def _volume(sts: Dict[str, Any], opts: ApplyOptions) -> Dict[str, Any]:
    """The database container's mount of the claim template that holds the
    data directory, as the upgrade pod repeats it (``subPath`` included)."""
    mount = _data_mount(sts, opts, opts.data_dir or "")
    if mount is None:
        raise _StepFailed("the data directory {0} is not below the mount of any volume claim "
                          "template of statefulset/{1}".format(opts.data_dir, opts.statefulset))
    return {k: mount[k] for k in ("name", "mountPath", "subPath") if mount.get(k)}


def _run_on_ordinal_0(kube: Kube, sts: Dict[str, Any], opts: ApplyOptions, pod: str, image: str,
                      script: str, env: Dict[str, str]) -> Tuple[int, str, str]:
    """Start a one-off pod running ``script`` on ordinal 0's disks: every
    claim template the database container mounts, where it mounts them.

    Scheduled as the database pods are: where they may run (taints,
    selectors, affinity), with their pull secrets, account, resources and
    user. Without these it can sit Pending on a cluster with dedicated
    database nodes or a private registry.
    """
    template = sts.get("spec", {}).get("template", {}).get("spec", {})
    container = next((c for c in template.get("containers") or []
                      if c.get("name") == opts.container), {})
    _volume(sts, opts)  # the data directory has to be on one of them
    mounts = [{k: m[k] for k in ("name", "mountPath", "subPath") if m.get(k)}
              for m in _claim_mounts(sts, opts)]
    spec = {
        "apiVersion": "v1",
        "spec": dict({key: template[key] for key in _SCHEDULING if template.get(key)}, **{
            "restartPolicy": "Never",
            "securityContext": template.get("securityContext") or {},
            "containers": [dict({"resources": container["resources"]} if container.get("resources")
                                else {}, **{
                "name": "pg-upgrade",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["bash", "-c", script],
                "env": [{"name": k, "value": v} for k, v in env.items()],
                "volumeMounts": mounts,
            })],
            "volumes": [{"name": m["name"], "persistentVolumeClaim": {
                "claimName": "{0}-{1}-0".format(m["name"], opts.statefulset)}} for m in mounts],
        }),
    }
    return kube.run(["run", pod, "--image", image, "--restart=Never",
                     "--labels", "app.kubernetes.io/part-of=timescaledb-upgrader",
                     "--overrides", json.dumps(spec)])


@dataclass
class _Hop:
    """One major upgrade, as its stages see it. ``output`` and ``state`` are
    shared with the step: what was done, and what a failure now leaves."""

    step: MajorUpgrade
    cluster: _Cluster
    sts: Dict[str, Any]
    output: List[str]
    state: List[str]
    scope: str = ""
    dcs: List[str] = field(default_factory=list)
    locale: Dict[str, str] = field(default_factory=dict)
    system_identifier: str = ""

    @property
    def opts(self) -> ApplyOptions:
        return self.cluster.opts

    @property
    def kube(self) -> Kube:
        return self.cluster.kube

    @property
    def replicas(self) -> int:
        return int(self.sts.get("spec", {}).get("replicas") or 1)

    @property
    def claim(self) -> str:
        return _volume(self.sts, self.opts)["name"]

    @property
    def wal_kept(self) -> Optional[str]:
        wal = self.cluster.wal
        return "{0}.pg{1}".format(wal, self.step.from_pg) if wal else None

    @property
    def first(self) -> str:
        return "{0}-0".format(self.opts.statefulset)

    @property
    def pod(self) -> str:
        return "{0}-pg-upgrade-{1}-{2}".format(self.opts.statefulset, self.step.from_pg, self.step.to_pg)

    @property
    def image(self) -> str:
        return mirrored(pinned(self.step.image, self.step.digest), self.opts.image_registry)

    @property
    def run_image(self) -> str:
        return mirrored(pinned(self.step.run_image, self.step.run_digest), self.opts.image_registry)

    @property
    def old_image(self) -> Optional[str]:
        """What the cluster ran before, for a rollback to start it on again."""
        template = self.sts.get("spec", {}).get("template", {}).get("spec", {})
        return next((c.get("image") for c in template.get("containers") or []
                     if c.get("name") == self.opts.container), None)

    def scale_command(self) -> str:
        return _kubectl(self.opts, "scale", "statefulset/" + self.opts.statefulset,
                        "--replicas={0}".format(self.replicas))


def _refuse_leftover_pod(hop: _Hop) -> None:
    """0. An upgrade pod left from an earlier run may still be working on the
    disk, or holds the only account of what happened to it: never delete it."""
    leftover = _pod_phase(hop.kube, hop.pod)
    if leftover is None:
        return
    phase = leftover.get("phase") or "no phase"
    if phase in ("Succeeded", "Failed"):
        _, log, _ = hop.kube.run(["logs", hop.pod])
        raise _StepFailed(
            "pod {0} is left from an earlier run ({1}); nothing was done".format(hop.pod, phase),
            [l for l in log.splitlines() if l.strip()][-40:]
            + ["read its log above, then delete it yourself ("
               + _kubectl(hop.opts, "delete", "pod", hop.pod) + ") and plan again from the "
               "cluster's state as it is now"])
    raise _StepFailed(
        "pod {0} already exists and is {1}: a pg_upgrade may be running on ordinal 0's "
        "disk; nothing was done".format(hop.pod, phase),
        ["watch it: " + _kubectl(hop.opts, "logs", "-f", hop.pod),
         "do not start the cluster while it runs"])


def _read_old_cluster(hop: _Hop) -> str:
    """1a. What has to be read while the old cluster still runs: Patroni's
    scope and objects (deleted by name later, when no pod can say), the
    locale initdb must copy, and the system identifier. Returns the primary."""
    cluster, opts = hop.cluster, hop.opts
    primary = cluster.primary()
    if primary is None:
        raise _StepFailed("no ready primary")
    hop.scope = _patroni_scope(hop.kube, primary, opts.container)
    hop.dcs, others = _dcs_objects(hop.kube, opts, hop.scope)
    if others:
        raise _StepFailed(
            "the pod selector {0} also matches {1}, which are not Patroni's objects for scope "
            "{2}; apply deletes Patroni's state by name and will not guess. Nothing was "
            "done".format(opts.selector, ", ".join(others), hop.scope))
    hop.locale = _old_locale(cluster, primary, hop.step.from_pg)
    hop.output.append("old cluster's template0: {0}".format(
        ", ".join("{0}={1}".format(k.lower(), v) for k, v in hop.locale.items() if v)))
    _, out, _ = cluster.psql(primary, opts.admin_db, "SELECT system_identifier FROM pg_control_system()")
    hop.system_identifier = out.strip()
    hop.output.append("before: primary {0}, system identifier {1}".format(primary, hop.system_identifier))
    return primary


def _move_leader(hop: _Hop, primary: str) -> None:
    """1b. The data has to be on ordinal 0's disk: the StatefulSet is
    OrderedReady, so ordinal 0 starts first and alone."""
    if primary == hop.first:
        return
    rc, out, err = hop.kube.exec(primary, hop.opts.container, [
        "patronictl", "switchover", "--leader", primary, "--candidate", hop.first, "--force"])
    if rc != 0:
        raise _StepFailed("patronictl switchover to {0} failed".format(hop.first), [err or out])
    hop.cluster.wait_for("{0} to become the primary".format(hop.first),
                         lambda: hop.cluster.primary() == hop.first)
    hop.output.append("switched the primary over to {0}".format(hop.first))


def _stop(hop: _Hop) -> None:
    """2. Stop every instance, cleanly."""
    rc, out, err = hop.kube.run(["scale", "statefulset/" + hop.opts.statefulset, "--replicas=0"])
    if rc != 0:
        raise _StepFailed("kubectl scale to 0 failed", [err or out])
    hop.state[:] = ["the cluster is stopped and its data untouched; to start it again on "
                    "PostgreSQL {0}: {1}".format(hop.step.from_pg, hop.scale_command())]
    hop.cluster.wait_for("every instance to stop", lambda: not hop.cluster.pods())
    hop.output.append("scaled statefulset/{0} to 0".format(hop.opts.statefulset))


def _finish_list(hop: _Hop) -> List[str]:
    """The commands that finish the upgrade by hand. There is no listing
    again by hand, so it names the failover object a switchover may have
    left, in the kind Patroni uses here."""
    kinds = {d.split("/", 1)[0] for d in hop.dcs}
    return _finish_by_hand(hop.opts, _replica_pvcs(hop.sts, hop.opts, hop.replicas), hop.replicas,
                           hop.run_image, sorted(
        set(hop.dcs) | {"{0}/{1}-failover".format(k, hop.scope) for k in kinds}))


def _run_pg_upgrade(hop: _Hop) -> None:
    """3. pg_upgrade on ordinal 0's disk."""
    step, opts, kube = hop.step, hop.opts, hop.kube
    old, new = step.from_pg, step.to_pg
    rc, out, err = _run_on_ordinal_0(kube, hop.sts, opts, hop.pod, hop.image, _PG_UPGRADE_SCRIPT, dict({
        "FROM": str(old), "TO": str(new),
        "PG_BIN_OLD": opts.pg_bin.format(old), "PG_BIN_NEW": opts.pg_bin.format(new),
        "DATA": opts.data_dir, "LINK": "--link" if step.link else "",
    }, **hop.locale))
    if rc != 0:
        raise _StepFailed("cannot start the pg_upgrade pod: " + (err or out))
    finish = _finish_list(hop)
    phase = _await_pg_upgrade(hop.cluster, hop.pod, finish)
    _, log, _ = kube.run(["logs", hop.pod])
    lines = [l for l in log.splitlines() if l.strip()]
    if phase != "Succeeded":
        hop.state[:] = (_after_failed_upgrade(lines, opts, old, hop.replicas, hop.claim,
                                              hop.cluster.wal) or hop.state)
        raise _StepFailed("pg_upgrade {0} -> {1} failed in {2}".format(old, new, hop.image),
                          lines[-40:])
    # pg_upgrade's own word on copy or link sits mid-log, above the tail.
    hop.output.extend([l for l in lines[:-12] if "user relation files" in l] + lines[-12:])
    hop.output.append("backups: " + BACKUPS_AT_A_MAJOR_UPGRADE)
    kube.run(["delete", "pod", hop.pod, "--wait=false"])
    hop.state[:] = ["ordinal 0's data directory is upgraded to PostgreSQL {0} and the cluster is "
                    "stopped; do not start it on PostgreSQL {1}'s image. The old directory is "
                    "{2}.pg{1}{4}{3}. To finish by hand:".format(
                        new, old, opts.data_dir,
                        "; with --link it is reusable only while the new cluster has never "
                        "started, after renaming its global/pg_control.old back to "
                        "global/pg_control" if step.link else "",
                        ", its WAL " + hop.wal_kept if hop.wal_kept else "")] + [
                            "    " + l for l in finish]


def _reset_replicas_and_dcs(hop: _Hop) -> None:
    """4. The replicas' copies and Patroni's stored state belong to the old
    cluster, whose system identifier the upgraded one no longer has."""
    opts, kube = hop.opts, hop.kube
    for name in _replica_pvcs(hop.sts, opts, hop.replicas):
        rc, out, err = kube.run(["delete", "pvc", name, "--ignore-not-found", "--wait=true"])
        if rc != 0:
            raise _StepFailed("cannot delete {0}".format(name), [err or out])
        hop.output.append("deleted {0}".format(name))
    # Listed again: a switchover since (1b) leaves a <scope>-failover.
    dcs, others = _dcs_objects(kube, opts, hop.scope)
    if dcs:
        rc, out, err = kube.run(["delete"] + dcs + ["--ignore-not-found"])
        if rc != 0:
            raise _StepFailed("cannot reset Patroni's state", [err or out])
    hop.output.append("reset Patroni's state for scope {0}: {1}".format(
        hop.scope, ", ".join(dcs) or "nothing found"))
    if others:
        hop.output.append("left alone, not Patroni's: " + ", ".join(others))


def _start(hop: _Hop) -> None:
    """5. The new major's image, every instance back, on the new major."""
    opts, kube, cluster = hop.opts, hop.kube, hop.cluster
    new, run_image, replicas = hop.step.to_pg, hop.run_image, hop.replicas
    rc, out, err = kube.run(["set", "image", "statefulset/" + opts.statefulset,
                             "{0}={1}".format(opts.container, run_image)])
    if rc != 0:
        raise _StepFailed("kubectl set image failed", [err or out])
    rc, out, err = kube.run(["scale", "statefulset/" + opts.statefulset,
                             "--replicas={0}".format(replicas)])
    if rc != 0:
        raise _StepFailed("kubectl scale back to {0} failed".format(replicas), [err or out])
    hop.state[:] = ["the cluster was started on PostgreSQL {0} ({1}); the replicas re-clone from "
                    "ordinal 0. Check it with {2}".format(new, run_image,
                                                          _kubectl(opts, "get", "pods"))]

    def back() -> bool:
        pods = cluster.pods()
        return (len(pods) == replicas
                and all(cluster.ready(p) and cluster.pod_image(p) == run_image for p in pods)
                and cluster.primary() is not None)

    cluster.wait_for("{0} instance(s) ready on {1}".format(replicas, run_image), back)
    primary = cluster.primary()
    rc, out, _ = cluster.psql(primary, opts.admin_db,
                              "SELECT current_setting('server_version_num')::int / 10000, "
                              "system_identifier FROM pg_control_system()")
    major, _, sysid = out.strip().partition("|")
    if rc != 0 or major != str(new):
        raise _StepFailed("the primary runs PostgreSQL {0}, not {1}".format(major or "?", new))
    hop.output.append("after: primary {0} on PostgreSQL {1}, system identifier {2}".format(
        primary, major, sysid))


def _major_upgrade(step: MajorUpgrade, cluster: _Cluster, output: List[str],
                   state: List[str]) -> _Outcome:
    sts = cluster.kube.json(["get", "statefulset", cluster.opts.statefulset])
    hop = _Hop(step, cluster, sts, output, state)
    _volume(sts, cluster.opts)  # no data volume: fail here, before anything
    output.append("data directory: {0} ({1})".format(cluster.opts.data_dir, cluster.data_how))
    if cluster.wal:
        output.append("WAL directory: {0} (pg_wal is a symlink); the new cluster's goes beside it "
                      "and the old one is kept as {1}".format(cluster.wal, hop.wal_kept))
    _refuse_leftover_pod(hop)
    _move_leader(hop, _read_old_cluster(hop))
    _stop(hop)
    _run_pg_upgrade(hop)
    _reset_replicas_and_dcs(hop)
    _start(hop)
    opts = cluster.opts
    return _Outcome("PostgreSQL {0} -> {1}; {2} instance(s) on {3}".format(
        step.from_pg, step.to_pg, hop.replicas, hop.run_image), output, detail={
            "from_pg": step.from_pg, "to_pg": step.to_pg, "link": step.link,
            "old_image": hop.old_image, "run_image": hop.run_image,
            "data": opts.data_dir, "data_from": cluster.data_how,
            "kept": "{0}.pg{1}".format(opts.data_dir, step.from_pg),
            "wal": cluster.wal, "wal_kept": hop.wal_kept,
            "replicas": hop.replicas, "claim": hop.claim, "claims": _claims(sts),
            "scope": hop.scope,
            "system_identifier_before": hop.system_identifier,
        })


def _step_analyze(cluster: _Cluster, databases: Sequence[str]) -> _Outcome:
    primary = cluster.primary()
    if primary is None:
        raise _StepFailed("no ready primary to analyze on")
    # Statistics for a large database take longer than one kubectl call is
    # normally allowed; this one gets the rollout timeout.
    kube = cluster.kube
    if isinstance(kube, Kube):
        kube = Kube(kube.context, kube.namespace, cluster.opts.rollout_timeout)
    rc, out, err = kube.exec(primary, cluster.opts.container, [
        "vacuumdb", "-U", cluster.opts.psql_user, "--all", "--analyze-in-stages"])
    lines = [l for l in (out + "\n" + err).splitlines() if l.strip()]
    if rc != 0:
        raise _StepFailed("vacuumdb --analyze-in-stages failed", lines[-20:])
    return _Outcome("analyzed every database on {0}".format(primary), lines[-6:])



# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _final_state(cluster: _Cluster, databases: Sequence[str]) -> Dict[str, Any]:
    """What the cluster looks like afterwards -- the facts evidence needs."""
    state: Dict[str, Any] = {"instances": [], "databases": {}}
    try:
        pods = cluster.pods()
    except PreflightError as exc:
        state["error"] = str(exc)
        return state
    for pod in pods:
        name = pod["metadata"]["name"]
        rc, out, _ = cluster.psql(name, cluster.opts.admin_db, "SHOW server_version")
        state["instances"].append({
            "pod": name,
            "role": cluster.role(pod),
            "image": cluster.pod_image(pod),
            "image_id": cluster.pod_image_id(pod),
            "server_version": out.strip().split()[0] if rc == 0 and out.strip() else None,
        })
    primary = cluster.primary()
    for db in databases:
        if primary is None:
            state["databases"][db] = None
            continue
        rc, out, _ = cluster.psql(
            primary, db,
            "SELECT coalesce((SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'), '')",
        )
        state["databases"][db] = out.strip() if rc == 0 else None
    return state


def apply_plan(
    plan: Dict[str, Any],
    opts: ApplyOptions,
    kube: Optional[Kube] = None,
    preflight: Optional[Callable[[PreflightOptions], Sequence[Any]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Run the plan; return the report. Raises :class:`ApplyRefused` before
    any write when the plan or the cluster disqualifies it."""
    reasons = check_plan(plan, opts)
    if reasons:
        raise ApplyRefused(reasons)

    kube = kube or Kube(opts.context, opts.namespace, opts.timeout)
    instances, found = discover_instances(
        kube,
        PreflightOptions(
            target_image=plan.get("target", {}).get("image", ""),
            context=opts.context, namespace=opts.namespace, selector=opts.selector,
            container=opts.container, psql_user=opts.psql_user, admin_db=opts.admin_db,
            timeout=opts.timeout,
        ),
    )
    if found.status == FAIL:
        raise ApplyRefused(["cannot read the cluster: {0}".format(found.summary)] + found.details)
    reasons, databases = check_source(plan, instances, opts)
    reasons += check_statefulset(kube, opts)
    steps, _ = parse_steps(plan["steps"])  # check_plan has already refused a bad one
    data_how, wal = "", None
    if any(isinstance(s, MajorUpgrade) for s in steps) and not reasons:
        sts = kube.json(["get", "statefulset", opts.statefulset])
        found_reasons, data_dir, data_how = resolve_data_dir(instances, sts, opts)
        reasons += found_reasons
        opts = replace(opts, data_dir=data_dir)
        if not reasons:
            primary = next(i.pod for i in instances if i.role == "primary" and not i.error)
            found_reasons, wal = check_major_layout(_Cluster(kube, opts, sleep, clock), sts, primary)
            reasons += found_reasons
    if reasons:
        raise ApplyRefused(reasons)

    cluster = _Cluster(kube, opts, sleep, clock, data_how, wal)
    image_before = _statefulset_image(kube, opts)
    _plan_cagg_migration(steps, plan, cluster, instances, databases, opts)
    preflight = preflight or run_preflight
    report: Dict[str, Any] = {
        "apply_format": APPLY_FORMAT,
        "mode": plan.get("mode"),
        "source": plan.get("source"),
        "target": plan.get("target"),
        "databases": list(databases),
        "image_registry": opts.image_registry,
        "started_at": _now(),
        "steps": [],
    }
    if data_how:
        report["data_directory"] = {"path": opts.data_dir, "from": data_how}
    if opts.discovered:
        report["discovered"] = list(opts.discovered)
    failure: Optional[Dict[str, Any]] = None

    run: Dict[str, Callable[[Any], _Outcome]] = {
        "preflight": lambda step: _step_preflight(
            step, opts, preflight, steps[steps.index(step) + 1:],
            not any(isinstance(s, Preflight) for s in steps[:steps.index(step)])),
        "ensure_image": lambda step: _step_ensure_image(step, cluster),
        "update_extension": lambda step: _step_update(step, cluster, databases),
        "cycle_sessions": lambda step: _step_cycle(cluster, databases),
        "verify_version": lambda step: _step_verify(step, cluster, databases),
        "major_upgrade": lambda step: _step_major_upgrade(step, cluster),
        "migrate_caggs": lambda step: _step_migrate_caggs(step, cluster, databases),
        "analyze": lambda step: _step_analyze(cluster, databases),
    }
    for step in steps:
        action, hop = step.action, step.hop
        try:
            outcome = run[action](step)
        except (_StepFailed, PreflightError) as exc:
            out = exc.output if isinstance(exc, _StepFailed) else []
            summary = exc.summary if isinstance(exc, _StepFailed) else str(exc)
            report["steps"].append(StepResult(hop, action, FAILED, summary, out).to_dict())
            failure = {"hop": hop, "action": action, "summary": summary, "error": out}
            break
        report["steps"].append(StepResult(hop, action, outcome.status, outcome.summary,
                                          outcome.output, outcome.detail).to_dict())

    report["finished_at"] = _now()
    report["result"] = "fail" if failure else "pass"
    if failure:
        report["failure"] = failure
    report["final"] = _final_state(cluster, databases)
    helm = _helm_section(kube, opts, image_before, any(isinstance(s, MajorUpgrade) for s in steps))
    if helm:
        report["helm"] = helm
    return report


def _statefulset_image(kube: Kube, opts: ApplyOptions) -> Optional[str]:
    try:
        sts = kube.json(["get", "statefulset", opts.statefulset])
    except PreflightError:
        return None
    template = sts.get("spec", {}).get("template", {}).get("spec", {})
    return next((c.get("image") for c in template.get("containers") or []
                 if c.get("name") == opts.container), None)


def _helm_section(kube: Kube, opts: ApplyOptions, before: Optional[str],
                  major: bool) -> Optional[Dict[str, Any]]:
    """When Helm owns the StatefulSet and apply changed its image: what to put
    in the chart's values before the next ``helm upgrade`` (R28)."""
    try:
        sts = kube.json(["get", "statefulset", opts.statefulset])
    except PreflightError:
        return None
    release = helm_owner(sts)
    after = _statefulset_image(kube, opts)
    if release is None or not after or after == before:
        return None
    return {"release": release, "image": after,
            "note": values_note(after, release, major=major, statefulset=opts.statefulset)}


def render_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    if report.get("mode") == VERIFICATION:
        lines.append("VERIFICATION RUN -- applied without an established verdict")
    src, tgt = report.get("source") or {}, report.get("target") or {}
    lines.append("source    : pg{0} / ts{1}".format(src.get("pg"), src.get("ts")))
    lines.append("target    : pg{0} / ts{1}  ({2})".format(tgt.get("pg"), tgt.get("ts"), tgt.get("image")))
    lines.append("databases : {0}".format(", ".join(report.get("databases") or []) or "none"))
    lines.extend(report.get("discovered") or [])
    data = report.get("data_directory")
    if data:
        lines.append("data dir  : {0} ({1})".format(data.get("path"), data.get("from")))
    lines.append("")
    mark = {DONE: " ok ", SKIPPED: "skip", FAILED: "FAIL"}
    for step in report.get("steps", []):
        lines.append("[{0}] hop {1} {2}: {3}".format(
            mark.get(step["status"], "????"), step["hop"], step["action"], step["summary"]))
        if step["status"] == FAILED:
            lines.extend("         " + line for line in step["output"])
    lines.append("")
    final = report.get("final") or {}
    for inst in final.get("instances", []):
        lines.append("{0} [{1}] PostgreSQL {2} on {3}".format(
            inst.get("pod"), inst.get("role"), inst.get("server_version"), inst.get("image")))
    for db, version in sorted((final.get("databases") or {}).items()):
        lines.append("{0}: timescaledb {1}".format(db, version))
    if report.get("helm"):
        lines.append("")
        lines.extend(report["helm"]["note"])
    lines.append("")
    lines.append("result    : {0}".format(report.get("result", "?").upper()))
    return "\n".join(lines)
