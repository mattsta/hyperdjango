"""
Microbench for the native metric primitive FFI (v0.14.19+, task P1.5).

Targets:

    Counter.inc()                     ≤  50 ns / op
    Gauge.set()                       ≤  50 ns / op
    Gauge.add()                       ≤  50 ns / op
    Histogram.observe()               ≤ 100 ns / op
    CounterVec.inc(['GET', '200'])    ≤ 250 ns / op
    HistogramVec.observe(...)         ≤ 300 ns / op
    registry.write_prometheus() 50×   ≤   1 ms / op

Every mode runs 1M iterations × median-of-5 runs. Fails if any
target misses by > 2x. Writes structured output to
`logs/bench_metric_primitives.json`.

Run:
    uv run python scripts/bench_metric_primitives.py
"""

import json
import sys
import time
from pathlib import Path

from hyperdjango._hyperdjango_native import (
    _metric_counter_inc,
    _metric_counter_read,
    _metric_counter_register,
    _metric_counter_vec_inc,
    _metric_counter_vec_register,
    _metric_gauge_add,
    _metric_gauge_register,
    _metric_gauge_set,
    _metric_histogram_observe,
    _metric_histogram_register,
    _metric_histogram_vec_observe,
    _metric_histogram_vec_register,
    _metric_registry_size,
    _metric_registry_write_prometheus,
)

LOGS = Path(__file__).resolve().parent.parent / "logs"

# ── Benchmark config ─────────────────────────────────────────────────────────

ITERS = 1_000_000
RUNS = 5

# Targets in nanoseconds per op
TARGETS_NS = {
    "counter_inc": 50,
    "gauge_set": 50,
    "gauge_add": 50,
    "histogram_observe": 100,
    "counter_vec_inc": 250,
    "histogram_vec_observe": 300,
}

# Special target: whole-registry scrape ≤ 1ms when 50 counters registered
SCRAPE_TARGET_MS = 1.0
SCRAPE_ITERS = 1000


def bench(name: str, op, iters: int = ITERS) -> dict:
    """Run `op(i)` iters times, RUNS times, return median + jitter."""
    run_times: list[float] = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        for i in range(iters):
            op(i)
        run_times.append(time.perf_counter() - t0)
    run_times_sorted = sorted(run_times)
    median_elapsed = run_times_sorted[len(run_times_sorted) // 2]
    ns_per_op = (median_elapsed / iters) * 1e9
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
        "pass": (TARGETS_NS.get(name) is None)
        or ns_per_op <= TARGETS_NS[name] * 2,  # fail if > 2x target
    }


def bench_scrape(scrape_iters: int = SCRAPE_ITERS) -> dict:
    """Scrape the full Prometheus text N times, measure per-call cost."""
    run_times: list[float] = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        for _ in range(scrape_iters):
            _ = _metric_registry_write_prometheus()
        run_times.append(time.perf_counter() - t0)
    median_elapsed = sorted(run_times)[len(run_times) // 2]
    ms_per_call = (median_elapsed / scrape_iters) * 1000
    return {
        "name": "registry_write_prometheus",
        "iterations": scrape_iters,
        "runs": RUNS,
        "median_ms_per_call": round(ms_per_call, 3),
        "target_ms": SCRAPE_TARGET_MS,
        "pass": ms_per_call <= SCRAPE_TARGET_MS * 2,
    }


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  Native metric primitives microbench (Phase 1 / P1.5)")
    print("=" * 70)
    print(f"  {ITERS:,} iterations × {RUNS} runs per operation")
    print()

    # ── Register the metrics we'll bench ─────────────────────────────────
    counter_h = _metric_counter_register("bench_counter", "microbench counter")
    gauge_h = _metric_gauge_register("bench_gauge", "microbench gauge")
    hist_h = _metric_histogram_register(
        "bench_hist",
        "microbench histogram",
        (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
    )
    cvec_h = _metric_counter_vec_register(
        "bench_cvec",
        "microbench labeled counter",
        ["method", "status"],
    )
    hvec_h = _metric_histogram_vec_register(
        "bench_hvec",
        "microbench labeled histogram",
        ["endpoint"],
        (0.001, 0.01, 0.1, 1.0, 10.0),
    )

    # Pre-populate the counter_vec with one label so we're measuring the
    # hit path, not the initial insert
    _metric_counter_vec_inc(cvec_h, ["GET", "200"], 0)
    _metric_histogram_vec_observe(hvec_h, ["/api/v1/books/"], 0.0)

    results: list[dict] = []

    # Counter.inc
    results.append(bench("counter_inc", lambda i: _metric_counter_inc(counter_h, 1)))

    # Gauge.set / Gauge.add
    results.append(bench("gauge_set", lambda i: _metric_gauge_set(gauge_h, i)))
    results.append(bench("gauge_add", lambda i: _metric_gauge_add(gauge_h, 1)))

    # Histogram.observe
    results.append(
        bench("histogram_observe", lambda i: _metric_histogram_observe(hist_h, 0.123))
    )

    # CounterVec.inc — hit the pre-populated slot
    labels = ["GET", "200"]
    results.append(
        bench("counter_vec_inc", lambda i: _metric_counter_vec_inc(cvec_h, labels, 1))
    )

    # HistogramVec.observe
    hvec_labels = ["/api/v1/books/"]
    results.append(
        bench(
            "histogram_vec_observe",
            lambda i: _metric_histogram_vec_observe(hvec_h, hvec_labels, 0.05),
        )
    )

    # ── Register 50 additional counters, then bench full Prometheus scrape
    for i in range(50):
        _metric_counter_register(f"bench_scrape_counter_{i}", f"scrape counter {i}")
    # Increment each one so they have non-zero values
    registry_size = _metric_registry_size()
    print(f"  Registry size before scrape bench: {registry_size} metrics\n")

    scrape_result = bench_scrape()
    results.append(scrape_result)

    # ── Print results table ─────────────────────────────────────────────
    print(f"{'Operation':<28} {'median':>12} {'target':>12} {'jitter':>10} {'pass':>6}")
    print("-" * 70)
    for r in results:
        if r["name"] == "registry_write_prometheus":
            median_str = f"{r['median_ms_per_call']:.3f}ms"
            target_str = f"{r['target_ms']}ms"
            jitter_str = ""
        else:
            median_str = f"{r['median_ns_per_op']:.1f}ns"
            target_str = f"{r['target_ns']}ns"
            jitter_str = f"±{r['jitter_pct']:.1f}%"
        pass_str = "PASS" if r["pass"] else "FAIL"
        print(
            f"{r['name']:<28} {median_str:>12} {target_str:>12} {jitter_str:>10} {pass_str:>6}"
        )

    # ── Verify correctness (counter was incremented N times per bench run)
    expected = ITERS * RUNS
    actual = _metric_counter_read(counter_h)
    correctness_pass = actual == expected
    print()
    print(
        f"  Counter correctness: {actual} == {expected} → "
        f"{'PASS' if correctness_pass else 'FAIL'}"
    )

    # ── Write JSON + exit ───────────────────────────────────────────────
    output = {
        "iterations": ITERS,
        "runs_per_op": RUNS,
        "results": results,
        "counter_correctness": {
            "expected": expected,
            "actual": actual,
            "pass": correctness_pass,
        },
    }
    out_path = LOGS / "bench_metric_primitives.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n  Wrote: {out_path}")

    any_fail = any(not r["pass"] for r in results) or not correctness_pass
    if any_fail:
        print("\n  FAIL — one or more targets missed")
        return 1
    print("\n  ALL TARGETS HIT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
