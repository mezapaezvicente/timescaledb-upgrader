"""The pg_upgrade script against real binaries, in a throwaway container.

Skipped unless Docker answers and the image is already local (nothing is
pulled), or when TSUPGRADE_NO_DOCKER is set. One image carries both majors
(``pg13.12-ts2.11.2-all``: 12 and 13), as in the 12 -> 13 hop. Each case is
its own ``docker run --rm``; nothing outside the container is written.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tsupgrade.apply import _PG_UPGRADE_SCRIPT

IMAGE = "timescale/timescaledb-ha:pg13.12-ts2.11.2-all"


def _docker_ready() -> bool:
    if os.environ.get("TSUPGRADE_NO_DOCKER") or not shutil.which("docker"):
        return False
    try:
        done = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(), reason="Docker or {0} not available".format(IMAGE))

SETUP = r"""
set -e
mkdir -p /tmp/pg
/usr/lib/postgresql/12/bin/initdb -D /tmp/pg/data -U postgres -E UTF8 --locale=C.UTF-8 \
  --data-checksums >/dev/null
{extra}
cat > /tmp/pg_upgrade.sh <<'__TSUPGRADE_EOF__'
{script}
__TSUPGRADE_EOF__
set +e
FROM=12 TO=13 PG_BIN_OLD=/usr/lib/postgresql/12/bin PG_BIN_NEW=/usr/lib/postgresql/13/bin \
DATA=/tmp/pg/data LINK={link} ENCODING=UTF8 LC_COLLATE=C.UTF-8 LC_CTYPE=C.UTF-8 \
LOCALE_PROVIDER=libc PROVIDER_LOCALE= bash /tmp/pg_upgrade.sh
echo "exit $?"
cat /tmp/pg/data/PG_VERSION 2>/dev/null | sed 's/^/PG_VERSION /'
"""


def _run(link: str = "", extra: str = "") -> str:
    program = SETUP.format(script=_PG_UPGRADE_SCRIPT, link=link, extra=extra)
    # Bytes, not text: on Windows text mode would send CRLF into bash.
    done = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "bash", IMAGE, "-s"],
                          input=program.encode("utf-8"), capture_output=True, timeout=600)
    return (done.stdout + done.stderr).decode("utf-8", "replace")


def test_a_real_copy_upgrades_12_to_13_and_keeps_the_old_directory():
    out = _run()
    assert "exit 0" in out, out
    assert "PG_VERSION 13" in out
    assert "relation files sharing an inode with /tmp/pg/data.pg12: 0" in out
    assert "to remove once the result is accepted: /tmp/pg/data.pg12" in out


def test_a_real_link_shares_the_relation_files():
    out = _run(link="--link")
    assert "exit 0" in out, out
    shared = [l for l in out.splitlines() if l.startswith("relation files sharing an inode")]
    assert shared and int(shared[0].rsplit(":", 1)[1]) > 0, out


def test_a_real_check_failure_leaves_the_old_cluster_as_it_was():
    """A database that refuses connections fails pg_upgrade --check ("All
    non-template0 databases must allow connections"), before any linking,
    so the trap reports nothing changed."""
    closed = (
        "B=/usr/lib/postgresql/12/bin\n"
        "$B/pg_ctl -D /tmp/pg/data -o '-k /tmp' -w start >/dev/null\n"
        "$B/psql -h /tmp -U postgres -qc 'CREATE DATABASE closed' "
        "-c 'ALTER DATABASE closed ALLOW_CONNECTIONS false'\n"
        "$B/pg_ctl -D /tmp/pg/data -w stop >/dev/null")
    out = _run(link="--link", extra=closed)
    assert "exit 1" in out, out
    assert "pg_upgrade --check failed" in out
    assert "old cluster disabled" not in out and "moved" not in out
    assert "PG_VERSION 12" in out
