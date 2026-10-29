#!/usr/bin/env python3
"""Verification harness: run matrix rows with the upgrader itself.

    python verify/harness.py --list [--pg 15]
    python verify/harness.py --pg 15 [--from 2.13] [--mode both] [--cluster tsdb-verify]

For each same-PG matrix row it picks images from the measured contents
(``data/image-contents.json``), resolves the row's series to exact releases,
and then, once per mode (empty, seeded):

1. ``tsupgrade plan`` -- offline; a refusal is the row's result, no cluster
2. brings the kind cluster onto the row's first image (a fresh namespace when
   it runs anything else) and loads every image the plan names
3. stages database ``verify`` at the exact source release, seeds it or not
4. ``tsupgrade apply`` with the verification acknowledgement
5. reads ``extversion`` back and, on a seeded run, runs ``check.sql``
6. writes the evidence record (``verify/evidence/``), full log in ``verify/out/``

Failures are recorded and the run goes on. A failure to stage or seed is the
harness's or the environment's, not the row's, so it gets no record -- it is
reported and logged only.

One row database at a time: staging drops ``verify`` first, and every other
database in the cluster has TimescaleDB dropped, because preflight inspects
every database and another row's leftovers would block an unrelated row.

The cluster is disposable: the harness deletes and recreates its namespace.
It refuses to touch ``tsdb-upgrader``, the long-lived ``make up`` cluster.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))

from tsupgrade.contents import ImageContents, load_contents  # noqa: E402
from tsupgrade.evidence import (  # noqa: E402
    EvidenceError, build_record, load_record, record_path, upgrader_revision, write_record)
from tsupgrade.matrix import MatrixEntry, load_matrix  # noqa: E402
from tsupgrade.preflight import patroni_role  # noqa: E402
from tsupgrade.versions import InvalidVersion, parse_version  # noqa: E402

DATABASE = "verify"
NAMESPACE = "timescale"
CONTAINER = "timescaledb"
STATEFULSET = "tsdb"
SELECTOR = "application=patroni,cluster-name=tsdb"
PROTECTED_CLUSTERS = ("tsdb-upgrader",)
CHECKS = (
    "version", "rows", "still_compressed", "cagg_survived",
    "cagg_matches", "write_and_refresh", "decompress",
)
MODES = ("empty", "seeded")
FIXTURE = REPO / "verify" / "fixture"
EVIDENCE = REPO / "verify" / "evidence"
OUT = REPO / "verify" / "out"
# Server messages arrive bare from -c and as `psql:<stdin>:44: ERROR:` from -f.
ERROR_LINE = re.compile(r"(^|: )(ERROR|FATAL|DETAIL|HINT|CONTEXT|PANIC):|error:|refused|^\s+- ")


class HarnessError(Exception):
    """The harness or the environment failed; nothing about the row."""


# ---------------------------------------------------------------------------
# Resolving a row to images and exact releases (no cluster, no network)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """A matrix row, made concrete."""

    entry: MatrixEntry
    start_image: str     # the image the source is staged on
    target_image: str
    source: str          # exact release staged
    target: str          # exact release asked for
    start_pg: str        # exact server version of start_image
    target_pg: str
    candidates: Tuple[str, ...] = ()  # same-major images offered to plan as --via

    @property
    def major(self) -> int:
        return int(self.entry.source_pg)

    @property
    def transition(self) -> Dict[str, str]:
        e = self.entry
        return {"source_pg": e.source_pg, "source_ts": e.source_ts,
                "target_pg": e.target_pg, "target_ts": e.target_ts}

    @property
    def slug(self) -> str:
        return "pg{0}/{1}-to-{2}".format(self.major, self.entry.source_ts, self.entry.target_ts)


@dataclass(frozen=True)
class Unrunnable:
    entry: MatrixEntry
    reason: str


def _key(version: str) -> Tuple[int, ...]:
    return parse_version(version)


def _tag_major(tag: str) -> Optional[int]:
    m = re.match(r"pg(\d+)", tag)
    return int(m.group(1)) if m else None


def _newest_in(installable: Sequence[str], series: str) -> Optional[str]:
    want = _key(series)[:2]
    hits = [v for v in installable if _key(v)[:2] == want]
    return max(hits, key=_key) if hits else None


def _images_for(contents: ImageContents, major: int) -> List[Tuple[str, List[str], str]]:
    """(reference, installable, server_version) of every measured image whose
    own tag is this PostgreSQL major. A multi-PG image is only used for the
    major it is tagged with, which is the one its PATH starts."""
    found = []
    for tag, entry in sorted(contents.images.items()):
        if _tag_major(tag) != major:
            continue
        per = (entry.get("pg") or {}).get(str(major)) or {}
        if per.get("installable") and per.get("server_version"):
            found.append(("{0}:{1}".format(contents.repository, tag),
                          list(per["installable"]), per["server_version"]))
    return found


def resolve(entry: MatrixEntry, contents: ImageContents) -> Union[Run, Unrunnable]:
    """Pick images and exact releases for a row.

    The target is the newest patch of the target series on the image that
    ships the newest one. The source is staged on that same image when it
    ships the source series (one image, no rollout); otherwise on the
    same-major image that ships it and reaches furthest, and ``plan`` finds
    the bridge. Each series resolves to its newest patch on the image used.
    """
    if entry.source_pg != entry.target_pg:
        return Unrunnable(entry, "crosses PostgreSQL majors; out of scope")
    try:
        major = int(entry.source_pg)
    except ValueError:
        return Unrunnable(entry, "source_pg {0!r} is not a major".format(entry.source_pg))
    images = _images_for(contents, major)

    targets = [(ref, inst, sv, _newest_in(inst, entry.target_ts)) for ref, inst, sv in images]
    targets = [t for t in targets if t[3]]
    if not targets:
        return Unrunnable(entry, "no measured pg{0} image ships {1}.x".format(major, entry.target_ts))
    t_ref, t_inst, t_sv, target = max(targets, key=lambda t: (_key(t[3]), _key(t[2])))

    source = _newest_in(t_inst, entry.source_ts)
    if source is not None:
        s_ref, s_sv = t_ref, t_sv
    else:
        starts = [(ref, inst, sv, _newest_in(inst, entry.source_ts)) for ref, inst, sv in images]
        starts = [s for s in starts if s[3]]
        if not starts:
            return Unrunnable(entry, "no measured pg{0} image ships {1}.x".format(major, entry.source_ts))
        s_ref, s_inst, s_sv, source = max(
            starts, key=lambda s: (_key(max(s[1], key=_key)), _key(s[2]))
        )
    if _key(source) >= _key(target):
        return Unrunnable(entry, "source {0} is not below target {1}".format(source, target))
    candidates = tuple(ref for ref, _, _ in images if ref != t_ref)
    return Run(entry, s_ref, t_ref, source, target, s_sv, t_sv, candidates)


def select_rows(
    entries: Sequence[MatrixEntry],
    majors: Sequence[str] = (),
    sources: Sequence[str] = (),
) -> List[MatrixEntry]:
    rows = [e for e in entries if e.source_pg == e.target_pg]
    if majors:
        rows = [e for e in rows if e.source_pg in majors]
    if sources:
        rows = [e for e in rows if e.source_ts in sources]
    return rows


def run_order(runs: Sequence[Run]) -> List[Run]:
    """Group by first image so the cluster is rebuilt as rarely as possible;
    single-image rows first, since a bridged row leaves the cluster on
    another image."""
    return sorted(runs, key=lambda r: (
        r.major, r.start_image, r.start_image != r.target_image, _key(r.source)))


# ---------------------------------------------------------------------------
# Judging a run (pure)
# ---------------------------------------------------------------------------


def parse_checks(output: str) -> Dict[str, Tuple[str, str]]:
    """``check|name|pass|detail`` lines -> {name: (status, detail)}."""
    found: Dict[str, Tuple[str, str]] = {}
    for line in output.splitlines():
        parts = line.strip().split("|", 3)
        if len(parts) >= 3 and parts[0] == "check":
            found[parts[1]] = (parts[2], parts[3] if len(parts) > 3 else "")
    return found


def error_lines(lines: Sequence[str], limit: int = 12) -> str:
    """The lines worth quoting verbatim; all of them if none look like errors."""
    picked = [l for l in lines if ERROR_LINE.search(l)]
    return "\n".join((picked or [l for l in lines if l.strip()])[:limit])


def judge(
    run: Run,
    mode: str,
    apply_rc: int,
    apply_stderr: str,
    report: Optional[Dict[str, Any]],
    reached: Optional[str],
    checks: Optional[Dict[str, Tuple[str, str]]],
    check_stderr: str = "",
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """(passed, explanation, error, stage). ``stage`` is where it stopped.

    A failed check quotes its own line and whatever the server said while
    check.sql ran: the check line says what was wrong, the server says why.
    """
    if report is None:
        lines = apply_stderr.splitlines()
        if any(l.startswith("refused") for l in lines):
            return (False, "apply refused before writing anything",
                    error_lines(lines) or "refused", "apply")
        return (False, "apply exited {0} without a report".format(apply_rc),
                error_lines(lines) or "exit {0}".format(apply_rc), "apply")
    if report.get("result") != "pass":
        f = report.get("failure") or {}
        explanation = "hop {0} {1} failed: {2}".format(
            f.get("hop", "?"), f.get("action", "?"), f.get("summary", "no summary"))
        return (False, explanation,
                error_lines(f.get("error") or []) or f.get("summary") or "failed", "apply")
    if reached != run.target:
        return (False,
                "apply reported success but the database is at {0}, not {1}".format(reached, run.target),
                "extversion {0}".format(reached), "verify")
    if mode == "seeded":
        checks = checks or {}
        bad = [c for c in CHECKS if checks.get(c, ("missing",))[0] != "pass"]
        if bad:
            detail = ["check|{0}|{1}|{2}".format(c, *checks[c]) if c in checks
                      else "check|{0}|missing|printed nothing (errored)".format(c) for c in bad]
            server = [l for l in check_stderr.splitlines() if ERROR_LINE.search(l)]
            return (False, "fixture checks failed after the upgrade: " + ", ".join(bad),
                    "\n".join(detail + server[:8]), "check")
    return (True, None, None, "done")


def images_used(run: Run, plan: Optional[Dict[str, Any]]) -> List[str]:
    """The start image, then every image the plan rolls onto, in order."""
    seen = [run.start_image]
    for step in (plan or {}).get("steps") or []:
        image = step.get("image")
        if step.get("action") == "ensure_image" and image and image not in seen:
            seen.append(image)
    if plan is None and run.target_image not in seen:
        seen.append(run.target_image)
    return seen


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def write(self, text: str) -> None:
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")

    def section(self, title: str) -> None:
        self.write("\n== {0}  [{1}]".format(title, _now()))


def _run(argv: Sequence[str], input: Optional[str] = None, timeout: float = 900,
         env: Optional[Dict[str, str]] = None, log: Optional[Log] = None) -> Tuple[int, str, str]:
    if log:
        log.write("$ " + " ".join(argv))
    try:
        done = subprocess.run(list(argv), input=input, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        out = "timed out after {0:.0f}s".format(timeout)
        if log:
            log.write(out)
        return 124, (exc.stdout or "") if isinstance(exc.stdout, str) else "", out
    except FileNotFoundError as exc:
        raise HarnessError("cannot run {0}: {1}".format(argv[0], exc)) from exc
    if log:
        if done.stdout.strip():
            log.write(done.stdout)
        if done.stderr.strip():
            log.write(done.stderr)
        log.write("(exit {0})".format(done.returncode))
    return done.returncode, done.stdout, done.stderr


def tsupgrade(args: Sequence[str], log: Log, timeout: float = 3600) -> Tuple[int, str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TOOLS) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return _run([sys.executable, "-m", "tsupgrade"] + list(args), env=env, log=log, timeout=timeout)


def local_digest(reference: str, contents: ImageContents) -> Optional[str]:
    """The repository digest of the local image -- what was loaded into kind."""
    rc, out, _ = _run(["docker", "image", "inspect", "--format",
                       "{{json .RepoDigests}}", reference])
    if rc == 0:
        repo = reference.rsplit(":", 1)[0]
        for item in json.loads(out.strip() or "[]") or []:
            if item.startswith(repo + "@"):
                return item.split("@", 1)[1]
    entry = contents.entry(reference) or {}
    return entry.get("digest")


class Cluster:
    """The disposable kind cluster the rows run in."""

    def __init__(self, name: str, replicas: int, contents: ImageContents) -> None:
        if name in PROTECTED_CLUSTERS:
            raise HarnessError(
                "refusing to use kind cluster {0!r}: it is the long-lived `make up` "
                "cluster, and the harness deletes namespaces".format(name))
        self.name = name
        self.context = "kind-" + name
        self.replicas = replicas
        self.contents = contents
        bash = os.environ.get("HARNESS_BASH") or shutil.which("bash")
        if not bash:
            raise HarnessError("bash not found on PATH (set HARNESS_BASH)")
        self.bash = bash

    def kubectl(self, *args: str, input: Optional[str] = None, timeout: float = 120,
                log: Optional[Log] = None) -> Tuple[int, str, str]:
        return _run(["kubectl", "--context", self.context, "-n", NAMESPACE] + list(args),
                    input=input, timeout=timeout, log=log)

    def exists(self) -> bool:
        rc, out, _ = _run(["kind", "get", "clusters"])
        return rc == 0 and self.name in out.split()

    def pods(self) -> List[Dict[str, Any]]:
        rc, out, _ = self.kubectl("get", "pods", "-l", SELECTOR, "-o", "json")
        return json.loads(out).get("items", []) if rc == 0 else []

    @staticmethod
    def _ready(pod: Dict[str, Any]) -> bool:
        return any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in pod.get("status", {}).get("conditions") or [])

    @staticmethod
    def _image(pod: Dict[str, Any]) -> Optional[str]:
        for c in pod.get("spec", {}).get("containers") or []:
            if c.get("name") == CONTAINER:
                return c.get("image")
        return None

    def leader(self) -> Optional[str]:
        for pod in self.pods():
            if patroni_role(pod["metadata"].get("labels")) == "primary" and self._ready(pod):
                return pod["metadata"]["name"]
        return None

    def on(self, image: str) -> bool:
        pods = self.pods()
        return (len(pods) == self.replicas
                and all(self._ready(p) and self._image(p) == image for p in pods)
                and self.leader() is not None)

    def loaded(self, image: str) -> bool:
        rc, _, _ = _run(["docker", "exec", self.name + "-control-plane",
                         "crictl", "inspecti", image])
        return rc == 0

    def load(self, image: str, log: Log) -> None:
        if self.loaded(image):
            return
        log.section("load " + image)
        if _run(["docker", "image", "inspect", image])[0] != 0:
            if _run(["docker", "pull", image], timeout=3600, log=log)[0] != 0:
                raise HarnessError("cannot pull " + image)
        if _run(["kind", "load", "docker-image", image, "--name", self.name],
                timeout=3600, log=log)[0] == 0:
            return
        # With Docker's containerd image store, a multi-platform image is
        # saved as its whole index but only this platform's layers exist, and
        # kind's import (--all-platforms) dies on "content digest ... not
        # found". Saving just this platform imports fine.
        rc, platform, _ = _run(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"])
        if rc == 0 and self._import(image, platform.strip(), log):
            return
        raise HarnessError("cannot load {0} into kind cluster {1}".format(image, self.name))

    def _import(self, image: str, platform: str, log: Log) -> bool:
        save = ["docker", "save", "--platform", platform, image]
        load = ["docker", "exec", "-i", self.name + "-control-plane", "ctr", "--namespace=k8s.io",
                "images", "import", "--digests", "--snapshotter=overlayfs", "-"]
        log.write("$ {0} | {1}".format(" ".join(save), " ".join(load)))
        saver = subprocess.Popen(save, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        loader = subprocess.run(load, stdin=saver.stdout, capture_output=True, text=True,
                                errors="replace", timeout=3600)
        saver.stdout.close()
        save_err = saver.stderr.read().decode("utf-8", "replace")
        saver.wait(timeout=60)
        log.write((save_err + loader.stderr + loader.stdout)[-2000:] or "(no output)")
        log.write("(exit {0}/{1})".format(saver.returncode, loader.returncode))
        return saver.returncode == 0 and loader.returncode == 0 and self.loaded(image)

    def ensure(self, image: str, log: Log) -> None:
        """Leave the cluster healthy on ``image``, TimescaleDB in no database
        but the row's. Isolation runs every time: a healthy cluster says
        nothing about whether an earlier setup got that far."""
        if self.exists() and self.on(image):
            self.isolate(log)
            return
        log.section("reset cluster onto " + image)
        if not self.exists():
            # Created here rather than by up.sh, so the image is side-loaded
            # before any pod starts pulling it from the registry.
            rc, _, _ = _run(["kind", "create", "cluster", "--config",
                             str(REPO / "cluster" / "kind-config.yaml"),
                             "--name", self.name, "--wait", "120s"], timeout=600, log=log)
            if rc != 0:
                raise HarnessError("cannot create kind cluster " + self.name)
        self.load(image, log)
        self.kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found",
                     "--wait=true", "--timeout=600s", timeout=660, log=log)
        env = dict(os.environ, KIND_CLUSTER=self.name, TSDB_IMAGE=image,
                   REPLICAS=str(self.replicas), APP_DB="postgres", NO_COLOR="1",
                   MSYS_NO_PATHCONV="1")
        up = (REPO / "scripts" / "up.sh").as_posix()
        rc, _, _ = _run([self.bash, up], env=env, timeout=1800, log=log)
        if rc != 0:
            raise HarnessError("scripts/up.sh failed on {0} (see log)".format(image))
        if not self.on(image):
            raise HarnessError("cluster did not settle on {0}".format(image))
        self.isolate(log)

    def psql(self, database: str, *args: str, sql: Optional[str] = None,
             stdin: Optional[str] = None, log: Optional[Log] = None,
             timeout: float = 600) -> Tuple[int, str, str]:
        pod = self.leader()
        if pod is None:
            return 1, "", "no ready leader pod"
        argv = ["exec", "-i", pod, "-c", CONTAINER, "--", "psql", "-U", "postgres",
                "-X", "-v", "ON_ERROR_STOP=1", "-d", database] + list(args)
        if sql is not None:
            argv += ["-c", sql]
        return self.kubectl(*argv, input=stdin, timeout=timeout, log=log)

    def isolate(self, log: Log) -> None:
        """Drop TimescaleDB from every database, so the row database is the
        only one preflight and apply see with it installed."""
        rc, out, _ = self.psql("postgres", "-qtA", sql=(
            "SELECT datname FROM pg_database WHERE datallowconn ORDER BY 1"), log=log)
        if rc != 0:
            raise HarnessError("cannot list databases")
        for db in out.split():
            if db != DATABASE:
                if self.psql(db, "-q", sql="DROP EXTENSION IF EXISTS timescaledb CASCADE",
                             log=log)[0] != 0:
                    raise HarnessError("cannot drop timescaledb from " + db)


# ---------------------------------------------------------------------------
# One row
# ---------------------------------------------------------------------------


def make_plan(run: Run, path: Path, log: Log) -> Tuple[Optional[Dict[str, Any]], str]:
    log.section("plan")
    args = ["plan", "--from-pg", run.start_pg, "--from-ts", run.source,
            "--target-image", run.target_image, "--target-ts", run.target,
            "--verification-run", "--output", str(path)]
    for ref in run.candidates:
        args += ["--via", ref]
    rc, _, err = tsupgrade(args, log, timeout=300)
    if rc == 0 and path.exists():
        return json.loads(path.read_text(encoding="utf-8")), err
    return None, err


def stage(cluster: Cluster, run: Run, mode: str, log: Log) -> Optional[str]:
    """Database ``verify`` at exactly ``run.source``; returns the seed baseline."""
    log.section("stage {0} at {1} ({2})".format(DATABASE, run.source, mode))
    steps = [
        ("postgres", ["-q", "-c",
                      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = '{0}' AND pid <> pg_backend_pid()".format(DATABASE),
                      "-c", "DROP DATABASE IF EXISTS {0}".format(DATABASE),
                      "-c", "CREATE DATABASE {0}".format(DATABASE)]),
        # Separate sessions: up to at least 2.1, the library DROP loaded stays
        # loaded, and CREATE of another version in that session is refused.
        (DATABASE, ["-q", "-c", "DROP EXTENSION IF EXISTS timescaledb CASCADE"]),
        (DATABASE, ["-q", "-c", "CREATE EXTENSION timescaledb VERSION '{0}'".format(run.source)]),
    ]
    for db, args in steps:
        rc, _, err = cluster.psql(db, *args, log=log)
        if rc != 0:
            raise HarnessError("staging {0} failed: {1}".format(
                run.source, error_lines(err.splitlines(), 3)))
    if extversion(cluster, log) != run.source:
        raise HarnessError("staging did not leave {0} at {1}".format(DATABASE, run.source))
    if mode != "seeded":
        return None
    log.section("seed")
    seed = (FIXTURE / "seed.sql").read_text(encoding="utf-8")
    rc, out, err = cluster.psql(DATABASE, "-qtA", "-f", "-", stdin=seed, log=log)
    if rc != 0:
        raise HarnessError("seeding at {0} failed: {1}".format(
            run.source, error_lines(err.splitlines(), 3)))
    lines = [l for l in out.splitlines() if l.strip()]
    return lines[-1] if lines else None


def extversion(cluster: Cluster, log: Log) -> Optional[str]:
    rc, out, _ = cluster.psql(DATABASE, "-qtA", sql=(
        "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"), log=log)
    return (out.strip() or None) if rc == 0 else None


def server_version(cluster: Cluster, log: Log) -> Optional[str]:
    rc, out, _ = cluster.psql("postgres", "-qtA", sql="SHOW server_version", log=log)
    return out.split()[0] if rc == 0 and out.strip() else None


def run_one(run: Run, mode: str, plan: Dict[str, Any], plan_path: Path, cluster: Cluster,
            contents: ImageContents, revision: Dict[str, Any]) -> Dict[str, Any]:
    out_dir = OUT / "pg{0}".format(run.major)
    stem = "{0}-to-{1}.{2}".format(run.entry.source_ts, run.entry.target_ts, mode)
    log = Log(out_dir / (stem + ".log"))
    started = _now()
    log.write("row {0}  mode {1}  source {2} on {3}  target {4} on {5}".format(
        run.slug, mode, run.source, run.start_image, run.target, run.target_image))
    images = images_used(run, plan)
    try:
        cluster.ensure(run.start_image, log)
        for image in images:
            cluster.load(image, log)
        baseline = stage(cluster, run, mode, log)
    except HarnessError as exc:
        log.write("HARNESS ERROR: {0}".format(exc))
        return {"row": run.slug, "mode": mode, "result": "error", "detail": str(exc),
                "log": str(log.path)}
    start_pg = server_version(cluster, log) or run.start_pg

    log.section("apply")
    report_path = out_dir / (stem + ".report.json")
    if report_path.exists():
        report_path.unlink()
    apply_rc, _, apply_err = tsupgrade(
        ["apply", str(plan_path), "--acknowledge-verification-run",
         "--context", cluster.context, "--namespace", NAMESPACE, "--selector", SELECTOR,
         "--database", DATABASE, "--report", str(report_path)], log)
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None

    log.section("after")
    reached = extversion(cluster, log)
    pg_version = server_version(cluster, log)
    checks, check_err = None, ""
    if mode == "seeded" and reached is not None:
        log.section("check.sql, expect {0}".format(run.target))
        rc, out, check_err = cluster.psql(DATABASE, "-qtA", "-F", "|", "-v", "expect=" + run.target,
                                  "-f", "-", stdin=(FIXTURE / "check.sql").read_text(encoding="utf-8"),
                                  log=log)
        checks = parse_checks(out)
    passed, explanation, error, stopped = judge(
        run, mode, apply_rc, apply_err, report, reached, checks, check_err)

    final = (report or {}).get("final") or {}
    record = build_record(
        transition=run.transition, mode=mode,
        images=[{"reference": i, "digest": local_digest(i, contents)} for i in images],
        pg_version=pg_version or start_pg, source=run.source, target=run.target,
        reached=reached, passed=passed, log=log.path.relative_to(REPO).as_posix(),
        explanation=explanation, error=error, upgrader=revision,
        stage=stopped, started_at=started, finished_at=_now(),
        source_pg_version=start_pg, replicas=cluster.replicas,
        pods=final.get("instances"), baseline=baseline,
        checks={k: {"status": s, "detail": d} for k, (s, d) in checks.items()} if checks else None,
        plan={"hops": plan.get("hops"), "path": plan_path.relative_to(REPO).as_posix()},
        apply_exit=apply_rc,
    )
    path = record_path(EVIDENCE, run.transition, mode)
    write_record(path, record)
    log.write("\nRESULT {0}{1}".format(record["result"].upper(),
                                       ": " + explanation if explanation else ""))
    return {"row": run.slug, "mode": mode, "result": record["result"],
            "detail": explanation or "", "record": str(path), "log": str(log.path)}


def refused_record(run: Run, mode: str, plan_err: str, plan_log: Log,
                   contents: ImageContents, revision: Dict[str, Any]) -> Dict[str, Any]:
    """`plan` refused the row: that is the tool's answer, recorded as such."""
    images = images_used(run, None)
    record = build_record(
        transition=run.transition, mode=mode,
        images=[{"reference": i, "digest": local_digest(i, contents)} for i in images],
        pg_version=run.start_pg, source=run.source, target=run.target, reached=None,
        passed=False, log=plan_log.path.relative_to(REPO).as_posix(),
        explanation="plan refused; nothing ran against a cluster",
        error=error_lines(plan_err.splitlines()) or "refused", upgrader=revision,
        stage="plan", recorded_without_cluster=True,
    )
    path = record_path(EVIDENCE, run.transition, mode)
    write_record(path, record)
    return {"row": run.slug, "mode": mode, "result": "fail",
            "detail": "plan refused", "record": str(path), "log": str(plan_log.path)}


def recorded(run: Run, mode: str, root: Path = EVIDENCE) -> bool:
    """A valid record from a clean tree already exists for this row and mode."""
    try:
        doc = load_record(record_path(root, run.transition, mode))
    except EvidenceError:
        return False
    return doc["upgrader"].get("dirty") is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pg", action="append", default=[], help="PostgreSQL major; repeatable")
    ap.add_argument("--from", action="append", default=[], dest="sources",
                    help="source series, e.g. 2.13; repeatable")
    ap.add_argument("--mode", choices=MODES + ("both",), default="both")
    ap.add_argument("--cluster", default="tsdb-verify", help="kind cluster name")
    ap.add_argument("--replicas", type=int, default=2)
    ap.add_argument("--list", action="store_true", help="show the resolved rows and stop")
    ap.add_argument("--resume", action="store_true",
                    help="skip rows and modes that already have a record from a clean tree")
    args = ap.parse_args(argv)

    contents = load_contents()
    rows = select_rows(load_matrix().entries, args.pg, args.sources)
    resolved = [resolve(e, contents) for e in rows]
    runs = run_order([r for r in resolved if isinstance(r, Run)])
    skipped = [r for r in resolved if isinstance(r, Unrunnable)]

    for run in runs:
        print("{0:<18} {1:>7} -> {2:<7} on {3}{4}".format(
            run.slug, run.source, run.target, run.start_image.split(":")[-1],
            "" if run.start_image == run.target_image
            else " then " + run.target_image.split(":")[-1]))
    for skip in skipped:
        e = skip.entry
        print("{0:<18} skipped: {1}".format(
            "pg{0}/{1}-to-{2}".format(e.source_pg, e.source_ts, e.target_ts), skip.reason))
    if args.list or not runs:
        return 0

    modes = MODES if args.mode == "both" else (args.mode,)
    revision = upgrader_revision(REPO)
    if revision.get("dirty"):
        print("note: working tree is dirty; records will say so", file=sys.stderr)
    cluster = Cluster(args.cluster, args.replicas, contents)
    outcomes: List[Dict[str, Any]] = []
    for run in runs:
        todo = [m for m in modes if not (args.resume and recorded(run, m))]
        if not todo:
            print("{0:<18} already recorded".format(run.slug), flush=True)
            continue
        out_dir = OUT / "pg{0}".format(run.major)
        stem = "{0}-to-{1}".format(run.entry.source_ts, run.entry.target_ts)
        plan_log = Log(out_dir / (stem + ".plan.log"))
        plan_path = out_dir / (stem + ".plan.json")
        plan, plan_err = make_plan(run, plan_path, plan_log)
        for mode in todo:
            t0 = time.monotonic()
            if plan is None:
                outcome = refused_record(run, mode, plan_err, plan_log, contents, revision)
            else:
                outcome = run_one(run, mode, plan, plan_path, cluster, contents, revision)
            outcome["seconds"] = round(time.monotonic() - t0)
            outcomes.append(outcome)
            print("{0:<18} {1:<6} {2:<5} {3:>4}s  {4}".format(
                run.slug, mode, outcome["result"].upper(), outcome["seconds"],
                outcome["detail"]), flush=True)
            with open(OUT / "runs.jsonl", "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(dict(outcome, at=_now())) + "\n")

    counts = {k: sum(1 for o in outcomes if o["result"] == k) for k in ("pass", "fail", "error")}
    print("\n{pass} pass, {fail} fail, {error} harness error(s)".format(**counts))
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
