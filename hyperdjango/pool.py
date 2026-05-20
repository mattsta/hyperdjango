"""
Connection pool optimization — slow query log, health checks, graceful drain.

Enhances the Database class with production pool management:

- SlowQueryLog: Persistent UNLOGGED table for slow query tracking
- PoolHealthChecker: Periodic connection validation
- QueryTimer: Auto-wraps Database methods with timing + slow query detection
- Graceful drain: Wait for in-flight queries before shutdown

Usage:
    from hyperdjango.pool import SlowQueryLog, PoolHealthChecker, QueryTimer

    db = Database("postgres://localhost/mydb")
    await db.connect()

    # Slow query logging
    slow_log = SlowQueryLog(db, threshold_ms=100)
    await slow_log.ensure_table()

    # Auto-timing (wraps db.query/execute with timing)
    timer = QueryTimer(db, slow_log=slow_log, threshold_ms=100)
    timer.install()  # Patches db.query/execute to auto-time

    # Health checks
    checker = PoolHealthChecker(db, interval_seconds=30)
    await checker.check()  # Manual check
    checker.start()        # Background periodic checks

    # Graceful drain
    await timer.drain(timeout_seconds=30)  # Wait for in-flight queries
"""

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass

from hyperdjango.conf import DEFAULT_SLOW_QUERY_THRESHOLD_MS, get_setting
from hyperdjango.logging import logger
from hyperdjango.performance import get_perf_middleware

# ---------------------------------------------------------------------------
# Slow Query Log (persistent, PostgreSQL UNLOGGED)
# ---------------------------------------------------------------------------

CREATE_SLOW_LOG_SQL = """
CREATE UNLOGGED TABLE IF NOT EXISTS hyper_slow_queries (
    id SERIAL PRIMARY KEY,
    sql_text TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    params_summary TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

CREATE_SLOW_LOG_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_slow_ts ON hyper_slow_queries (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_slow_dur ON hyper_slow_queries (duration_ms DESC)",
)


class SlowQueryLog:
    """Persistent slow query log backed by PostgreSQL UNLOGGED table.

    Records queries exceeding the threshold for offline analysis.
    UNLOGGED for fast writes — survives restarts but not crashes.
    """

    def __init__(self, db, threshold_ms: float = DEFAULT_SLOW_QUERY_THRESHOLD_MS):
        self.db = db
        self.threshold_ms = threshold_ms
        self._count = 0
        # record() runs on the concurrent query path; under the free-threaded
        # runtime a bare `self._count += 1` loses increments. Guard the counter
        # (and its reader) with a lock so the session count is exact.
        self._count_lock = threading.Lock()

    async def ensure_table(self):
        """Create the slow query log table."""
        try:
            await self.db.execute(CREATE_SLOW_LOG_SQL)
        # blind-except: UNLOGGED is unsupported on some PG deployments (replicas, certain managed services); fall back to a plain TABLE. A real connection/permission error resurfaces on the fallback execute.
        except Exception:
            await self.db.execute(
                CREATE_SLOW_LOG_SQL.replace("UNLOGGED TABLE", "TABLE")
            )
        for sql in CREATE_SLOW_LOG_INDEX:
            with contextlib.suppress(Exception):
                await self.db.execute(sql)

    async def record(self, sql: str, duration_ms: float, params: list | None = None):
        """Record a slow query if it exceeds the threshold."""
        if duration_ms < self.threshold_ms:
            return
        with self._count_lock:
            self._count += 1
        sql_len = int(get_setting("SLOW_QUERY_SQL_LENGTH"))
        params_len = int(get_setting("SLOW_QUERY_PARAMS_LENGTH"))
        params_summary = str(params)[:params_len] if params else None
        with contextlib.suppress(Exception):
            await self.db.execute(
                "INSERT INTO hyper_slow_queries (sql_text, duration_ms, params_summary) "
                "VALUES ($1, $2, $3)",
                sql[:sql_len],
                duration_ms,
                params_summary,
            )

    async def get_recent(self, limit: int = 50) -> list[dict[str, str | float | None]]:
        """Get recent slow queries."""
        rows = await self.db.query(
            "SELECT id, sql_text, duration_ms, params_summary, timestamp "
            "FROM hyper_slow_queries ORDER BY timestamp DESC LIMIT $1",
            limit,
        )
        return rows

    async def get_slowest(self, limit: int = 20) -> list[dict[str, str | float | None]]:
        """Get the slowest queries ever recorded."""
        rows = await self.db.query(
            "SELECT id, sql_text, duration_ms, params_summary, timestamp "
            "FROM hyper_slow_queries ORDER BY duration_ms DESC LIMIT $1",
            limit,
        )
        return rows

    async def get_stats(self) -> dict[str, int | float]:
        """Get aggregate slow query statistics."""
        row = await self.db.query_one(
            "SELECT COUNT(*) as total, "
            "AVG(duration_ms) as avg_ms, "
            "MAX(duration_ms) as max_ms, "
            "MIN(duration_ms) as min_ms "
            "FROM hyper_slow_queries"
        )
        if row is None:
            return {"total": 0, "avg_ms": 0, "max_ms": 0, "min_ms": 0}
        return row

    async def cleanup(self, days: int | None = None):
        if days is None:
            days = int(get_setting("SLOW_QUERY_RETENTION_DAYS"))
        """Delete slow query log entries older than N days."""
        await self.db.execute(
            "DELETE FROM hyper_slow_queries "
            "WHERE timestamp < NOW() - $1 * INTERVAL '1 day'",
            int(days),
        )

    @property
    def count(self) -> int:
        """Number of slow queries recorded in this session.

        Read without the lock: a single attribute load is coherent (the race
        the lock guards against is the read-modify-write ``+=`` in record(),
        not a lone read). Returns a possibly-just-stale but never-torn value.
        """
        return self._count


# ---------------------------------------------------------------------------
# Query Timer — auto-wraps Database with timing
# ---------------------------------------------------------------------------


class QueryTimer:
    """Auto-times Database query/execute calls and records slow queries.

    Patches the Database instance to wrap query()/execute() with timing.
    Records to both the in-memory PerformanceMiddleware and persistent SlowQueryLog.
    Tracks in-flight queries for graceful drain.
    """

    def __init__(
        self,
        db,
        slow_log: SlowQueryLog | None = None,
        threshold_ms: float = DEFAULT_SLOW_QUERY_THRESHOLD_MS,
    ):
        self.db = db
        self.slow_log = slow_log
        self.threshold_ms = threshold_ms
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._total_queries = 0
        self._total_time_ms = 0.0
        self._installed = False

    def install(self):
        """Patch the Database instance to auto-time queries."""
        if self._installed:
            return

        original_query = self.db.query
        original_execute = self.db.execute

        timer = self

        async def timed_query(sql, *args):
            with timer._in_flight_lock:
                timer._in_flight += 1
            start = time.perf_counter()
            try:
                result = await original_query(sql, *args)
                return result
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000
                with timer._in_flight_lock:
                    timer._in_flight -= 1
                    timer._total_queries += 1
                    timer._total_time_ms += elapsed_ms

                # Record to performance middleware
                perf = _get_perf()
                if perf:
                    perf.record_query(sql, elapsed_ms)

                # Record slow query
                if timer.slow_log and elapsed_ms >= timer.threshold_ms:
                    with contextlib.suppress(Exception):
                        await timer.slow_log.record(
                            sql, elapsed_ms, list(args) if args else None
                        )

        async def timed_execute(sql, *args):
            with timer._in_flight_lock:
                timer._in_flight += 1
            start = time.perf_counter()
            try:
                result = await original_execute(sql, *args)
                return result
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000
                with timer._in_flight_lock:
                    timer._in_flight -= 1
                    timer._total_queries += 1
                    timer._total_time_ms += elapsed_ms

                perf = _get_perf()
                if perf:
                    perf.record_query(sql, elapsed_ms)

                if timer.slow_log and elapsed_ms >= timer.threshold_ms:
                    with contextlib.suppress(Exception):
                        await timer.slow_log.record(
                            sql, elapsed_ms, list(args) if args else None
                        )

        self.db.query = timed_query
        self.db.execute = timed_execute
        self._installed = True

    def uninstall(self):
        """Remove timing patches (for testing)."""
        if not self._installed:
            return
        # Restore originals via the class methods
        self.db.query = type(self.db).query.__get__(self.db)
        self.db.execute = type(self.db).execute.__get__(self.db)
        self._installed = False

    @property
    def in_flight(self) -> int:
        """Number of queries currently executing."""
        with self._in_flight_lock:
            return self._in_flight

    async def drain(self, timeout_seconds: float = 30.0):
        """Wait for all in-flight queries to complete.

        Returns True if drained successfully, False if timeout.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.in_flight == 0:
                return True
            await asyncio.sleep(0.05)
        return False

    def get_stats(self) -> dict[str, int | float]:
        """Get query timing statistics."""
        with self._in_flight_lock:
            avg = (
                (self._total_time_ms / self._total_queries)
                if self._total_queries > 0
                else 0
            )
            return {
                "total_queries": self._total_queries,
                "total_time_ms": round(self._total_time_ms, 2),
                "avg_query_ms": round(avg, 2),
                "in_flight": self._in_flight,
                "threshold_ms": self.threshold_ms,
            }


# ---------------------------------------------------------------------------
# Pool Health Checker
# ---------------------------------------------------------------------------


class PoolHealthChecker:
    """Periodic connection pool health validation.

    Runs a lightweight query (SELECT 1) to verify connections are alive.
    Can be run manually or as a background task.
    """

    def __init__(self, db, interval_seconds: float = 30.0):
        self.db = db
        self.interval_seconds = interval_seconds
        self._last_check: float = 0
        self._last_result: bool = False
        self._check_count: int = 0
        self._fail_count: int = 0
        self._task: asyncio.Task | None = None
        # Monotonic owner token. stop() is synchronous and cannot await the
        # cancelled task's settle, so a rapid stop()->start() can leave the
        # previous loop briefly winding down. Each start() bumps this token and
        # the loop only acts while it still owns the current token — a lingering
        # old loop sees a stale token and exits, guaranteeing a single owner.
        self._generation: int = 0

    async def check(self) -> bool:
        """Run a health check. Returns True if healthy."""
        self._check_count += 1
        try:
            result = await self.db.query_val("SELECT 1")
            self._last_result = result == 1
            self._last_check = time.monotonic()
            return self._last_result
        # blind-except: health check probe — any query failure means the pool is unhealthy; it must record the failure and return False, never propagate.
        except Exception:
            self._fail_count += 1
            self._last_result = False
            self._last_check = time.monotonic()
            return False

    def start(self):
        """Start periodic background health checks."""
        if self._task is not None:
            return
        self._generation += 1
        self._task = asyncio.ensure_future(self._run_loop(self._generation))

    def stop(self):
        """Stop background health checks."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # Bump the token so any loop still winding down (the just-cancelled task
        # may not have observed its CancelledError yet) stops owning it.
        self._generation += 1

    async def _run_loop(self, generation: int):
        """Background health check loop."""
        while self._generation == generation:
            try:
                await asyncio.sleep(self.interval_seconds)
                if self._generation != generation:
                    break
                await self.check()
            except asyncio.CancelledError:
                break
            # blind-except: background health-check loop must survive a failed probe; count it and keep looping (cancellation already breaks above).
            except Exception:
                self._fail_count += 1

    def get_stats(self) -> dict[str, bool | int | float | None]:
        """Get health check statistics."""
        return {
            "healthy": self._last_result,
            "checks": self._check_count,
            "failures": self._fail_count,
            "last_check_ago_s": round(time.monotonic() - self._last_check, 1)
            if self._last_check
            else None,
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_perf():
    """Get the global PerformanceMiddleware if configured."""
    return get_perf_middleware()


# ---------------------------------------------------------------------------
# Pool Auto-Tuner
# ---------------------------------------------------------------------------


class PoolAutoTuner:
    """Dynamic connection pool sizing based on load metrics.

    Monitors pool utilization, query latency, and thread contention to
    automatically scale the pool between min_size and max_size. Samples
    every `check_interval` seconds and makes conservative scaling decisions
    with hysteresis to prevent flapping.

    Scaling signals:
    - **Scale up**: utilization > 80%, or available connections = 0, or
      thread-owned slots approaching limit (> 75% of 64)
    - **Scale down**: utilization < 30% sustained for `cooldown_periods`
      consecutive checks with no recent scale-up

    Usage:
        db = Database("postgres://localhost/mydb", min_size=2, max_size=20)
        await db.connect()

        tuner = PoolAutoTuner(db, check_interval=10)
        tuner.start()  # Background asyncio task

        # Later:
        tuner.stop()
        print(tuner.stats())
    """

    def __init__(
        self,
        db,
        check_interval: int = 10,
        scale_up_threshold: float = 0.8,
        scale_down_threshold: float = 0.3,
        cooldown_periods: int = 6,
        scale_step: int = 2,
    ):
        self._db = db
        self._check_interval = check_interval
        self._scale_up_threshold = scale_up_threshold
        self._scale_down_threshold = scale_down_threshold
        self._cooldown_periods = cooldown_periods
        self._scale_step = scale_step

        # State
        self._task: asyncio.Task | None = None
        self._running = False
        # Owner token — see PoolHealthChecker. stop() is sync and cannot await
        # the cancelled loop's settle; the token lets a lingering old loop
        # detect it is no longer the owner and exit, so a rapid stop()->start()
        # never runs two monitor loops concurrently.
        self._generation: int = 0
        self._samples: list[dict[str, int | float]] = []
        self._max_samples = 100
        self._scale_up_count = 0
        self._scale_down_count = 0
        self._consecutive_low = 0
        self._last_scale_time = 0.0
        self._current_target: int | None = None

    def start(self):
        """Start the auto-tuner background task."""
        if self._running:
            return
        self._running = True
        self._generation += 1
        self._task = asyncio.ensure_future(self._monitor_loop(self._generation))

    def stop(self):
        """Stop the auto-tuner."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # Bump the token so a loop still winding down stops owning it.
        self._generation += 1

    async def _monitor_loop(self, generation: int):
        """Background monitoring loop — samples pool metrics and adjusts."""
        while self._running and self._generation == generation:
            try:
                await asyncio.sleep(self._check_interval)
                if not self._running or self._generation != generation:
                    break
                await self._check_and_adjust()
            except asyncio.CancelledError:
                break
            # blind-except: background auto-tuner loop must survive a bad sampling/adjust cycle; log at debug and keep looping (cancellation already breaks above).
            except Exception as exc:
                logger.debug(
                    "Pool auto-tuner monitor cycle failed: {error}", error=str(exc)
                )

    async def _check_and_adjust(self):
        """Sample pool metrics and decide whether to scale."""
        pool_stats = self._db.pool_stats()

        total = pool_stats.get("total", 0)
        available = pool_stats.get("available", 0)
        in_use = pool_stats.get("in_use", 0)
        thread_owned = pool_stats.get("thread_owned", 0)

        if total == 0:
            return

        utilization = in_use / total
        thread_pressure = thread_owned / 64.0  # 64 thread-owned slots per pool

        sample = {
            "timestamp": time.monotonic(),
            "total": total,
            "available": available,
            "in_use": in_use,
            "thread_owned": thread_owned,
            "utilization": round(utilization, 3),
            "thread_pressure": round(thread_pressure, 3),
            "action": "hold",
        }

        # Scale-up decision
        if (
            utilization > self._scale_up_threshold
            or available == 0
            or thread_pressure > 0.75
        ):
            sample["action"] = "scale_up"
            self._scale_up_count += 1
            self._consecutive_low = 0
            # Note: actual pool resizing requires creating a new pool handle
            # in pg.zig. For now, we record the recommendation. Full dynamic
            # resizing would require pool.resize() in the Zig layer.

        # Scale-down decision (with cooldown hysteresis)
        elif utilization < self._scale_down_threshold:
            self._consecutive_low += 1
            if self._consecutive_low >= self._cooldown_periods:
                sample["action"] = "scale_down"
                self._scale_down_count += 1
                self._consecutive_low = 0
        else:
            self._consecutive_low = 0

        # Store sample
        self._samples.append(sample)
        if len(self._samples) > self._max_samples:
            self._samples = self._samples[-self._max_samples :]

    def stats(self) -> dict[str, int | float | list[dict[str, int | float]]]:
        """Get auto-tuner statistics."""
        recent = self._samples[-10:] if self._samples else []
        return {
            "running": self._running,
            "check_interval": self._check_interval,
            "total_samples": len(self._samples),
            "scale_up_recommendations": self._scale_up_count,
            "scale_down_recommendations": self._scale_down_count,
            "consecutive_low_utilization": self._consecutive_low,
            "recent_samples": recent,
        }

    def recommendation(self) -> str:
        """Get current scaling recommendation based on recent samples."""
        if not self._samples:
            return "insufficient_data"

        recent = self._samples[-3:]
        up_count = sum(1 for s in recent if s["action"] == "scale_up")
        down_count = sum(1 for s in recent if s["action"] == "scale_down")

        if up_count >= 2:
            return "scale_up"
        if down_count >= 2:
            return "scale_down"
        return "hold"

    @property
    def utilization_history(self) -> list[float]:
        """Return recent utilization values for trend analysis."""
        return [s["utilization"] for s in self._samples[-20:]]

    @property
    def is_saturated(self) -> bool:
        """Check if pool is currently saturated (needs immediate attention)."""
        if not self._samples:
            return False
        latest = self._samples[-1]
        return latest["available"] == 0 or latest["utilization"] > 0.95


# ---------------------------------------------------------------------------
# Pool Heartbeat — background connection health monitoring
# ---------------------------------------------------------------------------

HEARTBEAT_SQL = "SELECT 1"


@dataclass(slots=True, frozen=True)
class HeartbeatStats:
    """Immutable snapshot of heartbeat health metrics."""

    running: bool
    interval_seconds: float
    total_beats: int
    total_failures: int
    consecutive_failures: int
    last_beat_at: float | None
    last_latency_ms: float | None
    avg_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    p99_latency_ms: float
    healthy: bool
    uptime_ratio: float


class PoolHeartbeat:
    """Background connection health heartbeat with latency tracking.

    Periodically executes a lightweight query (SELECT 1) against the pool
    to detect dead connections, network partitions, and server restarts.
    Tracks latency percentiles and consecutive failure counts for alerting.

    Usage:
        heartbeat = PoolHeartbeat(db, interval_seconds=15)
        heartbeat.start()

        stats = heartbeat.stats()
        # HeartbeatStats(running=True, healthy=True, avg_latency_ms=0.3, ...)

        heartbeat.stop()

    Integration with telemetry:
        from hyperdjango.telemetry import Gauge
        pool_healthy = Gauge("hyperdjango_db_pool_healthy", "Pool health gauge.")
        # In your background task, pull heartbeat.stats() and call
        # `pool_healthy.set(1 if stats.healthy else 0)` — the native
        # gauge flows through PrometheusSink automatically.
    """

    def __init__(
        self,
        db: object,
        interval_seconds: float = 15.0,
        failure_threshold: int = 3,
        latency_window: int = 100,
    ):
        self._db = db
        self._interval = interval_seconds
        self._failure_threshold = failure_threshold
        self._latency_window = latency_window

        # State
        self._total_beats: int = 0
        self._total_failures: int = 0
        self._consecutive_failures: int = 0
        self._last_beat_at: float = 0.0
        self._last_latency_ms: float = 0.0
        self._latencies: list[float] = []
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._running = False
        # Owner token — see PoolHealthChecker. Guards against a rapid
        # stop()->start() briefly running two heartbeat loops while the
        # cancelled one is still winding down (stop() is sync, can't await it).
        self._generation: int = 0

    def start(self) -> None:
        """Start the background heartbeat loop."""
        if self._running:
            return
        self._running = True
        self._generation += 1
        self._task = asyncio.ensure_future(self._heartbeat_loop(self._generation))
        logger.info(
            "Pool heartbeat started (interval={interval}s, failure_threshold={threshold})",
            interval=self._interval,
            threshold=self._failure_threshold,
        )

    def stop(self) -> None:
        """Stop the background heartbeat loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # Bump the token so a loop still winding down stops owning it.
        self._generation += 1
        logger.info("Pool heartbeat stopped")

    async def beat(self) -> bool:
        """Execute a single heartbeat. Returns True if healthy."""
        start = time.perf_counter()
        try:
            result = await self._db.query_val(HEARTBEAT_SQL)
            latency_ms = (time.perf_counter() - start) * 1000
            healthy = result == 1
        # blind-except: heartbeat probe — any query failure means the pool is unhealthy; it is logged and recorded as a failed beat below, and must produce a health verdict rather than propagate.
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            healthy = False
            logger.warning(
                "Heartbeat failed: {error} (latency={latency:.1f}ms)",
                error=str(exc),
                latency=latency_ms,
            )

        with self._lock:
            self._total_beats += 1
            self._last_beat_at = time.monotonic()
            self._last_latency_ms = latency_ms
            self._latencies.append(latency_ms)
            if len(self._latencies) > self._latency_window:
                self._latencies = self._latencies[-self._latency_window :]

            if healthy:
                self._consecutive_failures = 0
            else:
                self._total_failures += 1
                self._consecutive_failures += 1
                if self._consecutive_failures == self._failure_threshold:
                    logger.error(
                        "Pool heartbeat: {n} consecutive failures — pool may be unhealthy",
                        n=self._consecutive_failures,
                    )

        return healthy

    def stats(self) -> HeartbeatStats:
        """Return an immutable snapshot of heartbeat metrics."""
        with self._lock:
            total = self._total_beats
            failures = self._total_failures
            latencies = list(self._latencies)
            return HeartbeatStats(
                running=self._running,
                interval_seconds=self._interval,
                total_beats=total,
                total_failures=failures,
                consecutive_failures=self._consecutive_failures,
                last_beat_at=self._last_beat_at or None,
                last_latency_ms=self._last_latency_ms if total else None,
                avg_latency_ms=sum(latencies) / len(latencies) if latencies else 0.0,
                min_latency_ms=min(latencies) if latencies else 0.0,
                max_latency_ms=max(latencies) if latencies else 0.0,
                p99_latency_ms=_percentile(latencies, 0.99) if latencies else 0.0,
                healthy=self._consecutive_failures < self._failure_threshold,
                uptime_ratio=(total - failures) / total if total else 1.0,
            )

    def get_stats(self) -> dict[str, object]:
        """Dict-based stats for metrics integration (PoolHealthChecker compat)."""
        s = self.stats()
        return {
            "healthy": s.healthy,
            "checks": s.total_beats,
            "failures": s.total_failures,
            "last_check_ago_s": round(time.monotonic() - s.last_beat_at, 1)
            if s.last_beat_at
            else None,
            "consecutive_failures": s.consecutive_failures,
            "avg_latency_ms": round(s.avg_latency_ms, 2),
            "p99_latency_ms": round(s.p99_latency_ms, 2),
            "uptime_ratio": round(s.uptime_ratio, 4),
        }

    async def _heartbeat_loop(self, generation: int) -> None:
        """Background loop: beat at interval, log state transitions."""
        was_healthy = True
        while self._running and self._generation == generation:
            try:
                await asyncio.sleep(self._interval)
                if not self._running or self._generation != generation:
                    break
                healthy = await self.beat()
                # Log state transitions
                if was_healthy and not healthy:
                    logger.warning("Pool heartbeat: transitioned to UNHEALTHY")
                elif not was_healthy and healthy:
                    logger.info("Pool heartbeat: recovered to HEALTHY")
                was_healthy = healthy
            except asyncio.CancelledError:
                break
            # blind-except: background heartbeat loop must survive a failed beat; logged and the loop continues (cancellation already breaks above).
            except Exception as exc:
                logger.error("Heartbeat loop error: {error}", error=str(exc))


def _percentile(data: list[float], p: float) -> float:
    """Compute the p-th percentile of a sorted copy of data."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[-1]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])
