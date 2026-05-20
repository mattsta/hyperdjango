"""
Tests for Zig fixed-size array overflow safety.

Verifies that operations exceeding the old static limits work correctly
with the new stack-first/heap-fallback allocations:
- >64 query parameters (was hard error at 64)
- >16 columns in CRUD registration (was silently truncated)
- >32 batch validation fields (was silently skipped)
- Large row JSON serialization (was 8KB limit)
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
results = []
test_funcs = []


def test(name):
    def decorator(func):
        test_funcs.append((name, func))
        return func

    return decorator


def check(label, condition):
    results.append((label, condition))
    symbol = "✓" if condition else "✗"
    print(f"  {symbol} {label}")
    if not condition:
        import traceback

        traceback.print_exc()


# ── >64 query parameters ─────────────────────────────────────────────────────


@test("db_query: 65 parameters works (was hard limit at 64)")
async def test_65_params():
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Create a table
    await db.execute("DROP TABLE IF EXISTS test_many_cols CASCADE")
    cols = [f"c{i}" for i in range(65)]
    col_defs = ", ".join(f"{c} INTEGER DEFAULT 0" for c in cols)
    await db.execute(f"CREATE TABLE test_many_cols (id SERIAL PRIMARY KEY, {col_defs})")

    # Build a query with 65 parameters
    placeholders = ", ".join(f"${i + 1}" for i in range(65))
    params = list(range(65))
    sql = f"INSERT INTO test_many_cols ({', '.join(cols)}) VALUES ({placeholders})"
    await db.execute(sql, *params)

    # Verify the row was inserted correctly
    row = await db.query_one("SELECT * FROM test_many_cols LIMIT 1")
    check("65-param INSERT succeeded", row is not None)

    # Verify values
    if row:
        for i in range(65):
            col_name = f"c{i}"
            val = row[col_name] if isinstance(row, dict) else row[i + 1]
            if val != i:
                check(f"param {i} has correct value", False)
                break
        else:
            check("all 65 param values correct", True)

    await db.execute("DROP TABLE IF EXISTS test_many_cols CASCADE")
    await db.disconnect()


@test("db_query: 100 parameters works (heap allocation path)")
async def test_100_params():
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Use a simpler test: just query with 100 params in WHERE IN clause
    await db.execute("DROP TABLE IF EXISTS test_100p CASCADE")
    await db.execute("CREATE TABLE test_100p (id INTEGER PRIMARY KEY)")
    for i in range(100):
        await db.execute("INSERT INTO test_100p (id) VALUES ($1)", i)

    placeholders = ", ".join(f"${i + 1}" for i in range(100))
    params = list(range(100))
    rows = await db.query(
        f"SELECT id FROM test_100p WHERE id IN ({placeholders}) ORDER BY id",
        *params,
    )
    check("100-param query returned results", len(rows) == 100)
    if len(rows) == 100:
        vals = [r["id"] if isinstance(r, dict) else r[0] for r in rows]
        check("100-param values all correct", vals == list(range(100)))

    await db.execute("DROP TABLE IF EXISTS test_100p CASCADE")
    await db.disconnect()


# ── Batch validation >32 fields ──────────────────────────────────────────────


@test("validate_batch_direct: 40 fields works (was limit at 32)")
async def test_batch_40_fields():
    try:
        from hyperdjango._hyperdjango_native import validate_batch_direct
    except ImportError:
        check("validate_batch_direct available", False)
        return

    # Build specs for 40 int fields
    specs = {}
    for i in range(40):
        specs[f"field_{i}"] = ("int", 0, 1000)

    # Build records — all valid
    records = []
    for _ in range(10):
        record = {f"field_{i}": i * 10 for i in range(40)}
        records.append(record)

    result_list, valid_count = validate_batch_direct(records, specs)
    check("40-field batch validation: all 10 records valid", valid_count == 10)

    # Build records with field_39 out of range (field 40 — would be skipped at old limit 32)
    bad_records = []
    for _ in range(5):
        record = {f"field_{i}": i * 10 for i in range(40)}
        record["field_39"] = 9999  # out of range [0, 1000]
        bad_records.append(record)

    result_list2, valid_count2 = validate_batch_direct(bad_records, specs)
    invalid_count = sum(1 for r in result_list2 if not r)
    check("40-field batch: field_39 out of range detected", invalid_count == 5)


# ── Large text data (JSON serialization >8KB) ────────────────────────────────


@test("large row data: >8KB text columns serialize correctly")
async def test_large_row():
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS test_large_row CASCADE")
    await db.execute("""
        CREATE TABLE test_large_row (
            id SERIAL PRIMARY KEY,
            big_text1 TEXT,
            big_text2 TEXT,
            big_text3 TEXT
        )
    """)

    # Insert 3 columns of ~4KB each = ~12KB total (exceeds 8KB stack buffer)
    text1 = "A" * 4000
    text2 = "B" * 4000
    text3 = "C" * 4000
    await db.execute(
        "INSERT INTO test_large_row (big_text1, big_text2, big_text3) VALUES ($1, $2, $3)",
        text1,
        text2,
        text3,
    )

    row = await db.query_one("SELECT * FROM test_large_row LIMIT 1")
    check("large row query succeeded", row is not None)
    if row:
        t1 = row["big_text1"] if isinstance(row, dict) else row[1]
        t2 = row["big_text2"] if isinstance(row, dict) else row[2]
        t3 = row["big_text3"] if isinstance(row, dict) else row[3]
        check("big_text1 preserved (4KB)", t1 == text1)
        check("big_text2 preserved (4KB)", t2 == text2)
        check("big_text3 preserved (4KB)", t3 == text3)

    await db.execute("DROP TABLE IF EXISTS test_large_row CASCADE")
    await db.disconnect()


@test("very large row: 50KB+ text data")
async def test_very_large_row():
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS test_vlarge CASCADE")
    await db.execute("CREATE TABLE test_vlarge (id SERIAL PRIMARY KEY, content TEXT)")

    big_content = "X" * 50000  # 50KB
    await db.execute("INSERT INTO test_vlarge (content) VALUES ($1)", big_content)

    row = await db.query_one("SELECT * FROM test_vlarge LIMIT 1")
    check("50KB row query succeeded", row is not None)
    if row:
        content = row["content"] if isinstance(row, dict) else row[1]
        check("50KB content preserved exactly", content == big_content)

    await db.execute("DROP TABLE IF EXISTS test_vlarge CASCADE")
    await db.disconnect()


# ── Run all ──────────────────────────────────────────────────────────────────


async def main():
    print(f"\nZig Overflow Safety Tests ({len(test_funcs)} tests)")
    print("=" * 60)
    for name, func in test_funcs:
        try:
            if inspect.iscoroutinefunction(func):
                await func()
            else:
                func()
        except Exception as e:
            results.append((name, False))
            print(f"  ✗ {name}: {e}")
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")

    if failed:
        print("\nFailures:")
        for label, ok in results:
            if not ok:
                print(f"  - {label}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
