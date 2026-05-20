"""
Regression tests for fourth-pass correctness fixes.

Tests:
1. FK-spanning count() generates proper JOINs
2. When conditions use full lookup registry (not hardcoded subset)
3. Admin CSRF token generation and injection

Usage:
    uv run hyper-test fourthpass_regressions
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, set_db
from hyperdjango.expressions import Case, Value, When
from hyperdjango.models import Field, Model

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Models for FK-spanning tests
# ---------------------------------------------------------------------------


class FP4Author(Model):
    class Meta:
        table = "fp4_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class FP4Book(Model):
    class Meta:
        table = "fp4_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    author_id: int = Field(foreign_key=FP4Author)
    price: int = Field(default=0)
    status: str = Field(max_length=20, default="draft")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS fp4_books CASCADE")
    await db.execute("DROP TABLE IF EXISTS fp4_authors CASCADE")
    await db.execute("""
        CREATE TABLE fp4_authors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE fp4_books (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            author_id INTEGER REFERENCES fp4_authors(id) ON DELETE CASCADE,
            price INTEGER DEFAULT 0,
            status VARCHAR(20) DEFAULT 'draft'
        )
    """)

    # Seed data
    await db.execute(
        "INSERT INTO fp4_authors (id, name) VALUES (1, 'Alice'), (2, 'Bob')"
    )
    await db.execute(
        "INSERT INTO fp4_books (title, author_id, price, status) VALUES ('Book A', 1, 10, 'published')"
    )
    await db.execute(
        "INSERT INTO fp4_books (title, author_id, price, status) VALUES ('Book B', 1, 20, 'draft')"
    )
    await db.execute(
        "INSERT INTO fp4_books (title, author_id, price, status) VALUES ('Book C', 2, 30, 'published')"
    )

    return db


async def teardown_db(db):
    await db.execute("DROP TABLE IF EXISTS fp4_books CASCADE")
    await db.execute("DROP TABLE IF EXISTS fp4_authors CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# FK-spanning count
# ---------------------------------------------------------------------------


@test("count: FK-spanning filter generates correct count")
async def test_fk_count():
    # Alice has 2 books, Bob has 1
    count = await FP4Book.objects.filter(author_id=1).count()
    assert count == 2

    # FK-spanning: books where author name = "Alice"
    count_alice = await FP4Book.objects.filter(author__name="Alice").count()
    assert count_alice == 2, f"Expected 2 books by Alice, got {count_alice}"

    count_bob = await FP4Book.objects.filter(author__name="Bob").count()
    assert count_bob == 1, f"Expected 1 book by Bob, got {count_bob}"


@test("count: FK-spanning filter with lookup")
async def test_fk_count_lookup():
    count = await FP4Book.objects.filter(author__name__startswith="A").count()
    assert count == 2


@test("filter: FK-spanning returns correct results")
async def test_fk_filter():
    books = await FP4Book.objects.filter(author__name="Alice").all()
    assert len(books) == 2
    titles = {b.title for b in books}
    assert titles == {"Book A", "Book B"}


@test("exists: FK-spanning works correctly")
async def test_fk_exists():
    assert await FP4Book.objects.filter(author__name="Alice").exists() is True
    assert await FP4Book.objects.filter(author__name="Nobody").exists() is False


# ---------------------------------------------------------------------------
# When conditions with full lookup registry
# ---------------------------------------------------------------------------


@test("When: exact lookup (default)")
def test_when_exact():
    w = When(status="published", then=Value("Published!"))
    sql, params = w.as_sql(param_offset=0)
    assert "=" in sql
    assert "published" in params


@test("When: gt lookup")
def test_when_gt():
    w = When(price__gt=15, then=Value("expensive"))
    sql, params = w.as_sql(param_offset=0)
    assert ">" in sql
    assert 15 in params


@test("When: contains lookup with LIKE escaping")
def test_when_contains():
    w = When(title__contains="Book", then=Value("matched"))
    sql, params = w.as_sql(param_offset=0)
    assert "LIKE" in sql
    assert "ESCAPE" in sql
    assert "%Book%" in params


@test("When: isnull lookup")
def test_when_isnull():
    w = When(author_id__isnull=True, then=Value("orphan"))
    sql, params = w.as_sql(param_offset=0)
    assert "IS NULL" in sql


@test("When: in lookup (uses ANY for pg.zig)")
def test_when_in():
    w = When(status__in=["draft", "review"], then=Value("pending"))
    sql, params = w.as_sql(param_offset=0)
    assert "ANY" in sql or "IN" in sql


@test("Case/When: full expression with multiple conditions")
async def test_case_when_db():
    # Use Case/When in annotate
    books = await FP4Book.objects.annotate(
        label=Case(
            When(price__gt=25, then=Value("premium")),
            When(price__gt=10, then=Value("standard")),
            default=Value("budget"),
        )
    ).all()

    assert len(books) == 3
    labels = {b.title: b.label for b in books}
    assert labels["Book A"] == "budget"  # price=10
    assert labels["Book B"] == "standard"  # price=20
    assert labels["Book C"] == "premium"  # price=30


# ---------------------------------------------------------------------------
# Admin CSRF
# ---------------------------------------------------------------------------


@test("admin: CSRF token generation and verification")
def test_admin_csrf():
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp

    app = HyperApp(title="CSRF Test")
    admin = HyperAdmin(app, prefix="/admin")

    # Verify _generate_csrf_token and _verify_csrf_token exist
    assert hasattr(admin, "_generate_csrf_token")
    assert hasattr(admin, "_verify_csrf_token")
    assert hasattr(admin, "_base_context")


@test("admin: _base_context includes csrf_input when request provided")
def test_admin_csrf_context():
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="CSRF Test")
    admin = HyperAdmin(app, prefix="/admin")

    req = Request(
        method="GET", path="/admin/", headers={"cookie": "hyper_admin_session=test"}
    )
    ctx = admin._base_context(request=req)

    assert "csrf_token" in ctx
    assert "csrf_input" in ctx
    assert "_csrf_token" in ctx["csrf_input"]
    assert 'type="hidden"' in ctx["csrf_input"]


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    db = await setup_db()

    print(f"\nFourth-Pass Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    await teardown_db(db)
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
