"""Tests for off-loop execution of blocking native DB round-trips.

`Database.query/execute/...` are `async def` but perform a synchronous
PostgreSQL round-trip. How they run depends on the ROLE of the event loop:

  * Thread-per-request / single-flow loops (HTTP worker loop, tests, scripts,
    startup, thread-mode WS) run the round-trip INLINE — optimal, and keeps
    the query on the thread that owns the pinned pool connection.
  * MULTIPLEXING loops (the shared WebSocket pool, later the HTTP reactor),
    flagged via `mark_loop_multiplexing`, OFFLOAD the round-trip to the bounded
    DB executor so one query can't stall the loop's other connections.

Covers:
  (a) inline on an unflagged loop — the op runs on the loop thread.
  (b) offload on a flagged loop — the op runs on a DIFFERENT thread.
  (c) non-stall — a slow offloaded op does NOT block the flagged loop; a
      co-scheduled pure-asyncio coroutine keeps making progress.
  (d) real query correctness on a flagged loop (result round-trips).
  (e) transaction integrity on a flagged loop — queries inside a transaction
      stay INLINE (on the BEGIN thread's pinned connection), so a ROLLBACK
      actually discards the write. If offload leaked a tx query onto another
      pooled connection, the write would survive the rollback.
  (f) connection budget — the offload workers are folded into the derived
      pool size so the executor never over-subscribes PostgreSQL.

# hyper-test: db_isolated

Usage:
    uv run hyper-test db_offload
"""

import asyncio
import inspect
import os
import sys
import threading
import time
import traceback

from hyperdjango.conf import get_setting
from hyperdjango.database import (
    Database,
    _derive_pool_size_from_thread_count,
    _run_db_blocking,
    db_offload_worker_count,
    mark_loop_multiplexing,
)

RESULTS = {"passed": 0, "failed": 0, "errors": []}


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


# ── loop-role helpers ──────────────────────────────────────────────────────


def _run_on_loop(coro_fn, *, multiplexing: bool):
    """Run coro_fn() to completion on a fresh loop (optionally flagged
    multiplexing) on a DEDICATED thread. Mirrors how the runtime owns
    single-flow vs shared loops, and lets these tests run their own loop even
    though the test harness already drives one on the main thread."""
    box: dict = {}

    def runner():
        loop = asyncio.new_event_loop()
        if multiplexing:
            mark_loop_multiplexing(loop)
        try:
            box["value"] = loop.run_until_complete(coro_fn())
        except BaseException as e:  # noqa: BLE001 — re-raised on the caller thread
            box["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=runner, name="db-offload-test-loop")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ── (a)/(b) inline vs offload thread identity ──────────────────────────────


@test("inline on an unflagged loop runs on the loop thread")
def test_inline_same_thread():
    async def body():
        loop_tid = threading.get_ident()
        op_tid = await _run_db_blocking(threading.get_ident)
        assert op_tid == loop_tid, (
            f"expected inline (same thread), got {op_tid} != {loop_tid}"
        )

    _run_on_loop(body, multiplexing=False)


@test("offload on a flagged loop runs on a different thread")
def test_offload_other_thread():
    async def body():
        loop_tid = threading.get_ident()
        op_tid = await _run_db_blocking(threading.get_ident)
        assert op_tid != loop_tid, (
            f"expected offload (other thread), got same tid {op_tid}"
        )

    _run_on_loop(body, multiplexing=True)


# ── (c) non-stall: a slow offloaded op must not block the flagged loop ──────


@test("a slow offloaded op does not stall the multiplexing loop")
def test_non_stall():
    async def body():
        ticks = 0

        async def ticker():
            nonlocal ticks
            # ~40 * 10ms = 400ms of wall clock; runs purely on the loop.
            for _ in range(40):
                await asyncio.sleep(0.01)
                ticks += 1

        async def slow_query():
            # 300ms blocking op offloaded to the DB executor.
            await _run_db_blocking(lambda: time.sleep(0.3))

        t0 = time.monotonic()
        await asyncio.gather(ticker(), slow_query())
        elapsed = time.monotonic() - t0
        # If the blocking op had run inline it would have frozen the loop and
        # the ticker could not have advanced during those 300ms. Require that
        # the ticker kept ticking throughout the slow op.
        assert ticks >= 25, f"loop stalled during offloaded op — only {ticks} ticks"
        # And the whole thing overlaps (< sum of 400ms + 300ms).
        assert elapsed < 0.6, f"no overlap — took {elapsed:.3f}s (expected concurrency)"

    _run_on_loop(body, multiplexing=True)


# ── (d)/(e) real DB behaviour on a flagged loop ─────────────────────────────

_DB_URL = os.environ.get("DATABASE_URL", "")
_TABLE = "test_db_offload_rows"


@test("real query round-trips correctly on a flagged loop")
def test_offload_real_query():
    async def body():
        db = Database(_DB_URL)
        await db.connect()
        try:
            row = await db.query_one("SELECT 42 AS n")
            assert row == {"n": 42}, row
        finally:
            await db.disconnect()

    _run_on_loop(body, multiplexing=True)


@test("transaction ROLLBACK discards the write on a flagged loop (tx stays inline)")
def test_transaction_integrity_on_flagged_loop():
    async def body():
        db = Database(_DB_URL)
        await db.connect()
        try:
            await db.execute(f"DROP TABLE IF EXISTS {_TABLE}")
            await db.execute(f"CREATE TABLE {_TABLE} (id INT PRIMARY KEY)")
            # Insert inside a transaction, then roll back. If a query inside the
            # transaction had been offloaded to a *different* pooled connection,
            # the INSERT would be a separate autocommit session and would
            # SURVIVE this rollback — corrupting isolation.
            try:
                async with db.transaction():
                    await db.execute(f"INSERT INTO {_TABLE} (id) VALUES (1)")
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
            count = await db.query_val(f"SELECT COUNT(*) FROM {_TABLE}")
            assert int(count) == 0, f"rollback did not discard write — count={count}"

            # Committed write survives.
            async with db.transaction():
                await db.execute(f"INSERT INTO {_TABLE} (id) VALUES (2)")
            count = await db.query_val(f"SELECT COUNT(*) FROM {_TABLE}")
            assert int(count) == 1, f"committed write missing — count={count}"
        finally:
            try:
                await db.execute(f"DROP TABLE IF EXISTS {_TABLE}")
            finally:
                await db.disconnect()

    _run_on_loop(body, multiplexing=True)


# ── (f) connection budget ───────────────────────────────────────────────────


@test("offload workers are folded into the derived pool size")
def test_budget_folds_offload_workers():
    thread_pool = int(get_setting("THREAD_POOL_SIZE", 24))
    offload = db_offload_worker_count()
    derived = _derive_pool_size_from_thread_count()
    # headroom is a fixed internal constant (8); assert the derived size leaves
    # room for BOTH headroom and the offload executor above the worker threads.
    assert derived >= thread_pool + offload, (
        f"derived pool {derived} < threads {thread_pool} + offload {offload}"
    )
    assert offload >= 1


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print(f"\nDB off-loop execution tests ({len(tests)} tests)")
    print("=" * 60)
    for t in tests:
        await t()
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
