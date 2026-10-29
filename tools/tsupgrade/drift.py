"""Version drift between what a database has installed and what its image ships.

The most-reported TimescaleDB upgrade failure is not a compatibility question.
It is::

    extension timescaledb version mismatch: shared library version X; SQL version Y
    could not access file "$libdir/timescaledb-X": No such file or directory

which happens when the image changes and the extension does not. A database at
extension version *V* on an image offering *W* is in one of four states, and two
of them are not upgrade opportunities at all -- they are outages:

``aligned``    ``V`` is the image's default. Nothing to do.
``lagging``    ``V`` is older but still installable here. This is the ordinary
               case an upgrade acts on.
``stranded``   ``V`` is not installable on this image, so the server cannot load
               the extension it has recorded. Rolling *forward* does not fix it.
``ahead``      ``V`` is newer than the image's default -- the image was rolled
               back underneath the catalog. Downgrade scripts are single-step
               only, so the answer is to roll the image forward again.

``stranded`` is the one nothing else reports, and it is not exotic: image floors
rise over time, so "pin the newest image" is a live way to reach it. A pg15
cluster at 2.9 cannot move to ``pg15-ts2.28`` at all, because that image's floor
is 2.13.0. :func:`bridge` computes the sequence of images that gets it there.

Read-only, like preflight. The only in-pod command this adds is ``ls``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .images import ImageCatalog, ImageLabels, RegistryError
from .support import documented_ts_floor
from .versions import InvalidVersion, parse_version

__all__ = [
    "ALIGNED",
    "LAGGING",
    "STRANDED",
    "AHEAD",
    "INDETERMINATE",
    "DriftVerdict",
    "ImageProfile",
    "classify",
    "classify_offline",
    "effective_floor",
    "profile_from_labels",
    "published_floors",
    "bridge",
    "plan_chain",
    "BridgeHop",
    "parse_install_scripts",
    "parse_shared_objects",
    "installable_from_listings",
    "AVAILABLE_VERSIONS_VIEW_WARNING",
]

ALIGNED = "aligned"
LAGGING = "lagging"
STRANDED = "stranded"
AHEAD = "ahead"
# Not a fifth state of the world -- a statement that we could not establish
# which of the four it is. Never collapsed into one of them.
INDETERMINATE = "indeterminate"

# `pg_available_extension_versions` is the obvious source for "what can this
# image install", and it is wrong. On a pg16.15-ts2.30.1 image it reports 56
# versions including 2.12.2, which has neither an install script nor a shared
# object -- PostgreSQL lists it because update scripts *referencing* it exist.
# Asking for it does not fail cleanly; the backend closes the connection:
#
#     DETAIL: The new version is "2.12.2", this session is using version "2.13.0".
#             The session will be restarted.
#     server closed the connection unexpectedly
#
# So code that trusts that view and then issues version-specific DDL looks like
# a crash rather than a validation error. The authoritative set is the
# intersection of two filesystem facts; see `installable_from_listings`.
AVAILABLE_VERSIONS_VIEW_WARNING = (
    "pg_available_extension_versions overstates what an image can install and "
    "requesting one of the phantom versions drops the connection; the "
    "filesystem is the authority"
)

_INSTALL_SCRIPT = re.compile(r"^timescaledb--([0-9]+(?:\.[0-9]+)*)\.sql$")
_SHARED_OBJECT = re.compile(r"^timescaledb-([0-9]+(?:\.[0-9]+)*)\.so$")


def _key(version: object) -> Tuple[int, ...]:
    return parse_version(version)


def _safe_key(version: object) -> Optional[Tuple[int, ...]]:
    try:
        return parse_version(version)
    except InvalidVersion:
        return None


# ---------------------------------------------------------------------------
# What an image can actually install
# ---------------------------------------------------------------------------


def parse_install_scripts(names: Sequence[str]) -> List[str]:
    """Extension versions with an install script, from a bare ``ls``.

    Only ``timescaledb--<version>.sql`` counts. The update scripts
    (``timescaledb--2.13.0--2.30.1.sql``) are a different thing entirely: their
    presence is exactly what makes ``pg_available_extension_versions``
    overstate.
    """
    found = []
    for name in names:
        match = _INSTALL_SCRIPT.match(name.strip())
        if match:
            found.append(match.group(1))
    return sorted(set(found), key=_key)


def parse_shared_objects(names: Sequence[str]) -> List[str]:
    """Extension versions with a shared object, from a bare ``ls``.

    ``timescaledb.so`` (the loader) and ``timescaledb-tsl-<version>.so`` are
    deliberately not matched: neither is a version the extension can be pinned
    to on its own.
    """
    found = []
    for name in names:
        match = _SHARED_OBJECT.match(name.strip())
        if match:
            found.append(match.group(1))
    return sorted(set(found), key=_key)


def installable_from_listings(
    sharedir_names: Sequence[str], pkglibdir_names: Sequence[str]
) -> List[str]:
    """Versions this image can genuinely install: both artifacts present.

    An install script without a shared object is a version PostgreSQL will
    offer and then fail to load. Requiring both is what makes this the
    authority that the labels and the catalog view are not.
    """
    scripts = set(parse_install_scripts(sharedir_names))
    objects = set(parse_shared_objects(pkglibdir_names))
    return sorted(scripts & objects, key=_key)


# ---------------------------------------------------------------------------
# Image profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageProfile:
    """What one image tag offers, reduced to what a hop needs to know."""

    reference: str
    pg_majors: Tuple[int, ...]
    ts_floor: str
    ts_ceiling: str
    ts_default: Optional[str] = None
    # True when floor/ceiling came from a filesystem listing rather than labels.
    from_filesystem: bool = False
    notes: Tuple[str, ...] = field(default_factory=tuple)
    # The exact installable versions, when measured. Ranges have holes -- the
    # pg12 2.11 image ships 1.7.5 and then 2.1.0 onwards, nothing of 2.0 -- so
    # once this is known, membership replaces the floor/ceiling test.
    installable: Tuple[str, ...] = ()

    def serves(self, pg_major: int) -> bool:
        return pg_major in self.pg_majors

    def accepts(self, version: object) -> bool:
        """Whether a database at ``version`` can run on this image at all."""
        key = _safe_key(version)
        if key is None:
            return False
        if self.installable:
            return key in {_key(v) for v in self.installable}
        return _key(self.ts_floor) <= key <= _key(self.ts_ceiling)

    def landings(self, version: object) -> List[str]:
        """Versions an ``ALTER EXTENSION`` on this image can move ``version`` to.

        Every installable version above it when the set is measured; otherwise
        only the ceiling, the one version a label range vouches for.
        """
        key = _safe_key(version)
        if key is None:
            return []
        if self.installable:
            return [v for v in self.installable if _key(v) > key]
        ceiling = _safe_key(self.ts_ceiling)
        return [self.ts_ceiling] if ceiling is not None and ceiling > key else []


def published_floors(catalog: ImageCatalog) -> Dict[int, Tuple[int, int]]:
    """Lowest TimescaleDB series the registry actually publishes per PG major."""
    floors: Dict[int, Tuple[int, int]] = {}
    for pair in catalog.pairs:
        current = floors.get(pair.pg_major)
        if current is None or pair.ts_series < current:
            floors[pair.pg_major] = pair.ts_series
    return floors


def effective_floor(
    pg_major: int,
    label_floor: str,
    catalog: Optional[ImageCatalog] = None,
) -> Tuple[str, List[str]]:
    """Correct an image label's understated floor, returning ``(floor, notes)``.

    The labels are the union across the whole multi-PG build, not what was
    installed for one PostgreSQL major: a pg16 image advertises TimescaleDB
    2.12.2 although 2.12 never supported pg16 and the image ships neither its
    shared object nor its install script.

    The clamp uses the floor the **registry** publishes for that major, not the
    one the support matrix documents. Those disagree for pg15 -- the matrix's
    first pg15 row is 2.10, while ``pg15-ts2.9`` images exist and a 2.9 cluster
    is exactly the case bridging has to solve. An image that exists is a
    stronger fact than a table that omits it, so the registry wins and the
    disagreement is reported rather than quietly resolved.

    This is still a prediction. Where a filesystem listing is available it
    overrules this outright.
    """
    notes: List[str] = []
    floor_key = _safe_key(label_floor)
    if floor_key is None:
        return label_floor, ["cannot parse label floor {0!r}".format(label_floor)]

    published = published_floors(catalog).get(pg_major) if catalog else None
    documented = documented_ts_floor(pg_major)

    if published is not None and documented is not None and published < documented:
        notes.append(
            "pg{0}: registry publishes ts{1}.{2} but the support matrix starts "
            "at ts{3}.{4}; using the registry".format(
                pg_major, published[0], published[1], documented[0], documented[1]
            )
        )

    clamp = published if published is not None else documented
    if clamp is None or floor_key[:2] >= clamp:
        return label_floor, notes

    corrected = "{0}.{1}".format(clamp[0], clamp[1])
    notes.append(
        "label advertises ts{0} for pg{1}, but the oldest published pg{1} image "
        "is ts{2}; floor clamped".format(label_floor, pg_major, corrected)
    )
    return corrected, notes


def profile_from_labels(
    labels: ImageLabels,
    pg_major: Optional[int] = None,
    catalog: Optional[ImageCatalog] = None,
) -> ImageProfile:
    """Build a profile from registry labels, clamping the floor per PG major."""
    if not labels.ts_versions:
        raise RegistryError(
            "{0}: image declares no TimescaleDB versions".format(labels.reference)
        )
    majors = labels.pg_majors
    if not majors:
        raise RegistryError(
            "{0}: image declares no PostgreSQL versions".format(labels.reference)
        )

    target_major = pg_major if pg_major is not None else max(majors)
    floor, notes = effective_floor(target_major, labels.ts_floor or "", catalog)
    notes.append(
        "floor and ceiling come from image labels, which are a prediction; a "
        "filesystem listing overrules them"
    )
    return ImageProfile(
        reference=labels.reference,
        pg_majors=majors,
        ts_floor=floor,
        ts_ceiling=labels.ts_ceiling or floor,
        ts_default=labels.ts_default,
        from_filesystem=False,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftVerdict:
    state: str
    summary: str
    remedy: str = ""

    @property
    def is_outage(self) -> bool:
        return self.state in (STRANDED, AHEAD)


def classify(
    installed: Optional[str],
    installable: Sequence[str],
    image_default: Optional[str],
) -> DriftVerdict:
    """Place one database in one of the four states.

    ``installed`` is ``extversion`` from ``pg_extension`` in that database --
    per database, never per cluster, because they genuinely drift apart.
    ``installable`` is what the image can really install. ``image_default`` is
    ``default_version`` from ``pg_available_extensions``.

    An unreadable version returns :data:`INDETERMINATE`. A stranded database and
    an unreachable one look alike from the outside, and reporting the second as
    the first would be a false outage.
    """
    if not installed:
        return DriftVerdict(
            INDETERMINATE,
            "no installed extension version to compare",
            "install the extension, or confirm this database is not meant to have it",
        )

    installed_key = _safe_key(installed)
    if installed_key is None:
        return DriftVerdict(
            INDETERMINATE,
            "cannot parse installed version {0!r}".format(installed),
        )

    known = {v for v in installable if _safe_key(v) is not None}
    default_key = _safe_key(image_default) if image_default else None

    if default_key is None:
        # Without a default there is no aligned/lagging/ahead axis, but the
        # strand test stands on its own and is the one that matters.
        if known and installed not in known:
            return DriftVerdict(
                STRANDED,
                "{0} is not installable on this image".format(installed),
                "roll back to an image whose floor is at or below {0}, run "
                "ALTER EXTENSION, then roll forward".format(installed),
            )
        return DriftVerdict(INDETERMINATE, "image default version unknown")

    if installed_key > default_key:
        return DriftVerdict(
            AHEAD,
            "{0} is newer than the image default {1}".format(installed, image_default),
            "the image was rolled back underneath the catalog; downgrade "
            "scripts are single-step only, so roll the image forward to one "
            "shipping {0} or later".format(installed),
        )

    if known and installed not in known:
        return DriftVerdict(
            STRANDED,
            "{0} is not installable on this image".format(installed),
            "the server cannot load the extension it has recorded; roll back "
            "to an image whose floor is at or below {0}, run ALTER EXTENSION, "
            "then roll forward".format(installed),
        )

    if installed_key == default_key:
        return DriftVerdict(ALIGNED, "at the image default {0}".format(image_default))

    return DriftVerdict(
        LAGGING,
        "{0} is behind the image default {1}".format(installed, image_default),
        "ALTER EXTENSION timescaledb UPDATE, in a fresh psql -X session, "
        "subject to the update-script gates",
    )


def classify_offline(installed: Optional[str], profile: ImageProfile) -> DriftVerdict:
    """Classify against an image's declared range, before applying it.

    The point of the offline mode: a stranded database may not be queryable, so
    "would rolling out this image strand us?" has to be answerable from registry
    metadata alone, in advance. That is also the only honest time to ask it --
    afterwards the answer is an incident.

    For a label-only profile, range membership stands in for the exact
    installable set, so this is a prediction. A measured profile answers with
    the set itself; :func:`classify`, given a live filesystem listing, overrules
    both.
    """
    if not installed:
        return DriftVerdict(INDETERMINATE, "no installed version supplied")
    key = _safe_key(installed)
    if key is None:
        return DriftVerdict(
            INDETERMINATE, "cannot parse installed version {0!r}".format(installed)
        )

    floor_key, ceiling_key = _safe_key(profile.ts_floor), _safe_key(profile.ts_ceiling)
    if floor_key is None or ceiling_key is None:
        return DriftVerdict(
            INDETERMINATE, "image {0} declares no usable range".format(profile.reference)
        )

    if key > ceiling_key:
        return DriftVerdict(
            AHEAD,
            "{0} is above everything {1} ships (ceiling {2})".format(
                installed, profile.reference, profile.ts_ceiling
            ),
            "roll forward to an image shipping {0} or later".format(installed),
        )
    if key < floor_key:
        return DriftVerdict(
            STRANDED,
            "{0} is below the floor of {1} ({2})".format(
                installed, profile.reference, profile.ts_floor
            ),
            "do not roll this image out yet; bridge through an older image "
            "first, then roll forward",
        )
    if profile.installable and not profile.accepts(installed):
        if len(key) < 3:
            return DriftVerdict(
                INDETERMINATE,
                "{0} is not an exact release; {1}'s installable set is measured, "
                "so the patch release decides".format(installed, profile.reference),
            )
        return DriftVerdict(
            STRANDED,
            "{0} is inside {1}'s range but the image does not ship it (no "
            "install script and shared object for it)".format(
                installed, profile.reference
            ),
            "do not roll this image out yet; move to a version it ships first",
        )

    default_key = _safe_key(profile.ts_default) if profile.ts_default else None
    if default_key is not None and key == default_key:
        return DriftVerdict(
            ALIGNED, "already at {0}'s default".format(profile.reference)
        )
    return DriftVerdict(
        LAGGING,
        "{0} is within {1}'s range and can be updated there".format(
            installed, profile.reference
        ),
        "roll the image out, then ALTER EXTENSION timescaledb UPDATE",
    )


# ---------------------------------------------------------------------------
# Bridge search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeHop:
    image: str
    from_version: str
    to_version: str


def bridge(
    start: str,
    target_profile: ImageProfile,
    candidates: Sequence[ImageProfile],
    pg_major: Optional[int] = None,
) -> Tuple[List[BridgeHop], Optional[str]]:
    """Sequence of images carrying ``start`` up to ``target_profile``.

    A hop from version *V* using image *I* is legal when *I* accepts *V* and
    serves the PostgreSQL major. It lands on any version *I* can move *V* to
    (:meth:`ImageProfile.landings`). Breadth-first over versions, so the hop
    count is minimal; among the shortest chains, the one landing highest wins.

    Landing on the ceiling every time is not enough once installable sets have
    holes. A pg12 database at 1.7 on an image shipping 1.7.0 .. 2.0.1 has to
    stop at 1.7.5, because the next image ships 1.7.5 and 2.1.0 but no 2.0.

    Returns ``(hops, problem)``. An empty hop list with no problem means the
    target image already accepts ``start`` directly.
    """
    start_key = _safe_key(start)
    if start_key is None:
        return [], "cannot parse starting version {0!r}".format(start)

    major = pg_major
    if major is None:
        major = max(target_profile.pg_majors) if target_profile.pg_majors else None

    if target_profile.accepts(start) and (major is None or target_profile.serves(major)):
        return [], None

    usable = [
        c
        for c in candidates
        if (major is None or c.serves(major)) and c.reference != target_profile.reference
    ]
    if not usable:
        return [], "no candidate images to bridge with; supply more tags to consider"
    if not any(c.accepts(start) for c in usable):
        return [], (
            "no image accepts ts{0} for pg{1}; this version cannot be "
            "bridged from the candidates given".format(start, major)
        )

    # Every version is visited once and each hop strictly raises it, so this
    # terminates. Higher versions and higher landings are tried first, which
    # makes the first path found to a version also the preferred one.
    came_from: Dict[Tuple[int, ...], Optional[BridgeHop]] = {start_key: None}
    level = [start]
    highest = start
    while level:
        reached: List[str] = []
        following: List[str] = []
        for current in sorted(level, key=_key, reverse=True):
            for image in usable:
                if not image.accepts(current):
                    continue
                for landing in sorted(image.landings(current), key=_key, reverse=True):
                    if _key(landing) in came_from:
                        continue
                    came_from[_key(landing)] = BridgeHop(image.reference, current, landing)
                    following.append(landing)
                    if target_profile.accepts(landing):
                        reached.append(landing)
        if reached:
            hops: List[BridgeHop] = []
            hop = came_from[_key(max(reached, key=_key))]
            while hop is not None:
                hops.append(hop)
                hop = came_from[_key(hop.from_version)]
            return hops[::-1], None
        if following:
            highest = max(following + [highest], key=_key)
        level = following

    return [], (
        "bridging stalls at ts{0}: no candidate carries it to a version {1} "
        "accepts".format(highest, target_profile.reference)
    )


def plan_chain(
    start: str,
    target_profile: ImageProfile,
    candidates: Sequence[ImageProfile],
    pg_major: Optional[int] = None,
) -> Tuple[List[BridgeHop], Optional[str]]:
    """The whole rollout sequence, ending on the target image.

    :func:`bridge` answers "what has to happen before the target is reachable",
    which is the part with the search in it. This answers the question an
    operator actually asks -- *what do I roll out, in what order* -- by adding
    the final hop onto the target itself.

    Each hop is one image rollout followed by one ``ALTER EXTENSION``.
    """
    hops, problem = bridge(start, target_profile, candidates, pg_major)
    if problem is not None:
        return hops, problem

    current = hops[-1].to_version if hops else start
    ceiling = target_profile.ts_ceiling
    current_key, ceiling_key = _safe_key(current), _safe_key(ceiling)
    if current_key is not None and ceiling_key is not None and ceiling_key > current_key:
        hops.append(
            BridgeHop(
                image=target_profile.reference,
                from_version=current,
                to_version=ceiling,
            )
        )
    return hops, None
