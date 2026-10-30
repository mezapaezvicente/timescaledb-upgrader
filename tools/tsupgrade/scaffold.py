"""Generate the skeleton of the upgrade matrix from the image catalog.

The transitions worth listing are derived from images that actually exist, so
the matrix does not fill up with rows about version pairs nobody can run.

What this module will and will not decide:

* It **will** set ``no_image``, verified, with the registry as the source. That
  verdict is a statement about the registry, and the registry answered it.
* It **will not** set any other verdict. Whether a transition works in place or
  needs a dump and restore is a compatibility fact; rows get ``verdict: null``,
  ``verified: false`` and an empty reason, for a human to fill in with a
  citation.

Existing entries are never overwritten, so re-running this after refreshing the
catalog adds newly possible transitions and leaves verified work alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .images import ImageCatalog
from .matrix import Matrix

__all__ = ["candidate_transitions", "scaffold_matrix"]

Transition = Tuple[str, str, str, str]


def _series_str(series: Tuple[int, int]) -> str:
    return "{0}.{1}".format(series[0], series[1])


def candidate_transitions(catalog: ImageCatalog) -> List[Transition]:
    """Transitions worth having an opinion about, derived from real images.

    Three families:

    1. **PostgreSQL major upgrades** between every pair of majors that has
       images, each at the newest TimescaleDB series that major offers.
    2. **TimescaleDB series upgrades** within a PostgreSQL major, from each
       published series to the newest one for that major.
    3. **"Just move the extension to the newest release"** for each major. For
       older majors the newest series overall was never published against them,
       which is exactly the ``no_image`` case worth recording.
    """
    majors = catalog.pg_majors()
    if not majors:
        return []

    newest_overall: Optional[Tuple[int, int]] = None
    for major in majors:
        latest = catalog.latest_ts_series(major)
        if latest is not None and (newest_overall is None or latest > newest_overall):
            newest_overall = latest

    seen: set = set()
    out: List[Transition] = []

    def add(transition: Transition) -> None:
        if transition[:2] == transition[2:]:
            return  # a no-op transition is not an upgrade
        if transition in seen:
            return
        seen.add(transition)
        out.append(transition)

    # 1. PostgreSQL major upgrades.
    for src in majors:
        src_latest = catalog.latest_ts_series(src)
        if src_latest is None:
            continue
        for tgt in majors:
            if tgt <= src:
                continue
            tgt_latest = catalog.latest_ts_series(tgt)
            if tgt_latest is None:
                continue
            add((str(src), _series_str(src_latest), str(tgt), _series_str(tgt_latest)))

    # 2. TimescaleDB series upgrades inside one PostgreSQL major.
    for major in majors:
        series = catalog.ts_series_for(major)
        if len(series) < 2:
            continue
        latest = series[-1]
        for s in series[:-1]:
            add((str(major), _series_str(s), str(major), _series_str(latest)))

    # 3. Move the extension to the newest release overall, same major.
    if newest_overall is not None:
        for major in majors:
            latest = catalog.latest_ts_series(major)
            if latest is None or latest == newest_overall:
                continue
            add(
                (
                    str(major),
                    _series_str(latest),
                    str(major),
                    _series_str(newest_overall),
                )
            )

    return out


def _blank_entry(transition: Transition, catalog: ImageCatalog) -> Dict[str, Any]:
    src_pg, src_ts, tgt_pg, tgt_ts = transition

    # The one verdict this module is allowed to decide, because the registry
    # decides it: is there an image for the target pair at all?
    if not catalog.has_pair(tgt_pg, tgt_ts):
        latest = catalog.latest_ts_series(int(tgt_pg))
        latest_text = _series_str(latest) if latest else "none"
        return {
            "source_pg": src_pg,
            "source_ts": src_ts,
            "target_pg": tgt_pg,
            "target_ts": tgt_ts,
            "verdict": "no_image",
            "reason": (
                "No {repo} tag pairs PostgreSQL {pg} with TimescaleDB {ts}. "
                "The newest TimescaleDB series published for PostgreSQL {pg} "
                "is {latest}.".format(
                    repo=catalog.repository, pg=tgt_pg, ts=tgt_ts, latest=latest_text
                )
            ),
            "steps": [],
            "verified": True,
            "evidence": None,
            "references": [catalog.source_url] if catalog.source_url else [],
        }

    return {
        "source_pg": src_pg,
        "source_ts": src_ts,
        "target_pg": tgt_pg,
        "target_ts": tgt_ts,
        "verdict": None,
        "reason": "",
        "steps": [],
        "verified": False,
        "evidence": None,
        "references": [],
    }


def scaffold_matrix(
    catalog: ImageCatalog,
    existing: Optional[Matrix] = None,
) -> Dict[str, Any]:
    """Build the matrix document, preserving everything already recorded."""
    kept: List[Dict[str, Any]] = []
    index: set = set()

    if existing is not None:
        for entry in existing:
            kept.append(entry.to_dict())
            index.add(
                (entry.source_pg, entry.source_ts, entry.target_pg, entry.target_ts)
            )

    added = 0
    for transition in candidate_transitions(catalog):
        if transition in index:
            continue
        kept.append(_blank_entry(transition, catalog))
        index.add(transition)
        added += 1

    def sort_key(e: Dict[str, Any]):
        def parts(v: Any) -> List[int]:
            return [int(c) if str(c).isdigit() else 0 for c in str(v).split(".")]

        return (
            parts(e["source_pg"]),
            parts(e["source_ts"]),
            parts(e["target_pg"]),
            parts(e["target_ts"]),
        )

    kept.sort(key=sort_key)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "Transitions are scaffolded from data/images.json by "
            "`make matrix-scaffold`. Only no_image verdicts are set "
            "automatically, because only the registry can answer them. Every "
            "other verdict comes from verification runs by the upgrader "
            "(verify/harness.py), with both evidence records named."
        ),
        "verdicts": ["in_place", "dump_restore", "unsupported", "no_image", None],
        "entries": kept,
        "_added_this_run": added,
    }
