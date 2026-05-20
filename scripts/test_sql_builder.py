"""Tests for QuerySet SQL builder pre-allocation optimization.

Verifies SQL generation correctness after refactoring from string concatenation
to list-based assembly, and benchmarks complex query construction.
"""

# hyper-test: unit

import sys
import time

from hyperdjango.models import Field, Model

# ── Test models ───────────────────────────────────────────────────────────


class Author(Model):
    class Meta:
        table = "authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    email: str = Field(max_length=255)


class Post(Model):
    class Meta:
        table = "posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    body: str = Field()
    author_id: int = Field(foreign_key=Author)
    status: str = Field(max_length=20, default="draft")
    views: int = Field(default=0)
    created_at: str = Field(default="")


class Comment(Model):
    class Meta:
        table = "comments"

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field(foreign_key=Post)
    text: str = Field()
    author_id: int = Field(foreign_key=Author)


# ── SQL generation tests ─────────────────────────────────────────────────


def test_simple_select():
    """Basic SELECT query."""
    qs = Post.objects.filter()
    sql, params = qs._build_select()
    assert sql.startswith("SELECT ")
    assert "FROM posts" in sql
    assert params == []
    print("  PASS: Simple SELECT")


def test_select_with_filter():
    """SELECT with WHERE clause."""
    qs = Post.objects.filter(status="published")
    sql, params = qs._build_select()
    assert "FROM posts" in sql
    assert "WHERE" in sql
    assert len(params) == 1
    assert params[0] == "published"
    print("  PASS: SELECT with filter")


def test_select_with_ordering():
    """SELECT with ORDER BY."""
    qs = Post.objects.order_by("-created_at", "title")
    sql, params = qs._build_select()
    assert "ORDER BY" in sql
    assert "DESC" in sql
    assert "ASC" in sql
    print("  PASS: SELECT with ORDER BY")


def test_select_with_limit_offset():
    """SELECT with LIMIT and OFFSET — both are BOUND params, not inlined.

    Parameterizing LIMIT/OFFSET collapses every page of a query to one cached
    `... LIMIT $n OFFSET $m` template. They trail the WHERE params in order.
    """
    qs = Post.objects.filter()
    qs._limit = 10
    qs._offset = 20
    sql, params = qs._build_select()
    assert "LIMIT $" in sql, f"LIMIT should be a bound param, got {sql!r}"
    assert "OFFSET $" in sql, f"OFFSET should be a bound param, got {sql!r}"
    # LIMIT before OFFSET, both appended after the (empty) WHERE params.
    assert params[-2:] == [10, 20], f"expected trailing [10, 20], got {params!r}"
    print("  PASS: SELECT with LIMIT/OFFSET")


def test_select_distinct():
    """SELECT DISTINCT."""
    qs = Post.objects.distinct()
    sql, params = qs._build_select()
    assert sql.startswith("SELECT DISTINCT")
    print("  PASS: SELECT DISTINCT")


def test_select_values():
    """SELECT with specific fields (values)."""
    qs = Post.objects.values("title", "status")
    sql, params = qs._build_select()
    assert "title" in sql
    assert "status" in sql
    print("  PASS: SELECT values fields")


def test_select_for_update():
    """SELECT FOR UPDATE locking."""
    qs = Post.objects.select_for_update()
    sql, params = qs._build_select()
    assert "FOR UPDATE" in sql
    print("  PASS: SELECT FOR UPDATE")


def test_count_query():
    """COUNT query."""
    qs = Post.objects.filter(status="draft")
    sql, params = qs._build_count()
    assert "SELECT COUNT(*)" in sql
    assert "FROM posts" in sql
    assert "WHERE" in sql
    print("  PASS: COUNT query")


def test_update_query():
    """UPDATE query."""
    qs = Post.objects.filter(status="draft")
    sql, params = qs._build_update({"status": "published", "views": 0})
    assert "UPDATE posts SET" in sql
    assert "WHERE" in sql
    # Two SET values + one WHERE param
    assert len(params) >= 2
    print("  PASS: UPDATE query")


def test_delete_query():
    """DELETE query."""
    qs = Post.objects.filter(status="archived")
    sql, params = qs._build_delete()
    assert "DELETE FROM posts" in sql
    assert "WHERE" in sql
    print("  PASS: DELETE query")


def test_complex_query():
    """Complex query with multiple clauses."""
    qs = (
        Post.objects.filter(status="published")
        .filter(views__gt=100)
        .exclude(title="")
        .order_by("-views", "title")
        .distinct()
    )
    qs._limit = 25
    qs._offset = 50
    sql, params = qs._build_select()

    assert "SELECT DISTINCT" in sql
    assert "FROM posts" in sql
    assert "WHERE" in sql
    assert "ORDER BY" in sql
    assert "DESC" in sql
    # LIMIT/OFFSET are BOUND params (trailing), not inlined literals.
    assert "LIMIT $" in sql, f"LIMIT should be a bound param, got {sql!r}"
    assert "OFFSET $" in sql, f"OFFSET should be a bound param, got {sql!r}"
    assert params[-2:] == [25, 50], f"expected trailing [25, 50], got {params!r}"
    # Verify no double spaces (from old += pattern)
    assert "  " not in sql
    print("  PASS: Complex query with all clauses")


def test_sql_no_double_spaces():
    """Ensure list-based join produces clean SQL without double spaces."""
    qs = Post.objects.filter(status="draft").order_by("title")
    qs._limit = 10
    sql, _ = qs._build_select()
    # Check no consecutive spaces
    prev = ""
    for ch in sql:
        assert not (ch == " " and prev == " "), f"Double space found in: {sql}"
        prev = ch
    print("  PASS: No double spaces in generated SQL")


def test_select_related_join():
    """SELECT with select_related generates JOINs (requires model registration)."""
    # FK join resolution requires models to be registered in the global registry.
    # This is tested end-to-end in test_orm_relations.py with a live DB.
    # Here we verify the SQL builder doesn't crash with empty select_related.
    qs = Post.objects.filter(status="draft")
    sql, params = qs._build_select()
    assert "FROM posts" in sql
    print("  PASS: SELECT (FK joins tested in integration)")


def test_multiple_filters():
    """Multiple filters produce correct AND conditions."""
    qs = Post.objects.filter(status="published").filter(views__gt=100)
    sql, params = qs._build_select()
    assert "WHERE" in sql
    assert len(params) == 2
    print("  PASS: Multiple filters")


# ── Benchmark ─────────────────────────────────────────────────────────────


def test_sql_builder_benchmark():
    """Benchmark SQL building for simple and complex queries."""
    iterations = 10_000

    # Simple query
    qs_simple = Post.objects.filter(status="published")
    start = time.perf_counter_ns()
    for _ in range(iterations):
        qs_simple._build_select()
    simple_ns = (time.perf_counter_ns() - start) / iterations

    # Complex query
    qs_complex = (
        Post.objects.filter(status="published")
        .filter(views__gt=100)
        .exclude(title="")
        .order_by("-views", "title")
        .distinct()
    )
    qs_complex._limit = 25
    qs_complex._offset = 50
    start = time.perf_counter_ns()
    for _ in range(iterations):
        qs_complex._build_select()
    complex_ns = (time.perf_counter_ns() - start) / iterations

    # Count query
    qs_count = Post.objects.filter(status="draft")
    start = time.perf_counter_ns()
    for _ in range(iterations):
        qs_count._build_count()
    count_ns = (time.perf_counter_ns() - start) / iterations

    # Update query
    qs_update = Post.objects.filter(status="draft")
    start = time.perf_counter_ns()
    for _ in range(iterations):
        qs_update._build_update({"status": "published"})
    update_ns = (time.perf_counter_ns() - start) / iterations

    print("  PASS: SQL builder benchmark")
    print(f"    simple SELECT:  {simple_ns:,.0f} ns/query")
    print(f"    complex SELECT: {complex_ns:,.0f} ns/query")
    print(f"    COUNT:          {count_ns:,.0f} ns/query")
    print(f"    UPDATE:         {update_ns:,.0f} ns/query")


def main():
    tests = [
        test_simple_select,
        test_select_with_filter,
        test_select_with_ordering,
        test_select_with_limit_offset,
        test_select_distinct,
        test_select_values,
        test_select_for_update,
        test_count_query,
        test_update_query,
        test_delete_query,
        test_complex_query,
        test_sql_no_double_spaces,
        test_select_related_join,
        test_multiple_filters,
        test_sql_builder_benchmark,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("QuerySet SQL Builder Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback

            failed += 1
            errors.append((test.__name__, str(e)))
            traceback.print_exc()
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
