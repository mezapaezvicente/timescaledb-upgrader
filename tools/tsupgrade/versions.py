"""Version parsing and the matching rules used by the matrix lookup.

Two different units of comparison matter here:

* **PostgreSQL** upgrades are a *major version* concern. Going from 16.4 to
  16.15 is a restart; going from 16 to 17 is a different operation entirely.
* **TimescaleDB** extension upgrades are tracked per *series* (major.minor),
  which is the granularity its release notes use.

So a matrix entry that says ``"16"`` matches any 16.x, and one that says
``"2.30"`` matches any 2.30.x. An entry may also pin a full version, in which
case the match must be exact -- that is how you record something that is only
true of one specific patch release.
"""

from __future__ import annotations

from typing import Tuple

__all__ = [
    "InvalidVersion",
    "parse_version",
    "pg_matches",
    "ts_matches",
    "format_version",
]


class InvalidVersion(ValueError):
    """Raised when a version string cannot be parsed."""


def parse_version(text: object) -> Tuple[int, ...]:
    """Parse a dotted numeric version into a tuple of ints.

    >>> parse_version("16.15")
    (16, 15)
    >>> parse_version(17)
    (17,)

    Trailing qualifiers such as ``16.15-1.pgdg22.04+2`` are truncated at the
    first non-numeric component, because that suffix is a distribution packaging
    detail and never part of an upgrade decision.
    """
    if text is None:
        raise InvalidVersion("version is missing")
    if isinstance(text, bool):  # bool is an int subclass; refuse it explicitly.
        raise InvalidVersion(f"not a version: {text!r}")
    if isinstance(text, int):
        return (text,)

    raw = str(text).strip()
    if not raw:
        raise InvalidVersion("version is empty")

    parts = []
    for chunk in raw.split("."):
        # Stop at the first component that is not purely numeric.
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        if digits != chunk:
            break

    if not parts:
        raise InvalidVersion(f"not a version: {text!r}")
    return tuple(parts)


def format_version(parts: Tuple[int, ...]) -> str:
    return ".".join(str(p) for p in parts)


def _matches(entry: object, query: object, unit: int) -> bool:
    """Compare a matrix entry's version against a discovered version.

    ``unit`` is how many leading components form the natural unit of comparison
    (1 for PostgreSQL majors, 2 for TimescaleDB series). If the entry specifies
    *more* components than that, it is a deliberately pinned version and the
    query must match it exactly.
    """
    e = parse_version(entry)
    q = parse_version(query)

    if len(e) > unit:
        return e == q[: len(e)] and len(q) >= len(e)
    return e[:unit] == q[:unit] and len(q) >= len(e)


def pg_matches(entry: object, query: object) -> bool:
    """Whether a matrix entry's PostgreSQL version covers a discovered one."""
    return _matches(entry, query, unit=1)


def ts_matches(entry: object, query: object) -> bool:
    """Whether a matrix entry's TimescaleDB version covers a discovered one."""
    return _matches(entry, query, unit=2)
