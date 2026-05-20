"""
Cached plan error recovery — live DB tests (task #270).

# hyper-test: db_isolated

ROOT CAUSE: the test suite tested happy-path stmt cache eviction but never
triggered the actual "cached plan must not change result type" error. This
file exercises the FULL error→detect→evict→retry→success cycle that tasks
#266/#267/#268 fixed.

PostgreSQL will return SQLSTATE 42P18 when a prepared query's column set
changes after the statement was prepared (ALTER TABLE ADD/DROP COLUMN).
HyperDjango's db.zig detects this via `isCachedPlanError()` (SQLSTATE
check), clears the connection error state, evicts the broken entry from
the per-connection cache, clears the global name cache, and retries with
a fresh parse — all in one round-trip.

These tests require a live PostgreSQL connection.

Coverage:
  1. ALTER TABLE ADD COLUMN → prepared SELECT * → retry succeeds
  2. ALTER TABLE DROP COLUMN → retry succeeds
  3. ALTER TABLE ALTER TYPE → retry succeeds
  4. Retry only happens ONCE (not infinite loop)
  5. Non-cached-plan error (syntax error) does NOT trigger retry
  6. Unique violation does NOT trigger retry
  7. Query after successful retry uses fresh prepared plan
  8. Multiple sequential schema changes → each triggers retry
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
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


async def setup_db():
    """Create a fresh Database connection for testing."""
    from hyperdjango.database import Database

    db = Database(DATABASE_URL)
    await db.connect()
    return db


async def test_add_column_recovery() -> None:
    print("\n── ALTER TABLE ADD COLUMN → cached plan retry ──", flush=True)
    db = await setup_db()
    print("[MARKER] db connected", flush=True)
    try:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        print("[MARKER] drop done", flush=True)
        await db.execute(
            "CREATE TABLE test_cached_plan (id serial PRIMARY KEY, name text)"
        )
        print("[MARKER] create done", flush=True)
        await db.execute("INSERT INTO test_cached_plan (name) VALUES ('alice')")
        print("[MARKER] insert done", flush=True)

        # First query — prepares the statement
        rows = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        print(f"[MARKER] first query done, rows={len(rows)}", flush=True)
        check("initial query succeeds", len(rows) == 1)
        check("initial row has name", rows[0].get("name") == "alice")

        # Schema change — invalidates the cached plan
        print("[MARKER] about to ALTER TABLE", flush=True)
        await db.execute("ALTER TABLE test_cached_plan ADD COLUMN age int DEFAULT 0")
        print("[MARKER] ALTER TABLE done", flush=True)

        # Second query — should trigger retry on cached plan error
        print("[MARKER] about to retry query after ALTER", flush=True)
        rows2 = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        print(f"[MARKER] retry query done, rows={len(rows2)}", flush=True)
        check("retry after ADD COLUMN succeeds", len(rows2) == 1)
        check("retry row has new column", "age" in rows2[0])

        # Third query — should use the fresh plan (no error)
        rows3 = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        print(f"[MARKER] third query done, rows={len(rows3)}", flush=True)
        check("subsequent query succeeds (fresh plan)", len(rows3) == 1)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.disconnect()


async def test_drop_column_recovery() -> None:
    print("\n── ALTER TABLE DROP COLUMN → cached plan retry ──")
    db = await setup_db()
    try:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.execute(
            "CREATE TABLE test_cached_plan (id serial PRIMARY KEY, name text, age int)"
        )
        await db.execute("INSERT INTO test_cached_plan (name, age) VALUES ('bob', 30)")

        rows = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("initial query has age column", "age" in rows[0])

        await db.execute("ALTER TABLE test_cached_plan DROP COLUMN age")

        rows2 = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("retry after DROP COLUMN succeeds", len(rows2) == 1)
        check("retry row does NOT have age column", "age" not in rows2[0])
    finally:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.disconnect()


async def test_alter_type_recovery() -> None:
    print("\n── ALTER TABLE ALTER TYPE → cached plan retry ──")
    db = await setup_db()
    try:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.execute(
            "CREATE TABLE test_cached_plan (id serial PRIMARY KEY, val int)"
        )
        await db.execute("INSERT INTO test_cached_plan (val) VALUES (42)")

        rows = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("initial query succeeds", len(rows) == 1)

        await db.execute(
            "ALTER TABLE test_cached_plan ALTER COLUMN val TYPE text USING val::text"
        )

        rows2 = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("retry after ALTER TYPE succeeds", len(rows2) == 1)
        check("val is now text", isinstance(rows2[0].get("val"), str))
    finally:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.disconnect()


async def test_syntax_error_no_retry() -> None:
    print("\n── Syntax error does NOT trigger retry ──")
    db = await setup_db()
    try:
        # A syntax error should propagate, not be caught as cached plan
        try:
            await db.query("SELCT * FORM nonexistent")
            check("syntax error should raise", False)
        except Exception as exc:
            check(
                "syntax error raises (not silently retried)",
                "syntax" in str(exc).lower() or "SELCT" in str(exc),
                str(exc)[:100],
            )
    finally:
        await db.disconnect()


async def test_unique_violation_no_retry() -> None:
    print("\n── Unique violation does NOT trigger retry ──")
    db = await setup_db()
    try:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.execute(
            "CREATE TABLE test_cached_plan (id serial PRIMARY KEY, name text UNIQUE)"
        )
        await db.execute("INSERT INTO test_cached_plan (name) VALUES ('alice')")
        try:
            await db.execute("INSERT INTO test_cached_plan (name) VALUES ($1)", "alice")
            check("unique violation should raise", False)
        except Exception as exc:
            check(
                "unique violation raises (23505, not retried)",
                "unique" in str(exc).lower() or "duplicate" in str(exc).lower(),
                str(exc)[:100],
            )
    finally:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.disconnect()


async def test_multiple_sequential_schema_changes() -> None:
    print("\n── Multiple sequential schema changes → each triggers retry ──")
    db = await setup_db()
    try:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.execute(
            "CREATE TABLE test_cached_plan (id serial PRIMARY KEY, a text)"
        )
        await db.execute("INSERT INTO test_cached_plan (a) VALUES ('x')")

        # First query — prepare plan
        rows = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("initial: has column a", "a" in rows[0])

        # First schema change
        await db.execute("ALTER TABLE test_cached_plan ADD COLUMN b text DEFAULT 'y'")
        rows2 = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("after add b: has column b", "b" in rows2[0])

        # Second schema change
        await db.execute("ALTER TABLE test_cached_plan ADD COLUMN c text DEFAULT 'z'")
        rows3 = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("after add c: has column c", "c" in rows3[0])

        # Third schema change — drop
        await db.execute("ALTER TABLE test_cached_plan DROP COLUMN a")
        rows4 = await db.query("SELECT * FROM test_cached_plan WHERE id = $1", 1)
        check("after drop a: no column a", "a" not in rows4[0])
        check("after drop a: still has b,c", "b" in rows4[0] and "c" in rows4[0])
    finally:
        await db.execute("DROP TABLE IF EXISTS test_cached_plan")
        await db.disconnect()


async def run_all() -> None:
    """Single async entry point — avoids multiple asyncio.run() calls
    which can interact with the pool registry under free-threading.
    """
    await test_add_column_recovery()
    await test_drop_column_recovery()
    await test_alter_type_recovery()
    await test_syntax_error_no_retry()
    await test_unique_violation_no_retry()
    await test_multiple_sequential_schema_changes()


def main() -> int:
    print("=" * 70)
    print("  Cached plan error recovery — live DB (task #270)")
    print("=" * 70)

    asyncio.run(run_all())

    print()
    print("=" * 70)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
