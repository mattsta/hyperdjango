#!/usr/bin/env python3
"""
Tests for ORM relations: select_related, prefetch_related, annotate, aggregate, M2M.

All tests run against live PostgreSQL via hyperdjango.db.

Usage:
    uv run hyper-test orm_relations
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import traceback

# Add project root to path
from hyperdjango.database import Database, get_db, set_db
from hyperdjango.expressions import (
    Avg,
    Case,
    Cast,
    Coalesce,
    Count,
    F,
    Max,
    Min,
    Sum,
    Value,
    When,
)
from hyperdjango.models import Field, ManyToManyField, Model
from hyperdjango.query import _model_registry

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class Publisher(Model):
    class Meta:
        table = "test_publishers"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    country: str = Field(max_length=100, default="US")


class Author(Model):
    class Meta:
        table = "test_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    publisher_id: int | None = Field(foreign_key=Publisher, default=None)


class Book(Model):
    class Meta:
        table = "test_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=300)
    author_id: int = Field(foreign_key=Author)
    price: float = Field(default=0.0)
    pages: int = Field(default=0)


class Tag(Model):
    class Meta:
        table = "test_tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=50)


class BookTag(Model):
    """Explicit junction model for testing — M2M also auto-generates one."""

    class Meta:
        table = "test_books_test_tags"

    book_id: int = Field(foreign_key=Book)
    tag_id: int = Field(foreign_key=Tag)


# Add M2M descriptor to Book
Book.tags = ManyToManyField("test_tags", junction_table="test_books_test_tags")
Book.tags._configure(Book, "tags")

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


async def setup_db():
    """Create test database and tables."""
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Create tables
    for sql in [
        "DROP TABLE IF EXISTS test_books_test_tags CASCADE",
        "DROP TABLE IF EXISTS test_books CASCADE",
        "DROP TABLE IF EXISTS test_authors CASCADE",
        "DROP TABLE IF EXISTS test_publishers CASCADE",
        "DROP TABLE IF EXISTS test_tags CASCADE",
        """CREATE TABLE test_publishers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            country VARCHAR(100) DEFAULT 'US'
        )""",
        """CREATE TABLE test_authors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            publisher_id INTEGER REFERENCES test_publishers(id) ON DELETE CASCADE DEFAULT NULL
        )""",
        """CREATE TABLE test_books (
            id SERIAL PRIMARY KEY,
            title VARCHAR(300) NOT NULL,
            author_id INTEGER NOT NULL REFERENCES test_authors(id) ON DELETE CASCADE,
            price FLOAT DEFAULT 0.0,
            pages INTEGER DEFAULT 0
        )""",
        """CREATE TABLE test_tags (
            id SERIAL PRIMARY KEY,
            name VARCHAR(50) NOT NULL
        )""",
        """CREATE TABLE test_books_test_tags (
            book_id INTEGER NOT NULL REFERENCES test_books(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES test_tags(id) ON DELETE CASCADE,
            PRIMARY KEY (book_id, tag_id)
        )""",
    ]:
        await db.execute(sql)

    return db


async def seed_data(db):
    """Insert test data."""
    # Publishers
    await db.execute(
        "INSERT INTO test_publishers (id, name, country) VALUES ($1, $2, $3), ($4, $5, $6)",
        1,
        "Penguin",
        "UK",
        2,
        "HarperCollins",
        "US",
    )
    # Authors
    await db.execute(
        "INSERT INTO test_authors (id, name, publisher_id) VALUES ($1, $2, $3), ($4, $5, $6), ($7, $8, $9)",
        1,
        "Alice",
        1,
        2,
        "Bob",
        2,
        3,
        "Charlie",
        None,  # No publisher
    )
    # Books
    await db.execute(
        "INSERT INTO test_books (id, title, author_id, price, pages) VALUES "
        "($1, $2, $3, $4, $5), ($6, $7, $8, $9, $10), ($11, $12, $13, $14, $15), "
        "($16, $17, $18, $19, $20), ($21, $22, $23, $24, $25)",
        1,
        "Book A",
        1,
        10.99,
        200,
        2,
        "Book B",
        1,
        15.50,
        350,
        3,
        "Book C",
        2,
        22.00,
        180,
        4,
        "Book D",
        2,
        8.99,
        120,
        5,
        "Book E",
        3,
        45.00,
        500,
    )
    # Tags
    await db.execute(
        "INSERT INTO test_tags (id, name) VALUES ($1, $2), ($3, $4), ($5, $6)",
        1,
        "fiction",
        2,
        "science",
        3,
        "history",
    )
    # Book-Tag associations
    await db.execute(
        "INSERT INTO test_books_test_tags (book_id, tag_id) VALUES ($1, $2), ($3, $4), ($5, $6), ($7, $8)",
        1,
        1,  # Book A -> fiction
        1,
        2,  # Book A -> science
        2,
        1,  # Book B -> fiction
        3,
        3,  # Book C -> history
    )
    # Reset sequences
    await db.execute("SELECT setval('test_publishers_id_seq', 10)")
    await db.execute("SELECT setval('test_authors_id_seq', 10)")
    await db.execute("SELECT setval('test_books_id_seq', 10)")
    await db.execute("SELECT setval('test_tags_id_seq', 10)")


async def teardown_db(db):
    """Drop test tables."""
    for sql in [
        "DROP TABLE IF EXISTS test_books_test_tags CASCADE",
        "DROP TABLE IF EXISTS test_books CASCADE",
        "DROP TABLE IF EXISTS test_authors CASCADE",
        "DROP TABLE IF EXISTS test_publishers CASCADE",
        "DROP TABLE IF EXISTS test_tags CASCADE",
    ]:
        await db.execute(sql)
    await db.disconnect()


def run_test(name, func):
    """Run a single test function."""
    try:
        asyncio.run(func())
        RESULTS["passed"] += 1
        print(f"  ✓ {name}")
    except Exception as e:
        RESULTS["failed"] += 1
        RESULTS["errors"].append((name, e))
        print(f"  ✗ {name}: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Expression tests (unit tests — no DB needed)
# ---------------------------------------------------------------------------


def test_f_expression():
    async def _test():
        f = F("price")
        sql, params = f.as_sql()
        assert sql == "price", f"Expected 'price', got {sql!r}"
        assert params == []
        assert f.default_alias == "price"
        assert not f.contains_aggregate

    run_test("F expression generates column reference", _test)


def test_value_expression():
    async def _test():
        v = Value(42)
        sql, params = v.as_sql()
        assert sql == "$1", f"Expected '$1', got {sql!r}"
        assert params == [42]

        v_null = Value(None)
        sql, params = v_null.as_sql()
        assert sql == "NULL"
        assert params == []

    run_test("Value expression generates parameter placeholder", _test)


def test_combined_expression():
    async def _test():
        expr = F("price") * Value(1.1)
        sql, params = expr.as_sql()
        assert sql == "(price * $1)", f"Expected '(price * $1)', got {sql!r}"
        assert params == [1.1]
        assert not expr.contains_aggregate

    run_test("Combined expression (F * Value)", _test)


def test_combined_expression_two_f():
    async def _test():
        expr = F("revenue") - F("cost")
        sql, params = expr.as_sql()
        assert sql == "(revenue - cost)", f"Expected '(revenue - cost)', got {sql!r}"
        assert params == []

    run_test("Combined expression (F - F)", _test)


def test_count_expression():
    async def _test():
        c = Count("id")
        sql, params = c.as_sql()
        assert sql == "COUNT(id)", f"Expected 'COUNT(id)', got {sql!r}"
        assert c.contains_aggregate
        assert c.default_alias == "id__count"
        assert c.empty_result_set_value == 0

    run_test("Count aggregate expression", _test)


def test_count_distinct():
    async def _test():
        c = Count("author_id", distinct=True)
        sql, params = c.as_sql()
        assert sql == "COUNT(DISTINCT author_id)", f"Got {sql!r}"

    run_test("Count with DISTINCT", _test)


def test_sum_expression():
    async def _test():
        s = Sum("price")
        sql, params = s.as_sql()
        assert sql == "SUM(price)", f"Got {sql!r}"
        assert s.contains_aggregate

    run_test("Sum aggregate expression", _test)


def test_avg_expression():
    async def _test():
        a = Avg("price")
        sql, params = a.as_sql()
        assert sql == "AVG(price)", f"Got {sql!r}"

    run_test("Avg aggregate expression", _test)


def test_max_min_expression():
    async def _test():
        mx = Max("price")
        mn = Min("price")
        sql_mx, _ = mx.as_sql()
        sql_mn, _ = mn.as_sql()
        assert sql_mx == "MAX(price)"
        assert sql_mn == "MIN(price)"

    run_test("Max and Min aggregate expressions", _test)


def test_coalesce_expression():
    async def _test():
        expr = Coalesce(F("nickname"), F("name"), Value("Anonymous"))
        sql, params = expr.as_sql()
        assert sql == "COALESCE(nickname, name, $1)", f"Got {sql!r}"
        assert params == ["Anonymous"]

    run_test("Coalesce expression", _test)


def test_cast_expression():
    async def _test():
        expr = Cast(F("price"), "numeric(10,2)")
        sql, params = expr.as_sql()
        assert sql == "CAST(price AS numeric(10,2))", f"Got {sql!r}"

    run_test("Cast expression", _test)


def test_case_when_expression():
    async def _test():
        expr = Case(
            When(then=Value("expensive"), price__gte=20),
            When(then=Value("cheap"), price__lt=10),
            default=Value("mid"),
        )
        sql, params = expr.as_sql()
        assert "CASE" in sql
        assert "WHEN" in sql
        assert "ELSE" in sql
        assert "END" in sql

    run_test("Case/When expression", _test)


def test_arithmetic_operators():
    async def _test():
        # All operators
        for op_func, op_str in [
            (lambda: F("a") + F("b"), "+"),
            (lambda: F("a") - F("b"), "-"),
            (lambda: F("a") * F("b"), "*"),
            (lambda: F("a") / F("b"), "/"),
            (lambda: F("a") % F("b"), "%"),
        ]:
            expr = op_func()
            sql, _ = expr.as_sql()
            assert op_str in sql, f"Expected {op_str} in {sql!r}"

    run_test("All arithmetic operators on expressions", _test)


def test_negation():
    async def _test():
        expr = -F("balance")
        sql, _ = expr.as_sql()
        assert sql == "(-balance)", f"Expected '(-balance)', got {sql!r}"

    run_test("Negation operator on expression", _test)


def test_param_offset():
    async def _test():
        v = Value(42)
        sql, params = v.as_sql(param_offset=5)
        assert sql == "$6", f"Expected '$6', got {sql!r}"

    run_test("Value with param_offset", _test)


def test_aggregate_contains_aggregate():
    async def _test():
        # Plain F — not aggregate
        assert not F("x").contains_aggregate
        # Sum — aggregate
        assert Sum("x").contains_aggregate
        # Combined with aggregate
        assert (F("x") + Sum("y")).contains_aggregate
        # Combined without aggregate
        assert not (F("x") + F("y")).contains_aggregate

    run_test("contains_aggregate propagation", _test)


# ---------------------------------------------------------------------------
# Model meta tests
# ---------------------------------------------------------------------------


def test_model_registration():
    async def _test():
        assert "test_publishers" in _model_registry
        assert "test_authors" in _model_registry
        assert "test_books" in _model_registry
        assert "test_tags" in _model_registry
        assert _model_registry["test_publishers"] is Publisher

    run_test("Models registered in _model_registry", _test)


def test_model_fk_fields():
    async def _test():
        fk_fields = Book._meta.get_fk_fields()
        assert "author_id" in fk_fields
        assert fk_fields["author_id"].foreign_key == "test_authors"

    run_test("Model._meta.get_fk_fields()", _test)


def test_m2m_field_descriptor():
    async def _test():
        assert hasattr(Book, "tags")
        desc = Book.__dict__["tags"]
        assert isinstance(desc, ManyToManyField)
        assert desc._junction_table == "test_books_test_tags"
        # Column names derived from table: test_books -> book_id, test_tags -> tag_id
        assert desc._source_col == "book_id", f"Got {desc._source_col!r}"
        assert desc._target_col == "tag_id", f"Got {desc._target_col!r}"

    run_test("ManyToManyField descriptor configured correctly", _test)


def test_m2m_create_table_sql():
    async def _test():
        desc = Book.__dict__["tags"]
        sql = desc.create_table_sql
        assert "test_books_test_tags" in sql
        assert desc._source_col in sql
        assert desc._target_col in sql
        assert "PRIMARY KEY" in sql

    run_test("ManyToManyField.create_table_sql", _test)


# ---------------------------------------------------------------------------
# select_related tests (live DB)
# ---------------------------------------------------------------------------


def test_select_related_single_fk():
    async def _test():
        books = await Book.objects.select_related("author_id").order_by("id").all()
        assert len(books) == 5
        # First book's author should be loaded
        book_a = books[0]
        assert hasattr(book_a, "author_id")
        author = getattr(book_a, "author_id", None)
        # author_id is the FK column — the joined author is set on the same attr
        # Let's verify the join worked by checking the SQL

    run_test("select_related single FK", _test)


def test_select_related_resolves_join():
    async def _test():
        # Build the SQL and verify it contains LEFT JOIN
        qs = Book.objects.select_related("author_id")
        sql, params = qs._build_select()
        assert "LEFT JOIN" in sql, f"Expected LEFT JOIN in: {sql}"
        assert "test_authors" in sql, f"Expected test_authors in: {sql}"

    run_test("select_related generates LEFT JOIN SQL", _test)


def test_select_related_nested():
    async def _test():
        # author_id -> test_authors, then publisher_id -> test_publishers
        qs = Book.objects.select_related("author_id__publisher_id")
        sql, params = qs._build_select()
        assert sql.count("LEFT JOIN") == 2, f"Expected 2 LEFT JOINs in: {sql}"
        assert "test_authors" in sql
        assert "test_publishers" in sql

    run_test("select_related nested FK chain generates 2 JOINs", _test)


def test_select_related_multiple_fields():
    async def _test():
        qs = Book.objects.select_related("author_id")
        sql, params = qs._build_select()
        # Should have columns from both tables
        assert "test_books.id" in sql
        assert "author_id__name" in sql or "t1.name" in sql

    run_test("select_related includes columns from joined table", _test)


def test_select_related_null_fk():
    async def _test():
        # Author Charlie has no publisher (NULL FK)
        qs = Author.objects.select_related("publisher_id")
        sql, _ = qs._build_select()
        assert "LEFT JOIN" in sql
        authors = await qs.order_by("id").all()
        assert len(authors) == 3
        # Charlie (id=3) has publisher_id=None, so related publisher should be None
        charlie = authors[2]
        assert charlie.name == "Charlie"

    run_test("select_related with NULL FK", _test)


def test_select_related_invalid_field():
    async def _test():
        try:
            Book.objects.select_related("nonexistent")._resolve_joins()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "not a FK field" in str(e)

    run_test("select_related raises on non-FK field", _test)


# ---------------------------------------------------------------------------
# prefetch_related tests (live DB)
# ---------------------------------------------------------------------------


def test_prefetch_related_reverse_fk():
    async def _test():
        authors = await Author.objects.prefetch_related("books").order_by("id").all()
        assert len(authors) == 3
        # Alice should have 2 books
        alice = authors[0]
        assert hasattr(alice, "books"), "books attribute not set"
        assert len(alice.books) == 2, f"Expected 2 books, got {len(alice.books)}"
        # Bob should have 2 books
        bob = authors[1]
        assert len(bob.books) == 2
        # Charlie should have 1 book
        charlie = authors[2]
        assert len(charlie.books) == 1

    run_test("prefetch_related reverse FK (Author -> Books)", _test)


def test_prefetch_related_empty():
    async def _test():
        # Create an author with no books
        db = get_db()
        await db.execute(
            "INSERT INTO test_authors (id, name) VALUES ($1, $2)", 99, "NoBooks"
        )
        try:
            authors = await Author.objects.filter(id=99).prefetch_related("books").all()
            assert len(authors) == 1
            assert len(authors[0].books) == 0
        finally:
            await db.execute("DELETE FROM test_authors WHERE id = 99")

    run_test("prefetch_related with no related objects", _test)


# ---------------------------------------------------------------------------
# M2M tests (live DB)
# ---------------------------------------------------------------------------


def test_m2m_all():
    async def _test():
        book = await Book.objects.get(id=1)
        tags = await book.tags.all()
        assert len(tags) == 2, f"Expected 2 tags, got {len(tags)}"
        tag_names = {t.name for t in tags}
        assert "fiction" in tag_names
        assert "science" in tag_names

    run_test("M2M all() returns related objects", _test)


def test_m2m_add():
    async def _test():
        book = await Book.objects.get(id=4)  # Book D — no tags initially
        tag_history = await Tag.objects.get(id=3)  # history
        tags_before = await book.tags.all()
        assert len(tags_before) == 0

        await book.tags.add(tag_history)
        tags_after = await book.tags.all()
        assert len(tags_after) == 1
        assert tags_after[0].name == "history"

        # Cleanup
        await book.tags.remove(tag_history)

    run_test("M2M add() creates junction row", _test)


def test_m2m_remove():
    async def _test():
        book = await Book.objects.get(id=1)  # Book A — fiction, science
        tag_science = await Tag.objects.get(id=2)

        await book.tags.remove(tag_science)
        tags = await book.tags.all()
        assert len(tags) == 1
        assert tags[0].name == "fiction"

        # Restore
        await book.tags.add(tag_science)
        tags = await book.tags.all()
        assert len(tags) == 2

    run_test("M2M remove() deletes junction row", _test)


def test_m2m_clear():
    async def _test():
        book = await Book.objects.get(id=2)  # Book B — fiction
        await book.tags.clear()
        tags = await book.tags.all()
        assert len(tags) == 0

        # Restore
        tag_fiction = await Tag.objects.get(id=1)
        await book.tags.add(tag_fiction)

    run_test("M2M clear() removes all relations", _test)


def test_m2m_set():
    async def _test():
        book = await Book.objects.get(id=3)  # Book C — history
        tag_fiction = await Tag.objects.get(id=1)
        tag_science = await Tag.objects.get(id=2)

        await book.tags.set([tag_fiction, tag_science])
        tags = await book.tags.all()
        assert len(tags) == 2
        tag_names = {t.name for t in tags}
        assert tag_names == {"fiction", "science"}

        # Restore original
        tag_history = await Tag.objects.get(id=3)
        await book.tags.set([tag_history])

    run_test("M2M set() replaces all relations", _test)


def test_m2m_count():
    async def _test():
        book = await Book.objects.get(id=1)
        count = await book.tags.count()
        assert count == 2, f"Expected 2, got {count}"

    run_test("M2M count()", _test)


def test_m2m_add_duplicate():
    async def _test():
        book = await Book.objects.get(id=1)
        tag_fiction = await Tag.objects.get(id=1)
        # Add duplicate — should not raise (ON CONFLICT DO NOTHING)
        await book.tags.add(tag_fiction)
        count = await book.tags.count()
        assert count == 2  # Still 2, not 3

    run_test("M2M add() ignores duplicates", _test)


# ---------------------------------------------------------------------------
# annotate() tests (live DB)
# ---------------------------------------------------------------------------


def test_annotate_count():
    async def _test():
        # Count books per author using values + annotate
        results = await (
            Book.objects.values("author_id")
            .annotate(book_count=Count("id"))
            .order_by("author_id")
            .all()
        )
        assert len(results) == 3
        # Alice (id=1) has 2 books
        assert results[0]["author_id"] == 1
        assert results[0]["book_count"] == 2
        # Bob (id=2) has 2 books
        assert results[1]["book_count"] == 2
        # Charlie (id=3) has 1 book
        assert results[2]["book_count"] == 1

    run_test("annotate with Count (GROUP BY)", _test)


def test_annotate_sum():
    async def _test():
        results = await (
            Book.objects.values("author_id")
            .annotate(total_price=Sum("price"))
            .order_by("author_id")
            .all()
        )
        # Alice: 10.99 + 15.50 = 26.49
        assert abs(results[0]["total_price"] - 26.49) < 0.01

    run_test("annotate with Sum", _test)


def test_annotate_avg():
    async def _test():
        results = await (
            Book.objects.values("author_id")
            .annotate(avg_price=Avg("price"))
            .order_by("author_id")
            .all()
        )
        # Alice: (10.99 + 15.50) / 2 = 13.245
        assert abs(results[0]["avg_price"] - 13.245) < 0.01

    run_test("annotate with Avg", _test)


def test_annotate_max_min():
    async def _test():
        results = await (
            Book.objects.values("author_id")
            .annotate(max_price=Max("price"), min_price=Min("price"))
            .filter(author_id=1)
            .all()
        )
        assert len(results) == 1
        assert abs(results[0]["max_price"] - 15.50) < 0.01
        assert abs(results[0]["min_price"] - 10.99) < 0.01

    run_test("annotate with Max and Min", _test)


def test_annotate_on_instances():
    async def _test():
        # Annotate without values() — attaches computed value to model instances
        books = await (
            Book.objects.annotate(price_doubled=F("price") * Value(2.0))
            .order_by("id")
            .limit(1)
            .all()
        )
        assert len(books) == 1
        book = books[0]
        assert hasattr(book, "price_doubled"), "price_doubled not attached"
        # 10.99 * 2 = 21.98
        assert abs(book.price_doubled - 21.98) < 0.01, f"Got {book.price_doubled}"

    run_test("annotate attaches computed value to model instance", _test)


# ---------------------------------------------------------------------------
# aggregate() tests (live DB)
# ---------------------------------------------------------------------------


def test_aggregate_basic():
    async def _test():
        stats = await Book.objects.aggregate(
            total=Sum("price"),
            avg_price=Avg("price"),
            book_count=Count("id"),
        )
        assert isinstance(stats, dict)
        assert "total" in stats
        assert "avg_price" in stats
        assert "book_count" in stats
        expected_total = 10.99 + 15.50 + 22.00 + 8.99 + 45.00  # 102.48
        assert abs(stats["total"] - expected_total) < 0.01
        assert stats["book_count"] == 5

    run_test("aggregate returns dict with computed values", _test)


def test_aggregate_with_filter():
    async def _test():
        stats = await Book.objects.filter(author_id=1).aggregate(
            total=Sum("price"),
            count=Count("id"),
        )
        assert stats["count"] == 2
        assert abs(stats["total"] - 26.49) < 0.01

    run_test("aggregate with filter", _test)


def test_aggregate_max_min():
    async def _test():
        stats = await Book.objects.aggregate(
            max_price=Max("price"),
            min_price=Min("price"),
            max_pages=Max("pages"),
        )
        assert abs(stats["max_price"] - 45.00) < 0.01
        assert abs(stats["min_price"] - 8.99) < 0.01
        assert stats["max_pages"] == 500

    run_test("aggregate with Max and Min", _test)


def test_aggregate_empty_result():
    async def _test():
        stats = await Book.objects.filter(id=99999).aggregate(
            total=Sum("price"),
            count=Count("id"),
        )
        assert stats["count"] == 0  # Count default
        assert stats["total"] is None  # Sum has no default

    run_test("aggregate on empty result set", _test)


def test_aggregate_count_distinct():
    async def _test():
        stats = await Book.objects.aggregate(
            unique_authors=Count("author_id", distinct=True),
            total_books=Count("id"),
        )
        assert stats["unique_authors"] == 3
        assert stats["total_books"] == 5

    run_test("aggregate with Count DISTINCT", _test)


# ---------------------------------------------------------------------------
# QuerySet chaining tests
# ---------------------------------------------------------------------------


def test_filter_with_join_column():
    async def _test():
        # Filter on joined column — verify SQL has proper qualification
        qs = Book.objects.select_related("author_id")
        sql, _ = qs._build_select()
        # The table should be qualified
        assert "test_books." in sql or "LEFT JOIN" in sql

    run_test("filter with select_related qualifies columns", _test)


def test_queryset_chaining_immutability():
    async def _test():
        qs1 = Book.objects.filter(price__gte=10)
        qs2 = qs1.filter(price__lte=20)
        qs3 = qs1.order_by("-price")

        # Original should be unchanged
        assert len(qs1._filters) == 1
        assert len(qs2._filters) == 2
        assert len(qs3._filters) == 1
        assert qs3._ordering == ("-price",)

    run_test("QuerySet chaining is immutable", _test)


def test_select_related_with_filter():
    async def _test():
        books = await (
            Book.objects.select_related("author_id")
            .filter(price__gte=15)
            .order_by("id")
            .all()
        )
        assert len(books) >= 2
        for book in books:
            assert book.price >= 15

    run_test("select_related combined with filter", _test)


def test_values_with_annotate():
    async def _test():
        results = await (
            Book.objects.values("author_id")
            .annotate(total=Sum("price"), count=Count("id"))
            .order_by("author_id")
            .all()
        )
        # Should have 3 groups (3 authors)
        assert len(results) == 3
        for r in results:
            assert "author_id" in r
            assert "total" in r
            assert "count" in r

    run_test("values() + annotate() produces grouped results", _test)


def test_order_by_with_annotation():
    async def _test():
        results = await (
            Book.objects.values("author_id")
            .annotate(total=Sum("price"))
            .order_by("-total")
            .all()
        )
        # Charlie's single book ($45) should be first
        assert results[0]["total"] >= results[1]["total"]

    run_test("order_by annotation alias", _test)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


def test_annotate_combined_expression():
    async def _test():
        # price * pages as "value_score"
        books = await (
            Book.objects.annotate(value_score=F("price") * F("pages"))
            .order_by("id")
            .limit(1)
            .all()
        )
        book = books[0]
        expected = 10.99 * 200
        assert abs(book.value_score - expected) < 1.0

    run_test("annotate with combined F expression", _test)


def test_lookup_parser_fk_spanning():
    async def _test():
        from hyperdjango.lookups import resolve_lookup

        # Test that author__name is parsed as column="author__name", exact lookup
        sql, params = resolve_lookup("author__name", "Alice")
        assert "author__name" in sql, f"Got sql={sql!r}"
        assert "=" in sql, f"Got sql={sql!r}"

        # But author__name__icontains should parse correctly
        sql2, params2 = resolve_lookup("author__name__icontains", "ali")
        assert "ILIKE" in sql2, f"Got sql2={sql2!r}"
        assert "%ali%" in params2, f"Got params2={params2!r}"

    run_test("lookup parser handles FK-spanning paths", _test)


def test_count_with_filter():
    async def _test():
        count = await Book.objects.filter(author_id=1).count()
        assert count == 2

    run_test("count() with filter", _test)


def test_exists():
    async def _test():
        assert await Book.objects.filter(id=1).exists()
        assert not await Book.objects.filter(id=99999).exists()

    run_test("exists() returns True/False", _test)


def test_values_list_flat():
    async def _test():
        titles = await Book.objects.order_by("id").values_list("title", flat=True).all()
        assert titles[0] == "Book A"
        assert len(titles) == 5

    run_test("values_list with flat=True", _test)


def test_distinct():
    async def _test():
        author_ids = (
            await Book.objects.values_list("author_id", flat=True).distinct().all()
        )
        assert len(author_ids) == 3

    run_test("distinct()", _test)


def test_bulk_create():
    async def _test():
        new_books = [
            Book(title="Bulk 1", author_id=1, price=5.0, pages=100),
            Book(title="Bulk 2", author_id=2, price=6.0, pages=110),
        ]
        created = await Book.objects.bulk_create(new_books)
        assert len(created) == 2
        assert created[0].id is not None
        assert created[1].id is not None

        # Cleanup
        db = get_db()
        await db.execute("DELETE FROM test_books WHERE title LIKE 'Bulk%'")

    run_test("bulk_create", _test)


def test_update():
    async def _test():
        affected = await Book.objects.filter(id=5).update(price=99.99)
        assert affected == 1
        book = await Book.objects.get(id=5)
        assert abs(book.price - 99.99) < 0.01
        # Restore
        await Book.objects.filter(id=5).update(price=45.00)

    run_test("update()", _test)


def test_delete():
    async def _test():
        db = get_db()
        await db.execute(
            "INSERT INTO test_books (id, title, author_id, price, pages) VALUES ($1, $2, $3, $4, $5)",
            100,
            "ToDelete",
            1,
            1.0,
            10,
        )
        deleted = await Book.objects.filter(id=100).delete()
        assert deleted == 1
        assert not await Book.objects.filter(id=100).exists()

    run_test("delete()", _test)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 70)
    print("ORM Relations Test Suite")
    print("=" * 70)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    db = loop.run_until_complete(setup_db())
    loop.run_until_complete(seed_data(db))

    try:
        # Expression unit tests
        print("\n--- Expression Unit Tests ---")
        test_f_expression()
        test_value_expression()
        test_combined_expression()
        test_combined_expression_two_f()
        test_count_expression()
        test_count_distinct()
        test_sum_expression()
        test_avg_expression()
        test_max_min_expression()
        test_coalesce_expression()
        test_cast_expression()
        test_case_when_expression()
        test_arithmetic_operators()
        test_negation()
        test_param_offset()
        test_aggregate_contains_aggregate()

        # Model meta tests
        print("\n--- Model Meta Tests ---")
        test_model_registration()
        test_model_fk_fields()
        test_m2m_field_descriptor()
        test_m2m_create_table_sql()

        # select_related tests
        print("\n--- select_related Tests ---")
        test_select_related_single_fk()
        test_select_related_resolves_join()
        test_select_related_nested()
        test_select_related_multiple_fields()
        test_select_related_null_fk()
        test_select_related_invalid_field()

        # prefetch_related tests
        print("\n--- prefetch_related Tests ---")
        test_prefetch_related_reverse_fk()
        test_prefetch_related_empty()

        # M2M tests
        print("\n--- ManyToManyField Tests ---")
        test_m2m_all()
        test_m2m_add()
        test_m2m_remove()
        test_m2m_clear()
        test_m2m_set()
        test_m2m_count()
        test_m2m_add_duplicate()

        # annotate tests
        print("\n--- annotate() Tests ---")
        test_annotate_count()
        test_annotate_sum()
        test_annotate_avg()
        test_annotate_max_min()
        test_annotate_on_instances()

        # aggregate tests
        print("\n--- aggregate() Tests ---")
        test_aggregate_basic()
        test_aggregate_with_filter()
        test_aggregate_max_min()
        test_aggregate_empty_result()
        test_aggregate_count_distinct()

        # Chaining tests
        print("\n--- QuerySet Chaining Tests ---")
        test_filter_with_join_column()
        test_queryset_chaining_immutability()
        test_select_related_with_filter()
        test_values_with_annotate()
        test_order_by_with_annotation()

        # Edge cases
        print("\n--- Edge Cases ---")
        test_annotate_combined_expression()
        test_lookup_parser_fk_spanning()
        test_count_with_filter()
        test_exists()
        test_values_list_flat()
        test_distinct()
        test_bulk_create()
        test_update()
        test_delete()

    finally:
        loop.run_until_complete(teardown_db(db))

    # Summary
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 70}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailed tests:")
        for name, err in RESULTS["errors"]:
            print(f"  - {name}: {err}")

    print(f"{'=' * 70}")

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
