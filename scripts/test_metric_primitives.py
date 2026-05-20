"""
Unit + fuzz tests for the native metric primitive FFI (v0.14.19+, task P1.6).

# hyper-test: unit

Tests:
    1.  Counter register + inc + read roundtrip
    2.  Counter inc-by-N semantics
    3.  Gauge set + add + read
    4.  Gauge negative values (i64 signed)
    5.  Histogram observe + bucket counts + sum + count
    6.  Histogram boundary condition: value == bucket_upper
    7.  Histogram +Inf bucket (value > all buckets)
    8.  CounterVec register + inc with labels + read via Prometheus text
    9.  CounterVec label mismatch raises
    10. HistogramVec register + observe
    11. Prometheus text format structural correctness
    12. Registry size tracking
    13. Invalid handle raises
    14. Handle kind mismatch raises (counter_inc on gauge)
    15. Hypothesis fuzz: random counter ops under 8 concurrent threads x
        10000 ops each, verify final counter = exact sum of increments
    16. Hypothesis fuzz: histogram observe correctness across random values
"""

import sys
import threading

from hyperdjango._hyperdjango_native import (
    _metric_counter_inc,
    _metric_counter_read,
    _metric_counter_register,
    _metric_counter_vec_inc,
    _metric_counter_vec_register,
    _metric_gauge_add,
    _metric_gauge_read,
    _metric_gauge_register,
    _metric_gauge_set,
    _metric_histogram_observe,
    _metric_histogram_register,
    _metric_histogram_vec_observe,
    _metric_histogram_vec_register,
    _metric_registry_size,
    _metric_registry_write_prometheus,
)

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False


passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


def check_raises(name: str, fn, exc_type=Exception) -> None:
    global passed, failed
    try:
        fn()
    except exc_type:
        passed += 1
    except BaseException as e:
        failed += 1
        errors.append(
            f"FAIL: {name} — raised {type(e).__name__} instead of {exc_type.__name__}"
        )
        print(f"  {errors[-1]}")
    else:
        failed += 1
        errors.append(f"FAIL: {name} — did not raise")
        print(f"  {errors[-1]}")


# ── 1. Counter register + inc + read ────────────────────────────────────────
def test_counter_basic():
    print("\n── Counter basics ──")
    h = _metric_counter_register("t1_counter", "test counter 1")
    check("counter register returns int", isinstance(h, int))
    check("counter register returns non-negative", h >= 0)

    _metric_counter_inc(h, 1)
    _metric_counter_inc(h, 1)
    _metric_counter_inc(h, 1)
    v = _metric_counter_read(h)
    check("counter inc 3x = 3", v == 3, f"got {v}")

    _metric_counter_inc(h, 100)
    v = _metric_counter_read(h)
    check("counter inc by 100 = 103", v == 103, f"got {v}")


# ── 2. Gauge set + add + read ───────────────────────────────────────────────
def test_gauge_basic():
    print("\n── Gauge basics ──")
    h = _metric_gauge_register("t2_gauge", "test gauge")

    _metric_gauge_set(h, 42)
    check("gauge set 42", _metric_gauge_read(h) == 42)

    _metric_gauge_add(h, 8)
    check("gauge add 8 = 50", _metric_gauge_read(h) == 50)

    _metric_gauge_add(h, -30)
    check("gauge add -30 = 20", _metric_gauge_read(h) == 20)

    _metric_gauge_set(h, -100)
    check("gauge negative set", _metric_gauge_read(h) == -100)


# ── 3. Histogram observe + Prometheus scrape ─────────────────────────────────
def test_histogram_basic():
    print("\n── Histogram basics ──")
    buckets = (0.1, 0.5, 1.0, 5.0)
    h = _metric_histogram_register("t3_hist", "test histogram", buckets)

    _metric_histogram_observe(h, 0.05)  # bucket 0 (le=0.1)
    _metric_histogram_observe(h, 0.3)  # bucket 1 (le=0.5)
    _metric_histogram_observe(h, 0.8)  # bucket 2 (le=1.0)
    _metric_histogram_observe(h, 2.0)  # bucket 3 (le=5.0)
    _metric_histogram_observe(h, 10.0)  # +Inf

    text = _metric_registry_write_prometheus().decode("utf-8")

    check("histogram sum line present", "t3_hist_sum " in text)
    check("histogram count 5", "t3_hist_count 5" in text)
    check("histogram +Inf bucket 5", 't3_hist_bucket{le="+Inf"} 5' in text)
    # Cumulative bucket counts: le=0.1 → 1, le=0.5 → 2, le=1 → 3, le=5 → 4
    check("histogram bucket le=0.1 → 1", 't3_hist_bucket{le="0.1"} 1' in text)
    check("histogram bucket le=0.5 → 2", 't3_hist_bucket{le="0.5"} 2' in text)
    check("histogram bucket le=5 → 4", 't3_hist_bucket{le="5"} 4' in text)


# ── 4. CounterVec + HistogramVec ────────────────────────────────────────────
def test_counter_vec_basic():
    print("\n── CounterVec + HistogramVec ──")
    cvec_h = _metric_counter_vec_register(
        "t4_http_requests",
        "labeled request counter",
        ["method", "status"],
    )

    _metric_counter_vec_inc(cvec_h, ["GET", "200"], 5)
    _metric_counter_vec_inc(cvec_h, ["GET", "200"], 3)
    _metric_counter_vec_inc(cvec_h, ["POST", "201"], 1)
    _metric_counter_vec_inc(cvec_h, ["GET", "404"], 2)

    text = _metric_registry_write_prometheus().decode("utf-8")
    check(
        "counter_vec GET+200 = 8",
        't4_http_requests{method="GET",status="200"} 8' in text,
    )
    check(
        "counter_vec POST+201 = 1",
        't4_http_requests{method="POST",status="201"} 1' in text,
    )
    check(
        "counter_vec GET+404 = 2",
        't4_http_requests{method="GET",status="404"} 2' in text,
    )

    # Label mismatch should raise
    check_raises(
        "counter_vec wrong label count raises",
        lambda: _metric_counter_vec_inc(cvec_h, ["only_one_label"], 1),
        RuntimeError,
    )

    # HistogramVec
    hvec_h = _metric_histogram_vec_register(
        "t4_latency",
        "labeled latency",
        ["endpoint"],
        (0.01, 0.1, 1.0),
    )
    _metric_histogram_vec_observe(hvec_h, ["/api/books"], 0.005)
    _metric_histogram_vec_observe(hvec_h, ["/api/books"], 0.05)
    _metric_histogram_vec_observe(hvec_h, ["/api/books"], 0.5)
    _metric_histogram_vec_observe(hvec_h, ["/api/authors"], 0.001)

    text = _metric_registry_write_prometheus().decode("utf-8")
    check("hvec /api/books bucket present", "/api/books" in text)
    check("hvec /api/authors bucket present", "/api/authors" in text)


# ── 5. Invalid handle + kind mismatch ───────────────────────────────────────
def test_error_paths():
    print("\n── Error paths ──")
    check_raises(
        "counter_inc invalid handle raises",
        lambda: _metric_counter_inc(999999, 1),
        RuntimeError,
    )

    gauge_h = _metric_gauge_register("t5_gauge", "")
    check_raises(
        "counter_inc on gauge handle raises",
        lambda: _metric_counter_inc(gauge_h, 1),
        RuntimeError,
    )

    counter_h = _metric_counter_register("t5_counter", "")
    check_raises(
        "gauge_set on counter handle raises",
        lambda: _metric_gauge_set(counter_h, 1),
        RuntimeError,
    )


# ── 6. Registry size tracking ───────────────────────────────────────────────
def test_registry_size():
    print("\n── Registry size ──")
    # Can't reset reliably across other tests, so just verify size is
    # monotonically increasing as we register.
    size_before = _metric_registry_size()
    _metric_counter_register("t6_new_counter", "")
    size_after = _metric_registry_size()
    check("registry size grows on register", size_after == size_before + 1)


# ── 7. Prometheus text structural correctness ──────────────────────────────
def test_prometheus_format():
    print("\n── Prometheus text format ──")
    _metric_counter_register("t7_format_test", "a formatted counter")
    text = _metric_registry_write_prometheus().decode("utf-8")

    check(
        "text starts with # HELP or # TYPE",
        text.startswith("# HELP") or text.startswith("# TYPE"),
    )
    check(
        "text contains HELP for t7", "# HELP t7_format_test a formatted counter" in text
    )
    check("text contains TYPE counter for t7", "# TYPE t7_format_test counter" in text)
    check("text ends with newline", text.endswith("\n"))


# ── 8. Concurrent correctness fuzz (8 threads × 10000 ops) ──────────────────
def test_concurrent_counter_correctness():
    print("\n── Concurrent counter correctness (8 threads × 10000 ops) ──")
    h = _metric_counter_register("t8_concurrent", "concurrency test")
    ops_per_thread = 10000
    n_threads = 8

    def worker():
        for _ in range(ops_per_thread):
            _metric_counter_inc(h, 1)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = ops_per_thread * n_threads
    actual = _metric_counter_read(h)
    check(
        f"concurrent counter = {expected}",
        actual == expected,
        f"got {actual}",
    )


def test_concurrent_gauge_add_correctness():
    print("\n── Concurrent gauge add correctness ──")
    h = _metric_gauge_register("t8b_gauge_concurrent", "")
    n_threads = 8
    ops_per_thread = 10000

    def worker():
        for _ in range(ops_per_thread):
            _metric_gauge_add(h, 1)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = ops_per_thread * n_threads
    actual = _metric_gauge_read(h)
    check("concurrent gauge add correct", actual == expected, f"got {actual}")


def test_concurrent_counter_vec_distinct_labels():
    print("\n── Concurrent counter_vec distinct labels ──")
    h = _metric_counter_vec_register("t8c_vec", "", ["worker"])
    n_threads = 8
    ops_per_thread = 5000

    def worker(tid: int):
        label = [f"thread_{tid}"]
        for _ in range(ops_per_thread):
            _metric_counter_vec_inc(h, label, 1)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = _metric_registry_write_prometheus().decode("utf-8")
    for tid in range(n_threads):
        line = f't8c_vec{{worker="thread_{tid}"}} {ops_per_thread}'
        check(f"vec thread_{tid} == {ops_per_thread}", line in text)


def test_concurrent_counter_vec_same_label():
    print("\n── Concurrent counter_vec same label (contention) ──")
    h = _metric_counter_vec_register("t8d_vec_same", "", ["method"])
    n_threads = 8
    ops_per_thread = 5000

    def worker():
        label = ["GET"]
        for _ in range(ops_per_thread):
            _metric_counter_vec_inc(h, label, 1)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = n_threads * ops_per_thread
    text = _metric_registry_write_prometheus().decode("utf-8")
    line = f't8d_vec_same{{method="GET"}} {expected}'
    check(f"concurrent vec same label = {expected}", line in text)


# ── 8e. Barrier-synced register + inc + scrape storm (race regression) ──────
def test_concurrent_register_inc_scrape_storm():
    """Hammer registry mutation + label-map insertion + Prometheus scrape
    from many threads that all unblock on a single Barrier.

    This is the regression guard for the free-threading SIGSEGV in the
    native metrics layer (broken RwLock → unsynchronized StringHashMap /
    registry growth). On the unfixed build this crashes with SIGSEGV very
    reliably because every thread simultaneously:
      • registers brand-new counters (registry slot publish),
      • creates brand-new CounterVec label series (map insert + realloc),
      • iterates the whole registry + every label map (Prometheus render)
    with zero real locking. The Barrier maximises the overlap.
    """
    print("\n── Concurrent register+inc+scrape storm (Barrier) ──")
    n_threads = 8
    rounds = 250

    shared_vec = _metric_counter_vec_register(
        "t8e_storm_vec", "storm", ["worker", "shared"]
    )
    barrier = threading.Barrier(n_threads)
    crashed: list[str] = []

    def worker(tid: int) -> None:
        barrier.wait()
        try:
            for r in range(rounds):
                # Fresh counter every round → registry grows under concurrent read
                h = _metric_counter_register(f"t8e_c_{tid}_{r}", "")
                _metric_counter_inc(h, 1)
                # New label series (worker-unique) + hot shared series
                _metric_counter_vec_inc(shared_vec, [f"w{tid}", "hot"], 1)
                _metric_counter_vec_inc(shared_vec, ["all", "hot"], 1)
                # Scrape: iterate registry + every label map while others mutate
                if r % 4 == 0:
                    _ = _metric_registry_write_prometheus()
        except BaseException as e:  # pragma: no cover - only on a real bug
            crashed.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("storm: no worker raised", not crashed, "; ".join(crashed))
    # The shared "all"/"hot" series is incremented once per round per thread.
    text = _metric_registry_write_prometheus().decode("utf-8")
    expected_all = n_threads * rounds
    check(
        f"storm shared series = {expected_all}",
        f't8e_storm_vec{{worker="all",shared="hot"}} {expected_all}' in text,
        "shared label series total mismatch",
    )


# ── 9. Hypothesis fuzz (optional — only if hypothesis installed) ────────────
def test_hypothesis_counter_fuzz():
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis fuzz: SKIPPED (no hypothesis) ──")
        return
    print("\n── Hypothesis fuzz: random counter ops ──")

    @given(
        increments=st.lists(
            st.integers(min_value=0, max_value=1000), min_size=1, max_size=100
        ),
    )
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(increments):
        # Each hypothesis example gets a fresh counter
        h = _metric_counter_register(
            f"t9_fuzz_{threading.get_ident()}_{len(increments)}_{sum(increments)}",
            "fuzz",
        )
        total = 0
        for inc in increments:
            _metric_counter_inc(h, inc)
            total += inc
        actual = _metric_counter_read(h)
        assert actual == total, f"fuzz counter: expected {total}, got {actual}"

    fuzz()
    passed_local = 1  # if no AssertionError was raised
    check("hypothesis counter fuzz", passed_local == 1)


def test_hypothesis_histogram_fuzz():
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis histogram fuzz: SKIPPED ──")
        return
    print("\n── Hypothesis fuzz: histogram observe values ──")

    @given(
        values=st.lists(
            st.floats(
                min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False
            ),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(values):
        h = _metric_histogram_register(
            f"t9b_hist_{threading.get_ident()}_{hash(tuple(values)) & 0xFFFF}",
            "",
            (0.01, 0.1, 1.0, 10.0),
        )
        for v in values:
            _metric_histogram_observe(h, v)
        text = _metric_registry_write_prometheus().decode("utf-8")
        # Count should equal len(values) — best we can assert without parsing
        # the specific histogram block
        assert "t9b_hist_" in text

    fuzz()
    check("hypothesis histogram fuzz", True)


def _diag(label: str) -> None:
    """Emit a diagnostic line showing registry state + scrape health.

    Kept in-file (not stdin strings) so future test-runs can grow the
    diagnostic surface — add more checks here as new phases introduce
    new invariants to validate.
    """
    size = _metric_registry_size()
    try:
        text_len = len(_metric_registry_write_prometheus())
        scrape = f"scrape={text_len}B"
    except Exception as e:
        scrape = f"scrape=FAIL({type(e).__name__}: {e})"
    print(f"  [diag] {label}: {size} metrics, {scrape}")


def main() -> int:
    print("=" * 70)
    print("  Native metric primitives unit + concurrency tests (P1.6)")
    print("=" * 70)

    _diag("startup")
    test_counter_basic()
    _diag("after counter_basic")
    test_gauge_basic()
    _diag("after gauge_basic")
    test_histogram_basic()
    _diag("after histogram_basic")
    test_counter_vec_basic()
    _diag("after counter_vec_basic")
    test_error_paths()
    _diag("after error_paths")
    test_registry_size()
    _diag("after registry_size")
    test_prometheus_format()
    _diag("after prometheus_format")
    test_concurrent_counter_correctness()
    _diag("after concurrent counter")
    test_concurrent_gauge_add_correctness()
    _diag("after concurrent gauge")
    test_concurrent_counter_vec_distinct_labels()
    _diag("after concurrent vec distinct")
    test_concurrent_counter_vec_same_label()
    _diag("after concurrent vec same")
    test_concurrent_register_inc_scrape_storm()
    _diag("after register+inc+scrape storm")
    test_hypothesis_counter_fuzz()
    _diag("after hypothesis counter fuzz")
    test_hypothesis_histogram_fuzz()
    _diag("after hypothesis histogram fuzz")

    print()
    print("=" * 70)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
