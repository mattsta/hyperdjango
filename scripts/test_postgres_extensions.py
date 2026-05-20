"""
Tests for PostgreSQL extensions — arrays, full-text search, trigrams.

# hyper-test: db_isolated

Tests postgres.py features against a real PostgreSQL database:
- Array operations (@>, &&, array_length, unnest)
- SearchVector + SearchQuery + SearchRank (full-text search via tsvector)
- TrigramSimilarity (fuzzy matching via pg_trgm)
- GIN index creation
- Combined FTS + trigram search
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.database import Database, set_db

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


async def main():
    print("=" * 60)
    print("PostgreSQL Extensions Tests")
    print("=" * 60)

    db = Database(DATABASE_URL)
    await db.connect()
    set_db(db)

    # Setup
    await db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    await db.execute("DROP TABLE IF EXISTS pg_ext_articles CASCADE")
    await db.execute("""
        CREATE TABLE pg_ext_articles (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            tags TEXT[] DEFAULT '{}',
            tsv tsvector
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pg_ext_tsv ON pg_ext_articles USING gin(tsv)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pg_ext_tags ON pg_ext_articles USING gin(tags)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pg_ext_trgm ON pg_ext_articles USING gin(title gin_trgm_ops)"
    )

    # Seed
    articles = [
        (
            "PostgreSQL Performance Tuning",
            "Optimize queries with indexes and EXPLAIN ANALYZE",
            "{database,performance}",
        ),
        (
            "Python Async Programming",
            "Learn asyncio event loops and concurrent tasks",
            "{python,async}",
        ),
        (
            "Building REST APIs with Django",
            "Create RESTful web services using Django REST framework",
            "{python,web,django}",
        ),
        (
            "Docker Container Deployment",
            "Deploy applications using Docker containers",
            "{devops,docker}",
        ),
        (
            "Machine Learning Fundamentals",
            "Introduction to neural networks and deep learning",
            "{ml,python}",
        ),
        (
            "PostgreSQL Full-Text Search",
            "Use tsvector and tsquery for text search in PostgreSQL",
            "{database,search}",
        ),
        (
            "Web Security Best Practices",
            "Protect your web applications from common attacks",
            "{security,web}",
        ),
        (
            "Kubernetes Orchestration",
            "Scale containerized applications with Kubernetes",
            "{devops,kubernetes}",
        ),
    ]
    for title, body, tags in articles:
        await db.execute(
            "INSERT INTO pg_ext_articles (title, body, tags, tsv) "
            "VALUES ($1, $2, $3::text[], to_tsvector('english', $1 || ' ' || $2))",
            title,
            body,
            tags,
        )

    # ── Array Operations ──
    print("\n--- Array Operations ---")

    # @> contains
    rows = await db.query(
        "SELECT title FROM pg_ext_articles WHERE tags @> $1::text[]",
        "{python}",
    )
    check(
        "Array @> contains 'python'",
        len(rows) == 3,
        f"got {len(rows)}: {[r['title'] for r in rows]}",
    )

    # && overlap
    rows = await db.query(
        "SELECT title FROM pg_ext_articles WHERE tags && $1::text[]",
        "{database,ml}",
    )
    check("Array && overlap [database, ml]", len(rows) == 3, f"got {len(rows)}")

    # array_length
    rows = await db.query(
        "SELECT title, array_length(tags, 1) AS tag_count "
        "FROM pg_ext_articles ORDER BY tag_count DESC"
    )
    check("array_length works", rows[0]["tag_count"] >= 2, f"got {rows[0]}")

    # unnest + aggregate
    rows = await db.query(
        "SELECT unnest(tags) AS tag, COUNT(*) AS cnt "
        "FROM pg_ext_articles GROUP BY tag ORDER BY cnt DESC"
    )
    check("Unnest + aggregate works", len(rows) > 0)
    if rows:
        check("Top tag is 'python'", rows[0]["tag"] == "python", f"got {rows[0]}")

    # array_append
    await db.execute(
        "UPDATE pg_ext_articles SET tags = array_append(tags, $1) WHERE id = 1",
        "optimization",
    )
    row = await db.query_one("SELECT tags FROM pg_ext_articles WHERE id = 1")
    check(
        "array_append added tag",
        "optimization" in str(row["tags"]),
        f"tags={row['tags']}",
    )

    # ── Full-Text Search ──
    print("\n--- Full-Text Search ---")

    # Basic search
    rows = await db.query(
        "SELECT title, ts_rank(tsv, plainto_tsquery('english', $1)) AS rank "
        "FROM pg_ext_articles WHERE tsv @@ plainto_tsquery('english', $1) "
        "ORDER BY rank DESC",
        "postgresql optimization",
    )
    check("FTS finds PostgreSQL articles", len(rows) >= 1, f"got {len(rows)}")
    if rows:
        check(
            "Top FTS result is PostgreSQL-related",
            "PostgreSQL" in rows[0]["title"],
            f"got {rows[0]['title']}",
        )

    # DevOps search (OR query to match either Docker or Kubernetes articles)
    rows = await db.query(
        "SELECT title FROM pg_ext_articles WHERE tsv @@ to_tsquery('english', $1)",
        "docker | kubernetes | container",
    )
    check("FTS finds DevOps articles", len(rows) >= 2, f"got {len(rows)}")

    # No results
    rows = await db.query(
        "SELECT title FROM pg_ext_articles WHERE tsv @@ plainto_tsquery('english', $1)",
        "blockchain cryptocurrency",
    )
    check("FTS empty for irrelevant query", len(rows) == 0)

    # ts_headline
    rows = await db.query(
        "SELECT title, ts_headline('english', body, plainto_tsquery('english', $1)) AS headline "
        "FROM pg_ext_articles WHERE tsv @@ plainto_tsquery('english', $1)",
        "asyncio",
    )
    check("ts_headline works", len(rows) >= 1)
    if rows:
        check(
            "Headline has bold markers",
            "<b>" in rows[0]["headline"],
            f"headline={rows[0]['headline']}",
        )

    # Phrase search (tsquery)
    rows = await db.query(
        "SELECT title FROM pg_ext_articles "
        "WHERE tsv @@ phraseto_tsquery('english', $1)",
        "full text search",
    )
    check("Phrase search works", len(rows) >= 1, f"got {len(rows)}")

    # ── Trigram Similarity ──
    print("\n--- Trigram Similarity ---")

    # Fuzzy match for typo
    rows = await db.query(
        "SELECT title, similarity(title, $1) AS sim "
        "FROM pg_ext_articles "
        "WHERE similarity(title, $1) > 0.1 "
        "ORDER BY sim DESC",
        "Postgre Performanc",
    )
    check("Trigram finds similar", len(rows) >= 1, f"got {len(rows)}")
    if rows:
        check(
            "Top trigram result is PostgreSQL",
            "PostgreSQL" in rows[0]["title"],
            f"got {rows[0]['title']} sim={rows[0]['sim']:.3f}",
        )

    # "Did you mean" style fuzzy
    rows = await db.query(
        "SELECT title, similarity(title, $1) AS sim "
        "FROM pg_ext_articles "
        "WHERE similarity(title, $1) > 0.05 "
        "ORDER BY sim DESC LIMIT 3",
        "machin lerning",
    )
    check(
        "Fuzzy match 'machin lerning'",
        len(rows) >= 1,
        f"got {[r['title'] for r in rows]}",
    )

    # word_similarity (partial match)
    rows = await db.query(
        "SELECT title, word_similarity($1, title) AS wsim "
        "FROM pg_ext_articles "
        "WHERE word_similarity($1, title) > 0.3 "
        "ORDER BY wsim DESC",
        "docker",
    )
    check("word_similarity finds Docker article", len(rows) >= 1, f"got {len(rows)}")

    # ── Combined FTS + Trigram ──
    print("\n--- Combined FTS + Trigram ---")
    rows = await db.query(
        "SELECT title, "
        "  ts_rank(tsv, plainto_tsquery('english', $1)) AS fts_rank, "
        "  similarity(title, $1) AS trgm_sim "
        "FROM pg_ext_articles "
        "WHERE tsv @@ plainto_tsquery('english', $1) OR similarity(title, $1) > 0.1 "
        "ORDER BY ts_rank(tsv, plainto_tsquery('english', $1)) + similarity(title, $1) DESC "
        "LIMIT 3",
        "postgresql search",
    )
    check("Combined FTS+trigram returns results", len(rows) >= 1)

    # ── Array + FTS combined ──
    print("\n--- Array + FTS combined ---")
    rows = await db.query(
        "SELECT title FROM pg_ext_articles "
        "WHERE tags @> $1::text[] AND tsv @@ plainto_tsquery('english', $2)",
        "{python}",
        "async concurrent",
    )
    check(
        "Array + FTS combined filter",
        len(rows) >= 1,
        f"got {[r['title'] for r in rows]}",
    )

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS pg_ext_articles CASCADE")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
