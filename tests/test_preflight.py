"""Tests for preflight's decision logic.

No cluster and no network: these cover the parts that decide what a check
*means* -- status precedence, df parsing, per-database extension versions, and
the matrix verdict -- which is where a wrong answer would actually hurt.
"""

from __future__ import annotations

import json

import pytest

from tsupgrade.preflight import (
    FAIL,
    PASS,
    UNKNOWN,
    CheckResult,
    Instance,
    PreflightOptions,
    _human,
    _parse_df,
    check_matrix,
    check_pg_minor_floor,
    check_archiving,
    check_extensions,
    check_pg_upgrade_headroom,
    render_report,
    worst_status,
)


# --------------------------------------------------------------------------
# Status precedence
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([PASS, PASS], PASS),
        ([PASS, UNKNOWN], UNKNOWN),
        ([PASS, FAIL], FAIL),
        ([UNKNOWN, FAIL], FAIL),
        ([FAIL, UNKNOWN, PASS], FAIL),
    ],
)
def test_worst_status_precedence(statuses, expected):
    assert worst_status(statuses) == expected


def test_no_checks_is_unknown_not_pass():
    """A check that ran against nothing has not passed.

    This is the whole posture of the phase: absence of evidence is reported as
    unknown, never silently as success.
    """
    assert worst_status([]) == UNKNOWN


# --------------------------------------------------------------------------
# df parsing
# --------------------------------------------------------------------------

DF_OUTPUT = """Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sdd        1056287656  65424620 990863036       7% /home/postgres/pgdata
"""


def test_parse_df_returns_bytes():
    usage = _parse_df(DF_OUTPUT)
    assert usage == {
        "total": 1056287656 * 1024,
        "used": 65424620 * 1024,
        "free": 990863036 * 1024,
    }


@pytest.mark.parametrize(
    "text", ["", "Filesystem 1024-blocks Used Available Capacity Mounted on", "garbage\nalso garbage"]
)
def test_parse_df_returns_none_on_junk(text):
    assert _parse_df(text) is None


def test_human_readable_sizes():
    assert _human(0) == "0.0 B"
    assert _human(1024) == "1.0 KiB"
    assert _human(5 * 1024 ** 3) == "5.0 GiB"
    assert _human(None) == "unknown"


# --------------------------------------------------------------------------
# Per-database extension versions
# --------------------------------------------------------------------------

def test_single_extension_version_is_reported():
    inst = Instance(pod="p", extensions={"bench": "2.30.1", "postgres": None})
    assert inst.ts_version == "2.30.1"
    assert inst.ts_versions_disagree is False


def test_databases_disagreeing_yields_no_single_version():
    """Two databases on one instance can carry different extension versions.

    That is a real state, and an upgrade has to handle each database, so it must
    not collapse into one confident-looking answer.
    """
    inst = Instance(pod="p", extensions={"a": "2.28.3", "b": "2.30.1"})
    assert inst.ts_versions_disagree is True
    assert inst.ts_version is None


def test_extension_absent_everywhere():
    inst = Instance(pod="p", extensions={"postgres": None})
    assert inst.ts_version is None
    assert inst.ts_versions_disagree is False


# --------------------------------------------------------------------------
# Matrix verdict
# --------------------------------------------------------------------------

def _opts(matrix_path, **kw):
    return PreflightOptions(
        target_image="example/image:tag", matrix_path=str(matrix_path), **kw
    )


def _instance(pg="15.4", ts="2.20.1"):
    return Instance(pod="tsdb-0", server_version=pg, extensions={"bench": ts})


def test_verified_verdict_passes(basic_matrix_path):
    result = check_matrix(
        _opts(basic_matrix_path), [_instance("15.4", "2.20.1")], "15", "2.21"
    )
    assert result.status == PASS


def test_unsupported_verdict_fails(basic_matrix_path):
    result = check_matrix(
        _opts(basic_matrix_path), [_instance("12.1", "2.11.0")], "16", "2.30"
    )
    assert result.status == FAIL


def test_no_image_verdict_fails(basic_matrix_path):
    result = check_matrix(
        _opts(basic_matrix_path), [_instance("13.4", "2.15.0")], "13", "2.30"
    )
    assert result.status == FAIL


def test_unverified_entry_is_unknown_not_pass(basic_matrix_path):
    """An entry with no established verdict must never read as approval."""
    result = check_matrix(
        _opts(basic_matrix_path), [_instance("15.4", "2.21.0")], "16", "2.21"
    )
    assert result.status == UNKNOWN
    assert any("not established" in d for d in result.details)


def test_missing_entry_is_unknown_and_says_so(basic_matrix_path):
    result = check_matrix(
        _opts(basic_matrix_path), [_instance("15.4", "2.20.1")], "18", "2.30"
    )
    assert result.status == UNKNOWN
    assert any("no matrix entry" in d for d in result.details)
    assert any("not the same as unsupported" in d for d in result.details)


def test_unpinned_target_is_unknown(basic_matrix_path):
    result = check_matrix(_opts(basic_matrix_path), [_instance()], None, None)
    assert result.status == UNKNOWN
    assert "--target-pg" in result.summary


def test_unreachable_instances_are_not_looked_up(basic_matrix_path):
    broken = Instance(pod="tsdb-0", error="connection refused")
    result = check_matrix(_opts(basic_matrix_path), [broken], "15", "2.21")
    assert result.status == UNKNOWN


def test_instance_without_the_extension_is_unknown(basic_matrix_path):
    inst = Instance(pod="tsdb-0", server_version="15.4", extensions={"postgres": None})
    result = check_matrix(_opts(basic_matrix_path), [inst], "15", "2.21")
    assert result.status == UNKNOWN


def test_a_broken_matrix_file_is_unknown_not_a_crash(tmp_path):
    path = tmp_path / "matrix.json"
    path.write_text("not json", encoding="utf-8")
    result = check_matrix(_opts(path), [_instance()], "15", "2.21")
    assert result.status == UNKNOWN
    assert "cannot load matrix" in result.summary


# --------------------------------------------------------------------------
# PostgreSQL minor floor
# --------------------------------------------------------------------------

def _server(pg):
    return Instance(pod="tsdb-0", server_version=pg, extensions={"bench": "2.19.3"})


def test_minor_below_the_target_floor_fails():
    result = check_pg_minor_floor([_server("16.4")], "16", "2.30.1")
    assert result.status == FAIL
    assert any("16.6" in line for line in result.details)


def test_minor_at_the_target_floor_passes():
    assert check_pg_minor_floor([_server("16.6")], "16", "2.30.1").status == PASS


def test_pinned_target_image_minor_is_what_counts():
    """After the rollout the target runs on the image's PostgreSQL, not ours."""
    assert check_pg_minor_floor([_server("15.8")], "15.18", "2.28.3").status == PASS
    assert check_pg_minor_floor([_server("15.18")], "15.9", "2.28.3").status == FAIL


def test_unpinned_target_ts_is_unknown():
    assert check_pg_minor_floor([_server("16.6")], "16", None).status == UNKNOWN


def test_untranscribed_target_ts_is_unknown_not_pass():
    assert check_pg_minor_floor([_server("16.15")], "16", "2.31.0").status == UNKNOWN


def test_server_version_without_a_minor_is_unknown():
    assert check_pg_minor_floor([_server("16")], None, "2.30.1").status == UNKNOWN


def test_no_reachable_instance_is_unknown():
    broken = Instance(pod="tsdb-0", error="connection refused")
    assert check_pg_minor_floor([broken], "16", "2.30.1").status == UNKNOWN


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def test_report_states_it_is_read_only():
    text = render_report([CheckResult("a", "A check", PASS, "fine")])
    assert "read-only" in text


def test_report_counts_each_status():
    results = [
        CheckResult("a", "A", PASS, ""),
        CheckResult("b", "B", FAIL, ""),
        CheckResult("c", "C", UNKNOWN, ""),
    ]
    text = render_report(results)
    assert "1 pass, 1 fail, 1 unknown" in text
    assert "resolve it before upgrading" in text


def test_report_omits_the_unknown_warning_when_there_are_none():
    text = render_report([CheckResult("a", "A", PASS, "")])
    assert "resolve it before upgrading" not in text


def test_junk_server_version_does_not_crash_the_report(tmp_path):
    """An empty matrix skips version parsing, so a junk version reaches the label.

    It must degrade to unknown with the raw string shown, not take down the
    whole preflight run.
    """
    path = tmp_path / "matrix.json"
    path.write_text('{"entries": []}', encoding="utf-8")
    inst = Instance(pod="tsdb-0", server_version="not-a-version", extensions={"db": "2.30.1"})

    result = check_matrix(_opts(path), [inst], "17", "2.30")
    assert result.status == UNKNOWN
    assert any("not-a-version" in d for d in result.details)


def test_patroni_role_treats_the_old_master_label_as_primary():
    from tsupgrade.preflight import patroni_role

    assert patroni_role({"role": "primary"}) == "primary"
    assert patroni_role({"role": "master"}) == "primary"
    assert patroni_role({"role": "replica"}) == "replica"
    assert patroni_role({}) == "unknown"
    assert patroni_role(None) == "unknown"


# --------------------------------------------------------------------------
# Disk headroom for pg_upgrade (copy needs the data directory again)
# --------------------------------------------------------------------------

GIB = 1024 ** 3


class DiskKube:
    def __init__(self, data_kib, free_kib, left=""):
        self.data_kib, self.free_kib, self.left = data_kib, free_kib, left
        self.seen = []

    def exec(self, pod, container, argv):
        self.seen.append((pod, argv[0]))
        if argv[0] == "du":
            return 0, "{0}\t{1}".format(self.data_kib, argv[-1]), ""
        if argv[0] == "df":
            return 0, ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                       "/dev/x 999999999 1 {0} 1% /home/postgres/pgdata".format(self.free_kib)), ""
        return 0, self.left, ""


INSTANCES = [Instance("tsdb-1", role="replica", data_directory="/home/postgres/pgdata/data"),
             Instance("tsdb-0", role="primary", data_directory="/home/postgres/pgdata/data")]


@pytest.mark.parametrize("link,free_gib,status", [
    (False, 9, FAIL),    # a copy of 10 GiB does not fit in 9
    (False, 12, PASS),
    (True, 9, PASS),     # linking needs only a margin
    (True, 0.1, FAIL),
])
def test_pg_upgrade_headroom_needs_the_data_directory_again_only_when_copying(link, free_gib, status):
    kube = DiskKube(10 * GIB // 1024, int(free_gib * GIB) // 1024)
    result = check_pg_upgrade_headroom(kube, PreflightOptions("x", pg_upgrade_link=link), INSTANCES)
    assert result.status == status, result.details
    assert all(pod == "tsdb-0" for pod, _ in kube.seen)


def test_pg_upgrade_headroom_lists_the_directories_earlier_hops_left():
    kube = DiskKube(1024, 10 * GIB // 1024,
                    left="/home/postgres/pgdata/data.pg12\n/home/postgres/pgdata/data.pg13\n")
    result = check_pg_upgrade_headroom(kube, PreflightOptions("x", pg_upgrade_link=False), INSTANCES)
    assert any("data.pg12, /home/postgres/pgdata/data.pg13" in d and "remove them" in d
               for d in result.details)


def test_pg_upgrade_headroom_without_ordinal_0_is_unknown():
    result = check_pg_upgrade_headroom(DiskKube(1, 1), PreflightOptions("x", pg_upgrade_link=False),
                                       INSTANCES[:1])
    assert result.status == UNKNOWN


class ArchiveKube:
    def __init__(self, answer, rc=0):
        self.answer, self.rc = answer, rc

    def psql(self, pod, container, database, user, sql):
        assert pod == "tsdb-0" and "archive_mode" in sql
        return self.rc, self.answer, "" if self.rc == 0 else "permission denied"


@pytest.mark.parametrize("answer,rc,status", [
    ("off|", 0, PASS),
    ("on|pgbackrest --stanza=tsdb archive-push %p", 0, UNKNOWN),
    ("", 1, UNKNOWN),
])
def test_archiving_before_a_pg_upgrade_is_flagged_never_failed(answer, rc, status):
    """R11: the archive and backups stop matching; the tool cannot see the
    backup setup, so it says so rather than failing."""
    result = check_archiving(ArchiveKube(answer, rc), PreflightOptions("x"), INSTANCES)
    assert result.status == status
    if rc == 0:
        assert any("stanza-upgrade" in d for d in result.details)


# --------------------------------------------------------------------------
# Other extensions (R9): what pg_upgrade --check would find, found first
# --------------------------------------------------------------------------

RUN13 = "timescale/timescaledb-ha:pg13.22-ts2.15.3"
UP13 = "timescale/timescaledb-ha:pg13.12-ts2.11.2-all"


def _contents_file(tmp_path, run_libs, up_libs, run_ext=None):
    doc = {"repository": "timescale/timescaledb-ha", "images": {
        "pg13.22-ts2.15.3": {"pg": {"13": {"installable": ["2.11.2"], "libraries": run_libs,
                                           "extensions": run_ext or {"plpgsql": ["1.0"],
                                                                     "timescaledb": ["2.11.2"],
                                                                     "postgis": ["3.4.2"]}}}},
        "pg13.12-ts2.11.2-all": {"pg": {"13": {"installable": ["2.11.2"], "libraries": up_libs,
                                               "extensions": {"plpgsql": ["1.0"],
                                                              "timescaledb": ["2.11.2"],
                                                              "postgis": ["3.4.2"]}}}},
    }}
    path = tmp_path / "contents.json"
    path.write_text(json.dumps(doc))
    return str(path)


class ExtKube:
    def psql(self, pod, container, database, user, sql):
        assert pod == "tsdb-0"
        # What pg_upgrade --check would look at: user C functions only.
        assert "oid >= 16384" in sql and "prolang = 13" in sql
        return 0, "\n".join(["ext|plpgsql|1.0", "ext|postgis|3.4.2", "lib|$libdir/postgis-3"]), ""


EXT_INSTANCES = [Instance("tsdb-0", role="primary", extensions={"app": "2.11.2"})]
ALL_LIBS = ["plpgsql", "postgis-3"]


def _ext_opts(path, **kw):
    return PreflightOptions(RUN13, contents_path=path, pg_upgrade_image=UP13, pg_upgrade_to=13, **kw)


def test_every_library_and_extension_in_use_shipped_passes(tmp_path):
    result = check_extensions(ExtKube(), _ext_opts(_contents_file(tmp_path, ALL_LIBS, ALL_LIBS)),
                              EXT_INSTANCES, "13.22")
    assert result.status == PASS, result.details


def test_a_library_the_pg_upgrade_image_lacks_fails_before_anything_stops(tmp_path):
    path = _contents_file(tmp_path, ALL_LIBS, ["plpgsql"])
    result = check_extensions(ExtKube(), _ext_opts(path), EXT_INSTANCES, "13.22")
    assert result.status == FAIL
    assert any("runs pg_upgrade" in d and "$libdir/postgis-3" in d for d in result.details)


def test_an_extension_version_without_an_install_script_is_unknown(tmp_path):
    path = _contents_file(tmp_path, ALL_LIBS, ALL_LIBS,
                          run_ext={"plpgsql": ["1.0"], "timescaledb": ["2.11.2"], "postgis": ["3.5.0"]})
    result = check_extensions(ExtKube(), _ext_opts(path), EXT_INSTANCES, "13.22")
    assert result.status == UNKNOWN
    assert any("postgis 3.4.2: no install script" in d for d in result.details)


def test_an_image_measured_before_extensions_were_recorded_is_unknown(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"repository": "timescale/timescaledb-ha", "images": {}}))
    result = check_extensions(ExtKube(), PreflightOptions(RUN13, contents_path=str(path)),
                              EXT_INSTANCES, "13.22")
    assert result.status == UNKNOWN
