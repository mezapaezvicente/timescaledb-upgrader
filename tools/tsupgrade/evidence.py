"""Evidence records: what one verification run actually did.

A matrix row is verified only by running that migration with this tool, once
on an empty database and once on a seeded one. Each of those runs leaves one
record here. Records are evidence, not verdicts: nothing in this module maps a
result onto the matrix's verdict enum. That mapping is decided once the runs
have shown which failures actually occur, and not before.

So the format is deliberately loose. :func:`validate_record` insists on the
fields every run must carry and on their shape, and ignores anything else, so
a harness can add what turns out to matter without a schema change:

``transition``      the matrix row: ``{source_pg, source_ts, target_pg,
                    target_ts}`` with the row's own (series-level) keys
``mode``            ``empty`` or ``seeded``; ``history-empty`` or
                    ``history-seeded`` when the database was created at an
                    older release and updated to the source before the run
                    (``history`` then names that release); ``link-empty`` or
                    ``link-seeded`` when a PostgreSQL major change ran
                    ``pg_upgrade --link`` instead of copying; ``rollback-empty``
                    or ``rollback-seeded`` when a copy-mode major change was
                    then undone with ``tsupgrade rollback``: evidence for the
                    rollback, never a row's verdict. ``versions.target`` is
                    then the source, where the run must end. ``mirror-empty``
                    or ``mirror-seeded`` when a major change ran with
                    ``--image-registry`` and ``--use-cached-images`` against a
                    registry that cannot answer; ``mirror-refused-empty`` or
                    ``mirror-refused-seeded`` when the same run without
                    ``--use-cached-images`` stopped before changing anything,
                    which is its pass. Evidence for those flags, never a row's
                    verdict. ``caggs-refused-seeded`` when a route reaching 2.25
                    without ``--migrate-caggs`` stopped before changing
                    anything, which is its pass; never a row's verdict
``images``          every image the run used, in order, each ``{reference,
                    digest}`` -- a two-image row lists both
``pg_version``      the exact server version the extension ran on
                    (``15.18``, not ``15``); TimescaleDB 2.20+ refuses some
                    minors, so a row is only proven at the minor it ran on
``versions``        ``{source, target, reached}``: the exact release staged,
                    the one asked for, and ``extversion`` afterwards
                    (``reached`` may be null if the run never got that far)
``upgrader``        ``{commit, dirty}`` of this repository
``result``          ``pass`` or ``fail``
``failure``         on ``fail`` only: ``{explanation, error}`` -- a short plain
                    account of what went wrong, and the relevant error lines
                    copied verbatim (not the whole log)
``log``             path to the full log
``major_upgrades``  on a passing PostgreSQL major change only: one
                    ``{from_pg, to_pg, files, shared_inodes}`` per pg_upgrade,
                    where ``files`` is ``copy`` or ``link`` as pg_upgrade
                    itself reported it and ``shared_inodes`` counts the new
                    cluster's relation files hard-linked to the old directory.
                    A ``link-`` record must show ``link`` and a count above 0;
                    any other must show ``copy`` and 0

``route``           optional; when present, the route the run's plan took, in
                    the shape a matrix row cites it
                    (:func:`matrix.route_problems`): the start image, then
                    every update and ``pg_upgrade``, each image pinned by
                    digest. A row's ``route`` is copied from its records

Optional and unchecked beyond being present: ``checks`` (the fixture's check
lines on a seeded run), ``started_at``/``finished_at``, ``plan``, ``notes``.

``control: "direct"`` marks a negative control: the update ran as one
``ALTER EXTENSION`` instead of through ``plan``/``apply``, to show what a
step the planner adds is protecting against. It is never a verification of
the row. Its file is keyed ``direct-<mode>`` (:func:`evidence_key`).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .matrix import route_problems
from .versions import InvalidVersion, parse_version

__all__ = [
    "EVIDENCE_FORMAT",
    "MODES",
    "CONTROLS",
    "RESULTS",
    "evidence_key",
    "EvidenceError",
    "build_record",
    "validate_record",
    "write_record",
    "load_record",
    "record_path",
    "staleness",
    "upgrader_revision",
    "VERDICT_CODE",
]

# What decides how a verification run goes. A record made before a change to
# any of these ran code that no longer ships. Of the scripts, only the one
# apply runs: rollback_swap.sh runs only after a route has finished.
VERDICT_CODE = (
    "tools/tsupgrade/plan.py", "tools/tsupgrade/apply.py", "tools/tsupgrade/majors.py",
    "tools/tsupgrade/drift.py", "tools/tsupgrade/gates.py", "tools/tsupgrade/preflight.py",
    "tools/tsupgrade/steps.py", "tools/tsupgrade/scripts/pg_upgrade.sh",
)

EVIDENCE_FORMAT = 1
MODES = ("empty", "seeded", "history-empty", "history-seeded", "link-empty", "link-seeded",
         "rollback-empty", "rollback-seeded", "mirror-empty", "mirror-seeded",
         "mirror-refused-empty", "mirror-refused-seeded", "caggs-refused-seeded")
CONTROLS = ("direct",)
RESULTS = ("pass", "fail")


def evidence_key(mode: str, control: Optional[str] = None) -> str:
    """The name a record goes by in a file name and a matrix row's evidence."""
    return "{0}-{1}".format(control, mode) if control else mode


class EvidenceError(Exception):
    """A record is malformed, or could not be written or read."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upgrader_revision(repo: Optional[Path] = None) -> Dict[str, Any]:
    """This repository's commit, and whether the working tree differs from it.

    A dirty tree is recorded, not refused: a fix under test is exactly when a
    run matters. It does mean the commit alone does not reproduce the run.
    The records themselves (``verify/evidence/``) do not count: a run that
    has just written some would otherwise call every later run dirty.
    """
    root = repo or Path(__file__).resolve().parents[2]

    def git(*args: str) -> Optional[str]:
        try:
            done = subprocess.run(
                ["git", "-C", str(root)] + list(args),
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--", ".", ":(exclude)verify/evidence")
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        done = subprocess.run(["git", "-C", str(root)] + list(args),
                              capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def staleness(upgrader: Any, repo: Optional[Path] = None,
              git: Optional[Callable[..., Optional[str]]] = None) -> Optional[str]:
    """Why a record no longer speaks for the code in this checkout, or None.

    It does when it ran from a dirty tree, from a commit this history does
    not have, or from one followed by a change to :data:`VERDICT_CODE`.
    """
    root = repo or Path(__file__).resolve().parents[2]
    git = git or (lambda *args: _git(root, *args))
    commit = (upgrader or {}).get("commit") if isinstance(upgrader, dict) else None
    if not isinstance(upgrader, dict) or upgrader.get("dirty") is not False or not commit:
        return "not from a clean tree"
    changed = git("log", "--format=%h", commit + "..HEAD", "--", *VERDICT_CODE)
    if changed is None:
        return "commit {0} is not in this history".format(commit[:8])
    if changed:
        return "ran {0}; changed since by {1}".format(commit[:8], " ".join(changed.split()))
    return None


def build_record(
    *,
    transition: Dict[str, str],
    mode: str,
    images: Sequence[Dict[str, Optional[str]]],
    pg_version: str,
    source: str,
    target: str,
    reached: Optional[str],
    passed: bool,
    log: str,
    explanation: Optional[str] = None,
    error: Optional[str] = None,
    upgrader: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Assemble a record and validate it; raises :class:`EvidenceError`."""
    doc: Dict[str, Any] = {
        "evidence_format": EVIDENCE_FORMAT,
        "transition": dict(transition),
        "mode": mode,
        "images": [dict(i) for i in images],
        "pg_version": pg_version,
        "versions": {"source": source, "target": target, "reached": reached},
        "upgrader": upgrader if upgrader is not None else upgrader_revision(),
        "result": "pass" if passed else "fail",
        "log": log,
        "recorded_at": _now(),
    }
    if not passed:
        doc["failure"] = {"explanation": explanation, "error": error}
    doc.update(extra)
    problems = validate_record(doc)
    if problems:
        raise EvidenceError("; ".join(problems))
    return doc


def _exact(version: object, parts: int) -> bool:
    try:
        return len(parse_version(version)) >= parts
    except InvalidVersion:
        return False


def validate_record(doc: Any) -> List[str]:
    """Every problem with ``doc``, or an empty list. Extra keys are fine."""
    if not isinstance(doc, dict):
        return ["record is not an object"]
    problems: List[str] = []

    transition = doc.get("transition")
    if not isinstance(transition, dict) or not all(
        transition.get(k) for k in ("source_pg", "source_ts", "target_pg", "target_ts")
    ):
        problems.append("transition needs source_pg, source_ts, target_pg, target_ts")

    if doc.get("mode") not in MODES:
        problems.append("mode must be one of {0}".format(", ".join(MODES)))
    elif str(doc["mode"]).startswith("history-") and not _exact(doc.get("history"), 3):
        problems.append("a history run must name the release it was created at in history")
    if "control" in doc and doc["control"] not in CONTROLS:
        problems.append("control must be one of {0}".format(", ".join(CONTROLS)))

    images = doc.get("images")
    if not isinstance(images, list) or not images:
        problems.append("images must list every image the run used")
    else:
        for i, image in enumerate(images):
            if not isinstance(image, dict) or not image.get("reference"):
                problems.append("images[{0}] needs a reference".format(i))
            elif not str(image.get("digest") or "").startswith("sha256:"):
                problems.append("images[{0}] needs a sha256 digest".format(i))

    if not _exact(doc.get("pg_version"), 2):
        problems.append(
            "pg_version must be an exact server version such as 15.18, got {0!r}".format(
                doc.get("pg_version")
            )
        )

    versions = doc.get("versions")
    if not isinstance(versions, dict):
        problems.append("versions needs source, target and reached")
    else:
        for key in ("source", "target"):
            if not _exact(versions.get(key), 3):
                problems.append(
                    "versions.{0} must be an exact release, got {1!r}".format(
                        key, versions.get(key)
                    )
                )
        if "reached" not in versions:
            problems.append("versions.reached must be present (null if never read)")

    upgrader = doc.get("upgrader")
    if not isinstance(upgrader, dict) or "commit" not in upgrader or "dirty" not in upgrader:
        problems.append("upgrader needs commit and dirty")

    result = doc.get("result")
    if result not in RESULTS:
        problems.append("result must be pass or fail")
    elif result == "fail":
        failure = doc.get("failure")
        if not isinstance(failure, dict) or not failure.get("explanation") or not failure.get("error"):
            problems.append("a failed run needs failure.explanation and failure.error")
    elif "failure" in doc:
        problems.append("a passing run carries no failure")
    elif result == "pass" and isinstance(versions, dict) and versions.get("reached") != versions.get("target"):
        problems.append("a passing run must reach its target version")

    if (result == "pass" and isinstance(transition, dict) and "control" not in doc
            and not str(doc.get("mode")).startswith(("mirror-refused-", "caggs-refused-"))
            and transition.get("source_pg") != transition.get("target_pg")):
        problems.extend(_major_upgrade_problems(doc))

    if "route" in doc:
        problems.extend(route_problems(doc["route"]))

    if not doc.get("log"):
        problems.append("log must point at the full log")
    return problems


def _major_upgrade_problems(doc: Dict[str, Any]) -> List[str]:
    """A passing major change shows what each pg_upgrade did with the
    relation files, and that matches the mode: a ``link-`` run linked them
    (files share an inode with the old directory), any other run copied."""
    hops = doc.get("major_upgrades")
    if not isinstance(hops, list) or not hops:
        return ["a passing major change lists its pg_upgrade runs in major_upgrades"]
    link = str(doc.get("mode")).startswith("link-")
    want = "link" if link else "copy"
    problems = []
    for i, hop in enumerate(hops):
        shared = hop.get("shared_inodes") if isinstance(hop, dict) else None
        if not isinstance(hop, dict) or hop.get("files") != want or not isinstance(shared, int):
            problems.append("major_upgrades[{0}] must show pg_upgrade's {1} of the relation "
                            "files and a hard-link count".format(i, want))
        elif (shared > 0) != link:
            problems.append("major_upgrades[{0}]: {1} relation files share an inode with the old "
                            "directory, which does not fit a {2}".format(i, shared, want))
    return problems


def record_path(root: Path, transition: Dict[str, str], mode: str) -> Path:
    """``<root>/pg<major>/<source_ts>-to-<target_ts>.<mode>.json``.

    A row that changes the PostgreSQL major names the target major too
    (``2.11-to-pg16-2.30``), since several share a source and a series.
    ``mode`` is the record's :func:`evidence_key`, which differs from its
    ``mode`` field only for a control.

    One file per row and mode, overwritten by a re-run; git keeps the earlier
    ones, which is where a run before a tool fix stays visible.
    """
    target = transition["target_ts"]
    if transition["target_pg"] != transition["source_pg"]:
        target = "pg{0}-{1}".format(transition["target_pg"], target)
    return root / "pg{0}".format(transition["source_pg"]) / "{0}-to-{1}.{2}.json".format(
        transition["source_ts"], target, mode
    )


def write_record(path: Path, doc: Dict[str, Any]) -> None:
    problems = validate_record(doc)
    if problems:
        raise EvidenceError("{0}: {1}".format(path, "; ".join(problems)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load_record(path: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError("{0}: {1}".format(path, exc)) from exc
    problems = validate_record(doc)
    if problems:
        raise EvidenceError("{0}: {1}".format(path, "; ".join(problems)))
    return doc
