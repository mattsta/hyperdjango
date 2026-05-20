#!/usr/bin/env python3
"""Test connection timeout and query timeout configuration.

Tests:
1. Default connection (no explicit timeout) works
2. Explicit connect_timeout passes through to pool
3. Query timeout (statement_timeout) kills long-running queries
4. Query timeout does NOT affect fast queries
5. Connect timeout rejects unreachable hosts
6. Settings integration (conf.py defaults)
7. Timeout error messages are proper Python exceptions
"""

# hyper-test: db_isolated

import os
import sys
import time

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
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

    # ── Test 1: Default connection (backward compatible) ──────────────────
    print("\n=== Test 1: Default connection (no explicit timeouts) ===")
    pool = _db_configure(conn_str, 2)
    check("default pool connects", pool >= 0, f"handle={pool}")
    rows = _db_query(pool, "SELECT 1", [])
    check("simple query works", len(rows) == 1 and rows[0][0] == 1, f"got {rows}")
    _db_close_pool(pool)

    # ── Test 2: Explicit connect timeout ──────────────────────────────────
    print("\n=== Test 2: Explicit connect timeout ===")
    pool = _db_configure(conn_str, 2, 5000, 0)  # 5s connect, no query timeout
    check("pool with 5s connect timeout works", pool >= 0)
    rows = _db_query(pool, "SELECT 42", [])
    check("query works with connect timeout set", rows[0][0] == 42)
    _db_close_pool(pool)

    # ── Test 3: Query timeout kills long queries ──────────────────────────
    print("\n=== Test 3: Query timeout (statement_timeout) ===")
    pool = _db_configure(conn_str, 2, 10000, 500)  # 500ms query timeout
    check("pool with 500ms query timeout", pool >= 0)

    # Fast query should succeed
    rows = _db_query(pool, "SELECT 1", [])
    check("fast query succeeds under timeout", len(rows) == 1)

    # Slow query should be killed by PostgreSQL
    timed_out = False
    t0 = time.perf_counter()
    try:
        _db_query(pool, "SELECT pg_sleep(5)", [])  # 5s sleep vs 500ms timeout
    except RuntimeError as e:
        timed_out = True
        elapsed = time.perf_counter() - t0
        check("slow query raises RuntimeError", True)
        check(
            "timeout happened within ~1s (not 5s)",
            elapsed < 2.0,
            f"took {elapsed:.2f}s",
        )
        err_msg = str(e).lower()
        check(
            "error mentions timeout/cancel",
            "timeout" in err_msg or "cancel" in err_msg or "statement" in err_msg,
            f"msg: {e}",
        )

    if not timed_out:
        check(
            "slow query was cancelled by timeout",
            False,
            "pg_sleep(5) completed without timeout!",
        )

    # Connection should still be usable after timeout
    rows = _db_query(pool, "SELECT 'still alive'", [])
    check(
        "connection usable after timeout",
        len(rows) == 1 and rows[0][0] == "still alive",
    )
    _db_close_pool(pool)

    # ── Test 4: No query timeout (0 = unlimited) ─────────────────────────
    print("\n=== Test 4: No query timeout (unlimited) ===")
    pool = _db_configure(conn_str, 2, 10000, 0)  # no query timeout
    # pg_sleep(0.1) = 100ms — should complete fine with no timeout
    t0 = time.perf_counter()
    rows = _db_query(pool, "SELECT pg_sleep(0.1)", [])
    elapsed = time.perf_counter() - t0
    check("100ms query completes with no timeout", len(rows) == 1)
    check("took ~100ms", 0.05 < elapsed < 1.0, f"took {elapsed:.2f}s")
    _db_close_pool(pool)

    # ── Test 5: Connect timeout with unreachable host ─────────────────────
    print("\n=== Test 5: Connect timeout with unreachable host ===")
    # Use a non-routable IP to trigger connect timeout
    bad_conn_str = "postgresql://user:pass@192.0.2.1:5432/nope"
    t0 = time.perf_counter()
    try:
        # 1s connect timeout — should fail fast instead of hanging
        bad_pool = _db_configure(bad_conn_str, 1, 1000, 0)
        # If we somehow connected, clean up
        _db_close_pool(bad_pool)
        check("unreachable host raises error", False, "connected to non-routable IP!")
    except RuntimeError:
        elapsed = time.perf_counter() - t0
        check("unreachable host raises RuntimeError", True)
        check("connect timeout bounded (< 10s)", elapsed < 10.0, f"took {elapsed:.2f}s")

    # ── Test 6: Settings integration ──────────────────────────────────────
    print("\n=== Test 6: Settings integration ===")
    from hyperdjango.conf import DEFAULTS, get_setting

    check("CONNECT_TIMEOUT default is 10000", DEFAULTS["CONNECT_TIMEOUT"] == 10000)
    check("QUERY_TIMEOUT default is 0", DEFAULTS["QUERY_TIMEOUT"] == 0)
    check("get_setting CONNECT_TIMEOUT", get_setting("CONNECT_TIMEOUT") == 10000)
    check("get_setting QUERY_TIMEOUT", get_setting("QUERY_TIMEOUT") == 0)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All connection timeout tests passed!")


if __name__ == "__main__":
    main()
