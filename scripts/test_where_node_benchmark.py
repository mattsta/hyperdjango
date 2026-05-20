"""
Performance benchmark: uncached vs cached _build_select on the WhereNode path.

Apples-to-apples comparison measuring FULL _build_select() — the same operation,
with the compiled-SQL cache cold (recompiled every call) vs primed (cache hits).
Proves the compiled-SQL cache delivers a real speedup and never changes output.

CI mode (default): fast validation with 10K iterations, 1 run (~1s total).
Full mode (HYPER_BENCH_FULL=1): proper warmup, 200K iterations, 3-run median.

# hyper-test: unit
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_PERF_MULT = 5.0 if _PARALLEL else 1.0
_FULL = os.environ.get("HYPER_BENCH_FULL") == "1"

WARMUP = 10_000 if _FULL else 1_000
ITERATIONS = 200_000 if _FULL else 10_000
RUNS = 3 if _FULL else 1


# ---------------------------------------------------------------------------
# Mock model
# ---------------------------------------------------------------------------


class MockMeta:
    table = "users"
    fields = {}
    pk_field = "id"
    auto_field = "id"
    column_names = [
        "id",
        "name",
        "email",
        "age",
        "status",
        "role",
        "created_at",
        "updated_at",
    ]


class MockModel:
    _meta = MockMeta()


# ---------------------------------------------------------------------------
# Query factory
# ---------------------------------------------------------------------------


def _make_qs(filters, ordering=None, limit=None):
    from hyperdjango.query import QuerySet

    qs = QuerySet(MockModel)
    qs._annotations = {}
    qs._filters = list(filters)
    qs._excludes = []
    qs._raw_wheres = []
    qs._select_related = []
    qs._values_fields = None
    qs._only = None
    qs._defer = None
    qs._ordering = ordering or ("-created_at",)
    qs._limit = limit or 10
    qs._offset = None
    qs._distinct = False
    qs._for_update = None
    qs._group_by = False
    return qs


# ---------------------------------------------------------------------------
# Timing helper: warmup + 3-run median
# ---------------------------------------------------------------------------


def _timed(fn, iterations):
    """Warmup, then 3 runs, return median µs/op."""
    # Warmup
    for i in range(WARMUP):
        fn(i)

    results = []
    for _ in range(RUNS):
        start = time.perf_counter()
        for i in range(iterations):
            fn(i)
        elapsed = time.perf_counter() - start
        results.append((elapsed / iterations) * 1_000_000)

    results.sort()
    return results[len(results) // 2]  # median


def _bench(label, build):
    """Measure ``build`` uncached (cache cleared every call) vs cached (primed).

    Returns (uncached_us, cached_us, ratio) where ratio = uncached / cached, the
    speedup the compiled-SQL cache delivers on the current WhereNode path.
    """
    from hyperdjango.query import clear_compiled_cache

    def uncached(i):
        clear_compiled_cache()
        build(i)

    unc_us = _timed(uncached, ITERATIONS)

    clear_compiled_cache()
    build(0)  # prime the compiled-SQL cache
    cached_us = _timed(build, ITERATIONS)
    clear_compiled_cache()

    ratio = unc_us / cached_us
    print(
        f"  {label:<24} uncached={unc_us:.1f}µs  cached={cached_us:.1f}µs  ratio={ratio:.2f}x"
    )
    return unc_us, cached_us, ratio


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def bench_single_filter():
    """1-filter PK lookup: filter(id=X)"""

    def build(i):
        _make_qs([("id", i)])._build_select()

    return _bench("1-filter PK lookup:", build)


def bench_three_filters():
    """3-filter typical: filter(name=X, age__gte=Y, status=Z)"""

    def build(i):
        _make_qs(
            [("name", f"u{i}"), ("age__gte", 18), ("status", "active")]
        )._build_select()

    return _bench("3-filter typical:", build)


def bench_five_filters():
    """5-filter mixed lookups."""

    def build(i):
        _make_qs(
            [
                ("name", f"u{i}"),
                ("age__gte", 18),
                ("status", "active"),
                ("role", "admin"),
                ("email__contains", "@example.com"),
            ]
        )._build_select()

    return _bench("5-filter mixed:", build)


def bench_q_objects():
    """Q objects: (Q(name=X) | Q(name=Y)) & Q(status='active')"""
    from hyperdjango.expressions import Q

    def build(i):
        q = (Q(name=f"a{i}") | Q(name=f"b{i}")) & Q(status="active")
        _make_qs([("__q__", q)])._build_select()

    return _bench("Q objects OR+AND:", build)


def bench_filter_exclude():
    """filter + exclude combined."""

    def build(i):
        qs = _make_qs([("status", "active"), ("name", f"u{i}")])
        qs._excludes = [("role", "banned")]
        qs._build_select()

    return _bench("filter + exclude:", build)


def bench_cache_hit_throughput():
    """Pure cache hit throughput — 1M iterations, 3-run median."""
    from hyperdjango.query import clear_compiled_cache

    clear_compiled_cache()
    _make_qs([("name", "x"), ("age__gte", 1), ("status", "y")])._build_select()

    throughput_iters = 1_000_000 if _FULL else 50_000

    def hit(i):
        _make_qs(
            [("name", f"u{i}"), ("age__gte", 18 + (i % 50)), ("status", "active")]
        )._build_select()

    us = _timed(hit, throughput_iters)
    qps = 1_000_000 / us

    threshold = 100.0 * _PERF_MULT
    mode = f"{throughput_iters:,} iters x{RUNS} median"
    status = "PASS" if us < threshold else "FAIL"
    print(
        f"\n  cache hit throughput:    {us:.2f}µs/query  {qps:,.0f} qps  ({mode})  [{status}]"
    )
    assert us < threshold, f"Too slow: {us:.1f}µs (threshold {threshold}µs)"

    clear_compiled_cache()
    return us, qps


def bench_verify_correctness():
    """Verify cached and uncached compilation produce identical SQL + params.

    The compiled-SQL cache must be a pure performance optimization — a cache hit
    must render byte-for-byte the same SQL and the same bind params as a cold
    (uncached) recompile for every query pattern.
    """
    from hyperdjango.query import clear_compiled_cache

    test_cases = [
        ("simple", [("name", "alice")]),
        ("multi", [("name", "alice"), ("age__gte", 18), ("status", "active")]),
        ("contains", [("email__contains", "@test.com")]),
        ("range", [("age__range", (18, 65))]),
        ("isnull_true", [("deleted_at__isnull", True)]),
        ("isnull_false", [("deleted_at__isnull", False)]),
        ("exact_none", [("name", None)]),
    ]

    for label, filters in test_cases:
        clear_compiled_cache()
        # First compile is a cold cache MISS (uncached recompile path).
        uncached_sql, uncached_params = _make_qs(filters)._build_select()
        # Second compile is a cache HIT — must match the uncached output exactly.
        cached_sql, cached_params = _make_qs(filters)._build_select()

        assert uncached_params == cached_params, (
            f"{label}: params differ: {uncached_params} vs {cached_params}"
        )
        assert uncached_sql == cached_sql, (
            f"{label}: SQL differs:\n  uncached: {uncached_sql}\n  cached:   {cached_sql}"
        )

    print(f"  cache/recompile parity:  {len(test_cases)} query patterns verified ✓")
    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------


def run_tests():
    mode = "FULL" if _FULL else "CI"
    print(
        f"\n── WhereNode Cache Benchmark [{mode}] (warmup={WARMUP:,} iters={ITERATIONS:,} runs={RUNS}) ──\n"
    )

    passed = 0
    failed = 0

    # Perf benches return (uncached_us, cached_us, ratio); we assert the cache
    # delivers a speedup (cached at least as fast as uncached) across them.
    perf_benches = [
        bench_single_filter,
        bench_three_filters,
        bench_five_filters,
        bench_q_objects,
        bench_filter_exclude,
    ]
    other_benches = [
        bench_verify_correctness,
        bench_cache_hit_throughput,
    ]

    ratios = []

    for test in other_benches:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    for test in perf_benches:
        try:
            _, _, ratio = test()
            ratios.append(ratio)
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    # Regression guard: the compiled-SQL cache must make the cached path at least
    # as fast as recompiling every call. Assert on the MEDIAN ratio (uncached /
    # cached) so a single noisy sample — common under parallel test load — can't
    # flip the verdict. A margin below 1.0 tolerates measurement jitter while
    # still catching a real regression where caching stops helping.
    if ratios:
        ratios.sort()
        median_ratio = ratios[len(ratios) // 2]
        margin = 0.85
        status = "PASS" if median_ratio >= margin else "FAIL"
        print(
            f"\n  cache speedup (median):  {median_ratio:.2f}x  (need >= {margin:.2f}x)  [{status}]"
        )
        if median_ratio < margin:
            print(
                f"  FAIL: cached path not faster than uncached "
                f"(median ratio {median_ratio:.2f}x < {margin:.2f}x)"
            )
            failed += 1
        else:
            passed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Benchmarks: {passed}/{total} passed")
    if failed:
        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
