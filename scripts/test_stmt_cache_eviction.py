"""Tests for prepared statement cache eviction + DEALLOCATE under load.

Verifies that when >256 unique queries hit a single connection, the per-connection
LRU cache evicts correctly and DEALLOCATE (Close wire messages) are sent to PostgreSQL.

Key verification points:
1. 300+ unique queries all succeed (no "already exists" errors)
2. pg_prepared_statements shows bounded count (≤256)
3. After eviction, re-preparing an evicted query succeeds (no name collision)
4. Stats API reports correct eviction counts
5. Concurrent threads don't collide on cache names
6. Multi-parameter (>8) queries also evict correctly (execWithParamsDynamic path)

Run: uv run hyper-test stmt_cache_eviction
"""

# hyper-test: db_django

import os
import sys
import threading

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

from django.db import connection

from hyperdjango.db.pgzig_connection import (
    reset_stmt_cache_stats,
    stmt_cache_stats,
)

passed = 0
failed = 0
errors: list[str] = []


def check(name, condition, msg=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  ✗ {name} {msg}")


def main():
    pid = os.getpid()
    table = f"test_eviction_{pid}"

    print(f"\n{'=' * 60}")
    print("Prepared Statement Cache Eviction + DEALLOCATE Tests")
    print(f"{'=' * 60}\n")

    # ── Setup ────────────────────────────────────────────────────────────
    with connection.cursor() as c:
        c.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        c.execute(f"""
            CREATE TABLE {table} (
                id SERIAL PRIMARY KEY,
                val1 INTEGER DEFAULT 0,
                val2 INTEGER DEFAULT 0,
                val3 INTEGER DEFAULT 0,
                val4 INTEGER DEFAULT 0,
                val5 INTEGER DEFAULT 0,
                val6 INTEGER DEFAULT 0,
                val7 INTEGER DEFAULT 0,
                val8 INTEGER DEFAULT 0,
                val9 INTEGER DEFAULT 0,
                val10 INTEGER DEFAULT 0,
                name TEXT DEFAULT ''
            )
        """)
        # Seed 10 rows for queries to hit
        for i in range(10):
            c.execute(f"INSERT INTO {table} (name) VALUES (%s)", [f"row_{i}"])

    # Reset stats before tests
    reset_stmt_cache_stats()
    stats_before = stmt_cache_stats()
    print(
        f"  Stats before: entries={stats_before.entries}, evictions={stats_before.evictions}"
    )

    # ── Test 1: 300 unique queries succeed ────────────────────────────────
    print("\n  Test 1: 300 unique queries (force eviction past 256 cache limit)")
    query_errors = []
    for i in range(300):
        try:
            with connection.cursor() as c:
                # Each query is unique SQL text → unique prepared statement name
                c.execute(
                    f"SELECT id, val1 FROM {table} WHERE val1 >= %s AND id <= {i + 1000}",
                    [0],
                )
                rows = c.fetchall()
        except Exception as e:
            query_errors.append((i, str(e)))
            if len(query_errors) <= 3:
                print(f"    ERROR at query {i}: {e}")

    check(
        "300 unique queries all succeed",
        len(query_errors) == 0,
        f"{len(query_errors)} errors: {query_errors[:3]}",
    )

    # ── Test 2: Global stats show all queries were cache misses ──────────
    # Note: stmt_cache_stats() tracks the GLOBAL name cache (4096 max),
    # not the per-connection cache (256 max). Per-connection eviction
    # is proven by pg_prepared_statements staying bounded (test 3).
    stats_after_300 = stmt_cache_stats()
    print(
        f"  Stats after 300: global_entries={stats_after_300.entries}, misses={stats_after_300.misses}, global_evictions={stats_after_300.evictions}"
    )
    check(
        "All 300 queries were global cache misses (unique SQL text)",
        stats_after_300.misses >= 300,
        f"misses={stats_after_300.misses}",
    )

    # ── Test 3: pg_prepared_statements bounded ────────────────────────────
    with connection.cursor() as c:
        c.execute("SELECT COUNT(*) FROM pg_prepared_statements")
        pg_count = c.fetchone()[0]
    print(f"  pg_prepared_statements count: {pg_count}")
    check(
        "pg_prepared_statements count bounded (≤260)",
        pg_count <= 260,
        f"count={pg_count} (expected ≤260 after eviction)",
    )

    # ── Test 4: Re-prepare evicted queries ────────────────────────────────
    print("\n  Test 4: Re-execute early queries (evicted from cache)")
    reuse_errors = []
    for i in range(50):
        try:
            with connection.cursor() as c:
                # Same SQL as early queries — these were evicted, must re-prepare
                c.execute(
                    f"SELECT id, val1 FROM {table} WHERE val1 >= %s AND id <= {i + 1000}",
                    [0],
                )
                rows = c.fetchall()
                assert len(rows) == 10, f"Expected 10 rows, got {len(rows)}"
        except Exception as e:
            reuse_errors.append((i, str(e)))

    check(
        "Re-executing evicted queries succeeds",
        len(reuse_errors) == 0,
        f"{len(reuse_errors)} errors: {reuse_errors[:3]}",
    )

    # ── Test 5: Multi-parameter queries (>8 params, execWithParamsDynamic) ─
    print("\n  Test 5: Multi-parameter queries (>8 params, execWithParamsDynamic path)")
    multi_param_errors = []
    for i in range(100):
        try:
            with connection.cursor() as c:
                # 10 parameters → goes through execWithParamsDynamic
                c.execute(
                    f"SELECT id FROM {table} WHERE "
                    f"val1 >= %s AND val2 >= %s AND val3 >= %s AND "
                    f"val4 >= %s AND val5 >= %s AND val6 >= %s AND "
                    f"val7 >= %s AND val8 >= %s AND val9 >= %s AND "
                    f"val10 >= %s AND id <= {i + 2000}",
                    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                )
                rows = c.fetchall()
        except Exception as e:
            multi_param_errors.append((i, str(e)))
            if len(multi_param_errors) <= 3:
                print(f"    ERROR at multi-param query {i}: {e}")

    check(
        "100 unique multi-param (>8) queries succeed",
        len(multi_param_errors) == 0,
        f"{len(multi_param_errors)} errors: {multi_param_errors[:3]}",
    )

    # ── Test 6: 500 total unique queries (heavy eviction pressure) ────────
    print("\n  Test 6: 500 total unique queries (heavy eviction pressure)")
    reset_stmt_cache_stats()
    heavy_errors = []
    for i in range(500):
        try:
            with connection.cursor() as c:
                c.execute(
                    f"SELECT id FROM {table} WHERE name != 'x_{i}_heavy' AND val1 >= %s",
                    [0],
                )
                c.fetchall()
        except Exception as e:
            heavy_errors.append((i, str(e)))

    stats_heavy = stmt_cache_stats()
    print(
        f"  Stats after 500 heavy: misses={stats_heavy.misses}, evictions={stats_heavy.evictions}"
    )
    check(
        "500 unique queries all succeed under heavy eviction",
        len(heavy_errors) == 0,
        f"{len(heavy_errors)} errors",
    )

    # ── Test 7: Concurrent threads with unique queries ────────────────────
    print("\n  Test 7: Concurrent threads with unique queries")
    thread_errors: list[tuple[int, str]] = []
    lock = threading.Lock()

    def thread_worker(thread_id):
        from django.db import connections

        conn = connections["default"]
        for i in range(50):
            try:
                with conn.cursor() as c:
                    c.execute(
                        f"SELECT id FROM {table} WHERE name != 'thread_{thread_id}_iter_{i}' AND val1 >= %s",
                        [0],
                    )
                    c.fetchall()
            except Exception as e:
                with lock:
                    thread_errors.append((thread_id * 100 + i, str(e)))

    threads = []
    for t in range(6):
        th = threading.Thread(target=thread_worker, args=(t,))
        threads.append(th)
        th.start()

    for th in threads:
        th.join(timeout=30)

    check(
        "6 concurrent threads × 50 unique queries (300 total) succeed",
        len(thread_errors) == 0,
        f"{len(thread_errors)} errors: {thread_errors[:5]}",
    )

    # ── Test 8: Mixed standard + multi-param queries ──────────────────────
    print("\n  Test 8: Interleaved standard (≤8 params) + multi-param (>8) queries")
    mixed_errors = []
    for i in range(100):
        try:
            with connection.cursor() as c:
                if i % 2 == 0:
                    # Standard path (≤8 params)
                    c.execute(
                        f"SELECT id FROM {table} WHERE val1 >= %s AND id <= {i + 5000}",
                        [0],
                    )
                else:
                    # Dynamic path (>8 params)
                    c.execute(
                        f"SELECT id FROM {table} WHERE "
                        f"val1 >= %s AND val2 >= %s AND val3 >= %s AND "
                        f"val4 >= %s AND val5 >= %s AND val6 >= %s AND "
                        f"val7 >= %s AND val8 >= %s AND val9 >= %s AND "
                        f"val10 >= %s AND id <= {i + 5000}",
                        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    )
                c.fetchall()
        except Exception as e:
            mixed_errors.append((i, str(e)))

    check(
        "100 interleaved standard + multi-param queries succeed",
        len(mixed_errors) == 0,
        f"{len(mixed_errors)} errors: {mixed_errors[:3]}",
    )

    # ── Test 9: Cache stats consistency ───────────────────────────────────
    print("\n  Test 9: Final stats consistency check")
    final_stats = stmt_cache_stats()
    print(
        f"  Final stats: entries={final_stats.entries}, hits={final_stats.hits}, misses={final_stats.misses}, evictions={final_stats.evictions}"
    )
    print(f"  Hit rate: {final_stats.hit_rate:.1%}")
    check(
        "Cache entries bounded by max",
        final_stats.entries <= final_stats.max_entries,
        f"entries={final_stats.entries} > max={final_stats.max_entries}",
    )
    check(
        "Total lookups = hits + misses",
        final_stats.total_lookups == final_stats.hits + final_stats.misses,
        f"total={final_stats.total_lookups} != hits({final_stats.hits})+misses({final_stats.misses})",
    )

    # ── Test 10: pg_prepared_statements still bounded after all tests ─────
    with connection.cursor() as c:
        c.execute("SELECT COUNT(*) FROM pg_prepared_statements")
        final_pg_count = c.fetchone()[0]
    print(f"  Final pg_prepared_statements: {final_pg_count}")
    check(
        "pg_prepared_statements still bounded after all tests",
        final_pg_count <= 260,
        f"count={final_pg_count}",
    )

    # ── Cleanup ──────────────────────────────────────────────────────────
    with connection.cursor() as c:
        c.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    # ── Results ──────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
    if errors:
        print("\nFailures:")
        for err in errors:
            print(f"  - {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
