#!/usr/bin/env python3
"""Combine ALREADY-ARCHIVED runs into one canonical record.

`save_run` unifies suites fed close together (same label / commit / host) on its
own. This is the after-the-fact path: two records that should have been one — an
HTTP run and a WebSocket run archived separately — are merged into a single
entry so the dashboard's Suite selector switches views of the SAME record instead
of making the reader flip between two half-records.

Nothing is measured and nothing is destroyed: the combined entry keeps a
per-suite `provenance` stamp (source timestamp, label, commit, host, run id), the
source JSON files stay on disk with a `merged_into` pointer, and only their index
rows are removed so the dashboard shows one record.

    uv run python -m benchmarks.core.merge canonical-baseline ws-full-refeed
    uv run python -m benchmarks.core.merge <run-id> <run-id> --label canonical
"""

from __future__ import annotations

import argparse
import datetime

from benchmarks.core.results import (
    declared_suites,
    find_entry,
    read_index,
    summarize,
    write_entry,
    write_index,
)

DEFAULT_OUT = "benchmarks/out"


def _combined_id(base: dict, now: datetime.datetime) -> str:
    return (
        now.strftime("%Y%m%dT%H%M%S")
        + f"{now.microsecond // 1000:03d}_"
        + str(base.get("sha") or "nogit")
    )


def merge_runs(
    outdir: str, refs: list[str], label: str = "", quiet: bool = False
) -> str | None:
    """Merge the referenced runs (ids or labels, first = base) into one entry.
    Returns the combined entry's id, or None when there was nothing to merge."""

    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    index = read_index(outdir)
    indexed = {e["id"] for e in index}
    sources: list[dict] = []
    seen: set[str] = set()
    for ref in refs:
        e = find_entry(outdir, ref)
        if e is None:
            say(f"  merge: no archived run matches {ref!r} — skipped")
            continue
        # A source already absorbed into a record that is still in the index has
        # nothing left to contribute — this is what makes re-running idempotent.
        into = e.get("merged_into")
        if into and into in indexed:
            say(f"  merge: {ref!r} was already merged into {into} — skipped")
            continue
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        sources.append(e)

    if len(sources) < 2:
        say(
            "  merge: nothing to do — fewer than two distinct records resolved "
            f"({[e['id'] for e in sources]})"
        )
        return None

    base = sources[0]
    ordered = sorted(sources, key=lambda e: e.get("ts", ""))
    shas = {e.get("sha") for e in sources}
    hosts = {e.get("host") for e in sources}
    if len(shas) > 1:
        say(
            f"  merge: WARNING sources span commits {sorted(shas)} — provenance records each"
        )
    if len(hosts) > 1:
        say(
            f"  merge: WARNING sources span hosts {sorted(hosts)} — provenance records each"
        )

    suites: dict = {}
    provenance: dict = {}
    declared: set[str] = set()
    for e in ordered:  # oldest first, so the NEWEST measurement of a suite wins
        declared |= set(declared_suites(e))
        for key, block in e.get("suites", {}).items():
            if key in suites:
                say(
                    f"  merge: suite {key!r} present in several sources — "
                    f"keeping the newer one from {e['id']}"
                )
            suites[key] = block
            provenance[key] = e.get("provenance", {}).get(key) or {
                "ts": e.get("ts", ""),
                "label": e.get("label", ""),
                "sha": e.get("sha", ""),
                "host": e.get("host", ""),
                "run_id": e["id"],
            }

    now = datetime.datetime.now()
    newest = ordered[-1]
    combined = {
        "id": _combined_id(base, now),
        "ts": newest.get("ts", base.get("ts", "")),
        "sha": base.get("sha", ""),
        "branch": base.get("branch", ""),
        "subject": base.get("subject", ""),
        "host": base.get("host", ""),
        "cores": base.get("cores") or newest.get("cores"),
        "label": label or base.get("label", ""),
        # The combined record stands for every source's DECLARED coverage, not
        # just the union of what they happened to measure — merging two records
        # that each missed a suite must not launder the gap away.
        "expected_suites": sorted(declared),
        "suites": suites,
        "provenance": provenance,
        "merged": True,
        "merged_from": [e["id"] for e in ordered],
    }
    write_entry(outdir, combined)

    for e in sources:
        e["merged_into"] = combined["id"]
        write_entry(outdir, e)

    drop = {e["id"] for e in sources}
    index = [row for row in index if row["id"] not in drop]
    index.append(summarize(combined))
    write_index(outdir, index)

    say(
        f"  merge: {len(sources)} records -> {combined['id']} "
        f"label={combined['label']!r} suites={sorted(suites)}"
    )
    for key, prov in sorted(provenance.items()):
        say(
            f"    suite {key:12s} measured {prov['ts']} (from {prov['run_id']}, label={prov['label']!r})"
        )
    return combined["id"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("refs", nargs="+", help="run ids or labels; the first is the base")
    ap.add_argument(
        "--outdir", default=DEFAULT_OUT, help="shared history/dashboard dir"
    )
    ap.add_argument(
        "--label",
        default="",
        help="label for the combined record (default: the base's)",
    )
    ap.add_argument(
        "--no-render", action="store_true", help="skip the dashboard re-render"
    )
    args = ap.parse_args(argv)

    combined = merge_runs(args.outdir, args.refs, label=args.label)
    if combined is None:
        return 0
    if not args.no_render:
        try:
            from benchmarks.core.dashboard import write_dashboard

            print(f"Unified dashboard (all suites) -> {write_dashboard(args.outdir)}")
        except ModuleNotFoundError as exc:
            print(f"Dashboard render skipped ({exc}) — the merged record is archived.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
