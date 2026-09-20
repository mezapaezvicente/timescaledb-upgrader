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
    parse_image_reference,
    parse_release_tag,
)
from .matrix import Matrix, MatrixError, load_matrix
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
]

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

# Read-only at the server, not just by convention.
_READ_ONLY_PGOPTIONS = "-c default_transaction_read_only=on"


class PreflightError(Exception):
    """Raised when preflight cannot run at all (bad tooling, unreachable API)."""


@dataclass
class CheckResult:
    name: str
    title: str
    status: str
    summary: str
    details: List[str] = field(default_factory=list)


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
            role=(meta.get("labels") or {}).get("role", "unknown"),
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
        if entry.source_url:
            details.append("     source: {0}".format(entry.source_url))
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
    image_result, target_pg, target_ts = check_target_image(opts)
    results.append(image_result)
    results.append(check_image_on_nodes(kube, opts))

    instances, instances_result = discover_instances(kube, opts)
    results.append(instances_result)
    results.append(check_disk_headroom(kube, opts, instances))
    results.append(check_permissions(kube, opts, instances))
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
