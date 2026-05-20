"""
Live tests for VectorField ORM save path (v0.14.18 task #201).

# hyper-test: db_isolated

Verifies that Model(embedding=[...]).save() correctly converts
list[float] to pgvector bracket-literal format via the new
TableMeta.vector_columns pre-computed set + Model._format_vector
helper, instead of pg.zig's generic PG array literal path which
produces `{...}` (rejected by pgvector).

Before this fix, every service storing vector embeddings had
to use raw SQL INSERT with $N::vector casts — a platform gap the
v0.14.18 service sweep audit (task #201) surfaced via
semantic_search/seed.py.
"""

import asyncio
import os
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model, VectorField

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, msg: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


class VectorDoc(TimestampMixin, Model):
    class Meta:
        table = "vf_orm_test_docs"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    embedding: list[float] = VectorField(dimensions=4)


async def run() -> None:
    db_url = os.environ.get(
        "DATABASE_URL", "postgres://localhost:5432/hyperdjango_test"
    )
    db = Database(db_url, max_size=2)
    await db.connect()
    set_db(db)

    # Fresh schema
    await db.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # pgvector has a dynamic OID — must be registered with pg.zig so
    # reads decode correctly. Write path only needs the text format,
    # so `_format_vector` + `$N::vector` cast would work without this
    # registration, but we also read rows back for verification.
    from hyperdjango._hyperdjango_native import _db_register_vector

    _db_register_vector(db._pool_handle)

    await db.execute("DROP TABLE IF EXISTS vf_orm_test_docs CASCADE")
    await db.execute("""
        CREATE TABLE vf_orm_test_docs (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            embedding vector(4) NOT NULL,
            created_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ
        )
    """)

    # ── meta has vector_columns pre-computed ─────────────────────────────
    print("\n── TableMeta.vector_columns pre-computation ──")
    check(
        "embedding in vector_columns",
        "embedding" in VectorDoc._meta.vector_columns,
    )
    check(
        "non-vector columns NOT in vector_columns",
        "title" not in VectorDoc._meta.vector_columns
        and "id" not in VectorDoc._meta.vector_columns,
    )

    # ── _format_vector helper (unit-ish — doesn't hit the DB) ────────────
    print("\n── Model._format_vector helper ──")
    check(
        "list[float] → bracket literal",
        VectorDoc._format_vector([0.1, 0.2, 0.3, 0.4]) == "[0.1,0.2,0.3,0.4]",
    )
    check(
        "tuple[float] → bracket literal",
        VectorDoc._format_vector((1.5, 2.5, 3.5, 4.5)) == "[1.5,2.5,3.5,4.5]",
    )
    check(
        "None passes through",
        VectorDoc._format_vector(None) is None,
    )
    check(
        "pre-formatted string passes through unchanged",
        VectorDoc._format_vector("[1,2,3,4]") == "[1,2,3,4]",
    )
    check(
        "int list formats correctly",
        VectorDoc._format_vector([1, 2, 3, 4]) == "[1,2,3,4]",
    )

    # ── INSERT via ORM save() ────────────────────────────────────────────
    print("\n── Model(embedding=[...]).save() INSERT path ──")
    doc1 = VectorDoc(title="doc1", embedding=[0.1, 0.2, 0.3, 0.4])
    await doc1.save()
    check("doc1 got auto-assigned id", doc1.id is not None and doc1.id > 0)

    # Fetch back via raw SQL (we're testing write, not read)
    row = await db.query_one(
        "SELECT title, embedding FROM vf_orm_test_docs WHERE id = $1",
        doc1.id,
    )
    check("doc1 row present", row is not None)
    check("doc1 title round-trips", row["title"] == "doc1")
    # pgvector stores float32, so Python float64 values suffer tiny
    # precision loss on round-trip. Tolerance comparison is correct.
    expected = [0.1, 0.2, 0.3, 0.4]
    got = row["embedding"]
    round_trip_ok = len(got) == len(expected) and all(
        abs(a - e) < 1e-6 for a, e in zip(got, expected)
    )
    check(
        "doc1 embedding round-trips (float32 tolerance)",
        round_trip_ok,
        f"got {got!r}",
    )

    # ── Cosine distance query works on ORM-written vector ────────────────
    print("\n── pgvector operators work on ORM-written rows ──")
    doc2 = VectorDoc(title="doc2", embedding=[0.9, 0.8, 0.7, 0.6])
    await doc2.save()
    doc3 = VectorDoc(title="doc3", embedding=[0.1, 0.2, 0.3, 0.5])  # close to doc1
    await doc3.save()

    closest = await db.query(
        "SELECT title, embedding <=> '[0.1,0.2,0.3,0.4]'::vector AS dist "
        "FROM vf_orm_test_docs ORDER BY dist LIMIT 2"
    )
    check("closest has 2 rows", len(closest) == 2)
    check("doc1 is closest (self-match)", closest[0]["title"] == "doc1")
    check("doc3 is second (similar embedding)", closest[1]["title"] == "doc3")

    # ── UPDATE path via save() on existing instance ──────────────────────
    print("\n── Model.save() UPDATE path with new embedding ──")
    doc1.embedding = [0.5, 0.5, 0.5, 0.5]
    await doc1.save()
    row = await db.query_one(
        "SELECT embedding FROM vf_orm_test_docs WHERE id = $1",
        doc1.id,
    )
    updated_ok = all(abs(a - 0.5) < 1e-6 for a in row["embedding"])
    check(
        "doc1 embedding updated via ORM (float32 tolerance)",
        updated_ok,
        f"got {row['embedding']!r}",
    )

    # ── Many rows via ORM — realistic seed-style workload ───────────────
    print("\n── Bulk seed-style insert loop ──")
    for i in range(20):
        v = [i * 0.1, i * 0.1 + 0.1, i * 0.1 + 0.2, i * 0.1 + 0.3]
        doc = VectorDoc(title=f"bulk_{i}", embedding=v)
        await doc.save()
    bulk_count = await db.query_val(
        "SELECT COUNT(*) FROM vf_orm_test_docs WHERE title LIKE 'bulk_%'"
    )
    check("20 bulk rows inserted via ORM", bulk_count == 20)

    # Verify bulk embeddings round-trip correctly (float32 tolerance)
    bulk_rows = await db.query(
        "SELECT title, embedding FROM vf_orm_test_docs WHERE title LIKE 'bulk_%' ORDER BY title"
    )
    bulk_0 = next(r for r in bulk_rows if r["title"] == "bulk_0")
    expected_0 = [0.0, 0.1, 0.2, 0.3]
    bulk_0_ok = all(abs(a - e) < 1e-6 for a, e in zip(bulk_0["embedding"], expected_0))
    check(
        "bulk_0 embedding preserved (float32 tolerance)",
        bulk_0_ok,
        f"got {bulk_0['embedding']}",
    )
    # bulk_1, bulk_10..bulk_19, bulk_2..bulk_9 sorted alphabetically
    bulk_19 = next(r for r in bulk_rows if r["title"] == "bulk_19")
    # 19 * 0.1 = 1.9000000000000001 in binary float — need tolerance
    expected = [19 * 0.1, 19 * 0.1 + 0.1, 19 * 0.1 + 0.2, 19 * 0.1 + 0.3]
    actual = bulk_19["embedding"]
    all_close = all(abs(a - e) < 1e-6 for a, e in zip(actual, expected))
    check(
        "bulk_19 embedding preserved (within float tolerance)",
        all_close,
        f"got {actual}",
    )

    # Cleanup
    await db.execute("DROP TABLE vf_orm_test_docs CASCADE")
    await db.disconnect()


def main() -> int:
    print("=" * 60)
    print("VectorField ORM save() path — live tests")
    print("=" * 60)

    asyncio.run(run())

    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
