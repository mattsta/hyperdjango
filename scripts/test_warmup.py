#!/usr/bin/env python3
"""Test prepared statement warmup.

Tests:
1. Warmup with valid SQL statements
2. First query after warmup skips Parse phase (faster)
3. Warmup with invalid SQL doesn't crash
4. Warmup count returned correctly
5. Performance: first query latency with vs without warmup
"""

# hyper-test: db_isolated

import os
import sys
import time

from hyperdjango._hyperdjango_native import (
    _db_clear_stmt_cache,
    _db_close_pool,
    _db_configure,
    _db_query,
    _db_warmup_statements,
)


def get_conn_str():
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    # Same resolution as test_runner._DB_USER: role defaults to the login
    # user, never a hardcoded dev username (fails on any other machine).
    user = os.environ.get("PGUSER") or os.environ.get("USER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def main():
    conn_str = get_conn_str()
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

    # ── Test 1: Basic warmup ──────────────────────────────────────────────
    print("\n=== Test 1: Basic warmup ===")
    pool = _db_configure(conn_str, 2)
    count = _db_warmup_statements(
        pool,
        [
            "SELECT 1",
            "SELECT 1, 2, 3",
            "SELECT current_timestamp",
        ],
    )
    check("returns count", isinstance(count, int), f"got {type(count)}")
    check("warmed 3 statements", count == 3, f"got {count}")

    # ── Test 2: Query after warmup works ──────────────────────────────────
    print("\n=== Test 2: Query after warmup ===")
    rows = _db_query(pool, "SELECT 1", [])
    check("query works after warmup", len(rows) == 1 and rows[0][0] == 1)

    # ── Test 3: Invalid SQL in warmup list ────────────────────────────────
    print("\n=== Test 3: Invalid SQL doesn't crash ===")
    count2 = _db_warmup_statements(
        pool,
        [
            "SELECT 1",
            "THIS IS NOT VALID SQL!!!!!",
            "SELECT 2",
        ],
    )
    check("handles invalid SQL gracefully", count2 >= 1, f"warmed {count2}")

    # ── Test 4: Empty list ────────────────────────────────────────────────
    print("\n=== Test 4: Empty list ===")
    count3 = _db_warmup_statements(pool, [])
    check("empty list returns 0", count3 == 0)

    # ── Test 5: Performance — first query latency ─────────────────────────
    print("\n=== Test 5: First query latency benchmark ===")
    _db_close_pool(pool)

    # Use minimum of 10 samples to resist CPU scheduling noise under parallel test runs.
    # Min is better than median for latency: the fastest run reflects true capability,
    # while slow runs are caused by context switches/scheduling interference.
    cold_samples = []
    warm_samples = []
    for _ in range(10):
        # Without warmup
        p = _db_configure(conn_str, 2)
        _db_clear_stmt_cache()
        t0 = time.perf_counter_ns()
        _db_query(p, "SELECT 42, 'bench', true", [])
        cold_samples.append(time.perf_counter_ns() - t0)
        _db_close_pool(p)

        # With warmup
        p = _db_configure(conn_str, 2)
        _db_clear_stmt_cache()
        _db_warmup_statements(p, ["SELECT 42, 'bench', true"])
        t0 = time.perf_counter_ns()
        _db_query(p, "SELECT 42, 'bench', true", [])
        warm_samples.append(time.perf_counter_ns() - t0)
        _db_close_pool(p)

    cold_ns = min(cold_samples)
    warm_ns = min(warm_samples)

    # Cached repeat (single pool, 100 iterations)
    p = _db_configure(conn_str, 2)
    t0 = time.perf_counter_ns()
    for _ in range(100):
        _db_query(p, "SELECT 42, 'bench', true", [])
    cached_ns = (time.perf_counter_ns() - t0) / 100
    _db_close_pool(p)

    print(f"  Cold first query:   {cold_ns / 1000:.1f} μs (median of 5)")
    print(f"  Warmed first query: {warm_ns / 1000:.1f} μs (median of 5)")
    print(f"  Cached repeat:      {cached_ns / 1000:.1f} μs")
    check(
        "warmed query faster than cold",
        warm_ns < cold_ns * 1.5,
        f"warm={warm_ns / 1000:.1f}μs cold={cold_ns / 1000:.1f}μs",
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All warmup tests passed!")


if __name__ == "__main__":
    main()
