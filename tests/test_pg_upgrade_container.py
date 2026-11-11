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
from tsupgrade.rollback import _SWAP_SCRIPT

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
mkdir -p "$(dirname {data})"
/usr/lib/postgresql/12/bin/initdb -D {data} -U postgres -E UTF8 --locale=C.UTF-8 \
  --data-checksums >/dev/null
{extra}
cat > /tmp/pg_upgrade.sh <<'__TSUPGRADE_EOF__'
{script}
__TSUPGRADE_EOF__
set +e
FROM=12 TO=13 PG_BIN_OLD=/usr/lib/postgresql/12/bin PG_BIN_NEW=/usr/lib/postgresql/13/bin \
DATA={data} LINK={link} ENCODING=UTF8 LC_COLLATE=C.UTF-8 LC_CTYPE=C.UTF-8 \
LOCALE_PROVIDER=libc PROVIDER_LOCALE= bash /tmp/pg_upgrade.sh
echo "exit $?"
cat {data}/PG_VERSION 2>/dev/null | sed 's/^/PG_VERSION /'
"""


def _run(link: str = "", extra: str = "", data: str = "/tmp/pg/data") -> str:
    program = SETUP.format(script=_PG_UPGRADE_SCRIPT, link=link, extra=extra, data=data)
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


def test_a_real_upgrade_at_the_charts_data_directory():
    """R25: the archived chart's PGDATA, /var/lib/postgresql/data, with the
    volume mounted at /var/lib/postgresql. The kept directory lands beside it."""
    out = _run(data="/var/lib/postgresql/data")
    assert "exit 0" in out, out
    assert "PG_VERSION 13" in out
    assert "upgraded to PostgreSQL 13; the old data directory is /var/lib/postgresql/data.pg12" in out


# R26: the archived chart keeps WAL on a second volume, pg_wal a symlink into
# it. /var/lib/postgresql stands for the data volume, .../wal for the WAL one.
WAL_CASE = r"""
set -e
V=/var/lib/postgresql; D=$V/data; W=$V/wal/pg_wal
B12=/usr/lib/postgresql/12/bin; B13=/usr/lib/postgresql/13/bin
mkdir -p $V/wal
$B12/initdb -D $D --waldir=$W -U postgres -E UTF8 --locale=C.UTF-8 >/dev/null
$B12/pg_ctl -D $D -o '-k /tmp' -w start >/dev/null
$B12/psql -h /tmp -U postgres -qc 'CREATE TABLE kept AS SELECT 12 AS major'
$B12/pg_ctl -D $D -w stop >/dev/null
cat > /tmp/pg_upgrade.sh <<'__TSUPGRADE_EOF__'
{script}
__TSUPGRADE_EOF__
cat > /tmp/swap.sh <<'__TSUPGRADE_EOF__'
{swap}
__TSUPGRADE_EOF__
set +e
FROM=12 TO=13 PG_BIN_OLD=$B12 PG_BIN_NEW=$B13 DATA=$D LINK={link} ENCODING=UTF8 \
LC_COLLATE=C.UTF-8 LC_CTYPE=C.UTF-8 LOCALE_PROVIDER=libc PROVIDER_LOCALE= bash /tmp/pg_upgrade.sh
echo "upgrade exit $?"
set -e
echo "new pg_wal -> $(readlink $D/pg_wal)"
echo "kept pg_wal -> $(readlink $D.pg12/pg_wal)"
$B13/pg_ctl -D $D -o '-k /tmp' -w start >/dev/null
echo "new runs $($B13/psql -h /tmp -U postgres -Atc 'SHOW server_version_num') with $($B13/psql -h /tmp -U postgres -Atc 'SELECT major FROM kept')"
$B13/psql -h /tmp -U postgres -qc 'CREATE TABLE after_upgrade()' -c 'CHECKPOINT' -c 'SELECT pg_switch_wal()' >/dev/null
echo "new WAL segments in $W: $(ls $W | grep -c '^0000')"
$B13/pg_ctl -D $D -w stop >/dev/null
[ -n "{rollback}" ] || exit 0
set +e
FROM=12 TO=13 DATA=$D KEPT=$D.pg12 SYSID=7000000000000000002 bash /tmp/swap.sh
echo "swap exit $?"
set -e
echo "back pg_wal -> $(readlink $D/pg_wal)"
echo "aside pg_wal -> $(readlink $D.rolled-back-pg13-7000000000000000002/pg_wal)"
$B12/pg_ctl -D $D -o '-k /tmp' -w start >/dev/null
echo "old runs $($B12/psql -h /tmp -U postgres -Atc 'SHOW server_version_num'), after_upgrade $($B12/psql -h /tmp -U postgres -Atc "SELECT count(*) FROM pg_class WHERE relname = 'after_upgrade'")"
$B12/pg_ctl -D $D -w stop >/dev/null
"""


def _run_wal(link: str = "", rollback: bool = False) -> str:
    program = WAL_CASE.replace("{script}", _PG_UPGRADE_SCRIPT).replace("{swap}", _SWAP_SCRIPT) \
        .replace("{link}", link).replace("{rollback}", "yes" if rollback else "")
    done = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "bash", IMAGE, "-s"],
                          input=program.encode("utf-8"), capture_output=True, timeout=600)
    return (done.stdout + done.stderr).decode("utf-8", "replace")


@pytest.mark.parametrize("link", ["", "--link"])
def test_wal_on_its_own_volume_stays_there_and_the_old_wal_is_kept(link):
    out = _run_wal(link=link)
    assert "upgrade exit 0" in out, out
    assert "new pg_wal -> /var/lib/postgresql/wal/pg_wal\n" in out, out
    assert "kept pg_wal -> /var/lib/postgresql/wal/pg_wal.pg12\n" in out, out
    assert "WAL: PostgreSQL 13's is /var/lib/postgresql/wal/pg_wal, PostgreSQL 12's is kept as " \
           "/var/lib/postgresql/wal/pg_wal.pg12" in out
    assert "new runs 130" in out and "with 12" in out, out
    segments = [l for l in out.splitlines() if l.startswith("new WAL segments in")]
    assert segments and int(segments[0].rsplit(":", 1)[1]) > 0, out
    assert "/var/lib/postgresql/wal/pg_wal.pg12" in out.split("to remove once the result is accepted:")[1]


def test_the_rollback_swap_brings_back_the_old_data_and_its_wal():
    out = _run_wal(rollback=True)
    assert "swap exit 0" in out, out
    assert "back pg_wal -> /var/lib/postgresql/wal/pg_wal\n" in out, out
    assert ("aside pg_wal -> /var/lib/postgresql/wal/pg_wal.rolled-back-pg13-7000000000000000002\n"
            in out), out
    assert "old runs 120" in out and "after_upgrade 0" in out, out
