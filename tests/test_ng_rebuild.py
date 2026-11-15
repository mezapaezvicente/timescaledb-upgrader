"""Tests for rebuilding aggregates on time_bucket_ng, and for when apply does it.

What would hurt if wrong: a rewrite that changes which bucket a row falls in,
a rebuild offered where none is known to work, anything changed before the
operator has seen what was found, or a step placed after the update that
fails. The rebuild's real behaviour is in ``test_ng_rebuild_container.py``.
"""

from __future__ import annotations

import pytest

from tsupgrade.apply import ApplyOptions, ApplyRefused, _cleared_gates, _plan_cagg_migration
from tsupgrade.ngrebuild import NG_ORIGIN, candidates, ng_calls
from tsupgrade.preflight import Instance, PreflightOptions, check_gates
from tsupgrade.steps import (
    EnsureImage,
    MigrateCaggs,
    Preflight,
    RebuildNgCaggs,
    UpdateExtension,
    VerifyVersion,
)

NG = "timescaledb_experimental.time_bucket_ng"
SQL_TZ = (" SELECT {0}('1 day'::interval, m.ts, 'UTC'::text) AS b,\n    avg(m.v) AS a\n   FROM m\n"
          "  GROUP BY ({0}('1 day'::interval, m.ts, 'UTC'::text));").format(NG)


# --------------------------------------------------------------------------
# Reading and rewriting the definition
# --------------------------------------------------------------------------

def test_a_third_argument_cast_to_text_is_the_timezone():
    (_, _, roles), _ = ng_calls(SQL_TZ)
    assert roles == {"width": "'1 day'::interval", "ts": "m.ts", "timezone": "'UTC'::text"}


def test_four_arguments_are_origin_then_timezone_and_time_bucket_takes_them_the_other_way():
    sql = "SELECT {0}('1 day'::interval, m.ts, '2000-01-01 06:00:00+00'::timestamp with time zone, " \
          "'Europe/Madrid'::text) FROM m".format(NG)
    label = candidates(sql)[0][0]
    assert label == ("time_bucket('1 day'::interval, m.ts, 'Europe/Madrid'::text, "
                     "'2000-01-01 06:00:00+00'::timestamp with time zone)")


def test_named_arguments_are_read_by_name():
    sql = "SELECT {0}(bucket_width => '1 mon'::interval, ts => m.ts) AS \"Bucket\" FROM m".format(NG)
    (_, _, roles), = ng_calls(sql)
    assert roles == {"width": "'1 mon'::interval", "ts": "m.ts"}


def test_quotes_and_parentheses_inside_arguments_do_not_split_them():
    sql = "SELECT {0}('1 day'::interval, (m.ts + '1 hour, (x)'::interval), 'It''s, here'::text) FROM m".format(NG)
    (_, _, roles), = ng_calls(sql)
    assert roles["ts"] == "(m.ts + '1 hour, (x)'::interval)"
    assert roles["timezone"] == "'It''s, here'::text"


def test_every_call_is_rewritten_so_select_and_group_by_still_match():
    text = candidates(SQL_TZ)[0][1]
    assert NG not in text and text.count("time_bucket('1 day'::interval, m.ts, 'UTC'::text)") == 2
    assert not text.endswith(";")


def test_candidates_come_literal_first_then_without_origin():
    sql = "SELECT {0}('7 days'::interval, m.ts, '2000-01-05 00:00:00'::timestamp without time zone) FROM m".format(NG)
    labels = [c[0] for c in candidates(sql)]
    assert labels == ["time_bucket('7 days'::interval, m.ts, '2000-01-05 00:00:00'::timestamp without time zone)",
                      "time_bucket('7 days'::interval, m.ts)"]


def test_with_a_timezone_and_no_origin_time_bucket_ng_origin_is_tried_last():
    """Their default origins differ, so their weeks start on different days."""
    tries = candidates(SQL_TZ)
    assert [c[0] for c in tries] == ["time_bucket('1 day'::interval, m.ts, 'UTC'::text)",
                                     "time_bucket('1 day'::interval, m.ts, 'UTC'::text, {0})".format(NG_ORIGIN)]
    assert "origin" not in tries[1][2] and tries[1][3]["origin"] == NG_ORIGIN


def test_no_time_bucket_ng_no_candidates():
    assert candidates("SELECT time_bucket('1 day'::interval, m.ts) FROM m") == []


def test_unbalanced_text_is_refused_not_guessed():
    with pytest.raises(ValueError):
        ng_calls("SELECT {0}('1 day'::interval, m.ts".format(NG))


# --------------------------------------------------------------------------
# When apply rebuilds
# --------------------------------------------------------------------------

def _row(name, deps=""):
    return "\x1f".join([name, "public.metrics", "311296", "2", "1", deps, "reader:SELECT", "end"])


class Cluster:
    """Answers the read-only questions of planning and of the preview."""

    def __init__(self, ng=_row("public.ng_daily"), old="", definition=SQL_TZ, diff="0", lost="0",
                 kind="v"):
        self.ng, self.old, self.definition, self.diff, self.lost, self.kind = ng, old, definition, diff, lost, kind
        self.sql = []
        self.kube, self.opts = None, ApplyOptions()

    def psql(self, pod, database, sql):
        self.sql.append(sql)
        if sql.startswith("SELECT relkind FROM pg_class"):
            return 0, self.kind, ""
        if "_tsu_rebuild" in sql:
            return 0, "\x1f".join(["public.ng_daily", "ng_daily", "_timescaledb_internal._direct_view_3", "false",
                                   "_timescaledb_internal._materialized_hypertable_3", "b\x1etimestamp with time zone",
                                   "timestamp with time zone", "false", "3", "public.ng_daily_tsu_rebuild", "end"]), ""
        if "LIKE '%time_bucket_ng%'" in sql:
            return 0, self.ng, ""
        if sql.startswith("SELECT pg_get_viewdef("):
            return 0, self.definition, ""
        if "proc_name <>" in sql:
            return 0, "", ""
        if "AT TIME ZONE" in sql:
            return 0, "'2000-01-01 00:00:00+00'::timestamp with time zone", ""
        if "FROM pg_attribute" in sql:
            return 0, "b\x1etimestamp with time zone\x1fa\x1edouble precision", ""
        if "generate_series" in sql:
            return 0, self.diff, ""
        if "max_parallel_workers_per_gather" in sql:
            return 0, self.lost, ""
        if "information_schema.columns" in sql:
            return 0, "t" if "'finalized'" in sql else "f", ""
        if "FROM _timescaledb_catalog.continuous_agg" in sql:
            return 0, self.old, ""
        raise AssertionError("not a question this test expects: " + sql)


def _route():
    """pg15 from 2.12.2 to 2.28.3, as plan lays it out (two images, the #10272 stop)."""
    return [
        Preflight(hop=1, image="i:20"),
        UpdateExtension(hop=1, to="2.20.3", sql="x"),
        VerifyVersion(hop=1, expect="2.20.3"),
        Preflight(hop=2, image="i:28"),
        EnsureImage(hop=2, image="i:28"),
        UpdateExtension(hop=2, to="2.27.2", sql="x"),
        VerifyVersion(hop=2, expect="2.27.2"),
        UpdateExtension(hop=3, to="2.28.3", sql="x"),
        VerifyVersion(hop=3, expect="2.28.3"),
    ]


def _decide(steps=None, cluster=None, source="2.12.2", **opts):
    steps = steps if steps is not None else _route()
    primary = Instance(pod="tsdb-0", role="primary", extensions={"verify": source})
    _plan_cagg_migration(steps, {"source": {"pg": "15.13", "ts": source}}, cluster or Cluster(),
                         [primary], ["verify"], ApplyOptions(**opts))
    return steps


def _refusal(steps=None, cluster=None, **opts):
    with pytest.raises(ApplyRefused) as refused:
        _decide(steps, cluster, **opts)
    assert refused.value.plain
    return "\n".join(refused.value.reasons)


def test_it_stops_before_anything_and_says_what_it_found_and_how_it_would_rebuild():
    text = _refusal()
    assert text.startswith("apply did not start: no TimescaleDB update past 2.14 gets through a continuous "
                           "aggregate built on time_bucket_ng, and this plan updates TimescaleDB to 2.20.3 "
                           "on PostgreSQL 15.")
    for shown in ("verify: public.ng_daily", "built on public.metrics", "reader:SELECT",
                  "rebuilt on: time_bucket('1 day'::interval, m.ts, 'UTC'::text)",
                  "rebuilds each before anything else", "cannot be undone", "backup", "--migrate-caggs",
                  "Or plan to a target below 2.15."):
        assert shown in text
    assert text.endswith("This came from a read-only check. Nothing has been changed.")
    assert "hop" not in text.lower() and "CALL cagg_migrate" not in text


def test_everything_it_asks_before_stopping_is_read_only():
    cluster = Cluster()
    _refusal(cluster=cluster)
    for sql in cluster.sql:
        head = sql.lstrip().upper()
        assert head.startswith("SELECT") or head.startswith("SET MAX_PARALLEL_WORKERS_PER_GATHER")
        assert not any(w in head for w in ("CREATE ", "DROP ", "ALTER ", "CALL ", "INSERT ", "GRANT "))


def test_agreeing_rebuilds_before_the_first_update_when_the_source_is_in_the_window():
    steps = _decide(migrate_caggs=True)
    assert steps[1] == RebuildNgCaggs(hop=1, expect="2.12.2")
    assert isinstance(steps[2], UpdateExtension) and steps[2].to == "2.20.3"


def test_below_28_it_rebuilds_at_the_first_stop_in_the_window_and_cannot_preview():
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.11.2", sql="x"),
             VerifyVersion(hop=1, expect="2.11.2"), UpdateExtension(hop=2, to="2.15.3", sql="x"),
             VerifyVersion(hop=2, expect="2.15.3")]
    cluster = Cluster()
    _decide(steps, cluster, source="2.6.1", migrate_caggs=True)
    i = next(i for i, s in enumerate(steps) if isinstance(s, RebuildNgCaggs))
    assert steps[i] == RebuildNgCaggs(hop=1, expect="2.11.2") and steps[i - 1].expect == "2.11.2"
    assert steps[i + 1].to == "2.15.3"
    assert not any("generate_series" in sql for sql in cluster.sql)


def test_a_route_with_no_stop_from_28_to_214_stops_even_with_the_flag():
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.15.3", sql="x")]
    text = _refusal(steps, source="2.6.1", migrate_caggs=True)
    assert "never stops at a TimescaleDB release from 2.8 to 2.14" in text


def test_no_equivalent_form_stops_even_with_the_flag():
    text = _refusal(cluster=Cluster(diff="568606"), migrate_caggs=True)
    assert "verify: no time_bucket form of public.ng_daily buckets as it does" in text
    assert "differs from time_bucket_ng at 568606 of the compared times" in text
    assert "create it again on time_bucket yourself" in text


def test_history_the_raw_data_no_longer_has_stops_even_with_the_flag():
    text = _refusal(cluster=Cluster(lost="24"), migrate_caggs=True)
    assert "holds 24 row(s) the raw data no longer has" in text and "CREATE TABLE" in text


def test_a_dependent_view_comes_with_what_to_run_for_it():
    text = _refusal(cluster=Cluster(ng=_row("public.ng_daily", "public.ng_view")), migrate_caggs=True)
    assert "verify: public.ng_view, on public.ng_daily" in text and "the rebuild cannot drop it" in text


def test_an_old_format_aggregate_on_time_bucket_ng_is_rebuilt_not_migrated():
    """Created at 2.6 it is both; the rebuild makes it finalized too, so no
    2.14-2.24 stop is needed for it."""
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.11.2", sql="x"),
             VerifyVersion(hop=1, expect="2.11.2"), UpdateExtension(hop=2, to="2.27.2", sql="x"),
             VerifyVersion(hop=2, expect="2.27.2")]
    cluster = Cluster(old=_row("public.ng_daily"))
    _decide(steps, cluster, source="2.6.1", migrate_caggs=True)
    assert [type(s) for s in steps].count(RebuildNgCaggs) == 1
    assert not any(isinstance(s, MigrateCaggs) for s in steps)


def test_both_kinds_get_both_steps_the_rebuild_first():
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.27.2", sql="x"),
             VerifyVersion(hop=1, expect="2.27.2")]
    cluster = Cluster(old=_row("public.fx_hourly"))
    _decide(steps, cluster, source="2.14.2", migrate_caggs=True)
    assert steps[1] == RebuildNgCaggs(hop=1, expect="2.14.2")
    assert steps[2] == MigrateCaggs(hop=1, expect="2.14.2")


def test_the_preflight_before_the_rebuild_is_told_it_will_be_cleared():
    """Measured in the harness: the first preflight failed on the very gate the
    rebuild after it was there to clear."""
    steps = _decide(migrate_caggs=True)
    assert _cleared_gates(steps[1:]) == ("time_bucket_ng",)
    assert _cleared_gates(steps[3:]) == ()
    assert _cleared_gates([MigrateCaggs(hop=1, expect="2.19.3"), UpdateExtension(hop=1, to="2.27.2", sql="x")]) \
        == ("old_caggs",)


class GateKube:
    def __init__(self, answers):
        self.answers = answers

    def psql(self, pod, container, database, user, sql):
        for needle, value in self.answers.items():
            if needle in sql:
                return 0, value, ""
        return 0, "", ""


@pytest.mark.parametrize("cleared,status", [((), "fail"), (("time_bucket_ng",), "pass"),
                                            (("old_caggs",), "fail")])
def test_preflight_passes_only_the_gate_apply_clears(cleared, status):
    kube = GateKube({"LIKE '%time_bucket_ng%'": _row("public.ng_daily")})
    opts = PreflightOptions(target_image="i", cleared_gates=cleared)
    result = check_gates(kube, opts, [Instance(pod="tsdb-0", role="primary", extensions={"verify": "2.12.2"})],
                         "2.20.3")
    assert result.status == status
    if status == "pass":
        assert any("blocked now; apply's next step clears it" in d for d in result.details)


def test_a_source_past_214_is_not_asked_about_time_bucket_ng():
    steps = [Preflight(hop=1, image="i"), UpdateExtension(hop=1, to="2.20.3", sql="x")]
    cluster = Cluster()
    assert _decide(list(steps), cluster, source="2.15.3") == steps and cluster.sql == []
