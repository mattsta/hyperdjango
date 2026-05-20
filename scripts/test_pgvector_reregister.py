"""Deterministic regression lock: vector OID re-registration after in-session
CREATE EXTENSION.

On a fresh/empty database the ``vector`` type does not exist at connect time, so
its OID is unknown to the native decoder. When the app then runs
``CREATE EXTENSION vector`` and calls ``_db_register_vector``, subsequent SELECTs
of a vector column MUST decode to real ``list[float]`` values rather than an
undecoded string. Earlier pgvector tests relied on the extension being
preinstalled (``CREATE EXTENSION IF NOT EXISTS`` no-ops), so they never exercised
this path. This test forces it: DROP the extension, reconnect (so the OID is
genuinely absent at connect), CREATE it in-session, register, then decode.

# hyper-test: db_isolated
"""

import os
import sys

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hyperdjango_test")

RESULTS = {"passed": 0, "failed": 0}


def check(name: str, condition: bool, details: str = "") -> None:
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        print(f"  FAIL: {name} — {details}")


async def run_tests() -> None:
    from hyperdjango._hyperdjango_native import _db_register_vector

    from hyperdjango.database import Database

    # ── Force the "extension absent at connect" starting state ──────────
    prep = Database(DB_URL)
    await prep.connect()
    await prep.execute("DROP TABLE IF EXISTS reregister_docs CASCADE")
    # CASCADE removes any objects depending on the vector type so the extension
    # can be fully dropped, returning the DB to a vector-less state.
    await prep.execute("DROP EXTENSION IF EXISTS vector CASCADE")
    await prep.disconnect()

    # ── Reconnect: the vector type/OID is genuinely unknown at connect ──
    db = Database(DB_URL)
    await db.connect()

    # In-session CREATE EXTENSION + explicit OID re-registration (the fix).
    await db.execute("CREATE EXTENSION vector")
    _db_register_vector(db._pool_handle)

    await db.execute(
        """
        CREATE TABLE reregister_docs (
            id SERIAL PRIMARY KEY,
            embedding vector(4) NOT NULL
        )
        """
    )
    expected = [1.5, 2.5, 3.5, 4.0]
    await db.execute(
        "INSERT INTO reregister_docs (embedding) VALUES ($1::vector)",
        "[1.5, 2.5, 3.5, 4.0]",
    )

    row = await db.query_one("SELECT embedding FROM reregister_docs WHERE id = 1")
    got = row["embedding"]

    # The decisive assertions: decoded to a real float list, not a raw string.
    check("vector column decodes to a list (not str)", isinstance(got, list), repr(got))
    check(
        "decoded vector has 4 elements",
        isinstance(got, list) and len(got) == 4,
        repr(got),
    )
    check(
        "decoded elements are real floats matching input (float32 tolerance)",
        isinstance(got, list)
        and len(got) == len(expected)
        and all(isinstance(v, float) for v in got)
        and all(abs(a - e) < 1e-6 for a, e in zip(got, expected)),
        repr(got),
    )

    await db.execute("DROP TABLE IF EXISTS reregister_docs CASCADE")
    await db.disconnect()


def main() -> int:
    import asyncio

    print("=" * 60)
    print("pgvector OID re-registration (deterministic, DROP→CREATE in-session)")
    print("=" * 60)
    asyncio.run(run_tests())
    print(f"\n  {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    return 1 if RESULTS["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
