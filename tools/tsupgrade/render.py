"""Render the upgrade matrix as Markdown.

The table is the thing that gets published, so it has to be honest at a glance:
an unverified row must not look like a recommendation. Unknown verdicts are
rendered as ``unknown`` and the summary states plainly how many rows are still
unverified.

A verified row shows what proves it: the PostgreSQL minor it ran on and both
evidence records with their results, read from the records themselves. A
record that is missing or invalid is shown as such rather than skipped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .evidence import EvidenceError, load_record
from .matrix import EVIDENCE_MODES, OPTIONAL_EVIDENCE, Matrix, MatrixEntry

__all__ = ["render_markdown"]

_VERDICT_LABEL = {
    "in_place": "in place",
    "dump_restore": "dump + restore",
    "unsupported": "unsupported",
    "no_image": "no image",
    None: "unknown",
}


def _escape(text: str) -> str:
    """Escape the characters that would break a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _verdict_cell(entry: MatrixEntry) -> str:
    label = _VERDICT_LABEL.get(entry.verdict, str(entry.verdict))
    return "**{0}**".format(label) if entry.verdict == "unsupported" else label


_GITHUB = re.compile(r"^https://github\.com/[^/]+/[^/]+/(issues|pull)/(\d+)")
_RELEASE = re.compile(r"/releases/tag/([^/]+)$")


def reference_label(url: str) -> str:
    """A short link text: ``#10272`` for an issue or PR, else what it is."""
    m = _GITHUB.match(url)
    if m:
        return "#{0}".format(m.group(2))
    m = _RELEASE.search(url)
    if m:
        return "{0} release".format(m.group(1))
    if "registry" in url:
        return "registry"
    return "docs"


def _references_cell(entry: MatrixEntry) -> str:
    if not entry.references:
        return "—"
    return ", ".join("[{0}]({1})".format(reference_label(u), u) for u in entry.references)


def _cited(entry: MatrixEntry) -> List[str]:
    """The row's evidence keys, required ones first, in a fixed order."""
    return [m for m in EVIDENCE_MODES + OPTIONAL_EVIDENCE if m in (entry.evidence or {})]


def _records(entry: MatrixEntry, root: Optional[Path]) -> Dict[str, Optional[Dict[str, Any]]]:
    if not entry.evidence or root is None:
        return {}
    found: Dict[str, Optional[Dict[str, Any]]] = {}
    for mode in _cited(entry):
        try:
            found[mode] = load_record(root / entry.evidence[mode])
        except EvidenceError:
            found[mode] = None
    return found


def _pg_cell(records: Dict[str, Optional[Dict[str, Any]]]) -> str:
    minors = sorted({r["pg_version"] for r in records.values() if r and r.get("pg_version")})
    return " / ".join(minors) if minors else "—"


def _status(doc: Dict[str, Any]) -> str:
    """What a record's cell says. A control exists to break: one that errored
    in the update is the documented failure, never a green ``pass``; one that
    went through no longer shows what it was cited for."""
    if "control" not in doc:
        return doc["result"]
    if doc["result"] == "fail" and doc.get("stage") == "apply":
        return "errored as documented"
    if doc["result"] == "pass":
        return "did not error"
    return doc["result"]


def _evidence_cell(
    entry: MatrixEntry, records: Dict[str, Optional[Dict[str, Any]]], link_prefix: str
) -> str:
    if not entry.evidence:
        return "—"
    cells = []
    for mode in _cited(entry):
        if mode in records and records[mode] is None:
            status = "missing"
        elif mode in records:
            status = _status(records[mode])
        else:
            status = "?"
        cells.append("[{0}: {1}]({2}{3})".format(mode, status, link_prefix, entry.evidence[mode]))
    return " · ".join(cells)


def _rows(entries: Iterable[MatrixEntry], root: Optional[Path], link_prefix: str) -> List[str]:
    rows = []
    for e in entries:
        records = _records(e, root)
        rows.append(
            "| `{pg_s}` | `{ts_s}` | `{pg_t}` | `{ts_t}` | {verdict} | {pg} | {evidence} | {reason} | {refs} |".format(
                pg_s=_escape(e.source_pg),
                ts_s=_escape(e.source_ts),
                pg_t=_escape(e.target_pg),
                ts_t=_escape(e.target_ts),
                verdict=_verdict_cell(e),
                pg=_pg_cell(records),
                evidence=_evidence_cell(e, records, link_prefix),
                reason=_escape(e.reason) if e.reason else "—",
                refs=_references_cell(e),
            )
        )
    return rows


def _sort_key(e: MatrixEntry):
    def parts(v: str):
        out = []
        for chunk in str(v).split("."):
            out.append(int(chunk) if chunk.isdigit() else 0)
        return out

    return (parts(e.source_pg), parts(e.source_ts), parts(e.target_pg), parts(e.target_ts))


def render_markdown(
    matrix: Matrix,
    title: str = "TimescaleDB upgrade matrix",
    root: Optional[Path] = None,
    link_prefix: str = "../",
) -> str:
    """Render ``matrix`` as a complete Markdown document.

    ``root`` is the repository root the evidence paths are relative to; without
    it, results and PostgreSQL minors are not read. ``link_prefix`` turns an
    evidence path into a link from where the document is written (``docs/``).
    """
    entries = sorted(matrix.entries, key=_sort_key)
    total = len(entries)
    verified = sum(1 for e in entries if e.verified)
    unverified = total - verified

    out: List[str] = []
    out.append("# {0}".format(title))
    out.append("")
    out.append(
        "<!-- Generated by `make matrix`. Edit data/upgrade-matrix.json, not this file. -->"
    )
    out.append("")

    generated = matrix.meta.get("generated_at")
    if generated:
        out.append("Generated from `data/upgrade-matrix.json` at {0}.".format(generated))
        out.append("")

    out.append(
        "**{total} transitions listed — {verified} verified, {unverified} unverified.**".format(
            total=total, verified=verified, unverified=unverified
        )
    )
    out.append("")
    out.append(
        "> An `unknown` verdict means nobody has established the answer and "
        "written down where it came from. It does **not** mean the upgrade is "
        "safe, and it does not mean it is unsafe. Treat these rows as open "
        "questions: the tooling refuses to act on them rather than guessing."
    )
    out.append("")

    if verified:
        out.append(
            "A verdict is established only by running that migration with the "
            "upgrader itself (`verify/harness.py`), once on an empty database and "
            "once on a seeded one. Those runs are not the test suite: each leaves "
            "a record of what happened, and the verdict is written from the "
            "records. **Evidence** links both records and **PG** is "
            "the exact PostgreSQL minor they ran on: a row keyed by a PostgreSQL "
            "major is proven only at that minor. A row that changes PostgreSQL "
            "major ran both ways again with `pg_upgrade --link` (`link-` records), "
            "since `apply` offers it. A `direct-history-` record is a control: the "
            "same update without the stop `tsupgrade plan` inserts, run to show "
            "the stop is needed. It reads `errored as documented` when it broke "
            "the way the row's reason says, and never counts toward the verdict. "
            "`no image` rows are verified "
            "against the container registry: no such tag is published. "
            "**References** are upstream issues, "
            "pull requests and release notes that explain a verdict or a caveat; "
            "they are never its proof."
        )
        out.append("")
        out.append(
            "A verdict covers TimescaleDB and the route its records ran: the test "
            "databases hold `plpgsql` and `timescaledb` and nothing else. Other "
            "extensions (PostGIS, `timescaledb_toolkit`, `pg_stat_statements`, ...) "
            "are checked by preflight against the measured contents of each image, "
            "not by these verdicts."
        )
        out.append("")

    out.append("| Source PG | Source TS | Target PG | Target TS | Verdict | PG | Evidence | Reason | References |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    out.extend(_rows(entries, root, link_prefix))
    out.append("")

    with_steps = [e for e in entries if e.steps]
    if with_steps:
        out.append("## Required steps")
        out.append("")
        for e in with_steps:
            out.append("### {0}".format(e.transition))
            out.append("")
            for step in e.steps:
                out.append("1. {0}".format(step))
            out.append("")

    out.append("## Verdicts")
    out.append("")
    out.append("| Verdict | Meaning |")
    out.append("|---|---|")
    out.append("| `in place` | The existing data directory can be upgraded without exporting the data. A caveat in the reason still applies. |")
    out.append("| `dump + restore` | The data must be exported and reloaded into a new instance. |")
    out.append("| `unsupported` | This transition is not a supported path. |")
    out.append("| `no image` | No published `timescaledb-ha` image exists for the target pair. |")
    out.append("| `unknown` | Not established. The tooling will not act on it. |")
    out.append("")

    return "\n".join(out) + "\n"
