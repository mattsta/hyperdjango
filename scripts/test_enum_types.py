#!/usr/bin/env python3
"""Test custom PostgreSQL enum type registration and querying.

Tests:
1. CREATE TYPE with enum values
2. Register enum type via _db_register_enum
3. INSERT and SELECT enum values — verify Python gets strings
4. discover_enums() — auto-register all enum types
5. Multiple enum types simultaneously
6. ALTER TYPE ADD VALUE — re-registration picks up new values
7. Enum arrays
8. Python enum.Enum mapping
9. Performance benchmark
"""

# hyper-test: db_isolated

import enum
import os
import sys
import time

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_execute,
    _db_list_enums,
    _db_query,
    _db_register_enum,
)


def get_conn_str():
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    # Same resolution as test_runner._DB_USER: role defaults to the login
    # user, never a hardcoded dev username (fails on any other machine).
    user = os.environ.get("PGUSER") or os.environ.get("USER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def main():
    conn_str = get_conn_str()
    pool = _db_configure(conn_str, 2)
    print(f"Connected: pool_handle={pool}")

    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Setup: Create enum types ──────────────────────────────────────────
    print("\n=== Setup: Creating enum types ===")
    try:
        _db_execute(pool, "DROP TABLE IF EXISTS test_enum_table", [])
        _db_execute(pool, "DROP TYPE IF EXISTS mood", [])
        _db_execute(pool, "DROP TYPE IF EXISTS status_type", [])
        _db_execute(pool, "DROP TYPE IF EXISTS priority_level", [])
    except RuntimeError:
        pass

    _db_execute(
        pool, "CREATE TYPE mood AS ENUM ('happy', 'sad', 'neutral', 'excited')", []
    )
    _db_execute(
        pool,
        "CREATE TYPE status_type AS ENUM ('active', 'inactive', 'pending', 'archived')",
        [],
    )
    _db_execute(
        pool,
        "CREATE TYPE priority_level AS ENUM ('low', 'medium', 'high', 'critical')",
        [],
    )
    print("Created 3 enum types: mood, status_type, priority_level")

    # Create table using enum types
    _db_execute(
        pool,
        """
        CREATE TABLE test_enum_table (
            id SERIAL PRIMARY KEY,
            current_mood mood NOT NULL,
            status status_type DEFAULT 'pending',
            priority priority_level DEFAULT 'medium'
        )
    """,
        [],
    )
    print("Created test_enum_table")

    # ── Test 1: Register single enum type ─────────────────────────────────
    print("\n=== Test 1: Register single enum type ===")
    oid = _db_register_enum(pool, "mood")
    check("register mood returns OID > 0", oid > 0, f"got {oid}")
    print(f"  mood OID: {oid}")

    # ── Test 2: Query enum values after registration ──────────────────────
    print("\n=== Test 2: Query enum values ===")
    _db_execute(
        pool,
        "INSERT INTO test_enum_table (current_mood, status, priority) VALUES ('happy', 'active', 'high')",
        [],
    )
    _db_execute(
        pool,
        "INSERT INTO test_enum_table (current_mood, status, priority) VALUES ('sad', 'inactive', 'low')",
        [],
    )
    _db_execute(
        pool,
        "INSERT INTO test_enum_table (current_mood, status, priority) VALUES ('excited', 'pending', 'critical')",
        [],
    )

    rows = _db_query(pool, "SELECT current_mood FROM test_enum_table ORDER BY id", [])
    check("got 3 rows", len(rows) == 3, f"got {len(rows)}")
    check("first mood is 'happy'", rows[0][0] == "happy", f"got {rows[0][0]!r}")
    check("second mood is 'sad'", rows[1][0] == "sad", f"got {rows[1][0]!r}")
    check("third mood is 'excited'", rows[2][0] == "excited", f"got {rows[2][0]!r}")
    check(
        "mood values are strings",
        isinstance(rows[0][0], str),
        f"got {type(rows[0][0])}",
    )

    # ── Test 3: Register all enum types ───────────────────────────────────
    print("\n=== Test 3: Register multiple enum types ===")
    status_oid = _db_register_enum(pool, "status_type")
    priority_oid = _db_register_enum(pool, "priority_level")
    check("status_type OID > 0", status_oid > 0, f"got {status_oid}")
    check("priority_level OID > 0", priority_oid > 0, f"got {priority_oid}")
    check(
        "all OIDs are distinct",
        len({oid, status_oid, priority_oid}) == 3,
        f"oids: {oid}, {status_oid}, {priority_oid}",
    )

    # Query multiple enum columns
    rows = _db_query(
        pool,
        "SELECT current_mood, status, priority FROM test_enum_table ORDER BY id",
        [],
    )
    check(
        "row 1 all enums correct",
        rows[0] == ("happy", "active", "high"),
        f"got {rows[0]}",
    )
    check(
        "row 3 all enums correct",
        rows[2] == ("excited", "pending", "critical"),
        f"got {rows[2]}",
    )

    # ── Test 4: discover_enums — auto-register all ────────────────────────
    print("\n=== Test 4: discover_enums ===")
    enums = _db_list_enums(pool)
    check("discover_enums returns dict", isinstance(enums, dict), f"got {type(enums)}")
    check("'mood' in discovered enums", "mood" in enums, f"keys: {list(enums.keys())}")
    check("'status_type' in discovered enums", "status_type" in enums)
    check("'priority_level' in discovered enums", "priority_level" in enums)
    if "mood" in enums:
        check(
            "mood labels correct",
            enums["mood"] == ["happy", "sad", "neutral", "excited"],
            f"got {enums['mood']}",
        )
    if "status_type" in enums:
        check(
            "status_type labels correct",
            enums["status_type"] == ["active", "inactive", "pending", "archived"],
            f"got {enums['status_type']}",
        )

    # ── Test 5: Re-registration (simulates ALTER TYPE ADD VALUE) ──────────
    print("\n=== Test 5: Re-registration after ALTER TYPE ===")
    _db_execute(pool, "ALTER TYPE mood ADD VALUE 'angry'", [])
    oid2 = _db_register_enum(pool, "mood")
    check("re-registration returns same OID", oid2 == oid, f"got {oid2}")

    # Verify new value works
    _db_execute(pool, "INSERT INTO test_enum_table (current_mood) VALUES ('angry')", [])
    rows = _db_query(
        pool,
        "SELECT current_mood FROM test_enum_table WHERE current_mood = 'angry'",
        [],
    )
    check(
        "new enum value 'angry' queryable",
        len(rows) == 1 and rows[0][0] == "angry",
        f"got {rows}",
    )

    # ── Test 6: Non-existent enum type ────────────────────────────────────
    print("\n=== Test 6: Non-existent enum type ===")
    bad_oid = _db_register_enum(pool, "nonexistent_type")
    check("non-existent type returns 0", bad_oid == 0, f"got {bad_oid}")

    # ── Test 7: Python enum.Enum integration ──────────────────────────────
    print("\n=== Test 7: Python enum.Enum integration ===")

    class Mood(enum.Enum):
        HAPPY = "happy"
        SAD = "sad"
        NEUTRAL = "neutral"
        EXCITED = "excited"
        ANGRY = "angry"

    # Query returns strings — Python-side can convert
    rows = _db_query(
        pool,
        "SELECT current_mood FROM test_enum_table WHERE current_mood = 'happy'",
        [],
    )
    if rows:
        py_enum = Mood(rows[0][0])
        check("string → Python enum works", py_enum == Mood.HAPPY, f"got {py_enum}")
        check("Python enum value matches", py_enum.value == "happy")

    # ── Test 8: Performance benchmark ─────────────────────────────────────
    print("\n=== Test 8: Performance benchmark ===")

    # Insert more rows for benchmark
    for i in range(100):
        mood = ["happy", "sad", "neutral", "excited"][i % 4]
        _db_execute(
            pool, f"INSERT INTO test_enum_table (current_mood) VALUES ('{mood}')", []
        )

    # Benchmark: SELECT with registered enum OID
    iterations = 1000
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        rows = _db_query(
            pool,
            "SELECT current_mood, status, priority FROM test_enum_table LIMIT 10",
            [],
        )
    t1 = time.perf_counter_ns()
    elapsed_us = (t1 - t0) / 1000
    per_query_us = elapsed_us / iterations
    print(
        f"  {iterations} queries of 10 enum rows: {elapsed_us:.0f}μs total, {per_query_us:.1f}μs/query"
    )

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    _db_execute(pool, "DROP TABLE IF EXISTS test_enum_table", [])
    _db_execute(pool, "DROP TYPE IF EXISTS mood", [])
    _db_execute(pool, "DROP TYPE IF EXISTS status_type", [])
    _db_execute(pool, "DROP TYPE IF EXISTS priority_level", [])
    _db_close_pool(pool)
    print("Cleaned up")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All enum type tests passed!")


if __name__ == "__main__":
    main()
