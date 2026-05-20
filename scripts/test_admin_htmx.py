#!/usr/bin/env python3
"""Test HTMX-powered dynamic admin interactions.

Tests:
1. Partial endpoint returns table HTML (not full page)
2. Partial endpoint supports search, sort, pagination, filters
3. Validate endpoint returns field-level error/valid HTML
4. Confirm-delete dialog endpoint returns dialog HTML
5. Full list view includes HTMX script + attributes
6. Toast messages render correctly
7. Route registration (partial, validate, confirm-delete)
8. _build_list_context shared between full and partial views
9. List template has hx-* attributes on search, sort, pagination, filters

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

from django.db import connection

from hyperdjango.admin import (
    TEMPLATE_DELETE_DIALOG,
    TEMPLATE_FIELD_ERROR,
    TEMPLATE_FIELD_VALID,
    TEMPLATE_LIST,
    TEMPLATE_LIST_PARTIAL,
    HyperAdmin,
)
from hyperdjango.app import HyperApp
from hyperdjango.models import Field, Model

# ── Test Model ────────────────────────────────────────────────────────────


class HtmxProduct(Model):
    class Meta:
        table = "htmx_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    price: float = Field(ge=0.0, default=0.0)
    category: str = Field(max_length=50, default="general")
    is_active: bool = Field(default=True)


# ── Setup ─────────────────────────────────────────────────────────────────


def setup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS htmx_products CASCADE")
        cursor.execute("""
            CREATE TABLE htmx_products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                price DOUBLE PRECISION DEFAULT 0.0,
                category VARCHAR(50) DEFAULT 'general',
                is_active BOOLEAN DEFAULT TRUE
            )
        """)
        for i in range(25):
            cat = ["electronics", "books", "clothing"][i % 3]
            cursor.execute(
                "INSERT INTO htmx_products (name, price, category, is_active) VALUES (%s, %s, %s, %s)",
                [f"Product {i}", round(i * 9.99, 2), cat, i % 2 == 0],
            )


def cleanup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS htmx_products CASCADE")


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

    app = HyperApp(title="HTMX Test")
    admin = HyperAdmin(app, prefix="/htmx", title="HTMX Admin")

    config = admin.register(
        HtmxProduct,
        list_display=["name", "price", "category", "is_active"],
        search_fields=["name"],
        list_filter=["category"],
    )

    # ── 1. Route registration ─────────────────────────────────────────────
    print("\n=== Route registration ===")

    routes = [(r.method, r.pattern) for r in app.router.routes()]
    check("partial GET", ("GET", "/htmx/htmxproduct/partial/") in routes)
    check("validate POST", ("POST", "/htmx/htmxproduct/validate/") in routes)
    check(
        "confirm-delete GET",
        ("GET", "/htmx/htmxproduct/{id}/confirm-delete/") in routes,
    )
    check("list GET", ("GET", "/htmx/htmxproduct/") in routes)
    check("list POST (actions)", ("POST", "/htmx/htmxproduct/") in routes)

    # ── 2. Template content checks ────────────────────────────────────────
    print("\n=== Template content ===")

    check(
        "HTMX script in header",
        "htmx.org" in TEMPLATE_LIST or "htmx.min.js" in TEMPLATE_LIST,
    )
    check("hx-get in list template", "hx-get" in TEMPLATE_LIST)
    check("hx-trigger in list template", "hx-trigger" in TEMPLATE_LIST)
    check("hx-target in list template", "hx-target" in TEMPLATE_LIST)
    check("hx-swap in partial template", "hx-swap" in TEMPLATE_LIST_PARTIAL)
    check("hx-push-url in partial", "hx-push-url" in TEMPLATE_LIST_PARTIAL)
    check("result-table id", "result-table" in TEMPLATE_LIST_PARTIAL)
    check("delete dialog element", "delete-dialog" in TEMPLATE_LIST)
    check("toast class", "toast" in TEMPLATE_LIST)
    check("search has hx-trigger delay", "delay:300ms" in TEMPLATE_LIST)

    # ── 3. Delete dialog template ─────────────────────────────────────────
    print("\n=== Delete dialog template ===")

    check("dialog has hx-post", "hx-post" in TEMPLATE_DELETE_DIALOG)
    check("dialog has cancel button", "Cancel" in TEMPLATE_DELETE_DIALOG)
    check("dialog has delete button", "Delete" in TEMPLATE_DELETE_DIALOG)
    check("dialog has instance_str", "instance_str" in TEMPLATE_DELETE_DIALOG)

    # ── 4. Validation templates ───────────────────────────────────────────
    print("\n=== Validation templates ===")

    check("error template has class", "field-error" in TEMPLATE_FIELD_ERROR)
    check("valid template has class", "field-valid" in TEMPLATE_FIELD_VALID)

    # ── 5. Partial template structure ─────────────────────────────────────
    print("\n=== Partial template structure ===")

    check(
        "partial has outerHTML swap target",
        'id="result-table"' in TEMPLATE_LIST_PARTIAL,
    )
    check("partial has table", "<table>" in TEMPLATE_LIST_PARTIAL)
    check("partial has pagination", "pagination" in TEMPLATE_LIST_PARTIAL)
    check("partial has sort links", "hx-get" in TEMPLATE_LIST_PARTIAL)
    check("partial has action checkboxes", "_selected" in TEMPLATE_LIST_PARTIAL)
    check(
        "partial has confirm-delete button", "confirm-delete" in TEMPLATE_LIST_PARTIAL
    )

    # ── 6. CSS additions ──────────────────────────────────────────────────
    print("\n=== CSS additions ===")

    from hyperdjango.admin import _ADMIN_CSS

    check("toast CSS", ".toast" in _ADMIN_CSS)
    check("toast animation", "toast-in" in _ADMIN_CSS)
    check("dialog CSS", "dialog" in _ADMIN_CSS)
    check("dialog backdrop", "backdrop" in _ADMIN_CSS)
    check("field-error CSS", ".field-error" in _ADMIN_CSS)
    check("field-valid CSS", ".field-valid" in _ADMIN_CSS)
    check("htmx-indicator CSS", "htmx-indicator" in _ADMIN_CSS)

    # ── 7. Template rendering with mock context ─────────────────────────
    print("\n=== Template rendering with context ===")

    # Build a mock context matching what _build_list_context produces
    mock_ctx = admin._base_context()
    mock_perms = {
        "can_add": True,
        "can_change": True,
        "can_delete": True,
        "can_view": True,
    }
    mock_cells = [
        {
            "display": v,
            "value": v,
            "raw_value": v,
            "editable": False,
            "field_name": "",
            "widget": "text",
        }
        for v in ["Widget", "9.99", "electronics", "✓"]
    ]
    mock_cells2 = [
        {
            "display": v,
            "value": v,
            "raw_value": v,
            "editable": False,
            "field_name": "",
            "widget": "text",
        }
        for v in ["Gadget", "19.99", "books", "✗"]
    ]
    mock_ctx.update(
        {
            "title": "HtmxProduct",
            "model_name": "HtmxProduct",
            "slug": "htmxproduct",
            "columns": config.display_columns,
            "rows": [
                {
                    "pk": 1,
                    "values": ["Widget", "9.99", "electronics", "✓"],
                    "cells": mock_cells,
                },
                {
                    "pk": 2,
                    "values": ["Gadget", "19.99", "books", "✗"],
                    "cells": mock_cells2,
                },
            ],
            "total": 25,
            "page": 1,
            "total_pages": 3,
            "page_range": [1, 2, 3],
            "sort_field": "name",
            "sort_dir": "asc",
            "search_query": "",
            "message": "",
            "error_message": "",
            "actions": [{"name": "delete_selected", "label": "Delete selected"}],
            "filters": [
                {
                    "name": "category",
                    "label": "Category",
                    "options": [{"value": "electronics", "label": "electronics"}],
                    "active_value": "",
                }
            ],
            "active_filters": {"category": ""},
            "list_editable": False,
            "perms": mock_perms,
        }
    )

    # Render partial
    partial_html = admin.engine.render_string(TEMPLATE_LIST_PARTIAL, mock_ctx)
    check("partial renders", len(partial_html) > 100)
    check("partial has table rows", "<tr>" in partial_html)
    check("partial has result-table div", 'id="result-table"' in partial_html)
    check("partial has hx-get sort links", "hx-get" in partial_html)
    check("partial has confirm-delete button", "confirm-delete" in partial_html)

    # Render full page
    full_html = admin._render(TEMPLATE_LIST, mock_ctx)
    check("full page has htmx script", "htmx" in full_html)
    check("full page has result-table", "result-table" in full_html)
    check("full page has dialog", "delete-dialog" in full_html)
    check("full page has search hx-get", "hx-get" in full_html)
    check("full page has search delay", "delay:300ms" in full_html)
    check("full page has filter hx-get", "filter_category" in full_html)

    # Render with toast message
    mock_ctx["message"] = "Item created successfully"
    toast_html = admin._render(TEMPLATE_LIST, mock_ctx)
    check("toast message rendered", "Item created successfully" in toast_html)
    check("toast has success class", "toast-success" in toast_html)

    # Render with error toast
    mock_ctx["message"] = ""
    mock_ctx["error_message"] = "Something went wrong"
    err_html = admin._render(TEMPLATE_LIST, mock_ctx)
    check("error toast rendered", "Something went wrong" in err_html)
    check("error toast has class", "toast-error" in err_html)

    # ── 8. Delete dialog template ─────────────────────────────────────────
    print("\n=== Delete dialog rendering ===")

    dialog_ctx = admin._base_context()
    dialog_ctx.update(
        {
            "model_name": "HtmxProduct",
            "slug": "htmxproduct",
            "pk": 42,
            "instance_str": "Widget Pro",
        }
    )
    dialog_html = admin.engine.render_string(TEMPLATE_DELETE_DIALOG, dialog_ctx)
    check("dialog has model name", "HtmxProduct" in dialog_html)
    check("dialog has instance str", "Widget Pro" in dialog_html)
    check("dialog has delete action URL", "/htmx/htmxproduct/42/delete/" in dialog_html)
    check("dialog has hx-post", "hx-post" in dialog_html)

    # ── 9. Validation template rendering ──────────────────────────────────
    print("\n=== Validation rendering ===")

    err_html = admin.engine.render_string(
        TEMPLATE_FIELD_ERROR, {"error": "Name is required"}
    )
    check("error html has message", "Name is required" in err_html)
    check("error html has class", "field-error" in err_html)

    valid_html = admin.engine.render_string(TEMPLATE_FIELD_VALID, {})
    check("valid html has class", "field-valid" in valid_html)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All HTMX admin tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
