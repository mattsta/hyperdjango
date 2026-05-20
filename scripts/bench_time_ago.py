"""
Microbench: time_ago / naturaltime hot path.

Measures per-call ns for cold path (no cache) vs cached path (bucket-keyed
LRU). Also proves output equivalence between the two implementations.

Stability: 1M ops × 5 runs, median of per-op ns reported with jitter.

Run: uv run python scripts/bench_time_ago.py
"""

import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache


# Match hypernews/app.py:940 exactly
def time_ago_original(timestamp_str):
    if not timestamp_str:
        return ""
    try:
        if isinstance(timestamp_str, str):
            ts = datetime.fromisoformat(timestamp_str)
        else:
            ts = timestamp_str
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        diff = now - ts
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = hours // 24
        if days < 30:
            return f"{days} day{'s' if days != 1 else ''} ago"
        months = days // 30
        if months < 12:
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"
    except ValueError, TypeError:
        return ""


# Inline OrderedDict variant — matches humanize.time_bucket_cached impl
from collections import OrderedDict

_inline_cache: OrderedDict = OrderedDict()


def time_ago_cached(timestamp_str):
    if not timestamp_str:
        return ""
    bucket = int(time.monotonic() / 30)
    key = (timestamp_str, bucket)
    cached = _inline_cache.get(key)
    if cached is not None:
        return cached
    result = time_ago_original(timestamp_str)
    _inline_cache[key] = result
    if len(_inline_cache) > 512:
        _inline_cache.popitem(last=False)
    return result


@lru_cache(maxsize=512)
def _time_ago_lru_impl(timestamp_str, bucket):
    return time_ago_original(timestamp_str)


def time_ago_lru(timestamp_str):
    if not timestamp_str:
        return ""
    bucket = int(time.monotonic() / 30)
    return _time_ago_lru_impl(timestamp_str, bucket)


def bench(fn, ts_list, iterations: int) -> float:
    """Return ns per op, calling fn on a cycle of ts_list."""
    n_ts = len(ts_list)
    start = time.perf_counter_ns()
    for i in range(iterations):
        fn(ts_list[i % n_ts])
    end = time.perf_counter_ns()
    return (end - start) / iterations


def run(label, fn, ts_list, iterations: int, runs: int):
    results_ns: list[float] = []
    for _ in range(runs):
        results_ns.append(bench(fn, ts_list, iterations))
    results_ns.sort()
    median = statistics.median(results_ns)
    jitter_pct = (results_ns[-1] - results_ns[0]) / median * 100 / 2
    print(
        f"  {label:<30} median={median:>8.1f} ns/op  "
        f"per-run={[f'{r:.0f}' for r in results_ns]}  "
        f"jitter=±{jitter_pct:.1f}%"
    )
    return median


def main():
    # Realistic workload: a user profile page has ~5-10 distinct post
    # timestamps, each referenced ~5 times. That's 38 total calls with
    # high temporal locality — exactly the cache-friendly case.
    now = datetime.now(UTC)
    ts_list = [
        now - timedelta(minutes=5),
        now - timedelta(hours=2),
        now - timedelta(days=1),
        now - timedelta(days=7),
        now - timedelta(days=30),
        now - timedelta(days=100),
    ]

    # Correctness: cached output must match original on the same bucket
    bucket = int(time.monotonic() / 30)
    for ts in ts_list:
        got_orig = time_ago_original(ts)
        got_cache = time_ago_cached(ts)
        if got_orig != got_cache:
            print(f"FAIL: {ts}: orig={got_orig!r} cache={got_cache!r}")
            sys.exit(1)
    print("  correctness: cached output matches original ✓")

    # Clear caches for fair cold-path bench
    _inline_cache.clear()
    _time_ago_lru_impl.cache_clear()

    ITERATIONS = 1_000_000
    RUNS = 5

    print(f"\nBench: {ITERATIONS:,} ops × {RUNS} runs, cycling 6 timestamps")
    print()
    run("original (no cache)", time_ago_original, ts_list, ITERATIONS, RUNS)
    print()
    run("inline OrderedDict cache", time_ago_cached, ts_list, ITERATIONS, RUNS)
    print()
    run("functools.lru_cache", time_ago_lru, ts_list, ITERATIONS, RUNS)

    print(f"\n  inline cache size: {len(_inline_cache)}")
    print(f"  lru_cache info: {_time_ago_lru_impl.cache_info()}")


if __name__ == "__main__":
    main()
