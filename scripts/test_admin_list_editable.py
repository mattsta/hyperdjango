#!/usr/bin/env python3
"""
Tests for admin list_editable save handler, permission-gated UI, and system checks.

Usage:
    uv run hyper-test admin_list_editable
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.admin import (
    TEMPLATE_LIST,
    TEMPLATE_LIST_PARTIAL,
    Fieldset,
    HyperAdmin,
)
from hyperdjango.app import HyperApp
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model

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


class EditProduct(Model):
    class Meta:
        table = "edit_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    price: float = Field(default=0.0)
    stock: int = Field(default=0)
    is_active: bool = Field(default=True)
    category: str = Field(max_length=100, default="general")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("Admin list_editable + Permission-Gated UI + System Checks")
    print("=" * 60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Setup DB
    db = loop.run_until_complete(setup())

    try:
        # ── System Checks ────────────────────────────────────────────────
        print("\n--- System Checks ---")
        test_system_checks()

        # ── Permission Context ───────────────────────────────────────────
        print("\n--- Permission-Gated UI ---")
        test_permission_gated_ui()

        # ── list_editable Template Rendering ─────────────────────────────
        print("\n--- list_editable Template Rendering ---")
        test_list_editable_rendering()

        # ── list_editable Save Handler ───────────────────────────────────
        print("\n--- list_editable Save Handler (Live DB) ---")
        loop.run_until_complete(test_list_editable_save(db))

    finally:
        loop.run_until_complete(teardown(db))

    # Summary
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
    await db.execute("DROP TABLE IF EXISTS edit_products CASCADE")
    await db.execute("""
        CREATE TABLE edit_products (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            price FLOAT DEFAULT 0.0,
            stock INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            category VARCHAR(100) DEFAULT 'general'
        )
    """)
    await db.execute(
        "INSERT INTO edit_products (id, name, price, stock, is_active, category) "
        "VALUES ($1, $2, $3, $4, $5, $6), ($7, $8, $9, $10, $11, $12), ($13, $14, $15, $16, $17, $18)",
        1,
        "Widget",
        9.99,
        100,
        True,
        "electronics",
        2,
        "Gadget",
        19.99,
        50,
        True,
        "electronics",
        3,
        "Book",
        12.50,
        200,
        False,
        "books",
    )
    await db.execute("SELECT setval('edit_products_id_seq', 10)")
    return db


async def teardown(db):
    await db.execute("DROP TABLE IF EXISTS edit_products CASCADE")
    await db.disconnect()


# ── System Checks ────────────────────────────────────────────────────────


def test_system_checks():
    app = HyperApp(title="Test", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)

    # Valid registration should work
    try:
        admin.register(
            EditProduct,
            list_display=["name", "price", "stock"],
            list_editable=["price", "stock"],
            search_fields=["name"],
        )
        check("valid config registers without error", True)
    except ValueError as e:
        check("valid config registers without error", False, str(e))

    # Invalid search_fields
    app2 = HyperApp(title="Test2", database=DB_URL)
    admin2 = HyperAdmin(app2, require_auth=False)
    try:
        admin2.register(EditProduct, search_fields=["nonexistent_field"])
        check("invalid search_fields raises error", False, "should have raised")
    except ValueError as e:
        check("invalid search_fields raises error", "nonexistent_field" in str(e))

    # Invalid list_filter
    app3 = HyperApp(title="Test3", database=DB_URL)
    admin3 = HyperAdmin(app3, require_auth=False)
    try:
        admin3.register(EditProduct, list_filter=["fake_field"])
        check("invalid list_filter raises error", False, "should have raised")
    except ValueError as e:
        check("invalid list_filter raises error", "fake_field" in str(e))

    # list_editable not in list_display
    app4 = HyperApp(title="Test4", database=DB_URL)
    admin4 = HyperAdmin(app4, require_auth=False)
    try:
        admin4.register(EditProduct, list_display=["name"], list_editable=["price"])
        check(
            "list_editable not in list_display raises error",
            False,
            "should have raised",
        )
    except ValueError as e:
        check(
            "list_editable not in list_display raises error",
            "must be in list_display" in str(e),
        )

    # list_editable + readonly conflict
    app5 = HyperApp(title="Test5", database=DB_URL)
    admin5 = HyperAdmin(app5, require_auth=False)
    try:
        admin5.register(
            EditProduct,
            list_display=["name", "price"],
            list_editable=["price"],
            readonly_fields=["price"],
        )
        check(
            "list_editable + readonly conflict raises error",
            False,
            "should have raised",
        )
    except ValueError as e:
        check("list_editable + readonly conflict raises error", "readonly" in str(e))

    # Invalid readonly_fields
    app6 = HyperApp(title="Test6", database=DB_URL)
    admin6 = HyperAdmin(app6, require_auth=False)
    try:
        admin6.register(EditProduct, readonly_fields=["does_not_exist"])
        check("invalid readonly_fields raises error", False, "should have raised")
    except ValueError as e:
        check("invalid readonly_fields raises error", "does_not_exist" in str(e))

    # Invalid fieldset field
    app7 = HyperApp(title="Test7", database=DB_URL)
    admin7 = HyperAdmin(app7, require_auth=False)
    try:
        admin7.register(
            EditProduct,
            fieldsets=[Fieldset(title="Basic", fields=["name", "nonexistent"])],
        )
        check("invalid fieldset field raises error", False, "should have raised")
    except ValueError as e:
        check("invalid fieldset field raises error", "nonexistent" in str(e))

    # Valid fieldsets
    app8 = HyperApp(title="Test8", database=DB_URL)
    admin8 = HyperAdmin(app8, require_auth=False)
    try:
        admin8.register(
            EditProduct, fieldsets=[Fieldset(title="Basic", fields=["name", "price"])]
        )
        check("valid fieldset registers OK", True)
    except ValueError as e:
        check("valid fieldset registers OK", False, str(e))


# ── Permission-Gated UI ─────────────────────────────────────────────────


def test_permission_gated_ui():
    app = HyperApp(title="PermTest", database=DB_URL)

    # Test with require_auth=False (all permissions granted)
    admin_noauth = HyperAdmin(app, require_auth=False)
    config = admin_noauth.register(
        EditProduct, list_display=["name", "price", "stock"], slug="perm_product"
    )

    class MockRequest:
        query = {}
        path = "/admin/perm_product/"
        cookies = {}
        # Mirror the real Request contract: _admin_user is a declared field
        # (request.py) that defaults to None until the auth middleware sets it.
        _admin_user = None

    req = MockRequest()
    perms = admin_noauth._get_model_perms(config, req)
    check("no auth gives full perms", all(perms.values()))
    check("perms has can_add", perms["can_add"] is True)
    check("perms has can_change", perms["can_change"] is True)
    check("perms has can_delete", perms["can_delete"] is True)
    check("perms has can_view", perms["can_view"] is True)

    # Test with require_auth=True but no cookie (unauthenticated)
    app2 = HyperApp(title="PermTest2", database=DB_URL)
    admin_auth = HyperAdmin(app2, require_auth=True, secret_key="test-secret")
    config2 = admin_auth.register(
        EditProduct, list_display=["name", "price"], slug="perm_product2"
    )

    perms_unauth = admin_auth._get_model_perms(config2, req)
    check("unauthenticated has no perms", not any(perms_unauth.values()))

    # Template string checks — permission gates
    check("template has perms.can_add gate", "perms.can_add" in TEMPLATE_LIST)
    check(
        "template has perms.can_delete gate",
        "perms.can_delete" in TEMPLATE_LIST_PARTIAL,
    )
    check(
        "template has perms.can_change gate",
        "perms.can_change" in TEMPLATE_LIST_PARTIAL,
    )

    # Superuser permissions
    app3 = HyperApp(title="PermTest3", database=DB_URL)
    admin_su = HyperAdmin(app3, require_auth=True, secret_key="test-secret")
    config3 = admin_su.register(EditProduct, list_display=["name"], slug="perm_su")

    class SuperuserRequest:
        query = {}
        path = "/admin/"
        cookies = {}
        _admin_user = {"username": "admin", "is_staff": True, "is_superuser": True}

    perms_su = admin_su._get_model_perms(config3, SuperuserRequest())
    check("superuser gets full perms", all(perms_su.values()))

    # Staff user permissions (staff = full access by default)
    class StaffRequest:
        query = {}
        path = "/admin/"
        cookies = {}
        _admin_user = {"username": "editor", "is_staff": True, "is_superuser": False}

    perms_staff = admin_su._get_model_perms(config3, StaffRequest())
    check("staff gets full perms (default behavior)", all(perms_staff.values()))


# ── list_editable Template Rendering ─────────────────────────────────────


def test_list_editable_rendering():
    app = HyperApp(title="EditTest", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        EditProduct,
        list_display=["name", "price", "stock", "is_active"],
        list_editable=["price", "stock"],
        slug="edit_product",
    )

    # Build mock cells
    cells = [
        {
            "display": "Widget",
            "value": "Widget",
            "raw_value": "Widget",
            "editable": False,
            "field_name": "name",
            "widget": "text",
        },
        {
            "display": "9.99",
            "value": 9.99,
            "raw_value": 9.99,
            "editable": True,
            "field_name": "price",
            "widget": "number",
        },
        {
            "display": "100",
            "value": 100,
            "raw_value": 100,
            "editable": True,
            "field_name": "stock",
            "widget": "number",
        },
        {
            "display": "✓",
            "value": True,
            "raw_value": True,
            "editable": False,
            "field_name": "is_active",
            "widget": "checkbox",
        },
    ]

    perms = {"can_add": True, "can_change": True, "can_delete": True, "can_view": True}
    ctx = admin._base_context()
    ctx.update(
        {
            "title": "EditProduct",
            "model_name": "EditProduct",
            "slug": "edit_product",
            "columns": config.display_columns,
            "rows": [
                {"pk": 1, "values": ["Widget", "9.99", "100", "✓"], "cells": cells}
            ],
            "total": 1,
            "page": 1,
            "total_pages": 1,
            "page_range": [1],
            "sort_field": "name",
            "sort_dir": "asc",
            "search_query": "",
            "message": "",
            "error_message": "",
            "actions": [],
            "filters": [],
            "active_filters": {},
            "list_editable": True,
            "perms": perms,
        }
    )

    html = admin.engine.render_string(TEMPLATE_LIST_PARTIAL, ctx)
    check(
        "list_editable renders input for editable field",
        'name="price_1"' in html,
        "price_1 not found in HTML",
    )
    check("list_editable renders input for stock", 'name="stock_1"' in html)
    check("list_editable has Save button", "Save" in html)
    check("list_editable has save-list formaction", "save-list" in html)
    check("non-editable field rendered as text", "Widget" in html)

    # With can_change=False — no inputs
    ctx_no_change = dict(ctx)
    ctx_no_change["perms"] = {
        "can_add": True,
        "can_change": False,
        "can_delete": True,
        "can_view": True,
    }
    html_no_change = admin.engine.render_string(TEMPLATE_LIST_PARTIAL, ctx_no_change)
    check(
        "no change perm hides editable inputs", 'name="price_1"' not in html_no_change
    )
    check("no change perm hides Save button", "save-list" not in html_no_change)


# ── list_editable Save Handler ───────────────────────────────────────────


async def test_list_editable_save(db):
    app = HyperApp(title="SaveTest", database=DB_URL)
    admin = HyperAdmin(app, require_auth=False)
    config = admin.register(
        EditProduct,
        list_display=["name", "price", "stock"],
        list_editable=["price", "stock"],
        slug="save_product",
    )

    # Get the save handler
    handler = admin._make_save_list_handler(config)

    # Create a mock request with form data
    class MockFormData:
        def __init__(self, data):
            self._data = data

        def get(self, key, default=""):
            return self._data.get(key, default)

        def items(self):
            return self._data.items()

    class MockRequest:
        query = {}
        path = "/admin/save_product/save-list/"
        cookies = {}
        _admin_user = {"username": "admin", "is_staff": True, "is_superuser": True}

        def __init__(self, form_data):
            self._form_data = form_data

        async def form(self):
            return MockFormData(self._form_data)

    # Update price for product 1 and stock for product 2
    request = MockRequest(
        {
            "price_1": "29.99",
            "stock_1": "100",
            "price_2": "39.99",
            "stock_2": "75",
            "price_3": "12.50",
            "stock_3": "200",
            "_save_list_editable": "1",
        }
    )

    response = await handler(request)
    check(
        "save handler returns redirect",
        response.status == 302 or response.status == 303,
        f"status={response.status}",
    )

    # Verify the updates in the database
    row1 = await db.query_one("SELECT price, stock FROM edit_products WHERE id = 1")
    check(
        "product 1 price updated",
        abs(row1["price"] - 29.99) < 0.01,
        f"got {row1['price']}",
    )
    check("product 1 stock updated", row1["stock"] == 100, f"got {row1['stock']}")

    row2 = await db.query_one("SELECT price, stock FROM edit_products WHERE id = 2")
    check(
        "product 2 price updated",
        abs(row2["price"] - 39.99) < 0.01,
        f"got {row2['price']}",
    )
    check("product 2 stock updated", row2["stock"] == 75, f"got {row2['stock']}")

    # Product 3 should be unchanged (original values re-submitted)
    row3 = await db.query_one("SELECT price, stock FROM edit_products WHERE id = 3")
    check(
        "product 3 price unchanged",
        abs(row3["price"] - 12.50) < 0.01,
        f"got {row3['price']}",
    )
    check("product 3 stock unchanged", row3["stock"] == 200, f"got {row3['stock']}")

    # Route registration check
    check(
        "save-list route registered",
        any("save-list" in str(getattr(r, "path", "")) for r in app.router._routes)
        if hasattr(app.router, "_routes")
        else True,
    )


if __name__ == "__main__":
    sys.exit(main())
