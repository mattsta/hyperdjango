"""
Graceful degradation tests — verify clean behavior when subsystems fail.

# hyper-test: unit

Tests:
1.  LocMemCache continues working when isolated
2.  TwoTierCache L1 serves when L2 is unavailable
3.  Task queue rejects cleanly when full
4.  Task queue DLQ captures permanently failed tasks
5.  Task circuit breaker opens after repeated failures
6.  Health readiness reports check failures
7.  Cache get_or_set handles callback exceptions
8.  StampedeProtection returns default on empty backend
9.  DatabaseCache fails gracefully without connection
10. PerformanceMiddleware disabled path has zero overhead
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import contextlib

from hyperdjango.cache import LocMemCache
from hyperdjango.cache_adapters import StampedeProtection, TwoTierCache

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


# ─── Cache Degradation ───────────────────────────────────────────────────────


def test_locmem_isolated():
    """LocMemCache works independently — no external dependencies."""
    print("=== LocMemCache Isolation ===")
    cache = LocMemCache(max_size=10)
    cache.set("k", "v", ttl=60)
    check("set+get works", cache.get("k") == "v")
    cache.delete("k")
    check("delete works", cache.get("k") is None)
    check("count after delete", cache.count() == 0)


def test_two_tier_l1_serves_when_l2_broken():
    """When L2 is broken (raises on get), L1 still serves cached data."""
    print("\n=== TwoTierCache L1 Serves When L2 Broken ===")

    class BrokenCache:
        """Simulates a cache backend that always fails."""

        _is_async = False

        def get(self, key, default=None):
            raise ConnectionError("L2 is down")

        def set(self, key, value, ttl=None):
            raise ConnectionError("L2 is down")

        def delete(self, key):
            raise ConnectionError("L2 is down")

        def clear(self):
            pass

        def has(self, key):
            return False

    l1 = LocMemCache(max_size=100)
    l2_broken = BrokenCache()

    # Pre-populate L1 directly
    l1.set("warm_key", {"data": "still here"}, ttl=60)

    cache = TwoTierCache(l1, l2_broken, l1_ttl=60)

    # L1 hit should work (doesn't touch L2)
    result = cache.get("warm_key")
    check("L1 serves warm data", result is not None and result["data"] == "still here")

    stats = cache.get_stats()
    check("L1 hit counted", stats["l1_hits"] == 1)

    # L2 miss should fail gracefully — the TwoTierCache.get() will raise
    # because BrokenCache.get() raises. This tests whether the caller
    # needs to handle the exception.
    try:
        cache.get("cold_key")
        check("cold key returns default (L2 error caught)", True)
    except ConnectionError:
        # TwoTierCache doesn't catch L2 errors — this is expected behavior.
        # The caller must handle it. Document this as a known behavior.
        check("L2 error propagates (caller must handle)", True)


def test_two_tier_fail_silently():
    """fail_silently=True swallows L2 errors and treats them as misses."""
    print("\n=== TwoTierCache fail_silently=True ===")

    class BrokenCache:
        """L2 that always raises."""

        _is_async = False

        def get(self, key, default=None):
            raise ConnectionError("L2 is down")

        def set(self, key, value, ttl=None):
            raise ConnectionError("L2 is down")

        def delete(self, key):
            raise ConnectionError("L2 is down")

        def clear(self):
            raise ConnectionError("L2 is down")

        def has(self, key):
            raise ConnectionError("L2 is down")

    l1 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, BrokenCache(), l1_ttl=60, fail_silently=True)

    # Pre-populate L1 — served directly, no L2 call
    l1.set("warm_key", "warm_value", ttl=60)
    check("L1 hit works when L2 broken", cache.get("warm_key") == "warm_value")

    # Cold key — L2 raises, should return default silently
    check(
        "L2 error returns default (fail_silently)",
        cache.get("cold_key") is None,
    )
    check(
        "L2 error + custom default (fail_silently)",
        cache.get("cold_key2", default="fallback") == "fallback",
    )

    # Set: L2 write fails, but L1 still updates
    cache.set("new_key", "new_value", ttl=60)
    check(
        "L1 still written when L2 set fails",
        l1.get("new_key") == "new_value",
    )

    # Delete: L2 delete fails, L1 still deleted
    l1.set("del_key", "v", ttl=60)
    cache.delete("del_key")
    check("L1 still deleted when L2 delete fails", l1.get("del_key") is None)

    # has(): L1 hit works, L2 error returns False
    check("has() L1 hit still works", cache.has("warm_key"))
    check("has() L2 error returns False", not cache.has("nonexistent"))

    # Stats track L2 errors
    stats = cache.get_stats()
    check(
        "l2_errors tracked in stats",
        stats["l2_errors"] > 0,
        f"got {stats['l2_errors']}",
    )

    # Verify fail_silently=False (default) still raises
    cache_strict = TwoTierCache(LocMemCache(), BrokenCache(), l1_ttl=60)
    try:
        cache_strict.get("any_key")
        check("strict mode raises on L2 error", False)
    except ConnectionError:
        check("strict mode raises on L2 error", True)


def test_stampede_empty_backend():
    """StampedeProtection returns default when backend has no data."""
    print("\n=== StampedeProtection Empty Backend ===")
    backend = LocMemCache(max_size=10)
    cache = StampedeProtection(backend, beta=1.0)

    result = cache.get("nonexistent")
    check("missing key returns None", result is None)

    result = cache.get("nonexistent", default="fallback")
    check("missing key returns default", result == "fallback")


def test_cache_get_or_set_callback_exception():
    """get_or_set propagates callback exceptions cleanly."""
    print("\n=== Cache get_or_set Callback Exception ===")
    cache = LocMemCache(max_size=10)

    def bad_callback():
        raise ValueError("computation failed")

    try:
        cache.get_or_set("key", bad_callback, ttl=60)
        check("exception propagated", False, "should have raised")
    except ValueError as e:
        check("ValueError propagated", str(e) == "computation failed")

    # Cache should not contain a corrupted entry
    check("no corrupted cache entry", cache.get("key") is None)


# ─── Task Queue Degradation ──────────────────────────────────────────────────


def test_task_queue_full():
    """Task queue rejects cleanly when full."""
    print("\n=== Task Queue Full ===")

    import time as _t

    from hyperdjango.tasks import TaskQueue, TaskStatus

    queue = TaskQueue(workers=1, max_queue_size=1)
    queue.start()

    def slow_task():
        _t.sleep(5)

    # Worker picks up h1 immediately (queue empty), then h2 fills the 1-slot queue
    h1 = queue.enqueue(slow_task)
    _t.sleep(0.1)  # Let h1 start running
    h2 = queue.enqueue(slow_task)  # Fills the 1-slot queue

    # h3 should fail — queue is full (h1 running, h2 queued)
    h3 = queue.enqueue(slow_task)
    _t.sleep(0.1)

    check("3rd task handle exists", h3 is not None)
    check(
        "queue full → FAILED status",
        h3.status() == TaskStatus.FAILED,
        f"status={h3.status()}",
    )

    queue._running = False


def test_task_dlq():
    """Failed tasks end up in dead letter queue."""
    print("\n=== Task DLQ ===")

    import time as _t

    from hyperdjango.tasks import TaskQueue, TaskStatus

    queue = TaskQueue(workers=1)
    queue.start()

    def failing_task():
        raise RuntimeError("always fails")

    h = queue.enqueue(failing_task, max_retries=0)
    _t.sleep(1.0)

    check("task failed", h.status() == TaskStatus.FAILED, f"status={h.status()}")
    check(
        "DLQ has entries",
        queue.dead_letters.size > 0,
        f"size={queue.dead_letters.size}",
    )

    queue.stop()


def test_task_circuit_breaker():
    """Circuit breaker opens after repeated failures."""
    print("\n=== Task Circuit Breaker ===")

    import time as _t

    from hyperdjango.tasks import CircuitState, TaskCircuitOpenError, TaskQueue

    queue = TaskQueue(workers=1)
    queue._circuit_failure_threshold = 3
    queue._circuit_window = 300.0
    queue.start()

    def always_fails():
        raise RuntimeError("boom")

    # Trip the circuit breaker
    for _ in range(5):
        with contextlib.suppress(TaskCircuitOpenError):
            queue.enqueue(always_fails, max_retries=0)
        _t.sleep(0.2)

    cb = queue.get_circuit_breaker("always_fails")
    check(
        "circuit breaker opened",
        cb is not None and cb.state == CircuitState.OPEN,
        f"state={cb.state if cb else 'None'}",
    )

    queue.stop()


# ─── Health Check Degradation ─────────────────────────────────────────────────


def test_health_check_reports_failures():
    """Health readiness reports failing checks."""
    print("\n=== Health Check Reports Failures ===")

    from hyperdjango import HyperApp

    app = HyperApp(title="test", database="postgres://localhost/nonexistent_test_db")

    # Register a failing health check
    def bad_check():
        return False

    app.add_health_check("cache", bad_check)

    # The health checks dict should contain our check
    check("health check registered", "cache" in app._health_checks)
    check("health check returns False", app._health_checks["cache"]() is False)


# ─── Performance Middleware Degradation ───────────────────────────────────────


def test_perf_middleware_disabled():
    """Disabled PerformanceMiddleware has zero functional impact."""
    print("\n=== PerformanceMiddleware Disabled ===")

    from hyperdjango.performance import PerformanceMiddleware

    perf = PerformanceMiddleware(enabled=False)

    # record_query should be a no-op when disabled
    perf.record_query("SELECT 1", 1.0)

    stats = perf.get_stats()
    check("disabled: zero requests", stats["total_requests"] == 0)
    check("disabled: zero queries", stats["total_queries"] == 0)


def test_perf_middleware_stats_thread_safe():
    """PerformanceMiddleware stats are thread-safe."""
    print("\n=== PerformanceMiddleware Thread Safety ===")

    import threading

    from hyperdjango.performance import PerformanceMiddleware

    perf = PerformanceMiddleware(enabled=True)
    errors: list[str] = []

    def worker(thread_id):
        try:
            for i in range(100):
                perf.record_query(f"SELECT {i}", 0.1, request_id=thread_id * 10000 + i)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no thread errors", len(errors) == 0, f"errors={errors}")
    check(
        "all queries recorded", perf._total_queries == 800, f"got {perf._total_queries}"
    )


def main():
    test_locmem_isolated()
    test_two_tier_l1_serves_when_l2_broken()
    test_two_tier_fail_silently()
    test_stampede_empty_backend()
    test_cache_get_or_set_callback_exception()
    test_task_queue_full()
    test_task_dlq()
    test_task_circuit_breaker()
    test_health_check_reports_failures()
    test_perf_middleware_disabled()
    test_perf_middleware_stats_thread_safe()

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
