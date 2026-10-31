"""Plan steps as types: one dataclass per action, built from JSON in one place.

A plan is a file, and files get edited. :func:`parse_steps` is the only code
that reads a step's JSON: it returns typed steps, or every reason the document
cannot be applied. ``apply`` then runs each step through a table keyed by
action, so an action it does not know fails here, by name, rather than in a
fallback branch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

__all__ = [
    "ACTIONS",
    "UPDATE_SQL",
    "Analyze",
    "CycleSessions",
    "EnsureImage",
    "MajorUpgrade",
    "PgUpgradeCheck",
    "Preflight",
    "Step",
    "UpdateExtension",
    "VerifyVersion",
    "parse_steps",
]

UPDATE_SQL = "ALTER EXTENSION timescaledb UPDATE TO '{0}';"
_EXACT_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_RELEASE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class PgUpgradeCheck:
    """What a preflight before a ``pg_upgrade`` also checks."""

    from_pg: int
    to_pg: int
    link: bool
    image: Optional[str] = None


@dataclass(frozen=True)
class Preflight:
    hop: int
    image: str
    target_pg: Optional[str] = None
    target_ts: Optional[str] = None
    pg_upgrade: Optional[PgUpgradeCheck] = None
    action: str = "preflight"


@dataclass(frozen=True)
class EnsureImage:
    hop: int
    image: str
    digest: Optional[str] = None
    action: str = "ensure_image"


@dataclass(frozen=True)
class UpdateExtension:
    hop: int
    to: str
    sql: str
    from_version: Optional[str] = None
    action: str = "update_extension"


@dataclass(frozen=True)
class CycleSessions:
    hop: int
    action: str = "cycle_sessions"


@dataclass(frozen=True)
class VerifyVersion:
    hop: int
    expect: str
    action: str = "verify_version"


@dataclass(frozen=True)
class MajorUpgrade:
    hop: int
    image: str
    from_pg: int
    to_pg: int
    ts: str
    run_image: str
    link: bool
    digest: Optional[str] = None
    run_digest: Optional[str] = None
    action: str = "major_upgrade"


@dataclass(frozen=True)
class Analyze:
    hop: int
    action: str = "analyze"


Step = Union[Preflight, EnsureImage, UpdateExtension, CycleSessions, VerifyVersion,
             MajorUpgrade, Analyze]


def _preflight(raw: Dict[str, Any], why: List[str]) -> Optional[Preflight]:
    if not raw.get("image"):
        why.append("preflight needs an image")
    check = raw.get("pg_upgrade")
    upgrade = None
    if check is not None:
        if not isinstance(check, dict) or not isinstance(check.get("link"), bool):
            why.append("preflight's pg_upgrade needs link true or false")
        else:
            upgrade = PgUpgradeCheck(check.get("from_pg"), check.get("to_pg"), check["link"],
                                     check.get("image"))
    return None if why else Preflight(raw.get("hop", 0), raw["image"], raw.get("target_pg"),
                                      raw.get("target_ts"), upgrade)


def _ensure_image(raw: Dict[str, Any], why: List[str]) -> Optional[EnsureImage]:
    if not raw.get("image"):
        why.append("ensure_image needs an image")
    return None if why else EnsureImage(raw.get("hop", 0), raw["image"], raw.get("digest"))


def _update(raw: Dict[str, Any], why: List[str]) -> Optional[UpdateExtension]:
    to = str(raw.get("to") or "")
    if not _EXACT_VERSION.match(to):
        why.append("'to' is not a version: {0!r}".format(to))
    elif raw.get("sql") != UPDATE_SQL.format(to):
        # apply executes upgrades, not whatever SQL a document contains.
        why.append("sql is not the one statement its 'to' implies ({0})".format(UPDATE_SQL.format(to)))
    return None if why else UpdateExtension(raw.get("hop", 0), to, raw["sql"], raw.get("from"))


def _cycle(raw: Dict[str, Any], why: List[str]) -> CycleSessions:
    return CycleSessions(raw.get("hop", 0))


def _verify(raw: Dict[str, Any], why: List[str]) -> Optional[VerifyVersion]:
    if not raw.get("expect"):
        why.append("verify_version needs expect")
    return None if why else VerifyVersion(raw.get("hop", 0), str(raw["expect"]))


def _major(raw: Dict[str, Any], why: List[str]) -> Optional[MajorUpgrade]:
    if not raw.get("image") or not raw.get("run_image"):
        why.append("major_upgrade needs image and run_image")
    if not (isinstance(raw.get("from_pg"), int) and isinstance(raw.get("to_pg"), int)
            and raw["from_pg"] < raw["to_pg"]):
        why.append("major_upgrade needs from_pg below to_pg")
    if not _RELEASE.match(str(raw.get("ts") or "")):
        why.append("major_upgrade 'ts' is not a version")
    if not isinstance(raw.get("link"), bool):
        why.append("major_upgrade needs link true or false")
    if why:
        return None
    return MajorUpgrade(raw.get("hop", 0), raw["image"], raw["from_pg"], raw["to_pg"], raw["ts"],
                        raw["run_image"], raw["link"], raw.get("digest"), raw.get("run_digest"))


def _analyze(raw: Dict[str, Any], why: List[str]) -> Analyze:
    return Analyze(raw.get("hop", 0))


_PARSERS: Dict[str, Callable[[Dict[str, Any], List[str]], Optional[Step]]] = {
    "preflight": _preflight,
    "ensure_image": _ensure_image,
    "update_extension": _update,
    "cycle_sessions": _cycle,
    "verify_version": _verify,
    "major_upgrade": _major,
    "analyze": _analyze,
}
ACTIONS = tuple(_PARSERS)


def parse_steps(raw_steps: Any) -> Tuple[List[Step], List[str]]:
    """Typed steps, or every reason they cannot be applied (then no steps)."""
    if not isinstance(raw_steps, list):
        return [], ["plan.steps is not a list"]
    steps: List[Step] = []
    reasons: List[str] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            reasons.append("steps[{0}] is not an object".format(i))
            continue
        why: List[str] = []
        for key in ("digest", "run_digest"):
            if raw.get(key) is not None and not _DIGEST.match(str(raw[key])):
                why.append("{0} is not a sha256 digest: {1!r}".format(key, raw[key]))
        parser = _PARSERS.get(raw.get("action"))
        if parser is None:
            reasons.append("steps[{0}]: unknown action {1!r}".format(i, raw.get("action")))
            continue
        step = parser(raw, why)
        if why or step is None:
            reasons.extend("steps[{0}]: {1}".format(i, w) for w in why)
        else:
            steps.append(step)
    return (steps if not reasons else []), reasons
