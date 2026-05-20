"""Regression tests for free-threading races + transaction/resource bugs in
the hyperdjango DB layer.

Each test corresponds to an audited, empirically-proven defect and asserts the
FIXED behaviour. The original repro scripts live under the audit scratchpad
(proof_tx_cancel.py, proof_cursor_leak.py, proof_commit_swallow.py); these
tests reuse their approach so the fixes stay locked in.

Requires PostgreSQL running (the shared test database).
Run: uv run pytest tests/test_db/test_db_race_fixes.py -v
"""

import asyncio
import os
import threading

import pytest


def _dsn():
    user = os.environ.get("USER", "postgres")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    return f"postgresql://{user}:@localhost:5432/{dbname}"


# --------------------------------------------------------------------------
# CRITICAL #1 — on_commit callback list was process-shared while tx depth is
# thread-local. Under free-threading (GIL off) thread B's COMMIT fired thread
# A's not-yet-committed callback, and A's ROLLBACK discarded B's pending ones.
# Fix moved the pending list onto the thread-local. Each thread's callback must
# fire IFF its OWN transaction commits.
# --------------------------------------------------------------------------
def test_on_commit_callbacks_are_thread_isolated():
    from hyperdjango.database import Database

    N = 8  # keep <= pool size so all txns can be open simultaneously
    db = Database(_dsn(), max_size=N + 4)

    async def _connect():
        await db.connect()

    asyncio.run(_connect())

    fired: list[int] = []
    fired_lock = threading.Lock()
    # All workers reach the barrier while INSIDE their own open transaction —
    # maximises the interleaving the original race depended on.
    barrier = threading.Barrier(N)

    def worker(i: int):
        should_commit = i % 2 == 0

        def record(tag: int):
            with fired_lock:
                fired.append(tag)

        async def body():
            try:
                async with db.transaction():
                    db.on_commit(lambda i=i: record(i))
                    barrier.wait()  # all N threads now hold an open tx
                    if not should_commit:
                        raise RuntimeError("force rollback")
            except RuntimeError:
                pass

        asyncio.run(body())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    committed = {i for i in range(N) if i % 2 == 0}
    # Each committing thread fired exactly its own callback; no rolled-back
    # thread's callback fired, and none fired twice.
    assert sorted(fired) == sorted(committed), (
        f"expected {sorted(committed)}, got {sorted(fired)}"
    )

    asyncio.run(db.disconnect())


# --------------------------------------------------------------------------
# CRITICAL #2 (F8) — db.transaction() caught only `except Exception`, so an
# asyncio.CancelledError (client disconnect / timeout) skipped ROLLBACK. The
# cancelled writes stayed on the pinned connection and the NEXT transaction on
# that loop thread committed them silently. Fix: `except BaseException`.
# --------------------------------------------------------------------------
def test_cancelled_transaction_rolls_back():
    from hyperdjango.database import Database

    async def run():
        db = Database(_dsn(), max_size=4)
        await db.connect()
        await db.execute("DROP TABLE IF EXISTS tx_cancel_test")
        await db.execute("CREATE TABLE tx_cancel_test (id int)")

        fired: list[str] = []

        async def body():
            async with db.transaction():
                await db.execute("INSERT INTO tx_cancel_test VALUES (1)")
                db.on_commit(lambda: fired.append("leaked"))
                await asyncio.sleep(30)  # cancelled here, mid-transaction

        task = asyncio.create_task(body())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # A fresh, unrelated transaction on the same loop thread must begin
        # clean and must NOT commit the cancelled INSERT of 1.
        async with db.transaction():
            await db.execute("INSERT INTO tx_cancel_test VALUES (2)")

        rows = await db.query_tuples("SELECT id FROM tx_cancel_test ORDER BY id")
        ids = [r[0] for r in rows]
        assert ids == [2], f"cancelled write was not rolled back: {ids}"
        assert fired == [], "on_commit callback leaked across cancellation"

        await db.execute("DROP TABLE IF EXISTS tx_cancel_test")
        await db.disconnect()

    asyncio.run(run())


# --------------------------------------------------------------------------
# CRITICAL #3 (F2) — server_cursor() acquired a pinned connection then ran
# BEGIN + DECLARE with no try/finally. A failing DECLARE (bad SQL) leaked the
# connection (no owner object existed to release it), draining the pool. Fix
# wraps BEGIN+DECLARE, releasing the connection on error.
# --------------------------------------------------------------------------
def test_bad_server_cursor_does_not_leak_pool():
    from hyperdjango.database import Database

    async def run():
        db = Database(_dsn(), max_size=2)
        await db.connect()

        # Exhaust more attempts than the pool has slots. Without the fix, the
        # first two would leak both slots and later acquires would block/fail.
        for _ in range(4):
            with pytest.raises(Exception):
                await db.server_cursor("SELECT * FROM no_such_table_xyzzy")

        # Pool is intact: a valid server_cursor still acquires promptly.
        cur = await asyncio.wait_for(db.server_cursor("SELECT 1"), timeout=5)
        await cur.close()

        await db.disconnect()

    asyncio.run(run())


# --------------------------------------------------------------------------
# CRITICAL #4 (F14) — PgZigConnection.commit() wrapped COMMIT in
# `with suppress(RuntimeError)` then reported success. A COMMIT that fails at
# the server (DEFERRABLE INITIALLY DEFERRED constraint firing at commit) was
# swallowed: PG rolled back, the write was lost, the caller saw success. Under
# Django ATOMIC_REQUESTS that is silent data loss behind a 2xx. Fix: let the
# error propagate (classified) so Django surfaces a 500.
# --------------------------------------------------------------------------
def test_failed_commit_raises_not_swallowed(db_pool):
    from hyperdjango.db.pgzig_connection import IntegrityError, PgZigConnection

    db_pool.execute("DROP TABLE IF EXISTS commit_fail_test")
    db_pool.execute(
        "CREATE TABLE commit_fail_test ("
        "  id int,"
        "  CONSTRAINT commit_fail_uq UNIQUE (id) DEFERRABLE INITIALLY DEFERRED"
        ")"
    )
    try:
        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost",
            port=5432,
            dbname=os.environ.get("PGDATABASE", "hyperdjango_test"),
            user=user,
        )
        conn.connect()
        conn.autocommit = False  # like Django ATOMIC_REQUESTS

        cur = conn.cursor()
        cur.execute("INSERT INTO commit_fail_test VALUES (1)")
        cur.execute("INSERT INTO commit_fail_test VALUES (1)")  # deferred dup

        # The deferred UNIQUE violation fires at COMMIT. It must surface as a
        # classified IntegrityError, NOT be swallowed into a fake success.
        with pytest.raises(IntegrityError):
            conn.commit()
        conn.close()

        rows = db_pool.query("SELECT count(*) FROM commit_fail_test")
        assert rows[0][0] == 0, "rows present after a COMMIT that PG rolled back"
    finally:
        db_pool.execute("DROP TABLE IF EXISTS commit_fail_test")


# --------------------------------------------------------------------------
# MEDIUM #5 — _db_offload_executor() used @functools.cache, whose factory is
# NOT atomic under free-threading: racing first callers each built a
# ThreadPoolExecutor (proven: 14 executors, 14x the DB-connection budget). Fix
# uses double-checked locking so exactly one is created.
# --------------------------------------------------------------------------
def test_offload_executor_created_once_under_race():
    import hyperdjango.database as dbmod

    # The offload executor is now a SafeLazy singleton (the audited DCL primitive).
    prev = dbmod._db_offload_executor_lazy.peek()
    dbmod._db_offload_executor_lazy.reset()  # force a fresh build for the race
    try:
        N = 32
        barrier = threading.Barrier(N)
        seen: list[int] = []
        seen_lock = threading.Lock()

        def worker():
            barrier.wait()  # release all callers into the factory together
            ex = dbmod._db_offload_executor()
            with seen_lock:
                seen.append(id(ex))

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(seen) == N
        assert len(set(seen)) == 1, (
            f"racing callers created {len(set(seen))} executors, expected 1"
        )
    finally:
        created = dbmod._db_offload_executor_lazy.peek()
        if created is not None and created is not prev:
            created.shutdown(wait=False)
        dbmod._db_offload_executor_instance = prev


# --------------------------------------------------------------------------
# CORRECTNESS #6 — transaction(isolation_level=...) primitive. The outermost
# BEGIN must open at the requested SQL isolation level (data-integrity primitive
# for corporate/financial workloads that need SERIALIZABLE/REPEATABLE READ).
# Levels come from a fixed allowlist (no injection), and the option is rejected
# on nested (savepoint) blocks where PostgreSQL cannot change the level.
# --------------------------------------------------------------------------
def test_transaction_isolation_level_applied_at_outermost_begin():
    from hyperdjango.database import Database

    db = Database(_dsn())

    async def body():
        await db.connect()
        try:
            for level, expected in (
                ("serializable", "serializable"),
                ("repeatable_read", "repeatable read"),
                ("read_committed", "read committed"),
            ):
                async with db.transaction(isolation_level=level):
                    got = await db.query_val("SHOW transaction_isolation")
                    assert got == expected, (
                        f"isolation_level={level!r} opened at {got!r}, want {expected!r}"
                    )
        finally:
            await db.disconnect()

    asyncio.run(body())


def test_transaction_isolation_level_rejects_unknown_level():
    from hyperdjango.database import Database

    db = Database(_dsn())

    async def body():
        await db.connect()
        try:
            # An unknown / injection-shaped value must never reach the BEGIN.
            with pytest.raises(ValueError):
                async with db.transaction(isolation_level="drop table users; --"):
                    pass
        finally:
            await db.disconnect()

    asyncio.run(body())


def test_isolation_level_rejected_on_nested_transaction():
    from hyperdjango.database import Database

    db = Database(_dsn())

    async def body():
        await db.connect()
        try:
            async with db.transaction():
                # Postgres cannot change the isolation level of an already-open
                # tx; asking for one on a nested (savepoint) block is an error.
                with pytest.raises(ValueError):
                    async with db.transaction(isolation_level="serializable"):
                        pass
        finally:
            await db.disconnect()

    asyncio.run(body())
