#!/usr/bin/env python3
"""Test pg.zig with >8 params (dynamic path)."""

# hyper-test: db_isolated

import asyncio
import os
import traceback

from hyperdjango.database import Database, set_db
from hyperdjango.testkit import check, finish, run_main

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


async def main() -> bool:
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    print(f"Backend: {db.backend}")

    await db.execute("DROP TABLE IF EXISTS many_params_test CASCADE")
    await db.execute("""
        CREATE TABLE many_params_test (
            id INTEGER PRIMARY KEY,
            name VARCHAR(100),
            val FLOAT
        )
    """)

    try:
        # 6 params — fast path (should work)
        print("Testing 6 params (fast path)...")
        await db.execute(
            "INSERT INTO many_params_test (id, name, val) VALUES ($1, $2, $3), ($4, $5, $6)",
            1,
            "a",
            1.1,
            2,
            "b",
            2.2,
        )
        check("6 params (fast path)", True)

        # 9 params — dynamic path
        print("Testing 9 params (dynamic path)...")
        await db.execute(
            "INSERT INTO many_params_test (id, name, val) VALUES ($1, $2, $3), ($4, $5, $6), ($7, $8, $9)",
            3,
            "c",
            3.3,
            4,
            "d",
            4.4,
            5,
            "e",
            5.5,
        )
        check("9 params (dynamic path)", True)

        # 15 params — dynamic path
        print("Testing 15 params (dynamic path)...")
        await db.execute(
            "INSERT INTO many_params_test (id, name, val) VALUES "
            "($1, $2, $3), ($4, $5, $6), ($7, $8, $9), ($10, $11, $12), ($13, $14, $15)",
            6,
            "f",
            6.6,
            7,
            "g",
            7.7,
            8,
            "h",
            8.8,
            9,
            "i",
            9.9,
            10,
            "j",
            10.0,
        )
        check("15 params (dynamic path)", True)

        # Verify
        rows = await db.query("SELECT COUNT(*) FROM many_params_test")
        count = rows[0]["count"]
        print(f"  Total rows: {count}")
        check("all 10 rows inserted", count == 10, f"expected 10, got {count}")

    except Exception as e:
        check("no exception during param batches", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        await db.execute("DROP TABLE IF EXISTS many_params_test CASCADE")
        await db.disconnect()

    return finish()


def _main() -> bool:
    return asyncio.run(main())


if __name__ == "__main__":
    run_main(_main)
