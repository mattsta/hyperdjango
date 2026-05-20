#!/usr/bin/env python3
"""Test inline editing + related objects in HyperAdmin.

Tests:
1. InlineConfig dataclass — fields, extra, max_num, can_delete, fk_field
2. FK auto-detection — finds FK field pointing to parent model
3. Inline field introspection — _get_inline_fields
4. Registration with inlines — routes, config
5. Inline context building — existing rows, empty rows, columns
6. Inline row HTMX endpoint — returns single row HTML
7. Inline template rendering — table with existing + empty rows
8. Combined: parent with multiple inlines
9. Inline with custom fields selection
10. Inline with ordering

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

import asyncio

from asgiref.sync import sync_to_async
from django.db import connection

from hyperdjango.admin import (
    TEMPLATE_INLINE_ROW,
    HyperAdmin,
    InlineConfig,
    _detect_fk_field,
    _get_inline_fields,
)
from hyperdjango.app import HyperApp
from hyperdjango.models import Field, Model

# ── Test Models ───────────────────────────────────────────────────────────


class InlAuthor(Model):
    class Meta:
        table = "inl_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    email: str = Field(max_length=200, default="")


class InlBook(Model):
    class Meta:
        table = "inl_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    year: int = Field(ge=1900, le=2100, default=2024)
    author_id: int = Field(foreign_key=InlAuthor)


class InlChapter(Model):
    class Meta:
        table = "inl_chapters"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    order_num: int = Field(ge=0, default=0)
    book_id: int = Field(foreign_key=InlBook)


# ── Setup ─────────────────────────────────────────────────────────────────


def setup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS inl_chapters CASCADE")
        cursor.execute("DROP TABLE IF EXISTS inl_books CASCADE")
        cursor.execute("DROP TABLE IF EXISTS inl_authors CASCADE")
        cursor.execute("""CREATE TABLE inl_authors (
            id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL, email VARCHAR(200) DEFAULT '')""")
        cursor.execute("""CREATE TABLE inl_books (
            id SERIAL PRIMARY KEY, title VARCHAR(200) NOT NULL, year INTEGER DEFAULT 2024,
            author_id INTEGER NOT NULL REFERENCES inl_authors(id) ON DELETE CASCADE)""")
        cursor.execute("""CREATE TABLE inl_chapters (
            id SERIAL PRIMARY KEY, title VARCHAR(200) NOT NULL, order_num INTEGER DEFAULT 0,
            book_id INTEGER NOT NULL REFERENCES inl_books(id) ON DELETE CASCADE)""")

        # Seed data
        cursor.execute(
            "INSERT INTO inl_authors (id, name, email) VALUES (1, 'Alice', 'alice@test.com')"
        )
        cursor.execute(
            "INSERT INTO inl_books (id, title, year, author_id) VALUES (1, 'Book A', 2023, 1), (2, 'Book B', 2024, 1)"
        )
        cursor.execute(
            "INSERT INTO inl_chapters (id, title, order_num, book_id) VALUES (1, 'Ch 1', 1, 1), (2, 'Ch 2', 2, 1), (3, 'Ch 3', 3, 1)"
        )
        cursor.execute("SELECT setval('inl_authors_id_seq', 1)")
        cursor.execute("SELECT setval('inl_books_id_seq', 2)")
        cursor.execute("SELECT setval('inl_chapters_id_seq', 3)")


def cleanup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS inl_chapters CASCADE")
        cursor.execute("DROP TABLE IF EXISTS inl_books CASCADE")
        cursor.execute("DROP TABLE IF EXISTS inl_authors CASCADE")


# ── Tests ─────────────────────────────────────────────────────────────────


def main():
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

    setup()

    # ── 1. InlineConfig ───────────────────────────────────────────────────
    print("\n=== InlineConfig ===")

    ic = InlineConfig(
        model_class=InlBook, fields=["title", "year"], extra=2, can_delete=True
    )
    check("model_class set", ic.model_class is InlBook)
    check("fields set", ic.fields == ["title", "year"])
    check("extra=2", ic.extra == 2)
    check("can_delete=True", ic.can_delete)
    check("fk_field None initially", ic.fk_field is None)
    check("max_num None", ic.max_num is None)

    # ── 2. FK auto-detection ──────────────────────────────────────────────
    print("\n=== FK auto-detection ===")

    fk = _detect_fk_field(InlBook, InlAuthor)
    check("book→author FK detected", fk == "author_id", f"got {fk}")

    fk2 = _detect_fk_field(InlChapter, InlBook)
    check("chapter→book FK detected", fk2 == "book_id", f"got {fk2}")

    fk3 = _detect_fk_field(InlAuthor, InlBook)
    check("no FK → None", fk3 is None)

    # ── 3. Inline field introspection ─────────────────────────────────────
    print("\n=== Inline field introspection ===")

    ic_book = InlineConfig(model_class=InlBook, fk_field="author_id")
    book_fields = _get_inline_fields(ic_book)
    field_names = [f.name for f in book_fields]
    check("book inline excludes id", "id" not in field_names)
    check("book inline excludes fk", "author_id" not in field_names)
    check("book inline has title", "title" in field_names)
    check("book inline has year", "year" in field_names)

    # With custom fields
    ic_custom = InlineConfig(
        model_class=InlBook, fields=["title"], fk_field="author_id"
    )
    custom_fields = _get_inline_fields(ic_custom)
    check("custom fields only title", [f.name for f in custom_fields] == ["title"])

    # ── 4. Registration with inlines ──────────────────────────────────────
    print("\n=== Registration with inlines ===")

    app = HyperApp(title="Inline Test")
    admin = HyperAdmin(app, prefix="/inl")

    config = admin.register(
        InlAuthor,
        list_display=["name", "email"],
        inlines=[
            InlineConfig(model_class=InlBook, fields=["title", "year"], extra=1),
        ],
    )

    check("inlines registered", len(config.inlines) == 1)
    check("FK auto-detected on register", config.inlines[0].fk_field == "author_id")

    # Check routes
    routes = [(r.method, r.pattern) for r in app.router.routes()]
    check("inline-row route", ("GET", "/inl/inlauthor/inline-row/") in routes)

    # ── 5. Inline context building ────────────────────────────────────────
    print("\n=== Inline context building ===")

    # SyncDB wrapper for async tests
    class SyncDB:
        def _query_sync(self, sql, params):
            with connection.cursor() as cursor:
                converted = sql
                for i in range(len(params), 0, -1):
                    converted = converted.replace(f"${i}", "%s")
                cursor.execute(converted, list(params))
                return cursor.fetchall()

        async def query(self, sql, *params):
            return await sync_to_async(self._query_sync)(sql, params)

        async def query_one(self, sql, *params):
            rows = await self.query(sql, *params)
            return rows[0] if rows else None

        async def query_val(self, sql, *params):
            row = await self.query_one(sql, *params)
            return row[0] if row else None

        async def execute(self, sql, *params):
            def _exec():
                with connection.cursor() as cursor:
                    converted = sql
                    for i in range(len(params), 0, -1):
                        converted = converted.replace(f"${i}", "%s")
                    cursor.execute(converted, list(params))

            await sync_to_async(_exec)()

    admin.app._db = SyncDB()

    # Build context for author 1 (has 2 books)
    inlines_ctx = asyncio.run(admin._build_inline_context(config, parent_pk=1))
    check("1 inline group", len(inlines_ctx) == 1)

    book_inline = inlines_ctx[0]
    check("inline slug", book_inline["slug"] == "inlbook")
    check("inline name", book_inline["name"] == "InlBook")
    check(
        "2 existing rows",
        len(book_inline["rows"]) == 2,
        f"got {len(book_inline['rows'])}",
    )
    check("1 empty row", len(book_inline["empty_rows"]) == 1)
    check("2 columns", len(book_inline["columns"]) == 2)
    check(
        "column names", [c["name"] for c in book_inline["columns"]] == ["title", "year"]
    )
    check("can_delete", book_inline["can_delete"])
    check("total=3", book_inline["total"] == 3)
    check("initial=2", book_inline["initial"] == 2)

    # Existing row has data
    row0 = book_inline["rows"][0]
    check("row0 has pk", row0["pk"] is not None)
    check("row0 has fields", len(row0["fields"]) == 2)
    title_f = row0["fields"][0]
    check(
        "row0 title value", title_f["value"] == "Book A" or title_f["value"] == "Book B"
    )

    # Empty row for new entry
    empty0 = book_inline["empty_rows"][0]
    check("empty row has fields", len(empty0["fields"]) == 2)

    # No parent PK → no existing rows, only empty
    inlines_new = asyncio.run(admin._build_inline_context(config, parent_pk=None))
    check("new: no existing rows", len(inlines_new[0]["rows"]) == 0)
    check("new: has empty rows", len(inlines_new[0]["empty_rows"]) == 1)

    # ── 6. Inline template rendering ──────────────────────────────────────
    print("\n=== Inline template rendering ===")

    inline_html = admin._render_inline_html(config, inlines_ctx)
    check("inline HTML rendered", len(inline_html) > 100)
    check("has inline table", "<table" in inline_html)
    check("has existing row data", "Book A" in inline_html or "Book B" in inline_html)
    check("has add button", "Add InlBook" in inline_html)
    check("has hx-get on add", "hx-get" in inline_html)
    check("has delete checkbox", "DELETE" in inline_html)
    check("has hidden id", 'name="inline_inlbook-0-id"' in inline_html)

    # No inlines → empty string
    empty_html = admin._render_inline_html(config, [])
    check("no inlines → empty", empty_html == "")

    # ── 7. Inline row HTMX endpoint template ─────────────────────────────
    print("\n=== Inline row template ===")

    row_ctx = {
        "inline_slug": "inlbook",
        "prefix_name": "inline_inlbook",
        "index": 5,
        "fields": [
            {
                "name": "title",
                "widget": "text",
                "value": "",
                "attrs": {"maxlength": 200},
                "choices": [],
            },
            {
                "name": "year",
                "widget": "number",
                "value": 2024,
                "attrs": {"min": 1900, "max": 2100},
                "choices": [],
            },
        ],
        "can_delete": True,
    }
    row_html = admin.engine.render_string(TEMPLATE_INLINE_ROW, row_ctx)
    check("row has tr", "<tr" in row_html)
    check("row has correct index", "new-5" in row_html)
    check("row has title input", 'name="inline_inlbook-new-5-title"' in row_html)
    check("row has year input", 'name="inline_inlbook-new-5-year"' in row_html)

    # ── 8. Multiple inlines ───────────────────────────────────────────────
    print("\n=== Multiple inlines ===")

    app2 = HyperApp(title="Multi Inline")
    admin2 = HyperAdmin(app2, prefix="/mi")
    admin2.app._db = SyncDB()

    config2 = admin2.register(
        InlBook,
        slug="book",
        inlines=[
            InlineConfig(
                model_class=InlChapter, fields=["title", "order_num"], extra=2
            ),
        ],
    )

    check("chapter FK detected", config2.inlines[0].fk_field == "book_id")

    # Build for book 1 (has 3 chapters)
    inlines_ctx2 = asyncio.run(admin2._build_inline_context(config2, parent_pk=1))
    check("chapter inline exists", len(inlines_ctx2) == 1)
    check("3 existing chapters", len(inlines_ctx2[0]["rows"]) == 3)
    check("2 empty chapters", len(inlines_ctx2[0]["empty_rows"]) == 2)

    # ── 9. Inline with ordering ───────────────────────────────────────────
    print("\n=== Inline with ordering ===")

    app3 = HyperApp(title="Ordered")
    admin3 = HyperAdmin(app3, prefix="/ord")
    admin3.app._db = SyncDB()

    config3 = admin3.register(
        InlBook,
        slug="ordered_book",
        inlines=[
            InlineConfig(model_class=InlChapter, ordering="order_num"),
        ],
    )

    inlines_ord = asyncio.run(admin3._build_inline_context(config3, parent_pk=1))
    if inlines_ord and inlines_ord[0]["rows"]:
        orders = []
        for r in inlines_ord[0]["rows"]:
            for f in r["fields"]:
                if f["name"] == "order_num":
                    orders.append(f["value"])
        check("ordered by order_num", orders == sorted(orders), f"got {orders}")

    # ── 10. Form renders with inlines ─────────────────────────────────────
    print("\n=== Form with inlines ===")

    from hyperdjango.admin import TEMPLATE_FORM

    field_groups = admin._build_form_field_groups(
        config, values={"name": "Alice", "email": "a@b.com"}
    )
    inline_html = admin._render_inline_html(config, inlines_ctx)
    html = admin._render(
        admin._resolve_template(config, "form", TEMPLATE_FORM),
        {
            "title": "Edit Author",
            "model_name": "InlAuthor",
            "slug": "inlauthor",
            "field_groups": field_groups,
            "inline_html": inline_html,
            "is_edit": True,
            "pk": 1,
            "error": "",
        },
        config,
    )
    check("form has inline section", "InlBook" in html)
    check("form has inline table", "<table" in html)
    check("form has add button", "Add InlBook" in html)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All inline tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
