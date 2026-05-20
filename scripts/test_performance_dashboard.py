#!/usr/bin/env python3
"""Test performance monitoring middleware — query tracking, slow query detection, N+1 alerts.

Tests:
1. Query recording and stats
2. Slow query detection
3. N+1 pattern detection
4. Per-request headers (X-Query-Count, X-Query-Time)
5. Dashboard HTML endpoint
6. Dashboard JSON API
7. Ring buffer history
8. Thread safety
9. SQL normalization
"""

# hyper-test: unit

import sys
import threading

from hyperdjango import HyperApp
from hyperdjango.performance import (
    PerformanceMiddleware,
    _normalize_sql,
    set_perf_middleware,
)
from hyperdjango.testing import TestClient


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Query recording ───────────────────────────────────────────────────
    print("\n=== Query recording ===")

    perf = PerformanceMiddleware(slow_query_threshold_ms=50)
    set_perf_middleware(perf)

    # Simulate queries
    perf.record_query("SELECT * FROM users WHERE id = 1", 5.0)
    perf.record_query("SELECT * FROM orders WHERE user_id = 1", 3.0)

    stats = perf.get_stats()
    check(
        "total queries tracked",
        stats["total_queries"] == 2,
        f"got {stats['total_queries']}",
    )

    # ── Slow query detection ──────────────────────────────────────────────
    print("\n=== Slow query detection ===")

    perf2 = PerformanceMiddleware(slow_query_threshold_ms=10)
    perf2.record_query("SELECT * FROM big_table", 50.0)
    perf2.record_query("SELECT 1", 1.0)

    check("slow count", perf2._slow_count == 1, f"got {perf2._slow_count}")

    # ── N+1 detection ─────────────────────────────────────────────────────
    print("\n=== N+1 detection ===")

    app = HyperApp()
    perf3 = PerformanceMiddleware(slow_query_threshold_ms=100, n_plus_one_threshold=3)

    app.use(perf3)

    @app.get("/n-plus-one")
    async def n_plus_one_handler(request):
        # Simulate N+1: same query repeated many times
        rid = id(threading.current_thread())
        for i in range(5):
            perf3.record_query(f"SELECT * FROM users WHERE id = {i}", 2.0, rid)
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/n-plus-one")
    check("n+1 response ok", resp.ok)
    check(
        "n+1 header present",
        "X-N-Plus-One" in resp.headers,
        f"headers: {dict(resp.headers)}",
    )
    check(
        "n+1 count",
        resp.headers.get("X-N-Plus-One") == "1",
        f"got {resp.headers.get('X-N-Plus-One')}",
    )

    # Query count header
    check(
        "query count header",
        resp.headers.get("X-Query-Count") == "5",
        f"got {resp.headers.get('X-Query-Count')}",
    )

    # ── Per-request headers ───────────────────────────────────────────────
    print("\n=== Per-request headers ===")

    app2 = HyperApp()
    perf4 = PerformanceMiddleware()
    app2.use(perf4)

    @app2.get("/no-queries")
    async def no_queries(request):
        return {"ok": True}

    @app2.get("/with-queries")
    async def with_queries(request):
        rid = id(threading.current_thread())
        perf4.record_query("SELECT 1", 1.5, rid)
        perf4.record_query("SELECT 2", 2.5, rid)
        return {"ok": True}

    client2 = TestClient(app2)

    resp = client2.get("/no-queries")
    check("no queries count=0", resp.headers.get("X-Query-Count") == "0")
    check("no queries time=0", resp.headers.get("X-Query-Time") == "0.0ms")

    resp = client2.get("/with-queries")
    check("2 queries count", resp.headers.get("X-Query-Count") == "2")
    check("query time header", "ms" in resp.headers.get("X-Query-Time", ""))

    # ── Dashboard endpoints ───────────────────────────────────────────────
    print("\n=== Dashboard endpoints ===")

    resp = client2.get("/debug/performance")
    check("dashboard html", resp.ok and "Performance Dashboard" in resp.text())

    resp = client2.get("/debug/performance/json")
    check("dashboard json", resp.ok)
    data = resp.json()
    check("json has total_requests", "total_requests" in data)
    check("json has avg_queries", "avg_queries_per_request" in data)
    check("json has slow_count", "slow_query_count" in data)
    check("json has n_plus_one", "n_plus_one_count" in data)

    # ── Ring buffer ───────────────────────────────────────────────────────
    print("\n=== Ring buffer ===")

    app3 = HyperApp()
    perf5 = PerformanceMiddleware(max_history=5)
    app3.use(perf5)

    @app3.get("/tick")
    async def tick(request):
        return {"ok": True}

    client3 = TestClient(app3)
    for _ in range(10):
        client3.get("/tick")

    check("ring buffer capped", len(perf5._history) <= 5, f"got {len(perf5._history)}")
    check("total requests counted", perf5._total_requests == 10)

    # ── SQL normalization ─────────────────────────────────────────────────
    print("\n=== SQL normalization ===")

    check(
        "normalize ints",
        _normalize_sql("SELECT * FROM users WHERE id = 42")
        == "SELECT * FROM users WHERE id = ?",
    )
    check(
        "normalize strings",
        _normalize_sql("SELECT * FROM users WHERE name = 'alice'")
        == "SELECT * FROM users WHERE name = '?'",
    )
    check(
        "normalize mixed",
        _normalize_sql("UPDATE items SET price = 9.99 WHERE id = 1")
        == "UPDATE items SET price = ?.? WHERE id = ?",
    )

    # ── Disabled mode ─────────────────────────────────────────────────────
    print("\n=== Disabled mode ===")

    app4 = HyperApp()
    perf6 = PerformanceMiddleware(enabled=False)
    app4.use(perf6)

    @app4.get("/disabled")
    async def disabled(request):
        return {"ok": True}

    client4 = TestClient(app4)
    resp = client4.get("/disabled")
    check("disabled no headers", "X-Query-Count" not in resp.headers)

    # ── Thread safety ─────────────────────────────────────────────────────
    print("\n=== Thread safety ===")

    perf7 = PerformanceMiddleware()
    errors = []

    def record_many():
        try:
            for i in range(100):
                perf7.record_query(f"SELECT {i}", 1.0)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=record_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("thread safe no errors", len(errors) == 0, f"errors: {errors}")
    check(
        "thread safe count", perf7._total_queries == 400, f"got {perf7._total_queries}"
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All performance dashboard tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
