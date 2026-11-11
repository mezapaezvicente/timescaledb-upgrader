"""A tripwire on upstream fixing the time_bucket_ng conversion.

The update past 2.14 fails on an aggregate built on time_bucket_ng because the
2.14 to 2.15 script calls ``_timescaledb_functions.cagg_migrate_to_time_bucket``
inside the update (measured, 2.15.0 to 2.28.3). Upstream's fix, #7763, moved
it to the post-update part and was closed unmerged. If a release ever ships it
moved, the scripts change, and the gate and the rebuild may no longer be needed
-- or may then be wrong. Nothing here assumes which: it only notices.

``fixtures/ng_conversion_scripts.tsv`` records, per image and PostgreSQL
major, which update scripts carry the call and at which line. This test
derives the same from every measured image present locally and fails on any
difference, or on a measured image the fixture does not cover. Either means:
run ``test_ng_rebuild_container.py`` and the update on that image, and record
what it does. ``python tests/test_ng_tripwire_container.py <tag>...`` prints the
rows to record. Skips without docker.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "ng_conversion_scripts.tsv"
CALL = "CALL _timescaledb_functions.cagg_migrate_to_time_bucket"
REPOSITORY = "timescale/timescaledb-ha"

_FIND = (
    "for dir in /usr/share/postgresql/*/extension; do "
    "ls $dir/timescaledb--*--*.sql >/dev/null 2>&1 || continue; "
    "major=$(basename $(dirname $dir)); "
    "grep -n -m1 -H '" + CALL + "' $dir/timescaledb--*--*.sql | sed \"s|^$dir/|$major |\"; "
    "echo \"$major end\"; done"
)


def _key(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def derive(tag: str) -> List[str]:
    """``major, scripts with the call, first and last target, digest`` per major."""
    done = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", "{0}:{1}".format(REPOSITORY, tag),
                           "-c", _FIND], capture_output=True)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf-8", "replace"))
    found = {}
    for line in done.stdout.decode("utf-8").splitlines():
        major, _, rest = line.partition(" ")
        found.setdefault(major, [])
        if rest != "end":
            script, line_no = rest.split(":")[:2]
            found[major].append("{0}:{1}".format(script, line_no))
    rows = []
    for major, hits in sorted(found.items(), key=lambda kv: int(kv[0])):
        hits.sort()
        targets = sorted({h.split("--")[2].split(".sql")[0] for h in hits}, key=_key)
        digest = hashlib.sha256("\n".join(hits).encode("utf-8")).hexdigest()[:16]
        rows.append("\t".join([tag, major, str(len(hits)), targets[0] if targets else "-",
                               targets[-1] if targets else "-", digest]))
    return rows


def _recorded() -> List[List[str]]:
    return [line.split("\t") for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")]


def _measured_tags() -> List[str]:
    doc = json.loads((REPO / "data" / "image-contents.json").read_text(encoding="utf-8"))
    return list(doc["images"])


def _local(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", "{0}:{1}".format(REPOSITORY, tag)],
                          capture_output=True).returncode == 0


def test_the_fixture_says_the_call_is_only_in_scripts_to_215_through_224():
    """What the gate and the rebuild rest on, as recorded: no script to 2.25 or
    later converts (time_bucket_ng is gone there), and every one that does
    targets 2.15 to 2.24."""
    for tag, major, count, first, last, _ in _recorded():
        if count == "0":
            continue
        assert (2, 15) <= _key(first)[:2] and _key(last)[:2] <= (2, 24), (tag, major, first, last)


@pytest.mark.skipif(not shutil.which("docker"), reason="needs docker")
def test_every_local_measured_image_still_carries_the_call_where_recorded():
    recorded = {(r[0], r[1]): r for r in _recorded()}
    local = [tag for tag in _measured_tags() if _local(tag)]
    if not local:
        pytest.skip("no measured image is present locally")
    for tag in local:
        rows = derive(tag)
        for row in rows:
            fields = row.split("\t")
            assert (tag, fields[1]) in recorded, (
                "{0} pg{1} is not in {2}: re-measure the time_bucket_ng update on it, then record "
                "it".format(tag, fields[1], FIXTURE.name))
            assert fields == recorded[(tag, fields[1])], (
                "the time_bucket_ng conversion changed in {0} pg{1}: re-measure whether the update past "
                "2.14 still fails (NOTES question 6) before trusting the gate or the rebuild".format(tag, fields[1]))


if __name__ == "__main__":
    for tag in sys.argv[1:] or [t for t in _measured_tags() if _local(t)]:
        for row in derive(tag):
            print(row)
