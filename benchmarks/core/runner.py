"""Unified benchmark runner — run any subset of registered benchmarks with
automated server setup/teardown, archive every result suite into the shared
non-destructive history, and regenerate the one dashboard that unifies them all.

    uv run python -m benchmarks.core.runner --list
    uv run python -m benchmarks.core.runner --suite http --label fast-path
    uv run python -m benchmarks.core.runner --suite all --quick
"""

from __future__ import annotations

import argparse
import os

from benchmarks.core.dashboard import write_dashboard
from benchmarks.core.registry import registry
from benchmarks.core.results import save_run

DEFAULT_OUT = "benchmarks/out"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--suite",
        default="all",
        help="comma-separated suites to run, or 'all' (default)",
    )
    ap.add_argument("--label", default="", help="human label for this run")
    ap.add_argument("--quick", action="store_true", help="fast smoke matrix")
    ap.add_argument(
        "--outdir", default=DEFAULT_OUT, help="shared history/dashboard dir"
    )
    ap.add_argument(
        "--list", action="store_true", help="list registered benchmarks and exit"
    )
    args = ap.parse_args()

    reg = registry()
    if args.list or not reg:
        print("Registered benchmarks:")
        for k, b in reg.items():
            print(f"  {k:12s} {b.label} — {b.description}")
        return 0

    want = (
        list(reg)
        if args.suite == "all"
        else [s.strip() for s in args.suite.split(",") if s.strip()]
    )

    suites = {}
    for k in want:
        b = reg.get(k)
        if not b:
            print(f"  unknown suite '{k}' (registered: {list(reg)})")
            continue
        print(
            f"\n{'=' * 60}\n=== suite: {k} ({b.label}){' [quick]' if args.quick else ''}\n{'=' * 60}"
        )
        try:
            suites[k] = b.run(quick=args.quick)
        except Exception as e:  # noqa: BLE001 — one suite failing must not sink the run
            print(f"  !! suite {k} FAILED: {e}")

    if not suites:
        print("\nNo suites produced results.")
        return 1

    # The runner KNOWS what it set out to record, so the record says so: a suite
    # that failed mid-run leaves a declared-but-missing gap the dashboard labels,
    # instead of the reader having to infer coverage from what survived.
    declared = [k for k in want if k in reg]
    missing = [k for k in declared if k not in suites]
    # A --quick smoke matrix is not a comparison result, and neither is a battery
    # that lost a suite: both archive as DIAGNOSTIC — kept for the record,
    # invisible to the dashboard, the index and every merge target.
    diagnostic = bool(args.quick or missing)
    if missing:
        print(f"\n!! suites declared but not recorded: {missing}")
    run_id = save_run(
        args.outdir,
        suites,
        label=args.label,
        cores=os.cpu_count(),
        expected_suites=declared,
        diagnostic=diagnostic,
    )
    archive = "diagnostics" if diagnostic else "history"
    kind = "DIAGNOSTIC (restricted) run" if diagnostic else "COMPLETE run"
    print(
        f"\nArchived {kind} {run_id}  suites={list(suites)} -> {args.outdir}/{archive}/"
    )
    if diagnostic:
        print("  (quarantined: not shown on the unified dashboard, not a merge target)")
        return 0
    path = write_dashboard(args.outdir)
    print(f"Unified dashboard -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
