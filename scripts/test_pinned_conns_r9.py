"""R9 — pinned-connection cancellation-leak regression (multiplexing loop).

Guards `Database._transaction_multiplexed`'s OUTERMOST acquire against a
cancellation leak: the dedicated pool connection is acquired by an op OFFLOADED
to the DB executor, which runs to completion even if the awaiting Task is
cancelled. The pre-fix code did a bare

    conn_handle = await _run_db_blocking(lambda: _db_conn_acquire(pool_handle))

so a cancellation delivered while awaiting abandoned a connection the executor
still pinned — the handle was lost and the connection never returned to the pool
(leak). The fix shields the acquire+BEGIN and, on cancellation, registers a
done-callback that rolls back and releases the connection once the shielded op
settles — so every completed acquire has a matching release.

This drives many transactions that are cancelled right at the acquire await and
asserts the pool's checked-out count returns to baseline (no leaked connection).

TWO DISTINCT INVARIANTS are guarded here (the `_stress_cancel_jitter` docstring
has the full story):

  * RELEASE-EXACTLY-ONCE: every ``transaction()`` releases its one pinned pool
    connection exactly once no matter where a cancellation lands. The inline
    error-path cleanup + settle-shield (see `Database._transaction_multiplexed`
    and `_run_db_settled`) guarantee this by construction.

  * NO THREAD-OWNED ACCUMULATION on the offload executor: a NON-transaction query
    on a multiplexing loop is offloaded to the DB executor. Those worker threads
    are marked so they acquire/release a pool connection PER OP rather than
    pinning a thread-owned connection for their lifetime (native
    ``claimThreadSlot`` / ``offload_worker`` in zig/src/db.zig). Without that
    mark, each executor thread that ran a plain query permanently held a
    connection; because the executor scales its threads lazily, a query landing
    on a freshly-spawned thread pushed pool ``in_use`` above a baseline captured
    earlier — the true cause of the rare `in_use base -> base+1` flake (it also
    reused one connection across unrelated tasks WITHOUT session reset, a
    cross-task state-leak hazard). Per-op release makes ``in_use`` an accurate
    leak signal and isolates each offloaded op.

NATIVE-REBUILD-DEPENDENT: exercises the compiled `_hyperdjango_native` pinned
path and needs a live PostgreSQL (DATABASE_URL). The pure-Zig slot-array race
regression lives in zig/src/db.zig
("pinned_slots: concurrent claim/get/free is race-free").

# hyper-test: db_isolated

Usage:
    uv run hyper-test pinned_conns_r9
    # or: DATABASE_URL=... uv run python scripts/test_pinned_conns_r9.py
"""

import asyncio
import inspect
import os
import sys
import threading
import traceback
from contextlib import contextmanager, suppress

from hyperdjango.database import Database, mark_loop_multiplexing

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                # The test bodies are SYNC (they own a dedicated thread+loop via
                # _run_on_multiplexing_loop); await the result only if it's a
                # coroutine, so a plain sync body doesn't `await None`.
                result = func()
                if inspect.isawaitable(result):
                    await result
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:  # noqa: BLE001
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


def _run_on_multiplexing_loop(coro_fn):
    """Run coro_fn() to completion on a fresh loop flagged MULTIPLEXING, on a
    dedicated thread (mirrors how the shared WS loop is owned)."""
    box: dict = {}

    def runner():
        loop = asyncio.new_event_loop()
        mark_loop_multiplexing(loop)
        try:
            box["value"] = loop.run_until_complete(coro_fn())
        except BaseException as e:  # noqa: BLE001 — re-raised on caller thread
            box["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=runner, name="pinned-r9-loop")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


_DB_URL = os.environ.get("DATABASE_URL", "")


async def _settle(loop_iters: int = 60):
    """Let the DB executor finish in-flight shielded acquires and let their
    done-callbacks (ROLLBACK + release) run on the loop."""
    for _ in range(loop_iters):
        await asyncio.sleep(0.01)


@contextmanager
def _trace_pinned_conns():
    """Count pinned-connection acquires/releases at the native boundary.

    ``_transaction_multiplexed`` looks these module globals up at call time, so
    swapping them here observes the real lifecycle without touching the
    framework. Yields a ``pending()`` callable — acquires that have not yet been
    released — which is the only sound "has the in-flight work finished?" signal
    available to a test: the pool's ``in_use`` gauge reads at baseline both
    BEFORE an acquire reaches the pool and AFTER it is released, so it cannot
    distinguish "not started yet" from "already done".
    """
    import hyperdjango.database as _dbmod

    lock = threading.Lock()
    counts = {"acquired": 0, "released": 0}
    real_acquire = _dbmod._db_conn_acquire
    real_release = _dbmod._db_conn_release

    def traced_acquire(ph):
        handle = real_acquire(ph)
        with lock:
            counts["acquired"] += 1
        return handle

    def traced_release(h):
        with lock:
            counts["released"] += 1
        return real_release(h)

    def pending() -> int:
        with lock:
            return counts["acquired"] - counts["released"]

    _dbmod._db_conn_acquire = traced_acquire
    _dbmod._db_conn_release = traced_release
    try:
        yield pending
    finally:
        _dbmod._db_conn_acquire = real_acquire
        _dbmod._db_conn_release = real_release


async def _settle_until_released(
    db, target: int, pending, timeout_s: float = 90.0
) -> None:
    """Wait (bounded) until every in-flight acquire has been released.

    ``pending`` answers "how many pool connections are checked out by work this
    test started and has not yet released" — the acquire/release trace, not the
    pool gauge.

    Polling ``in_use <= target`` ALONE is unsound, and cost a CI cycle to learn:
    cancelling a transaction at the acquire await leaves the acquire running on
    the DB executor, so at the moment the poll first looks, the work has not
    reached the pool yet. ``in_use`` is still at baseline, the poll returns
    immediately, the acquires then land, and the caller reads an ``in_use`` far
    ABOVE target and reports a leak that does not exist. The faster the machine
    the more likely the executor has already drained — which is exactly why this
    passed on a dev box and failed on a 2-core CI runner.

    ``pending == 0`` cannot be satisfied early: it counts acquires that HAVE
    happened, so it stays non-zero until each one's release actually runs. A
    genuine leak never drains it, and the caller's assertion still fires once the
    bound elapses.
    """
    for _ in range(int(timeout_s / 0.01)):
        if pending() == 0 and db.pool_stats()["in_use"] <= target:
            return
        await asyncio.sleep(0.01)


@test(
    "cancelling a multiplexed transaction at the acquire await leaks no pool connection"
)
def _cancel_at_acquire():
    async def body():
        db = Database(_DB_URL)
        await db.connect()
        try:
            # Baseline AFTER connect so any startup checkout is already counted.
            await _settle(5)
            base = db.pool_stats()["in_use"]

            iterations = 40
            with _trace_pinned_conns() as pending:
                for _ in range(iterations):

                    async def run_tx():
                        async with db.transaction():
                            await db.execute("SELECT 1")

                    task = asyncio.ensure_future(run_tx())
                    # One loop step: the task advances into
                    # `_transaction_multiplexed` and suspends on
                    # `await asyncio.shield(begin_fut)` after scheduling the
                    # acquire+BEGIN on the DB executor.
                    await asyncio.sleep(0)
                    # Deliver the cancellation exactly at that acquire await.
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task

                # Let every shielded acquire+BEGIN complete and its cleanup
                # callback release the connection back to the pool. Waits on the
                # acquire/release balance, which cannot read as "done" before the
                # executor has even started (see _settle_until_released).
                await _settle_until_released(db, base, pending)

            final = db.pool_stats()["in_use"]
            assert final <= base, (
                f"pinned-connection leak: in_use went {base} -> {final} after "
                f"{iterations} transactions cancelled at the acquire await "
                f"(each leaked connection stays checked out of the pool)"
            )
        finally:
            await db.disconnect()

    _run_on_multiplexing_loop(body)


@test(
    "a fully-cancelled multiplexed transaction commits nothing and releases the connection"
)
def _cancel_inside_body():
    async def body():
        db = Database(_DB_URL)
        await db.connect()
        table = "test_pinned_r9_rows"
        try:
            await db.execute(f"DROP TABLE IF EXISTS {table}")
            await db.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY)")
            await _settle(5)
            base = db.pool_stats()["in_use"]

            async def run_tx():
                async with db.transaction():
                    await db.execute(f"INSERT INTO {table} (id) VALUES (1)")
                    # Cancel mid-body: the write must roll back AND the pinned
                    # connection must be released by the finally arm.
                    await asyncio.sleep(3600)

            with _trace_pinned_conns() as pending:
                task = asyncio.ensure_future(run_tx())
                await asyncio.sleep(0.05)  # let BEGIN + INSERT land
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
                # Bounded poll for the finally arm's ROLLBACK + release (see
                # _settle_until_released) — the rollback completes as part of
                # the same cleanup, so the COUNT below is 0 once every acquire
                # has been released.
                await _settle_until_released(db, base, pending)

            count = await db.query_val(f"SELECT COUNT(*) FROM {table}")
            assert int(count) == 0, f"cancelled write survived — count={count}"
            final = db.pool_stats()["in_use"]
            assert final <= base, (
                f"pinned-connection leak on mid-body cancel: in_use {base} -> {final}"
            )
        finally:
            with suppress(Exception):
                await db.execute(f"DROP TABLE IF EXISTS {table}")
            await db.disconnect()

    _run_on_multiplexing_loop(body)


@test(
    "STRESS: jittered cancellation + executor scale-up releases every connection exactly once"
)
def _stress_cancel_jitter():
    """Amplified regression for the `in_use base -> base+1` flake.

    Two independent invariants are asserted at once, because the original flake
    was mis-attributed to the FIRST and actually caused by the SECOND:

    1. RELEASE-EXACTLY-ONCE under cancellation. Every ``transaction()`` acquires
       one pinned pool connection and must release it exactly once regardless of
       where the cancellation lands (acquire await, INSERT settle, or the body
       sleep). We wrap the native acquire/release to prove ``acquired ==
       released`` with no double-release and no handle left outstanding — across
       thousands of jittered cancellations under concurrent load. (This held even
       BEFORE the fix: the inline-cleanup + settle-shield design is correct.)

    2. NO THREAD-OWNED ACCUMULATION on the offload executor. A NON-transaction
       query on a multiplexing loop is offloaded to the DB executor, which used
       to let each executor thread pin a thread-owned pool connection for its
       lifetime (native `claimThreadSlot`, `should_release=false`). `pool_stats`
       counts those in ``in_use``. The executor scales its worker threads lazily,
       so a query landing on a freshly-spawned thread would raise ``in_use``
       above a ``base`` captured before that thread warmed — the real cause of
       the rare `base -> base+1`. The trailing fan-out below deliberately forces
       the executor to spin up all its workers AFTER ``base`` to reproduce it.
       The fix marks offload workers so they acquire/release per op; ``in_use``
       now returns to ``base`` once the loop is idle.

    Iteration count / timing: a few thousand cancellations at randomized offsets
    across the 0-4 ms window (covering acquire await, INSERT-in-flight, and the
    body sleep) plus a handful of saturating background transactions reliably
    exercised both paths; the trailing 8-wide query fan-out (repeated) is what
    forced the executor thread scale-up that surfaced the accounting leak.
    """
    import random

    import hyperdjango.database as _dbmod

    async def body():
        db = Database(_DB_URL)
        await db.connect()
        table = "test_pinned_r9_stress"

        # Track the pinned-connection lifecycle at the native boundary. These
        # module globals are looked up at call time inside
        # `_transaction_multiplexed`, so replacing them here instruments the real
        # acquire/release without touching the framework.
        outstanding: dict[int, int] = {}
        lock = threading.Lock()
        stats = {"double_release": 0, "acquired": 0, "released": 0}
        real_acquire = _dbmod._db_conn_acquire
        real_release = _dbmod._db_conn_release

        def traced_acquire(ph):
            h = real_acquire(ph)
            with lock:
                outstanding[h] = outstanding.get(h, 0) + 1
                stats["acquired"] += 1
            return h

        def traced_release(h):
            with lock:
                if outstanding.get(h, 0) <= 0:
                    stats["double_release"] += 1
                else:
                    outstanding[h] -= 1
                    if outstanding[h] == 0:
                        del outstanding[h]
                stats["released"] += 1
            return real_release(h)

        def _stress_pending() -> int:
            """Acquires this test made that have not been released yet — the
            same completion signal `_trace_pinned_conns` provides, read from
            this test's richer trace (which also tracks double-release)."""
            with lock:
                return stats["acquired"] - stats["released"]

        _dbmod._db_conn_acquire = traced_acquire
        _dbmod._db_conn_release = traced_release
        try:
            await db.execute(f"DROP TABLE IF EXISTS {table}")
            await db.execute(f"CREATE TABLE {table} (id SERIAL PRIMARY KEY)")
            await _settle(5)
            base = db.pool_stats()["in_use"]

            stop = {"v": False}

            async def bg():
                # Saturate the offload executor so cancellations land while ops
                # are genuinely in flight on their pinned connections.
                while not stop["v"]:
                    with suppress(Exception):
                        async with db.transaction():
                            await db.execute(f"INSERT INTO {table} DEFAULT VALUES")
                    await asyncio.sleep(0)

            bg_tasks = [asyncio.ensure_future(bg()) for _ in range(6)]

            iterations = 2000
            for _ in range(iterations):

                async def run_tx():
                    async with db.transaction():
                        await db.execute(f"INSERT INTO {table} DEFAULT VALUES")
                        await asyncio.sleep(3600)

                task = asyncio.ensure_future(run_tx())
                # Jitter across the acquire await, the INSERT settle, and the
                # body sleep so cancellations exercise every cleanup arm.
                await asyncio.sleep(random.uniform(0, 0.004))
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

            stop["v"] = True
            for t in bg_tasks:
                t.cancel()
            for t in bg_tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await t

            await _settle_until_released(db, base, _stress_pending)
            # Force the offload executor to spin up ALL its workers AFTER `base`:
            # each fresh worker running a NON-transaction query is exactly what
            # used to pin an extra thread-owned connection and push `in_use` past
            # `base`. With the fix every offloaded op releases its connection.
            for _ in range(20):
                await asyncio.gather(
                    *(db.query_val(f"SELECT COUNT(*) FROM {table}") for _ in range(8))
                )
            await _settle_until_released(db, base, _stress_pending)

            final = db.pool_stats()["in_use"]
            with lock:
                leaked = dict(outstanding)
                double_release = stats["double_release"]
                acquired = stats["acquired"]
                released = stats["released"]

            assert not leaked, f"pinned connection never released: {leaked}"
            assert double_release == 0, (
                f"pinned connection double-released ×{double_release}"
            )
            assert acquired == released, (
                f"acquire/release imbalance: {acquired} acquired, {released} released"
            )
            assert final <= base, (
                f"connection leak: in_use {base} -> {final} after {iterations} "
                f"jittered cancellations + executor scale-up (offloaded queries "
                f"must not permanently pin thread-owned connections)"
            )
        finally:
            _dbmod._db_conn_acquire = real_acquire
            _dbmod._db_conn_release = real_release
            with suppress(Exception):
                await db.execute(f"DROP TABLE IF EXISTS {table}")
            await db.disconnect()

    _run_on_multiplexing_loop(body)


async def main():
    if not _DB_URL:
        print("SKIP: DATABASE_URL not set (needs a live PostgreSQL)")
        return 0
    tests = [
        obj
        for _, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print(f"\nPinned-connection R9 regression ({len(tests)} tests)")
    print("=" * 60)
    for t in tests:
        await t()
    print("\n" + "=" * 60)
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
