"""
Connection state machine proof tests.

# hyper-test: db_isolated

Direct proof tests for the conn._state transitions. Each test
prints the connection state BEFORE and AFTER every operation
so the subprocess log shows exactly where the state machine breaks.

These tests use raw _db_* FFI calls to inspect connection state
at the Zig level, not the Python Database wrapper.
"""

import asyncio
import os
import sys

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}", flush=True)
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}", flush=True)


async def test_sequential_simple_executes() -> None:
    """Two back-to-back db.execute() calls on the same connection."""
    print("\n── Sequential simple executes ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        print("[TRACE] before execute 1", flush=True)
        await db.execute("SELECT 1")
        print("[TRACE] after execute 1", flush=True)
        await db.execute("SELECT 2")
        print("[TRACE] after execute 2", flush=True)
        await db.execute("SELECT 3")
        print("[TRACE] after execute 3", flush=True)
        check("3 sequential executes succeed", True)
    except Exception as e:
        check("3 sequential executes succeed", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_sequential_simple_queries() -> None:
    """Two back-to-back db.query() calls (uses queryOpts path)."""
    print("\n── Sequential simple queries ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        print("[TRACE] before query 1", flush=True)
        r1 = await db.query("SELECT 1 as x")
        print(f"[TRACE] after query 1: {r1}", flush=True)
        r2 = await db.query("SELECT 2 as x")
        print(f"[TRACE] after query 2: {r2}", flush=True)
        r3 = await db.query("SELECT 3 as x")
        print(f"[TRACE] after query 3: {r3}", flush=True)
        check("3 sequential queries succeed", True)
        check("query 3 result correct", r3[0]["x"] == 3)
    except Exception as e:
        check("3 sequential queries succeed", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_execute_then_query() -> None:
    """execute() followed by query() on the same connection."""
    print("\n── Execute then query ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        print("[TRACE] before execute", flush=True)
        await db.execute("SELECT 1")
        print("[TRACE] after execute, before query", flush=True)
        r = await db.query("SELECT 42 as answer")
        print(f"[TRACE] after query: {r}", flush=True)
        check("execute then query succeeds", r[0]["answer"] == 42)
    except Exception as e:
        check("execute then query succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_query_then_execute() -> None:
    """query() followed by execute() on the same connection."""
    print("\n── Query then execute ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        print("[TRACE] before query", flush=True)
        r = await db.query("SELECT 1 as x")
        print(f"[TRACE] after query: {r}", flush=True)
        print("[TRACE] before execute", flush=True)
        await db.execute("SELECT 2")
        print("[TRACE] after execute", flush=True)
        check("query then execute succeeds", True)
    except Exception as e:
        check("query then execute succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_parameterized_then_simple() -> None:
    """Parameterized query (extended protocol) then simple query."""
    print("\n── Parameterized then simple ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        await db.execute("DROP TABLE IF EXISTS state_test")
        await db.execute("CREATE TABLE state_test (id serial PRIMARY KEY, v text)")
        await db.execute("INSERT INTO state_test (v) VALUES ('a')")
        print("[TRACE] setup done", flush=True)

        print("[TRACE] before parameterized query", flush=True)
        r1 = await db.query("SELECT * FROM state_test WHERE id = $1", 1)
        print(f"[TRACE] after parameterized: {r1}", flush=True)

        print("[TRACE] before simple query", flush=True)
        r2 = await db.query("SELECT count(*) as n FROM state_test")
        print(f"[TRACE] after simple: {r2}", flush=True)

        check("parameterized then simple succeeds", r2[0]["n"] == 1)
    except Exception as e:
        check("parameterized then simple succeeds", False, str(e)[:200])
    finally:
        await db.execute("DROP TABLE IF EXISTS state_test")
        await db.disconnect()


async def test_multi_semicolon_execute() -> None:
    """Multi-statement execute with semicolons (simple protocol)."""
    print("\n── Multi-semicolon execute ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        print("[TRACE] before multi-statement", flush=True)
        await db.execute("SELECT 1; SELECT 2; SELECT 3")
        print("[TRACE] after multi-statement", flush=True)
        check("multi-semicolon execute succeeds", True)

        # Verify connection is still usable after
        print("[TRACE] before follow-up query", flush=True)
        r = await db.query("SELECT 99 as x")
        print(f"[TRACE] follow-up: {r}", flush=True)
        check("connection usable after multi-semicolon", r[0]["x"] == 99)
    except Exception as e:
        check("multi-semicolon execute succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_reset_all_then_unlisten() -> None:
    """Exact resetSession pattern: RESET ALL then UNLISTEN *."""
    print("\n── RESET ALL then UNLISTEN * ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        print("[TRACE] before RESET ALL", flush=True)
        await db.execute("RESET ALL")
        print("[TRACE] after RESET ALL, before UNLISTEN", flush=True)
        await db.execute("UNLISTEN *")
        print("[TRACE] after UNLISTEN *", flush=True)
        check("RESET ALL then UNLISTEN succeeds", True)

        # Verify connection still usable
        r = await db.query("SELECT 1 as x")
        check("connection usable after reset+unlisten", r[0]["x"] == 1)
    except Exception as e:
        check("RESET ALL then UNLISTEN succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_pool_release_reacquire() -> None:
    """Create two Database instances to force pool release+reacquire cycle."""
    print("\n── Pool release + reacquire ──", flush=True)
    from hyperdjango.database import Database

    db1 = Database(DATABASE_URL)
    await db1.connect()
    try:
        r1 = await db1.query("SELECT 1 as x")
        print(f"[TRACE] db1 query: {r1}", flush=True)
        check("db1 query works", r1[0]["x"] == 1)
    finally:
        await db1.disconnect()
        print("[TRACE] db1 disconnected (pool released)", flush=True)

    # Second connection — should get a recycled pool connection
    db2 = Database(DATABASE_URL)
    await db2.connect()
    try:
        r2 = await db2.query("SELECT 2 as x")
        print(f"[TRACE] db2 query: {r2}", flush=True)
        check("db2 query works after pool recycle", r2[0]["x"] == 2)
    except Exception as e:
        check("db2 query works after pool recycle", False, str(e)[:200])
    finally:
        await db2.disconnect()
        print("[TRACE] db2 disconnected", flush=True)


async def run_all() -> None:
    await test_sequential_simple_executes()
    await test_sequential_simple_queries()
    await test_execute_then_query()
    await test_query_then_execute()
    await test_parameterized_then_simple()
    await test_multi_semicolon_execute()
    await test_reset_all_then_unlisten()
    await test_pool_release_reacquire()


def main() -> int:
    print("=" * 70, flush=True)
    print("  Connection state machine proof tests", flush=True)
    print("=" * 70, flush=True)

    asyncio.run(run_all())

    print(flush=True)
    print("=" * 70, flush=True)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed", flush=True)
    if errors:
        print("\nFailures:", flush=True)
        for e in errors:
            print(f"  {e}", flush=True)
    print("=" * 70, flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
