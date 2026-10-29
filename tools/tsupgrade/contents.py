"""What specific images really ship, measured from their filesystems.

The registry labels predict an image's TimescaleDB range, and the prediction
is wrong in ways that matter:

* ``pg15.18-ts2.28.3`` is labelled from 2.12.2, but 2.12.2 has neither an
  install script nor a shared object there. The real floor is 2.13.0, so a
  plan trusting the label sends a 2.12.2 database into a stranded rollout.
* ``pg12.22-ts2.11.2`` ships 1.7.5 and then 2.1.0 onwards -- no 2.0 at all. A
  floor and a ceiling cannot say that.
* The oldest images (``pg11.11-ts2.1.1-p0``, ``pg12.6-ts2.0.1-p0``) carry no
  version labels whatsoever.

So for the images this repository works with, the answer is measured instead:
``ls`` of ``SHAREDIR/extension`` and ``PKGLIBDIR`` in a throwaway container,
reduced by :func:`drift.installable_from_listings` (install script and shared
object both present) -- the same rule preflight applies to a live pod. The
result is ``data/image-contents.json``, written by ``tsupgrade contents
measure`` so it can be re-run rather than trusted.

A measured entry overrules the labels outright. Images that were never
measured fall back to the labels, still marked as a prediction.

Measuring starts no server and writes nothing but a container that ``--rm``
removes. It does need the image locally, which the caller arranges.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .drift import ImageProfile, installable_from_listings
from .images import (
    DEFAULT_REPOSITORY,
    RegistryError,
    parse_image_reference,
    parse_release_tag,
)
from .versions import InvalidVersion, parse_version

__all__ = [
    "CONTENTS_SCHEMA_VERSION",
    "ContentsError",
    "ImageContents",
    "default_contents_path",
    "load_contents",
    "measure_image",
    "merge_measurements",
    "parse_measurement",
]

CONTENTS_SCHEMA_VERSION = 1

_METHOD = (
    "docker run --rm --entrypoint sh <image>, then for each PostgreSQL major "
    "under /usr/lib/postgresql: ls of share/postgresql/<major>/extension and "
    "lib/postgresql/<major>/lib, `postgres --version`, and default_version "
    "from timescaledb.control. Installable = install script AND shared object."
)

# Run inside the image. Prints one block per PostgreSQL major. Only `ls`,
# `sed` and `postgres --version` -- no server is started.
_PROBE = r"""
for m in $(ls /usr/lib/postgresql); do
  echo "== $m"
  echo "-- server_version"; /usr/lib/postgresql/$m/bin/postgres --version
  echo "-- default_version"
  sed -n "s/^default_version *= *.\([0-9.]*\).*/\1/p" \
    /usr/share/postgresql/$m/extension/timescaledb.control 2>/dev/null
  echo "-- sharedir"; ls /usr/share/postgresql/$m/extension 2>/dev/null
  echo "-- pkglibdir"; ls /usr/lib/postgresql/$m/lib 2>/dev/null
done
"""


class ContentsError(Exception):
    """The contents file is missing, malformed, or a measurement failed."""


def _key(version: str) -> Tuple[int, ...]:
    return parse_version(version)


def _tag_for(reference: str, repository: str) -> Optional[str]:
    try:
        registry, repo, tag = parse_image_reference(reference)
    except RegistryError:
        return None
    if registry != "registry-1.docker.io" or repo != repository:
        return None
    return tag


class ImageContents:
    """Queryable view over ``data/image-contents.json``."""

    def __init__(self, doc: Dict[str, Any]) -> None:
        self.doc = doc
        self.repository: str = doc.get("repository", DEFAULT_REPOSITORY)
        self.images: Dict[str, Dict[str, Any]] = dict(doc.get("images", {}))

    def __len__(self) -> int:
        return len(self.images)

    def entry(self, reference: str) -> Optional[Dict[str, Any]]:
        tag = _tag_for(reference, self.repository)
        return self.images.get(tag) if tag is not None else None

    def profile(self, reference: str, pg_major: Optional[int] = None) -> Optional[ImageProfile]:
        """The measured profile for ``reference``, or None if never measured.

        ``pg_major`` picks which PostgreSQL major's set to use for a multi-PG
        image; by default the highest it ships. A major the image does not
        ship still yields a profile, one that does not serve it, so bridging
        skips it for the right reason.
        """
        entry = self.entry(reference)
        if entry is None:
            return None
        per_major = {int(m): v for m, v in entry.get("pg", {}).items() if v.get("installable")}
        if not per_major:
            return None
        majors = tuple(sorted(per_major))
        chosen = per_major[pg_major if pg_major in per_major else majors[-1]]
        installable = tuple(sorted(chosen["installable"], key=_key))
        return ImageProfile(
            reference=reference,
            pg_majors=majors,
            ts_floor=installable[0],
            ts_ceiling=installable[-1],
            ts_default=chosen.get("default_version"),
            from_filesystem=True,
            notes=(
                "installable versions measured from the image filesystem on {0} "
                "({1}); the labels are not used".format(
                    entry.get("measured_at", "unknown date"), entry.get("digest", "no digest")
                ),
            ),
            installable=installable,
        )


def default_contents_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "image-contents.json"


def load_contents(path: Optional[object] = None) -> ImageContents:
    target = Path(path) if path is not None else default_contents_path()
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentsError(
            "image contents not found: {0} (run `make image-contents`)".format(target)
        ) from exc
    except json.JSONDecodeError as exc:
        raise ContentsError("{0} is not valid JSON: {1}".format(target, exc)) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("images"), dict):
        raise ContentsError("{0}: expected an object with an 'images' object".format(target))
    return ImageContents(doc)


def parse_measurement(output: str) -> Dict[str, Dict[str, Any]]:
    """Reduce the probe's output to ``{major: {server_version, default_version,
    installable}}``. Majors with nothing installable are dropped."""
    blocks: Dict[str, Dict[str, List[str]]] = {}
    major: Optional[str] = None
    section: Optional[str] = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("== "):
            major = line[3:].strip()
            blocks[major] = {}
            section = None
        elif line.startswith("-- "):
            section = line[3:].strip()
            if major is not None:
                blocks[major][section] = []
        elif line and major is not None and section is not None:
            blocks[major][section].append(line)

    result: Dict[str, Dict[str, Any]] = {}
    for major, sections in blocks.items():
        installable = installable_from_listings(
            sections.get("sharedir", []), sections.get("pkglibdir", [])
        )
        if not installable:
            continue
        # "postgres (PostgreSQL) 15.18 (Ubuntu ...)" -> "15.18"
        server = None
        for line in sections.get("server_version", []):
            for word in line.split():
                try:
                    if parse_version(word)[0] == int(major):
                        server = word
                        break
                except (InvalidVersion, ValueError):
                    continue
        defaults = sections.get("default_version", [])
        result[major] = {
            "server_version": server,
            "default_version": defaults[0] if defaults else None,
            "installable": installable,
        }
    return result


Runner = Callable[[Sequence[str]], str]


def _docker(args: Sequence[str]) -> str:
    try:
        done = subprocess.run(
            ["docker"] + list(args), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise ContentsError("docker is not on PATH") from exc
    if done.returncode != 0:
        raise ContentsError(
            "docker {0} failed: {1}".format(args[0], (done.stderr or done.stdout).strip())
        )
    return done.stdout


def measure_image(reference: str, run: Runner = _docker) -> Dict[str, Any]:
    """Measure one locally present image. Returns its contents entry."""
    digests = run(["image", "inspect", "--format", "{{json .RepoDigests}}", reference])
    try:
        repo_digests = json.loads(digests) or []
    except json.JSONDecodeError:
        repo_digests = []
    digest = repo_digests[0].split("@", 1)[1] if repo_digests else None

    per_major = parse_measurement(
        run(["run", "--rm", "--entrypoint", "sh", reference, "-c", _PROBE])
    )
    if not per_major:
        raise ContentsError("{0}: no installable TimescaleDB version found".format(reference))
    return {
        "digest": digest,
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "pg": per_major,
    }


def merge_measurements(
    existing: Optional[Dict[str, Any]], measured: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Fold freshly measured entries (keyed by tag) into a contents document."""
    doc = dict(existing or {})
    doc["schema_version"] = CONTENTS_SCHEMA_VERSION
    doc.setdefault("repository", DEFAULT_REPOSITORY)
    doc["method"] = _METHOD
    doc["note"] = (
        "Generated by `make image-contents`. Records what each image can "
        "install, measured from its filesystem; overrules the registry labels. "
        "Nothing here says an upgrade between versions is supported."
    )
    images = dict(doc.get("images", {}))
    images.update(measured)
    doc["images"] = {tag: images[tag] for tag in sorted(images, key=_tag_sort_key)}
    return doc


def _tag_sort_key(tag: str) -> Tuple[Any, ...]:
    parsed = parse_release_tag(tag) or {}
    try:
        return (0, _key(parsed["pg"]), _key(parsed["ts"]), tag)
    except (KeyError, InvalidVersion):
        return (1, (), (), tag)
