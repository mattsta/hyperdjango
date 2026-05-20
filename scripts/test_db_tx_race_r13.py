#!/usr/bin/env python3
# hyper-test: unit
"""Regression tests for the task-scoped transaction pinned-connection race (r13).

Covers two confirmed bugs in hyperdjango/database.py, both specific to a
MULTIPLEXING event loop (shared WS pool / reactor) where many coroutines share
one loop thread and each DB round-trip is offloaded to a SEPARATE executor
thread.

#1 (CRITICAL — free-threading data race on ONE raw pg connection).
``transaction()`` on a multiplexing loop pins a dedicated pg connection and
stores its state in a ContextVar. asyncio.Task copies the context, so a CHILD
task spawned inside ``async with db.transaction():`` (e.g. via ``asyncio.gather``)
inherits the SAME state and routes to the SAME pinned connection. Each op runs on
a different executor thread → two commands in flight on one connection → wire
desync under free-threading. The fix adds a per-transaction ``asyncio.Lock`` so
every op on the pinned connection is mutually exclusive. This test proves the
SERIALIZATION contract without a live DB: an instrumented fake pinned-op records
max concurrent entry per handle. Inside a task-scoped transaction, two gathered
queries must observe max-concurrency == 1 on the pinned handle; OUTSIDE a
transaction, plain queries must still run concurrently (max >= 2) — proving the
lock is scoped to the transaction path and does not serialize normal traffic.

#2 (transactional correctness). ``copy_from`` / ``copy_to`` / ``server_cursor``
previously ignored the active transaction and ran on a SEPARATE autocommit
connection. The fix routes them onto the transaction's own connection: copy_* on
the raw pinned slot (``state.conn_handle``), the server cursor on the effective
handle (``state.pinned_handle``) with no BEGIN/COMMIT and no connection release.
This test asserts the exact handle each uses inside a task-scoped transaction.

Pure test — no DB, no network. The native ``_db_*`` entry points are replaced
with instrumented fakes that record (handle, sql) and simulate a round-trip
window so genuine cross-task concurrency is observable.
"""

import asyncio
import contextlib
import sys
import threading
import time

import hyperdjango.database as dbmod
from hyperdjango.database import Database, mark_loop_multiplexing

# Deterministic raw slot handed out by the fake _db_conn_acquire, so the test
# can assert the exact pinned-connection handles the code routes to.
_RAW_SLOT = 7
_PINNED = -(_RAW_SLOT + 2)  # encoded handle used by _db_query / _db_execute (-9)

passed = 0
failed = 0

# When set to a threading.Barrier(2), the query/execute fakes rendezvous on it
# INSIDE their recorded critical section instead of sleeping — so two ops that
# are ALLOWED to overlap are DETERMINISTICALLY caught overlapping (no reliance
# on wall-clock scheduling, which is flaky under full-suite CPU contention).
# Left None for the serialization scenario, whose max==1 is lock-enforced (a
# barrier there would deadlock — only one op can hold the tx lock at a time).
_rendezvous: threading.Barrier | None = None


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} :: {detail}")


class Tracker:
    """Thread-safe recorder of concurrent native-op entry, per handle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active: dict[int, int] = {}
        self.max: dict[int, int] = {}
        self.calls: list[tuple[str, int, str]] = []  # (kind, handle, sql)
        self.acquires = 0

    def enter(self, kind: str, handle: int, sql: str) -> None:
        with self._lock:
            self.calls.append((kind, handle, sql))
            n = self.active.get(handle, 0) + 1
            self.active[handle] = n
            if n > self.max.get(handle, 0):
                self.max[handle] = n

    def leave(self, handle: int) -> None:
        with self._lock:
            self.active[handle] -= 1


# The active tracker (swapped per test).
_tracker: Tracker


def _install_fakes(window: float = 0.02) -> None:
    """Replace native DB entry points on the module with instrumented fakes.

    Every op sleeps ``window`` seconds INSIDE its recorded critical section so
    that two ops allowed to overlap are actually caught overlapping.
    """

    def fake_conn_acquire(pool_handle):
        _tracker.acquires += 1
        return _RAW_SLOT

    def fake_conn_release(handle):
        return None

    def _hold():
        # Rendezvous when armed (deterministic overlap), else sleep a window.
        if _rendezvous is not None:
            with contextlib.suppress(threading.BrokenBarrierError):
                _rendezvous.wait(timeout=5.0)
        else:
            time.sleep(window)

    def fake_execute(handle, sql, params):
        _tracker.enter("execute", handle, sql)
        _hold()
        _tracker.leave(handle)
        return 0

    def fake_query(handle, sql, params):
        _tracker.enter("query", handle, sql)
        _hold()
        _tracker.leave(handle)
        return []

    def fake_conn_execute(handle, sql, params):
        _tracker.enter("conn_execute", handle, sql)
        time.sleep(window)
        _tracker.leave(handle)
        return 0

    def fake_copy_from(handle, sql, rows):
        _tracker.enter("copy_from", handle, sql)
        time.sleep(window)
        _tracker.leave(handle)
        return len(rows)

    def fake_copy_to(handle, sql):
        _tracker.enter("copy_to", handle, sql)
        time.sleep(window)
        _tracker.leave(handle)
        return []

    def fake_get_last_columns():
        return []

    dbmod._db_conn_acquire = fake_conn_acquire
    dbmod._db_conn_release = fake_conn_release
    dbmod._db_execute = fake_execute
    dbmod._db_query = fake_query
    dbmod._db_conn_execute = fake_conn_execute
    dbmod._db_copy_from = fake_copy_from
    dbmod._db_copy_to = fake_copy_to
    dbmod._db_get_last_columns = fake_get_last_columns


def _make_db() -> Database:
    """Build a Database without a real pool (bypasses __init__/connect)."""
    db = Database.__new__(Database)
    db._pool = object()  # so _check_pool() passes
    db._pool_handle = 0  # positive pool handle for non-tx routing
    db._execute_wrappers = []
    return db


async def _scenario_serialization() -> Tracker:
    """Two gathered queries INSIDE a task-scoped transaction on a multiplexing
    loop — reproduces the sibling-task-shares-one-connection race."""
    global _tracker
    _tracker = Tracker()
    mark_loop_multiplexing(asyncio.get_running_loop())
    db = _make_db()

    async with db.transaction():
        # Two child Tasks (gather) inheriting the SAME task-scoped tx state.
        await asyncio.gather(
            db.query_tuples("SELECT 1"),
            db.query_tuples("SELECT 2"),
        )
    return _tracker


async def _scenario_non_tx() -> Tracker:
    """Two gathered queries with NO transaction — must stay concurrent."""
    global _tracker, _rendezvous
    _tracker = Tracker()
    mark_loop_multiplexing(asyncio.get_running_loop())
    db = _make_db()
    # Arm a 2-party barrier so the two ops must be simultaneously in their
    # critical sections to proceed — deterministically proving they overlap
    # (they are NOT serialized by the tx lock off the transaction path).
    _rendezvous = threading.Barrier(2)
    try:
        await asyncio.gather(
            db.query_tuples("SELECT 1"),
            db.query_tuples("SELECT 2"),
        )
    finally:
        _rendezvous = None
    return _tracker


async def _scenario_copy_and_cursor() -> Tracker:
    """copy_from / copy_to / server_cursor INSIDE a task-scoped transaction —
    each must route onto the transaction's own connection."""
    global _tracker
    _tracker = Tracker()
    mark_loop_multiplexing(asyncio.get_running_loop())
    db = _make_db()

    async with db.transaction():
        await db.copy_from("t", ["a"], [["1"], ["2"]])
        await db.copy_to("COPY t TO STDOUT")
        cur = await db.server_cursor("SELECT * FROM t")
        # Exercise fetch + close so their routing is recorded too.
        await cur.fetch_page()
        await cur.close()
    return _tracker


def main() -> int:
    _install_fakes()

    # ── #1 serialization on the pinned connection ────────────────────────
    print("#1 task-scoped transaction serializes the pinned connection")
    t = asyncio.run(_scenario_serialization())
    check(
        "two gathered in-tx queries never overlap on the pinned handle",
        t.max.get(_PINNED, 0) == 1,
        f"max concurrency on pinned handle = {t.max.get(_PINNED)} (expected 1)",
    )
    # Sanity: the two body queries really did run on the pinned handle.
    body_q = [
        c for c in t.calls if c[0] == "query" and c[2] in ("SELECT 1", "SELECT 2")
    ]
    check(
        "both in-tx queries routed to the pinned handle",
        len(body_q) == 2 and all(c[1] == _PINNED for c in body_q),
        f"body queries = {body_q}",
    )
    # BEGIN + COMMIT also ran on the pinned handle.
    ctl = {c[2].split()[0] for c in t.calls if c[1] == _PINNED and c[0] == "execute"}
    check(
        "BEGIN and COMMIT ran on the pinned handle",
        {"BEGIN", "COMMIT"} <= ctl,
        f"control statements on pinned handle = {ctl}",
    )

    # ── control: non-tx queries stay concurrent (lock is tx-scoped) ──────
    print("\n#1 control: queries with NO transaction still run concurrently")
    t = asyncio.run(_scenario_non_tx())
    check(
        "two gathered non-tx queries overlap on the pool handle (max >= 2)",
        t.max.get(0, 0) >= 2,
        f"max concurrency on pool handle = {t.max.get(0)} "
        f"(expected >= 2 — proves the tracker detects overlap and the lock is "
        f"NOT engaged off the transaction path)",
    )

    # ── #2 copy_* / server_cursor route through the active-tx handle ─────
    print("\n#2 copy_from / copy_to / server_cursor join the active transaction")
    t = asyncio.run(_scenario_copy_and_cursor())

    copy_from = [c for c in t.calls if c[0] == "copy_from"]
    check(
        "copy_from runs on the transaction's RAW pinned slot",
        len(copy_from) == 1 and copy_from[0][1] == _RAW_SLOT,
        f"copy_from calls = {copy_from} (expected handle {_RAW_SLOT})",
    )
    copy_to = [c for c in t.calls if c[0] == "copy_to"]
    check(
        "copy_to runs on the transaction's RAW pinned slot",
        len(copy_to) == 1 and copy_to[0][1] == _RAW_SLOT,
        f"copy_to calls = {copy_to} (expected handle {_RAW_SLOT})",
    )

    declare = [c for c in t.calls if "DECLARE" in c[2]]
    check(
        "server_cursor DECLARE runs on the transaction's effective (pinned) handle",
        len(declare) == 1 and declare[0][1] == _PINNED,
        f"DECLARE calls = {declare} (expected handle {_PINNED})",
    )
    fetch = [c for c in t.calls if "FETCH" in c[2]]
    check(
        "server_cursor FETCH runs on the transaction's effective handle",
        len(fetch) == 1 and fetch[0][1] == _PINNED,
        f"FETCH calls = {fetch} (expected handle {_PINNED})",
    )
    cur_close = [c for c in t.calls if c[2].startswith("CLOSE")]
    check(
        "server_cursor CLOSE runs on the transaction's effective handle",
        len(cur_close) == 1 and cur_close[0][1] == _PINNED,
        f"CLOSE calls = {cur_close} (expected handle {_PINNED})",
    )
    # The borrowed cursor must NOT issue its own BEGIN/COMMIT nor acquire a
    # second connection — the surrounding transaction owns that lifecycle.
    check(
        "borrowed cursor issued no extra connection acquire (only the tx's one)",
        t.acquires == 1,
        f"_db_conn_acquire calls = {t.acquires} (expected 1)",
    )
    begins = [c for c in t.calls if c[2] == "BEGIN"]
    check(
        "exactly one BEGIN (the transaction's) — cursor added none",
        len(begins) == 1,
        f"BEGIN calls = {begins} (expected 1)",
    )
    commits = [c for c in t.calls if c[2] == "COMMIT"]
    check(
        "exactly one COMMIT (the transaction's) — cursor added none",
        len(commits) == 1,
        f"COMMIT calls = {commits} (expected 1)",
    )

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All db tx-race r13 tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
