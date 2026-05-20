"""
Multi-statement command proof tests.

# hyper-test: db_isolated

Tests every combination of semicolon-separated SQL commands through
every code path: db.execute(), db.query(), and the internal
resetSession() path (via pool release). Each test prints trace
output so failures are diagnosable from the subprocess log.

The original upstream pg.zig uses `RESET ALL; CLOSE ALL; UNLISTEN *`
as a single multi-statement string. We must verify this works through
our system end-to-end.
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


async def test_multi_select_execute() -> None:
    """Multiple SELECTs in one execute (simple protocol)."""
    print("\n── Multi-SELECT via execute ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        await db.execute("SELECT 1; SELECT 2; SELECT 3")
        print("[TRACE] multi-select done", flush=True)
        check("multi-select execute succeeds", True)
        # Verify connection still works after
        r = await db.query("SELECT 99 as x")
        check("connection usable after multi-select", r[0]["x"] == 99)
    except Exception as e:
        check("multi-select execute succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_multi_ddl_execute() -> None:
    """Multiple DDL statements in one execute."""
    print("\n── Multi-DDL via execute ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        await db.execute(
            "DROP TABLE IF EXISTS multi_test_a; "
            "DROP TABLE IF EXISTS multi_test_b; "
            "CREATE TABLE multi_test_a (id int); "
            "CREATE TABLE multi_test_b (id int)"
        )
        print("[TRACE] multi-ddl done", flush=True)
        # Verify both tables exist
        r1 = await db.query("SELECT count(*) as n FROM multi_test_a")
        r2 = await db.query("SELECT count(*) as n FROM multi_test_b")
        check("table a exists", r1[0]["n"] == 0)
        check("table b exists", r2[0]["n"] == 0)
    except Exception as e:
        check("multi-ddl execute succeeds", False, str(e)[:200])
    finally:
        await db.execute("DROP TABLE IF EXISTS multi_test_a")
        await db.execute("DROP TABLE IF EXISTS multi_test_b")
        await db.disconnect()


async def test_reset_all_semicolon_unlisten() -> None:
    """The exact upstream pattern: RESET ALL; UNLISTEN * as one string."""
    print("\n── RESET ALL; UNLISTEN * as single execute ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        await db.execute("RESET ALL; UNLISTEN *")
        print("[TRACE] compound reset done", flush=True)
        check("compound RESET ALL; UNLISTEN * succeeds", True)
        r = await db.query("SELECT 1 as x")
        check("connection usable after compound reset", r[0]["x"] == 1)
    except Exception as e:
        check("compound RESET ALL; UNLISTEN * succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_full_upstream_reset_pattern() -> None:
    """The full upstream pg.zig pattern: RESET ALL; CLOSE ALL; UNLISTEN *."""
    print("\n── RESET ALL; CLOSE ALL; UNLISTEN * ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        await db.execute("RESET ALL; CLOSE ALL; UNLISTEN *")
        print("[TRACE] full upstream reset done", flush=True)
        check("full upstream reset pattern succeeds", True)
        r = await db.query("SELECT 1 as x")
        check("connection usable after full reset", r[0]["x"] == 1)
    except Exception as e:
        check("full upstream reset pattern succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_set_then_reset_via_multi_statement() -> None:
    """SET a session variable, then RESET ALL in the same statement."""
    print("\n── SET + RESET in one execute ──", flush=True)
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        await db.execute("SET statement_timeout = '5s'; RESET ALL")
        print("[TRACE] set+reset done", flush=True)
        # Verify statement_timeout was reset to default
        r = await db.query("SHOW statement_timeout")
        timeout = r[0]["statement_timeout"]
        print(f"[TRACE] statement_timeout after reset: {timeout}", flush=True)
        check("statement_timeout reset to default", timeout == "0" or timeout == "0ms")
    except Exception as e:
        check("SET + RESET succeeds", False, str(e)[:200])
    finally:
        await db.disconnect()


async def test_pool_release_exercises_reset() -> None:
    """Force pool release by creating + destroying Database, then verify
    the next connection on the same pool is clean."""
    print("\n── Pool release exercises resetSession ──", flush=True)
    from hyperdjango.database import Database

    # First connection: SET a session variable
    db1 = Database(DATABASE_URL)
    await db1.connect()
    try:
        await db1.execute("SET statement_timeout = '999s'")
        r1 = await db1.query("SHOW statement_timeout")
        print(
            f"[TRACE] db1 statement_timeout: {r1[0]['statement_timeout']}", flush=True
        )
        check("db1 has custom timeout", "999" in r1[0]["statement_timeout"])
    finally:
        await db1.disconnect()
        print(
            "[TRACE] db1 disconnected (pool released, resetSession fired)", flush=True
        )

    # Second connection: should get a RESET connection
    db2 = Database(DATABASE_URL)
    await db2.connect()
    try:
        r2 = await db2.query("SHOW statement_timeout")
        timeout = r2[0]["statement_timeout"]
        print(f"[TRACE] db2 statement_timeout: {timeout}", flush=True)
        check(
            "db2 has default timeout after pool reset",
            timeout == "0" or timeout == "0ms",
        )
    except Exception as e:
        check("db2 query after pool reset succeeds", False, str(e)[:200])
    finally:
        await db2.disconnect()


async def test_listen_then_pool_reset_cleans_up() -> None:
    """LISTEN on a channel, release to pool, verify UNLISTEN fired."""
    print("\n── LISTEN + pool release → UNLISTEN ──", flush=True)
    from hyperdjango.database import Database

    db1 = Database(DATABASE_URL)
    await db1.connect()
    try:
        await db1.execute("LISTEN test_channel_xyz")
        print("[TRACE] LISTEN done", flush=True)
    finally:
        await db1.disconnect()
        print("[TRACE] db1 disconnected", flush=True)

    # New connection on same pool — verify no active listeners
    db2 = Database(DATABASE_URL)
    await db2.connect()
    try:
        # pg_listening_channels() returns the channels this session is listening on
        r = await db2.query("SELECT * FROM pg_listening_channels() AS channel")
        channels = [row.get("channel", "") for row in r]
        print(f"[TRACE] db2 listening channels: {channels}", flush=True)
        check(
            "no active listeners after pool reset", "test_channel_xyz" not in channels
        )
    except Exception as e:
        check("listener cleanup check succeeds", False, str(e)[:200])
    finally:
        await db2.disconnect()


async def run_all() -> None:
    await test_multi_select_execute()
    await test_multi_ddl_execute()
    await test_reset_all_semicolon_unlisten()
    await test_full_upstream_reset_pattern()
    await test_set_then_reset_via_multi_statement()
    await test_pool_release_exercises_reset()
    await test_listen_then_pool_reset_cleans_up()


def main() -> int:
    print("=" * 70, flush=True)
    print("  Multi-statement command proof tests", flush=True)
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
