"""Routes across PostgreSQL majors: which images, which releases, in what order.

A PostgreSQL major upgrade runs ``pg_upgrade`` inside one image that carries
both majors' binaries, and both sides need the *same* TimescaleDB release
installed: ``pg_upgrade`` carries the extension's catalog across, not its
version. So a route is a sequence of same-major segments joined by major hops:

1. on the old major, update the extension to a release X that some image
   ships for both majors (the usual same-major chain, stops included);
2. ``pg_upgrade`` in that image, at X;
3. restart on an image of the new major that ships X, and carry on.

The search wants the fewest ``pg_upgrade`` runs, since each one is an
outage. At each major hop it takes the highest shared release that is not
below where the database is and not above the target, which is the order
TimescaleDB documents: update the extension first, then PostgreSQL.

Only measured contents are used (``installable`` per major), because the
labels describe one major per image. An image runs a cluster only on the
major its tag names, since that is the one its PATH starts; carrying another
major's binaries makes it a ``pg_upgrade`` image, nothing more. No published
image pairs pg12 with anything newer than pg13, for example, so every pg12
route starts with 12 -> 13.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .drift import BridgeHop, ImageProfile, plan_chain
from .images import RegistryError, parse_image_reference, parse_release_tag
from .versions import InvalidVersion, parse_version

__all__ = ["MajorHop", "Route", "tag_major", "find_route"]

# What the planner gets: image reference -> {PostgreSQL major -> profile of
# what that major's directory installs}.
Profiles = Dict[str, Dict[int, ImageProfile]]


@dataclass(frozen=True)
class MajorHop:
    """One ``pg_upgrade`` run."""

    image: str      # carries both majors; the upgrade runs in it
    from_pg: int
    to_pg: int
    ts: str         # the TimescaleDB release on both sides
    run_image: str  # the new major's image the cluster restarts on


Step = Union[BridgeHop, MajorHop]


@dataclass(frozen=True)
class Route:
    steps: Tuple[Step, ...]

    @property
    def majors(self) -> Tuple[int, ...]:
        hops = [s for s in self.steps if isinstance(s, MajorHop)]
        return tuple([hops[0].from_pg] + [h.to_pg for h in hops]) if hops else ()


def _key(version: str) -> Tuple[int, ...]:
    return parse_version(version)


def tag_major(reference: str) -> Optional[int]:
    """The PostgreSQL major an image runs: the one its tag names."""
    try:
        _, _, tag = parse_image_reference(reference)
        pg = (parse_release_tag(tag) or {}).get("pg")
        return parse_version(pg)[0] if pg else None
    except (RegistryError, InvalidVersion):
        return None


def _single(reference: str) -> bool:
    """A tag with one major: the image a cluster normally runs, and smaller."""
    return not reference.split(":")[-1].endswith("-all")


def _runners(profiles: Profiles, major: int) -> List[ImageProfile]:
    """Images that can run a cluster on ``major``: reaching furthest first,
    then a plain tag before an ``-all`` one that installs the same."""
    found = [p[major] for ref, p in profiles.items() if major in p and tag_major(ref) == major]
    return sorted(found, key=lambda p: (_key(p.ts_ceiling), _single(p.reference), p.reference),
                  reverse=True)


def _segment(
    start: str, end: str, major: int, profiles: Profiles, target: Optional[ImageProfile] = None
) -> Tuple[List[BridgeHop], Optional[str]]:
    """Same-major hops from ``start`` to exactly ``end``.

    Lands on ``target`` when given (the route's final image); otherwise on
    the runner that reaches furthest among those shipping ``end``.
    """
    if _key(start) == _key(end):
        return [], None
    runners = _runners(profiles, major)
    if target is None:
        landing = [p for p in runners if p.accepts(end)]
        if not landing:
            return [], "no pg{0} image ships {1}".format(major, end)
        target = landing[0]
    elif not target.accepts(end):
        return [], "{0} does not ship {1} for pg{2}".format(target.reference, end, major)
    hops, problem = plan_chain(start, target, runners, major)
    if problem is not None:
        return [], problem
    if any(_key(h.to_version) > _key(end) for h in hops[:-1]):
        return [], "bridging on pg{0} passes {1}".format(major, end)
    if hops and _key(hops[-1].to_version) != _key(end):
        last = hops[-1]
        hops[-1] = BridgeHop(last.image, last.from_version, end)
    return hops, None


def _paths(source: int, target: int, profiles: Profiles) -> List[List[Tuple[int, int]]]:
    """Every rising sequence of major hops some image can make, fewest first."""
    edges = set()
    for per in profiles.values():
        for a, b in combinations(sorted(per), 2):
            if source <= a < b <= target:
                edges.add((a, b))
    found: List[List[Tuple[int, int]]] = []

    def walk(at: int, path: List[Tuple[int, int]]) -> None:
        if at == target:
            found.append(list(path))
            return
        for a, b in sorted(edges, key=lambda e: -e[1]):
            if a == at:
                walk(b, path + [(a, b)])

    walk(source, [])
    # Fewest hops; among those, the biggest jumps first.
    return sorted(found, key=lambda p: (len(p), [-b for _, b in p]))


def _bridges(profiles: Profiles, a: int, b: int) -> List[str]:
    """Images carrying both majors: those tagged with the new major first."""
    refs = [ref for ref, per in profiles.items() if a in per and b in per]
    return sorted(refs, key=lambda r: (tag_major(r) == b, r), reverse=True)


def _pg_upgrade_options(
    profiles: Profiles, a: int, b: int, current: str, target_ts: str
) -> List[Tuple[str, str]]:
    """``(image, release)`` pairs that can run ``pg_upgrade`` from ``a`` to ``b``,
    best first.

    A pair qualifies when the image ships ``release`` for both majors and the
    release is not below where the database is nor above the target. Best
    means: the highest shared release first (update the extension as far as
    possible on the old major, which is TimescaleDB's documented order), and
    for one release, the image tagged with the new major before the others
    (:func:`_bridges`' order), since that is the image whose new-major
    binaries the cluster will keep running.
    """
    bridges = _bridges(profiles, a, b)
    preference = {ref: i for i, ref in enumerate(bridges)}
    options = []
    for ref in bridges:
        shared = set(profiles[ref][a].installable) & set(profiles[ref][b].installable)
        options.extend((ref, x) for x in shared if _key(current) <= _key(x) <= _key(target_ts))

    def best_first(option: Tuple[str, str]) -> Tuple[Tuple[int, ...], int]:
        ref, release = option
        # Negated: a higher release, then an earlier image in _bridges, sorts first.
        return tuple(-p for p in _key(release)), preference[ref]

    return sorted(options, key=best_first)


def find_route(
    source_major: int,
    source_ts: str,
    target_major: int,
    target_ts: str,
    target: str,
    profiles: Profiles,
) -> Tuple[Optional[Route], Optional[str]]:
    """The route from ``source_ts`` on ``source_major`` to ``target_ts`` on ``target``.

    Returns ``(route, None)`` or ``(None, why)``. ``target`` must be a key of
    ``profiles`` that serves ``target_major``.
    """
    final = profiles.get(target, {}).get(target_major)
    if final is None or tag_major(target) != target_major:
        return None, "{0} has no measured pg{1} contents".format(target, target_major)
    paths = _paths(source_major, target_major, profiles)
    if not paths:
        return None, "no measured image carries a pg_upgrade from pg{0} towards pg{1}".format(
            source_major, target_major)

    why = "no route found"
    for path in paths:
        route, problem = _try(path, source_ts, target_ts, final, profiles)
        if route is not None:
            return route, None
        why = problem or why
    return None, why


def _try(
    path: Sequence[Tuple[int, int]],
    source_ts: str,
    target_ts: str,
    final: ImageProfile,
    profiles: Profiles,
) -> Tuple[Optional[Route], Optional[str]]:
    steps: List[Step] = []
    current = source_ts
    for a, b in path:
        chosen = None
        problem = "no image ships one TimescaleDB release for both pg{0} and pg{1} at or above {2}".format(
            a, b, current)
        for ref, x in _pg_upgrade_options(profiles, a, b, current, target_ts):
            if not any(p.accepts(x) for p in _runners(profiles, b)):
                problem = "no pg{0} image ships {1} to restart on".format(b, x)
                continue
            hops, why = _segment(current, x, a, profiles)
            if why is not None:
                problem = why
                continue
            chosen = (hops, ref, x)
            break
        if chosen is None:
            return None, problem
        hops, ref, x = chosen
        steps.extend(hops)
        steps.append(MajorHop(ref, a, b, x, run_image=""))
        current = x

    hops, why = _segment(current, target_ts, path[-1][1], profiles, target=final)
    if why is not None:
        return None, why
    steps.extend(hops)
    return Route(tuple(_with_run_images(steps, profiles, final))), None


def _with_run_images(steps: List[Step], profiles: Profiles, final: ImageProfile) -> List[Step]:
    """Each major hop restarts on the image the next segment begins with.

    When the next segment is empty, that is the final image if the hop lands
    on the target major, or else the runner reaching furthest that ships the
    release.
    """
    out: List[Step] = list(steps)
    for i, step in enumerate(out):
        if not isinstance(step, MajorHop):
            continue
        following = out[i + 1] if i + 1 < len(out) else None
        if isinstance(following, BridgeHop):
            image = following.image
        elif step.to_pg == tag_major(final.reference) and final.accepts(step.ts) and not any(
            isinstance(s, MajorHop) for s in out[i + 1:]
        ):
            image = final.reference
        else:
            image = next(p.reference for p in _runners(profiles, step.to_pg) if p.accepts(step.ts))
        out[i] = MajorHop(step.image, step.from_pg, step.to_pg, step.ts, image)
    return out
