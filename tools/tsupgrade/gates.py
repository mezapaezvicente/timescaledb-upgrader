"""The blockers compiled into the TimescaleDB update scripts, as read-only checks.

These are not advice. Each is a ``RAISE EXCEPTION`` that ships inside the
extension's own update SQL, so an upgrade that trips one stops dead. Because
they are mechanical, they are also exactly checkable in advance, and every check
here is a read-only query.

That is what turns a matrix verdict from a flat enum into *enum plus gate*. No
source says "yes, this upgrade is safe"; the sources say what blocks it. A row
can be mechanically ``in_place`` and still be blocked by what the database
happens to contain, and only the database can answer that.

The gates
---------
``multinode``           Multi-node was removed in 2.14. Distributed hypertables,
                        a ``timescaledb_fdw`` foreign server, or a ``dist_uuid``
                        metadata key each abort the update.
``old_caggs``           Continuous aggregates in the pre-2.10 format were fully
                        removed in 2.25. The update that removes them also drops
                        ``cagg_migrate``, so this cannot be fixed afterwards --
                        migrate first or restore a backup.
``time_bucket_ng``      Continuous aggregates built on ``time_bucket_ng`` stop
                        every update past 2.14: the 2.14 to 2.15 script tries to
                        convert them and fails (measured, 2.15.0 to 2.28.3).
                        They have to be rebuilt on ``time_bucket`` first.
``information_views``   User views built on ``timescaledb_information`` block the
                        update with a stock dependency error, whenever its script
                        drops the views. That depends on both versions: from
                        2.28 targets keep them for sources past 2.24.0, but
                        2.25.x to 2.27.2 still drops them.

Probing for the catalog
-----------------------
**Every gate query must probe for the columns it reads**, because the columns a
gate inspects are precisely the ones the upgrade path adds or removes. This is
not a special case for one check; it is the general shape of the problem, and
three separate instances of it were found in this catalog alone:

===================================================  =======================
Column                                               Absent when
===================================================  =======================
``continuous_agg.finalized``                         at 2.25 and later
``hypertable.replication_factor``                    at 2.14 and later
``continuous_aggs_bucket_function.bucket_func``      before 2.15 (the older
                                                     schema uses ``name`` and
                                                     ``experimental`` instead)
===================================================  =======================

The ``time_bucket_ng`` gate reads neither: before 2.15 a fixed-width bucket
without a timezone has no row in that catalog at all (measured), so it reads
the query each aggregate was defined with instead.

A missing column that the *upgrade removed* means the database is already past
that cutoff, so the gate is **vacuously satisfied**. A missing column because
the database predates it means the same question has to be asked a different
way. Neither is a failure, and neither may be reported as though the gate were
tripped. Probing first is also what keeps a gate from erroring out on exactly
the long multi-version hops this tool exists to serve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from .versions import InvalidVersion, parse_version

__all__ = [
    "OldCagg",
    "check_old_caggs",
    "check_time_bucket_ng",
    "old_caggs",
    "reaches_old_cagg_removal",
    "reaches_time_bucket_ng_failure",
    "time_bucket_ng_caggs",
    "GateResult",
    "CLEAR",
    "BLOCKED",
    "INDETERMINATE",
    "NOT_APPLICABLE",
    "Runner",
    "check_multinode",
    "check_old_caggs",
    "check_information_views",
    "run_gates",
    "DUMP_RESTORE_PROCEDURE",
    "SESSION_RULES",
]

CLEAR = "clear"
BLOCKED = "blocked"
INDETERMINATE = "indeterminate"
NOT_APPLICABLE = "not_applicable"

# A query runner: (database, sql) -> (returncode, stdout, stderr).
# Injected so the gates can be exercised without a cluster, and so preflight
# can hand in its existing read-only psql path unchanged.
Runner = Callable[[str, str], Tuple[int, str, str]]


@dataclass
class GateResult:
    name: str
    title: str
    status: str
    summary: str
    details: List[str] = field(default_factory=list)
    source_url: Optional[str] = None

    @property
    def blocks(self) -> bool:
        return self.status == BLOCKED


def _key(version: object) -> Optional[Tuple[int, ...]]:
    try:
        return parse_version(version)
    except InvalidVersion:
        return None


def _at_or_below(version: object, bound: Tuple[int, ...]) -> Optional[bool]:
    key = _key(version)
    return None if key is None else key[: len(bound)] <= bound


def _at_or_above(version: object, bound: Tuple[int, ...]) -> Optional[bool]:
    key = _key(version)
    return None if key is None else key[: len(bound)] >= bound


def _column_exists(runner: Runner, database: str, table: str, column: str) -> Optional[bool]:
    """Whether a catalog column is present, without touching it.

    Returns None when the probe itself could not be run, which is different
    from the column being absent and must stay different.
    """
    rc, out, _ = runner(
        database,
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='_timescaledb_catalog' AND table_name='{0}' "
        "AND column_name='{1}')".format(table, column),
    )
    if rc != 0:
        return None
    return out.strip() == "t"


# ---------------------------------------------------------------------------
# 4b. Multi-node, removed in 2.14
# ---------------------------------------------------------------------------

MULTINODE_SOURCE = (
    "https://github.com/timescale/timescaledb/blob/main/sql/updates/2.13.1--2.14.0.sql"
)


def check_multinode(
    runner: Runner, database: str, source_ts: object, target_ts: object
) -> GateResult:
    """Three independent triggers, each checked separately.

    The update script aborts on any of: a hypertable with ``replication_factor
    > 0``, a foreign server on the ``timescaledb_fdw`` wrapper, or a
    ``dist_uuid`` key in the catalog metadata.

    ``replication_factor`` is itself removed in 2.14, so querying it on a
    database at 2.14 or later raises ``column "replication_factor" does not
    exist``. The three triggers are therefore checked independently rather than
    in one statement: one missing column must not blank the other two findings.
    """
    title = "Multi-node state (removed in 2.14)"
    details: List[str] = []

    below = _at_or_below(source_ts, (2, 13))
    above = _at_or_above(target_ts, (2, 14))
    if below is False or above is False:
        return GateResult(
            "multinode",
            title,
            NOT_APPLICABLE,
            "source ts{0} -> ts{1} does not cross the 2.14 removal".format(
                source_ts, target_ts
            ),
            details,
            MULTINODE_SOURCE,
        )

    statuses: List[str] = []

    has_column = _column_exists(runner, database, "hypertable", "replication_factor")
    if has_column is None:
        statuses.append(INDETERMINATE)
        details.append("distributed hypertables : could not probe for the column")
    elif not has_column:
        details.append(
            "distributed hypertables : _timescaledb_catalog.hypertable has no "
            "replication_factor column, so this database is already at 2.14 or "
            "later and cannot hold any"
        )
        statuses.append(CLEAR)
    else:
        rc, out, err = runner(
            database,
            "SELECT count(*) FROM _timescaledb_catalog.hypertable "
            "WHERE replication_factor > 0",
        )
        if rc != 0:
            statuses.append(INDETERMINATE)
            details.append(
                "distributed hypertables : query failed: {0}".format(
                    (err or out).splitlines()[:1]
                )
            )
        else:
            count = out.strip()
            statuses.append(BLOCKED if count not in ("0", "") else CLEAR)
            details.append("distributed hypertables : {0}".format(count))

    rc, out, err = runner(
        database,
        "SELECT count(*) FROM pg_foreign_server s "
        "JOIN pg_foreign_data_wrapper w ON w.oid = s.srvfdw "
        "WHERE w.fdwname = 'timescaledb_fdw'",
    )
    if rc != 0:
        statuses.append(INDETERMINATE)
        details.append("timescaledb_fdw servers : query failed")
    else:
        count = out.strip()
        statuses.append(BLOCKED if count not in ("0", "") else CLEAR)
        details.append("timescaledb_fdw servers : {0}".format(count))

    rc, out, err = runner(
        database,
        "SELECT count(*) FROM _timescaledb_catalog.metadata WHERE key = 'dist_uuid'",
    )
    if rc != 0:
        statuses.append(INDETERMINATE)
        details.append("dist_uuid metadata key  : query failed")
    else:
        count = out.strip()
        statuses.append(BLOCKED if count not in ("0", "") else CLEAR)
        details.append("dist_uuid metadata key  : {0}".format(count))

    status = _worst(statuses)
    summary = {
        BLOCKED: "multi-node state present; the update will abort",
        INDETERMINATE: "could not establish whether multi-node state exists",
        CLEAR: "no multi-node state",
    }.get(status, status)
    return GateResult("multinode", title, status, summary, details, MULTINODE_SOURCE)


# ---------------------------------------------------------------------------
# 4c. Old-format continuous aggregates, removed in 2.25
# ---------------------------------------------------------------------------

CAGG_SOURCE = (
    "https://github.com/timescale/timescaledb/blob/main/sql/updates/2.24.0--2.25.0.sql"
)


def _check_unfinalized(runner: Runner, database: str, source_ts: object, details: List[str]) -> str:
    """Sub-check one: aggregates still in the pre-2.10 non-finalized format.

    The ``finalized`` column is missing at both ends: dropped by 2.25, and not
    there yet on a database from before 2.7 (measured: none in 2.1.1's install
    script), where every aggregate is in the old format.
    """
    has_column = _column_exists(runner, database, "continuous_agg", "finalized")
    if has_column is None:
        details.append("old format : could not probe for continuous_agg.finalized")
        return INDETERMINATE
    if not has_column:
        past = _at_or_above(source_ts, (2, 25))
        if past is None:
            details.append(
                "old format : no finalized column, and version {0} says nothing "
                "about why".format(source_ts)
            )
            return INDETERMINATE
        if past:
            details.append(
                "old format : continuous_agg has no finalized column, so this "
                "database is already past the 2.25 cutoff"
            )
            return CLEAR

    rc, out, err = runner(
        database,
        "SELECT coalesce(string_agg(user_view_schema || '.' || user_view_name, ', '), '') "
        "FROM _timescaledb_catalog.continuous_agg"
        + (" WHERE finalized IS FALSE" if has_column else ""),
    )
    if rc != 0:
        details.append(
            "old format : query failed: {0}".format((err or out).splitlines()[:1])
        )
        return INDETERMINATE

    offenders = out.strip()
    if not offenders:
        details.append("old format : none")
        return CLEAR
    details.append("old format : {0}".format(offenders))
    return BLOCKED


# Catalog columns only, and no hypertable_size(): it failed at 2.1.1 (measured:
# hypertable_detailed_size(regclass) does not exist). Checked on 2.1.1 (pg11,
# pg13) and 2.15.3. One column, split on chr(31), so that no name can be taken
# for a separator, and closed by a last field: kubectl output gets stripped, chr(31)
# is whitespace to Python, and empty last fields would go with it (measured).
_OLD_CAGG_REPORT = (
    "SELECT concat_ws(chr(31), "
    "c.user_view_schema || '.' || c.user_view_name, "
    "r.schema_name || '.' || r.table_name, "
    "(SELECT coalesce(sum(pg_total_relation_size(to_regclass(format('%I.%I', ch.schema_name, ch.table_name)))), 0) "
    "FROM _timescaledb_catalog.chunk ch WHERE ch.hypertable_id = m.id), "
    "(SELECT count(*) FROM _timescaledb_catalog.chunk ch WHERE ch.hypertable_id = m.id), "
    "(SELECT count(*) FROM _timescaledb_config.bgw_job j WHERE j.hypertable_id = m.id), "
    "(SELECT coalesce(string_agg(DISTINCT n.nspname || '.' || k.relname, ', '), '') "
    "FROM pg_depend d "
    "JOIN pg_rewrite w ON d.objid = w.oid AND d.classid = 'pg_rewrite'::regclass "
    "JOIN pg_class k ON k.oid = w.ev_class "
    "JOIN pg_namespace n ON n.oid = k.relnamespace "
    "WHERE d.refobjid = to_regclass(format('%I.%I', c.user_view_schema, c.user_view_name)) "
    "AND k.oid <> d.refobjid AND n.nspname NOT LIKE '\\_timescaledb%'), "
    "(SELECT coalesce(string_agg(a.grantee_name || ':' || a.privilege_type, ', '), '') FROM ("
    "SELECT CASE e.grantee WHEN 0 THEN 'PUBLIC' ELSE g.rolname END AS grantee_name, e.privilege_type "
    "FROM pg_class v, aclexplode(v.relacl) e LEFT JOIN pg_roles g ON g.oid = e.grantee "
    "WHERE v.oid = to_regclass(format('%I.%I', c.user_view_schema, c.user_view_name)) "
    "AND e.grantee <> v.relowner) a), "
    "'end') "
    "FROM _timescaledb_catalog.continuous_agg c "
    "JOIN _timescaledb_catalog.hypertable m ON m.id = c.mat_hypertable_id "
    "JOIN _timescaledb_catalog.hypertable r ON r.id = c.raw_hypertable_id"
)


@dataclass(frozen=True)
class OldCagg:
    """A continuous aggregate the update would refuse, as the operator has to
    judge it: an old-format one, or one built on ``time_bucket_ng``."""

    database: str
    name: str
    built_on: str
    size_bytes: int
    chunks: int
    jobs: int
    dependents: Tuple[str, ...] = ()
    grants: Tuple[str, ...] = ()


def _cagg_report(runner: Runner, database: str, where: str) -> Optional[List[OldCagg]]:
    rc, out, _ = runner(database, _OLD_CAGG_REPORT + where)
    if rc != 0:
        return None
    found: List[OldCagg] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split(chr(31))
        if len(parts) != 8:
            return None
        found.append(OldCagg(
            database, parts[0], parts[1], int(parts[2]), int(parts[3]), int(parts[4]),
            tuple(x for x in parts[5].split(", ") if x), tuple(x for x in parts[6].split(", ") if x)))
    return found


def old_caggs(runner: Runner, database: str, source_ts: object) -> Optional[List[OldCagg]]:
    """Every old-format aggregate in ``database``; None if it cannot be
    established. Read-only. Past 2.25 the format, and so the answer, is gone."""
    has_column = _column_exists(runner, database, "continuous_agg", "finalized")
    if has_column is None:
        return None
    if not has_column:
        past = _at_or_above(source_ts, (2, 25))
        if past is None:
            return None
        if past:
            return []
    return _cagg_report(runner, database, " WHERE c.finalized IS FALSE" if has_column else "")


def reaches_old_cagg_removal(target_ts: object) -> bool:
    """Whether an update to ``target_ts`` meets the 2.25 refusal."""
    return bool(_at_or_above(target_ts, (2, 25)))


def check_old_caggs(
    runner: Runner, database: str, source_ts: object, target_ts: object
) -> GateResult:
    """Continuous aggregates in the pre-2.10 format, which 2.25 refuses to
    carry forward.

    The trap: the same script drops ``cagg_migrate`` and its whole supporting
    cast. It cannot be repaired after the fact -- migrate before updating, or
    restore a backup. That makes a preflight answer here worth considerably
    more than usual.

    ``2.24.0--2.25.0.sql`` also refuses aggregates built on ``time_bucket_ng``,
    but no database holding one gets that far: every update past 2.14 already
    fails on them. That is :func:`check_time_bucket_ng`.
    """
    title = "Continuous aggregates blocked at 2.25"
    details: List[str] = []

    if _at_or_above(target_ts, (2, 25)) is False:
        return GateResult(
            "old_caggs",
            title,
            NOT_APPLICABLE,
            "target ts{0} is below the 2.25 removal".format(target_ts),
            details,
            CAGG_SOURCE,
        )

    status = _check_unfinalized(runner, database, source_ts, details)

    if status == BLOCKED:
        details.append(
            "Run CALL cagg_migrate('<aggregate>', override => true, drop_old => "
            "true) on each BEFORE updating: without those options the old one "
            "stays, and this still blocks. The 2.25 update refuses while they "
            "exist and removes cagg_migrate, so migrate before reaching 2.25."
        )

    summary = {
        BLOCKED: "continuous aggregates present that the update will refuse",
        INDETERMINATE: "could not establish the continuous aggregate format",
        CLEAR: "no blocking continuous aggregates",
    }.get(status, status)
    return GateResult("old_caggs", title, status, summary, details, CAGG_SOURCE)


# ---------------------------------------------------------------------------
# 4e. Aggregates on time_bucket_ng: no update past 2.14
# ---------------------------------------------------------------------------

# Upstream's fix, closed unmerged; the failure it describes is the one measured.
NG_SOURCE = "https://github.com/timescale/timescaledb/pull/7763"

# Measured: created from 2.4.0 to 2.14.2, refused from 2.15.0; and a database
# holding one fails every update past 2.14 (2.15.0 to 2.24.0 with "tried
# calling catalog_get when extension isn't loaded", 2.25.0 and 2.28.3 with
# "time_bucket function not found in CAgg definition"), rolled back each time.
NG_LAST = (2, 14)

# The query each aggregate was defined with, which names its bucket function
# on every release from 2.1 (measured). continuous_aggs_bucket_function does
# not: before 2.15 a fixed width without a timezone has no row there.
_NG_WHERE = (
    " WHERE pg_get_viewdef(to_regclass(format('%I.%I', c.direct_view_schema, "
    "c.direct_view_name))) LIKE '%time_bucket_ng%'"
)


def time_bucket_ng_caggs(runner: Runner, database: str) -> Optional[List[OldCagg]]:
    """Every aggregate in ``database`` built on ``time_bucket_ng``; None if it
    cannot be established. Read-only."""
    return _cagg_report(runner, database, _NG_WHERE)


def reaches_time_bucket_ng_failure(target_ts: object) -> bool:
    """Whether an update to ``target_ts`` fails on ``time_bucket_ng`` aggregates."""
    key = _key(target_ts)
    return key is not None and key[:2] > NG_LAST


def check_time_bucket_ng(
    runner: Runner, database: str, source_ts: object, target_ts: object
) -> GateResult:
    """Continuous aggregates built on ``time_bucket_ng``, which no update past
    2.14 gets through.

    The 2.14 to 2.15 script converts them with
    ``_timescaledb_functions.cagg_migrate_to_time_bucket``, a C function of
    the new library that cannot run inside the update, so the update fails;
    the call stays in every script up to 2.24, and 2.25 removed it along with
    ``time_bucket_ng``. ``cagg_migrate`` refuses them ("does not require any
    migration"), whatever the 2.25 script's hint says. What works, measured, is
    rebuilding each on ``time_bucket`` at 2.14 or below, which apply does.

    A database at 2.15 or later cannot hold one: they cannot be created there,
    and no database with one gets there.
    """
    title = "Continuous aggregates on time_bucket_ng (no update past 2.14)"
    details: List[str] = []
    below = _at_or_below(source_ts, NG_LAST)
    if below is False or not reaches_time_bucket_ng_failure(target_ts):
        return GateResult(
            "time_bucket_ng",
            title,
            NOT_APPLICABLE,
            "ts{0} -> ts{1} does not update a database at 2.14 or below past 2.14".format(
                source_ts, target_ts),
            details,
            NG_SOURCE,
        )

    found = time_bucket_ng_caggs(runner, database)
    if found is None:
        return GateResult("time_bucket_ng", title, INDETERMINATE,
                          "could not read the continuous aggregate definitions", details, NG_SOURCE)
    if not found:
        return GateResult("time_bucket_ng", title, CLEAR,
                          "no continuous aggregates on time_bucket_ng", details, NG_SOURCE)
    details.append("time_bucket_ng : {0}".format(", ".join(c.name for c in found)))
    details.append(
        "Every update past 2.14 fails while these exist, and cagg_migrate refuses "
        "them. Rebuild each on time_bucket at a release from 2.8 to 2.14 first: "
        "tsupgrade apply does it with --migrate-caggs."
    )
    return GateResult(
        "time_bucket_ng",
        title,
        BLOCKED,
        "{0} continuous aggregate(s) on time_bucket_ng; the update will fail".format(len(found)),
        details,
        NG_SOURCE,
    )


# ---------------------------------------------------------------------------
# 4d. User views on timescaledb_information
# ---------------------------------------------------------------------------

VIEWS_SOURCE = "https://github.com/timescale/timescaledb/issues/5057"

# Whether the update script from A to B drops the information views outright,
# measured from every forward script the images ship (3805 pairs across eight
# images, 2026-10-30; no image disagrees): it does exactly when A is below a
# floor that depends only on B. Each row gives the floor for targets from its
# first version up to the next row's; the last row holds for later releases.
# Below 2.28 the floor is the first release of the series that last redefined
# a view. From 2.28 the scripts swap in stub views first (views_detached.sql,
# built only for start versions past 2.24.0), so the floor stops moving.
_VIEW_DROP_FLOORS: Tuple[Tuple[Tuple[int, ...], Tuple[int, ...]], ...] = (
    ((1, 7, 0), (1, 7, 0)),
    ((2, 0, 0), (2, 0, 0)),
    ((2, 3, 0), (2, 3, 0)),
    ((2, 6, 0), (2, 6, 0)),
    ((2, 7, 0), (2, 7, 0)),
    ((2, 8, 0), (2, 8, 0)),
    ((2, 9, 0), (2, 9, 0)),
    ((2, 10, 2), (2, 10, 2)),
    ((2, 12, 0), (2, 12, 0)),
    ((2, 13, 0), (2, 13, 0)),
    ((2, 14, 0), (2, 14, 0)),
    ((2, 15, 0), (2, 15, 0)),
    ((2, 20, 0), (2, 20, 0)),
    ((2, 22, 0), (2, 22, 0)),
    ((2, 23, 0), (2, 23, 0)),
    ((2, 24, 0), (2, 24, 0)),
    ((2, 25, 0), (2, 25, 0)),
    ((2, 26, 0), (2, 26, 0)),
    ((2, 28, 0), (2, 25, 0)),
)

# The other way views get dropped: scripts from 2.25.0-2.28.0 to 2.28.1 or
# later repair a bgw_job_stat_history that the 2.14.2--2.15.0 script migrated
# with an integer id (#10272), and the repair drops job_history and job_errors.
# Only a database created below 2.15 has the bad column, so this asks the
# database instead of assuming.
_REPAIR_SOURCES = ((2, 25, 0), (2, 28, 0))
_REPAIR_TARGET = (2, 28, 1)
_REPAIRED_VIEWS = ("job_history", "job_errors")

_VIEW_DEPENDENCY_SQL = """SELECT DISTINCT dependent.relnamespace::regnamespace || '.' || dependent.relname
FROM pg_depend d
JOIN pg_rewrite r ON r.oid = d.objid AND d.classid = 'pg_rewrite'::regclass
JOIN pg_class dependent ON dependent.oid = r.ev_class
JOIN pg_class target ON target.oid = d.refobjid AND d.refclassid = 'pg_class'::regclass
WHERE target.relnamespace = 'timescaledb_information'::regnamespace
  AND dependent.oid <> target.oid
  AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.objid = dependent.oid
                    AND e.classid = 'pg_class'::regclass AND e.deptype = 'e')"""

# Only the views the repair drops, and only if the repair will run.
_REPAIR_DEPENDENCY_SQL = (
    _VIEW_DEPENDENCY_SQL
    + "\n  AND target.relname IN ("
    + ", ".join("'{0}'".format(v) for v in _REPAIRED_VIEWS)
    + """)
  AND EXISTS (SELECT 1 FROM pg_attribute
              WHERE attrelid = to_regclass('_timescaledb_internal.bgw_job_stat_history')
                AND attname = 'id' AND atttypid <> 'bigint'::regtype)"""
)


def _drops_information_views(source_ts: object, target_ts: object) -> bool:
    """Whether the script from source to target drops the views outright.

    Anything not pinned to a full release counts as dropping: a series-only
    source may be its oldest patch, and a series-only target spans floors.
    """
    source, target = _key(source_ts), _key(target_ts)
    if source is None or target is None or len(target) < 3:
        return True
    floor = None
    for first, value in _VIEW_DROP_FLOORS:
        if target >= first:
            floor = value
    return floor is None or source < floor


def _runs_history_repair(source_ts: object, target_ts: object) -> bool:
    source, target = _key(source_ts), _key(target_ts)
    if source is None or target is None:
        return True
    low, high = _REPAIR_SOURCES
    return low <= source[:3] <= high and target >= _REPAIR_TARGET


def check_information_views(
    runner: Runner, database: str, source_ts: object, target_ts: object
) -> GateResult:
    """User views over ``timescaledb_information``, which block the drop.

    An update script that drops an information view is blocked by any view of
    the user's built on top, with an ordinary PostgreSQL dependency error.
    Whether a script drops them depends on both ends (``_VIEW_DROP_FLOORS``):
    2.25.0 to 2.28.0 is safe, 2.25.0 to 2.27.2 is not. Even a script that
    keeps them can drop ``job_history`` and ``job_errors`` while repairing a
    database created below 2.15, which the query checks for itself.

    The ``deptype = 'e'`` exclusion keeps the extension's own views out of the
    result; without it every ``timescaledb_information`` view reports itself.

    This one fails safe. The update is transactional, so tripping it leaves a
    blocked upgrade rather than a broken database.
    """
    title = "User views on timescaledb_information"
    details: List[str] = []

    if _drops_information_views(source_ts, target_ts):
        query = _VIEW_DEPENDENCY_SQL
    elif _runs_history_repair(source_ts, target_ts):
        query = _REPAIR_DEPENDENCY_SQL
    else:
        return GateResult(
            "information_views",
            title,
            NOT_APPLICABLE,
            "the update script from ts{0} to ts{1} keeps the information "
            "views".format(source_ts, target_ts),
            details,
            VIEWS_SOURCE,
        )

    rc, out, err = runner(database, query)
    if rc != 0:
        return GateResult(
            "information_views",
            title,
            INDETERMINATE,
            "dependency query failed: {0}".format((err or out).splitlines()[:1]),
            details,
            VIEWS_SOURCE,
        )

    offenders = [line.strip() for line in out.splitlines() if line.strip()]
    if not offenders:
        return GateResult(
            "information_views",
            title,
            CLEAR,
            "no user views depend on timescaledb_information",
            details,
            VIEWS_SOURCE,
        )

    for name in offenders:
        details.append("blocking view : {0}".format(name))
    if query is _REPAIR_DEPENDENCY_SQL:
        details.append(
            "This database was created below 2.15, so the update repairs "
            "bgw_job_stat_history and drops job_history and job_errors (#10272)."
        )
    details.append(
        "The update drops the information views first, so these must be "
        "dropped and recreated around it. The update is transactional, so it "
        "rolls back cleanly -- this blocks an upgrade, it does not break a "
        "database."
    )
    return GateResult(
        "information_views",
        title,
        BLOCKED,
        "{0} user view(s) depend on timescaledb_information".format(len(offenders)),
        details,
        VIEWS_SOURCE,
    )


# ---------------------------------------------------------------------------
# 4a. The dump/restore procedure, which is a sequence rather than a query
# ---------------------------------------------------------------------------

# `version_check.sql` is prepended to every update script and raises
# 'catalog version mismatch' when the catalog's recorded version is not the
# script's start version. That is the mechanism behind the documented rule:
# restore into an instance carrying the *same* extension version as the dump,
# and bump afterwards. The procedure is fixed and getting it wrong is its own
# genre of bug report, so it travels with any dump_restore verdict.
DUMP_RESTORE_PROCEDURE = (
    "CREATE EXTENSION timescaledb VERSION '<the dump's exact version>'",
    "SELECT timescaledb_pre_restore()",
    "pg_restore  -- without -j; parallel restore is unsupported",
    "SELECT timescaledb_post_restore()",
    "ALTER EXTENSION timescaledb UPDATE  -- only now, and only if wanted",
)

# The single most repeated piece of advice in the whole issue corpus. Both
# measured from 2.5 to 2.30 on PostgreSQL 12 to 17: verify/probes/sessions/.
SESSION_RULES = (
    "Run ALTER EXTENSION as the first statement in its session, via psql -X; "
    "after any statement that loads the library, one from a .psqlrc included, "
    "the update refuses with 'cannot be updated after the old version has "
    "already been loaded'.",
    "Sessions open during the update keep the old library loaded, on replicas "
    "too, and the server ends each one at its next statement with "
    "'\"timescaledb\" already loaded with a different version'; their clients "
    "have to reconnect.",
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _worst(statuses: Sequence[str]) -> str:
    """BLOCKED beats INDETERMINATE beats CLEAR. Nothing checked is not clear."""
    if not statuses:
        return INDETERMINATE
    if BLOCKED in statuses:
        return BLOCKED
    if INDETERMINATE in statuses:
        return INDETERMINATE
    if all(s == NOT_APPLICABLE for s in statuses):
        return NOT_APPLICABLE
    return CLEAR


def run_gates(
    runner: Runner, database: str, source_ts: object, target_ts: object
) -> List[GateResult]:
    """Every gate that could stand between ``source_ts`` and ``target_ts``."""
    return [
        check_multinode(runner, database, source_ts, target_ts),
        check_old_caggs(runner, database, source_ts, target_ts),
        check_time_bucket_ng(runner, database, source_ts, target_ts),
        check_information_views(runner, database, source_ts, target_ts),
    ]
