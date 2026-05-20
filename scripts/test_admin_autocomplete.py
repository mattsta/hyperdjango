#!/usr/bin/env python3
"""
Tests for admin FK autocomplete search.

Usage:
    uv run hyper-test admin_autocomplete
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from urllib.parse import urlencode

from hyperdjango.admin import TEMPLATE_FORM, HyperAdmin
from hyperdjango.app import HyperApp
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.request import Request

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class AcCategory(Model):
    class Meta:
        table = "ac_categories"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class AcProduct(Model):
    class Meta:
        table = "ac_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    category_id: int | None = Field(foreign_key=AcCategory, default=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("Admin FK Autocomplete Tests")
    print("=" * 60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = loop.run_until_complete(setup())

    try:
        test_template_has_fk_autocomplete()
        test_autocomplete_route_registered()
        test_form_field_has_foreign_key()
        loop.run_until_complete(test_autocomplete_endpoint(db))
        loop.run_until_complete(test_autocomplete_search(db))
        loop.run_until_complete(test_autocomplete_empty(db))
    finally:
        loop.run_until_complete(teardown(db))

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


async def setup():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    await db.execute("DROP TABLE IF EXISTS ac_products CASCADE")
    await db.execute("DROP TABLE IF EXISTS ac_categories CASCADE")
    await db.execute("""
        CREATE TABLE ac_categories (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE ac_products (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            category_id INTEGER REFERENCES ac_categories(id) ON DELETE CASCADE
        )
    """)
    await db.execute(
        "INSERT INTO ac_categories (id, name) VALUES ($1, $2), ($3, $4), ($5, $6), ($7, $8)",
        1,
        "Electronics",
        2,
        "Books",
        3,
        "Clothing",
        4,
        "Electronics & Gadgets",
    )
    await db.execute("SELECT setval('ac_categories_id_seq', 10)")
    return db


async def teardown(db):
    await db.execute("DROP TABLE IF EXISTS ac_products CASCADE")
    await db.execute("DROP TABLE IF EXISTS ac_categories CASCADE")
    await db.disconnect()


def test_template_has_fk_autocomplete():
    print("\n--- Template FK Autocomplete ---")

    check("template has foreign_key conditional", "f.foreign_key" in TEMPLATE_FORM)
    check("template has autocomplete endpoint", "autocomplete" in TEMPLATE_FORM)
    check("template has hidden input for FK", 'type="hidden"' in TEMPLATE_FORM)
    check("template has search input", "Search" in TEMPLATE_FORM)
    check(
        "template has debounced search",
        "setTimeout" in TEMPLATE_FORM or "delay:250ms" in TEMPLATE_FORM,
    )
    check("template has data-pk click handler", "data-pk" in TEMPLATE_FORM)


def test_autocomplete_route_registered():
    print("\n--- Autocomplete Route Registration ---")

    app = HyperApp(title="ACTest", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    admin.register(AcProduct, list_display=["name", "category_id"], slug="ac_product")

    routes = app.router.routes()
    route_patterns = [r.pattern for r in routes]
    check(
        "autocomplete route registered",
        any("autocomplete" in p for p in route_patterns),
        f"routes: {route_patterns}",
    )


def test_form_field_has_foreign_key():
    print("\n--- Form Field foreign_key attribute ---")

    app = HyperApp(title="ACTest2", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        AcProduct, list_display=["name", "category_id"], slug="ac_product2"
    )

    fields = admin._build_form_fields(config, values={})
    fk_field = next((f for f in fields if f["name"] == "category_id"), None)
    check("category_id field found", fk_field is not None)
    if fk_field:
        check(
            "has foreign_key attribute", fk_field.get("foreign_key") == "ac_categories"
        )
        check("has display_value", "display_value" in fk_field)


async def test_autocomplete_endpoint(db):
    print("\n--- Autocomplete Endpoint ---")

    app = HyperApp(title="ACTest3", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        AcProduct, list_display=["name", "category_id"], slug="ac_product3"
    )

    handler = admin._make_autocomplete_handler(config)

    def make_req(query_params):
        req = Request(
            method="GET",
            path="/admin/ac_product3/autocomplete/",
            query_string=urlencode(query_params),
        )
        req._admin_user = {"username": "admin", "is_staff": True, "is_superuser": True}
        return req

    # Test: search categories (non-empty query required)
    req = make_req({"field": "category_id", "q": "e"})
    response = await handler(req)
    html = response.body if hasattr(response, "body") else ""
    if hasattr(response, "_body"):
        html = response._body
    elif hasattr(response, "content"):
        html = response.content

    # The response should contain category names
    check("autocomplete returns HTML", len(str(html)) > 10, f"got: {html!r}")
    check("autocomplete has Electronics", "Electronics" in str(html))
    check("autocomplete has results", "data-pk" in str(html))
    check("autocomplete has data-pk", "data-pk" in str(html))


async def test_autocomplete_search(db):
    print("\n--- Autocomplete Search ---")

    app = HyperApp(title="ACTest4", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        AcProduct, list_display=["name", "category_id"], slug="ac_product4"
    )

    handler = admin._make_autocomplete_handler(config)

    def make_req4(query_params):
        req = Request(
            method="GET",
            path="/admin/ac_product4/autocomplete/",
            query_string=urlencode(query_params),
        )
        req._admin_user = {"username": "admin", "is_staff": True, "is_superuser": True}
        return req

    # Search for "elec" — should find "Electronics" and "Electronics & Gadgets"
    req = make_req4({"field": "category_id", "q": "elec"})
    response = await handler(req)
    html = str(
        getattr(
            response,
            "body",
            getattr(response, "_body", getattr(response, "content", "")),
        )
    )
    check("search finds Electronics", "Electronics" in html)
    check("search does NOT find Books", "Books" not in html or "Book" not in html)


async def test_autocomplete_empty(db):
    print("\n--- Autocomplete Empty Result ---")

    app = HyperApp(title="ACTest5", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        AcProduct, list_display=["name", "category_id"], slug="ac_product5"
    )

    handler = admin._make_autocomplete_handler(config)

    def make_req5(query_params):
        req = Request(
            method="GET",
            path="/admin/ac_product5/autocomplete/",
            query_string=urlencode(query_params),
        )
        req._admin_user = {"username": "admin", "is_staff": True, "is_superuser": True}
        return req

    # Search for something that doesn't exist
    req = make_req5({"field": "category_id", "q": "zzzznonexistent"})
    response = await handler(req)
    html = str(
        getattr(
            response,
            "body",
            getattr(response, "_body", getattr(response, "content", "")),
        )
    )
    check("empty search shows no results", "No results" in html)

    # Invalid field name
    req2 = make_req5({"field": "nonexistent_field", "q": ""})
    response2 = await handler(req2)
    html2 = str(
        getattr(
            response2,
            "body",
            getattr(response2, "_body", getattr(response2, "content", "")),
        )
    )
    check("invalid field returns empty", len(html2) < 10 or html2.strip() == "b''")


if __name__ == "__main__":
    sys.exit(main())
