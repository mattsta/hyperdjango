#!/usr/bin/env python3
"""Round-7 regression tests: multi-DB instance routing, task-scoped transactions
on multiplexing loops, channels LISTEN dedup, and execute_wrapper wiring.

Covers the data-integrity bugs fixed in this round:

1. Instance writes (save/delete/refresh_from_db, M2M, create/get_or_create/
   update_or_create) now honor Meta.database + the write router instead of
   silently using the global default (split-brain read-here/write-there).
2. transaction() on a MULTIPLEXING loop is TASK-scoped (its own pinned
   connection + contextvar depth), so concurrent transactions can't share
   depth or a connection.
4. channels LISTEN dedup compares the PREFIXED name and registers under the
   layer lock (no always-true guard, no double LISTEN).
5. execute_wrapper() actually wraps each query.

Most assertions run WITHOUT a live database (fake connections + direct unit
checks). A few end-to-end checks run only when DATABASE_URL points at a live
PostgreSQL; they self-skip otherwise.

Usage:
    uv run hyper-test db_routing_tx_channels_r7
    uv run python scripts/test_db_routing_tx_channels_r7.py
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import traceback

import hyperdjango.database as dbmod
from hyperdjango.channels import PgChannelLayer
from hyperdjango.database import Database, mark_loop_multiplexing
from hyperdjango.models import Field, Model, _resolve_instance_db
from hyperdjango.multi_db import ConnectionManager, DatabaseRouter, set_connections

RESULTS = {"passed": 0, "failed": 0, "skipped": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


class _Skip(Exception):
    """Raised by a test to skip itself (e.g. no live DB)."""


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
            except _Skip as e:
                RESULTS["skipped"] += 1
                print(f"  ⊘ {name} (skipped: {e})")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDB:
    """Records the queries routed to it; returns benign values so save()/create()
    complete without a real PostgreSQL."""

    def __init__(self, alias):
        self.alias = alias
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return 1

    async def query(self, sql, *args):
        self.calls.append(("query", sql, args))
        return []

    async def query_one(self, sql, *args):
        self.calls.append(("query_one", sql, args))
        return None

    async def query_val(self, sql, *args):
        self.calls.append(("query_val", sql, args))
        return 123  # simulated RETURNING pk

    @property
    def wrote(self):
        return bool(self.calls)


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class R7Routed(Model):
    class Meta:
        table = "test_r7_routed"
        database = "analytics"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class R7Plain(Model):
    class Meta:
        table = "test_r7_plain"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


# ═══════════════════════════════════════════════════════════════════════════
# Bug #1 — instance-write routing
# ═══════════════════════════════════════════════════════════════════════════


@test("save() honors Meta.database (writes to routed connection, not default)")
async def test_save_routing():
    default_db, analytics = FakeDB("default"), FakeDB("analytics")
    cm = ConnectionManager()
    cm._databases = {"default": default_db, "analytics": analytics}
    set_connections(cm)
    try:
        # _resolve_instance_db must pick the model's bound database.
        assert _resolve_instance_db(R7Routed, for_write=True) is analytics
        assert _resolve_instance_db(R7Routed, for_write=False) is analytics

        inst = R7Routed(name="x")
        await inst.save()
        assert analytics.wrote, "save() did not write to the analytics connection"
        assert not default_db.wrote, "save() leaked a write to the default connection"
        assert inst.id == 123, "RETURNING pk not assigned back onto the instance"
    finally:
        set_connections(ConnectionManager())


@test("delete() honors Meta.database")
async def test_delete_routing():
    default_db, analytics = FakeDB("default"), FakeDB("analytics")
    cm = ConnectionManager()
    cm._databases = {"default": default_db, "analytics": analytics}
    set_connections(cm)
    try:
        inst = R7Routed(id=5, name="x")
        inst._loaded_from_db = True
        await inst.delete()
        assert analytics.wrote and not default_db.wrote
    finally:
        set_connections(ConnectionManager())


@test("write router routes save() for an unbound model")
async def test_router_routing():
    primary, replica = FakeDB("default"), FakeDB("replica")
    cm = ConnectionManager()
    cm._databases = {"default": primary, "replica": replica}

    class ReadReplicaRouter(DatabaseRouter):
        def db_for_read(self, model):
            return "replica"

        def db_for_write(self, model):
            return "default"

    cm.router = ReadReplicaRouter()
    set_connections(cm)
    try:
        inst = R7Plain(name="y")
        await inst.save()
        assert primary.wrote, "write did not route to primary via router"
        assert not replica.wrote, "write leaked to the read replica"
    finally:
        set_connections(ConnectionManager())


@test("QuerySet.create() propagates .using() into the created instance's save()")
async def test_create_using_propagation():
    default_db, analytics = FakeDB("default"), FakeDB("analytics")
    cm = ConnectionManager()
    cm._databases = {"default": default_db, "analytics": analytics}
    set_connections(cm)
    try:
        await R7Plain.objects.using("analytics").create(name="z")
        assert analytics.wrote, "create().using() did not route to analytics"
        assert not default_db.wrote, "create() dropped the queryset's .using() binding"
    finally:
        set_connections(ConnectionManager())


@test("falls back to global default when no multi-db manager configured")
async def test_no_manager_fallback():
    set_connections(ConnectionManager())  # empty (_databases == {})
    sentinel = FakeDB("global-default")
    orig = dbmod.get_db
    dbmod.get_db = lambda: sentinel
    # models.py imported get_db by value; patch there too.
    import hyperdjango.models as mmod

    mmod_orig = mmod.get_db
    mmod.get_db = lambda: sentinel
    try:
        assert _resolve_instance_db(R7Plain, for_write=True) is sentinel
    finally:
        dbmod.get_db = orig
        mmod.get_db = mmod_orig


# ═══════════════════════════════════════════════════════════════════════════
# Bug #2 — task-scoped transaction state on multiplexing loops
# ═══════════════════════════════════════════════════════════════════════════


@test("tx state is TASK-scoped: two tasks on one loop don't share depth")
async def test_tx_task_scoped_isolation():
    db = Database.__new__(Database)  # no pool needed — we probe state only
    db.__dict__["_tx_local"] = dbmod._TxLocal()
    seen = {}
    # These two tasks must interleave in a SPECIFIC order for the test to mean
    # anything: task_b has to look while task_a's transaction is genuinely open.
    # `await asyncio.sleep(0)` only inherited that order from how `gather`
    # happens to schedule the pair — the ordering was never stated, so nothing
    # would have failed if it changed; task_b would simply have looked before
    # task_a installed anything and passed vacuously. Two events state it.
    a_open = asyncio.Event()  # task_a's tx state is installed
    b_looked = asyncio.Event()  # task_b has made its observation

    async def task_a():
        assert db._task_tx() is None
        state = dbmod._TaskTxState(
            depth=1, conn_handle=0, pinned_handle=-2, callbacks=[]
        )
        reg = {id(db): state}
        tok = dbmod._tx_context.set(reg)
        try:
            a_open.set()
            # Hold the tx open until task_b has actually looked at it.
            await b_looked.wait()
            # Our own state must be intact and unseen by task_b.
            assert db._task_tx() is state
            state.depth += 1
            assert db._task_tx().depth == 2
            seen["a_final_depth"] = db._task_tx().depth
        finally:
            dbmod._tx_context.reset(tok)

    async def task_b():
        await a_open.wait()  # task_a's tx is now demonstrably open
        # The whole point: task_b must NOT observe task_a's transaction.
        try:
            assert db._task_tx() is None, "task B saw task A's task-scoped tx state!"
            seen["b_sees_none"] = True
        finally:
            # Release task_a even if the assertion above failed, so a failure
            # surfaces as a failure instead of a hung gather.
            b_looked.set()

    await asyncio.gather(task_a(), task_b())
    assert seen == {"a_final_depth": 2, "b_sees_none": True}, seen


@test("contrast: the OLD thread-local depth WOULD be shared across tasks")
async def test_threadlocal_shared_contrast():
    # Documents *why* the fix was needed: a thread-local (not task-scoped) depth
    # is visible to every coroutine sharing the loop thread.
    db = Database.__new__(Database)
    db.__dict__["_tx_local"] = dbmod._TxLocal()
    # Same ordering contract as above, stated rather than assumed: task_b's
    # read is only evidence of sharing if task_a's write has already landed.
    a_wrote = asyncio.Event()

    async def task_a():
        db._tx_depth.depth = 1
        a_wrote.set()

    async def task_b():
        await a_wrote.wait()
        # Same thread → thread-local IS shared (the bug the contextvar fixes).
        assert db._tx_depth.depth == 1

    await asyncio.gather(task_a(), task_b())
    db._tx_depth.depth = 0


@test("live: concurrent transactions on a multiplexing loop stay isolated")
async def test_live_multiplexed_tx():
    db = await _connect_or_skip()
    await db.execute("DROP TABLE IF EXISTS test_r7_tx")
    await db.execute("CREATE TABLE test_r7_tx (id INT PRIMARY KEY, who TEXT)")

    loop = asyncio.get_running_loop()
    already_mux = getattr(loop, dbmod._MULTIPLEXING_LOOP_ATTR, False)
    mark_loop_multiplexing(loop)
    try:

        async def worker(who, key, do_commit):
            async with db.transaction():
                await db.execute(
                    "INSERT INTO test_r7_tx (id, who) VALUES ($1, $2)", key, who
                )
                await asyncio.sleep(0)  # interleave with the other tx
                if not do_commit:
                    raise RuntimeError("rollback me")

        # A commits, B rolls back — B's rollback must not lose A's row and A's
        # commit must not persist B's row (no shared connection/depth).
        results = await asyncio.gather(
            worker("a", 1, True),
            worker("b", 2, False),
            return_exceptions=True,
        )
        assert isinstance(results[1], RuntimeError)
        rows = await db.query("SELECT id, who FROM test_r7_tx ORDER BY id")
        ids = [r["id"] for r in rows]
        assert ids == [1], f"expected only committed row 1, got {ids}"
    finally:
        if not already_mux:
            # Restore so later tests on this loop keep their original path.
            setattr(loop, dbmod._MULTIPLEXING_LOOP_ATTR, False)
        await db.execute("DROP TABLE IF EXISTS test_r7_tx")


# ═══════════════════════════════════════════════════════════════════════════
# Bug #4 — channels LISTEN dedup
# ═══════════════════════════════════════════════════════════════════════════


@test("channel() dedup guard uses the PREFIXED name (no re-start per call)")
def test_channel_guard_prefixed():
    layer = PgChannelLayer(database_url="postgres://x/y")
    layer._db = object()  # truthy so the LISTEN guard path runs
    calls = []

    def fake_start(name):
        calls.append(name)
        layer._listener_channels.add(layer._pg_channel_name(name))

    layer._start_listener = fake_start
    layer.channel("room1")
    layer.channel("room1")
    layer.channel("room1")
    assert calls == ["room1"], f"guard re-ran _start_listener (raw-name bug): {calls}"
    # The set holds the PREFIXED name only.
    assert layer._pg_channel_name("room1") in layer._listener_channels
    assert "room1" not in layer._listener_channels


@test("_start_listener registers a PG LISTEN at most once per channel")
def test_start_listener_single_registration():
    import hyperdjango._hyperdjango_native as nat

    listen_calls = []
    orig = nat._db_listen
    nat._db_listen = lambda url, ch, cb: listen_calls.append(ch)
    try:
        layer = PgChannelLayer(database_url="postgres://x/y")
        layer._start_listener("room2")
        layer._start_listener("room2")  # inner double-check under lock dedups
        layer._start_listener("room2")
        assert listen_calls == [layer._pg_channel_name("room2")], listen_calls
    finally:
        nat._db_listen = orig


# ═══════════════════════════════════════════════════════════════════════════
# Bug #5 — execute_wrapper actually wraps queries
# ═══════════════════════════════════════════════════════════════════════════


@test("execute_wrapper composition wraps the native call (order + pass-through)")
def test_execute_wrapper_composition():
    db = Database.__new__(Database)
    db._execute_wrappers = []
    events = []

    def w_outer(execute, sql, params):
        events.append("outer-before")
        r = execute(sql, params)
        events.append("outer-after")
        return r

    def w_inner(execute, sql, params):
        events.append("inner-before")
        r = execute(sql, params)
        events.append("inner-after")
        return r

    db._execute_wrappers.append(w_outer)  # first registered → innermost...
    db._execute_wrappers.append(w_inner)  # last registered → OUTERMOST

    def native():
        events.append("native")
        return "RESULT"

    wrapped = db._apply_execute_wrappers("SELECT 1", (7,), native)
    assert wrapped() == "RESULT"
    # last-registered (w_inner) is outermost, native runs once in the middle.
    assert events == [
        "inner-before",
        "outer-before",
        "native",
        "outer-after",
        "inner-after",
    ], events


@test("execute_wrapper can BLOCK a query (raises before native call)")
def test_execute_wrapper_blocks():
    db = Database.__new__(Database)
    db._execute_wrappers = []
    ran = []

    def block(execute, sql, params):
        raise RuntimeError(f"blocked: {sql}")

    db._execute_wrappers.append(block)
    wrapped = db._apply_execute_wrappers("SELECT 2", (), lambda: ran.append(1))
    try:
        wrapped()
        raise AssertionError("blocking wrapper did not raise")
    except RuntimeError as e:
        assert "blocked: SELECT 2" in str(e)
    assert not ran, "native call ran despite a blocking wrapper"


@test("no wrappers → native_call returned unchanged (zero overhead)")
def test_execute_wrapper_noop():
    db = Database.__new__(Database)
    db._execute_wrappers = []
    native = lambda: "X"
    assert db._apply_execute_wrappers("SELECT 3", (), native) is native


@test("live: db.execute_wrapper() wraps a real query end-to-end")
async def test_live_execute_wrapper():
    db = await _connect_or_skip()
    seen = []

    def logger(execute, sql, params):
        seen.append(sql)
        return execute(sql, params)

    with db.execute_wrapper(logger):
        rows = await db.query("SELECT 1 AS one")
    assert rows and rows[0]["one"] == 1
    assert any("SELECT 1" in s for s in seen), f"wrapper never saw the query: {seen}"

    # A blocking wrapper must actually block a live query.
    def blocker(execute, sql, params):
        raise RuntimeError("blocked-live")

    blocked = False
    with db.execute_wrapper(blocker):
        try:
            await db.query("SELECT 1")
        except RuntimeError as e:
            blocked = "blocked-live" in str(e)
    assert blocked, "live blocking wrapper did not prevent the query"


# ---------------------------------------------------------------------------
# Live-DB helper
# ---------------------------------------------------------------------------

_LIVE_DB = None
_LIVE_TRIED = False


async def _connect_or_skip() -> Database:
    global _LIVE_DB, _LIVE_TRIED
    if _LIVE_DB is not None:
        return _LIVE_DB
    if _LIVE_TRIED:
        raise _Skip("no live database")
    _LIVE_TRIED = True
    try:
        db = Database(DB_URL)
        await db.connect()
        await db.query("SELECT 1")
        _LIVE_DB = db
        return db
    except Exception as e:
        raise _Skip(f"no live database ({type(e).__name__})")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    print("Round-7 DB routing / tx / channels / execute_wrapper regression tests\n")
    tests = [
        v
        for v in list(globals().values())
        if callable(v) and getattr(v, "_is_test", False)
    ]
    for t in tests:
        await t()

    print(
        f"\n{RESULTS['passed']} passed, {RESULTS['failed']} failed, "
        f"{RESULTS['skipped']} skipped"
    )
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n=== {name} ===\n{tb}")
    if _LIVE_DB is not None:
        await _LIVE_DB.disconnect()
    return 1 if RESULTS["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
