#!/usr/bin/env python3
"""Test _db_pool_stats native function.

Tests:
1. Pool stats after configure
2. Stats reflect correct pool size
3. Stats after queries (connection in use)
4. Thread-owned count
5. Multiple pools
6. Invalid pool handle
"""

# hyper-test: db_isolated

import os
import sys

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_pool_stats,
    _db_query,
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

    # ── Test 1: Pool stats after configure ────────────────────────────────
    print("\n=== Test 1: Pool stats after configure ===")
    pool = _db_configure(conn_str, 4)
    stats = _db_pool_stats(pool)
    check("returns dict", isinstance(stats, dict), f"got {type(stats)}")
    check("has 'total' key", "total" in stats, f"keys: {list(stats.keys())}")
    check("has 'available' key", "available" in stats)
    check("has 'in_use' key", "in_use" in stats)
    check("has 'thread_owned' key", "thread_owned" in stats)
    check("has 'pools_registered' key", "pools_registered" in stats)
    check("has 'active_handle' key", "active_handle" in stats)
    check("has 'database' key", "database" in stats)
    print(f"  Stats: {stats}")

    # ── Test 2: Correct pool size ─────────────────────────────────────────
    print("\n=== Test 2: Pool size ===")
    check("total == 4", stats["total"] == 4, f"got {stats['total']}")
    check("available <= total", stats["available"] <= stats["total"])
    expected_db = os.environ.get("PGDATABASE", "hyperdjango_test")
    check(
        f"database is {expected_db}",
        stats["database"] == expected_db,
        f"got {stats.get('database')}",
    )

    # ── Test 3: Stats after query ─────────────────────────────────────────
    print("\n=== Test 3: Stats after query ===")
    _db_query(pool, "SELECT 1", [])
    stats2 = _db_pool_stats(pool)
    check(
        "thread_owned >= 1 after query",
        stats2["thread_owned"] >= 1,
        f"got {stats2['thread_owned']}",
    )
    print(f"  Stats after query: {stats2}")

    # ── Test 4: Multiple pools ────────────────────────────────────────────
    print("\n=== Test 4: Multiple pools ===")
    pool2 = _db_configure(conn_str, 2)
    stats_pool2 = _db_pool_stats(pool2)
    check(
        "second pool total == 2",
        stats_pool2["total"] == 2,
        f"got {stats_pool2['total']}",
    )
    check(
        "pools_registered >= 2",
        stats_pool2["pools_registered"] >= 2,
        f"got {stats_pool2['pools_registered']}",
    )
    _db_close_pool(pool2)

    # ── Test 5: Invalid pool handle ───────────────────────────────────────
    print("\n=== Test 5: Invalid pool handle ===")
    stats_bad = _db_pool_stats(999)
    check("invalid handle returns dict", isinstance(stats_bad, dict))
    check(
        "invalid handle has no 'total'",
        "total" not in stats_bad,
        f"keys: {list(stats_bad.keys())}",
    )
    check("still has global stats", "pools_registered" in stats_bad)

    # ── Cleanup ───────────────────────────────────────────────────────────
    _db_close_pool(pool)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All pool stats tests passed!")


if __name__ == "__main__":
    main()
