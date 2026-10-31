"""Tests for `plan --format script`: the plan as a script a person runs by hand.

What would hurt if wrong: an update that skips a stop, a script that runs the
deployment's steps itself (it cannot know how), or one bash will not parse.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from tsupgrade import cli
from tsupgrade.matrix import Matrix
from tsupgrade.plan import build_plan, render_script
from test_majors import IMAGES, R, _plan as cross_plan
from test_plan import PG15_28

BASH = shutil.which("bash")


def _same_major():
    return render_script(build_plan("15", "2.20.3", PG15_28, matrix=Matrix([]), verification_run=True))


def test_every_update_runs_in_order_with_its_stop_and_why():
    script = _same_major()
    first = script.index("UPDATE TO '\\''2.27.2'\\''")
    second = script.index("UPDATE TO '\\''2.28.3'\\''")
    assert first < second
    assert "# stops here: 2.20.3 -> 2.28.3 directly" in script
    assert script.count("check_version '2.27.2'") == 1 and script.count("check_version '2.28.3'") == 1
    assert script.index("check_version '2.20.3'") < first


def test_the_deployments_steps_are_paused_on_not_run():
    script = _same_major()
    assert "kubectl" not in script
    assert "pause 'Roll every instance onto" in script
    assert "VERIFICATION RUN" in script


def test_a_major_change_pauses_for_pg_upgrade_and_warns_off_its_extension_script():
    script = render_script(cross_plan("pg16.15-ts2.30.1"))
    assert script.count("pause 'Upgrade PostgreSQL") == 3
    assert "Do NOT run the update_extensions.sql" in script
    assert "stanza-upgrade" in script
    assert script.rstrip().endswith("on PostgreSQL 16.15'")
    assert "vacuumdb --all --analyze-in-stages" in script


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("make", [_same_major, lambda: render_script(cross_plan("pg16.15-ts2.30.1"))])
def test_bash_parses_the_script(make, tmp_path):
    path = tmp_path / "plan.sh"
    path.write_bytes(make().encode("utf-8"))
    assert subprocess.run([BASH, "-n", str(path)], capture_output=True).returncode == 0


def test_the_command_writes_the_script_with_lf_only(monkeypatch, tmp_path, capfdbinary):
    monkeypatch.setattr(cli, "_profile", lambda ref, *a, **k: PG15_28)
    code = cli.main(["plan", "--from-pg", "15", "--from-ts", "2.20.3", "--target-image",
                     PG15_28.reference, "--verification-run", "--format", "script",
                     "--matrix", str(tmp_path / "none.json")])
    assert code == cli.EXIT_OK
    out = capfdbinary.readouterr().out
    assert out.startswith(b"#!/usr/bin/env bash\n") and b"\r" not in out
