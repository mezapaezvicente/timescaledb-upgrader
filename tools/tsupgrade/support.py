"""Vendor-stated facts about which versions go together.

Two different things live here, both quoted from primary sources rather than
recalled, and both *facts about versions* rather than verdicts about upgrades:

* :data:`SUPPORT_MATRIX` -- which PostgreSQL majors each TimescaleDB series
  supports, transcribed from TigerData's published matrix.
* :data:`ABI_BROKEN` -- the six PostgreSQL patch releases that shipped an
  accidental ABI break and cannot load TimescaleDB at all.

Neither says an upgrade is safe. The matrix says a combination is *supportable*;
whether you can get there from where you are is the matrix module's job, and
whether the database's contents allow it is preflight's.

Note on floors
--------------
The published matrix and the container registry disagree in one place: the
matrix's first PostgreSQL 15 row is TimescaleDB 2.10, but ``pg15-ts2.9`` images
exist and are in ``data/images.json``. Code that needs a floor should prefer the
registry -- an image that exists is a stronger fact than a table that omits it --
and :func:`floor_disagreements` exists to surface the gap rather than hide it.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .versions import InvalidVersion, parse_version

__all__ = [
    "SUPPORT_MATRIX",
    "SUPPORT_MATRIX_SOURCE",
    "ABI_BROKEN",
    "ABI_BROKEN_SOURCE",
    "abi_break_for",
    "documented_pg_majors",
    "documented_ts_floor",
    "supports",
    "floor_disagreements",
]

SUPPORT_MATRIX_SOURCE = (
    "https://www.tigerdata.com/docs/deploy/self-hosted/upgrades/upgrade-pg"
)

# (ts_series_low, ts_series_high, PostgreSQL majors supported across that band).
# Transcribed row for row from SUPPORT_MATRIX_SOURCE. The bands are inclusive
# and, deliberately, not contiguous: the published table has no row for the 2.11
# series, and inventing one to close the gap would be exactly the kind of guess
# this repository refuses to make. `documented_pg_majors` returns None there.
SUPPORT_MATRIX: Tuple[Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, ...]], ...] = (
    ((2, 29), (2, 30), (16, 17, 18)),
    ((2, 23), (2, 28), (15, 16, 17, 18)),
    ((2, 20), (2, 22), (15, 16, 17)),
    ((2, 17), (2, 19), (14, 15, 16, 17)),
    ((2, 16), (2, 16), (14, 15, 16)),
    ((2, 13), (2, 15), (13, 14, 15, 16)),
    ((2, 12), (2, 12), (13, 14, 15)),
    ((2, 10), (2, 10), (12, 13, 14, 15)),
    ((2, 5), (2, 9), (12, 13, 14)),
    ((2, 4), (2, 4), (12, 13)),
    ((2, 1), (2, 3), (11, 12, 13)),
    ((2, 0), (2, 0), (11, 12)),
    ((1, 7), (1, 7), (10, 11, 12)),
)

ABI_BROKEN_SOURCE = (
    "https://www.crunchydata.com/blog/"
    "a-change-to-relresultinfo-a-near-miss-with-postgres-17-1"
)

# PostgreSQL patch releases that grew `struct ResultRelInfo`, leaving
# TimescaleDB unable to load. Reverted in the release named as the fix.
# This is a property of the *running server*, not of any transition, which is
# why it belongs in preflight rather than in the matrix.
ABI_BROKEN: Dict[Tuple[int, int], str] = {
    (17, 1): "17.2",
    (16, 5): "16.6",
    (15, 9): "15.10",
    (14, 14): "14.15",
    (13, 17): "13.18",
    (12, 21): "12.22",
}


def _series(ts: object) -> Tuple[int, int]:
    parts = parse_version(ts)
    return (parts[0], parts[1] if len(parts) > 1 else 0)


def abi_break_for(server_version: object) -> Optional[str]:
    """The fixed release, if ``server_version`` is one of the broken six.

    Returns None for every other version -- including versions this table says
    nothing about. Absence from the table is not a clean bill of health, it is
    simply the absence of this specific defect.
    """
    try:
        parts = parse_version(server_version)
    except InvalidVersion:
        return None
    if len(parts) < 2:
        return None
    return ABI_BROKEN.get((parts[0], parts[1]))


def documented_pg_majors(ts: object) -> Optional[Tuple[int, ...]]:
    """PostgreSQL majors the published matrix lists for a TimescaleDB series.

    None means the table has no row covering that series -- the 2.11 gap, or a
    series newer than the transcription. It does not mean "none supported".
    """
    try:
        want = _series(ts)
    except InvalidVersion:
        return None
    for low, high, majors in SUPPORT_MATRIX:
        if low <= want <= high:
            return majors
    return None


def documented_ts_floor(pg_major: int) -> Optional[Tuple[int, int]]:
    """Lowest TimescaleDB series the published matrix shows for a PG major."""
    floors = [low for low, _high, majors in SUPPORT_MATRIX if pg_major in majors]
    return min(floors) if floors else None


def supports(pg_major: int, ts: object) -> Optional[bool]:
    """Whether the published matrix pairs a PG major with a TimescaleDB series.

    None when the table is silent, which callers must not read as False.
    """
    majors = documented_pg_majors(ts)
    if majors is None:
        return None
    return pg_major in majors


def floor_disagreements(
    published_floors: Dict[int, Tuple[int, int]]
) -> List[Tuple[int, Tuple[int, int], Tuple[int, int]]]:
    """Where the registry publishes an image the support matrix omits.

    ``published_floors`` maps a PostgreSQL major to the lowest TimescaleDB
    series the registry actually serves for it. Returns
    ``(pg_major, published, documented)`` for each major where the registry
    reaches further back than the table.

    Known case at the time of writing: pg15 publishes 2.9, the table starts at
    2.10. Surfaced rather than reconciled -- whichever is wrong, hiding it makes
    the other one harder to trust.
    """
    out: List[Tuple[int, Tuple[int, int], Tuple[int, int]]] = []
    for pg_major, published in sorted(published_floors.items()):
        documented = documented_ts_floor(pg_major)
        if documented is not None and published < documented:
            out.append((pg_major, published, documented))
    return out
