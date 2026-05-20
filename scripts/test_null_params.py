#!/usr/bin/env python3
"""
Test that Python None is correctly handled as SQL NULL through pg.zig wire protocol.

Usage:
    uv run hyper-test null_params
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.database import Database, set_db

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


async def main():
    print("=" * 60)
    print("NULL Parameter Tests (pg.zig wire protocol)")
    print("=" * 60)

    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    check("connected via pg.zig", db.backend == "pgzig")

    # Setup test table
    await db.execute("DROP TABLE IF EXISTS null_test CASCADE")
    await db.execute("""
        CREATE TABLE null_test (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100),
            value INTEGER,
            active BOOLEAN,
            notes TEXT
        )
    """)

    try:
        # ── INSERT with NULL params ──────────────────────────────────
        print("\n--- INSERT with NULL ---")

        await db.execute(
            "INSERT INTO null_test (name, value, active, notes) VALUES ($1, $2, $3, $4)",
            "alice",
            42,
            True,
            "has all values",
        )
        check("insert with all values", True)

        await db.execute(
            "INSERT INTO null_test (name, value, active, notes) VALUES ($1, $2, $3, $4)",
            "bob",
            None,
            True,
            None,
        )
        check("insert with None integer", True)

        await db.execute(
            "INSERT INTO null_test (name, value, active, notes) VALUES ($1, $2, $3, $4)",
            None,
            None,
            None,
            None,
        )
        check("insert with all None", True)

        # ── Verify NULL storage ──────────────────────────────────────
        print("\n--- Verify NULL storage ---")

        rows = await db.query(
            "SELECT name, value, active, notes FROM null_test ORDER BY id"
        )
        check("3 rows inserted", len(rows) == 3)

        # Row 1: all values present
        r1 = rows[0]
        check("row 1 name = alice", r1["name"] == "alice")
        check("row 1 value = 42", r1["value"] == 42)
        check("row 1 active = True", r1["active"] is True)
        check("row 1 notes present", r1["notes"] == "has all values")

        # Row 2: None integer and None text
        r2 = rows[1]
        check("row 2 name = bob", r2["name"] == "bob")
        check("row 2 value is NULL", r2["value"] is None)
        check("row 2 active = True", r2["active"] is True)
        check("row 2 notes is NULL", r2["notes"] is None)

        # Row 3: all None
        r3 = rows[2]
        check("row 3 name is NULL", r3["name"] is None)
        check("row 3 value is NULL", r3["value"] is None)
        check("row 3 active is NULL", r3["active"] is None)
        check("row 3 notes is NULL", r3["notes"] is None)

        # ── SELECT with NULL comparison ──────────────────────────────
        print("\n--- SELECT with NULL ---")

        null_rows = await db.query("SELECT id, name FROM null_test WHERE value IS NULL")
        check("IS NULL filter works", len(null_rows) == 2)

        not_null = await db.query(
            "SELECT id, name FROM null_test WHERE value IS NOT NULL"
        )
        check("IS NOT NULL filter works", len(not_null) == 1)
        check("not null row is alice", not_null[0]["name"] == "alice")

        # ── UPDATE with NULL ─────────────────────────────────────────
        print("\n--- UPDATE with NULL ---")

        await db.execute(
            "UPDATE null_test SET notes = $1 WHERE name = $2", None, "alice"
        )
        row = await db.query_one("SELECT notes FROM null_test WHERE name = $1", "alice")
        check("update to NULL works", row["notes"] is None)

        await db.execute(
            "UPDATE null_test SET notes = $1 WHERE name = $2", "restored", "alice"
        )
        row2 = await db.query_one(
            "SELECT notes FROM null_test WHERE name = $1", "alice"
        )
        check("update from NULL works", row2["notes"] == "restored")

        # ── query_one returns dict ───────────────────────────────────
        print("\n--- query_one returns dict ---")

        row = await db.query_one(
            "SELECT name, value FROM null_test WHERE name = $1", "alice"
        )
        check("query_one returns dict", isinstance(row, dict))
        check("query_one dict has name key", "name" in row)
        check("query_one dict has value key", "value" in row)

        # ── query_val returns scalar ─────────────────────────────────
        print("\n--- query_val returns scalar ---")

        count = await db.query_val("SELECT COUNT(*) FROM null_test")
        check("query_val returns int", count == 3)

        name = await db.query_val("SELECT name FROM null_test WHERE id = $1", 1)
        check("query_val returns string", name == "alice")

        # ── execute returns affected-row count as an int ─────────────
        print("\n--- execute returns rowcount ---")

        result = await db.execute(
            "UPDATE null_test SET active = $1 WHERE name = $2", False, "bob"
        )
        check("execute returns int rowcount", isinstance(result, int))
        check("execute rowcount is 1", result == 1)

        # ── Bool params ──────────────────────────────────────────────
        print("\n--- Bool params ---")

        await db.execute(
            "INSERT INTO null_test (name, value, active) VALUES ($1, $2, $3)",
            "truthy",
            1,
            True,
        )
        await db.execute(
            "INSERT INTO null_test (name, value, active) VALUES ($1, $2, $3)",
            "falsy",
            0,
            False,
        )
        true_row = await db.query_one(
            "SELECT active FROM null_test WHERE name = $1", "truthy"
        )
        false_row = await db.query_one(
            "SELECT active FROM null_test WHERE name = $1", "falsy"
        )
        check("True param stored correctly", true_row["active"] is True)
        check("False param stored correctly", false_row["active"] is False)

    finally:
        await db.execute("DROP TABLE IF EXISTS null_test CASCADE")
        await db.disconnect()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
