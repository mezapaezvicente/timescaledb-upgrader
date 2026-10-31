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
* a digest in the plan is not a ``sha256:`` digest.

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
   state and the replicas' copies belong to a cluster that no longer exists;
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
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .plan import PLAN_FORMAT, VERIFICATION, pinned
from .preflight import (
    FAIL,
    Instance,
    Kube,
    PreflightError,
    PreflightOptions,
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

DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"

_UPDATE_SQL = "ALTER EXTENSION timescaledb UPDATE TO '{0}';"
_EXACT_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_RELEASE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ApplyRefused(Exception):
    """Nothing was written: the plan, the options or the cluster disqualify it."""

    def __init__(self, reasons: Sequence[str]) -> None:
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
    # Where Patroni keeps the data directory, and where each major's binaries
    # are; both as the timescale/timescaledb-ha images lay them out.
    data_dir: str = "/home/postgres/pgdata/data"
    pg_bin: str = "/usr/lib/postgresql/{0}/bin"
    registry_timeout: float = 15.0
    poll_interval: float = 2.0


@dataclass
class StepResult:
    hop: int
    action: str
    status: str
    summary: str
    output: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hop": self.hop,
            "action": self.action,
            "status": self.status,
            "summary": self.summary,
            "output": list(self.output),
        }


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
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return reasons + ["plan.steps is not a list"]
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            reasons.append("steps[{0}] is not an object".format(i))
            continue
        action = step.get("action")
        for key in ("digest", "run_digest"):
            if step.get(key) is not None and not _DIGEST.match(str(step[key])):
                reasons.append("steps[{0}]: {1} is not a sha256 digest: {2!r}".format(
                    i, key, step[key]))
        if action not in _ACTIONS:
            reasons.append("steps[{0}]: unknown action {1!r}".format(i, action))
        elif action == "update_extension":
            to = str(step.get("to") or "")
            if not _EXACT_VERSION.match(to):
                reasons.append("steps[{0}]: 'to' is not a version: {1!r}".format(i, to))
            elif step.get("sql") != _UPDATE_SQL.format(to):
                reasons.append(
                    "steps[{0}]: sql is not the one statement its 'to' implies "
                    "({1})".format(i, _UPDATE_SQL.format(to))
                )
        elif action in ("preflight", "ensure_image") and not step.get("image"):
            reasons.append("steps[{0}]: {1} needs an image".format(i, action))
        elif action == "preflight" and step.get("pg_upgrade") is not None and not isinstance(
                (step["pg_upgrade"] or {}).get("link"), bool):
            reasons.append("steps[{0}]: preflight's pg_upgrade needs link true or false".format(i))
        elif action == "verify_version" and not step.get("expect"):
            reasons.append("steps[{0}]: verify_version needs expect".format(i))
        elif action == "major_upgrade":
            if not step.get("image") or not step.get("run_image"):
                reasons.append("steps[{0}]: major_upgrade needs image and run_image".format(i))
            if not (isinstance(step.get("from_pg"), int) and isinstance(step.get("to_pg"), int)
                    and step["from_pg"] < step["to_pg"]):
                reasons.append("steps[{0}]: major_upgrade needs from_pg below to_pg".format(i))
            if not _RELEASE.match(str(step.get("ts") or "")):
                reasons.append("steps[{0}]: major_upgrade 'ts' is not a version".format(i))
            if not isinstance(step.get("link"), bool):
                reasons.append("steps[{0}]: major_upgrade needs link true or false".format(i))
    return reasons


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
    ) -> None:
        self.kube = kube
        self.opts = opts
        self.sleep = sleep
        self.clock = clock

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


def _step_preflight(
    step: Dict[str, Any],
    opts: ApplyOptions,
    preflight: Callable[[PreflightOptions], Sequence[Any]],
) -> Tuple[str, List[str]]:
    results = preflight(
        PreflightOptions(
            target_image=step["image"],
            context=opts.context,
            namespace=opts.namespace,
            selector=opts.selector,
            container=opts.container,
            psql_user=opts.psql_user,
            admin_db=opts.admin_db,
            target_pg=step.get("target_pg"),
            target_ts=step.get("target_ts"),
            matrix_path=opts.matrix_path,
            timeout=opts.timeout,
            registry_timeout=opts.registry_timeout,
            pg_upgrade_link=(step.get("pg_upgrade") or {}).get("link"),
        )
    )
    lines = ["{0}: {1} - {2}".format(r.name, r.status, r.summary) for r in results]
    failed = [r for r in results if r.status == FAIL]
    if failed:
        detail = []
        for r in failed:
            detail.append("{0}: {1}".format(r.name, r.summary))
            detail.extend("  " + d for d in r.details)
        raise _StepFailed(
            "preflight failed: {0}".format(", ".join(r.name for r in failed)), detail
        )
    return "{0} checks, none failed".format(len(results)), lines


def _step_ensure_image(step: Dict[str, Any], cluster: _Cluster) -> Tuple[str, List[str]]:
    image = pinned(step["image"], step.get("digest"))
    opts = cluster.opts
    sts = cluster.kube.json(["get", "statefulset", opts.statefulset])
    containers = sts.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    template_image = next(
        (c.get("image") for c in containers if c.get("name") == opts.container), None
    )
    pods = cluster.pods()
    stale = [p for p in pods if cluster.pod_image(p) != image]
    if template_image == image and not stale:
        return "already running {0}".format(image), []

    output: List[str] = []
    try:
        _roll(cluster, image, template_image, stale, output)
    except (_StepFailed, PreflightError) as exc:
        # Keep what was already done: a half-finished rollout is the state
        # the operator has to act on.
        summary = exc.summary if isinstance(exc, _StepFailed) else str(exc)
        extra = exc.output if isinstance(exc, _StepFailed) else []
        raise _StepFailed(summary, output + extra) from exc
    return "rolled {0} pod(s) onto {1}".format(len(stale), image), output


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
    step: Dict[str, Any], cluster: _Cluster, databases: Sequence[str]
) -> Tuple[str, List[str]]:
    primary = cluster.primary()
    if primary is None:
        raise _StepFailed("no ready primary to run ALTER EXTENSION on")
    output: List[str] = []
    for db in databases:
        rc, out, err = cluster.psql(primary, db, step["sql"])
        if rc != 0:
            done = ", ".join(output) or "none"
            raise _StepFailed(
                "ALTER EXTENSION failed in database {0} (already updated: {1})".format(db, done),
                [line for line in (err or out).splitlines() if line.strip()],
            )
        output.append(db)
    return "updated {0} database(s) to {1}".format(len(databases), step["to"]), [
        "{0}: {1}".format(db, step["sql"]) for db in output
    ]


def _step_cycle(cluster: _Cluster, databases: Sequence[str]) -> Tuple[str, List[str]]:
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
    return "sessions cycled on {0} instance(s)".format(len(output)), output


def _step_verify(
    step: Dict[str, Any], cluster: _Cluster, databases: Sequence[str]
) -> Tuple[str, List[str]]:
    expect = step["expect"]
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
    return "{0} database(s) x {1} instance(s) report {2}".format(len(databases), len(pods), expect), [
        "{0}/{1}: {2}".format(p, d, v) for (p, d), v in sorted(seen.items())
    ]


# Runs in the pg_upgrade pod, as the StatefulSet's user, on ordinal 0's disk.
# set -e and an explicit message per stage: the pod's log is the step's output.
_PG_UPGRADE_SCRIPT = r"""
set -euo pipefail
old_bin="$PG_BIN_OLD"; new_bin="$PG_BIN_NEW"; data="$DATA"
new="$data.new"; keep="$data.pg$FROM"
cd "$(dirname "$data")"
# On failure, what was changed: apply reads these lines to say how to go back.
trap 'rc=$?
if [ "$rc" -ne 0 ]; then
  for d in "$data" "$keep"; do
    [ -e "$d/global/pg_control.old" ] && echo "old cluster disabled: $d/global/pg_control.old"
  done
  [ -d "$keep" ] && [ ! -d "$data" ] && echo "old data directory moved: $keep"
fi
exit "$rc"' EXIT
for b in "$old_bin" "$new_bin"; do
  [ -x "$b/pg_upgrade" ] || [ -x "$b/postgres" ] || { echo "this image has no $b"; exit 2; }
done
[ -e "$keep" ] && { echo "$keep already exists; refusing to overwrite it"; exit 2; }
rm -rf "$new"
sums=$("$old_bin/pg_controldata" "$data" | sed -n 's/^Data page checksum version: *//p')
if [ "$sums" = 0 ]; then
  opt=$("$new_bin/initdb" --help | grep -q -- --no-data-checksums && echo --no-data-checksums || true)
else
  opt=--data-checksums
fi
echo "old cluster: PostgreSQL $FROM, data checksums ${sums:-unknown}; initdb $opt"
"$new_bin/initdb" $opt -U postgres -E UTF8 -D "$new" >/dev/null
echo "shared_preload_libraries = 'timescaledb'" >> "$new/postgresql.conf"
report() { echo "pg_upgrade $1 failed:"; tail -n 40 "$new"/pg_upgrade_output.d/*/*.txt "$new"/pg_upgrade_output.d/*/log/* 2>/dev/null || true; }
"$new_bin/pg_upgrade" -b "$old_bin" -B "$new_bin" -d "$data" -D "$new" -U postgres $LINK --check   || { report --check; exit 1; }
"$new_bin/pg_upgrade" -b "$old_bin" -B "$new_bin" -d "$data" -D "$new" -U postgres $LINK   || { report ""; exit 1; }
mv "$data" "$keep"
mv "$new" "$data"
# pg_upgrade's suggested script updates timescaledb straight to this image's
# default release, past any stop the plan makes; the plan's updates replace it.
if [ -f update_extensions.sql ]; then
  echo "not run, removed: pg_upgrade's update_extensions.sql:"; sed 's/^/    /' update_extensions.sql
  rm -f update_extensions.sql
fi
# Proof of the mode: with --link the new cluster's relation files are hard
# links into the old directory; a copy has none.
echo "relation files sharing an inode with $keep: $(find "$data/base" -type f -links +1 | wc -l)"
echo "upgraded to PostgreSQL $TO; the old data directory is $keep"
echo "old data directories on this disk, to remove once the result is accepted: $(ls -d "$data".pg* | tr '\n' ' ')"
"""


def _step_major_upgrade(step: Dict[str, Any], cluster: _Cluster) -> Tuple[str, List[str]]:
    opts = cluster.opts
    kube = cluster.kube
    output: List[str] = []
    # What a failure from here on leaves behind and how to go on; replaced as
    # the upgrade moves through its stages, and attached to every failure.
    state: List[str] = []
    try:
        return _major_upgrade(step, cluster, kube, opts, output, state)
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


def _finish_by_hand(opts: ApplyOptions, claim: str, replicas: int, run_image: str) -> List[str]:
    """What apply does after a successful pg_upgrade, as commands."""
    lines = [_kubectl(opts, "delete", "pvc", "{0}-{1}-{2}".format(claim, opts.statefulset, i))
             for i in range(1, replicas)]
    return lines + [
        _kubectl(opts, "delete", "endpoints,configmaps", "-l", opts.selector),
        _kubectl(opts, "set", "image", "statefulset/" + opts.statefulset,
                 "{0}={1}".format(opts.container, run_image)),
        _kubectl(opts, "scale", "statefulset/" + opts.statefulset, "--replicas={0}".format(replicas)),
    ]


def _after_failed_upgrade(lines: Sequence[str], opts: ApplyOptions, old: int, replicas: int,
                          claim: str) -> List[str]:
    """What a failed pg_upgrade left, from the script's exit lines; empty when
    the old cluster is as it was (a copy, or --link before linking began)."""
    disabled = [l.split(": ", 1)[1] for l in lines if l.startswith("old cluster disabled: ")]
    moved = [l.split(": ", 1)[1] for l in lines if l.startswith("old data directory moved: ")]
    if not disabled and not moved:
        return []
    disk = "{0}-{1}-0".format(claim, opts.statefulset)
    notes = ["the cluster is stopped, and PostgreSQL {0} will not start on ordinal 0's data "
             "as it is now:".format(old)]
    if moved:
        notes.append("    the old data directory was moved to {0}; move it back to {1}".format(
            moved[0], opts.data_dir))
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


def _major_upgrade(step, cluster, kube, opts, output, state):
    old, new = step["from_pg"], step["to_pg"]
    image = pinned(step["image"], step.get("digest"))
    run_image = pinned(step["run_image"], step.get("run_digest"))
    sts = kube.json(["get", "statefulset", opts.statefulset])
    replicas = int(sts.get("spec", {}).get("replicas") or 1)
    template = sts.get("spec", {}).get("template", {}).get("spec", {})
    claims = sts.get("spec", {}).get("volumeClaimTemplates") or []
    if not claims:
        raise _StepFailed("statefulset/{0} has no volumeClaimTemplates".format(opts.statefulset))
    claim = claims[0]["metadata"]["name"]
    container = next((c for c in template.get("containers") or []
                      if c.get("name") == opts.container), {})
    mount = next((m["mountPath"] for m in container.get("volumeMounts") or []
                  if m.get("name") == claim), None)
    if mount is None:
        raise _StepFailed("container {0} does not mount {1}".format(opts.container, claim))
    first = "{0}-0".format(opts.statefulset)
    pod = "{0}-pg-upgrade-{1}-{2}".format(opts.statefulset, old, new)

    # 0. An upgrade pod left from an earlier run may still be working on the
    # disk, or holds the only account of what happened to it: never delete it.
    leftover = _pod_phase(kube, pod)
    if leftover is not None:
        phase = leftover.get("phase") or "no phase"
        if phase in ("Succeeded", "Failed"):
            _, log, _ = kube.run(["logs", pod])
            raise _StepFailed(
                "pod {0} is left from an earlier run ({1}); nothing was done".format(pod, phase),
                [l for l in log.splitlines() if l.strip()][-40:]
                + ["read its log above, then delete it yourself ("
                   + _kubectl(opts, "delete", "pod", pod) + ") and plan again from the "
                   "cluster's state as it is now"])
        raise _StepFailed(
            "pod {0} already exists and is {1}: a pg_upgrade may be running on ordinal 0's "
            "disk; nothing was done".format(pod, phase),
            ["watch it: " + _kubectl(opts, "logs", "-f", pod),
             "do not start the cluster while it runs"])

    # 1. The data has to be on ordinal 0's disk.
    primary = cluster.primary()
    if primary is None:
        raise _StepFailed("no ready primary")
    rc, out, _ = cluster.psql(primary, opts.admin_db,
                              "SELECT system_identifier FROM pg_control_system()")
    output.append("before: primary {0}, system identifier {1}".format(primary, out.strip()))
    if primary != first:
        rc, out, err = kube.exec(primary, opts.container, [
            "patronictl", "switchover", "--leader", primary, "--candidate", first, "--force"])
        if rc != 0:
            raise _StepFailed("patronictl switchover to {0} failed".format(first), [err or out])
        cluster.wait_for("{0} to become the primary".format(first),
                         lambda: cluster.primary() == first)
        output.append("switched the primary over to {0}".format(first))

    # 2. Stop everything, cleanly.
    rc, out, err = kube.run(["scale", "statefulset/" + opts.statefulset, "--replicas=0"])
    if rc != 0:
        raise _StepFailed("kubectl scale to 0 failed", [err or out])
    state[:] = ["the cluster is stopped and its data untouched; to start it again on "
                "PostgreSQL {0}: {1}".format(old, _kubectl(
                    opts, "scale", "statefulset/" + opts.statefulset,
                    "--replicas={0}".format(replicas)))]
    cluster.wait_for("every instance to stop", lambda: not cluster.pods())
    output.append("scaled statefulset/{0} to 0".format(opts.statefulset))

    # 3. pg_upgrade on ordinal 0's disk, scheduled as the database pods are:
    # where they may run (taints, selectors, affinity), with their pull
    # secrets, account and resources. Without these it can sit Pending on a
    # cluster with dedicated database nodes or a private registry.
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
                "command": ["bash", "-c", _PG_UPGRADE_SCRIPT],
                "env": [
                    {"name": "FROM", "value": str(old)},
                    {"name": "TO", "value": str(new)},
                    {"name": "PG_BIN_OLD", "value": opts.pg_bin.format(old)},
                    {"name": "PG_BIN_NEW", "value": opts.pg_bin.format(new)},
                    {"name": "DATA", "value": opts.data_dir},
                    {"name": "LINK", "value": "--link" if step["link"] else ""},
                ],
                "volumeMounts": [{"name": claim, "mountPath": mount}],
            })],
            "volumes": [{"name": claim, "persistentVolumeClaim": {
                "claimName": "{0}-{1}".format(claim, first)}}],
        }),
    }
    rc, out, err = kube.run(["run", pod, "--image", image, "--restart=Never",
                             "--labels", "app.kubernetes.io/part-of=timescaledb-upgrader",
                             "--overrides", json.dumps(spec)])
    if rc != 0:
        raise _StepFailed("cannot start the pg_upgrade pod: " + (err or out))

    finish = _finish_by_hand(opts, claim, replicas, run_image)
    phase = _await_pg_upgrade(cluster, pod, finish)
    _, log, _ = kube.run(["logs", pod])
    lines = [l for l in log.splitlines() if l.strip()]
    if phase != "Succeeded":
        state[:] = _after_failed_upgrade(lines, opts, old, replicas, claim) or state
        raise _StepFailed("pg_upgrade {0} -> {1} failed in {2}".format(old, new, image),
                          lines[-40:])
    # pg_upgrade's own word on copy or link sits mid-log, above the tail.
    output.extend([l for l in lines[:-12] if "user relation files" in l] + lines[-12:])
    kube.run(["delete", "pod", pod, "--wait=false"])
    state[:] = ["ordinal 0's data directory is upgraded to PostgreSQL {0} and the cluster is "
                "stopped; do not start it on PostgreSQL {1}'s image. The old directory is "
                "{2}.pg{1}{3}. To finish by hand:".format(
                    new, old, opts.data_dir,
                    "; with --link it is reusable only while the new cluster has never started, "
                    "after renaming its global/pg_control.old back to global/pg_control"
                    if step["link"] else "")] + ["    " + l for l in finish]

    # 4. The replicas' copies and Patroni's stored state belong to the old cluster.
    for i in range(1, replicas):
        name = "{0}-{1}-{2}".format(claim, opts.statefulset, i)
        rc, out, err = kube.run(["delete", "pvc", name, "--ignore-not-found", "--wait=true"])
        if rc != 0:
            raise _StepFailed("cannot delete {0}".format(name), [err or out])
        output.append("deleted {0}".format(name))
    rc, out, err = kube.run(["delete", "endpoints,configmaps", "-l", opts.selector,
                             "--ignore-not-found"])
    if rc != 0:
        raise _StepFailed("cannot reset Patroni's state", [err or out])
    output.append("reset Patroni's state: " + (" ".join(out.split()) or "nothing found"))

    # 5. The new major's image, every instance back.
    rc, out, err = kube.run(["set", "image", "statefulset/" + opts.statefulset,
                             "{0}={1}".format(opts.container, run_image)])
    if rc != 0:
        raise _StepFailed("kubectl set image failed", [err or out])
    rc, out, err = kube.run(["scale", "statefulset/" + opts.statefulset,
                             "--replicas={0}".format(replicas)])
    if rc != 0:
        raise _StepFailed("kubectl scale back to {0} failed".format(replicas), [err or out])
    state[:] = ["the cluster was started on PostgreSQL {0} ({1}); the replicas re-clone from "
                "ordinal 0. Check it with {2}".format(new, run_image, _kubectl(opts, "get", "pods"))]

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
    output.append("after: primary {0} on PostgreSQL {1}, system identifier {2}".format(
        primary, major, sysid))
    return "PostgreSQL {0} -> {1}; {2} instance(s) on {3}".format(old, new, replicas, run_image), output


def _step_analyze(cluster: _Cluster, databases: Sequence[str]) -> Tuple[str, List[str]]:
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
    return "analyzed every database on {0}".format(primary), lines[-6:]


_ACTIONS = ("preflight", "ensure_image", "update_extension", "cycle_sessions", "verify_version",
            "major_upgrade", "analyze")


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
    if reasons:
        raise ApplyRefused(reasons)

    cluster = _Cluster(kube, opts, sleep, clock)
    preflight = preflight or run_preflight
    report: Dict[str, Any] = {
        "apply_format": APPLY_FORMAT,
        "mode": plan.get("mode"),
        "source": plan.get("source"),
        "target": plan.get("target"),
        "databases": list(databases),
        "started_at": _now(),
        "steps": [],
    }
    failure: Optional[Dict[str, Any]] = None

    for step in plan["steps"]:
        action, hop = step["action"], step.get("hop", 0)
        try:
            if action == "preflight":
                summary, output = _step_preflight(step, opts, preflight)
            elif action == "ensure_image":
                summary, output = _step_ensure_image(step, cluster)
            elif action == "update_extension":
                summary, output = _step_update(step, cluster, databases)
            elif action == "cycle_sessions":
                summary, output = _step_cycle(cluster, databases)
            elif action == "major_upgrade":
                summary, output = _step_major_upgrade(step, cluster)
            elif action == "analyze":
                summary, output = _step_analyze(cluster, databases)
            else:
                summary, output = _step_verify(step, cluster, databases)
        except (_StepFailed, PreflightError) as exc:
            out = exc.output if isinstance(exc, _StepFailed) else []
            summary = exc.summary if isinstance(exc, _StepFailed) else str(exc)
            report["steps"].append(StepResult(hop, action, FAILED, summary, out).to_dict())
            failure = {"hop": hop, "action": action, "summary": summary, "error": out}
            break
        status = SKIPPED if summary.startswith("already running") else DONE
        report["steps"].append(StepResult(hop, action, status, summary, output).to_dict())

    report["finished_at"] = _now()
    report["result"] = "fail" if failure else "pass"
    if failure:
        report["failure"] = failure
    report["final"] = _final_state(cluster, databases)
    return report


def render_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    if report.get("mode") == VERIFICATION:
        lines.append("VERIFICATION RUN -- applied without an established verdict")
    src, tgt = report.get("source") or {}, report.get("target") or {}
    lines.append("source    : pg{0} / ts{1}".format(src.get("pg"), src.get("ts")))
    lines.append("target    : pg{0} / ts{1}  ({2})".format(tgt.get("pg"), tgt.get("ts"), tgt.get("image")))
    lines.append("databases : {0}".format(", ".join(report.get("databases") or []) or "none"))
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
    lines.append("")
    lines.append("result    : {0}".format(report.get("result", "?").upper()))
    return "\n".join(lines)
