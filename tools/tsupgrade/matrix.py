"""Loader and lookup for the upgrade compatibility matrix.

The matrix is *data*, not code: a JSON file that this module reads. Adding a
verified upgrade path means running it and editing JSON, not editing Python.

A row is verified only by running that migration with the upgrader itself, on
an empty database and on a seeded one (``verify/harness.py``). Its
``evidence`` names the two records those runs left in ``verify/evidence/``,
and may name more: the same runs on a database with an upgrade history, and
negative controls that show what a planner's step protects against.
The one exception is ``no_image``, which the registry answers: such a row
carries the registry listing in ``references`` instead. ``references``
otherwise holds upstream issues, pull requests and release notes that explain
a verdict; they are never its proof.

An entry whose verdict has not been established has ``verdict: null`` and
``verified: false``. That is a deliberate, load-bearing state: callers are
expected to surface it as *unknown* and refuse to proceed, rather than
treating a missing verdict as permission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .versions import InvalidVersion, pg_matches, ts_matches

__all__ = [
    "VERDICTS",
    "EVIDENCE_MODES",
    "OPTIONAL_EVIDENCE",
    "MatrixError",
    "MatrixEntry",
    "Matrix",
    "load_matrix",
    "default_matrix_path",
]

# The verdicts an entry may carry. None is also permitted and means
# "not established" -- see the module docstring.
VERDICTS = frozenset({"in_place", "dump_restore", "unsupported", "no_image"})

# The two runs every verified row has evidence for, by the evidence key.
EVIDENCE_MODES = ("empty", "seeded")
# Records a row may cite as well. They never replace the two above: history
# runs repeat them on a database updated from an older release, and controls
# (``direct-*``) bypass the planner to show what its step protects against.
OPTIONAL_EVIDENCE = (
    "history-empty", "history-seeded",
    "direct-history-empty", "direct-history-seeded",
)

_REQUIRED_FIELDS = ("source_pg", "source_ts", "target_pg", "target_ts")


class MatrixError(Exception):
    """Raised when the matrix file is missing, malformed or self-inconsistent."""


@dataclass(frozen=True)
class MatrixEntry:
    """One row of the matrix: a transition and what is known about it."""

    source_pg: str
    source_ts: str
    target_pg: str
    target_ts: str
    verdict: Optional[str] = None
    reason: str = ""
    steps: Sequence[str] = field(default_factory=tuple)
    verified: bool = False
    # {"empty": path, "seeded": path}, repo-relative, or None.
    evidence: Optional[Dict[str, str]] = field(default=None, hash=False, compare=True)
    references: Sequence[str] = field(default_factory=tuple)

    @property
    def is_known(self) -> bool:
        """True when this row carries a verdict backed by verification runs."""
        return self.verdict is not None and self.verified

    @property
    def transition(self) -> str:
        return (
            "pg{0}/ts{1} -> pg{2}/ts{3}".format(
                self.source_pg, self.source_ts, self.target_pg, self.target_ts
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_pg": self.source_pg,
            "source_ts": self.source_ts,
            "target_pg": self.target_pg,
            "target_ts": self.target_ts,
            "verdict": self.verdict,
            "reason": self.reason,
            "steps": list(self.steps),
            "verified": self.verified,
            "evidence": dict(self.evidence) if self.evidence else None,
            "references": list(self.references),
        }


def _entry_from_dict(raw: Any, index: int) -> MatrixEntry:
    where = "entries[{0}]".format(index)
    if not isinstance(raw, dict):
        raise MatrixError(
            "{0}: expected an object, got {1}".format(where, type(raw).__name__)
        )

    for key in _REQUIRED_FIELDS:
        if key not in raw:
            raise MatrixError("{0}: missing required field {1!r}".format(where, key))
        if raw[key] is None or str(raw[key]).strip() == "":
            raise MatrixError("{0}: field {1!r} must not be empty".format(where, key))

    verdict = raw.get("verdict")
    if verdict is not None and verdict not in VERDICTS:
        raise MatrixError(
            "{0}: verdict {1!r} is not one of {2} or null".format(
                where, verdict, sorted(VERDICTS)
            )
        )

    verified = raw.get("verified", False)
    if not isinstance(verified, bool):
        raise MatrixError(
            "{0}: 'verified' must be a boolean, got {1!r}".format(where, verified)
        )

    steps = raw.get("steps") or []
    if not isinstance(steps, list) or any(not isinstance(s, str) for s in steps):
        raise MatrixError("{0}: 'steps' must be a list of strings".format(where))

    if "source_url" in raw:
        raise MatrixError(
            "{0}: 'source_url' is no longer a field; proof goes in 'evidence' "
            "and upstream links in 'references'".format(where)
        )

    references = raw.get("references") or []
    if not isinstance(references, list) or any(
        not isinstance(r, str) or not r.strip() for r in references
    ):
        raise MatrixError(
            "{0}: 'references' must be a list of non-empty strings".format(where)
        )

    evidence = raw.get("evidence")
    if evidence is not None:
        if (
            not isinstance(evidence, dict)
            or not set(EVIDENCE_MODES) <= set(evidence)
            or not set(evidence) <= set(EVIDENCE_MODES + OPTIONAL_EVIDENCE)
            or any(not isinstance(v, str) or not v.strip() for v in evidence.values())
        ):
            raise MatrixError(
                "{0}: 'evidence' must map {1} to record paths, and may add {2}".format(
                    where, " and ".join(EVIDENCE_MODES), ", ".join(OPTIONAL_EVIDENCE)
                )
            )

    # A verified row has to say what verified it, and has to have a verdict.
    # This invariant is what keeps "verified" from decaying into decoration.
    if verified:
        if verdict is None:
            raise MatrixError(
                "{0}: verified entries must carry a verdict".format(where)
            )
        if verdict == "no_image":
            if not references:
                raise MatrixError(
                    "{0}: a verified no_image entry must carry the registry "
                    "listing in references".format(where)
                )
        elif evidence is None:
            raise MatrixError(
                "{0}: verified entries must carry evidence of the empty and "
                "seeded runs".format(where)
            )
    elif evidence is not None:
        raise MatrixError(
            "{0}: an unverified entry must not carry evidence".format(where)
        )

    entry = MatrixEntry(
        source_pg=str(raw["source_pg"]),
        source_ts=str(raw["source_ts"]),
        target_pg=str(raw["target_pg"]),
        target_ts=str(raw["target_ts"]),
        verdict=verdict,
        reason=raw.get("reason") or "",
        steps=tuple(steps),
        verified=verified,
        evidence=dict(evidence) if evidence is not None else None,
        references=tuple(references),
    )

    # Fail at load time, not at lookup time, on unparseable versions.
    try:
        pg_matches(entry.source_pg, entry.source_pg)
        pg_matches(entry.target_pg, entry.target_pg)
        ts_matches(entry.source_ts, entry.source_ts)
        ts_matches(entry.target_ts, entry.target_ts)
    except InvalidVersion as exc:
        raise MatrixError("{0}: {1}".format(where, exc)) from exc

    return entry


def _specificity(entry: MatrixEntry) -> int:
    """How precisely an entry pins its four versions; higher wins ties."""
    return sum(
        len(str(v).split("."))
        for v in (entry.source_pg, entry.source_ts, entry.target_pg, entry.target_ts)
    )


class Matrix:
    """An immutable, queryable view over the loaded matrix entries."""

    def __init__(
        self,
        entries: Sequence[MatrixEntry],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._entries: List[MatrixEntry] = list(entries)
        self.meta: Dict[str, Any] = dict(meta or {})

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    @property
    def entries(self) -> List[MatrixEntry]:
        return list(self._entries)

    def from_source(self, source_pg: object, source_ts: object) -> List[MatrixEntry]:
        """Every recorded transition starting from a given source pair.

        Used to turn a lookup miss into something actionable: the matrix may
        well describe how to get somewhere from here, just not in one hop.
        """
        found: List[MatrixEntry] = []
        for entry in self._entries:
            try:
                if pg_matches(entry.source_pg, source_pg) and ts_matches(
                    entry.source_ts, source_ts
                ):
                    found.append(entry)
            except InvalidVersion:
                continue
        return found

    def lookup(
        self,
        source_pg: object,
        source_ts: object,
        target_pg: object,
        target_ts: object,
    ) -> Optional[MatrixEntry]:
        """Find the entry describing a transition, or None if unlisted.

        ``source_*`` is what was discovered on the running instance and
        ``target_*`` is what is being asked for. Versions match by their natural
        unit -- PostgreSQL major, TimescaleDB series -- unless an entry pins
        something more specific, in which case it must match exactly.

        When several entries match, the most specific wins, so a row pinned to
        an exact patch release takes precedence over the series-wide row it
        sits inside.

        Returning None means "this matrix says nothing about that transition".
        It does *not* mean the transition is unsupported.
        """
        matches: List[MatrixEntry] = []
        for entry in self._entries:
            try:
                if (
                    pg_matches(entry.source_pg, source_pg)
                    and ts_matches(entry.source_ts, source_ts)
                    and pg_matches(entry.target_pg, target_pg)
                    and ts_matches(entry.target_ts, target_ts)
                ):
                    matches.append(entry)
            except InvalidVersion as exc:
                raise MatrixError("cannot compare versions: {0}".format(exc)) from exc

        if not matches:
            return None
        return max(matches, key=_specificity)


def default_matrix_path() -> Path:
    """Path to the matrix shipped with the repository."""
    return Path(__file__).resolve().parents[2] / "data" / "upgrade-matrix.json"


def load_matrix(path: Optional[object] = None) -> Matrix:
    """Load and validate the matrix from ``path`` (default: the shipped file)."""
    target = Path(path) if path is not None else default_matrix_path()

    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MatrixError("matrix file not found: {0}".format(target)) from exc
    except OSError as exc:
        raise MatrixError(
            "cannot read matrix file {0}: {1}".format(target, exc)
        ) from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MatrixError("{0} is not valid JSON: {1}".format(target, exc)) from exc

    if not isinstance(raw, dict):
        raise MatrixError(
            "{0}: expected a JSON object at the top level".format(target)
        )
    if "entries" not in raw:
        raise MatrixError("{0}: missing top-level 'entries'".format(target))
    if not isinstance(raw["entries"], list):
        raise MatrixError("{0}: 'entries' must be a list".format(target))

    entries = [_entry_from_dict(item, i) for i, item in enumerate(raw["entries"])]
    meta = {k: v for k, v in raw.items() if k != "entries"}
    return Matrix(entries, meta)
