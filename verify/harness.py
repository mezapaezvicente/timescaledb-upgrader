#!/usr/bin/env python3
"""Verification harness: run matrix rows with the upgrader itself.

    python verify/harness.py --list [--pg 15]
    python verify/harness.py --pg 15 [--from 2.13] [--mode both] [--cluster tsdb-verify]
    python verify/harness.py --pg 15 --from 2.20 --history 2.13.1 [--control direct]
    python verify/harness.py --pg 12 --from 2.11 --to-pg 16 [--link]

For each matrix row (same-PG, or with ``--to-pg`` the ones changing to that
major) it picks images from the measured contents
(``data/image-contents.json``), resolves the row's series to exact releases,
and then, once per mode (empty, seeded):

1. ``tsupgrade plan`` -- offline; a refusal is the row's result, no cluster
2. brings the kind cluster onto the row's first image (a fresh namespace when
   it runs anything else, or after a major change, which leaves directories
   on the disk) and loads every image the plan names
3. stages database ``verify`` at the exact source release, seeds it or not
4. ``tsupgrade apply`` with the verification acknowledgement
5. reads ``extversion`` back and, on a seeded run, runs ``check.sql``
6. writes the evidence record (``verify/evidence/``), full log in ``verify/out/``

``--history V`` stages the source with an upgrade history instead: created at
release V (and seeded there), then updated to the source with one ALTER
EXTENSION. Its records are ``history-empty`` and ``history-seeded``. Some
TimescaleDB bugs only exist for a database that got to its version that way.

``--control direct`` (with ``--history``) is a negative control: after
staging, the update runs as a single ALTER EXTENSION to the target instead of
``plan`` and ``apply``. It shows what a step the planner adds protects
against, and its records (``direct-history-*``) never verify a row. Only a
row that runs on one image can be controlled this way.

A row that changes the PostgreSQL major is staged on an image of the source
major and planned with every measured image carrying a major in between;
``plan`` routes it through ``pg_upgrade``. ``--link`` plans ``pg_upgrade
--link`` instead of copying; its records are ``link-empty`` and
``link-seeded``, and a verified major change needs them as well.

Images run by digest, as ``plan`` pins them: the cluster is staged on
``image@sha256:...`` and every record names the route it ran (``route``:
the start image, then each update and ``pg_upgrade``, pinned), which is what
the matrix row cites and what ``plan`` reproduces for a verified row.
``--write-routes`` copies each verified row's route from its records into
the matrix, and refuses when the records disagree. In kind, a side-loaded
image is known by its tag only, so after loading the harness also names it
by its registry digest, once Docker confirms that the local image is the one
pulled from that digest.

``--rollback`` (a copy-mode major change only) is evidence for
``tsupgrade rollback``, not for the row: after ``apply`` passes, the run goes
back from the last major upgrade, and the version and the fixture checks are
judged against the *source*. Its records are ``rollback-empty`` and
``rollback-seeded``, and no matrix row cites them.

Every run passes ``apply --use-cached-images``: the images are side-loaded, so
a registry that does not answer is the network, not the row's result. The flags
themselves get their own runs, with no download and no registry: ``--mirror``
names the loaded images on ``MIRROR``, a host that cannot resolve, and runs the
whole upgrade from it (records ``mirror-empty``, ``mirror-seeded``);
``--mirror-refused`` is the same without ``--use-cached-images``, and passes only
if ``apply`` stops at its first preflight, says what can be done, and changes
nothing (``mirror-refused-empty``, ``mirror-refused-seeded``). No row cites them.

Every run also passes ``--migrate-caggs``, so a route that reaches 2.25 with the
old-format aggregate of a pre-2.7 seed migrates it on the way, and the checks
judge the result. ``--caggs-refused`` is the same without it, and passes only if
``apply`` stops before changing anything and names the aggregate
(``caggs-refused-seeded``). No row cites it.

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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))

from tsupgrade.contents import ImageContents, load_contents  # noqa: E402
from tsupgrade.evidence import (  # noqa: E402
    EvidenceError, build_record, evidence_key, load_record, record_path, staleness,
    upgrader_revision, write_record)
from tsupgrade.matrix import MatrixEntry, default_matrix_path, load_matrix, route_problems  # noqa: E402
from tsupgrade.images import mirrored, node_image_name  # noqa: E402
from tsupgrade.plan import pinned, route_of  # noqa: E402
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
    "cagg_matches", "write_and_refresh", "decompress", "jobs",
)
MODES = ("empty", "seeded")
CONTROLS = ("direct",)
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
    history: Optional[str] = None     # created at this release, then updated to source
    control: Optional[str] = None     # "direct": one ALTER instead of plan/apply
    link: bool = False                # pg_upgrade --link on a major change
    rollback: bool = False            # then tsupgrade rollback; ends at the source
    mirror: bool = False              # from MIRROR, which cannot answer, on cached images
    refused: bool = False             # the same without --use-cached-images: must stop
    caggs_refused: bool = False       # without --migrate-caggs: must stop, nothing changed

    @property
    def major(self) -> int:
        return int(self.entry.source_pg)

    def mode(self, base: str) -> str:
        """The record mode for a base mode (empty or seeded)."""
        if self.link:
            return "link-" + base
        if self.rollback:
            return "rollback-" + base
        if self.refused:
            return "mirror-refused-" + base
        if self.caggs_refused:
            return "caggs-refused-" + base
        if self.mirror:
            return "mirror-" + base
        return "history-" + base if self.history else base

    @property
    def end(self) -> str:
        """The release the run must end at: the target, or after a rollback
        the source again, or the source still if it had to stop."""
        return self.source if self.rollback or self.refused or self.caggs_refused else self.target

    @property
    def cross(self) -> bool:
        return self.entry.source_pg != self.entry.target_pg

    def key(self, base: str) -> str:
        """What the record is filed and cited as."""
        return evidence_key(self.mode(base), self.control)

    @property
    def transition(self) -> Dict[str, str]:
        e = self.entry
        return {"source_pg": e.source_pg, "source_ts": e.source_ts,
                "target_pg": e.target_pg, "target_ts": e.target_ts}

    @property
    def stem(self) -> str:
        """``<source_ts>-to-<target_ts>``, naming the target major on a major change."""
        e = self.entry
        target = "pg{0}-{1}".format(e.target_pg, e.target_ts) if self.cross else e.target_ts
        return "{0}-to-{1}".format(e.source_ts, target)

    @property
    def slug(self) -> str:
        return "pg{0}/{1}".format(self.major, self.stem)


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
    try:
        major, target_major = int(entry.source_pg), int(entry.target_pg)
    except ValueError:
        return Unrunnable(entry, "{0!r} -> {1!r} are not majors".format(entry.source_pg, entry.target_pg))
    if target_major != major:
        return _resolve_cross(entry, contents, major, target_major)
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


def _resolve_cross(entry: MatrixEntry, contents: ImageContents, major: int,
                   target_major: int) -> Union[Run, Unrunnable]:
    """A major change: the target as for any row, on the target major; the
    source on the source-major image reaching furthest that ships it; and as
    candidates every measured image carrying any major in between, which is
    where ``plan`` finds the ``pg_upgrade`` images."""
    if target_major < major:
        return Unrunnable(entry, "a PostgreSQL downgrade")
    targets = [(ref, inst, sv, _newest_in(inst, entry.target_ts))
               for ref, inst, sv in _images_for(contents, target_major)]
    targets = [t for t in targets if t[3]]
    if not targets:
        return Unrunnable(entry, "no measured pg{0} image ships {1}.x".format(
            target_major, entry.target_ts))
    t_ref, _, t_sv, target = max(targets, key=lambda t: (_key(t[3]), _key(t[2])))
    starts = [(ref, inst, sv, _newest_in(inst, entry.source_ts))
              for ref, inst, sv in _images_for(contents, major)]
    starts = [s for s in starts if s[3]]
    if not starts:
        return Unrunnable(entry, "no measured pg{0} image ships {1}.x".format(major, entry.source_ts))
    s_ref, _, s_sv, source = max(starts, key=lambda s: (_key(s[3]), _key(max(s[1], key=_key)),
                                                       _key(s[2])))
    candidates = tuple(
        "{0}:{1}".format(contents.repository, tag)
        for tag, item in sorted(contents.images.items())
        if any(major <= int(m) <= target_major for m in (item.get("pg") or {}))
        and "{0}:{1}".format(contents.repository, tag) != t_ref)
    return Run(entry, s_ref, t_ref, source, target, s_sv, t_sv, candidates)


def with_link(run: Run) -> Union[Run, Unrunnable]:
    """The same major change, with pg_upgrade --link."""
    if not run.cross:
        return Unrunnable(run.entry, "--link only applies to a PostgreSQL major change")
    if run.history or run.control:
        return Unrunnable(run.entry, "--link is not combined with --history or --control")
    return replace(run, link=True)


def with_mirror(run: Run, refused: bool = False) -> Union[Run, Unrunnable]:
    """The same copy-mode major change, from a registry that cannot answer."""
    if not run.cross:
        return Unrunnable(run.entry, "--mirror only applies to a PostgreSQL major change")
    if run.link or run.rollback or run.history or run.control:
        return Unrunnable(run.entry, "--mirror is for a copy, without --history or --control")
    return replace(run, mirror=True, refused=refused)


def with_caggs_refused(run: Run) -> Union[Run, Unrunnable]:
    """The same route without --migrate-caggs: it has to stop before anything."""
    if not run.cross or run.link or run.rollback or run.mirror or run.history or run.control:
        return Unrunnable(run.entry, "--caggs-refused is for a copy-mode major change alone")
    return replace(run, caggs_refused=True)


def with_rollback(run: Run) -> Union[Run, Unrunnable]:
    """The same copy-mode major change, then back with tsupgrade rollback."""
    if not run.cross:
        return Unrunnable(run.entry, "--rollback only applies to a PostgreSQL major change")
    if run.link or run.history or run.control:
        return Unrunnable(run.entry, "--rollback is for a copy, without --history or --control")
    return replace(run, rollback=True)


def with_history(run: Run, version: str, contents: ImageContents) -> Union[Run, Unrunnable]:
    """The same row, staged by creating at ``version`` and updating to the source.

    Staging happens on the start image, so it has to ship both releases.
    """
    try:
        if len(_key(version)) < 3:
            raise InvalidVersion(version)
    except InvalidVersion:
        return Unrunnable(run.entry, "--history needs an exact release, got {0!r}".format(version))
    if _key(version) >= _key(run.source):
        return Unrunnable(run.entry, "history {0} is not below source {1}".format(version, run.source))
    profile = contents.profile(run.start_image, run.major)
    if profile is None or version not in profile.installable:
        return Unrunnable(run.entry, "{0} does not ship {1}".format(
            run.start_image.split(":")[-1], version))
    return replace(run, history=version)


def as_control(run: Run, control: str) -> Union[Run, Unrunnable]:
    """The same row as a negative control: one ALTER, no planner."""
    if control not in CONTROLS:
        return Unrunnable(run.entry, "unknown control {0!r}".format(control))
    if run.history is None:
        return Unrunnable(run.entry, "a control is only recorded with --history")
    if run.start_image != run.target_image:
        return Unrunnable(run.entry, "a direct control needs one image; this row rolls out "
                          + run.target_image.split(":")[-1])
    return replace(run, control=control)


def select_rows(
    entries: Sequence[MatrixEntry],
    majors: Sequence[str] = (),
    sources: Sequence[str] = (),
    to_pgs: Sequence[str] = (),
) -> List[MatrixEntry]:
    """Same-PG rows; with ``to_pgs``, the rows changing to those majors instead."""
    if to_pgs:
        rows = [e for e in entries if e.target_pg in to_pgs and e.source_pg != e.target_pg]
    else:
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


def judge_refusal(report: Optional[Dict[str, Any]], pg_version: Optional[str],
                  run: Run) -> Optional[Tuple[bool, str, str, str]]:
    """A run that had to stop: None if it did, as it should. It must stop at its
    first preflight, say what can be done, and leave the PostgreSQL it started on."""
    failure = (report or {}).get("failure") or {}
    said = "\n".join(failure.get("error") or [])
    wanted = ["does not mean an image is missing", "Running without the registry is possible",
              "--image-registry", "--use-cached-images"]
    if (failure.get("action") != "preflight" or failure.get("hop") != 1
            or "route_images" not in str(failure.get("summary"))):
        return (False, "apply did not stop at its first preflight on the images",
                str(failure.get("summary") or (report or {}).get("result")), "apply")
    missing = [w for w in wanted if w not in said]
    if missing:
        return (False, "apply stopped without saying what can be done", ", ".join(missing), "apply")
    if pg_version != run.start_pg:
        return (False, "apply stopped but PostgreSQL changed",
                "{0}, was {1}".format(pg_version, run.start_pg), "verify")
    return None


def judge_cagg_refusal(apply_stderr: str, report: Optional[Dict[str, Any]],
                       pg_version: Optional[str], run: Run) -> Optional[Tuple[bool, str, str, str]]:
    """A run that had to stop for old aggregates: None if it did, as it should.
    It must stop before writing, say so, and name what it found."""
    if report is not None:
        return (False, "apply got past the check for old continuous aggregates",
                str(report.get("result")), "apply")
    wanted = ["read-only check", "Nothing has been changed", "public.fx_hourly", "--migrate-caggs",
              "built on public.fx_metrics"]
    missing = [w for w in wanted if w not in apply_stderr]
    if missing:
        return (False, "apply stopped without saying what it found", ", ".join(missing), "apply")
    if pg_version != run.start_pg:
        return (False, "apply stopped but PostgreSQL changed",
                "{0}, was {1}".format(pg_version, run.start_pg), "verify")
    return None


def judge(
    run: Run,
    mode: str,
    apply_rc: int,
    apply_stderr: str,
    report: Optional[Dict[str, Any]],
    reached: Optional[str],
    checks: Optional[Dict[str, Tuple[str, str]]],
    check_stderr: str = "",
    back: Optional[Dict[str, Any]] = None,
    pg_version: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """(passed, explanation, error, stage). ``stage`` is where it stopped.

    A failed check quotes its own line and whatever the server said while
    check.sql ran: the check line says what was wrong, the server says why.
    """
    if run.caggs_refused or run.refused:
        refusal = (judge_cagg_refusal(apply_stderr, report, pg_version, run) if run.caggs_refused
                   else judge_refusal(report, pg_version, run))
        if refusal:
            return refusal
        report = {"result": "pass"}
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
    if run.rollback and (back or {}).get("result") != "pass":
        failure = (back or {}).get("failure") or {}
        return (False, "rollback failed: {0}".format(failure.get("summary") or "no result"),
                error_lines(failure.get("error") or []) or "rollback failed", "rollback")
    if reached != run.end:
        return (False,
                "{0} reported success but the database is at {1}, not {2}".format(
                    "rollback" if run.rollback else "apply", reached, run.end),
                "extversion {0}".format(reached), "verify")
    if mode == "seeded":
        checks = checks or {}
        bad = [c for c in CHECKS if checks.get(c, ("missing",))[0] != "pass"]
        if bad:
            detail = ["check|{0}|{1}|{2}".format(c, *checks[c]) if c in checks
                      else "check|{0}|missing|printed nothing (errored)".format(c) for c in bad]
            server = [l for l in check_stderr.splitlines() if ERROR_LINE.search(l)]
            return (False, "fixture checks failed after the {0}: ".format(
                "rollback" if run.rollback else "upgrade") + ", ".join(bad),
                    "\n".join(detail + server[:8]), "check")
    return (True, None, None, "done")


def images_used(run: Run, plan: Optional[Dict[str, Any]]) -> List[str]:
    """The start image, then every image the plan runs, in order: rollouts,
    and for a major change the pg_upgrade image and the one restarted on."""
    seen = [run.start_image]
    for step in (plan or {}).get("steps") or []:
        if step.get("action") == "ensure_image":
            names = [step.get("image")]
        elif step.get("action") == "major_upgrade":
            names = [step.get("image"), step.get("run_image")]
        else:
            continue
        for image in names:
            if image and image not in seen:
                seen.append(image)
    if plan is None and run.target_image not in seen:
        seen.append(run.target_image)
    return seen


def major_upgrades(report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What each pg_upgrade did with the relation files, from apply's report:
    its own "Copying"/"Linking" line, and how many files share an inode with
    the old directory (none after a copy)."""
    found = []
    for step in (report or {}).get("steps") or []:
        if step.get("action") != "major_upgrade":
            continue
        majors = re.search(r"PostgreSQL (\d+) -> (\d+)", step.get("summary") or "")
        hop: Dict[str, Any] = {"from_pg": int(majors.group(1)) if majors else None,
                               "to_pg": int(majors.group(2)) if majors else None,
                               "files": None, "shared_inodes": None}
        for line in step.get("output") or []:
            line = line.strip()
            # pg_upgrade 15+ ends the line with its "ok"; 13 prints that apart.
            said = re.match(r"(Copying|Linking) user relation files\b", line)
            if said:
                hop["files"] = {"Copying": "copy", "Linking": "link"}[said.group(1)]
            elif line.startswith("relation files sharing an inode"):
                count = line.rsplit(":", 1)[-1].strip()
                hop["shared_inodes"] = int(count) if count.isdigit() else None
        found.append(hop)
    return found


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


def pin(reference: str, contents: ImageContents) -> str:
    """The reference with its measured digest, as plan pins it."""
    return pinned(reference, (contents.entry(reference) or {}).get("digest"))


def apply_arguments(run: Run, plan_path: Path, report_path: Path, context: str) -> List[str]:
    """``tsupgrade apply`` for a run. The images are side-loaded, so a registry
    that does not answer says nothing about the row."""
    args = ["apply", str(plan_path), "--acknowledge-verification-run",
            "--context", context, "--namespace", NAMESPACE, "--selector", SELECTOR,
            "--database", DATABASE, "--report", str(report_path)]
    if run.mirror:
        args += ["--image-registry", MIRROR]
    if not run.refused:
        args.append("--use-cached-images")
    if not run.caggs_refused:
        args.append("--migrate-caggs")
    return args


def containerd_name(reference: str) -> str:
    """How containerd names an image: registry and ``library/`` spelled out."""
    head, slash, tail = reference.partition("/")
    if slash and ("." in head or ":" in head or head == "localhost"):
        return reference
    return "docker.io/" + (reference if slash else "library/" + reference)


def record_route(run: Run, plan: Optional[Dict[str, Any]],
                 contents: ImageContents) -> Optional[Dict[str, Any]]:
    """The route a run's plan took, as a matrix row cites it; None when it
    cannot be pinned all the way (an image without a measured digest)."""
    if not plan:
        return None
    route = {"start": {"image": run.start_image,
                       "digest": (contents.entry(run.start_image) or {}).get("digest")},
             "steps": route_of(plan.get("steps") or [])}
    return None if route_problems(route) else route


def docker_digest(reference: str) -> Optional[str]:
    """The registry digest Docker pulled the local image from, if it says."""
    rc, out, _ = _run(["docker", "image", "inspect", "--format",
                       "{{json .RepoDigests}}", reference])
    if rc == 0:
        repo = reference.rsplit(":", 1)[0]
        for item in json.loads(out.strip() or "[]") or []:
            if item.startswith(repo + "@"):
                return item.split("@", 1)[1]
    return None


def local_digest(reference: str, contents: ImageContents) -> Optional[str]:
    """The repository digest of the local image -- what was loaded into kind."""
    return docker_digest(reference) or (contents.entry(reference) or {}).get("digest")


# A name that can never resolve (RFC 2606), so the mirror cannot answer.
MIRROR = "mirror.invalid/timescale"


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
        # A major change leaves directories on ordinal 0's disk (pg_upgrade's
        # old one, or rollback's set-aside); a run after it starts on a fresh
        # namespace even when the cluster looks healthy on the right image,
        # as it does after a rollback.
        self.used = False

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
        """Side-load ``image`` (``tag`` or ``tag@digest``) into the node."""
        if self.loaded(image):
            return
        tag, _, digest = image.partition("@")
        if not self.loaded(tag):
            self._load(tag, log)
        if digest:
            self._alias(tag, digest, log)

    def name_on_mirror(self, image: str, contents: ImageContents, log: Log) -> None:
        """Name a loaded image on ``MIRROR``, so it runs from there unpulled."""
        reference = pin(image, contents)
        _, _, digest = reference.partition("@")
        name = node_image_name(mirrored(reference, MIRROR))
        rc, _, err = _run(["docker", "exec", self.name + "-control-plane", "ctr",
                           "--namespace=k8s.io", "images", "tag", "--force",
                           containerd_name(image), name], log=log)
        if rc != 0 or not digest:
            raise HarnessError("cannot name {0} on {1}: {2}".format(image, MIRROR, err or "no digest"))

    def await_listed(self, references: Sequence[str], timeout: float = 300) -> None:
        """Wait until the node lists these images: its list lags (measured),
        and a registry outage meanwhile would look like an image not there."""
        wanted = {node_image_name(r) for r in references}
        deadline = time.monotonic() + timeout
        while True:
            rc, out, _ = self.kubectl("get", "nodes", "-o", "json")
            names = {n for node in (json.loads(out).get("items") or [] if rc == 0 else [])
                     for i in node.get("status", {}).get("images") or [] for n in i.get("names") or []}
            if rc == 0 and wanted <= names:
                return
            if time.monotonic() > deadline:
                raise HarnessError("the node does not list {0}".format(", ".join(sorted(wanted - names))))
            time.sleep(5)

    def _alias(self, tag: str, digest: str, log: Log) -> None:
        """Name the loaded tag by its registry digest, so a pinned reference
        resolves on the node without a pull. Only for the image Docker
        pulled from that digest."""
        if docker_digest(tag) != digest:
            raise HarnessError("the local {0} is not {1}; pull it again".format(tag, digest))
        repo = containerd_name(tag).rsplit(":", 1)[0]
        rc, _, err = _run(["docker", "exec", self.name + "-control-plane", "ctr",
                           "--namespace=k8s.io", "images", "tag", "--force",
                           containerd_name(tag), "{0}@{1}".format(repo, digest)], log=log)
        if rc != 0 or not self.loaded("{0}@{1}".format(tag, digest)):
            raise HarnessError("cannot name {0} by {1} in kind: {2}".format(tag, digest, err))

    def _load(self, image: str, log: Log) -> None:
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
        if self.exists() and not self.used and self.on(image):
            self.isolate(log)
            return
        log.section("reset cluster onto " + image)
        self.used = False
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
    if run.link:
        args.append("--link")
    for ref in run.candidates:
        args += ["--via", ref]
    rc, _, err = tsupgrade(args, log, timeout=300)
    if rc == 0 and path.exists():
        return json.loads(path.read_text(encoding="utf-8")), err
    return None, err


def stage(cluster: Cluster, run: Run, mode: str, log: Log) -> Optional[str]:
    """Database ``verify`` at exactly ``run.source``; returns the seed baseline.

    With a history the database is created (and seeded) at ``run.history``
    and then updated to the source in a session of its own.
    """
    created = run.history or run.source
    log.section("stage {0} at {1} ({2}{3})".format(
        DATABASE, run.source, mode, ", created at " + run.history if run.history else ""))
    steps = [
        ("postgres", ["-q", "-c",
                      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = '{0}' AND pid <> pg_backend_pid()".format(DATABASE),
                      "-c", "DROP DATABASE IF EXISTS {0}".format(DATABASE),
                      "-c", "CREATE DATABASE {0}".format(DATABASE)]),
        # Separate sessions: up to at least 2.1, the library DROP loaded stays
        # loaded, and CREATE of another version in that session is refused.
        (DATABASE, ["-q", "-c", "DROP EXTENSION IF EXISTS timescaledb CASCADE"]),
        (DATABASE, ["-q", "-c", "CREATE EXTENSION timescaledb VERSION '{0}'".format(created)]),
    ]
    for db, args in steps:
        rc, _, err = cluster.psql(db, *args, log=log)
        if rc != 0:
            raise HarnessError("staging {0} failed: {1}".format(
                created, error_lines(err.splitlines(), 3)))
    if extversion(cluster, log) != created:
        raise HarnessError("staging did not leave {0} at {1}".format(DATABASE, created))
    baseline = None
    if mode == "seeded":
        log.section("seed")
        seed = (FIXTURE / "seed.sql").read_text(encoding="utf-8")
        rc, out, err = cluster.psql(DATABASE, "-qtA", "-f", "-", stdin=seed, log=log)
        if rc != 0:
            raise HarnessError("seeding at {0} failed: {1}".format(
                created, error_lines(err.splitlines(), 3)))
        lines = [l for l in out.splitlines() if l.strip()]
        baseline = lines[-1] if lines else None
    if run.history:
        log.section("history: update {0} -> {1}".format(run.history, run.source))
        rc, _, err = cluster.psql(DATABASE, "-q", sql=(
            "ALTER EXTENSION timescaledb UPDATE TO '{0}'".format(run.source)), log=log)
        if rc != 0 or extversion(cluster, log) != run.source:
            raise HarnessError("history update {0} -> {1} failed: {2}".format(
                run.history, run.source, error_lines(err.splitlines(), 3)))
    return baseline


def extversion(cluster: Cluster, log: Log) -> Optional[str]:
    rc, out, _ = cluster.psql(DATABASE, "-qtA", sql=(
        "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"), log=log)
    return (out.strip() or None) if rc == 0 else None


def server_version(cluster: Cluster, log: Log) -> Optional[str]:
    rc, out, _ = cluster.psql("postgres", "-qtA", sql="SHOW server_version", log=log)
    return out.split()[0] if rc == 0 and out.strip() else None


def direct_update(cluster: Cluster, run: Run, log: Log) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """The control's whole upgrade: one ALTER, first statement of a fresh session.

    Answers like ``apply`` does, so the run is judged by the same rules: a
    report when the update went through, none (and the server's error) when
    it did not.
    """
    log.section("direct update {0} -> {1} (control; no plan, no apply)".format(run.source, run.target))
    rc, _, err = cluster.psql(DATABASE, "-q", sql=(
        "ALTER EXTENSION timescaledb UPDATE TO '{0}'".format(run.target)), log=log)
    return rc, ({"result": "pass"} if rc == 0 else None), err


def run_one(run: Run, base: str, plan: Optional[Dict[str, Any]], plan_path: Optional[Path],
            cluster: Cluster, contents: ImageContents, revision: Dict[str, Any]) -> Dict[str, Any]:
    mode, key = run.mode(base), run.key(base)
    out_dir = OUT / "pg{0}".format(run.major)
    stem = "{0}.{1}".format(run.stem, key)
    log = Log(out_dir / (stem + ".log"))
    started = _now()
    log.write("row {0}  mode {1}  source {2} on {3}  target {4} on {5}{6}{7}".format(
        run.slug, mode, run.source, run.start_image, run.target, run.target_image,
        "  created at " + run.history if run.history else "",
        "  CONTROL " + run.control if run.control else ""))
    images = images_used(run, plan)
    try:
        cluster.ensure(pin(run.start_image, contents), log)
        for image in images:
            cluster.load(pin(image, contents), log)
            if run.mirror:
                cluster.name_on_mirror(image, contents, log)
        cluster.await_listed([mirrored(pin(image, contents), MIRROR if run.mirror else None)
                              for image in images])
        cluster.used = run.cross
        baseline = stage(cluster, run, base, log)
    except HarnessError as exc:
        log.write("HARNESS ERROR: {0}".format(exc))
        return {"row": run.slug, "mode": key, "result": "error", "detail": str(exc),
                "log": str(log.path)}
    start_pg = server_version(cluster, log) or run.start_pg

    back: Optional[Dict[str, Any]] = None  # tsupgrade rollback's result, on a rollback run
    if run.control:
        apply_rc, report, apply_err = direct_update(cluster, run, log)
    else:
        log.section("apply")
        report_path = out_dir / (stem + ".report.json")
        if report_path.exists():
            report_path.unlink()
        apply_rc, _, apply_err = tsupgrade(apply_arguments(run, plan_path, report_path, cluster.context), log)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
        if run.rollback and report and report.get("result") == "pass":
            log.section("rollback")
            rb_rc, rb_out, _ = tsupgrade(
                ["rollback", "--report", str(report_path), "--discard-writes-since-upgrade",
                 "--context", cluster.context, "--namespace", NAMESPACE, "--selector", SELECTOR,
                 "--json"], log)
            try:
                back = json.loads(rb_out)
            except ValueError:
                back = {"result": "fail", "failure": {"summary": "rollback exited {0} without a "
                                                                 "report".format(rb_rc)}}

    log.section("after")
    reached = extversion(cluster, log)
    pg_version = server_version(cluster, log)
    checks, check_err = None, ""
    if base == "seeded" and reached is not None:
        log.section("check.sql, expect {0}".format(run.end))
        rc, out, check_err = cluster.psql(DATABASE, "-qtA", "-F", "|", "-v", "expect=" + run.end,
                                  "-f", "-", stdin=(FIXTURE / "check.sql").read_text(encoding="utf-8"),
                                  log=log)
        checks = parse_checks(out)
    passed, explanation, error, stopped = judge(
        run, base, apply_rc, apply_err, report, reached, checks, check_err, back, pg_version)
    if run.control and report is None:
        explanation = "the direct update {0} -> {1} failed".format(run.source, run.target)

    final = (report or {}).get("final") or {}
    extra: Dict[str, Any] = {}
    if run.history:
        extra["history"] = run.history
    if run.control:
        extra["control"] = run.control
    else:
        extra["plan"] = {"hops": plan.get("hops"), "path": plan_path.relative_to(REPO).as_posix()}
        route = record_route(run, plan, contents)
        if route is not None:
            extra["route"] = route
        if run.cross and not run.refused and not run.caggs_refused:
            extra["major_upgrades"] = major_upgrades(report)
        if run.mirror:
            extra["image_registry"] = MIRROR
        if run.rollback:
            extra["forward"] = {"target": run.target, "apply_exit": apply_rc}
            extra["rollback"] = back
    record = build_record(
        transition=run.transition, mode=mode,
        images=[{"reference": i, "digest": local_digest(i, contents)} for i in images],
        pg_version=pg_version or start_pg, source=run.source, target=run.end,
        reached=reached, passed=passed, log=log.path.relative_to(REPO).as_posix(),
        explanation=explanation, error=error, upgrader=revision,
        stage=stopped, started_at=started, finished_at=_now(),
        source_pg_version=start_pg, replicas=cluster.replicas,
        pods=final.get("instances"), baseline=baseline,
        checks={k: {"status": s, "detail": d} for k, (s, d) in checks.items()} if checks else None,
        apply_exit=apply_rc, **extra,
    )
    path = record_path(EVIDENCE, run.transition, key)
    write_record(path, record)
    log.write("\nRESULT {0}{1}".format(record["result"].upper(),
                                       ": " + explanation if explanation else ""))
    return {"row": run.slug, "mode": key, "result": record["result"],
            "detail": explanation or "", "record": str(path), "log": str(log.path)}


def refused_record(run: Run, base: str, plan_err: str, plan_log: Log,
                   contents: ImageContents, revision: Dict[str, Any]) -> Dict[str, Any]:
    """`plan` refused the row: that is the tool's answer, recorded as such."""
    mode, key = run.mode(base), run.key(base)
    extra = {"history": run.history} if run.history else {}
    images = images_used(run, None)
    record = build_record(
        transition=run.transition, mode=mode,
        images=[{"reference": i, "digest": local_digest(i, contents)} for i in images],
        pg_version=run.start_pg, source=run.source, target=run.target, reached=None,
        passed=False, log=plan_log.path.relative_to(REPO).as_posix(),
        explanation="plan refused; nothing ran against a cluster",
        error=error_lines(plan_err.splitlines()) or "refused", upgrader=revision,
        stage="plan", recorded_without_cluster=True, **extra,
    )
    path = record_path(EVIDENCE, run.transition, key)
    write_record(path, record)
    return {"row": run.slug, "mode": key, "result": "fail",
            "detail": "plan refused", "record": str(path), "log": str(plan_log.path)}


def recorded(run: Run, base: str, root: Path = EVIDENCE, git=None) -> bool:
    """A valid record already exists for this row and mode that still
    speaks for this checkout: clean, and no change to the code that decides
    a run since its commit (:func:`evidence.staleness`)."""
    try:
        doc = load_record(record_path(root, run.transition, run.key(base)))
    except EvidenceError:
        return False
    return staleness(doc["upgrader"], REPO, git=git) is None


def routes_for(entries: Sequence[MatrixEntry], root: Path = REPO) -> Tuple[Dict[int, Dict[str, Any]], List[str]]:
    """{entry index: route} for every verified in_place row whose cited
    records (empty, seeded and, on a major change, the link runs) all carry
    the same route; and a line for each row they do not settle."""
    routes: Dict[int, Dict[str, Any]] = {}
    problems: List[str] = []
    for i, entry in enumerate(entries):
        if not (entry.is_known and entry.verdict == "in_place" and entry.evidence):
            continue
        keys = [k for k in ("empty", "seeded", "link-empty", "link-seeded") if k in entry.evidence]
        try:
            found = [load_record(root / entry.evidence[k]).get("route") for k in keys]
        except EvidenceError as exc:
            problems.append("{0}: {1}".format(entry.transition, exc))
            continue
        if any(r is None for r in found):
            problems.append("{0}: not every record names its route ({1})".format(
                entry.transition, ", ".join(k for k, r in zip(keys, found) if r is None)))
        elif any(r != found[0] for r in found[1:]):
            problems.append("{0}: its records ran different routes".format(entry.transition))
        else:
            routes[i] = found[0]
    return routes, problems


def write_routes(path: Optional[Path] = None) -> int:
    """Copy each verified row's route from its records into the matrix."""
    path = path or default_matrix_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    routes, problems = routes_for(load_matrix(path).entries)
    changed = 0
    for i, row in enumerate(doc["entries"]):
        if i in routes and row.get("route") != routes[i]:
            row["route"] = routes[i]
            changed += 1
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    load_matrix(path)
    for line in problems:
        print("no route: " + line)
    print("{0} row(s) updated; {1} verified row(s) carry a route; {2} without".format(
        changed, len(routes), len(problems)))
    return 0


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
                    help="skip rows and modes whose record still speaks for this checkout: "
                         "clean, and no change since to the code that decides a run")
    ap.add_argument("--history", metavar="RELEASE",
                    help="create the database at this release, then update it to the source")
    ap.add_argument("--to-pg", action="append", default=[], dest="to_pgs",
                    help="run the rows that change to this PostgreSQL major; repeatable")
    ap.add_argument("--link", action="store_true",
                    help="on a major change, pg_upgrade --link instead of copying")
    ap.add_argument("--rollback", action="store_true",
                    help="on a copy-mode major change, then tsupgrade rollback; evidence for "
                         "rollback, judged against the source, never a row's verdict")
    ap.add_argument("--mirror", action="store_true",
                    help="on a copy-mode major change, run from a registry that cannot answer, on "
                         "cached images; evidence for those flags, never a row's verdict")
    ap.add_argument("--mirror-refused", action="store_true", dest="mirror_refused",
                    help="the same without --use-cached-images: passes only if apply stops first")
    ap.add_argument("--caggs-refused", action="store_true", dest="caggs_refused",
                    help="without --migrate-caggs: passes only if apply stops before changing anything")
    ap.add_argument("--control", choices=CONTROLS,
                    help="negative control: one ALTER EXTENSION instead of plan/apply "
                         "(needs --history)")
    ap.add_argument("--write-routes", action="store_true", dest="write_routes",
                    help="copy each verified row's route from its records into the matrix, and stop")
    args = ap.parse_args(argv)

    if args.write_routes:
        return write_routes()
    contents = load_contents()
    rows = select_rows(load_matrix().entries, args.pg, args.sources, args.to_pgs)
    resolved = [resolve(e, contents) for e in rows]
    if args.link:
        resolved = [with_link(r) if isinstance(r, Run) else r for r in resolved]
    if args.rollback:
        resolved = [with_rollback(r) if isinstance(r, Run) else r for r in resolved]
    if args.mirror or args.mirror_refused:
        resolved = [with_mirror(r, args.mirror_refused) if isinstance(r, Run) else r for r in resolved]
    if args.caggs_refused:
        resolved = [with_caggs_refused(r) if isinstance(r, Run) else r for r in resolved]
    if args.history:
        resolved = [with_history(r, args.history, contents) if isinstance(r, Run) else r
                    for r in resolved]
    if args.control:
        resolved = [as_control(r, args.control) if isinstance(r, Run) else r for r in resolved]
    runs = run_order([r for r in resolved if isinstance(r, Run)])
    skipped = [r for r in resolved if isinstance(r, Unrunnable)]

    for run in runs:
        print("{0:<18} {1:>7} -> {2:<7} on {3}{4}{5}{6}{7}".format(
            run.slug, run.source, run.target, run.start_image.split(":")[-1],
            "" if run.start_image == run.target_image
            else " then " + run.target_image.split(":")[-1],
            "  (created at {0})".format(run.history) if run.history else "",
            "  CONTROL {0}".format(run.control) if run.control else "",
            "  --link" if run.link else "  then rollback" if run.rollback
            else "  must stop" if run.refused or run.caggs_refused else "  from " + MIRROR if run.mirror else ""))
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
        plan: Optional[Dict[str, Any]] = None
        plan_path: Optional[Path] = None
        if not run.control:
            out_dir = OUT / "pg{0}".format(run.major)
            stem = run.stem + (".link" if run.link else "")
            plan_log = Log(out_dir / (stem + ".plan.log"))
            plan_path = out_dir / (stem + ".plan.json")
            plan, plan_err = make_plan(run, plan_path, plan_log)
        for mode in todo:
            t0 = time.monotonic()
            if plan is None and not run.control:
                outcome = refused_record(run, mode, plan_err, plan_log, contents, revision)
            else:
                outcome = run_one(run, mode, plan, plan_path, cluster, contents, revision)
            outcome["seconds"] = round(time.monotonic() - t0)
            outcomes.append(outcome)
            print("{0:<18} {1:<6} {2:<5} {3:>4}s  {4}".format(
                run.slug, outcome["mode"], outcome["result"].upper(), outcome["seconds"],
                outcome["detail"]), flush=True)
            with open(OUT / "runs.jsonl", "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(dict(outcome, at=_now())) + "\n")

    counts = {k: sum(1 for o in outcomes if o["result"] == k) for k in ("pass", "fail", "error")}
    print("\n{pass} pass, {fail} fail, {error} harness error(s)".format(**counts))
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
