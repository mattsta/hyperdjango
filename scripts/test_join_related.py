"""Tests for QuerySet.join_related() — task #196.

`join_related(**aliases)` is the non-destructive variant of
`select_related` that PRESERVES FK integer columns and attaches
the related instances on sibling attribute names. Designed for
tight-coupling cases like hyperticket where FK columns are read
as integers downstream by many handlers.

Tests cover:
1. FK integer columns are preserved (not overwritten)
2. Alias attributes hold the related instance
3. Alias is None when the FK itself is NULL
4. Works alongside plain `select_related`
5. Chaining `join_related` calls accumulates aliases
6. .all() hydrates multiple rows correctly

Usage:
    uv run hyper-test join_related
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


class JrAuthor(Model):
    class Meta:
        table = "jr_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class JrCategory(Model):
    class Meta:
        table = "jr_categories"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class JrBook(Model):
    class Meta:
        table = "jr_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    author_id: int = Field(foreign_key=JrAuthor)
    category_id: int | None = Field(foreign_key=JrCategory, default=None)


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    for sql in [
        "DROP TABLE IF EXISTS jr_books CASCADE",
        "DROP TABLE IF EXISTS jr_categories CASCADE",
        "DROP TABLE IF EXISTS jr_authors CASCADE",
        """CREATE TABLE jr_authors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL
        )""",
        """CREATE TABLE jr_categories (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL
        )""",
        """CREATE TABLE jr_books (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            author_id INTEGER NOT NULL REFERENCES jr_authors(id) ON DELETE CASCADE,
            category_id INTEGER REFERENCES jr_categories(id) ON DELETE SET NULL DEFAULT NULL
        )""",
    ]:
        await db.execute(sql)
    return db


async def teardown_db(db):
    for tbl in ("jr_books", "jr_categories", "jr_authors"):
        await db.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


async def main() -> int:
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    db = await setup_db()
    try:
        alice = JrAuthor(name="Alice")
        await alice.save()
        fiction = JrCategory(name="Fiction")
        await fiction.save()

        book1 = JrBook(title="Book One", author_id=alice.id, category_id=fiction.id)
        await book1.save()
        book2 = JrBook(title="Book Two", author_id=alice.id, category_id=None)
        await book2.save()

        # ── Test 1: FK int columns preserved ──────────────────────────
        print("\n=== FK int columns preserved ===")

        book = (
            await JrBook.objects.join_related(
                author="author_id", category="category_id"
            )
            .filter(id=book1.id)
            .first()
        )
        check("book fetched", book is not None)
        check(
            "author_id stays int",
            isinstance(book.author_id, int),
            f"got {type(book.author_id).__name__}",
        )
        check("author_id == alice.id", book.author_id == alice.id)
        check(
            "category_id stays int",
            isinstance(book.category_id, int),
            f"got {type(book.category_id).__name__}",
        )
        check("category_id == fiction.id", book.category_id == fiction.id)

        # ── Test 2: Alias attributes hold related instances ───────────
        print("\n=== Alias attributes hold related instances ===")

        check(
            "book.author is JrAuthor instance",
            isinstance(book.author, JrAuthor),
            f"got {type(book.author).__name__}",
        )
        check("book.author.name == 'Alice'", book.author.name == "Alice")
        check(
            "book.category is JrCategory instance",
            isinstance(book.category, JrCategory),
            f"got {type(book.category).__name__}",
        )
        check("book.category.name == 'Fiction'", book.category.name == "Fiction")

        # ── Test 3: Alias is None when FK is NULL ─────────────────────
        print("\n=== NULL FK → alias is None ===")

        book2_fetched = (
            await JrBook.objects.join_related(
                author="author_id", category="category_id"
            )
            .filter(id=book2.id)
            .first()
        )
        check("book2 fetched", book2_fetched is not None)
        check(
            "book2.author_id preserved",
            book2_fetched.author_id == alice.id,
        )
        check(
            "book2.category_id is None",
            book2_fetched.category_id is None,
            f"got {book2_fetched.category_id}",
        )
        check("book2.author loaded", isinstance(book2_fetched.author, JrAuthor))
        check(
            "book2.category is None (no JOIN match)",
            book2_fetched.category is None,
            f"got {book2_fetched.category}",
        )

        # ── Test 4: select_related still works (destructive) ──────────
        print("\n=== Plain select_related still REPLACES FK ===")

        book_sr = (
            await JrBook.objects.select_related("author_id").filter(id=book1.id).first()
        )
        check(
            "select_related replaces author_id with JrAuthor instance",
            isinstance(book_sr.author_id, JrAuthor),
            f"got {type(book_sr.author_id).__name__}",
        )
        check(
            "select_related.author_id.name == 'Alice'",
            book_sr.author_id.name == "Alice",
        )

        # ── Test 5: Chaining join_related accumulates ─────────────────
        print("\n=== Chaining accumulates aliases ===")

        book_chain = (
            await JrBook.objects.join_related(author="author_id")
            .join_related(category="category_id")
            .filter(id=book1.id)
            .first()
        )
        check("chain: author_id int", isinstance(book_chain.author_id, int))
        check("chain: category_id int", isinstance(book_chain.category_id, int))
        check("chain: author attached", isinstance(book_chain.author, JrAuthor))
        check(
            "chain: category attached",
            isinstance(book_chain.category, JrCategory),
        )

        # ── Test 6: .all() hydrates all rows with aliases ─────────────
        print("\n=== .all() hydrates all rows ===")

        books_all = (
            await JrBook.objects.join_related(author="author_id").order_by("id").all()
        )
        check("got 2 books", len(books_all) == 2)
        check(
            "all books have author_id as int",
            all(isinstance(b.author_id, int) for b in books_all),
        )
        check(
            "all books have author attribute attached",
            all(isinstance(b.author, JrAuthor) for b in books_all),
        )

    finally:
        await teardown_db(db)
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All join_related tests passed!")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
