"""Rebuild continuous aggregates built on ``time_bucket_ng`` on ``time_bucket``.

A database holding one cannot be updated past 2.14 (gates.check_time_bucket_ng),
and nothing upstream converts it: the conversion in the 2.14 to 2.15 script is
what fails, and ``cagg_migrate`` refuses. What does work, measured from 2.8.0
to 2.14.2, is building the same aggregate on ``time_bucket`` beside it and
swapping it in under the old name. That is what :func:`rebuild` does, for one
aggregate, at a release from :data:`REBUILD_FROM` to 2.14:

1. reads the query the aggregate was defined with, its real-time setting, its
   jobs, owner, grants and comment; anything it cannot put back (a job other
   than the refresh policy, compression) stops it here;
2. builds a twin from that query with each ``time_bucket_ng(...)`` call
   rewritten (:func:`candidates`): the literal translation; without the
   origin, if there is one; with ``time_bucket_ng``'s own origin, if there is
   a timezone. Translating the declaration is not enough:
   ``time_bucket_ng('1 day', ts, '2000-01-01 06:00', 'UTC')`` buckets at
   midnight and ``time_bucket`` with that origin at 06:00, and the two start
   their weeks on different days (measured). So a twin is only kept if both
   functions agree at every point of a dense series from 2001 to 2041, and
   the two aggregates' queries return the same rows on the live data;
3. stops if the old aggregate holds history the raw data no longer has
   (a retention policy dropped it): the twin could not rebuild it;
4. stops if the old aggregate has indexes the twin would not;
5. materializes the twin over the range the old one had materialized;
6. in one transaction drops the old aggregate, renames the twin, and puts back
   the refresh policy, owner, grants and comment.

Until step 6 the old aggregate is untouched; a failure drops the twin and
leaves the database as it was. Step 6 commits or rolls back as a whole.

:func:`preview` answers, read-only, everything but step 4 in advance, so that
``apply`` can refuse before it changes anything.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "REBUILD_FROM",
    "RebuildFailed",
    "candidates",
    "ng_calls",
    "preview",
    "rebuild",
]

# time_bucket in a continuous aggregate takes a timezone from 2.8.0 (measured;
# a month without one from 2.6.0). Below this no twin of every shape exists.
REBUILD_FROM = (2, 8, 0)

NG = "timescaledb_experimental.time_bucket_ng("
Run = Callable[[str, str], Tuple[int, str, str]]

# The series both functions are compared over, per type of the time column.
# time_bucket_ng raises for a time before its origin ("origin must be before
# the given date"; 2000-01-01 by default), so a series starts at the later of
# 2001 and the origin.
_SERIES = {
    "timestamp with time zone": ("timestamptz", "'2001-01-01 00:00:00+00'", "'2041-01-01 00:00:00+00'",
                                 "'37 minutes'"),
    "timestamp without time zone": ("timestamp", "'2001-01-01 00:00:00'", "'2041-01-01 00:00:00'",
                                    "'37 minutes'"),
    "date": ("date", "'2001-01-01'", "'2041-01-01'", "'1 day'"),
}
_NAMED = re.compile(r"^([a-z_]+)\s*=>\s*(.*)$", re.S)


class RebuildFailed(Exception):
    def __init__(self, summary: str, output: Sequence[str] = ()) -> None:
        self.summary = summary
        self.output = list(output)
        super().__init__(summary)


# ---------------------------------------------------------------------------
# Reading the definition (pure)
# ---------------------------------------------------------------------------


def _split_args(text: str, start: int) -> Tuple[List[str], int]:
    """The top-level arguments of the call whose ``(`` is just before
    ``start``, and the index after its ``)``."""
    args: List[str] = []
    depth, i, begin = 0, start, start
    while i < len(text):
        ch = text[i]
        if ch in ("'", '"'):
            end = text.find(ch, i + 1)
            while end != -1 and text[end + 1:end + 2] == ch:
                end = text.find(ch, end + 2)
            if end == -1:
                raise ValueError("unterminated quote")
            i = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                args.append(text[begin:i].strip())
                return args, i + 1
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[begin:i].strip())
            begin = i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def ng_calls(definition: str) -> List[Tuple[int, int, Dict[str, str]]]:
    """Each ``time_bucket_ng(...)`` call: (start, end, arguments by role).

    Roles: ``width``, ``ts``, then ``origin`` and ``timezone`` when present;
    ``third`` for a third positional argument whose type the text does not
    say. ``pg_get_viewdef`` writes constants with a cast, so it usually does.
    """
    calls = []
    at = definition.find(NG)
    while at != -1:
        args, end = _split_args(definition, at + len(NG))
        roles: Dict[str, str] = {}
        positional = []
        for arg in args:
            named = _NAMED.match(arg)
            if named:
                roles[{"bucket_width": "width"}.get(named.group(1), named.group(1))] = named.group(2).strip()
            else:
                positional.append(arg)
        for role, arg in zip(("width", "ts"), positional):
            roles[role] = arg
        rest = positional[2:]
        if len(rest) == 2:
            roles["origin"], roles["timezone"] = rest
        elif len(rest) == 1:
            role = "timezone" if rest[0].endswith("::text") else (
                "origin" if "::" in rest[0] else "third")
            roles[role] = rest[0]
        if "width" not in roles or "ts" not in roles or set(roles) - {"width", "ts", "origin", "timezone",
                                                                       "third"}:
            raise ValueError("cannot read the arguments of " + definition[at:end])
        calls.append((at, end, roles))
        at = definition.find(NG, end)
    return calls


def _time_bucket(roles: Dict[str, str]) -> str:
    args = [roles["width"], roles["ts"]]
    for role in ("timezone", "origin", "third"):
        if role in roles:
            args.append(roles[role])
    return "time_bucket(" + ", ".join(args) + ")"


def _ng(roles: Dict[str, str]) -> str:
    args = [roles["width"], roles["ts"]]
    for role in ("origin", "timezone", "third"):
        if role in roles:
            args.append(roles[role])
    return NG + ", ".join(args) + ")"


# time_bucket_ng's default origin is local midnight on 2000-01-01, time_bucket's
# 2000-01-03 for widths in days, so their weeks start on different days. With a
# timezone an aggregate takes time_bucket's origin, as a literal from 2.12
# ("invalid origin value: infinity" for the expression; measured), so the
# rebuild works it out first. Without one it takes neither origin nor offset
# (measured, 2.8.0 to 2.14.2), and nothing makes those weeks.
NG_ORIGIN = "<time_bucket_ng's origin>"


def candidates(definition: str) -> List[Tuple[str, str, Dict[str, str], Dict[str, str]]]:
    """(label, rewritten definition, the first call's arguments, and its
    time_bucket arguments) to try, in order: the literal translation; without
    the origin, if there is one; with time_bucket_ng's own origin
    (:data:`NG_ORIGIN`, for the caller to fill in), if there is a timezone and
    no origin. The label is the first call rewritten, as the operator will
    read it."""
    calls = ng_calls(definition)
    if not calls:
        return []

    def literal(roles: Dict[str, str]) -> Dict[str, str]:
        return dict(roles)

    def without_origin(roles: Dict[str, str]) -> Dict[str, str]:
        return {k: v for k, v in roles.items() if k not in ("origin", "third")}

    def ng_origin(roles: Dict[str, str]) -> Dict[str, str]:
        return dict(roles, origin=NG_ORIGIN)

    tries = [literal]
    if any("origin" in r or "third" in r for _, _, r in calls):
        tries.append(without_origin)
    if all("timezone" in r and "origin" not in r for _, _, r in calls):
        tries.append(ng_origin)
    found = []
    for variant in tries:
        text = definition.rstrip().rstrip(";")
        for start, end, roles in reversed(calls):
            text = text[:start] + _time_bucket(variant(roles)) + text[end:]
        first = calls[0][2]
        found.append((_time_bucket(variant(first)), text, first, variant(first)))
    return found


# ---------------------------------------------------------------------------
# Against the database
# ---------------------------------------------------------------------------


def _one(run: Run, database: str, sql: str, what: str) -> str:
    rc, out, err = run(database, sql)
    if rc != 0:
        raise RebuildFailed("could not read {0}".format(what), _lines(out, err))
    return out.strip()


def _lines(out: str, err: str) -> List[str]:
    return [line for line in (err or out).splitlines() if line.strip()]


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


_META = (
    "SELECT concat_ws(chr(31), "
    "format('%I.%I', c.user_view_schema, c.user_view_name), "
    "quote_ident(c.user_view_name), "
    "format('%I.%I', c.direct_view_schema, c.direct_view_name), "
    "c.materialized_only, "
    "format('%I.%I', m.schema_name, m.table_name), "
    "(SELECT quote_ident(d.column_name) || chr(30) || d.column_type::text "
    "FROM _timescaledb_catalog.dimension d WHERE d.hypertable_id = m.id ORDER BY d.id LIMIT 1), "
    "(SELECT d.column_type::text FROM _timescaledb_catalog.dimension d "
    "WHERE d.hypertable_id = c.raw_hypertable_id AND d.interval_length IS NOT NULL ORDER BY d.id LIMIT 1), "
    "m.compressed_hypertable_id IS NOT NULL, "
    "c.mat_hypertable_id, "
    "format('%I.%I', c.user_view_schema, left(c.user_view_name, 46) || '_tsu_rebuild'), "
    "'end') "
    "FROM _timescaledb_catalog.continuous_agg c "
    "JOIN _timescaledb_catalog.hypertable m ON m.id = c.mat_hypertable_id "
    "WHERE format('%I.%I', c.user_view_schema, c.user_view_name) = {0} "
    "OR c.user_view_schema || '.' || c.user_view_name = {0}"
)

# What the swap puts back, written by the server from what it holds now. The
# policy comes before the owner: a job added after belongs to the new owner,
# which may not be allowed to start one (measured: "permission denied to start
# background process"). A continuous aggregate takes COMMENT ON VIEW, not ON
# MATERIALIZED VIEW (measured).
_RESTORE = (
    "SELECT s FROM ("
    "SELECT 1 AS o, format('SELECT add_continuous_aggregate_policy(%L, start_offset => %s, end_offset => %s, "
    "schedule_interval => %L::interval)', {name}, "
    "coalesce(quote_literal(j.config->>'start_offset') || '::interval', 'NULL'), "
    "coalesce(quote_literal(j.config->>'end_offset') || '::interval', 'NULL'), j.schedule_interval) AS s "
    "FROM _timescaledb_config.bgw_job j WHERE j.hypertable_id = {mat} "
    "AND j.proc_name = 'policy_refresh_continuous_aggregate' "
    "UNION ALL SELECT 2, format('ALTER MATERIALIZED VIEW %s OWNER TO %I', {name}, pg_get_userbyid(v.relowner)) "
    "FROM pg_class v WHERE v.oid = to_regclass({name}) "
    "UNION ALL SELECT 3, format('GRANT %s ON %s TO %s', e.privilege_type, {name}, "
    "CASE e.grantee WHEN 0 THEN 'PUBLIC' ELSE quote_ident(g.rolname) END) "
    "FROM pg_class v, aclexplode(v.relacl) e LEFT JOIN pg_roles g ON g.oid = e.grantee "
    "WHERE v.oid = to_regclass({name}) AND e.grantee <> v.relowner "
    "UNION ALL SELECT 4, format('COMMENT ON VIEW %s IS %L', {name}, obj_description(v.oid, 'pg_class')) "
    "FROM pg_class v WHERE v.oid = to_regclass({name}) AND obj_description(v.oid, 'pg_class') IS NOT NULL"
    ") r ORDER BY o, s"
)


class _Cagg:
    """What the rebuild reads about one aggregate before it changes anything."""

    def __init__(self, run: Run, database: str, name: str) -> None:
        fields = _one(run, database, _META.format(_literal(name)), "the catalog of " + name).split(chr(31))
        if len(fields) != 11:
            raise RebuildFailed("{0} is not a continuous aggregate here".format(name))
        (self.exact, self.view, self.direct, self.realtime_off, self.mat, dim, self.raw_type,
         compressed, self.mat_id, self.twin) = fields[:10]
        self.name = name
        self.dim_name, _, self.dim_type = dim.partition(chr(30))
        self.definition = _one(run, database, "SELECT pg_get_viewdef(to_regclass({0}))".format(
            _literal(self.direct)), "the definition of " + name)
        others = _one(run, database, (
            "SELECT coalesce(string_agg(proc_name || ' (job ' || id || ')', ', '), '') "
            "FROM _timescaledb_config.bgw_job WHERE hypertable_id = {0} "
            "AND proc_name <> 'policy_refresh_continuous_aggregate'").format(self.mat_id), "the jobs of " + name)
        if others or compressed == "true":
            raise RebuildFailed("{0} has {1}, which the rebuild would not put back".format(
                name, "jobs other than its refresh policy: " + others if others else "compression enabled"))
        if self.raw_type not in _SERIES:
            raise RebuildFailed("{0} buckets a {1} column; the rebuild knows timestamptz, timestamp and "
                                "date".format(name, self.raw_type or "non-time"))
        try:
            self.tries = candidates(self.definition)
        except ValueError as exc:
            raise RebuildFailed("cannot read the definition of {0}: {1}".format(name, exc))
        if not self.tries:
            raise RebuildFailed("{0} is not built on time_bucket_ng".format(name))


def _filled(run: Run, database: str, label: str, text: str, roles: Dict[str, str],
            tb_roles: Dict[str, str]) -> Tuple[str, str, Dict[str, str]]:
    """A candidate with time_bucket_ng's origin written in, if it uses it."""
    if NG_ORIGIN not in text:
        return label, text, tb_roles
    at = _one(run, database, "SELECT quote_literal((('2000-01-01 00:00:00'::timestamp) AT TIME ZONE "
                             "{0})::text) || '::timestamp with time zone'".format(roles["timezone"]),
              "time_bucket_ng's origin")
    return label.replace(NG_ORIGIN, at), text.replace(NG_ORIGIN, at), dict(tb_roles, origin=at)


def _creatable(cagg: _Cagg, tb_roles: Dict[str, str]) -> bool:
    """Measured, 2.8.0 to 2.14.2: an aggregate takes time_bucket with an
    origin only together with a timezone."""
    return not (("origin" in tb_roles or "third" in tb_roles) and "timezone" not in tb_roles)


def _bucket_diff(run: Run, database: str, cagg: _Cagg, roles: Dict[str, str], tb_roles: Dict[str, str]) -> str:
    """At how many times of a dense series the two functions disagree."""
    series_type, low, high, step = _SERIES[cagg.raw_type]
    point = "g.t::{0}".format(series_type)
    start = "{0}::{1}".format(low, series_type)
    if "origin" in roles:
        start = "greatest({0}, ({1})::{2})".format(start, roles["origin"], series_type)
    return _one(run, database, (
        "SELECT count(*) FROM generate_series({0}, {1}::{2}, {3}::interval) AS g(t) "
        "WHERE {4} IS DISTINCT FROM {5}").format(start, high, series_type, step, _ng(dict(roles, ts=point)),
                                                  _time_bucket(dict(tb_roles, ts=point))),
        "the bucket comparison")


def _lost(run: Run, database: str, cagg: _Cagg) -> str:
    """Rows only the old aggregate has: older than the first bucket the raw
    data still makes, or a different first bucket (part of it dropped)."""
    first = "(SELECT min({0}) FROM {1})".format(cagg.dim_name, cagg.direct)
    older = "(SELECT count(*) FROM {0} u WHERE {1} IS NULL OR u.{2} < {1})".format(cagg.exact, first, cagg.dim_name)
    differ = _diff(_columns(run, database, cagg.exact),
                   "SELECT * FROM {0} WHERE {1} = {2}".format(cagg.exact, cagg.dim_name, first),
                   "SELECT * FROM {0} WHERE {1} = {2}".format(cagg.direct, cagg.dim_name, first))
    return _one(run, database, "SET max_parallel_workers_per_gather = 0; SELECT {0} + ({1})".format(older, differ),
                "the history check").splitlines()[-1].strip()


def _lost_failure(cagg: _Cagg, lost: str) -> RebuildFailed:
    return RebuildFailed(
        "{0} holds {1} row(s) the raw data no longer has (a retention policy, most likely), which a "
        "rebuild would lose".format(cagg.name, lost), [
            "To keep them: copy them into a table of your own (CREATE TABLE ... AS SELECT * FROM {0} "
            "WHERE ...), drop {0}, create it again on time_bucket over the data that remains, and run "
            "apply again.".format(cagg.name)])


def _none_failure(cagg: _Cagg, tried: List[str]) -> RebuildFailed:
    return RebuildFailed("no time_bucket form of {0} buckets as it does".format(cagg.name), tried + [
        "At 2.14 and below an aggregate takes time_bucket with an origin only on timestamptz with a "
        "timezone, and never with an offset. To go on, create it again on time_bucket yourself, knowing "
        "its buckets will start elsewhere, or drop it; then run apply again."])


def preview(run: Run, database: str, name: str) -> str:
    """Read-only: the time_bucket form :func:`rebuild` would use for ``name``.
    Raises :class:`RebuildFailed` for what it can tell now that the rebuild
    would refuse. Run where time_bucket takes a timezone, from 2.8."""
    cagg = _Cagg(run, database, name)
    tried: List[str] = []
    for label, text, roles, tb_roles in cagg.tries:
        label, text, tb_roles = _filled(run, database, label, text, roles, tb_roles)
        if not _creatable(cagg, tb_roles):
            tried.append("{0}: an aggregate cannot be created on it at 2.14 or below".format(label))
            continue
        diff = _bucket_diff(run, database, cagg, roles, tb_roles)
        if diff == "0":
            lost = _lost(run, database, cagg)
            if lost != "0":
                raise _lost_failure(cagg, lost)
            return label
        tried.append("{0}: differs from time_bucket_ng at {1} of the compared times".format(label, diff))
    raise _none_failure(cagg, tried)


_FLOATS = ("double precision", "real")


def _columns(run: Run, database: str, relation: str) -> List[Tuple[str, str]]:
    out = _one(run, database, (
        "SELECT string_agg(quote_ident(attname) || chr(30) || format_type(atttypid, atttypmod), chr(31) "
        "ORDER BY attnum) FROM pg_attribute WHERE attrelid = to_regclass({0}) AND attnum > 0 "
        "AND NOT attisdropped").format(_literal(relation)), "the columns of " + relation)
    return [tuple(c.split(chr(30), 1)) for c in out.split(chr(31)) if c]  # type: ignore[misc]


def _diff(columns: Sequence[Tuple[str, str]], left: str, right: str) -> str:
    """How many rows of two queries with these columns have no match in the
    other. Floating-point columns match within a relative 1e-9: the same rows
    summed in another order differ in the last digits (measured: a refresh
    and the live query, 49.4155319148936 and 49.41553191489359). Everything
    else -- the bucket, the groups, counts, numeric sums -- matches exactly."""
    keys = [c for c, t in columns if t not in _FLOATS]
    floats = [c for c, t in columns if t in _FLOATS]
    # A FULL JOIN needs a hashable condition, which IS NOT DISTINCT FROM is
    # not (measured); a row's text tells NULL from '' and hashes.
    key = "md5(ROW({0})::text)".format(", ".join("s." + c for c in keys)) if keys else "''"
    pick = "".join(", s.{0}".format(c) for c in floats)
    close = " AND ".join("((l.{0} IS NULL AND r.{0} IS NULL) OR abs(l.{0} - r.{0}) <= 1e-9 * "
                         "greatest(1, abs(l.{0}), abs(r.{0})))".format(c) for c in floats) or "true"
    return ("SELECT count(*) FROM (SELECT {2} AS tsu_k{3} FROM ({0}) s) l FULL JOIN "
            "(SELECT {2} AS tsu_k{3} FROM ({1}) s) r ON l.tsu_k = r.tsu_k "
            "WHERE l.tsu_k IS NULL OR r.tsu_k IS NULL OR NOT ({4})").format(left, right, key, pick, close)


def rebuild(run: Run, database: str, name: str) -> List[str]:
    """Rebuild aggregate ``name`` (schema.name) on ``time_bucket``; the lines
    to report. Raises :class:`RebuildFailed`, with the old aggregate intact."""
    try:
        cagg = _Cagg(run, database, name)
    except RebuildFailed as exc:
        exc.summary += "; it is left as it was"
        raise
    if _one(run, database, "SELECT to_regclass({0}) IS NOT NULL".format(_literal(cagg.twin)), cagg.twin) == "t":
        raise RebuildFailed("{0} already exists, left from an earlier run; drop it with DROP MATERIALIZED "
                            "VIEW {0} and run apply again".format(cagg.twin))
    restore = [l for l in _one(run, database, _RESTORE.format(name=_literal(cagg.exact), mat=cagg.mat_id),
                               "what to put back on " + name).splitlines() if l.strip()]
    twin = cagg.twin
    made = False
    try:
        tried: List[str] = []
        chosen = None
        for label, text, roles, tb_roles in cagg.tries:
            label, text, tb_roles = _filled(run, database, label, text, roles, tb_roles)
            rc, out, err = run(database, (
                "CREATE MATERIALIZED VIEW {0} WITH (timescaledb.continuous, timescaledb.materialized_only = {1}) "
                "AS {2} WITH NO DATA").format(twin, cagg.realtime_off, text))
            if rc != 0:
                said = _lines(out, err)
                tried.append("{0}: cannot be created here: {1}".format(label, said[0] if said else rc))
                continue
            made = True
            diff = _bucket_diff(run, database, cagg, roles, tb_roles)
            live = "0"
            if diff == "0":
                twin_direct = _one(run, database, (
                    "SELECT format('%I.%I', direct_view_schema, direct_view_name) FROM "
                    "_timescaledb_catalog.continuous_agg WHERE format('%I.%I', user_view_schema, "
                    "user_view_name) = {0}").format(_literal(twin)), "the twin's definition")
                live = _one(run, database, "SET max_parallel_workers_per_gather = 0; " + _diff(
                    _columns(run, database, cagg.direct), "SELECT * FROM " + cagg.direct,
                    "SELECT * FROM " + twin_direct), "the comparison of {0} with its twin".format(name)).splitlines()[-1]
            if diff == "0" and live.strip() == "0":
                chosen = (label, roles)
                break
            run(database, "DROP MATERIALIZED VIEW {0}".format(twin))
            made = False
            tried.append("{0}: {1}".format(label, "differs from time_bucket_ng at {0} of the compared "
                                           "times".format(diff) if diff != "0"
                                           else "returns {0} row(s) different from the aggregate".format(live)))
        if chosen is None:
            raise _none_failure(cagg, tried)
        label, roles = chosen

        lost = _lost(run, database, cagg)
        if lost != "0":
            raise _lost_failure(cagg, lost)

        twin_mat = _one(run, database, (
            "SELECT format('%I.%I', m.schema_name, m.table_name) FROM _timescaledb_catalog.continuous_agg c "
            "JOIN _timescaledb_catalog.hypertable m ON m.id = c.mat_hypertable_id "
            "WHERE format('%I.%I', c.user_view_schema, c.user_view_name) = {0}").format(_literal(twin)),
            "the twin's materialization")
        extra = _one(run, database, (
            "SELECT coalesce(string_agg(i.indexrelid::regclass::text, ', '), '') FROM pg_index i "
            "WHERE i.indrelid = to_regclass({0}) AND regexp_replace(pg_get_indexdef(i.indexrelid), "
            "'^.* USING ', '') NOT IN (SELECT regexp_replace(pg_get_indexdef(j.indexrelid), '^.* USING ', '') "
            "FROM pg_index j WHERE j.indrelid = to_regclass({1}))").format(
                _literal(cagg.mat), _literal(twin_mat)), "the indexes of " + name)
        if extra:
            raise RebuildFailed("{0} has indexes the rebuild would not create again: {1}".format(name, extra), [
                "Drop them, run apply again, and create them on {0} afterwards.".format(name)])

        window = _one(run, database, "SELECT concat_ws(chr(31), coalesce(min({0})::text, ''), "
                                     "coalesce((max({0}) + {1})::{2}::text, ''), 'end') FROM {3}".format(
                                         cagg.dim_name, roles["width"], cagg.dim_type, cagg.mat),
                      "the materialized range of " + name).split(chr(31))
        refreshed = "nothing materialized"
        if window[0]:
            rc, out, err = run(database, "CALL refresh_continuous_aggregate({0}, {1}::{3}, {2}::{3})".format(
                _literal(twin), _literal(window[0]), _literal(window[1]), cagg.dim_type))
            if rc != 0:
                raise RebuildFailed("materializing the rebuilt {0} failed".format(name), _lines(out, err))
            refreshed = "materialized from {0} to {1}".format(window[0], window[1])

        # One statement string is one transaction: all of it, or none.
        swap = ["DROP MATERIALIZED VIEW {0}".format(cagg.exact),
                "ALTER MATERIALIZED VIEW {0} RENAME TO {1}".format(twin, cagg.view)] + restore
        rc, out, err = run(database, "; ".join(swap))
        if rc != 0:
            raise RebuildFailed("swapping in the rebuilt {0} failed and was rolled back".format(name),
                                _lines(out, err) + swap)
        made = False
    except RebuildFailed as exc:
        if made:
            run(database, "DROP MATERIALIZED VIEW {0}".format(twin))
        exc.summary += "; it is left as it was"
        raise
    step = _SERIES[cagg.raw_type][3].strip("'")
    return ["{0}: rebuilt {1} on {2}, {3}; it agreed with time_bucket_ng at every {4} from 2001 to 2041 "
            "and on the live data".format(database, name, label, refreshed, step)] + [
        "{0}: {1}".format(database, s) for s in restore]
