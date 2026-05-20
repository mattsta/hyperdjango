"""
Tests for connection pool optimization — slow query log, health checks, query timer, drain.

Usage:
    uv run hyper-test pool_optimization
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import time
import traceback

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.pool import PoolHealthChecker, QueryTimer, SlowQueryLog

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# DB setup / teardown
# ---------------------------------------------------------------------------


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    return db


async def teardown_db(db):
    await db.execute("DROP TABLE IF EXISTS hyper_slow_queries CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Unit Tests: SlowQueryLog
# ---------------------------------------------------------------------------


@test("SlowQueryLog: threshold filtering")
def test_slow_log_threshold():
    # Can't test DB writes without connection, but verify construction
    log = SlowQueryLog.__new__(SlowQueryLog)
    log.threshold_ms = 100.0
    log._count = 0
    assert log.threshold_ms == 100.0
    assert log.count == 0


@test("QueryTimer: stats tracking")
def test_timer_stats():
    timer = QueryTimer.__new__(QueryTimer)
    timer._in_flight = 0
    timer._in_flight_lock = __import__("threading").Lock()
    timer._total_queries = 10
    timer._total_time_ms = 500.0
    timer.threshold_ms = 100.0

    stats = timer.get_stats()
    assert stats["total_queries"] == 10
    assert stats["avg_query_ms"] == 50.0
    assert stats["in_flight"] == 0


@test("QueryTimer: in_flight tracking")
def test_timer_in_flight():
    timer = QueryTimer.__new__(QueryTimer)
    timer._in_flight = 3
    timer._in_flight_lock = __import__("threading").Lock()
    assert timer.in_flight == 3


@test("PoolHealthChecker: initial state")
def test_health_checker_initial():
    checker = PoolHealthChecker.__new__(PoolHealthChecker)
    checker.interval_seconds = 30.0
    checker._last_check = 0
    checker._last_result = False
    checker._check_count = 0
    checker._fail_count = 0
    checker._task = None

    stats = checker.get_stats()
    assert stats["healthy"] is False
    assert stats["checks"] == 0
    assert stats["failures"] == 0


# ---------------------------------------------------------------------------
# DB Tests: SlowQueryLog
# ---------------------------------------------------------------------------


@test("DB: SlowQueryLog ensure_table")
async def test_slow_log_ensure_table():
    db = get_db()
    log = SlowQueryLog(db, threshold_ms=50)
    await log.ensure_table()
    result = await db.query_val(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'hyper_slow_queries'"
    )
    assert result >= 1


@test("DB: SlowQueryLog records slow query")
async def test_slow_log_record():
    db = get_db()
    log = SlowQueryLog(db, threshold_ms=0)  # threshold 0 = log everything
    await db.execute("DELETE FROM hyper_slow_queries")

    await log.record("SELECT pg_sleep(0.01)", 15.5, [])

    rows = await log.get_recent()
    assert len(rows) == 1
    assert rows[0]["duration_ms"] > 10
    assert "pg_sleep" in rows[0]["sql_text"]
    assert log.count == 1


@test("DB: SlowQueryLog skips fast queries")
async def test_slow_log_skip_fast():
    db = get_db()
    log = SlowQueryLog(db, threshold_ms=1000)
    await db.execute("DELETE FROM hyper_slow_queries")

    await log.record("SELECT 1", 0.5, None)

    rows = await log.get_recent()
    assert len(rows) == 0


@test("DB: SlowQueryLog get_slowest")
async def test_slow_log_get_slowest():
    db = get_db()
    log = SlowQueryLog(db, threshold_ms=0)
    await db.execute("DELETE FROM hyper_slow_queries")

    await log.record("SELECT fast", 10.0)
    await log.record("SELECT slow", 500.0)
    await log.record("SELECT medium", 100.0)

    rows = await log.get_slowest(limit=2)
    assert len(rows) == 2
    assert rows[0]["duration_ms"] == 500.0
    assert rows[1]["duration_ms"] == 100.0


@test("DB: SlowQueryLog get_stats")
async def test_slow_log_get_stats():
    db = get_db()
    log = SlowQueryLog(db, threshold_ms=0)
    await db.execute("DELETE FROM hyper_slow_queries")

    await log.record("SELECT 1", 10.0)
    await log.record("SELECT 2", 20.0)
    await log.record("SELECT 3", 30.0)

    stats = await log.get_stats()
    assert stats["total"] == 3
    assert abs(stats["avg_ms"] - 20.0) < 0.1
    assert stats["max_ms"] == 30.0
    assert stats["min_ms"] == 10.0


@test("DB: SlowQueryLog cleanup")
async def test_slow_log_cleanup():
    db = get_db()
    log = SlowQueryLog(db, threshold_ms=0)
    await db.execute("DELETE FROM hyper_slow_queries")

    await log.record("old query", 100.0)
    await log.cleanup(days=0)

    rows = await log.get_recent()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# DB Tests: QueryTimer
# ---------------------------------------------------------------------------


@test("DB: QueryTimer install and auto-time")
async def test_timer_install():
    db = get_db()
    slow_log = SlowQueryLog(db, threshold_ms=0)
    await db.execute("DELETE FROM hyper_slow_queries")

    timer = QueryTimer(db, slow_log=slow_log, threshold_ms=0)
    timer.install()

    try:
        # Execute a query — should be auto-timed
        rows = await db.query("SELECT 1 as val")
        assert len(rows) == 1

        assert timer._total_queries >= 1
        assert timer._total_time_ms > 0

        # Slow log should have recorded it (threshold 0)
        log_rows = await slow_log.get_recent()
        assert len(log_rows) >= 1
    finally:
        timer.uninstall()


@test("DB: QueryTimer execute auto-times")
async def test_timer_execute():
    db = get_db()
    timer = QueryTimer(db, threshold_ms=999999)  # High threshold, no slow log writes
    timer.install()

    try:
        initial = timer._total_queries
        await db.execute("SELECT 1")
        assert timer._total_queries == initial + 1
    finally:
        timer.uninstall()


@test("DB: QueryTimer drain with no in-flight")
async def test_timer_drain_empty():
    db = get_db()
    timer = QueryTimer(db)
    timer.install()

    try:
        result = await timer.drain(timeout_seconds=1.0)
        assert result is True
        assert timer.in_flight == 0
    finally:
        timer.uninstall()


@test("DB: QueryTimer stats after queries")
async def test_timer_stats_after_queries():
    db = get_db()
    timer = QueryTimer(db, threshold_ms=999999)
    timer.install()

    try:
        for _ in range(5):
            await db.query("SELECT 1")

        stats = timer.get_stats()
        assert stats["total_queries"] >= 5
        assert stats["total_time_ms"] > 0
        assert stats["avg_query_ms"] > 0
        assert stats["in_flight"] == 0
    finally:
        timer.uninstall()


# ---------------------------------------------------------------------------
# DB Tests: PoolHealthChecker
# ---------------------------------------------------------------------------


@test("DB: PoolHealthChecker check succeeds")
async def test_health_check():
    db = get_db()
    checker = PoolHealthChecker(db, interval_seconds=60)

    result = await checker.check()
    assert result is True

    stats = checker.get_stats()
    assert stats["healthy"] is True
    assert stats["checks"] == 1
    assert stats["failures"] == 0


@test("DB: PoolHealthChecker multiple checks")
async def test_health_check_multiple():
    db = get_db()
    checker = PoolHealthChecker(db)

    for _ in range(3):
        await checker.check()

    stats = checker.get_stats()
    assert stats["checks"] == 3
    assert stats["last_check_ago_s"] is not None
    assert stats["last_check_ago_s"] < 5.0  # Just ran


@test("DB: PoolHealthChecker start/stop background")
async def test_health_check_background():
    db = get_db()
    checker = PoolHealthChecker(db, interval_seconds=0.1)

    checker.start()
    try:
        # Wait for the loop to REPORT a check instead of sleeping a multiple of
        # the interval and hoping one landed: on a loaded runner the interval is
        # a floor, not a promise, and the fixed sleep is what decided whether
        # this passed.
        deadline = time.monotonic() + 30.0
        while checker.get_stats()["checks"] < 1:
            assert time.monotonic() < deadline, (
                "background health checker ran no check within 30s "
                f"(interval={checker.interval_seconds}s)"
            )
            await asyncio.sleep(0.005)
    finally:
        checker.stop()

    # stop() must actually stop it. There is no state to wait for here — the
    # claim is that nothing further happens — so a window is the construct, and
    # the count cannot move once the loop is cancelled (a check in flight has
    # already been counted before its first await).
    settled = checker.get_stats()["checks"]
    # timing-window: bounded NEGATIVE — no further check may be counted after
    # stop(); several intervals of quiet is the only way to state "it stopped".
    await asyncio.sleep(0.5)  # 5x the interval
    assert checker.get_stats()["checks"] == settled, (
        f"checker kept running after stop(): {settled} -> "
        f"{checker.get_stats()['checks']}"
    )


@test("DB: Pool stats from native driver")
async def test_pool_stats():
    db = get_db()
    stats = db.pool_stats()
    assert isinstance(stats, dict)
    # pg.zig pool_stats should return something
    assert stats is not None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    all_tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            all_tests.append(obj)

    unit_tests = [t for t in all_tests if not t.__name__.startswith("DB:")]
    db_tests = [t for t in all_tests if t.__name__.startswith("DB:")]

    print("\n═══ Unit Tests ═══")
    for t in unit_tests:
        await t()

    print("\n═══ DB Integration Tests ═══")
    try:
        db = await setup_db()
        try:
            slow_log = SlowQueryLog(db, threshold_ms=50)
            await slow_log.ensure_table()
            for t in db_tests:
                await t()
        finally:
            await teardown_db(db)
    except Exception as e:
        print(f"\n  ⚠ Database connection failed ({e}), skipping integration tests")

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
