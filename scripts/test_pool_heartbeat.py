"""Tests for PoolHeartbeat — background connection health monitoring.

# hyper-test: unit

Covers:
  - Heartbeat lifecycle (start/stop)
  - Single beat execution and latency tracking
  - Consecutive failure detection and threshold alerting
  - Latency percentile computation (avg, min, max, p99)
  - Stats snapshot immutability (HeartbeatStats dataclass)
  - State transition logging (healthy→unhealthy→recovered)
  - Uptime ratio computation
  - Latency window rotation (bounded buffer)
  - get_stats() dict compatibility with metrics/PoolHealthChecker
  - Thread safety under concurrent beats
  - Edge cases (no beats, single beat, all failures)
"""

import asyncio
import os
import threading

_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


# ---------------------------------------------------------------------------
# Mock Database
# ---------------------------------------------------------------------------


class MockDB:
    """Mock Database that tracks query_val calls."""

    def __init__(self, fail_after: int = -1, latency_ms: float = 0.1):
        self.call_count = 0
        self.fail_after = fail_after  # -1 = never fail
        self.latency_ms = latency_ms
        self._fail_once_at: set[int] = set()
        self._error_type: type = RuntimeError
        self._error_msg: str = "connection reset"

    async def query_val(self, sql: str) -> int:
        self.call_count += 1
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.call_count in self._fail_once_at:
            raise self._error_type(self._error_msg)
        if self.fail_after >= 0 and self.call_count > self.fail_after:
            raise self._error_type(self._error_msg)
        return 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_initial_state():
    """HeartbeatStats defaults before any beats."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB()
    hb = PoolHeartbeat(db, interval_seconds=1.0)

    check("not running initially", not hb._running)
    check("no task", hb._task is None)
    check("zero beats", hb._total_beats == 0)
    check("zero failures", hb._total_failures == 0)

    stats = hb.stats()
    check("stats.running is False", stats.running is False)
    check("stats.healthy is True (no failures)", stats.healthy is True)
    check("stats.total_beats is 0", stats.total_beats == 0)
    check("stats.uptime_ratio is 1.0", stats.uptime_ratio == 1.0)
    check("stats.last_beat_at is None", stats.last_beat_at is None)
    check("stats.last_latency_ms is None", stats.last_latency_ms is None)
    check("stats.avg_latency_ms is 0", stats.avg_latency_ms == 0.0)


def test_single_beat():
    """A single successful beat updates all counters."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB(latency_ms=0.5)
    hb = PoolHeartbeat(db)

    result = asyncio.run(hb.beat())
    check("beat returns True", result is True)
    check("total_beats is 1", hb._total_beats == 1)
    check("total_failures is 0", hb._total_failures == 0)
    check("consecutive_failures is 0", hb._consecutive_failures == 0)
    check("db called once", db.call_count == 1)

    stats = hb.stats()
    check("stats.total_beats is 1", stats.total_beats == 1)
    check("stats.healthy is True", stats.healthy is True)
    check("stats.last_latency_ms > 0", stats.last_latency_ms > 0)
    check("stats.avg_latency_ms > 0", stats.avg_latency_ms > 0)
    check("stats.min_latency_ms > 0", stats.min_latency_ms > 0)
    check("stats.max_latency_ms > 0", stats.max_latency_ms > 0)
    check("stats.p99_latency_ms > 0", stats.p99_latency_ms > 0)


def test_failed_beat():
    """A failed beat increments failure counters."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB(fail_after=0)  # All calls fail
    hb = PoolHeartbeat(db)

    result = asyncio.run(hb.beat())
    check("beat returns False", result is False)
    check("total_failures is 1", hb._total_failures == 1)
    check("consecutive_failures is 1", hb._consecutive_failures == 1)

    stats = hb.stats()
    check("stats.healthy (1 < threshold 3)", stats.healthy is True)
    check("stats.uptime_ratio is 0", stats.uptime_ratio == 0.0)


def test_consecutive_failure_threshold():
    """After N consecutive failures, healthy becomes False."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB(fail_after=0)
    hb = PoolHeartbeat(db, failure_threshold=3)

    for i in range(3):
        asyncio.run(hb.beat())

    stats = hb.stats()
    check("3 consecutive failures", stats.consecutive_failures == 3)
    check("unhealthy after threshold", stats.healthy is False)
    check("total_failures is 3", stats.total_failures == 3)


def test_recovery_resets_consecutive():
    """A successful beat after failures resets consecutive counter."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB()
    db._fail_once_at = {1, 2}  # First two fail, third succeeds
    hb = PoolHeartbeat(db, failure_threshold=3)

    asyncio.run(hb.beat())  # fail
    asyncio.run(hb.beat())  # fail
    check("consecutive is 2", hb._consecutive_failures == 2)

    asyncio.run(hb.beat())  # success
    check("consecutive reset to 0", hb._consecutive_failures == 0)
    check("total_failures still 2", hb._total_failures == 2)
    check("healthy after recovery", hb.stats().healthy is True)


def test_latency_window_rotation():
    """Latency buffer stays bounded at latency_window size."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB(latency_ms=0)
    hb = PoolHeartbeat(db, latency_window=10)

    for _ in range(25):
        asyncio.run(hb.beat())

    check("total_beats is 25", hb._total_beats == 25)
    check("latency buffer bounded", len(hb._latencies) == 10)


def test_percentile_computation():
    """P99 computation produces correct values."""
    from hyperdjango.pool import _percentile

    data = list(range(1, 101))  # 1..100
    check("p99 of 1-100 is ~99.01", 98 < _percentile(data, 0.99) <= 100)
    check("p50 of 1-100 is ~50.5", 49 < _percentile(data, 0.50) < 52)
    check("p0 of 1-100 is 1", _percentile(data, 0.0) == 1)
    check("empty returns 0", _percentile([], 0.99) == 0.0)
    check("single value", _percentile([42.0], 0.99) == 42.0)


def test_stats_snapshot_immutability():
    """HeartbeatStats is a frozen dataclass — immutable after creation."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB()
    hb = PoolHeartbeat(db)
    asyncio.run(hb.beat())

    stats = hb.stats()
    try:
        stats.total_beats = 999
        check("HeartbeatStats is mutable (bad)", False)
    except AttributeError:
        check("HeartbeatStats is frozen", True)


def test_get_stats_dict_compat():
    """get_stats() returns dict compatible with PoolHealthChecker/metrics."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB()
    hb = PoolHeartbeat(db)
    asyncio.run(hb.beat())

    d = hb.get_stats()
    check("dict has 'healthy'", "healthy" in d)
    check("dict has 'checks'", "checks" in d)
    check("dict has 'failures'", "failures" in d)
    check("dict has 'last_check_ago_s'", "last_check_ago_s" in d)
    check("dict has 'consecutive_failures'", "consecutive_failures" in d)
    check("dict has 'avg_latency_ms'", "avg_latency_ms" in d)
    check("dict has 'p99_latency_ms'", "p99_latency_ms" in d)
    check("dict has 'uptime_ratio'", "uptime_ratio" in d)
    check("healthy is True", d["healthy"] is True)
    check("checks is 1", d["checks"] == 1)


def test_uptime_ratio():
    """Uptime ratio reflects success/failure distribution."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB()
    db._fail_once_at = {2, 4}
    hb = PoolHeartbeat(db)

    for _ in range(5):
        asyncio.run(hb.beat())

    stats = hb.stats()
    check("5 beats total", stats.total_beats == 5)
    check("2 failures", stats.total_failures == 2)
    check(
        "uptime is 0.6",
        abs(stats.uptime_ratio - 0.6) < 0.01,
        f"got {stats.uptime_ratio}",
    )


def test_start_stop_lifecycle():
    """Start/stop correctly manage the background task."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB()
    hb = PoolHeartbeat(db, interval_seconds=0.05)

    async def run():
        hb.start()
        check("running after start", hb._running is True)
        check("task exists", hb._task is not None)

        # Let beats happen - longer sleep under parallel load
        await asyncio.sleep(1.0 if _PARALLEL else 0.2)

        hb.stop()
        check("not running after stop", hb._running is False)
        check("task cleared", hb._task is None)
        _min_beats = 1 if _PARALLEL else 2
        check("beats happened", hb._total_beats >= _min_beats, f"got {hb._total_beats}")

    asyncio.run(run())


def test_start_idempotent():
    """Calling start() twice doesn't create duplicate tasks."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB()
    hb = PoolHeartbeat(db, interval_seconds=60)

    async def run():
        hb.start()
        task1 = hb._task
        hb.start()  # Second call
        task2 = hb._task
        check("same task", task1 is task2)
        hb.stop()

    asyncio.run(run())


def test_thread_safety():
    """Concurrent beats from multiple threads don't corrupt state."""
    from hyperdjango.pool import PoolHeartbeat

    db = MockDB(latency_ms=0)
    hb = PoolHeartbeat(db, latency_window=200)

    errors: list[str] = []

    def beat_n(n: int):
        loop = asyncio.new_event_loop()
        try:
            for _ in range(n):
                loop.run_until_complete(hb.beat())
        except Exception as e:
            errors.append(str(e))
        finally:
            loop.close()

    threads = [threading.Thread(target=beat_n, args=(20,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no errors", len(errors) == 0, f"errors: {errors}")
    check("100 total beats", hb._total_beats == 100, f"got {hb._total_beats}")
    check("latencies bounded", len(hb._latencies) <= 200)


def test_heartbeat_stats_dataclass():
    """HeartbeatStats has all expected fields with correct types."""
    from hyperdjango.pool import HeartbeatStats

    stats = HeartbeatStats(
        running=True,
        interval_seconds=15.0,
        total_beats=100,
        total_failures=5,
        consecutive_failures=0,
        last_beat_at=1000.0,
        last_latency_ms=0.3,
        avg_latency_ms=0.25,
        min_latency_ms=0.1,
        max_latency_ms=2.5,
        p99_latency_ms=1.8,
        healthy=True,
        uptime_ratio=0.95,
    )
    check("running", stats.running is True)
    check("interval", stats.interval_seconds == 15.0)
    check("total_beats", stats.total_beats == 100)
    check("total_failures", stats.total_failures == 5)
    check("consecutive_failures", stats.consecutive_failures == 0)
    check("healthy", stats.healthy is True)
    check("uptime_ratio", stats.uptime_ratio == 0.95)
    check("p99", stats.p99_latency_ms == 1.8)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_initial_state()
    test_single_beat()
    test_failed_beat()
    test_consecutive_failure_threshold()
    test_recovery_resets_consecutive()
    test_latency_window_rotation()
    test_percentile_computation()
    test_stats_snapshot_immutability()
    test_get_stats_dict_compat()
    test_uptime_ratio()
    test_start_stop_lifecycle()
    test_start_idempotent()
    test_thread_safety()
    test_heartbeat_stats_dataclass()

    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
