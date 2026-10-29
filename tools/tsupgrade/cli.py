"""Command line entry point: ``python -m tsupgrade``.

Subcommands
-----------
``images refresh``    query the registry and regenerate ``data/images.json``
``images show``       summarise the catalog already on disk
``contents measure``  measure what local images really install
``contents show``     summarise the measured image contents
``matrix scaffold``   add missing transitions to ``data/upgrade-matrix.json``
``matrix render``     render the matrix to Markdown
``matrix lookup``     look up one transition
``drift``             classify databases against what their image can install
``plan``              emit the runbook for an upgrade; writes nothing to a cluster

Exit codes for ``matrix lookup`` are meant to be scripted against:
0 a verified verdict, 3 an entry with no established verdict, 4 no entry at all,
1 an error.

``plan`` exits 0 with a plan, 2 when refused outright, and 3 when refused only
because the verdict is not established -- the refusal ``--verification-run``
lifts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .contents import (
    ContentsError,
    ImageContents,
    default_contents_path,
    load_contents,
    measure_image,
    merge_measurements,
)
from .drift import (
    AHEAD,
    STRANDED,
    bridge,
    classify_offline,
    plan_chain,
    profile_from_labels,
)
from .images import (
    DEFAULT_REPOSITORY,
    RegistryError,
    build_catalog,
    default_catalog_path,
    fetch_image_labels,
    fetch_tags,
    load_catalog,
    parse_image_reference,
)
from .matrix import MatrixError, default_matrix_path, load_matrix
from .plan import PlanRefused, build_plan, render_runbook
from .preflight import (
    FAIL,
    UNKNOWN,
    PreflightError,
    PreflightOptions,
    render_report,
    run_preflight,
)
from .render import render_markdown
from .versions import InvalidVersion, parse_version
from .scaffold import scaffold_matrix

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNKNOWN = 3
EXIT_NOT_FOUND = 4
# preflight: 2 means at least one check failed, 3 at least one is unknown.
EXIT_CHECK_FAILED = 2


def _write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" keeps generated files LF on Windows, matching .gitattributes.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _default_docs_path() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "matrix.md"


def cmd_images_refresh(args: argparse.Namespace) -> int:
    output = Path(args.output) if args.output else default_catalog_path()
    print("querying {0} ...".format(args.repository), file=sys.stderr)
    tags = fetch_tags(args.repository, timeout=args.timeout)
    catalog = build_catalog(tags, args.repository)
    _write_json(output, catalog)
    counts = catalog["counts"]
    print(
        "wrote {0}\n  {1} tags total, {2} release tags, {3} distinct (pg, ts) pairs".format(
            output, counts["tags_total"], counts["release_tags"], counts["pairs"]
        )
    )
    return EXIT_OK


def cmd_images_show(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    print("repository : {0}".format(catalog.repository))
    print("queried at : {0}".format(catalog.queried_at))
    print("source     : {0}".format(catalog.source_url))
    print("pairs      : {0}".format(len(catalog)))
    print()
    print("PostgreSQL major   TimescaleDB series published")
    print("----------------   -----------------------------")
    for major in catalog.pg_majors():
        series = catalog.ts_series_for(major)
        text = ", ".join("{0}.{1}".format(a, b) for a, b in series)
        print("pg{0:<16} {1}".format(major, text))
    return EXIT_OK


def cmd_matrix_scaffold(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    output = Path(args.output) if args.output else default_matrix_path()

    existing = None
    if output.exists():
        existing = load_matrix(output)

    doc = scaffold_matrix(catalog, existing)
    added = doc.pop("_added_this_run", 0)
    _write_json(output, doc)

    total = len(doc["entries"])
    verified = sum(1 for e in doc["entries"] if e.get("verified"))
    print(
        "wrote {0}\n  {1} entries ({2} added this run), {3} verified, {4} unverified".format(
            output, total, added, verified, total - verified
        )
    )
    return EXIT_OK


def cmd_matrix_render(args: argparse.Namespace) -> int:
    matrix = load_matrix(args.matrix)
    text = render_markdown(matrix)

    if args.output == "-":
        sys.stdout.write(text)
        return EXIT_OK

    output = Path(args.output) if args.output else _default_docs_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("wrote {0} ({1} rows)".format(output, len(matrix)))
    return EXIT_OK


def cmd_matrix_lookup(args: argparse.Namespace) -> int:
    matrix = load_matrix(args.matrix)
    entry = matrix.lookup(args.from_pg, args.from_ts, args.to_pg, args.to_ts)

    if entry is None:
        print(
            "no matrix entry for pg{0}/ts{1} -> pg{2}/ts{3}".format(
                args.from_pg, args.from_ts, args.to_pg, args.to_ts
            ),
            file=sys.stderr,
        )
        print(
            "the matrix says nothing about this transition; that is not the "
            "same as saying it is unsupported",
            file=sys.stderr,
        )

        # A miss is often just a multi-hop path: the matrix records upgrades
        # via the newest TimescaleDB series each PostgreSQL major published,
        # so moving both at once may not be listed while each leg is.
        nearby = matrix.from_source(args.from_pg, args.from_ts)
        if nearby:
            print(file=sys.stderr)
            print(
                "from pg{0}/ts{1} the matrix does list:".format(
                    args.from_pg, args.from_ts
                ),
                file=sys.stderr,
            )
            for entry in nearby[:8]:
                print(
                    "  -> pg{0}/ts{1}  ({2})".format(
                        entry.target_pg, entry.target_ts, entry.verdict or "unknown"
                    ),
                    file=sys.stderr,
                )
            if len(nearby) > 8:
                print("  ... and {0} more".format(len(nearby) - 8), file=sys.stderr)

        return EXIT_NOT_FOUND

    if args.json:
        print(json.dumps(entry.to_dict(), indent=2))
    else:
        print("transition : {0}".format(entry.transition))
        print("verdict    : {0}".format(entry.verdict or "unknown"))
        print("verified   : {0}".format("yes" if entry.verified else "no"))
        print("reason     : {0}".format(entry.reason or "-"))
        print("source     : {0}".format(entry.source_url or "-"))
        if entry.steps:
            print("steps      :")
            for i, step in enumerate(entry.steps, 1):
                print("  {0}. {1}".format(i, step))

    return EXIT_OK if entry.is_known else EXIT_UNKNOWN


def cmd_preflight(args: argparse.Namespace) -> int:
    opts = PreflightOptions(
        target_image=args.target_image,
        context=args.context,
        namespace=args.namespace,
        selector=args.selector,
        container=args.container,
        psql_user=args.psql_user,
        admin_db=args.admin_db,
        dump_path=args.dump_path,
        headroom_factor=args.headroom_factor,
        target_pg=args.target_pg,
        target_ts=args.target_ts,
        matrix_path=args.matrix,
        timeout=args.timeout,
        registry_timeout=args.registry_timeout,
    )
    results = run_preflight(opts)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": r.name,
                        "title": r.title,
                        "status": r.status,
                        "summary": r.summary,
                        "details": r.details,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        print(render_report(results, color=sys.stdout.isatty()))

    statuses = [r.status for r in results]
    if FAIL in statuses:
        return EXIT_CHECK_FAILED
    if UNKNOWN in statuses:
        return EXIT_UNKNOWN
    return EXIT_OK


def _load_contents(path: Optional[str]) -> Optional[ImageContents]:
    """The measured contents; absent is fine unless a path was asked for."""
    try:
        return load_contents(path)
    except ContentsError:
        if path is not None:
            raise
        return None


def _profile(reference: str, pg_major, catalog, timeout: float, contents=None):
    """A measured profile when there is one; the labels only otherwise."""
    if contents is not None:
        measured = contents.profile(reference, pg_major)
        if measured is not None:
            return measured
    return profile_from_labels(
        fetch_image_labels(reference, timeout=timeout), pg_major, catalog
    )


def cmd_contents_measure(args: argparse.Namespace) -> int:
    """Measure local images and fold the results into the contents file."""
    output = Path(args.output) if args.output else default_contents_path()
    existing = load_contents(output).doc if output.exists() else None

    measured = {}
    failed = 0
    for tag in args.tags:
        reference = tag if ":" in tag else "{0}:{1}".format(DEFAULT_REPOSITORY, tag)
        try:
            entry = measure_image(reference)
        except ContentsError as exc:
            print("error: {0}".format(exc), file=sys.stderr)
            failed += 1
            continue
        _, _, short = parse_image_reference(reference)
        measured[short] = entry
        for major, found in sorted(entry["pg"].items(), key=lambda kv: int(kv[0])):
            versions = found["installable"]
            print("{0:24} pg{1:<3} {2:>3} versions  {3} .. {4}".format(
                short, major, len(versions), versions[0], versions[-1]))

    if measured:
        _write_json(output, merge_measurements(existing, measured))
        print("wrote {0}".format(output), file=sys.stderr)
    return EXIT_ERROR if failed else EXIT_OK


def cmd_contents_show(args: argparse.Namespace) -> int:
    contents = load_contents(args.contents)
    print("image                    pg   installable")
    print("-----------------------  ---  -----------")
    for tag, entry in contents.images.items():
        for major, found in sorted(entry.get("pg", {}).items(), key=lambda kv: int(kv[0])):
            print("{0:24} {1:<4} {2}".format(tag, major, _ranges(found["installable"])))
    return EXIT_OK


def _ranges(versions: List[str]) -> str:
    """Collapse a version list into runs, so a hole in it is visible.

    A run breaks where a minor series is skipped: 1.7.5, 2.1.0 .. 2.11.2 reads
    as two runs because 2.0 is missing.
    """
    runs: List[List[str]] = []
    for version in versions:
        parts = parse_version(version)
        if runs:
            prev = parse_version(runs[-1][-1])
            same_major_next_minor = parts[0] == prev[0] and parts[1] - prev[1] <= 1
            next_major_first = parts[0] == prev[0] + 1 and parts[1] == 0
            if same_major_next_minor or next_major_first:
                runs[-1].append(version)
                continue
        runs.append([version])
    return ", ".join(r[0] if len(r) == 1 else "{0} .. {1}".format(r[0], r[-1]) for r in runs)


def cmd_drift(args: argparse.Namespace) -> int:
    """Offline drift: would this image strand a database at this version?

    The online counterpart lives in ``preflight``, which has the cluster in
    hand. This mode exists because the question is only useful *before* the
    rollout -- afterwards the answer is an incident -- and because a stranded
    database may be exactly the one that cannot be queried.
    """
    try:
        catalog = load_catalog(args.catalog)
    except RegistryError:
        catalog = None

    pg_major = int(args.pg_major) if args.pg_major else None
    contents = _load_contents(args.contents)

    try:
        target = _profile(args.image, pg_major, catalog, args.registry_timeout, contents)
    except RegistryError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return EXIT_ERROR

    verdict = classify_offline(args.installed, target)

    if args.json:
        doc = {
            "image": target.reference,
            "pg_majors": list(target.pg_majors),
            "ts_floor": target.ts_floor,
            "ts_ceiling": target.ts_ceiling,
            "ts_default": target.ts_default,
            "installed": args.installed,
            "state": verdict.state,
            "summary": verdict.summary,
            "remedy": verdict.remedy,
            "notes": list(target.notes),
        }
    else:
        print("image      : {0}".format(target.reference))
        print("serves pg  : {0}".format(", ".join(str(m) for m in target.pg_majors)))
        print("ships ts   : {0} .. {1} (default {2})".format(
            target.ts_floor, target.ts_ceiling, target.ts_default or "?"))
        print("installed  : {0}".format(args.installed))
        print("state      : {0}".format(verdict.state.upper()))
        print("summary    : {0}".format(verdict.summary))
        if verdict.remedy:
            print("remedy     : {0}".format(verdict.remedy))
        for note in target.notes:
            print("note       : {0}".format(note))

    hops = []
    problem = None
    if verdict.state == STRANDED and args.via:
        major = pg_major or (max(target.pg_majors) if target.pg_majors else None)
        candidates = []
        for ref in args.via:
            try:
                candidates.append(
                    _profile(ref, major, catalog, args.registry_timeout, contents)
                )
            except RegistryError as exc:
                print("warning: {0}".format(exc), file=sys.stderr)
        hops, problem = plan_chain(args.installed, target, candidates, major)

        if args.json:
            doc["bridge"] = {
                "problem": problem,
                "hops": [
                    {"image": h.image, "from": h.from_version, "to": h.to_version}
                    for h in hops
                ],
            }
        else:
            print()
            if problem:
                print("bridge     : {0}".format(problem))
            else:
                print("bridge     : {0} image rollout(s)".format(len(hops)))
                for i, hop in enumerate(hops, 1):
                    print("  {0}. roll to {1}".format(i, hop.image))
                    print("     then ALTER EXTENSION timescaledb UPDATE: {0} -> {1}".format(
                        hop.from_version, hop.to_version))

    if args.json:
        print(json.dumps(doc, indent=2))

    if verdict.state in (STRANDED, AHEAD):
        return EXIT_CHECK_FAILED
    return EXIT_OK


def cmd_plan(args: argparse.Namespace) -> int:
    """Build a plan from a stated source and a target image. Offline apart
    from the registry label lookups; it never touches a cluster.
    """
    try:
        catalog = load_catalog(args.catalog)
    except RegistryError:
        catalog = None

    try:
        major = parse_version(args.from_pg)[0]
    except InvalidVersion:
        print("error: cannot parse --from-pg {0!r}".format(args.from_pg), file=sys.stderr)
        return EXIT_ERROR

    contents = _load_contents(args.contents)
    try:
        target = _profile(
            args.target_image, major, catalog, args.registry_timeout, contents
        )
    except RegistryError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    candidates = []
    for ref in args.via:
        try:
            candidates.append(
                _profile(ref, major, catalog, args.registry_timeout, contents)
            )
        except RegistryError as exc:
            print("warning: {0}".format(exc), file=sys.stderr)

    try:
        matrix = load_matrix(args.matrix)
    except MatrixError as exc:
        if not args.verification_run:
            raise
        print("warning: no matrix: {0}".format(exc), file=sys.stderr)
        matrix = None

    if args.verification_run:
        print(
            "VERIFICATION RUN: planning without an established verdict. The "
            "plan is for recording a result, not for production.",
            file=sys.stderr,
        )

    try:
        plan = build_plan(
            args.from_pg,
            args.from_ts,
            target,
            candidates,
            matrix,
            target_pg=args.target_pg,
            target_ts=args.target_ts,
            verification_run=args.verification_run,
        )
    except PlanRefused as exc:
        print("refused: no plan produced", file=sys.stderr)
        for refusal in exc.refusals:
            print("  - [{0}] {1}".format(refusal.kind, refusal.reason), file=sys.stderr)
        if exc.only_unestablished:
            print(
                "only an unestablished verdict stands in the way; "
                "--verification-run plans it anyway, for recording a result",
                file=sys.stderr,
            )
            return EXIT_UNKNOWN
        return EXIT_CHECK_FAILED

    if args.output:
        _write_json(Path(args.output), plan)
        print("wrote {0}".format(args.output), file=sys.stderr)
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(render_runbook(plan))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsupgrade",
        description="Tooling for upgrading self-hosted TimescaleDB on Kubernetes.",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # --- images ---
    images = sub.add_parser("images", help="the catalog of published images")
    images_sub = images.add_subparsers(dest="command", required=True)

    refresh = images_sub.add_parser(
        "refresh", help="query the registry and regenerate the catalog"
    )
    refresh.add_argument("--repository", default="timescale/timescaledb-ha")
    refresh.add_argument("--output", default=None)
    refresh.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="per-request timeout in seconds (default: 30)",
    )
    refresh.set_defaults(func=cmd_images_refresh)

    show = images_sub.add_parser("show", help="summarise the catalog on disk")
    show.add_argument("--catalog", default=None)
    show.set_defaults(func=cmd_images_show)

    # --- contents ---
    contents = sub.add_parser(
        "contents", help="what specific images really install, measured"
    )
    contents_sub = contents.add_subparsers(dest="command", required=True)

    measure = contents_sub.add_parser(
        "measure",
        help="measure local images (docker run, ls only) into the contents file",
    )
    measure.add_argument(
        "tags", nargs="+", metavar="TAG", help="image tag or full reference; must be pulled"
    )
    measure.add_argument("--output", default=None)
    measure.set_defaults(func=cmd_contents_measure)

    contents_show = contents_sub.add_parser(
        "show", help="summarise the measured contents on disk"
    )
    contents_show.add_argument("--contents", default=None)
    contents_show.set_defaults(func=cmd_contents_show)

    # --- matrix ---
    matrix = sub.add_parser("matrix", help="the upgrade compatibility matrix")
    matrix_sub = matrix.add_subparsers(dest="command", required=True)

    scaffold = matrix_sub.add_parser(
        "scaffold", help="add missing transitions, preserving existing rows"
    )
    scaffold.add_argument("--catalog", default=None)
    scaffold.add_argument("--output", default=None)
    scaffold.set_defaults(func=cmd_matrix_scaffold)

    render = matrix_sub.add_parser("render", help="render the matrix as Markdown")
    render.add_argument("--matrix", default=None)
    render.add_argument(
        "--output", default=None, help="output path, or - for stdout"
    )
    render.set_defaults(func=cmd_matrix_render)

    lookup = matrix_sub.add_parser("lookup", help="look up a single transition")
    lookup.add_argument("--matrix", default=None)
    lookup.add_argument("--from-pg", required=True, dest="from_pg")
    lookup.add_argument("--from-ts", required=True, dest="from_ts")
    lookup.add_argument("--to-pg", required=True, dest="to_pg")
    lookup.add_argument("--to-ts", required=True, dest="to_ts")
    lookup.add_argument("--json", action="store_true")
    lookup.set_defaults(func=cmd_matrix_lookup)

    # --- preflight ---
    pre = sub.add_parser(
        "preflight",
        help="read-only checks against a live instance before an upgrade",
    )
    pre.add_argument(
        "--target-image",
        required=True,
        help="the image you intend to upgrade to",
    )
    pre.add_argument("--context", default=None, help="kubectl context")
    pre.add_argument("--namespace", "-n", default="timescale")
    pre.add_argument("--selector", "-l", default="application=patroni")
    pre.add_argument("--container", default="timescaledb")
    pre.add_argument("--psql-user", default="postgres", dest="psql_user")
    pre.add_argument("--admin-db", default="postgres", dest="admin_db")
    pre.add_argument(
        "--dump-path",
        default=None,
        dest="dump_path",
        help="where a dump would be written (default: the data directory volume)",
    )
    pre.add_argument(
        "--headroom-factor",
        type=float,
        default=1.2,
        dest="headroom_factor",
        help="required free space as a multiple of database size (default: 1.2)",
    )
    pre.add_argument("--target-pg", default=None, dest="target_pg")
    pre.add_argument("--target-ts", default=None, dest="target_ts")
    pre.add_argument("--matrix", default=None)
    pre.add_argument("--timeout", type=float, default=30.0)
    pre.add_argument(
        "--registry-timeout", type=float, default=15.0, dest="registry_timeout"
    )
    pre.add_argument("--json", action="store_true")
    pre.set_defaults(func=cmd_preflight)

    # --- drift ---
    drift = sub.add_parser(
        "drift",
        help="would this image strand a database at this extension version?",
    )
    drift.add_argument(
        "--image", required=True, help="the image reference you intend to roll out"
    )
    drift.add_argument(
        "--installed",
        required=True,
        help="the extension version the database currently has",
    )
    drift.add_argument(
        "--pg-major",
        default=None,
        dest="pg_major",
        help="which PostgreSQL major to evaluate for multi-PG (-all) images",
    )
    drift.add_argument(
        "--via",
        action="append",
        default=[],
        metavar="IMAGE",
        help="a candidate bridging image; repeat to offer several",
    )
    drift.add_argument("--catalog", default=None)
    drift.add_argument(
        "--contents",
        default=None,
        help="measured image contents (default: data/image-contents.json)",
    )
    drift.add_argument(
        "--registry-timeout", type=float, default=15.0, dest="registry_timeout"
    )
    drift.add_argument("--json", action="store_true")
    drift.set_defaults(func=cmd_drift)

    # --- plan ---
    plan = sub.add_parser(
        "plan",
        help="emit the runbook for an upgrade; writes nothing to any cluster",
    )
    plan.add_argument("--from-pg", required=True, dest="from_pg")
    plan.add_argument(
        "--from-ts",
        required=True,
        dest="from_ts",
        help="the exact installed extension version, e.g. 2.13.0",
    )
    plan.add_argument("--target-image", required=True, dest="target_image")
    plan.add_argument("--target-pg", default=None, dest="target_pg")
    plan.add_argument("--target-ts", default=None, dest="target_ts")
    plan.add_argument(
        "--via",
        action="append",
        default=[],
        metavar="IMAGE",
        help="a candidate bridging image; repeat to offer several",
    )
    plan.add_argument(
        "--verification-run",
        action="store_true",
        dest="verification_run",
        help="plan a transition whose verdict is not established, to record "
        "its result (matrix verification, or a TimescaleDB release newer than "
        "this repository). Never for production.",
    )
    plan.add_argument("--matrix", default=None)
    plan.add_argument("--catalog", default=None)
    plan.add_argument(
        "--contents",
        default=None,
        help="measured image contents (default: data/image-contents.json)",
    )
    plan.add_argument("--output", default=None, help="also write the plan as JSON here")
    plan.add_argument(
        "--registry-timeout", type=float, default=15.0, dest="registry_timeout"
    )
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=cmd_plan)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (MatrixError, RegistryError, PreflightError, ContentsError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
