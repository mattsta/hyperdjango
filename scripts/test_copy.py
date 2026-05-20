#!/usr/bin/env python3
"""
Test COPY protocol through pg.zig native extension.

Tests COPY TO STDOUT and COPY FROM STDIN via the native Zig COPY
wire protocol implementation.

Run: uv run hyper-test copy
"""

# hyper-test: db_isolated

import os
import time
import traceback
from collections.abc import Callable

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_conn_acquire,
    _db_conn_release,
    _db_copy_from,
    _db_copy_to,
    _db_execute,
    _db_query,
)

from hyperdjango.testkit import check, finish, run_main


def _conn_str() -> str:
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", os.environ.get("USER", "postgres"))
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


h = _db_configure(_conn_str(), 2)

# Setup test table
_db_execute(h, "DROP TABLE IF EXISTS copy_test", [])
_db_execute(
    h, "CREATE TABLE copy_test (id SERIAL PRIMARY KEY, name TEXT, value INTEGER)", []
)

# Insert test data
for i in range(100):
    _db_execute(
        h, "INSERT INTO copy_test (name, value) VALUES ($1, $2)", [f"item_{i}", str(i)]
    )


def test_copy_to():
    """Test COPY TO STDOUT."""
    pinned = _db_conn_acquire(h)
    rows = _db_copy_to(pinned, "COPY copy_test TO STDOUT")
    _db_conn_release(pinned)

    assert len(rows) == 100, f"Expected 100 rows, got {len(rows)}"
    # Each row is tab-delimited: "id\tname\tvalue\n"
    first = rows[0]
    assert "\t" in first, f"Expected tab-delimited row, got: {first!r}"
    parts = first.strip().split("\t")
    assert len(parts) == 3, f"Expected 3 columns, got {len(parts)}: {parts}"
    print(f"  COPY TO: OK ({len(rows)} rows, first={first.strip()!r})")


def test_copy_from():
    """Test COPY FROM STDIN."""
    # Create a separate table for import
    _db_execute(h, "DROP TABLE IF EXISTS copy_import", [])
    _db_execute(h, "CREATE TABLE copy_import (name TEXT, score INTEGER)", [])

    # Build rows in COPY text format (tab-delimited, newline-terminated)
    import_rows = [f"user_{i}\t{i * 10}\n" for i in range(50)]

    pinned = _db_conn_acquire(h)
    count = _db_copy_from(pinned, "COPY copy_import FROM STDIN", import_rows)
    _db_conn_release(pinned)

    assert count == 50, f"Expected 50 rows imported, got {count}"

    # Verify data
    rows = _db_query(h, "SELECT COUNT(*) FROM copy_import", [])
    assert rows[0][0] == 50, f"Expected 50 rows in table, got {rows[0][0]}"

    rows = _db_query(
        h, "SELECT name, score FROM copy_import ORDER BY score LIMIT 3", []
    )
    assert rows[0][0] == "user_0"
    assert rows[0][1] == 0
    assert rows[1][0] == "user_1"
    assert rows[1][1] == 10

    _db_execute(h, "DROP TABLE copy_import", [])
    print(f"  COPY FROM: OK ({count} rows imported)")


def test_copy_cursor_api():
    """Test COPY through PgZigCursor.copy() context manager."""
    from hyperdjango.db.pgzig_connection import PgZigConnection

    conn = PgZigConnection(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "hyperdjango_test"),
        user=os.environ.get("PGUSER", os.environ.get("USER", "postgres")),
    )
    conn.connect()
    conn.autocommit = True

    # COPY TO
    cursor = conn.cursor()
    with cursor.copy("COPY copy_test TO STDOUT") as copy:
        rows = list(copy.rows())
    assert len(rows) == 100
    print(f"  cursor.copy() TO: OK ({len(rows)} rows)")

    # COPY FROM
    _db_execute(h, "DROP TABLE IF EXISTS copy_cursor_import", [])
    _db_execute(h, "CREATE TABLE copy_cursor_import (name TEXT, value INTEGER)", [])

    with cursor.copy("COPY copy_cursor_import FROM STDIN") as copy:
        for i in range(25):
            copy.write_row(f"item_{i}\t{i}\n")

    rows = _db_query(h, "SELECT COUNT(*) FROM copy_cursor_import", [])
    assert rows[0][0] == 25
    _db_execute(h, "DROP TABLE copy_cursor_import", [])
    print("  cursor.copy() FROM: OK (25 rows)")

    cursor.close()
    conn.close()


def bench_copy_vs_insert():
    """Benchmark COPY FROM vs row-by-row INSERT."""
    n = 5000

    # INSERT benchmark
    _db_execute(h, "DROP TABLE IF EXISTS bench_insert", [])
    _db_execute(h, "CREATE TABLE bench_insert (name TEXT, value INTEGER)", [])
    start = time.perf_counter()
    for i in range(n):
        _db_execute(
            h,
            "INSERT INTO bench_insert (name, value) VALUES ($1, $2)",
            [f"item_{i}", str(i)],
        )
    insert_time = time.perf_counter() - start
    _db_execute(h, "DROP TABLE bench_insert", [])

    # COPY benchmark
    _db_execute(h, "DROP TABLE IF EXISTS bench_copy", [])
    _db_execute(h, "CREATE TABLE bench_copy (name TEXT, value INTEGER)", [])
    rows = [f"item_{i}\t{i}\n" for i in range(n)]
    pinned = _db_conn_acquire(h)
    start = time.perf_counter()
    _db_copy_from(pinned, "COPY bench_copy FROM STDIN", rows)
    copy_time = time.perf_counter() - start
    _db_conn_release(pinned)
    _db_execute(h, "DROP TABLE bench_copy", [])

    ratio = insert_time / copy_time if copy_time > 0 else 0
    print(f"\n  Benchmark ({n} rows):")
    print(f"    INSERT:  {insert_time:.3f}s ({n / insert_time:.0f} rows/sec)")
    print(f"    COPY:    {copy_time:.3f}s ({n / copy_time:.0f} rows/sec)")
    print(f"    Speedup: {ratio:.1f}x")


def _run(name: str, fn: Callable[[], None]) -> bool:
    """Run one assert-battery function and record a single pass/fail.

    The asserts inside each function abort that function on the first bad
    value — that is this file's contract — so a failure is reported once, at
    function granularity, and the caller stops.
    """
    try:
        fn()
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False
    return check(name, True)


def main() -> bool:
    print("Testing COPY protocol (native Zig pg.zig wire protocol):")
    stages: tuple[tuple[str, Callable[[], None]], ...] = (
        ("COPY TO STDOUT", test_copy_to),
        ("COPY FROM STDIN", test_copy_from),
        ("cursor.copy() context manager", test_copy_cursor_api),
        ("COPY vs INSERT benchmark", bench_copy_vs_insert),
    )
    for name, fn in stages:
        if not _run(name, fn):
            return finish()

    # Cleanup
    _db_execute(h, "DROP TABLE IF EXISTS copy_test", [])
    _db_close_pool(h)
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
