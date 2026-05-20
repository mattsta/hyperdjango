#!/usr/bin/env python3
"""Re-feed an ALREADY-MEASURED WebSocket run into the unified cross-suite record.

`benchmarks.websocket.run` feeds the unified dashboard itself at the end of a
run. This module is the recovery path for results that were measured before that
wiring existed (or whose feed failed): it loads an existing ``results.json`` and
performs exactly the same archive + dashboard render, WITHOUT measuring anything
— no servers started, no load generated, no numbers invented.

Usage:
    uv run python -m benchmarks.websocket.refeed
    uv run python -m benchmarks.websocket.refeed --results benchmarks/websocket/out/results.json --label ws-full
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.websocket.suite import (
    SUITE_KEY,
    UNIFIED_OUTDIR,
    feed_unified,
    websocket_completeness,
)

DEFAULT_RESULTS = Path(__file__).resolve().parent / "out" / "results.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--results",
        default=str(DEFAULT_RESULTS),
        help="existing WebSocket results.json to re-feed",
    )
    ap.add_argument(
        "--outdir", default=UNIFIED_OUTDIR, help="shared history/dashboard dir"
    )
    ap.add_argument(
        "--label", default="websocket-refeed", help="human label for this record"
    )
    ap.add_argument(
        "--cores",
        type=int,
        default=None,
        help="core count to record (default: this box)",
    )
    ap.add_argument(
        "--no-render",
        action="store_true",
        help="archive only; skip the dashboard render (no plotly needed)",
    )
    ap.add_argument(
        "--quick-source",
        action="store_true",
        help=(
            "the source results.json came from a --quick smoke run, so quarantine "
            "the record under diagnostics/ (a replay cannot tell which matrix was "
            "measured; sections are checked either way)"
        ),
    )
    ap.add_argument(
        "--expect-suites",
        default="",
        help="comma-separated suites the unified record is SUPPOSED to end up with",
    )
    args = ap.parse_args(argv)

    src = Path(args.results)
    if not src.exists():
        print(f"no results file at {src} — run the WebSocket suite first")
        return 2
    results = json.loads(src.read_text())
    verdict = websocket_completeness(results, full_matrix=not args.quick_source)
    if not verdict.complete:
        print("Restricted result set — archiving as DIAGNOSTIC:")
        for line in verdict.missing:
            print(f"  ✗ {line}")
    expect = [s.strip() for s in args.expect_suites.split(",") if s.strip()]
    fed = feed_unified(
        results,
        label=args.label,
        outdir=args.outdir,
        cores=args.cores,
        render=not args.no_render,
        diagnostic=not verdict.complete,
        expected_suites=expect or None,
    )
    print(f"Re-fed {src} (no measurement performed)")
    print(
        f"Archived run {fed.run_id}  suites=['{SUITE_KEY}']  "
        f"-> {args.outdir}/{fed.archive_dir}/"
    )
    if fed.dashboard:
        print(f"Unified dashboard (all suites) -> {fed.dashboard}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
