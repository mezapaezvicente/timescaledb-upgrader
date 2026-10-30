"""The planner: turn a source version and a target image into a runbook.

``plan`` decides and ``apply`` acts, and the two are kept apart on purpose. A
plan writes nothing to any cluster. It is a document an operator can read,
diff and hand to ``apply``, which executes exactly what it says and nothing
else.

A plan is a sequence of *hops*. Each hop is one image the cluster has to be
running and one ``ALTER EXTENSION`` onto a version that image ships. A
single-image upgrade is one hop whose image the cluster may already run; a
stranded source needs bridging images first (see :func:`drift.plan_chain`).
The two shapes are the same data, so ``apply`` has one code path for both.

What the planner refuses
------------------------
Normal mode plans only what the matrix says is an established ``in_place``
transition. A row that exists but has no established verdict, or no row at
all, is refused. That refusal is the whole point of ``verdict: null``.

``verification_run=True`` lifts exactly that refusal and nothing else. It exists
for the matrix harness, which has to run the unknown rows to make them known,
and for anyone checking a TimescaleDB release newer than this repository's
tables before the repository catches up. It is loud by design: the mode is in
the plan document, in every rendering, and ``apply`` is expected to demand its
own acknowledgement before executing such a plan.

Some refusals hold in either mode, because no run could turn them into a
result: a PostgreSQL major downgrade, a source above the target (a
downgrade), a chain that cannot be bridged, and a PostgreSQL minor the target
TimescaleDB is known to refuse at load time.

Changing the PostgreSQL major
-----------------------------
A target on a newer major is planned as a route (:mod:`majors`): same-major
hops up to a release some image ships for both majors, a ``major_upgrade``
step that runs ``pg_upgrade`` in that image, and so on, ending with one
``analyze`` step, because ``pg_upgrade`` does not carry planner statistics
over (before PostgreSQL 18). It needs the measured contents of every image
involved, per major (``images``); labels describe one major only.
``pg_upgrade`` copies the data files by default, which leaves the old
cluster intact to go back to; ``link=True`` hard-links them instead, which
is fast and needs no space but makes the old cluster unusable once the new
one has started.

Known-bad updates
-----------------
A hop whose single ``ALTER EXTENSION`` would hit a known TimescaleDB bug
(:data:`support.UPDATE_HAZARDS`) is split into two hops on the same image,
stopping at the release measured to avoid it. The second hop's rollout is a
no-op for ``apply``: the cluster already runs that image. An image that does
not ship the stop blocks the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .drift import BridgeHop, ImageProfile, plan_chain
from .images import RegistryError, parse_image_reference, parse_release_tag
from .majors import MajorHop, Profiles, find_route, tag_major
from .matrix import Matrix, MatrixError
from .support import pg_minor_refusal, pg_minor_rules_for, update_hazard
from .versions import InvalidVersion, parse_version

__all__ = [
    "PLAN_FORMAT",
    "NORMAL",
    "VERIFICATION",
    "BLOCKED",
    "UNESTABLISHED",
    "Refusal",
    "PlanRefused",
    "build_plan",
    "render_runbook",
]

PLAN_FORMAT = 1

NORMAL = "normal"
VERIFICATION = "verification"

# Refusal kinds. UNESTABLISHED is the only one --verification-run lifts.
BLOCKED = "blocked"
UNESTABLISHED = "unestablished"

# Carried into every update step, because each has bitten someone. See
# UPGRADE-RESEARCH.md §10.6, traps 5 and 6.
_SESSION_RULES = (
    "run once in every database that has timescaledb installed",
    "use psql -X, so no .psqlrc loads the old library first",
    "ALTER EXTENSION must be the first statement of a fresh session",
)


@dataclass(frozen=True)
class Refusal:
    kind: str
    reason: str


class PlanRefused(Exception):
    """The planner will not produce a plan. Carries every reason, not the first."""

    def __init__(self, refusals: Sequence[Refusal]) -> None:
        self.refusals: List[Refusal] = list(refusals)
        super().__init__("; ".join(r.reason for r in self.refusals))

    @property
    def only_unestablished(self) -> bool:
        """True when --verification-run would have let this through."""
        return all(r.kind == UNESTABLISHED for r in self.refusals)


@dataclass
class _Collector:
    verification_run: bool
    refusals: List[Refusal] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def block(self, reason: str) -> None:
        self.refusals.append(Refusal(BLOCKED, reason))

    def unestablished(self, reason: str) -> None:
        """Refuse in normal mode; in a verification run, say so and carry on."""
        if self.verification_run:
            self.warnings.append("verification run: " + reason)
        else:
            self.refusals.append(Refusal(UNESTABLISHED, reason))


def _parts(version: object) -> Optional[tuple]:
    try:
        return parse_version(version)
    except InvalidVersion:
        return None


def _tag_versions(image: str) -> Dict[str, str]:
    try:
        _, _, tag = parse_image_reference(image)
    except RegistryError:
        return {}
    return parse_release_tag(tag) or {}


def _check_loads(out: _Collector, image: str, ts: str) -> None:
    """Will TimescaleDB ``ts`` load on the PostgreSQL minor ``image`` ships?"""
    pg = _tag_versions(image).get("pg")
    pg_parts = _parts(pg)
    if pg_parts is None or len(pg_parts) < 2:
        out.unestablished(
            "{0}: the tag does not pin a PostgreSQL minor, so whether "
            "TimescaleDB {1} loads on it cannot be checked".format(image, ts)
        )
        return
    if pg_minor_rules_for(ts) is None:
        out.unestablished(
            "no transcribed compat.h rules for TimescaleDB {0}; whether it loads "
            "on PostgreSQL {1} is not established".format(ts, pg)
        )
        return
    refusal = pg_minor_refusal(ts, pg)
    if refusal is not None:
        out.block("{0}: {1}".format(image, refusal))


def _avoid_hazards(
    out: _Collector,
    hops: Sequence[Any],
    profiles: Dict[str, ImageProfile],
) -> Tuple[List[Any], Dict[BridgeHop, str]]:
    """Split every hop that hits a known update bug at its measured stop.

    Returns the hops and, for each hop that stops short, why. Both halves of
    a split are checked again, so stops compose. Major hops pass through.
    """
    done: List[Any] = []
    why: Dict[BridgeHop, str] = {}
    pending = list(hops)
    while pending:
        hop = pending.pop(0)
        if isinstance(hop, MajorHop):
            done.append(hop)
            continue
        hazard = update_hazard(hop.from_version, hop.to_version)
        if hazard is None:
            done.append(hop)
            continue
        direct = "{0} -> {1} directly {2} ({3})".format(
            hop.from_version, hop.to_version, hazard.reason, hazard.url)
        profile = profiles.get(hop.image)
        between = _parts(hop.from_version) < _parts(hazard.via) < _parts(hop.to_version)
        if not between or profile is None or not profile.accepts(hazard.via):
            out.block("{0}: {1}, and the image does not ship the stop {2}".format(
                hop.image, direct, hazard.via))
            done.append(hop)
            continue
        out.warnings.append("stopping at {0} on {1}: {2}".format(hazard.via, hop.image, direct))
        first = BridgeHop(hop.image, hop.from_version, hazard.via)
        why[first] = direct
        pending[:0] = [first, BridgeHop(hop.image, hazard.via, hop.to_version)]
    return done, why


def _check_matrix(
    out: _Collector,
    matrix: Optional[Matrix],
    source_pg: str,
    source_ts: str,
    target_pg: str,
    target_ts: str,
) -> Optional[Dict[str, Any]]:
    label = "pg{0}/ts{1} -> pg{2}/ts{3}".format(source_pg, source_ts, target_pg, target_ts)
    if matrix is None:
        out.unestablished("no matrix loaded; {0} has no established verdict".format(label))
        return None
    try:
        entry = matrix.lookup(source_pg, source_ts, target_pg, target_ts)
    except MatrixError as exc:
        out.block("matrix lookup failed for {0}: {1}".format(label, exc))
        return None

    if entry is None:
        out.unestablished(
            "the matrix has no entry for {0}; that is not the same as "
            "unsupported, but it is not permission either".format(label)
        )
        return None

    doc = {
        "transition": entry.transition,
        "verdict": entry.verdict,
        "verified": entry.verified,
        "reason": entry.reason,
        "evidence": dict(entry.evidence) if entry.evidence else None,
        "references": list(entry.references),
    }
    if not entry.is_known:
        out.unestablished(
            "{0} is in the matrix but its verdict is not established "
            "(verified: false)".format(entry.transition)
        )
    elif entry.verdict != "in_place":
        message = "{0} is recorded as {1}{2}".format(
            entry.transition,
            entry.verdict,
            ": " + entry.reason if entry.reason else "",
        )
        if out.verification_run:
            # Re-running a settled row is legitimate -- a newer release may
            # have changed it -- but the recorded verdict must stay visible.
            out.warnings.append("verification run: " + message)
        else:
            out.block(message + "; plan only produces in-place upgrades")
    return doc


def build_plan(
    source_pg: str,
    source_ts: str,
    target_profile: ImageProfile,
    candidates: Sequence[ImageProfile] = (),
    matrix: Optional[Matrix] = None,
    target_pg: Optional[str] = None,
    target_ts: Optional[str] = None,
    verification_run: bool = False,
    images: Optional[Profiles] = None,
    link: bool = False,
) -> Dict[str, Any]:
    """Build the plan document, or raise :class:`PlanRefused` with every reason.

    ``source_ts`` must be the exact installed version (``2.13.0``, not
    ``2.13``): whether an image can load it, and so whether bridging is
    needed, depends on the patch release. ``target_pg`` and ``target_ts``
    default to what the target image's tag pins. ``images`` (measured
    contents per image and major) and ``link`` matter only when the target
    is on a newer PostgreSQL major.
    """
    out = _Collector(verification_run)
    target_image = target_profile.reference
    tag = _tag_versions(target_image)
    target_pg = target_pg or tag.get("pg")
    target_ts = target_ts or tag.get("ts")

    src_pg, src_ts = _parts(source_pg), _parts(source_ts)
    tgt_pg, tgt_ts = _parts(target_pg), _parts(target_ts)

    if src_pg is None:
        out.block("cannot parse source PostgreSQL version {0!r}".format(source_pg))
    if src_ts is None or len(src_ts) < 3:
        out.block(
            "source TimescaleDB version must be the exact installed release "
            "(e.g. 2.13.0), got {0!r}".format(source_ts)
        )
    if tgt_pg is None or tgt_ts is None:
        out.block(
            "{0}: the tag does not pin PostgreSQL and TimescaleDB versions; "
            "pass --target-pg and --target-ts".format(target_image)
        )
    if out.refusals:
        raise PlanRefused(out.refusals)

    major = src_pg[0]
    cross = tgt_pg[0] != major
    if tgt_pg[0] < major:
        out.block("pg{0} -> pg{1} is a PostgreSQL downgrade; not planned".format(major, tgt_pg[0]))
    elif cross and images is None:
        out.block(
            "pg{0} -> pg{1} changes the PostgreSQL major, which needs the measured "
            "contents of every image (`make image-contents`)".format(major, tgt_pg[0])
        )
    elif cross and len(tgt_ts) < 3:
        out.block("a PostgreSQL major change needs an exact target release, got {0!r}".format(
            target_ts))
    if link and not cross:
        out.block("--link applies to pg_upgrade, and this plan stays on pg{0}".format(major))
    if tgt_ts[: len(src_ts)] < src_ts[: len(tgt_ts)]:
        out.block(
            "source ts{0} is above target ts{1}; downgrades are not "
            "planned".format(source_ts, target_ts)
        )
    if out.refusals:
        raise PlanRefused(out.refusals)

    matrix_doc = _check_matrix(out, matrix, source_pg, source_ts, target_pg, target_ts)

    if cross:
        route, problem = find_route(major, source_ts, tgt_pg[0], target_ts, target_image, images)
        hops = list(route.steps) if route is not None else []
    else:
        hops, problem = plan_chain(source_ts, target_profile, candidates, major)
    if problem is not None:
        out.block(problem)
    elif not cross and hops and len(tgt_ts) >= 3 and hops[-1].image == target_image:
        # plan_chain lands the last hop on the image's ceiling; a tag that pins
        # an exact release is the version actually asked for.
        last = hops[-1]
        if _parts(last.to_version) != tgt_ts:
            if not target_profile.accepts(target_ts):
                out.block(
                    "{0} does not ship ts{1} (it ships {2} .. {3})".format(
                        target_image, target_ts, target_profile.ts_floor,
                        target_profile.ts_ceiling,
                    )
                )
            hops[-1] = type(last)(last.image, last.from_version, target_ts)

    profiles = {c.reference: c for c in candidates}
    profiles[target_image] = target_profile
    if cross:
        # An image runs only the major its tag names; that is its profile here.
        profiles = {ref: per[tag_major(ref)] for ref, per in images.items()
                    if tag_major(ref) in per}
    hops, stops = _avoid_hazards(out, hops, profiles)

    for hop in hops:
        if isinstance(hop, MajorHop):
            _check_loads(out, hop.image, hop.ts)
            _check_loads(out, hop.run_image, hop.ts)
        else:
            _check_loads(out, hop.image, hop.to_version)

    for note in target_profile.notes:
        out.warnings.append("{0}: {1}".format(target_image, note))
    for candidate in candidates:
        if any(getattr(h, "image", None) == candidate.reference for h in hops):
            for note in candidate.notes:
                out.warnings.append("{0}: {1}".format(candidate.reference, note))

    if out.refusals:
        raise PlanRefused(out.refusals)

    steps: List[Dict[str, Any]] = []
    for i, hop in enumerate(hops, 1):
        if isinstance(hop, MajorHop):
            steps.extend(
                [
                    {
                        "hop": i,
                        "action": "major_upgrade",
                        "image": hop.image,
                        "from_pg": hop.from_pg,
                        "to_pg": hop.to_pg,
                        "ts": hop.ts,
                        "run_image": hop.run_image,
                        "link": link,
                    },
                    {"hop": i, "action": "verify_version", "expect": hop.ts},
                ]
            )
            continue
        hop_pg = _tag_versions(hop.image).get("pg") or target_pg
        update: Dict[str, Any] = {
            "hop": i,
            "action": "update_extension",
            "from": hop.from_version,
            "to": hop.to_version,
            "sql": "ALTER EXTENSION timescaledb UPDATE TO '{0}';".format(hop.to_version),
            "rules": list(_SESSION_RULES),
        }
        if hop in stops:
            update["why"] = stops[hop]
        steps.extend(
            [
                {
                    "hop": i,
                    "action": "preflight",
                    "image": hop.image,
                    "target_pg": hop_pg,
                    "target_ts": hop.to_version,
                },
                {"hop": i, "action": "ensure_image", "image": hop.image},
                update,
                {
                    "hop": i,
                    "action": "cycle_sessions",
                    "why": "existing sessions, replicas included, keep the old "
                    "shared object loaded until they reconnect",
                },
                {"hop": i, "action": "verify_version", "expect": hop.to_version},
            ]
        )

    if any(isinstance(h, MajorHop) for h in hops):
        steps.append({"hop": len(hops), "action": "analyze",
                      "why": "pg_upgrade carries no planner statistics over before PostgreSQL 18"})

    return {
        "plan_format": PLAN_FORMAT,
        "mode": VERIFICATION if verification_run else NORMAL,
        "source": {"pg": source_pg, "ts": source_ts},
        "target": {"image": target_image, "pg": target_pg, "ts": target_ts},
        "matrix": matrix_doc,
        "hops": len(hops),
        "warnings": out.warnings,
        "steps": steps,
    }


def render_runbook(plan: Dict[str, Any]) -> str:
    """The plan as an operator reads it. Same content as the JSON, nothing more."""
    lines: List[str] = []
    verification = plan["mode"] == VERIFICATION
    if verification:
        lines += [
            "!" * 72,
            "!! VERIFICATION RUN -- this plan was built WITHOUT an established",
            "!! verdict. Run it only against a disposable database whose result",
            "!! you intend to record. It is not advice for a production cluster.",
            "!" * 72,
            "",
        ]

    src, tgt = plan["source"], plan["target"]
    lines.append("source  : pg{0} / ts{1}".format(src["pg"], src["ts"]))
    lines.append("target  : pg{0} / ts{1}  ({2})".format(tgt["pg"], tgt["ts"], tgt["image"]))
    m = plan["matrix"]
    if m is None:
        lines.append("matrix  : no entry")
    else:
        lines.append(
            "matrix  : {0} ({1})".format(
                m["verdict"] or "unknown", "verified" if m["verified"] else "not verified"
            )
        )
        for ref in m.get("references") or []:
            lines.append("          {0}".format(ref))
    lines.append("writes  : nothing -- this is a plan; `tsupgrade apply` executes it")

    for warning in plan["warnings"]:
        lines.append("warning : {0}".format(warning))

    lines.append("")
    if not plan["steps"]:
        lines.append("nothing to do: the source is already at the target version")
        return "\n".join(lines)

    for step in plan["steps"]:
        hop, action = step["hop"], step["action"]
        prefix = "hop {0}".format(hop)
        if action == "preflight":
            lines.append("{0}. preflight against {1} (target ts{2}); stop on any FAIL".format(
                prefix, step["image"], step["target_ts"]))
        elif action == "ensure_image":
            lines.append("{0}. make sure every instance runs {1} (roll it out if not)".format(
                prefix, step["image"]))
        elif action == "update_extension":
            lines.append("{0}. update the extension {1} -> {2}:".format(
                prefix, step["from"], step["to"]))
            lines.append("         {0}".format(step["sql"]))
            if step.get("why"):
                lines.append("         - stops here because {0}".format(step["why"]))
            for rule in step["rules"]:
                lines.append("         - {0}".format(rule))
        elif action == "cycle_sessions":
            lines.append("{0}. cycle client sessions: {1}".format(prefix, step["why"]))
        elif action == "verify_version":
            lines.append("{0}. confirm pg_extension reports {1} in every database".format(
                prefix, step["expect"]))
        elif action == "major_upgrade":
            lines.append("{0}. upgrade PostgreSQL {1} -> {2} with pg_upgrade ({3}) in {4}, "
                         "at TimescaleDB {5}:".format(
                             prefix, step["from_pg"], step["to_pg"],
                             "--link" if step["link"] else "copy", step["image"], step["ts"]))
            lines.append("         - stop the cluster; the leader's data directory is upgraded")
            lines.append("         - {0}".format(
                "the old data directory is unusable afterwards (hard links)" if step["link"]
                else "the old data directory is kept, to go back to"))
            lines.append("         - replicas are re-created, and Patroni's stored state reset "
                         "(pg_upgrade gives the cluster a new system identifier)")
            lines.append("         - restart every instance on {0}".format(step["run_image"]))
        elif action == "analyze":
            lines.append("{0}. vacuumdb --all --analyze-in-stages: {1}".format(prefix, step["why"]))
    return "\n".join(lines)
