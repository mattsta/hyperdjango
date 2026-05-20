"""Tests for connection pool deduplication registry.

Tests _acquire_pool, _release_pool, pool_registry_stats, ref counting,
and Database.connect()/disconnect() sharing behavior.
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import time

from hyperdjango.database import (
    Database,
    _acquire_pool,
    _ensure_url_user,
    _pool_registry,
    _pool_registry_lock,
    _release_pool,
    pool_registry_stats,
)

_RAW_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hyperdjango_test")
DB_URL = _ensure_url_user(_RAW_URL)


def run_async(coro):
    return asyncio.run(coro)


# ── Registry unit tests ──────────────────────────────────────────────────


def test_registry_stats_initial():
    """Fresh registry is empty."""
    # Clear registry for test isolation
    with _pool_registry_lock:
        _pool_registry.clear()
    stats = pool_registry_stats()
    assert stats["pools"] == 0
    assert stats["total_refs"] == 0
    print("  PASS: Registry initial state is empty")


def test_acquire_creates_pool():
    """First acquire creates a new pool."""
    with _pool_registry_lock:
        _pool_registry.clear()

    handle = _acquire_pool(DB_URL, 5)
    assert handle is not None
    assert isinstance(handle, int)

    stats = pool_registry_stats()
    assert stats["pools"] == 1
    assert stats["total_refs"] == 1
    print(f"  PASS: Acquire creates pool (handle={handle})")

    # Clean up
    _release_pool(DB_URL, 5)


def test_acquire_deduplicates():
    """Second acquire with same params returns same handle, increments ref."""
    with _pool_registry_lock:
        _pool_registry.clear()

    h1 = _acquire_pool(DB_URL, 5)
    h2 = _acquire_pool(DB_URL, 5)
    assert h1 == h2, f"Expected same handle, got {h1} and {h2}"

    stats = pool_registry_stats()
    assert stats["pools"] == 1
    assert stats["total_refs"] == 2
    print("  PASS: Duplicate acquire returns same handle")

    # Clean up
    _release_pool(DB_URL, 5)
    _release_pool(DB_URL, 5)


def test_different_params_separate_pools():
    """Different max_size creates separate pools."""
    with _pool_registry_lock:
        _pool_registry.clear()

    h1 = _acquire_pool(DB_URL, 5)
    h2 = _acquire_pool(DB_URL, 10)
    assert h1 != h2, "Different pool sizes should create different pools"

    stats = pool_registry_stats()
    assert stats["pools"] == 2
    assert stats["total_refs"] == 2
    print("  PASS: Different params create separate pools")

    # Clean up
    _release_pool(DB_URL, 5)
    _release_pool(DB_URL, 10)


def test_release_decrements_ref():
    """Release decrements ref count without closing pool."""
    with _pool_registry_lock:
        _pool_registry.clear()

    _acquire_pool(DB_URL, 5)
    _acquire_pool(DB_URL, 5)  # ref=2

    _release_pool(DB_URL, 5)  # ref=1
    stats = pool_registry_stats()
    assert stats["pools"] == 1
    assert stats["total_refs"] == 1
    print("  PASS: Release decrements ref count")

    # Final cleanup
    _release_pool(DB_URL, 5)


def test_release_last_ref_removes():
    """Releasing last reference removes pool from registry."""
    with _pool_registry_lock:
        _pool_registry.clear()

    _acquire_pool(DB_URL, 5)
    _release_pool(DB_URL, 5)

    stats = pool_registry_stats()
    assert stats["pools"] == 0
    assert stats["total_refs"] == 0
    print("  PASS: Last release removes pool")


def test_release_nonexistent_noop():
    """Releasing a pool that doesn't exist is a no-op."""
    with _pool_registry_lock:
        _pool_registry.clear()

    _release_pool("postgres://nonexistent", 5)  # Should not raise
    stats = pool_registry_stats()
    assert stats["pools"] == 0
    print("  PASS: Release nonexistent is no-op")


# ── Database integration tests ────────────────────────────────────────────


def test_database_connect_creates_pool():
    """Database.connect() creates a pool in the registry."""
    with _pool_registry_lock:
        _pool_registry.clear()

    async def run():
        db = Database(DB_URL, max_size=5)
        await db.connect()
        stats = pool_registry_stats()
        assert stats["pools"] == 1
        assert stats["total_refs"] == 1
        await db.disconnect()

    run_async(run())
    stats = pool_registry_stats()
    assert stats["pools"] == 0
    print("  PASS: Database.connect() uses registry")


def test_database_shared_pool():
    """Two Database instances with same URL share one pool."""
    with _pool_registry_lock:
        _pool_registry.clear()

    async def run():
        db1 = Database(DB_URL, max_size=5)
        db2 = Database(DB_URL, max_size=5)
        await db1.connect()
        await db2.connect()

        assert db1._pool_handle == db2._pool_handle
        stats = pool_registry_stats()
        assert stats["pools"] == 1
        assert stats["total_refs"] == 2

        # Both can query
        rows1 = await db1.query("SELECT 1 AS n")
        rows2 = await db2.query("SELECT 2 AS n")
        assert rows1[0]["n"] == 1
        assert rows2[0]["n"] == 2

        await db1.disconnect()
        stats = pool_registry_stats()
        assert stats["pools"] == 1  # Still alive (db2 holds ref)

        await db2.disconnect()
        stats = pool_registry_stats()
        assert stats["pools"] == 0  # Now gone

    run_async(run())
    print("  PASS: Two Database instances share one pool")


def test_database_different_urls_separate():
    """Databases with different URLs get separate pools."""
    with _pool_registry_lock:
        _pool_registry.clear()

    async def run():
        db1 = Database(DB_URL, max_size=5)
        db2 = Database(DB_URL, max_size=10)  # Different max_size
        await db1.connect()
        await db2.connect()

        assert db1._pool_handle != db2._pool_handle
        stats = pool_registry_stats()
        assert stats["pools"] == 2

        await db1.disconnect()
        await db2.disconnect()

    run_async(run())
    print("  PASS: Different URLs/sizes get separate pools")


def test_database_disconnect_idempotent():
    """Calling disconnect() twice doesn't crash."""
    with _pool_registry_lock:
        _pool_registry.clear()

    async def run():
        db = Database(DB_URL, max_size=5)
        await db.connect()
        await db.disconnect()
        await db.disconnect()  # Should be no-op

    run_async(run())
    stats = pool_registry_stats()
    assert stats["pools"] == 0
    print("  PASS: Disconnect is idempotent")


def test_database_reconnect():
    """Can connect → disconnect → connect again."""
    with _pool_registry_lock:
        _pool_registry.clear()

    async def run():
        db = Database(DB_URL, max_size=5)
        await db.connect()
        h1 = db._pool_handle
        await db.disconnect()

        await db.connect()
        h2 = db._pool_handle
        assert h2 is not None
        await db.disconnect()

    run_async(run())
    stats = pool_registry_stats()
    assert stats["pools"] == 0
    print("  PASS: Reconnect works after disconnect")


def test_pool_dedup_benchmark():
    """Benchmark: shared pool vs fresh pool creation."""
    with _pool_registry_lock:
        _pool_registry.clear()

    iterations = 20

    # Fresh pool creation (no dedup)
    async def bench_fresh():
        total_ns = 0
        for _ in range(iterations):
            db = Database(DB_URL, max_size=3)
            start = time.perf_counter_ns()
            # Bypass dedup to measure raw pool creation
            from contextlib import suppress

            from hyperdjango._hyperdjango_native import (
                _db_close_pool,
                _db_configure,
            )

            from hyperdjango.database import _ensure_url_user

            conn_url = _ensure_url_user(DB_URL)
            h = _db_configure(conn_url, 3, 10000, 0)
            elapsed = time.perf_counter_ns() - start
            total_ns += elapsed
            with suppress(RuntimeError):
                _db_close_pool(h)
        return total_ns / iterations

    # Shared pool (dedup hit after first)
    async def bench_shared():
        total_ns = 0
        first_db = Database(DB_URL, max_size=3)
        await first_db.connect()
        for _ in range(iterations):
            db = Database(DB_URL, max_size=3)
            start = time.perf_counter_ns()
            await db.connect()
            elapsed = time.perf_counter_ns() - start
            total_ns += elapsed
            await db.disconnect()
        await first_db.disconnect()
        return total_ns / iterations

    fresh_ns = run_async(bench_fresh())
    with _pool_registry_lock:
        _pool_registry.clear()
    shared_ns = run_async(bench_shared())

    speedup = fresh_ns / shared_ns if shared_ns > 0 else 0
    print(
        f"  PASS: Pool dedup benchmark — fresh: {fresh_ns / 1e6:.1f}ms, shared: {shared_ns / 1e3:.1f}μs, speedup: {speedup:.0f}x"
    )


def main():
    tests = [
        # Registry unit tests
        test_registry_stats_initial,
        test_acquire_creates_pool,
        test_acquire_deduplicates,
        test_different_params_separate_pools,
        test_release_decrements_ref,
        test_release_last_ref_removes,
        test_release_nonexistent_noop,
        # Database integration tests
        test_database_connect_creates_pool,
        test_database_shared_pool,
        test_database_different_urls_separate,
        test_database_disconnect_idempotent,
        test_database_reconnect,
        # Benchmark
        test_pool_dedup_benchmark,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Connection Pool Deduplication Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback

            failed += 1
            errors.append((test.__name__, str(e)))
            traceback.print_exc()
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    # Final cleanup
    with _pool_registry_lock:
        _pool_registry.clear()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
