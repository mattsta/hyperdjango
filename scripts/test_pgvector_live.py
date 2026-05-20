"""Live database tests for pgvector integration using pg.zig native driver.

Tests actual PostgreSQL pgvector operations:
- CREATE TABLE with vector(N) columns
- INSERT vectors as text format
- Vector distance queries (cosine, L2, inner product)
- HNSW and IVFFlat index creation
- ORDER BY distance for KNN search
- OID detection for vector type

Requires: PostgreSQL 18 with pgvector extension installed.

Usage:
    uv run hyper-test pgvector_live
"""

# hyper-test: db_isolated

import os
import sys

RESULTS = {"passed": 0, "failed": 0, "errors": []}

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hyperdjango_test")


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    import asyncio

    return asyncio.run(run_tests())


async def run_tests():
    from hyperdjango.database import Database

    print("=" * 60)
    print("pgvector Live Database Tests (PostgreSQL 18 + pg.zig)")
    print("=" * 60)

    db = Database(DB_URL)
    await db.connect()

    # ── Setup ──────────────────────────────────────────────────────

    print("\n--- Setup ---")

    await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    check("pgvector extension created", True)

    # Re-register vector OID now that the extension exists
    # (on empty isolated DBs, the OID wasn't known at connect time)
    from hyperdjango._hyperdjango_native import _db_register_vector

    _db_register_vector(db._pool_handle)

    rows = await db.query("SELECT version() AS v")
    version = rows[0]["v"]
    check("PostgreSQL 18", "PostgreSQL 18" in version, version)

    await db.execute("DROP TABLE IF EXISTS test_documents CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_products CASCADE")

    # ── Create Table with Vector Column ───────────────────────────

    print("\n--- CREATE TABLE with vector ---")

    await db.execute("""
        CREATE TABLE test_documents (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            embedding vector(4) NOT NULL
        )
    """)
    check("create table with vector(4)", True)

    # ── Insert Vectors ────────────────────────────────────────────

    print("\n--- INSERT vectors ---")

    test_data = [
        ("Doc A", "tech", "[1.0, 0.0, 0.0, 0.0]"),
        ("Doc B", "tech", "[0.0, 1.0, 0.0, 0.0]"),
        ("Doc C", "science", "[0.0, 0.0, 1.0, 0.0]"),
        ("Doc D", "science", "[0.0, 0.0, 0.0, 1.0]"),
        ("Doc E", "tech", "[0.707, 0.707, 0.0, 0.0]"),
        ("Doc F", "science", "[0.5, 0.5, 0.5, 0.5]"),
    ]

    for title, category, embedding in test_data:
        await db.execute(
            "INSERT INTO test_documents (title, category, embedding) VALUES ($1, $2, $3::vector)",
            title,
            category,
            embedding,
        )
    check("inserted 6 vectors", True)

    rows = await db.query("SELECT count(*) AS cnt FROM test_documents")
    check("row count is 6", rows[0]["cnt"] == 6, repr(rows[0]["cnt"]))

    # ── Read Vectors Back ─────────────────────────────────────────

    print("\n--- SELECT vectors ---")

    rows = await db.query(
        "SELECT title, embedding FROM test_documents WHERE title = 'Doc A'"
    )
    check("select vector returns data", len(rows) == 1)
    check("title is correct", rows[0]["title"] == "Doc A", repr(rows[0]["title"]))
    check(
        "embedding is returned",
        rows[0]["embedding"] is not None,
        repr(rows[0]["embedding"]),
    )

    # ── Cosine Distance Queries ───────────────────────────────────

    print("\n--- Cosine Distance (<=>)  ---")

    query_vec = "[1.0, 0.0, 0.0, 0.0]"
    rows = await db.query(
        f"SELECT title, embedding <=> '{query_vec}'::vector AS dist "
        f"FROM test_documents ORDER BY dist LIMIT 3"
    )
    check("cosine query returns 3 rows", len(rows) == 3)
    check("cosine nearest is Doc A", rows[0]["title"] == "Doc A", repr(rows[0]))
    check("cosine second is Doc E", rows[1]["title"] == "Doc E", repr(rows[1]))
    check(
        "cosine dist of self is 0", abs(rows[0]["dist"]) < 1e-6, repr(rows[0]["dist"])
    )

    # ── L2 Distance Queries ───────────────────────────────────────

    print("\n--- L2 Distance (<->)  ---")

    rows = await db.query(
        f"SELECT title, embedding <-> '{query_vec}'::vector AS dist "
        f"FROM test_documents ORDER BY dist LIMIT 3"
    )
    check("l2 query returns 3 rows", len(rows) == 3)
    check("l2 nearest is Doc A", rows[0]["title"] == "Doc A", repr(rows[0]))
    check("l2 dist of self is 0", abs(rows[0]["dist"]) < 1e-6, repr(rows[0]["dist"]))

    # ── Inner Product Queries ─────────────────────────────────────

    print("\n--- Inner Product (<#>)  ---")

    rows = await db.query(
        f"SELECT title, embedding <#> '{query_vec}'::vector AS neg_ip "
        f"FROM test_documents ORDER BY neg_ip LIMIT 3"
    )
    check("ip query returns 3 rows", len(rows) == 3)
    check("ip nearest is Doc A", rows[0]["title"] == "Doc A", repr(rows[0]))
    check(
        "ip of self is -1.0",
        abs(rows[0]["neg_ip"] + 1.0) < 1e-6,
        repr(rows[0]["neg_ip"]),
    )

    # ── Distance Threshold Filtering ──────────────────────────────

    print("\n--- Distance Threshold ---")

    rows = await db.query(
        f"SELECT title FROM test_documents "
        f"WHERE embedding <=> '{query_vec}'::vector < 0.1 "
        f"ORDER BY embedding <=> '{query_vec}'::vector"
    )
    check("threshold returns only exact match", len(rows) == 1, repr(len(rows)))
    check("threshold match is Doc A", rows[0]["title"] == "Doc A", repr(rows[0]))

    rows = await db.query(
        f"SELECT title FROM test_documents "
        f"WHERE embedding <=> '{query_vec}'::vector < 0.35 "
        f"ORDER BY embedding <=> '{query_vec}'::vector"
    )
    check("wider threshold returns A and E", len(rows) == 2, repr(len(rows)))

    # ── Combined Filters ──────────────────────────────────────────

    print("\n--- Combined Filters (category + distance) ---")

    rows = await db.query(
        f"SELECT title FROM test_documents "
        f"WHERE category = 'tech' AND embedding <=> '{query_vec}'::vector < 0.5 "
        f"ORDER BY embedding <=> '{query_vec}'::vector"
    )
    check("combined filter returns tech docs near A", len(rows) == 2, repr(len(rows)))
    check("combined first is Doc A", rows[0]["title"] == "Doc A")
    check("combined second is Doc E", rows[1]["title"] == "Doc E")

    # ── HNSW Index ────────────────────────────────────────────────

    print("\n--- HNSW Index ---")

    await db.execute(
        "CREATE INDEX idx_test_documents_embedding_hnsw ON test_documents "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    check("hnsw index created", True)

    rows = await db.query(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'test_documents' "
        "AND indexname LIKE '%hnsw%'"
    )
    check("hnsw index visible in pg_indexes", len(rows) == 1, repr(rows))

    rows = await db.query(
        f"SELECT title FROM test_documents "
        f"ORDER BY embedding <=> '{query_vec}'::vector LIMIT 1"
    )
    check("hnsw indexed query works", rows[0]["title"] == "Doc A")

    await db.execute("DROP INDEX idx_test_documents_embedding_hnsw")

    # ── IVFFlat Index ─────────────────────────────────────────────

    print("\n--- IVFFlat Index ---")

    await db.execute(
        "CREATE INDEX idx_test_documents_embedding_ivfflat ON test_documents "
        "USING ivfflat (embedding vector_l2_ops) WITH (lists = 4)"
    )
    check("ivfflat index created", True)

    rows = await db.query(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'test_documents' "
        "AND indexname LIKE '%ivfflat%'"
    )
    check("ivfflat index visible", len(rows) == 1, repr(rows))

    # ── Vector Type OID ───────────────────────────────────────────

    print("\n--- Vector Type OID ---")

    rows = await db.query(
        "SELECT oid::integer AS oid FROM pg_type WHERE typname = 'vector'"
    )
    check("vector type has OID", len(rows) == 1)
    vector_oid = rows[0]["oid"]
    check("vector OID is positive", vector_oid > 0, repr(vector_oid))
    print(f"    Vector OID: {vector_oid}")

    # ── Higher Dimensions ─────────────────────────────────────────

    print("\n--- Higher Dimensions ---")

    await db.execute("""
        CREATE TABLE test_products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            embedding vector(1536) NOT NULL
        )
    """)
    check("create table with vector(1536)", True)

    big_vec = "[" + ",".join(str(float(i) / 1536) for i in range(1536)) + "]"
    await db.execute(
        "INSERT INTO test_products (name, embedding) VALUES ($1, $2::vector)",
        "Product A",
        big_vec,
    )
    check("insert 1536-dim vector", True)

    rows = await db.query("SELECT name FROM test_products WHERE id = 1")
    check("select 1536-dim vector", rows[0]["name"] == "Product A")

    rows = await db.query(
        f"SELECT name, embedding <=> '{big_vec}'::vector AS dist FROM test_products ORDER BY dist LIMIT 1"
    )
    check(
        "knn on 1536-dim returns self",
        abs(rows[0]["dist"]) < 1e-6,
        repr(rows[0]["dist"]),
    )

    await db.execute(
        "CREATE INDEX idx_test_products_hnsw ON test_products "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    check("hnsw index on 1536-dim column", True)

    # ── Cleanup ───────────────────────────────────────────────────

    print("\n--- Cleanup ---")

    await db.execute("DROP TABLE IF EXISTS test_documents CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_products CASCADE")
    check("cleanup complete", True)

    # ── Summary ──────────────────────────────────────────────────

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Results: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 60)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
