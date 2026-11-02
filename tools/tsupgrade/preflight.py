"""Read-only preflight checks against a live TimescaleDB instance.

Everything in this module is read-only. Nothing here may mutate cluster or
database state, and that is enforced rather than merely intended:

* Only ``kubectl get``/``describe``/``exec`` are used, never ``apply``,
  ``patch``, ``delete`` or ``scale``.
* Every database connection is opened with
  ``PGOPTIONS=-c default_transaction_read_only=on``, so PostgreSQL itself
  rejects any statement that would write, no matter what the SQL says.
* Shell commands run inside pods are limited to ``df``, ``stat``, ``id`` and
  ``test``, none of which change anything.

Every check reports **pass**, **fail** or **unknown**. ``unknown`` is a real
answer and is never quietly upgraded to ``pass``: a check that could not be
carried out has not been passed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .images import (
    RegistryError,
    manifest_exists,
    node_image_name,
    parse_image_reference,
    parse_release_tag,
)
from .contents import ContentsError, load_contents
from .drift import AVAILABLE_VERSIONS_VIEW_WARNING
from .drift import INDETERMINATE as DRIFT_INDETERMINATE
from .drift import classify, installable_from_listings
from .gates import BLOCKED, CLEAR, NOT_APPLICABLE, SESSION_RULES
from .gates import INDETERMINATE as GATE_INDETERMINATE
from .gates import check_old_caggs, reaches_old_cagg_removal, run_gates
from .matrix import Matrix, MatrixError, load_matrix
from .support import (
    ABI_BROKEN,
    ABI_BROKEN_SOURCE,
    PG_MINOR_RULES_SOURCE,
    abi_break_for,
    pg_minor_refusal,
    pg_minor_rules_for,
)
from .versions import InvalidVersion, parse_version

__all__ = [
    "PASS",
    "FAIL",
    "UNKNOWN",
    "CheckResult",
    "PreflightError",
    "PreflightOptions",
    "run_preflight",
    "render_report",
    "worst_status",
    "check_abi_break",
    "check_pg_minor_floor",
    "check_drift",
    "check_gates",
    "check_later_gates",
    "check_route_images",
    "RouteImage",
    "installable_on_instance",
    "patroni_role",
]

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

# Read-only at the server, not just by convention.
_READ_ONLY_PGOPTIONS = "-c default_transaction_read_only=on"

# drift's vocabulary, named here so preflight does not import it wholesale.
ALIGNED_STATE = "aligned"


class PreflightError(Exception):
    """Raised when preflight cannot run at all (bad tooling, unreachable API)."""


@dataclass
class CheckResult:
    name: str
    title: str
    status: str
    summary: str
    details: List[str] = field(default_factory=list)
    # Something the operator should read even though the check does not stop.
    notice: Optional[str] = None


@dataclass(frozen=True)
class RouteImage:
    """An image a plan still has to run. ``on_statefulset``: it runs through
    the StatefulSet, under that pull policy. If not, it runs in apply's one-off
    pod, which is always ``IfNotPresent``."""

    reference: str
    on_statefulset: bool = True


@dataclass
class PreflightOptions:
    target_image: str
    context: Optional[str] = None
    namespace: str = "timescale"
    selector: str = "application=patroni"
    container: str = "timescaledb"
    psql_user: str = "postgres"
    admin_db: str = "postgres"
    dump_path: Optional[str] = None
    headroom_factor: float = 1.2
    target_pg: Optional[str] = None
    target_ts: Optional[str] = None
    matrix_path: Optional[str] = None
    timeout: float = 30.0
    registry_timeout: float = 15.0
    # Set before a pg_upgrade: True for --link, False for a copy. Adds the
    # check that ordinal 0's disk can hold what pg_upgrade writes there.
    pg_upgrade_link: Optional[bool] = None
    # Before a pg_upgrade: the image it runs in and the major it upgrades to,
    # whose measured contents the other extensions are checked against too.
    pg_upgrade_image: Optional[str] = None
    pg_upgrade_to: Optional[int] = None
    # Measured image contents (default: data/image-contents.json).
    contents_path: Optional[str] = None
    # From apply, at its first hop: every image the plan has to run, checked by
    # digest in place of the target image, and the StatefulSet that runs them.
    route_images: Tuple[RouteImage, ...] = ()
    # From apply, at a later hop: those were checked, and a digest does not change.
    route_checked: bool = False
    # The updates later in the plan, as ``(hop, release)``.
    later_updates: Tuple[Tuple[int, str], ...] = ()
    use_cached_images: bool = False
    statefulset: Optional[str] = None


# Older Patroni labels the leader `master` whatever the configuration asks
# for: 2.0.2 (the pg11 image) was seen doing it with leader_label_value set.
# Both mean the primary.
_LEADER_LABELS = ("primary", "master")


def patroni_role(labels: Optional[Dict[str, str]]) -> str:
    """The pod's Patroni role from its labels: primary, replica, or as labelled."""
    role = (labels or {}).get("role", "unknown")
    return "primary" if role in _LEADER_LABELS else role


@dataclass
class Instance:
    pod: str
    role: str = "unknown"
    node: str = ""
    server_version: Optional[str] = None
    in_recovery: Optional[bool] = None
    data_directory: Optional[str] = None
    total_size_bytes: Optional[int] = None
    extensions: Dict[str, Optional[str]] = field(default_factory=dict)
    available_ts: Optional[str] = None
    error: Optional[str] = None

    @property
    def ts_version(self) -> Optional[str]:
        """The installed TimescaleDB version, if every database agrees.

        Returns None when no database has it installed, and raises nothing when
        databases disagree -- that disagreement is surfaced as its own finding,
        because it changes what an upgrade has to do.
        """
        versions = {v for v in self.extensions.values() if v}
        if len(versions) == 1:
            return next(iter(versions))
        return None

    @property
    def ts_versions_disagree(self) -> bool:
        return len({v for v in self.extensions.values() if v}) > 1


# ---------------------------------------------------------------------------
# kubectl plumbing
# ---------------------------------------------------------------------------


class Kube:
    """A kubectl wrapper pinned to one context and namespace.

    Every invocation carries an explicit timeout. A cluster that stops
    answering must surface as a failed check, not as a hung command.
    """

    def __init__(self, context: Optional[str], namespace: str, timeout: float) -> None:
        if shutil.which("kubectl") is None:
            raise PreflightError("kubectl not found on PATH")
        self.context = context
        self.namespace = namespace
        self.timeout = timeout

    def _base(self) -> List[str]:
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        return cmd

    def run(self, args: Sequence[str], namespaced: bool = True) -> Tuple[int, str, str]:
        cmd = self._base()
        if namespaced:
            cmd += ["-n", self.namespace]
        cmd += list(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PreflightError(
                "kubectl timed out after {0}s: {1}".format(
                    self.timeout, " ".join(cmd)
                )
            ) from exc
        except OSError as exc:
            raise PreflightError("cannot run kubectl: {0}".format(exc)) from exc
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def json(self, args: Sequence[str], namespaced: bool = True) -> Any:
        rc, out, err = self.run(list(args) + ["-o", "json"], namespaced=namespaced)
        if rc != 0:
            raise PreflightError("kubectl failed: {0}".format(err or out))
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise PreflightError("kubectl returned non-JSON: {0}".format(exc)) from exc

    def exec(
        self, pod: str, container: str, argv: Sequence[str]
    ) -> Tuple[int, str, str]:
        return self.run(["exec", pod, "-c", container, "--"] + list(argv))

    def psql(
        self, pod: str, container: str, database: str, user: str, sql: str
    ) -> Tuple[int, str, str]:
        """Run one read-only query. The connection refuses writes server-side."""
        return self.exec(
            pod,
            container,
            [
                "env",
                "PGOPTIONS=" + _READ_ONLY_PGOPTIONS,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-X",
                "-qtAF",
                "|",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
        )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_context(kube: Kube, opts: PreflightOptions) -> CheckResult:
    """Echo back exactly which cluster and namespace we are pointed at."""
    details: List[str] = []

    rc, out, err = kube.run(["config", "current-context"], namespaced=False)
    current = out if rc == 0 and out else "(none)"
    effective = opts.context or current

    details.append("kubectl context : {0}".format(effective))
    if opts.context and opts.context != current:
        details.append(
            "note            : overriding the current context ({0})".format(current)
        )
    details.append("namespace       : {0}".format(opts.namespace))

    try:
        cluster = kube.json(["config", "view", "--minify"], namespaced=False)
        entries = cluster.get("clusters") or []
        if entries:
            details.append(
                "api server      : {0}".format(
                    entries[0].get("cluster", {}).get("server", "unknown")
                )
            )
    except PreflightError:
        details.append("api server      : unknown")

    rc, out, err = kube.run(["get", "namespace", opts.namespace, "-o", "name"], namespaced=False)
    if rc != 0:
        return CheckResult(
            "context",
            "Cluster and namespace",
            FAIL,
            "namespace {0} not reachable".format(opts.namespace),
            details + [(err or out)],
        )

    rc, out, _ = kube.run(["version", "-o", "json"], namespaced=False)
    if rc == 0:
        try:
            server = json.loads(out).get("serverVersion", {}).get("gitVersion")
            if server:
                details.append("kubernetes      : {0}".format(server))
        except json.JSONDecodeError:
            pass

    return CheckResult(
        "context",
        "Cluster and namespace",
        PASS,
        "pointed at {0} / {1}".format(effective, opts.namespace),
        details,
    )


def check_target_image(opts: PreflightOptions) -> Tuple[CheckResult, Optional[str], Optional[str]]:
    """Confirm the target image exists in its registry.

    Fails loudly on auth failure or timeout: a registry that will not answer is
    never recorded as a registry that has no such image.
    """
    details = ["image : {0}".format(opts.target_image)]

    target_pg, target_ts = opts.target_pg, opts.target_ts
    try:
        _, _, tag = parse_image_reference(opts.target_image)
        parsed = parse_release_tag(tag)
        if parsed:
            target_pg = target_pg or parsed["pg"]
            target_ts = target_ts or parsed["ts"]
            details.append(
                "tag implies : PostgreSQL {0}, TimescaleDB {1}".format(
                    parsed["pg"], parsed["ts"]
                )
            )
        else:
            details.append(
                "tag implies : nothing pinned; supply --target-pg/--target-ts"
            )
    except RegistryError as exc:
        return (
            CheckResult(
                "target_image",
                "Target image exists",
                FAIL,
                str(exc),
                details,
            ),
            target_pg,
            target_ts,
        )

    try:
        exists, detail = manifest_exists(
            opts.target_image, timeout=opts.registry_timeout
        )
    except RegistryError as exc:
        # Explicitly not "unknown": the instruction is to fail loudly here.
        return (
            CheckResult(
                "target_image",
                "Target image exists",
                FAIL,
                "registry did not answer: {0}".format(exc),
                details,
            ),
            target_pg,
            target_ts,
        )

    details.append("registry : {0}".format(detail))
    status = PASS if exists else FAIL
    summary = (
        "target image is published"
        if exists
        else "no such tag in the registry"
    )
    return (
        CheckResult("target_image", "Target image exists", status, summary, details),
        target_pg,
        target_ts,
    )


def _implied_versions(opts: PreflightOptions) -> Tuple[Optional[str], Optional[str]]:
    """The target's versions as given, else as its tag implies them."""
    try:
        parsed = parse_release_tag(parse_image_reference(opts.target_image)[2]) or {}
    except RegistryError:
        parsed = {}
    return opts.target_pg or parsed.get("pg"), opts.target_ts or parsed.get("ts")


def _why_cache_is_not_enough(kube: "Kube", opts: PreflightOptions,
                             images: Sequence[RouteImage]) -> List[str]:
    """Why these images could not run without their registry; empty if they
    could: on every node by digest, under a pull policy that will not pull."""
    try:
        nodes = kube.json(["get", "nodes"], namespaced=False).get("items") or []
    except PreflightError as exc:
        return ["cannot list the nodes: {0}".format(exc)]
    if not nodes:
        return ["no nodes are listed"]
    reasons: List[str] = []
    listed = {
        node.get("metadata", {}).get("name", "?"): {
            name for image in node.get("status", {}).get("images") or []
            for name in image.get("names") or []}
        for node in nodes}
    for image in images:
        wanted = node_image_name(image.reference)
        absent = sorted(name for name, names in listed.items() if wanted not in names)
        if absent:
            reasons.append("{0} is not on {1}".format(image.reference, ", ".join(absent)))
    if not any(image.on_statefulset for image in images):
        return reasons
    if not opts.statefulset:
        return reasons + ["no StatefulSet was named, so its pull policy is unknown"]
    try:
        sts = kube.json(["get", "statefulset", opts.statefulset])
    except PreflightError as exc:
        return reasons + ["cannot read statefulset/{0}: {1}".format(opts.statefulset, exc)]
    containers = sts.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    policy = next((c.get("imagePullPolicy") for c in containers if c.get("name") == opts.container), None)
    if policy == "Always":
        reasons.append(
            "statefulset/{0} pulls with imagePullPolicy Always, which needs the registry even "
            "for a cached image. To change it: kubectl -n {1} patch statefulset {0} -p "
            "'{{\"spec\":{{\"template\":{{\"spec\":{{\"containers\":[{{\"name\":\"{2}\","
            "\"imagePullPolicy\":\"IfNotPresent\"}}]}}}}}}}}'".format(
                opts.statefulset, opts.namespace, opts.container))
    elif policy not in ("IfNotPresent", "Never"):
        reasons.append("cannot read the pull policy of container {0} in statefulset/{1}".format(
            opts.container, opts.statefulset))
    return reasons


def check_route_images(kube: "Kube", opts: PreflightOptions) -> CheckResult:
    """Can every image the plan still has to run be run? Asked before anything
    stops, of each image by digest, not only the next hop's.

    A registry that says an image is not there always fails. One that does not
    answer also fails, and says why and what can be done about it, unless the
    operator chose ``--use-cached-images`` and the images can really run
    without it.
    """
    name, title = "route_images", "Images this plan still has to run"
    details: List[str] = []
    missing: List[RouteImage] = []
    silent: List[RouteImage] = []
    for image in opts.route_images:
        try:
            exists, answer = manifest_exists(image.reference, timeout=opts.registry_timeout)
        except RegistryError as exc:
            silent.append(image)
            details.append("{0} : registry did not answer: {1}".format(image.reference, exc))
            continue
        details.append("{0} : {1}".format(image.reference, answer))
        if not exists:
            missing.append(image)
    if missing:
        details.append("Nothing has been changed. If the images are in another registry, name "
                       "it with --image-registry <host/path>; it must hold these digests, which "
                       "a copy of every platform keeps (skopeo copy --all).")
        return CheckResult(name, title, FAIL, "the registry does not have {0}".format(
            ", ".join(i.reference for i in missing)), details)
    if not silent:
        return CheckResult(name, title, PASS, "{0} image(s), all in their registry".format(
            len(opts.route_images)), details)

    reasons = _why_cache_is_not_enough(kube, opts, silent)
    summary = "the registry did not answer for {0} of {1} image(s)".format(
        len(silent), len(opts.route_images))
    if not reasons and opts.use_cached_images:
        return CheckResult(
            name, title, UNKNOWN, summary + "; going on with cached images, as asked", details,
            notice="{0}: going on with the images cached on the nodes (--use-cached-images). A "
                   "pod that moves to a node without them will not start until the registry "
                   "answers.".format(summary))
    details.append("This does not mean an image is missing. Nothing has been changed.")
    if reasons:
        details.append("Running without the registry is not possible yet:")
        details.extend("  - " + reason for reason in reasons)
    else:
        details.append("Running without the registry is possible: every image is on every "
                       "node, and the pull policy will not force a pull.")
    details.extend([
        "Options:",
        "  1. try again once the registry answers",
        "  2. use another registry: --image-registry <host/path>, which must hold these digests",
        "  3. use the cached images: --use-cached-images"
        + (", once the above is fixed" if reasons else ""),
    ])
    return CheckResult(name, title, FAIL, summary, details)


def check_image_on_nodes(kube: Kube, opts: PreflightOptions) -> CheckResult:
    """Whether the target image is already cached on the cluster's nodes.

    Proving that a cluster *can* pull an image would require pulling it, which
    writes to node storage. That is out of scope for a read-only preflight, so
    this reports what can be observed without side effects and returns
    ``unknown`` rather than implying more than it checked.
    """
    details: List[str] = []
    try:
        nodes = kube.json(["get", "nodes"], namespaced=False)
    except PreflightError as exc:
        return CheckResult(
            "image_on_nodes",
            "Target image present on nodes",
            UNKNOWN,
            "cannot list nodes: {0}".format(exc),
            details,
        )

    wanted = opts.target_image
    short = wanted.split("/")[-1]
    items = nodes.get("items", [])
    present, absent = [], []

    for node in items:
        name = node.get("metadata", {}).get("name", "?")
        names: List[str] = []
        for image in node.get("status", {}).get("images", []) or []:
            names.extend(image.get("names") or [])
        if any(wanted in n or short in n for n in names):
            present.append(name)
        else:
            absent.append(name)

    for name in present:
        details.append("{0} : image already present".format(name))
    for name in absent:
        details.append("{0} : image not cached".format(name))

    # A cluster-wide imagePullSecret is the usual reason a private image is
    # pullable; worth surfacing, but its presence still proves nothing.
    rc, out, _ = kube.run(
        ["get", "serviceaccount", "default", "-o", "jsonpath={.imagePullSecrets[*].name}"]
    )
    if rc == 0 and out:
        details.append("default serviceaccount pull secrets : {0}".format(out))

    if items and not absent:
        return CheckResult(
            "image_on_nodes",
            "Target image present on nodes",
            PASS,
            "already cached on every node; no pull needed",
            details,
        )

    details.append(
        "Pullability cannot be proven without pulling, which would write to "
        "node storage. Preflight is read-only, so this stays unknown."
    )
    return CheckResult(
        "image_on_nodes",
        "Target image present on nodes",
        UNKNOWN,
        "not cached on {0} of {1} node(s)".format(len(absent), len(items) or 1),
        details,
    )


_INSTANCE_SQL = (
    "SELECT current_setting('server_version'), "
    "pg_is_in_recovery(), "
    "coalesce(current_setting('data_directory', true), ''), "
    "(SELECT coalesce(sum(pg_database_size(datname)), 0) FROM pg_database WHERE datallowconn), "
    "(SELECT coalesce(string_agg(datname, ','), '') FROM pg_database WHERE datallowconn), "
    "coalesce((SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb'), '')"
)


def discover_instances(kube: Kube, opts: PreflightOptions) -> Tuple[List[Instance], CheckResult]:
    """Query every member for its running versions.

    Versions come from the database itself -- ``current_setting`` and
    ``pg_extension`` -- never from the pod's image tag or from chart values. An
    image tag records what someone intended to deploy; only the database knows
    what is actually running.
    """
    try:
        pods = kube.json(["get", "pods", "-l", opts.selector])
    except PreflightError as exc:
        return [], CheckResult(
            "instances",
            "Running versions, per instance",
            FAIL,
            "cannot list pods: {0}".format(exc),
            [],
        )

    items = pods.get("items", [])
    if not items:
        return [], CheckResult(
            "instances",
            "Running versions, per instance",
            FAIL,
            "no pods match selector {0!r} in namespace {1}".format(
                opts.selector, opts.namespace
            ),
            [],
        )

    instances: List[Instance] = []
    for pod in items:
        meta = pod.get("metadata", {})
        name = meta.get("name", "?")
        inst = Instance(
            pod=name,
            role=patroni_role(meta.get("labels")),
            node=pod.get("spec", {}).get("nodeName", ""),
        )

        rc, out, err = kube.psql(
            name, opts.container, opts.admin_db, opts.psql_user, _INSTANCE_SQL
        )
        if rc != 0:
            inst.error = err or out or "psql failed"
            instances.append(inst)
            continue

        fields = out.split("|")
        if len(fields) < 6:
            inst.error = "unexpected psql output: {0!r}".format(out)
            instances.append(inst)
            continue

        inst.server_version = fields[0].strip()
        inst.in_recovery = fields[1].strip() == "t"
        inst.data_directory = fields[2].strip() or None
        try:
            inst.total_size_bytes = int(fields[3].strip())
        except ValueError:
            inst.total_size_bytes = None
        databases = [d for d in fields[4].split(",") if d]
        inst.available_ts = fields[5].strip() or None

        # The extension version is per database, and they can legitimately
        # differ. An upgrade has to know that, so ask each one.
        for database in databases:
            rc, out, err = kube.psql(
                name,
                opts.container,
                database,
                opts.psql_user,
                "SELECT coalesce((SELECT extversion FROM pg_extension "
                "WHERE extname = 'timescaledb'), '')",
            )
            inst.extensions[database] = out.strip() if rc == 0 and out.strip() else None

        instances.append(inst)

    details: List[str] = []
    failed = 0
    for inst in instances:
        if inst.error:
            failed += 1
            details.append("{0} : UNREACHABLE - {1}".format(inst.pod, inst.error))
            continue
        installed = {d: v for d, v in inst.extensions.items() if v}
        installed_text = (
            ", ".join("{0}={1}".format(d, v) for d, v in sorted(installed.items()))
            if installed
            else "not installed in any database"
        )
        details.append(
            "{pod} [{role}] : PostgreSQL {pg}  |  timescaledb {ts}".format(
                pod=inst.pod,
                role=inst.role,
                pg=inst.server_version,
                ts=installed_text,
            )
        )
        if inst.available_ts:
            details.append(
                "{0}          image ships timescaledb up to {1}".format(
                    inst.pod, inst.available_ts
                )
            )
        if inst.ts_versions_disagree:
            details.append(
                "{0} : databases disagree on the extension version; an upgrade "
                "must handle each one".format(inst.pod)
            )

    if failed == len(instances):
        status, summary = FAIL, "no instance could be queried"
    elif failed:
        status, summary = FAIL, "{0} of {1} instances unreachable".format(
            failed, len(instances)
        )
    else:
        status, summary = PASS, "queried {0} instance(s)".format(len(instances))

    return instances, CheckResult(
        "instances", "Running versions, per instance", status, summary, details
    )


def _parse_df(output: str) -> Optional[Dict[str, int]]:
    """Parse `df -Pk` output into byte counts."""
    lines = [l for l in output.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 5:
        return None
    try:
        return {
            "total": int(parts[1]) * 1024,
            "used": int(parts[2]) * 1024,
            "free": int(parts[3]) * 1024,
        }
    except ValueError:
        return None


def _human(size: Optional[float]) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return "{0:.1f} {1}".format(value, unit)
        value /= 1024.0
    return "{0:.1f} TiB".format(value)


def check_disk_headroom(
    kube: Kube, opts: PreflightOptions, instances: Sequence[Instance]
) -> CheckResult:
    """Compare free space where a dump would land against the live database size.

    The comparison is deliberately against the *current database size*, not an
    estimate of dump size. A dump is usually smaller than the live database, but
    "usually" is not a guarantee worth betting a maintenance window on, so this
    errs conservative and says so.
    """
    details: List[str] = []
    statuses: List[str] = []

    usable = [i for i in instances if not i.error]
    if not usable:
        return CheckResult(
            "disk",
            "Disk headroom for a dump",
            UNKNOWN,
            "no instance could be queried",
            details,
        )

    for inst in usable:
        path = opts.dump_path
        if not path:
            # Default to the volume the data directory sits on.
            path = (
                inst.data_directory.rsplit("/", 1)[0]
                if inst.data_directory and "/" in inst.data_directory
                else "/tmp"
            )

        rc, out, err = kube.exec(inst.pod, opts.container, ["df", "-Pk", path])
        usage = _parse_df(out) if rc == 0 else None
        if usage is None:
            statuses.append(UNKNOWN)
            details.append(
                "{0} : cannot read free space at {1} ({2})".format(
                    inst.pod, path, (err or out or "df failed").splitlines()[:1]
                )
            )
            continue

        size = inst.total_size_bytes
        if size is None:
            statuses.append(UNKNOWN)
            details.append(
                "{0} : free {1} at {2}, database size unknown".format(
                    inst.pod, _human(usage["free"]), path
                )
            )
            continue

        needed = size * opts.headroom_factor
        ok = usage["free"] >= needed
        statuses.append(PASS if ok else FAIL)
        details.append(
            "{pod} : {path} free {free} of {total} | databases {size} | "
            "need >= {needed} ({factor}x) -> {verdict}".format(
                pod=inst.pod,
                path=path,
                free=_human(usage["free"]),
                total=_human(usage["total"]),
                size=_human(size),
                needed=_human(needed),
                factor=opts.headroom_factor,
                verdict="ok" if ok else "SHORT",
            )
        )

    details.append(
        "Compared against live database size, which is conservative: a dump is "
        "usually smaller, but that is not guaranteed."
    )
    details.append(
        "Free space is what df reports inside the pod. Provisioners that do not "
        "enforce quotas (local-path, hostPath) report the whole node disk, so "
        "this can look far larger than the PVC request below."
    )
    return CheckResult(
        "disk",
        "Disk headroom for a dump",
        worst_status(statuses),
        "checked {0} instance(s)".format(len(usable)),
        details,
    )


# A copy needs the data directory's size again, plus a margin; a link only
# the new catalog and some room, whichever is larger.
PG_UPGRADE_COPY_FACTOR = 1.1
PG_UPGRADE_LINK_MARGIN = 256 * 1024 * 1024
PG_UPGRADE_LINK_FRACTION = 0.02


def check_pg_upgrade_headroom(
    kube: Kube, opts: PreflightOptions, instances: Sequence[Instance]
) -> CheckResult:
    """Can ordinal 0's disk hold what pg_upgrade writes next to the data?

    ``pg_upgrade`` runs on ordinal 0's volume and keeps the old directory
    beside the new one (``<data>.pg<old>``). A copy writes the whole data
    directory again, WAL included, so it needs that much free; ``--link``
    hard-links the relation files and needs only the new catalog. Earlier
    ``<data>.pg<N>`` directories from previous hops are on the same disk,
    already counted as used, and listed so they can be removed once the
    result is accepted.
    """
    title = "Disk headroom for pg_upgrade on ordinal 0"
    link = bool(opts.pg_upgrade_link)
    first = next((i for i in instances if not i.error and i.pod.endswith("-0")), None)
    if first is None or not first.data_directory:
        return CheckResult("pg_upgrade_disk", title, UNKNOWN,
                           "no reachable ordinal 0 with a known data directory", [])
    data = first.data_directory.rstrip("/")
    parent = data.rsplit("/", 1)[0] or "/"
    rc, out, err = kube.exec(first.pod, opts.container, ["du", "-sk", data])
    try:
        size = int(out.split()[0]) * 1024 if rc == 0 else None
    except (IndexError, ValueError):
        size = None
    rc, df_out, df_err = kube.exec(first.pod, opts.container, ["df", "-Pk", parent])
    usage = _parse_df(df_out) if rc == 0 else None
    details: List[str] = []
    _, ls_out, _ = kube.exec(first.pod, opts.container,
                             ["sh", "-c", "ls -d {0}.pg* 2>/dev/null || true".format(data)])
    left = [l.strip() for l in ls_out.splitlines() if l.strip()]
    if left:
        details.append("earlier data directories on this disk, kept by previous pg_upgrade runs: "
                       "{0}; remove them once the result is accepted".format(", ".join(left)))
    if size is None or usage is None:
        details.append("{0} : cannot read the size of {1} or the free space at {2} ({3})".format(
            first.pod, data, parent, (err or df_err or "du/df failed").splitlines()[:1]))
        return CheckResult("pg_upgrade_disk", title, UNKNOWN, "could not measure", details)
    if link:
        needed = max(PG_UPGRADE_LINK_MARGIN, size * PG_UPGRADE_LINK_FRACTION)
        how = "--link: the relation files are hard-linked, so only a margin"
    else:
        needed = size * PG_UPGRADE_COPY_FACTOR
        how = "copy: the data directory again, {0}x".format(PG_UPGRADE_COPY_FACTOR)
    ok = usage["free"] >= needed
    details.insert(0, "{0} : {1} is {2}; {3} free at {4}; need >= {5} ({6}) -> {7}".format(
        first.pod, data, _human(size), _human(usage["free"]), parent, _human(needed), how,
        "ok" if ok else "SHORT"))
    details.append(
        "Free space is what df reports inside the pod. Provisioners that do not "
        "enforce quotas (local-path, hostPath) report the whole node disk.")
    return CheckResult("pg_upgrade_disk", title, PASS if ok else FAIL,
                       "{0} needed, {1} free".format(_human(needed), _human(usage["free"])), details)


# What a pg_upgrade does to backups; said by preflight, the runbook and apply.
BACKUPS_AT_A_MAJOR_UPGRADE = (
    "pg_upgrade gives the cluster a new system identifier, so backups and the WAL archive "
    "made before it no longer match: take a full backup before; after, with pgBackRest, run "
    "stanza-upgrade (--no-online while the database is down) and a new full backup. pgBackRest "
    "checks the system identifier, so expect archiving to fail, and WAL to accumulate in "
    "pg_wal, until stanza-upgrade has run. The data directory "
    "keeps its path here, so pgBackRest's pg1-path does not change")


def check_archiving(kube: Kube, opts: PreflightOptions, instances: Sequence[Instance]) -> CheckResult:
    """Before a pg_upgrade: is WAL archived? If so, backups need attention.

    Not a failure: whether and how the cluster is backed up is outside what
    this tool can see. pgBackRest's own steps are in its user guide,
    "Upgrading PostgreSQL" (checked for 2.54-2.59, the versions the images ship).
    """
    title = "Backups across pg_upgrade"
    primary = next((i for i in instances if not i.error and i.role == "primary"), None)
    if primary is None:
        return CheckResult("archiving", title, UNKNOWN, "no reachable primary", [])
    rc, out, err = kube.psql(primary.pod, opts.container, opts.admin_db, opts.psql_user,
                             "SELECT current_setting('archive_mode') || '|' || "
                             "current_setting('archive_command')")
    if rc != 0:
        return CheckResult("archiving", title, UNKNOWN, "cannot read archive_mode",
                           [(err or out).strip()[:200]])
    mode, _, command = out.strip().partition("|")
    if mode == "off":
        return CheckResult("archiving", title, PASS,
                           "archive_mode is off; no WAL archive to break",
                           [BACKUPS_AT_A_MAJOR_UPGRADE])
    return CheckResult(
        "archiving", title, UNKNOWN,
        "archive_mode is {0}: the archive and backups will not match the upgraded cluster".format(mode),
        ["archive_command: {0}".format(command or "(empty)"), BACKUPS_AT_A_MAJOR_UPGRADE])


# Other extensions, and the libraries of user C functions as pg_upgrade --check
# lists them (prolang 13 is C; below FirstNormalObjectId, 16384, is built in).
# TimescaleDB is left out: drift and the plan's releases already cover it.
_EXTENSIONS_SQL = (
    "SELECT 'ext|' || extname || '|' || extversion FROM pg_extension "
    "WHERE extname <> 'timescaledb' "
    "UNION ALL SELECT DISTINCT 'lib|' || probin FROM pg_proc "
    "WHERE prolang = 13 AND probin IS NOT NULL AND oid >= 16384 "
    "AND probin NOT LIKE '$libdir/timescaledb%'")


def _images_to_check(opts: PreflightOptions, target_pg: Optional[str]) -> List[Tuple[str, Optional[int], str]]:
    """``(image, major, role)``: the target image on its major and, before a
    pg_upgrade, the pg_upgrade image on the new one."""
    try:
        major = parse_version(target_pg)[0] if target_pg else None
    except InvalidVersion:
        major = None
    found = [(opts.target_image, major, "runs the cluster")]
    if opts.pg_upgrade_image and opts.pg_upgrade_to:
        found.append((opts.pg_upgrade_image, opts.pg_upgrade_to, "runs pg_upgrade"))
    return found


def check_extensions(
    kube: Kube, opts: PreflightOptions, instances: Sequence[Instance], target_pg: Optional[str]
) -> CheckResult:
    """Do the target image's binaries cover every other extension in use?

    The matrix verdicts are about TimescaleDB only. Per database on the
    primary: every other installed extension at its version, and every shared
    library a user C function is defined in (``pg_proc.probin``, selected as
    ``pg_upgrade --check`` selects them), against what the target image ships for the target
    major, measured (:mod:`contents`). Before a ``pg_upgrade`` the image it
    runs in is checked too, for the new major. A library that is not there
    fails: ``pg_upgrade --check`` would refuse in the middle of the outage,
    and a same-major rollout would leave functions that cannot load. An
    extension version without an install script there is unknown: it can
    still run, but cannot be created or updated from that image.
    """
    title = "Other extensions on the target image"
    primary = next((i for i in instances if not i.error and i.role == "primary"), None)
    if primary is None:
        return CheckResult("extensions", title, UNKNOWN, "no reachable primary", [])
    try:
        contents = load_contents(opts.contents_path)
    except ContentsError as exc:
        return CheckResult("extensions", title, UNKNOWN, "no measured contents: {0}".format(exc), [])

    used_ext: Dict[str, set] = {}
    used_lib: Dict[str, set] = {}
    details: List[str] = []
    statuses: List[str] = []
    for database in sorted(primary.extensions):
        rc, out, err = kube.psql(primary.pod, opts.container, database, opts.psql_user, _EXTENSIONS_SQL)
        if rc != 0:
            statuses.append(UNKNOWN)
            details.append("{0} : cannot list extensions ({1})".format(database, (err or out).strip()[:120]))
            continue
        for line in out.splitlines():
            kind, _, rest = line.strip().partition("|")
            if kind == "ext":
                name, _, version = rest.partition("|")
                used_ext.setdefault("{0} {1}".format(name, version), set()).add(database)
            elif kind == "lib":
                used_lib.setdefault(rest, set()).add(database)

    for image, major, role in _images_to_check(opts, target_pg):
        measured = contents.major(image, major) if major else None
        if not measured or "libraries" not in measured:
            statuses.append(UNKNOWN)
            details.append("{0} ({1}) : pg{2} contents not measured with extensions; "
                           "cannot check".format(image, role, major or "?"))
            continue
        shipped, libraries = measured.get("extensions") or {}, set(measured.get("libraries") or [])
        missing, unknown = [], []
        for lib in sorted(used_lib):
            if not lib.startswith("$libdir/"):
                unknown.append("library {0} (not under $libdir)".format(lib))
            elif lib[len("$libdir/"):] not in libraries:
                missing.append("library {0} (used in {1})".format(lib, ", ".join(sorted(used_lib[lib]))))
        for key in sorted(used_ext):
            name, version = key.split(" ", 1)
            if name not in shipped:
                missing.append("extension {0} {1} (in {2}): no control file".format(
                    name, version, ", ".join(sorted(used_ext[key]))))
            elif version not in shipped[name]:
                unknown.append("extension {0} {1}: no install script for that version (ships {2})".format(
                    name, version, ", ".join(shipped[name]) or "none"))
        statuses.append(FAIL if missing else UNKNOWN if unknown else PASS)
        details.append("{0} ({1}, pg{2}) : {3}".format(
            image, role, major, "missing " + "; ".join(missing) if missing
            else "ships every extension and library in use"))
        details.extend("     unknown: " + u for u in unknown)
    details.append("in use: {0}".format(", ".join(sorted(used_ext)) or "nothing"))
    details.append("The matrix verdicts are about TimescaleDB only; this check is what covers the rest.")
    return CheckResult("extensions", title, worst_status(statuses),
                       "{0} extension(s), {1} librar(ies) in use".format(len(used_ext), len(used_lib)),
                       details)


def _code_point_order(collate: str) -> bool:
    """C, POSIX and C.<encoding> sort by byte value, whatever glibc says."""
    return collate in ("C", "POSIX") or collate.startswith("C.")


def _running_image(kube: Kube, opts: PreflightOptions) -> Optional[str]:
    pods = kube.json(["get", "pods", "-l", opts.selector]).get("items", [])
    for pod in pods:
        for c in pod.get("spec", {}).get("containers") or []:
            if c.get("name") == opts.container and c.get("image"):
                return c["image"].split("@", 1)[0]
    return None


def check_collation(kube: Kube, opts: PreflightOptions, instances: Sequence[Instance]) -> CheckResult:
    """Will the target image sort text the way the indexes were built?

    A database whose collation comes from glibc (libc provider, not C or
    POSIX) has its text indexes ordered by that glibc's rules; an image
    with another glibc can order some strings differently, and those
    indexes then return wrong answers until rebuilt. Measured: the Debian
    10 tags carry glibc 2.28, the Ubuntu 22.04 ones 2.35. A warning, not a
    refusal: what changes depends on the data. ICU collations depend on the
    ICU version, which is not measured.
    """
    title = "Collation across the image change"
    primary = next((i for i in instances if not i.error and i.role == "primary"), None)
    if primary is None:
        return CheckResult("collation", title, UNKNOWN, "no reachable primary", [])
    try:
        contents = load_contents(opts.contents_path)
        current = _running_image(kube, opts)
    except (ContentsError, PreflightError) as exc:
        return CheckResult("collation", title, UNKNOWN, "cannot compare: {0}".format(exc), [])
    before = (contents.entry(current) or {}).get("glibc") if current else None
    after = (contents.entry(opts.target_image) or {}).get("glibc")
    if not before or not after:
        return CheckResult("collation", title, UNKNOWN, "glibc not measured for {0}".format(
            " and ".join(r for r, g in ((current or "the running image", before),
                                        (opts.target_image, after)) if not g)), [])
    try:
        major = parse_version(primary.server_version or "")[0]
    except (InvalidVersion, IndexError):
        major = 0
    provider = "datlocprovider" if major >= 15 else "'c'"
    rc, out, err = kube.psql(primary.pod, opts.container, opts.admin_db, opts.psql_user,
                             "SELECT datname || '|' || datcollate || '|' || {0} FROM pg_database "
                             "WHERE datallowconn".format(provider))
    if rc != 0:
        return CheckResult("collation", title, UNKNOWN, "cannot read the databases' collations",
                           [(err or out).strip()[:200]])
    affected, icu = [], []
    for line in out.splitlines():
        name, _, rest = line.strip().partition("|")
        collate, _, kind = rest.partition("|")
        if kind == "i":
            icu.append(name)
        elif kind == "c" and not _code_point_order(collate):
            affected.append("{0} ({1})".format(name, collate))
    details = ["glibc {0} on {1} -> {2} on {3}".format(before, current, after, opts.target_image)]
    if icu:
        details.append("ICU collations in {0}: the ICU version is not measured".format(", ".join(icu)))
    if before == after or not affected:
        return CheckResult("collation", title, UNKNOWN if icu else PASS,
                           "same glibc" if before == after else "no database sorts by glibc's rules",
                           details)
    details.append("databases sorting by glibc's rules: {0}".format(", ".join(affected)))
    details.append("after the upgrade, check their text indexes (amcheck: SELECT bt_index_check(oid) "
                   "for each btree index on a text column) or REINDEX them; column-level "
                   "collations are not inspected here")
    return CheckResult("collation", title, UNKNOWN,
                       "glibc changes {0} -> {1} under {2} database(s) with libc collations".format(
                           before, after, len(affected)), details)


def check_permissions(
    kube: Kube, opts: PreflightOptions, instances: Sequence[Instance]
) -> CheckResult:
    """Ownership and writability of the paths and volumes an upgrade touches."""
    details: List[str] = []
    statuses: List[str] = []

    usable = [i for i in instances if not i.error]
    if not usable:
        return CheckResult(
            "permissions",
            "Permissions on paths and volumes",
            UNKNOWN,
            "no instance could be queried",
            details,
        )

    for inst in usable:
        rc, who, _ = kube.exec(inst.pod, opts.container, ["id", "-u"])
        uid = who.strip() if rc == 0 else "?"

        data_dir = inst.data_directory
        dump_path = opts.dump_path or (
            data_dir.rsplit("/", 1)[0] if data_dir and "/" in data_dir else "/tmp"
        )

        for label, path in (("data dir", data_dir), ("dump path", dump_path)):
            if not path:
                statuses.append(UNKNOWN)
                details.append("{0} : {1} unknown".format(inst.pod, label))
                continue
            rc, out, err = kube.exec(
                inst.pod, opts.container, ["stat", "-c", "%U:%G %a", path]
            )
            if rc != 0:
                statuses.append(UNKNOWN)
                details.append(
                    "{0} : cannot stat {1} ({2})".format(inst.pod, path, err or out)
                )
                continue
            details.append(
                "{pod} : {label} {path} owner/mode {info} (process uid {uid})".format(
                    pod=inst.pod, label=label, path=path, info=out.strip(), uid=uid
                )
            )

        rc, _, _ = kube.exec(inst.pod, opts.container, ["test", "-w", dump_path])
        writable = rc == 0
        statuses.append(PASS if writable else FAIL)
        details.append(
            "{0} : dump path {1} is {2} by the database process".format(
                inst.pod, dump_path, "writable" if writable else "NOT writable"
            )
        )

    # Volume-level facts: capacity, access modes and whether the class can grow.
    try:
        pvcs = kube.json(["get", "pvc"])
        for pvc in pvcs.get("items", []):
            meta = pvc.get("metadata", {})
            spec = pvc.get("spec", {})
            status = pvc.get("status", {})
            class_name = spec.get("storageClassName")
            expandable = "unknown"
            if class_name:
                rc, out, _ = kube.run(
                    [
                        "get",
                        "storageclass",
                        class_name,
                        "-o",
                        "jsonpath={.allowVolumeExpansion}",
                    ],
                    namespaced=False,
                )
                if rc == 0:
                    expandable = out.strip() or "false"
            details.append(
                "pvc {name} : {cap} {modes} class={cls} expandable={exp} phase={phase}".format(
                    name=meta.get("name", "?"),
                    cap=(status.get("capacity") or {}).get("storage", "?"),
                    modes=",".join(spec.get("accessModes") or []),
                    cls=class_name or "(default)",
                    exp=expandable,
                    phase=status.get("phase", "?"),
                )
            )
    except PreflightError as exc:
        details.append("cannot list PVCs: {0}".format(exc))
        statuses.append(UNKNOWN)

    return CheckResult(
        "permissions",
        "Permissions on paths and volumes",
        worst_status(statuses),
        "checked {0} instance(s)".format(len(usable)),
        details,
    )


def check_abi_break(instances: Sequence[Instance]) -> CheckResult:
    """Refuse the six PostgreSQL patch releases that cannot load TimescaleDB.

    PostgreSQL 17.1, 16.5, 15.9, 14.14, 13.17 and 12.21 grew
    ``struct ResultRelInfo`` by accident, breaking the ABI the extension is
    compiled against. The change was reverted in the next patch release each.

    This is a property of the running server, not of any transition, so it is
    checked against what was discovered rather than looked up in the matrix. It
    fails rather than warns: on these releases the extension does not load, so
    there is no upgrade to plan.
    """
    details: List[str] = []
    statuses: List[str] = []

    usable = [i for i in instances if not i.error and i.server_version]
    if not usable:
        return CheckResult(
            "abi_break",
            "PostgreSQL ABI-break patch levels",
            UNKNOWN,
            "no instance reported a server version",
            details,
        )

    for inst in usable:
        fixed = abi_break_for(inst.server_version)
        if fixed is None:
            statuses.append(PASS)
            details.append(
                "{0} : PostgreSQL {1} is not one of the affected releases".format(
                    inst.pod, inst.server_version
                )
            )
            continue
        statuses.append(FAIL)
        details.append(
            "{0} : PostgreSQL {1} shipped the ResultRelInfo ABI break; "
            "TimescaleDB cannot load. Move to {2} or later first.".format(
                inst.pod, inst.server_version, fixed
            )
        )

    details.append("affected : " + ", ".join(
        "{0}.{1} (fixed in {2})".format(major, minor, fixed)
        for (major, minor), fixed in sorted(ABI_BROKEN.items(), reverse=True)
    ))
    details.append("source   : {0}".format(ABI_BROKEN_SOURCE))

    return CheckResult(
        "abi_break",
        "PostgreSQL ABI-break patch levels",
        worst_status(statuses),
        "checked {0} instance(s)".format(len(usable)),
        details,
    )


def check_pg_minor_floor(
    instances: Sequence[Instance],
    target_pg: Optional[str],
    target_ts: Optional[str],
) -> CheckResult:
    """Refuse a target TimescaleDB that will not load on the PostgreSQL minor.

    TimescaleDB checks ``server_version_num`` against its own compat.h at load
    time: 2.20+ needs 15.10 / 16.6 / 17.2, for example. Like the ABI break this
    is a property of the server, not of a transition, so it is not in the
    matrix.

    The server that matters is the one the target TimescaleDB will run on. When
    the target image pins a PostgreSQL minor, that is the image's; otherwise the
    image is not changing PostgreSQL as far as we know, and it is each running
    server's.
    """
    title = "PostgreSQL minor accepted by target TimescaleDB"
    details: List[str] = []

    if not target_ts:
        return CheckResult(
            "pg_minor_floor",
            title,
            UNKNOWN,
            "target TimescaleDB version not pinned; pass --target-ts",
            details,
        )

    found = pg_minor_rules_for(target_ts)
    if found is None:
        return CheckResult(
            "pg_minor_floor",
            title,
            UNKNOWN,
            "no transcribed compat.h rules for TimescaleDB {0}; it may have "
            "raised a floor".format(target_ts),
            details,
        )
    tag, _rules = found

    try:
        target_has_minor = target_pg is not None and len(parse_version(target_pg)) >= 2
    except InvalidVersion:
        target_has_minor = False

    if target_has_minor:
        servers = [("target image", target_pg)]
    else:
        servers = [
            (i.pod, i.server_version)
            for i in instances
            if not i.error and i.server_version
        ]
        if not servers:
            return CheckResult(
                "pg_minor_floor",
                title,
                UNKNOWN,
                "no instance reported a server version",
                details,
            )

    statuses: List[str] = []
    for who, version in servers:
        try:
            has_minor = len(parse_version(version)) >= 2
        except InvalidVersion:
            has_minor = False
        if not has_minor:
            statuses.append(UNKNOWN)
            details.append(
                "{0} : cannot read a PostgreSQL minor from {1!r}".format(who, version)
            )
            continue
        refusal = pg_minor_refusal(target_ts, version)
        if refusal is None:
            statuses.append(PASS)
            details.append(
                "{0} : PostgreSQL {1} is accepted by TimescaleDB {2}".format(
                    who, version, target_ts
                )
            )
        else:
            statuses.append(FAIL)
            details.append(
                "{0} : PostgreSQL {1} - {2}. The extension will not load; "
                "move PostgreSQL first.".format(who, version, refusal)
            )

    details.append("source : {0}".format(PG_MINOR_RULES_SOURCE.format(tag=tag)))

    return CheckResult(
        "pg_minor_floor",
        title,
        worst_status(statuses),
        "target ts{0}".format(target_ts),
        details,
    )


def installable_on_instance(
    kube: "Kube", opts: PreflightOptions, pod: str
) -> Tuple[List[str], Optional[str]]:
    """Which extension versions this pod's image can genuinely install.

    Deliberately not ``pg_available_extension_versions``: that view lists every
    version any update script so much as mentions, and asking for one of the
    phantoms drops the backend connection outright.

    The directories come from the ``pg_config`` *view* -- ordinary read-only SQL
    over the path preflight already uses -- so this adds ``ls`` to the in-pod
    command set and nothing else.
    """
    rc, out, err = kube.psql(
        pod,
        opts.container,
        opts.admin_db,
        opts.psql_user,
        "SELECT setting FROM pg_config WHERE name = 'SHAREDIR' "
        "UNION ALL SELECT setting FROM pg_config WHERE name = 'PKGLIBDIR'",
    )
    if rc != 0 or len(out.splitlines()) < 2:
        return [], "cannot read SHAREDIR/PKGLIBDIR: {0}".format(
            err or out or "no rows"
        )

    sharedir, pkglibdir = [line.strip() for line in out.splitlines()[:2]]

    rc, share_out, err = kube.exec(pod, opts.container, ["ls", sharedir + "/extension"])
    if rc != 0:
        return [], "cannot list {0}/extension: {1}".format(sharedir, err or share_out)
    rc, lib_out, err = kube.exec(pod, opts.container, ["ls", pkglibdir])
    if rc != 0:
        return [], "cannot list {0}: {1}".format(pkglibdir, err or lib_out)

    return installable_from_listings(share_out.split(), lib_out.split()), None


def check_drift(
    kube: "Kube", opts: PreflightOptions, instances: Sequence[Instance]
) -> CheckResult:
    """Classify every database against what its own image can install.

    Per database, never per cluster: ``ALTER EXTENSION`` runs once per database
    and they genuinely drift apart, so a cluster-level answer would be wrong.

    ``stranded`` and ``ahead`` are outages and fail. ``lagging`` is a correct,
    running configuration that simply is not current, so it passes with an
    advisory. A version that cannot be read is ``unknown`` -- a stranded
    database and an unreachable one look alike from outside, and inventing the
    first from the second would be a false alarm.
    """
    details: List[str] = []
    statuses: List[str] = []

    usable = [i for i in instances if not i.error]
    if not usable:
        return CheckResult(
            "drift",
            "Extension version drift",
            UNKNOWN,
            "no instance could be queried",
            details,
        )

    for inst in usable:
        installable, problem = installable_on_instance(kube, opts, inst.pod)
        if problem:
            statuses.append(UNKNOWN)
            details.append("{0} : {1}".format(inst.pod, problem))
            continue

        details.append(
            "{0} : image installs {1} version(s), {2} .. {3}; default {4}".format(
                inst.pod,
                len(installable),
                installable[0] if installable else "?",
                installable[-1] if installable else "?",
                inst.available_ts or "unknown",
            )
        )

        for database, version in sorted(inst.extensions.items()):
            if version is None:
                continue
            verdict = classify(version, installable, inst.available_ts)
            if verdict.state == DRIFT_INDETERMINATE:
                statuses.append(UNKNOWN)
            elif verdict.is_outage:
                statuses.append(FAIL)
            else:
                statuses.append(PASS)
            details.append(
                "{0}/{1} : {2} - {3}".format(
                    inst.pod, database, verdict.state.upper(), verdict.summary
                )
            )
            if verdict.remedy and verdict.state != ALIGNED_STATE:
                details.append("     -> {0}".format(verdict.remedy))

    details.append("note : {0}".format(AVAILABLE_VERSIONS_VIEW_WARNING))
    return CheckResult(
        "drift",
        "Extension version drift",
        worst_status(statuses),
        "checked {0} instance(s)".format(len(usable)),
        details,
    )


def check_gates(
    kube: "Kube",
    opts: PreflightOptions,
    instances: Sequence[Instance],
    target_ts: Optional[str],
) -> CheckResult:
    """The blockers compiled into the update scripts, per database.

    A transition can be mechanically in-place and still be blocked by what a
    database happens to contain. These are the conditions that decide it, and
    every one of them is a read-only query.
    """
    details: List[str] = []
    statuses: List[str] = []

    if not target_ts:
        return CheckResult(
            "gates",
            "Update-script gates",
            UNKNOWN,
            "target TimescaleDB version not pinned; pass --target-ts",
            details,
        )

    usable = [i for i in instances if not i.error]
    if not usable:
        return CheckResult(
            "gates",
            "Update-script gates",
            UNKNOWN,
            "no instance could be queried",
            details,
        )

    for inst in usable:

        def runner(database: str, sql: str, _pod: str = inst.pod):
            return kube.psql(_pod, opts.container, database, opts.psql_user, sql)

        for database, version in sorted(inst.extensions.items()):
            if version is None:
                continue
            for gate in run_gates(runner, database, version, target_ts):
                if gate.status == NOT_APPLICABLE:
                    continue
                if gate.status == BLOCKED:
                    statuses.append(FAIL)
                elif gate.status == GATE_INDETERMINATE:
                    statuses.append(UNKNOWN)
                elif gate.status == CLEAR:
                    statuses.append(PASS)
                details.append(
                    "{0}/{1} : {2} - {3}".format(
                        inst.pod, database, gate.title, gate.summary
                    )
                )
                if gate.status == BLOCKED:
                    for line in gate.details:
                        details.append("     {0}".format(line))
                    if gate.source_url:
                        details.append("     source: {0}".format(gate.source_url))

    for rule in SESSION_RULES:
        details.append("note : {0}".format(rule))

    return CheckResult(
        "gates",
        "Update-script gates",
        worst_status(statuses),
        "target ts{0}".format(target_ts),
        details,
    )


def check_later_gates(
    kube: "Kube",
    opts: PreflightOptions,
    instances: Sequence[Instance],
    target_ts: Optional[str],
) -> Optional[CheckResult]:
    """Will a later hop be refused for what this database holds?

    Only the 2.25 refusal is looked ahead for: nothing but ``cagg_migrate``
    clears it, and the update it stops comes after every ``pg_upgrade`` before
    it. It does not stop this hop, since a release may be too old to have
    ``cagg_migrate`` at all (2.1.1 has none, 2.15.3 has it); the operator gets
    told now, and the gate still stops the hop that needs stopping. None when
    no later update reaches 2.25, or this hop already does and ``gates`` covers it.
    """
    crossing = next(((hop, ts) for hop, ts in opts.later_updates if reaches_old_cagg_removal(ts)), None)
    if crossing is None or reaches_old_cagg_removal(target_ts):
        return None
    hop, release = crossing
    name, title = "later_gates", "Update-script gates later in this plan"
    details: List[str] = []
    blocked: List[str] = []
    unsure = False
    seen = set()
    for inst in [i for i in instances if not i.error]:

        def runner(database: str, sql: str, _pod: str = inst.pod):
            return kube.psql(_pod, opts.container, database, opts.psql_user, sql)

        for database, version in sorted(inst.extensions.items()):
            if version is None or database in seen:
                continue
            seen.add(database)
            gate = check_old_caggs(runner, database, version, release)
            details.append("{0}/{1} : {2}".format(inst.pod, database, gate.summary))
            if gate.status == BLOCKED:
                blocked.extend("{0}: {1}".format(database, line) for line in gate.details if " : " in line
                               and not line.endswith(" : none"))
            elif gate.status != CLEAR:
                unsure = True
    if blocked:
        return CheckResult(
            name, title, UNKNOWN,
            "hop {0} updates to {1} and will be refused".format(hop, release), details + blocked,
            notice="{0}. Hop {1} of this plan updates to {2}, and the 2.25 update refuses these "
                   "continuous aggregates, so apply will stop before that hop, after the hops "
                   "ahead of it have run. Migrate them with cagg_migrate() below 2.25: now, if "
                   "this release has it, or when apply stops.".format("; ".join(blocked), hop, release))
    if unsure or not seen:
        return CheckResult(name, title, UNKNOWN, "could not establish what hop {0} would meet".format(hop), details)
    return CheckResult(name, title, PASS, "nothing hop {0} would refuse".format(hop), details)


def check_matrix(
    opts: PreflightOptions,
    instances: Sequence[Instance],
    target_pg: Optional[str],
    target_ts: Optional[str],
) -> CheckResult:
    """Look the discovered versions up against the requested target."""
    details: List[str] = []

    if not target_pg or not target_ts:
        return CheckResult(
            "matrix",
            "Matrix verdict",
            UNKNOWN,
            "target versions not pinned; pass --target-pg and --target-ts",
            details,
        )

    usable = [i for i in instances if not i.error and i.server_version]
    if not usable:
        return CheckResult(
            "matrix", "Matrix verdict", UNKNOWN, "no discovered versions to look up", details
        )

    try:
        matrix: Matrix = load_matrix(opts.matrix_path)
    except MatrixError as exc:
        return CheckResult(
            "matrix", "Matrix verdict", UNKNOWN, "cannot load matrix: {0}".format(exc), details
        )

    statuses: List[str] = []
    for inst in usable:
        source_ts = inst.ts_version
        if not source_ts:
            statuses.append(UNKNOWN)
            details.append(
                "{0} : timescaledb not installed (or versions differ between "
                "databases); nothing to look up".format(inst.pod)
            )
            continue

        try:
            entry = matrix.lookup(
                inst.server_version, source_ts, target_pg, target_ts
            )
        except (MatrixError, InvalidVersion) as exc:
            statuses.append(UNKNOWN)
            details.append("{0} : lookup failed: {1}".format(inst.pod, exc))
            continue

        # An empty matrix returns None without ever parsing a version, so this
        # is the first place a junk server_version could surface. Fall back to
        # the raw string rather than crashing the whole report over a label.
        try:
            source_pg_label = str(parse_version(inst.server_version)[0])
        except InvalidVersion:
            source_pg_label = inst.server_version
        label = "pg{0}/ts{1} -> pg{2}/ts{3}".format(
            source_pg_label, source_ts, target_pg, target_ts
        )

        if entry is None:
            statuses.append(UNKNOWN)
            details.append(
                "{0} : {1} - no matrix entry. The matrix says nothing about "
                "this transition; that is not the same as unsupported.".format(
                    inst.pod, label
                )
            )
            continue

        if not entry.is_known:
            statuses.append(UNKNOWN)
            details.append(
                "{0} : {1} - entry exists but its verdict is not established "
                "(verified: false)".format(inst.pod, label)
            )
            continue

        if entry.verdict in ("unsupported", "no_image"):
            statuses.append(FAIL)
        else:
            statuses.append(PASS)
        details.append(
            "{0} : {1} - {2}".format(inst.pod, label, entry.verdict)
        )
        if entry.reason:
            details.append("     reason: {0}".format(entry.reason))
        for ref in entry.references:
            details.append("     reference: {0}".format(ref))
        for i, step in enumerate(entry.steps, 1):
            details.append("     step {0}: {1}".format(i, step))

    return CheckResult(
        "matrix",
        "Matrix verdict",
        worst_status(statuses),
        "target pg{0}/ts{1}".format(target_pg, target_ts),
        details,
    )


# ---------------------------------------------------------------------------
# Runner and report
# ---------------------------------------------------------------------------


def worst_status(statuses: Sequence[str]) -> str:
    """FAIL beats UNKNOWN beats PASS. An empty run is UNKNOWN, never PASS."""
    if not statuses:
        return UNKNOWN
    if FAIL in statuses:
        return FAIL
    if UNKNOWN in statuses:
        return UNKNOWN
    return PASS


def run_preflight(opts: PreflightOptions) -> List[CheckResult]:
    kube = Kube(opts.context, opts.namespace, opts.timeout)
    results: List[CheckResult] = []

    results.append(check_context(kube, opts))
    if opts.route_images or opts.route_checked:
        if opts.route_images:
            results.append(check_route_images(kube, opts))
        target_pg, target_ts = _implied_versions(opts)
    else:
        image_result, target_pg, target_ts = check_target_image(opts)
        results.append(image_result)
        results.append(check_image_on_nodes(kube, opts))

    instances, instances_result = discover_instances(kube, opts)
    results.append(instances_result)
    results.append(check_abi_break(instances))
    results.append(check_pg_minor_floor(instances, target_pg, target_ts))
    results.append(check_disk_headroom(kube, opts, instances))
    results.append(check_extensions(kube, opts, instances, target_pg))
    results.append(check_collation(kube, opts, instances))
    if opts.pg_upgrade_link is not None:
        results.append(check_pg_upgrade_headroom(kube, opts, instances))
        results.append(check_archiving(kube, opts, instances))
    results.append(check_permissions(kube, opts, instances))
    results.append(check_drift(kube, opts, instances))
    results.append(check_gates(kube, opts, instances, target_ts))
    later = check_later_gates(kube, opts, instances, target_ts)
    if later:
        results.append(later)
    results.append(check_matrix(opts, instances, target_pg, target_ts))

    return results


_MARK = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "UNKN"}


def render_report(results: Sequence[CheckResult], color: bool = False) -> str:
    codes = {PASS: "\033[32m", FAIL: "\033[31m", UNKNOWN: "\033[33m"}
    reset = "\033[0m"

    lines: List[str] = []
    lines.append("preflight: read-only; nothing below changes cluster or database state")
    lines.append("")

    for result in results:
        mark = _MARK.get(result.status, "????")
        if color:
            mark = codes.get(result.status, "") + mark + reset
        lines.append("[{0}] {1}: {2}".format(mark, result.title, result.summary))
        for detail in result.details:
            lines.append("       {0}".format(detail))
        lines.append("")

    counts = {PASS: 0, FAIL: 0, UNKNOWN: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    lines.append(
        "summary: {0} pass, {1} fail, {2} unknown".format(
            counts[PASS], counts[FAIL], counts[UNKNOWN]
        )
    )
    if counts[UNKNOWN]:
        lines.append(
            "an unknown check has not been passed; resolve it before upgrading"
        )
    return "\n".join(lines)
