"""Command line entry point: ``python -m tsupgrade``.

Subcommands
-----------
``images refresh``    query the registry and regenerate ``data/images.json``
``images show``       summarise the catalog already on disk
``matrix scaffold``   add missing transitions to ``data/upgrade-matrix.json``
``matrix render``     render the matrix to Markdown
``matrix lookup``     look up one transition

Exit codes for ``matrix lookup`` are meant to be scripted against:
0 a verified verdict, 3 an entry with no established verdict, 4 no entry at all,
1 an error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .images import (
    RegistryError,
    build_catalog,
    default_catalog_path,
    fetch_tags,
    load_catalog,
)
from .matrix import MatrixError, default_matrix_path, load_matrix
from .preflight import (
    FAIL,
    UNKNOWN,
    PreflightError,
    PreflightOptions,
    render_report,
    run_preflight,
)
from .render import render_markdown
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

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (MatrixError, RegistryError, PreflightError) as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
