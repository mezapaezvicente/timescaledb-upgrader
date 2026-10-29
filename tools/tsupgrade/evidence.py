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
``mode``            ``empty`` or ``seeded``
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

Optional and unchecked beyond being present: ``checks`` (the fixture's check
lines on a seeded run), ``started_at``/``finished_at``, ``plan``, ``notes``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .versions import InvalidVersion, parse_version

__all__ = [
    "EVIDENCE_FORMAT",
    "MODES",
    "RESULTS",
    "EvidenceError",
    "build_record",
    "validate_record",
    "write_record",
    "load_record",
    "record_path",
    "upgrader_revision",
]

EVIDENCE_FORMAT = 1
MODES = ("empty", "seeded")
RESULTS = ("pass", "fail")


class EvidenceError(Exception):
    """A record is malformed, or could not be written or read."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upgrader_revision(repo: Optional[Path] = None) -> Dict[str, Any]:
    """This repository's commit, and whether the working tree differs from it.

    A dirty tree is recorded, not refused: a fix under test is exactly when a
    run matters. It does mean the commit alone does not reproduce the run.
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
    status = git("status", "--porcelain")
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


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

    if not doc.get("log"):
        problems.append("log must point at the full log")
    return problems


def record_path(root: Path, transition: Dict[str, str], mode: str) -> Path:
    """``<root>/pg<major>/<source_ts>-to-<target_ts>.<mode>.json``.

    One file per row and mode, overwritten by a re-run; git keeps the earlier
    ones, which is where a run before a tool fix stays visible.
    """
    return root / "pg{0}".format(transition["source_pg"]) / "{0}-to-{1}.{2}.json".format(
        transition["source_ts"], transition["target_ts"], mode
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
