#!/usr/bin/env python3
"""Concurrency-hardening round 13 — proves four fixes.

Run standalone (`python scripts/test_concurrency_hardening_r13.py`) or via the
runner (`uv run hyper-test concurrency_hardening_r13`).

Covers:
  A2#7  SlowQueryLog._count is exact under many concurrent increments.
  A5#6  A channel delivery failure is logged at >= WARNING (was DEBUG).
  A2#6  profiling / middleware-timeline request state is isolated per asyncio
        Task (ContextVar conversion), not shared via a thread-local.
  A4    Pool background loops hand off cleanly: a rapid stop()->start() never
        runs two loops concurrently, and stop() actually stops the loop.
"""

# hyper-test: unit

import asyncio
import threading

from hyperdjango.testkit import check, finish, run_main

# ---------------------------------------------------------------------------
# A2#7 — SlowQueryLog._count exact under concurrent increments
# ---------------------------------------------------------------------------


def test_slow_query_count_atomic():
    from hyperdjango.pool import SlowQueryLog

    class _FakeDB:
        async def execute(self, *args):
            return None

    log = SlowQueryLog(_FakeDB(), threshold_ms=0.0)

    N_THREADS = 16
    N_PER = 2000

    async def hammer():
        for _ in range(N_PER):
            # duration_ms (1.0) >= threshold (0.0) so every call increments.
            await log.record("SELECT 1", 1.0, None)

    def run():
        asyncio.run(hammer())

    threads = [threading.Thread(target=run) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = N_THREADS * N_PER
    check(
        f"SlowQueryLog._count exact under concurrency ({log.count}=={expected})",
        log.count == expected,
    )


# ---------------------------------------------------------------------------
# A5#6 — channel delivery failure logged at >= WARNING
# ---------------------------------------------------------------------------


def test_channel_failure_logged_at_warning():
    from hyperdjango.channels import Channel, Message, Subscription
    from hyperdjango.logging import logger

    captured: list[tuple[str, str]] = []
    cap_lock = threading.Lock()

    def sink(record, message):
        with cap_lock:
            captured.append((record["level"].name, str(message)))

    # enqueue=False -> synchronous delivery so records are captured in-line.
    hid = logger.add(sink, level="DEBUG", enqueue=False)
    try:
        ch = Channel(name="room", layer=object())

        def boom_sync(msg):
            raise ValueError("sync boom")

        async def boom_async(msg):
            raise ValueError("async boom")

        subs = [
            Subscription(id=1, callback=boom_sync, channel_name="room", is_async=False),
            Subscription(id=2, callback=boom_async, channel_name="room", is_async=True),
        ]
        msg = Message(channel="room", data={"x": 1})
        asyncio.run(ch._deliver_to(subs, msg))
        # Flush the logging queue so both records reach our sink before we read.
        logger.complete()
    finally:
        logger.remove(hid)

    with cap_lock:
        errs = [(lvl, m) for lvl, m in captured if "subscriber error" in m]

    check("channel delivery failures were logged", len(errs) >= 2)
    check(
        "every delivery-failure record is at WARNING (not DEBUG)",
        bool(errs) and all(lvl == "WARNING" for lvl, _ in errs),
    )


# ---------------------------------------------------------------------------
# A2#6 — profiling profile state isolated per asyncio Task
# ---------------------------------------------------------------------------


def test_profiling_contextvar_isolation():
    from hyperdjango.profiling import (
        end_profile,
        get_current_profile,
        start_profile,
    )

    results: dict[str, object] = {}
    barrier_a = asyncio.Event()
    barrier_b = asyncio.Event()

    async def worker(
        method: str, path: str, key: str, mine: asyncio.Event, other: asyncio.Event
    ):
        p = start_profile(method=method, path=path)
        # Signal we've set our own, then wait for the other Task to set theirs.
        mine.set()
        await other.wait()
        # A thread-local would now show the LAST writer's profile; a ContextVar
        # (copied per Task) still shows our own.
        cur = get_current_profile()
        results[key] = (cur is p, cur.method if cur is not None else None)
        end_profile()

    async def main():
        await asyncio.gather(
            worker("A", "/a", "a", barrier_a, barrier_b),
            worker("B", "/b", "b", barrier_b, barrier_a),
        )

    asyncio.run(main())

    check(
        "profiling: Task A sees its own profile",
        results.get("a") == (True, "A"),
    )
    check(
        "profiling: Task B sees its own profile",
        results.get("b") == (True, "B"),
    )
    check("profiling: no profile leaks across Tasks", get_current_profile() is None)


# ---------------------------------------------------------------------------
# A2#6 — middleware timeline isolated per asyncio Task
# ---------------------------------------------------------------------------


def test_timeline_contextvar_isolation():
    from hyperdjango.standalone_middleware import (
        MiddlewareStack,
        get_current_timeline,
    )

    ready_a = asyncio.Event()
    ready_b = asyncio.Event()

    def make_handler(mine: asyncio.Event, other: asyncio.Event):
        async def handler(request):
            mine.set()
            await other.wait()
            # Return the timeline THIS Task observes.
            return get_current_timeline()

        return handler

    async def main():
        stack_a = MiddlewareStack(instrument=True)
        stack_b = MiddlewareStack(instrument=True)
        entry_a = stack_a.wrap(make_handler(ready_a, ready_b))
        entry_b = stack_b.wrap(make_handler(ready_b, ready_a))
        return await asyncio.gather(entry_a(object()), entry_b(object()))

    tl_a, tl_b = asyncio.run(main())

    check("timeline: Task A saw a timeline", tl_a is not None)
    check("timeline: Task B saw a timeline", tl_b is not None)
    check(
        "timeline: Tasks saw distinct per-Task timelines (no cross-Task bleed)",
        tl_a is not None and tl_b is not None and tl_a is not tl_b,
    )
    # Post-request readability is preserved (thread-local mirror): after the
    # request the timeline is still reachable, matching current behavior.
    check(
        "timeline: remains readable after the request (behavior preserved)",
        get_current_timeline() is not None,
    )


# ---------------------------------------------------------------------------
# A4 — pool background-loop single-owner handoff
# ---------------------------------------------------------------------------


class _ConcurrencyProbe:
    """Records the peak number of overlapping query_val() calls."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._lock = threading.Lock()

    async def query_val(self, sql):
        with self._lock:
            self.active += 1
            self.calls += 1
            if self.active > self.max_active:
                self.max_active = self.active
        try:
            await asyncio.sleep(0.005)
        finally:
            with self._lock:
                self.active -= 1
        return 1


def test_health_checker_single_owner():
    from hyperdjango.pool import PoolHealthChecker

    async def main():
        probe = _ConcurrencyProbe()
        hc = PoolHealthChecker(probe, interval_seconds=0.001)
        hc.start()
        # Rapid stop()->start() while loops may be mid-flight in query_val.
        for _ in range(8):
            await asyncio.sleep(0.002)
            hc.stop()
            hc.start()
        await asyncio.sleep(0.05)
        hc.stop()
        # Let any cancelled loop settle, then confirm it stays stopped.
        await asyncio.sleep(0.02)
        calls_after_stop = probe.calls
        await asyncio.sleep(0.03)
        return probe, calls_after_stop

    probe, calls_after_stop = asyncio.run(main())

    check(f"health loop actually ran (calls={probe.calls})", probe.calls > 0)
    check(
        f"health loop: never two loops concurrently (max_active={probe.max_active})",
        probe.max_active <= 1,
    )
    check(
        "health loop: stop() actually stops the loop",
        probe.calls == calls_after_stop,
    )


def test_generation_guard_present():
    """stop()/start() bump the owner token on all three loop classes so a
    lingering old loop detects it no longer owns the token and exits."""
    from hyperdjango.pool import PoolAutoTuner, PoolHealthChecker, PoolHeartbeat

    async def main():
        ok = True
        for cls, kwargs in (
            (PoolHealthChecker, {"interval_seconds": 100.0}),
            (PoolAutoTuner, {"check_interval": 100}),
            (PoolHeartbeat, {"interval_seconds": 100.0}),
        ):
            obj = cls(object(), **kwargs)
            g0 = obj._generation
            obj.start()
            g1 = obj._generation
            obj.stop()
            g2 = obj._generation
            obj.start()
            g3 = obj._generation
            obj.stop()
            # Each start() and each stop() must advance the token.
            ok = ok and g0 < g1 < g2 < g3
        return ok

    ok = asyncio.run(main())
    check("pool loops: start()/stop() advance the owner token", ok)


def main() -> bool:
    test_slow_query_count_atomic()
    test_channel_failure_logged_at_warning()
    test_profiling_contextvar_isolation()
    test_timeline_contextvar_isolation()
    test_health_checker_single_owner()
    test_generation_guard_present()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
