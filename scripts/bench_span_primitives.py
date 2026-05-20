"""
Microbench for the native span ring primitives (Phase 3 / P3.6).

Targets (release build, M-class Apple Silicon dev laptop):

    span_start + end            ≤ 2000 ns / cycle
    span_start + 3 attrs + end  ≤ 2500 ns / cycle
    span_start (unsampled)      ≤   50 ns / cycle  (no slot claim)
    span_drain (1000 spans)     ≤  500 μs / call

Every mode runs 500,000 iterations × median-of-5 runs. Fails if any
target misses by > 2x. Writes structured output to
`logs/bench_span_primitives.json`.

Run:
    uv run python scripts/bench_span_primitives.py
"""

import json
import sys
import time
from pathlib import Path

from hyperdjango._hyperdjango_native import (
    _span_drain,
    _span_end,
    _span_reset_for_tests,
    _span_set_attr_str,
    _span_start,
)

LOGS = Path(__file__).resolve().parent.parent / "logs"

ITERS_SAMPLED = 500_000
ITERS_UNSAMPLED = 2_000_000  # cheaper — let bench run longer for stability
RUNS = 5

TARGETS_NS = {
    "span_start_end_no_attrs": 2000,
    "span_start_3attrs_end": 2500,
    "span_start_unsampled": 50,
}
DRAIN_BATCH = 1000
DRAIN_TARGET_US = 500.0


def bench(name: str, iters: int, op) -> dict:
    """Run `op(i)` iters times, RUNS times, return median ns/op."""
    run_times: list[float] = []
    for _ in range(RUNS):
        # Reset the ring before each run so slot contention is fresh
        _span_reset_for_tests()
        t0 = time.perf_counter()
        op(iters)
        run_times.append(time.perf_counter() - t0)
        # Drain to free slots (must happen AFTER the timed section)
        _span_drain()
    run_times_sorted = sorted(run_times)
    elapsed = run_times_sorted[len(run_times_sorted) // 2]
    ns_per_op = (elapsed / iters) * 1e9
    per_run_ns = [(t / iters) * 1e9 for t in run_times]
    jitter_pct = (
        ((max(per_run_ns) - min(per_run_ns)) / ns_per_op * 100 / 2) if ns_per_op else 0
    )
    return {
        "name": name,
        "iterations": iters,
        "runs": RUNS,
        "median_ns_per_op": round(ns_per_op, 2),
        "per_run_ns": [round(v, 2) for v in per_run_ns],
        "jitter_pct": round(jitter_pct, 2),
        "target_ns": TARGETS_NS.get(name),
        "pass": (TARGETS_NS.get(name) is None) or ns_per_op <= TARGETS_NS[name] * 2,
    }


def _drive_start_end_no_attrs(n: int) -> None:
    for i in range(n):
        h = _span_start(0, i, 0, "b", True)
        _span_end(h)


def _drive_start_3attrs_end(n: int) -> None:
    key1, key2, key3 = "k1", "k2", "k3"
    val1, val2, val3 = "v1", "v2", "v3"
    for i in range(n):
        h = _span_start(0, i, 0, "b", True)
        _span_set_attr_str(h, key1, val1)
        _span_set_attr_str(h, key2, val2)
        _span_set_attr_str(h, key3, val3)
        _span_end(h)


def _drive_unsampled(n: int) -> None:
    for i in range(n):
        _ = _span_start(0, i, 0, "b", False)


def bench_drain() -> dict:
    """Measure the cost of draining a ring of N completed spans."""
    run_times: list[float] = []
    for _ in range(RUNS):
        _span_reset_for_tests()
        # Fill the ring with DRAIN_BATCH completed spans
        for i in range(DRAIN_BATCH):
            h = _span_start(0, i, 0, "drain", True)
            _span_end(h)
        # Now time just the drain call
        t0 = time.perf_counter()
        _span_drain()
        run_times.append(time.perf_counter() - t0)
    median_elapsed = sorted(run_times)[len(run_times) // 2]
    us_per_call = median_elapsed * 1_000_000
    return {
        "name": "span_drain_1000",
        "iterations": DRAIN_BATCH,
        "runs": RUNS,
        "median_us_per_call": round(us_per_call, 2),
        "target_us": DRAIN_TARGET_US,
        "pass": us_per_call <= DRAIN_TARGET_US * 2,
    }


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  Native span ring primitives microbench (P3.6)")
    print("=" * 70)
    print(f"  {ITERS_SAMPLED:,} iters × {RUNS} runs for sampled ops")
    print(f"  {ITERS_UNSAMPLED:,} iters × {RUNS} runs for unsampled ops")
    print()

    results: list[dict] = []

    # Sampled start + end, no attrs
    results.append(
        bench("span_start_end_no_attrs", ITERS_SAMPLED, _drive_start_end_no_attrs)
    )

    # Sampled start + 3 attrs + end
    results.append(
        bench("span_start_3attrs_end", ITERS_SAMPLED, _drive_start_3attrs_end)
    )

    # Unsampled fast-path (should be <50ns per call)
    results.append(bench("span_start_unsampled", ITERS_UNSAMPLED, _drive_unsampled))

    # Drain 1000 spans
    drain_result = bench_drain()
    results.append(drain_result)

    # ── Print results table ────────────────────────────────────────────
    header = (
        f"{'Operation':<28} {'median':>12} {'target':>12} {'jitter':>10} {'pass':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        if r["name"] == "span_drain_1000":
            median_str = f"{r['median_us_per_call']:.1f}μs"
            target_str = f"{r['target_us']:.0f}μs"
            jitter_str = ""
        else:
            median_str = f"{r['median_ns_per_op']:.0f}ns"
            target_str = f"{r['target_ns']}ns"
            jitter_str = f"±{r['jitter_pct']:.1f}%"
        pass_str = "PASS" if r["pass"] else "FAIL"
        print(
            f"{r['name']:<28} {median_str:>12} {target_str:>12} {jitter_str:>10} {pass_str:>6}"
        )

    # ── Dump JSON + exit ────────────────────────────────────────────────
    output = {
        "iters_sampled": ITERS_SAMPLED,
        "iters_unsampled": ITERS_UNSAMPLED,
        "runs_per_op": RUNS,
        "results": results,
    }
    out_path = LOGS / "bench_span_primitives.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Wrote: {out_path}")

    any_fail = any(not r["pass"] for r in results)
    if any_fail:
        print("\n  FAIL — one or more targets missed")
        return 1
    print("\n  ALL TARGETS HIT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
