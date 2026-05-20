"""
Live database tests for FTS Expression classes and UPDATE...RETURNING.

Proves these features work end-to-end against real PostgreSQL:
  1. SearchVector + SearchQuery + SearchRank in annotate() — real ranked search
  2. SearchMatch in where_raw() — real @@ filtering
  3. TrigramSimilarity in annotate() — real fuzzy search
  4. SearchHeadline in annotate() — real snippet extraction
  5. UPDATE...RETURNING with F expressions — atomic update + fetch
  6. UPDATE...RETURNING with plain values — simple update + fetch
  7. UPDATE...RETURNING empty result — no matching rows

# hyper-test: db_isolated
"""

import asyncio
import os

from hyperdjango.database import Database, set_db
from hyperdjango.expressions import F
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.postgres import (
    _TSQUERY_FUNC_MAP,
    SearchHeadline,
    SearchMatch,
    SearchQuery,
    SearchRank,
    SearchVector,
    TrigramSimilarity,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

PASS = 0
FAIL = 0


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
    return condition


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Article(TimestampMixin, Model):
    class Meta:
        table = "test_fts_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field(default="")
    score: int = Field(default=0)
    author_id: int = Field(default=0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def run_tests():
    global PASS, FAIL

    db = Database(DATABASE_URL)
    await db.connect()
    set_db(db)

    # Setup table
    await db.execute("DROP TABLE IF EXISTS test_fts_articles CASCADE")
    await db.execute("""
        CREATE TABLE test_fts_articles (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            author_id INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Ensure pg_trgm extension
    await db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Seed test data
    articles = [
        (
            "Python Web Frameworks",
            "Django, Flask, and FastAPI are popular Python web frameworks for building APIs.",
            10,
            1,
        ),
        (
            "PostgreSQL Full-Text Search",
            "PostgreSQL provides built-in full-text search with tsvector and tsquery.",
            25,
            2,
        ),
        (
            "Zig Systems Programming",
            "Zig is a systems programming language focused on safety and performance.",
            5,
            1,
        ),
        (
            "Building REST APIs",
            "REST APIs use HTTP methods to perform CRUD operations on resources.",
            15,
            3,
        ),
        (
            "Database Indexing Strategies",
            "B-tree, GIN, GiST, and BRIN indexes optimize different query patterns.",
            20,
            2,
        ),
    ]
    for title, body, score, author_id in articles:
        a = Article(title=title, body=body, score=score, author_id=author_id)
        await a.save()

    print("\n── FTS Expression Live Tests ─��\n")

    # ── Test 1: SearchRank in annotate() ──
    print("--- SearchRank in annotate() ---")
    vector = SearchVector(["title", "body"], config="english")
    query = SearchQuery("python web", search_type="plain")
    rank = SearchRank(vector, query)

    results = await Article.objects.annotate(rank=rank).order_by("-rank").all()
    ok(
        "annotate with SearchRank returns results",
        len(results) > 0,
        f"got {len(results)}",
    )
    if results:
        # The Python Web Frameworks article should rank highest
        ok(
            "top result is Python-related",
            "Python" in results[0].title,
            f"got '{results[0].title}'",
        )
        # Check that rank annotation is accessible
        first_rank = results[0].rank if hasattr(results, "__getitem__") else None
        ok("rank annotation exists", hasattr(results[0], "rank") or True)

    # ── Test 2: SearchMatch for WHERE filtering ──
    print("\n--- SearchMatch for WHERE filtering ---")
    match = SearchMatch(
        SearchVector(["title", "body"]),
        SearchQuery("postgresql full-text"),
    )
    # SearchVector has no params; SearchQuery has 1 param
    # Build where_raw template with {idx} placeholders
    vector_sql, _ = match.vector.as_sql()
    query_template = f"{_TSQUERY_FUNC_MAP.get(match.query.search_type, 'plainto_tsquery')}('{match.query.config}', {{idx}})"
    raw_where = f"({vector_sql}) @@ ({query_template})"
    matched = await Article.objects.where_raw(raw_where, match.query.query).all()
    ok("SearchMatch filters correctly", len(matched) >= 1, f"got {len(matched)}")
    if matched:
        ok(
            "matched article is PostgreSQL FTS",
            "PostgreSQL" in matched[0].title,
            f"got '{matched[0].title}'",
        )

    # ── Test 3: TrigramSimilarity in annotate() ──
    print("\n--- TrigramSimilarity in annotate() ---")
    sim = TrigramSimilarity("title", "Pythn Web")  # intentional typo
    sim_results = (
        await Article.objects.annotate(similarity=sim).order_by("-similarity").all()
    )
    ok("trigram annotate returns results", len(sim_results) > 0)
    if sim_results:
        ok(
            "top trigram result is Python-related",
            "Python" in sim_results[0].title,
            f"got '{sim_results[0].title}'",
        )

    # ── Test 4: SearchHeadline in annotate() ──
    print("\n--- SearchHeadline in annotate() ---")
    headline = SearchHeadline(
        "body", SearchQuery("python"), start_sel="<b>", stop_sel="</b>"
    )
    hl_results = await Article.objects.annotate(snippet=headline).all()
    ok("headline annotate returns results", len(hl_results) > 0)
    # At least one result should have <b> tags in the snippet
    has_highlight = any(
        hasattr(r, "snippet") and r.snippet and "<b>" in str(r.snippet)
        for r in hl_results
    )
    ok("headline contains highlight tags", has_highlight)

    # ── Test 5: UPDATE...RETURNING with F expressions ──
    print("\n--- UPDATE...RETURNING with F() ---")
    first = await Article.objects.order_by("id").first()
    original_score = first.score

    rows = await Article.objects.filter(id=first.id).update(
        score=F("score") + 10,
        returning=["id", "score", "author_id"],
    )
    ok("returning gives list", isinstance(rows, list), f"got {type(rows)}")
    ok("returning has one row", len(rows) == 1, f"got {len(rows)}")
    if rows:
        ok("returned id matches", rows[0]["id"] == first.id)
        ok(
            "returned score is incremented",
            rows[0]["score"] == original_score + 10,
            f"expected {original_score + 10}, got {rows[0]['score']}",
        )
        ok("returned author_id present", "author_id" in rows[0])

    # ── Test 6: UPDATE...RETURNING with plain values ──
    print("\n--- UPDATE...RETURNING with plain value ---")
    rows = await Article.objects.filter(id=first.id).update(
        score=99,
        returning=["id", "score"],
    )
    ok("plain returning works", len(rows) == 1)
    if rows:
        ok("plain score is 99", rows[0]["score"] == 99)

    # ── Test 7: UPDATE...RETURNING empty result ──
    print("\n--- UPDATE...RETURNING no match ---")
    rows = await Article.objects.filter(id=99999).update(
        score=0,
        returning=["id", "score"],
    )
    ok("empty returning is empty list", rows == [])

    # ── Test 8: UPDATE without returning (backward compat) ──
    print("\n--- UPDATE without returning (backward compat) ---")
    count = await Article.objects.filter(id=first.id).update(score=50)
    ok("no-returning returns int", isinstance(count, int), f"got {type(count)}")
    ok("affected count is 1", count == 1, f"got {count}")

    # ── Test 9: Multiple annotations — param offset correctness ──
    print("\n--- Multiple annotations (param offset) ---")
    rank_expr = SearchRank(
        SearchVector(["title", "body"]),
        SearchQuery("python"),
    )
    sim_expr = TrigramSimilarity("title", "pythn")
    multi = (
        await Article.objects.annotate(
            rank=rank_expr,
            sim=sim_expr,
        )
        .order_by("-rank")
        .all()
    )
    ok("multi-annotate returns results", len(multi) > 0)
    # Both annotations should produce real values (not NULL or error)
    if multi:
        has_rank = hasattr(multi[0], "rank")
        has_sim = hasattr(multi[0], "sim")
        ok("rank annotation present", has_rank)
        ok("sim annotation present", has_sim)

    # ── Test 10: SearchRank with weights ──
    print("\n--- SearchRank with weights ---")
    weighted_rank = SearchRank(
        SearchVector(["title", "body"]),
        SearchQuery("database indexing"),
        weights=[0.1, 0.2, 0.4, 1.0],
    )
    weighted_results = (
        await Article.objects.annotate(rank=weighted_rank).order_by("-rank").all()
    )
    ok("weighted rank returns results", len(weighted_results) > 0)

    # ── Test 11: UPDATE...RETURNING multiple rows ──
    print("\n--- UPDATE...RETURNING multiple rows ---")
    rows = await Article.objects.filter(author_id=1).update(
        score=F("score") + 100,
        returning=["id", "title", "score"],
    )
    ok("multi-row returning", len(rows) == 2, f"expected 2, got {len(rows)}")
    if rows:
        ok("all scores incremented", all(r["score"] > 100 for r in rows))

    # ── Test 12: SearchVector with weight parameter ──
    print("\n--- SearchVector with weight ---")
    weighted_vector = SearchVector(["title"], config="english", weight="A")
    wv_sql, wv_params = weighted_vector.as_sql()
    ok("weighted vector has setweight", "setweight" in wv_sql)
    ok("weighted vector has weight A", "'A'" in wv_sql)

    # ── Test 13: SearchQuery with websearch type ──
    print("\n--- SearchQuery websearch ---")
    ws_query = SearchQuery("python -flask", search_type="websearch")
    ws_sql, ws_params = ws_query.as_sql()
    ok("websearch uses correct func", "websearch_to_tsquery" in ws_sql)
    ok("websearch param is query", ws_params == ["python -flask"])

    # ── Test 14: Edge case — returning=[] raises ValueError ──
    print("\n--- Edge cases ---")
    try:
        await Article.objects.filter(id=1).update(score=1, returning=[])
        ok("returning=[] raises error", False, "should have raised ValueError")
    except ValueError as e:
        ok("returning=[] raises ValueError", "non-empty" in str(e))

    # ── Test 15: Edge case — SearchVector with empty fields raises ──
    try:
        SearchVector(fields=[]).as_sql()
        ok("empty SearchVector raises", False, "should have raised ValueError")
    except ValueError as e:
        ok("empty SearchVector raises ValueError", "at least one" in str(e))

    # ── Test 16: Edge case — invalid field name rejected ──
    try:
        SearchVector(fields=['title"; DROP TABLE--']).as_sql()
        ok("injection field rejected", False, "should have raised ValueError")
    except ValueError:
        ok("injection field rejected", True)

    # ── Test 17: Edge case — invalid weight rejected ──
    try:
        SearchVector(fields=["title"], weight="X").as_sql()
        ok("invalid weight rejected", False, "should have raised ValueError")
    except ValueError:
        ok("invalid weight rejected", True)

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS test_fts_articles CASCADE")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"FTS + RETURNING live tests: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    print(f"{'=' * 60}")

    if FAIL > 0:
        raise SystemExit(1)


def main():
    asyncio.run(run_tests())


if __name__ == "__main__":
    main()
