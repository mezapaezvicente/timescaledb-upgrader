"""Vendor-stated facts about which versions go together.

Three different things live here, all quoted from primary sources rather than
recalled, and all *facts about versions* rather than verdicts about upgrades:

* :data:`SUPPORT_MATRIX` -- which PostgreSQL majors each TimescaleDB series
  supports, transcribed from TigerData's published matrix.
* :data:`ABI_BROKEN` -- the six PostgreSQL patch releases that shipped an
  accidental ABI break and cannot load TimescaleDB at all.
* :data:`PG_MINOR_RULES` -- the PostgreSQL minors each TimescaleDB series
  refuses at load time, from its own ``compat.h``.

* :data:`UPDATE_HAZARDS` -- single ``ALTER EXTENSION`` updates that a known,
  unfixed TimescaleDB bug makes fail, and the release to stop at instead.

None says an upgrade is safe. The matrix says a combination is *supportable*;
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

from typing import Dict, List, NamedTuple, Optional, Tuple

from .versions import InvalidVersion, parse_version

__all__ = [
    "SUPPORT_MATRIX",
    "SUPPORT_MATRIX_SOURCE",
    "ABI_BROKEN",
    "ABI_BROKEN_SOURCE",
    "abi_break_for",
    "PG_MINOR_RULES",
    "PG_MINOR_RULES_SOURCE",
    "pg_minor_rules_for",
    "pg_minor_refusal",
    "UpdateHazard",
    "UPDATE_HAZARDS",
    "update_hazard",
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


PG_MINOR_RULES_SOURCE = (
    "https://github.com/timescale/timescaledb/blob/{tag}/src/compat/compat.h"
)

# The PostgreSQL minors each TimescaleDB series refuses to load on, per major:
# {major: (lowest accepted minor, minors excluded outright)}. Transcribed from
# the `is_supported_pg_version_*` macros in `src/compat/compat.h`, which
# `src/extension.c` evaluates against `server_version_num` at load time. Every
# 2.x release tag up to the band's upper bound was read, and each band is
# constant across its patch releases; the tag named is where the band starts.
#
# An empty dict means the series was read and places no minor-level condition
# on any major. A series outside every band is silence, not permission: a
# release newer than the transcription may well have raised a floor. Majors a
# series does not build for at all are the support matrix's business, not this
# table's.
PG_MINOR_RULES: Tuple[
    Tuple[Tuple[int, int], Tuple[int, int], str, Dict[int, Tuple[int, Tuple[int, ...]]]],
    ...,
] = (
    ((2, 0), (2, 0), "2.0.0", {}),
    ((2, 1), (2, 15), "2.1.0", {13: (2, ())}),
    ((2, 16), (2, 17), "2.16.0", {}),
    # The ResultRelInfo ABI break, excluded by exact version before a floor
    # replaced it.
    ((2, 18), (2, 19), "2.18.0", {14: (0, (14,)), 15: (0, (9,)), 16: (0, (5,)), 17: (0, (1,))}),
    ((2, 20), (2, 30), "2.20.0", {15: (10, ()), 16: (6, ()), 17: (2, ())}),
)


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


def pg_minor_rules_for(
    ts: object,
) -> Optional[Tuple[str, Dict[int, Tuple[int, Tuple[int, ...]]]]]:
    """The source tag and per-major minor rules for a TimescaleDB series.

    None when the transcription does not cover the series (or it cannot be
    parsed). That is silence: the caller cannot conclude the series loads.
    """
    try:
        want = _series(ts)
    except InvalidVersion:
        return None
    for low, high, tag, rules in PG_MINOR_RULES:
        if low <= want <= high:
            return tag, rules
    return None


def pg_minor_refusal(ts: object, server_version: object) -> Optional[str]:
    """Why TimescaleDB ``ts`` would refuse to load on ``server_version``.

    Returns None when it would not refuse -- and also when this cannot be
    answered (series not transcribed, version without a minor). Like
    :func:`abi_break_for`, None is not a clean bill of health; callers that
    need to tell the two apart check :func:`pg_minor_rules_for` and the
    version's shape themselves.
    """
    found = pg_minor_rules_for(ts)
    if found is None:
        return None
    tag, rules = found
    try:
        parts = parse_version(server_version)
    except InvalidVersion:
        return None
    if len(parts) < 2 or parts[0] not in rules:
        return None
    major, minor = parts[0], parts[1]
    floor, excluded = rules[major]
    if minor < floor:
        return "TimescaleDB {0} requires PostgreSQL {1}.{2} or later (compat.h at {3})".format(
            ts, major, floor, tag
        )
    if minor in excluded:
        return "TimescaleDB {0} excludes PostgreSQL {1}.{2} (compat.h at {3})".format(
            ts, major, minor, tag
        )
    return None


class UpdateHazard(NamedTuple):
    """One update edge a TimescaleDB bug breaks, and the stop that avoids it.

    The edge is every single ``ALTER EXTENSION`` from a version in
    ``[from_low, from_high)`` to one in ``[to_low, to_high)``; ``to_high``
    None means no fixed release exists. ``via`` is the stop that was measured
    to work, inside the same image.
    """

    from_low: str
    from_high: str
    to_low: str
    to_high: Optional[str]
    via: str
    reason: str
    url: str


# Measured with plain containers and with the harness, as each entry says; the
# upstream reports are the reference, not the proof.
UPDATE_HAZARDS: Tuple[UpdateHazard, ...] = (
    # `2.10.1--2.10.2.sql` drops `_timescaledb_internal.ping_data_node(name)`
    # without IF EXISTS, and that function only exists from 2.0.0. Closed
    # upstream without a fix. Measured on pg12.22: 1.7.5 -> 2.11.2 fails,
    # 1.7.5 -> 2.1.0 -> 2.11.2 and 1.7.5 -> 2.1.1 -> 2.11.2 pass.
    UpdateHazard(
        "0", "2.0.0", "2.10.2", None, "2.1.1",
        "fails on `function _timescaledb_internal.ping_data_node(name) does not exist`",
        "https://github.com/timescale/timescaledb/issues/5789",
    ),
    # The 2.28.1 repair block in `2.28.0--2.28.1.sql` drops
    # `bgw_job_stat_history` but not the `job_errors` view on it. It fires only
    # for a database that was *updated* across 2.15 from below, and only on
    # update paths starting at or below 2.24. A plan cannot see that history,
    # so the stop is taken for every such edge. Fixed in 2.29.0 (#10278); the
    # 2.28.x backport (#10284) merged after 2.28.3, the last 2.28 release, so
    # PG15 has no fixed release. Measured on pg15.18: 2.13.1 -> 2.20.3 ->
    # 2.28.3 fails, 2.13.1 -> 2.20.3 -> 2.27.2 -> 2.28.3 passes.
    UpdateHazard(
        "2.15.0", "2.25.0", "2.28.2", "2.28.4", "2.27.2",
        "fails on `cannot drop table _timescaledb_internal.bgw_job_stat_history` "
        "when the database was once updated across 2.15",
        "https://github.com/timescale/timescaledb/issues/10272",
    ),
)


def update_hazard(from_version: object, to_version: object) -> Optional[UpdateHazard]:
    """The known bug a direct update ``from_version -> to_version`` hits, if any.

    None also when either version cannot be parsed: absence of a hazard here is
    only the absence of these specific defects.
    """
    try:
        src, dst = parse_version(from_version), parse_version(to_version)
    except InvalidVersion:
        return None
    for hazard in UPDATE_HAZARDS:
        if not parse_version(hazard.from_low) <= src < parse_version(hazard.from_high):
            continue
        if dst < parse_version(hazard.to_low):
            continue
        if hazard.to_high is not None and dst >= parse_version(hazard.to_high):
            continue
        return hazard
    return None


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
