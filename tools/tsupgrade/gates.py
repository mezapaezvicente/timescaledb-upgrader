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
``information_views``   User views built on ``timescaledb_information`` block the
                        update with a stock dependency error. Fixed in 2.28, but
                        the fix keys off the *source* version, so updating from
                        2.24.0 or older is still exposed.

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
``continuous_aggs_bucket_function.bucket_func``      before ~2.14 (the older
                                                     schema uses ``name`` and
                                                     ``experimental`` instead)
===================================================  =======================

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


def _check_unfinalized(runner: Runner, database: str, details: List[str]) -> str:
    """Sub-check one: aggregates still in the pre-2.10 non-finalized format."""
    has_column = _column_exists(runner, database, "continuous_agg", "finalized")
    if has_column is None:
        details.append("old format : could not probe for continuous_agg.finalized")
        return INDETERMINATE
    if not has_column:
        details.append(
            "old format : continuous_agg has no finalized column, so this "
            "database is already past the 2.25 cutoff"
        )
        return CLEAR

    rc, out, err = runner(
        database,
        "SELECT coalesce(string_agg(user_view_schema || '.' || user_view_name, ', '), '') "
        "FROM _timescaledb_catalog.continuous_agg WHERE finalized IS FALSE",
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


def _check_time_bucket_ng(runner: Runner, database: str, details: List[str]) -> str:
    """Sub-check two: aggregates built on ``time_bucket_ng``, also removed at 2.25.

    Independent of the format check -- a fully finalized aggregate still blocks
    the update if its bucket function is ``time_bucket_ng``.

    The catalog that records this was itself reshaped. Current releases store a
    ``bucket_func`` regprocedure; releases around 2.13 and earlier store
    ``name`` plus an ``experimental`` flag. Asking the wrong one raises
    ``column ... does not exist``, which would leave this gate indeterminate on
    exactly the old databases it is meant to protect, so both shapes are
    supported.
    """
    table = "continuous_aggs_bucket_function"
    modern = _column_exists(runner, database, table, "bucket_func")
    if modern is None:
        details.append("time_bucket_ng : could not probe the bucket-function catalog")
        return INDETERMINATE

    if modern:
        predicate = "bf.bucket_func::text LIKE '%time_bucket_ng%'"
    else:
        legacy = _column_exists(runner, database, table, "name")
        if legacy is None:
            details.append("time_bucket_ng : could not probe the bucket-function catalog")
            return INDETERMINATE
        if not legacy:
            # Neither shape present: no such catalog, so nothing can be using it.
            details.append("time_bucket_ng : no bucket-function catalog; none possible")
            return CLEAR
        predicate = "bf.name LIKE '%time_bucket_ng%'"

    rc, out, err = runner(
        database,
        "SELECT coalesce(string_agg(cagg.user_view_schema || '.' || "
        "cagg.user_view_name, ', '), '') "
        "FROM _timescaledb_catalog.continuous_agg cagg "
        "JOIN _timescaledb_catalog.{0} bf "
        "ON cagg.mat_hypertable_id = bf.mat_hypertable_id "
        "WHERE {1}".format(table, predicate),
    )
    if rc != 0:
        details.append(
            "time_bucket_ng : query failed: {0}".format((err or out).splitlines()[:1])
        )
        return INDETERMINATE

    offenders = out.strip()
    if not offenders:
        details.append("time_bucket_ng : none")
        return CLEAR
    details.append("time_bucket_ng : {0}".format(offenders))
    return BLOCKED


def check_old_caggs(
    runner: Runner, database: str, source_ts: object, target_ts: object
) -> GateResult:
    """Continuous aggregates that 2.25 refuses to carry forward.

    Two independent blockers ship in ``2.24.0--2.25.0.sql``: aggregates still in
    the pre-2.10 format, and aggregates built on ``time_bucket_ng``. Either one
    aborts the update on its own, so both are checked.

    The trap is shared: the same script drops ``cagg_migrate`` and its whole
    supporting cast. Neither problem can be repaired after the fact -- migrate
    before updating, or restore a backup. That makes a preflight answer here
    worth considerably more than usual.
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

    statuses = [
        _check_unfinalized(runner, database, details),
        _check_time_bucket_ng(runner, database, details),
    ]
    status = _worst(statuses)

    if status == BLOCKED:
        details.append(
            "Run the cagg_migrate procedure on each BEFORE updating. The 2.25 "
            "update drops cagg_migrate itself, so afterwards the only remaining "
            "fix is restoring a backup."
        )

    summary = {
        BLOCKED: "continuous aggregates present that the update will refuse",
        INDETERMINATE: "could not establish the continuous aggregate format",
        CLEAR: "no blocking continuous aggregates",
    }.get(status, status)
    return GateResult("old_caggs", title, status, summary, details, CAGG_SOURCE)


# ---------------------------------------------------------------------------
# 4d. User views on timescaledb_information
# ---------------------------------------------------------------------------

VIEWS_SOURCE = "https://github.com/timescale/timescaledb/issues/5057"

_VIEW_DEPENDENCY_SQL = """SELECT DISTINCT dependent.relnamespace::regnamespace || '.' || dependent.relname
FROM pg_depend d
JOIN pg_rewrite r ON r.oid = d.objid AND d.classid = 'pg_rewrite'::regclass
JOIN pg_class dependent ON dependent.oid = r.ev_class
JOIN pg_class target ON target.oid = d.refobjid AND d.refclassid = 'pg_class'::regclass
WHERE target.relnamespace = 'timescaledb_information'::regnamespace
  AND dependent.oid <> target.oid
  AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.objid = dependent.oid
                    AND e.classid = 'pg_class'::regclass AND e.deptype = 'e')"""


def check_information_views(
    runner: Runner, database: str, source_ts: object, target_ts: object
) -> GateResult:
    """User views over ``timescaledb_information``, which block the drop.

    Update scripts begin by dropping the information views, so anything of the
    user's built on top blocks the update with an ordinary PostgreSQL
    dependency error. 2.28 ships stub views that avoid this, but the guard keys
    off the *source* version -- updating from 2.24.0 or older still drops the
    views for real, which is exactly the long hops that make this tool worth
    running.

    The ``deptype = 'e'`` exclusion keeps the extension's own views out of the
    result; without it every ``timescaledb_information`` view reports itself.

    This one fails safe. The update is transactional, so tripping it leaves a
    blocked upgrade rather than a broken database.
    """
    title = "User views on timescaledb_information"
    details: List[str] = []

    protected = _at_or_above(source_ts, (2, 24, 1))
    if protected:
        return GateResult(
            "information_views",
            title,
            NOT_APPLICABLE,
            "source ts{0} is past 2.24.0, so views_detached.sql protects the "
            "update".format(source_ts),
            details,
            VIEWS_SOURCE,
        )

    rc, out, err = runner(database, _VIEW_DEPENDENCY_SQL)
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

# The single most repeated piece of advice in the whole issue corpus.
SESSION_RULES = (
    "Run ALTER EXTENSION as the first statement in its session, via psql -X; a "
    ".psqlrc that touches the extension loads the old library first and the "
    "update then refuses with 'cannot be updated after the old version has "
    "already been loaded'.",
    "Existing connections keep the old shared object and keep failing until "
    "they are cycled -- on replicas too, since the catalog change arrives by "
    "WAL while each replica's sessions hold their own library.",
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
        check_information_views(runner, database, source_ts, target_ts),
    ]
