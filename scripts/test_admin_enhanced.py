#!/usr/bin/env python3
"""Test enhanced HyperAdmin features — fieldsets, filters, actions, callables, hooks.

Tests:
1. Fieldsets — grouped form layout with titles and collapsible sections
2. List filters — sidebar filter by field values
3. Bulk actions — select multiple + apply action (built-in delete, custom)
4. Callable list columns — computed display values
5. Save/delete hooks — lifecycle callbacks
6. Display columns — mixed model fields + callables
7. Grouped form field rendering — fieldset ordering, remaining fields
8. Action registration — built-in delete_selected auto-added
9. ModelConfig properties — filter_fields, grouped_form_fields, display_columns
10. Integration — all features combined

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

from enum import Enum

from django.db import connection

from hyperdjango.admin import (
    Action,
    Fieldset,
    HyperAdmin,
)
from hyperdjango.app import HyperApp
from hyperdjango.models import Field, Model

# ── Test Models ───────────────────────────────────────────────────────────


class Status(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class EnhAdminProduct(Model):
    class Meta:
        table = "enh_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    description: str = Field(max_length=2000, default="")
    price: float = Field(ge=0.0, default=0.0)
    quantity: int = Field(ge=0, default=0)
    status: Status = Field(default=Status.ACTIVE)
    category: str = Field(max_length=50, default="general")
    is_featured: bool = Field(default=False)


# ── DB setup ──────────────────────────────────────────────────────────────


def setup_tables():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS enh_products CASCADE")
        cursor.execute("""
            CREATE TABLE enh_products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                description TEXT DEFAULT '',
                price DOUBLE PRECISION DEFAULT 0.0,
                quantity INTEGER DEFAULT 0,
                status VARCHAR(20) DEFAULT 'active',
                category VARCHAR(50) DEFAULT 'general',
                is_featured BOOLEAN DEFAULT FALSE
            )
        """)
        for i in range(30):
            cat = ["electronics", "books", "clothing"][i % 3]
            status = ["active", "inactive", "archived"][i % 3]
            cursor.execute(
                "INSERT INTO enh_products (name, description, price, quantity, status, category, is_featured) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    f"Product {i}",
                    f"Description for product {i}",
                    round(i * 9.99, 2),
                    i * 5,
                    status,
                    cat,
                    i % 2 == 0,
                ],
            )


def cleanup_tables():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS enh_products CASCADE")


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

    setup_tables()

    app = HyperApp(title="Enhanced Admin Test")
    admin = HyperAdmin(app, prefix="/enh", title="Enhanced Admin")

    # ── 1. Fieldsets ──────────────────────────────────────────────────────
    print("\n=== Fieldsets ===")

    fieldsets = [
        Fieldset(title="Basic Info", fields=["name", "description"]),
        Fieldset(
            title="Pricing",
            fields=["price", "quantity"],
            description="Product pricing and stock",
        ),
        Fieldset(
            title="Classification",
            fields=["status", "category", "is_featured"],
            classes=["collapse"],
        ),
    ]

    config = admin.register(
        EnhAdminProduct,
        fieldsets=fieldsets,
        list_display=["name", "price", "quantity", "status", "category"],
    )

    check(
        "fieldsets stored", config.fieldsets is not None and len(config.fieldsets) == 3
    )
    check("fieldset 0 title", config.fieldsets[0].title == "Basic Info")
    check("fieldset 0 fields", config.fieldsets[0].fields == ["name", "description"])
    check(
        "fieldset 1 description",
        config.fieldsets[1].description == "Product pricing and stock",
    )
    check("fieldset 2 classes", "collapse" in config.fieldsets[2].classes)

    # grouped_form_fields
    groups = config.grouped_form_fields
    check("3 groups", len(groups) == 3)
    check("group 0 title", groups[0]["title"] == "Basic Info")
    check("group 0 field count", len(groups[0]["fields"]) == 2)
    check("group 1 fields", len(groups[1]["fields"]) == 2)
    check("group 2 classes", "collapse" in groups[2]["classes"])

    # ── 2. _build_form_field_groups ───────────────────────────────────────
    print("\n=== Form field groups ===")

    field_groups = admin._build_form_field_groups(
        config, values={"name": "Test", "price": 19.99}
    )
    check("groups have title", field_groups[0]["title"] == "Basic Info")
    name_field = next(f for f in field_groups[0]["fields"] if f["name"] == "name")
    check("name value in group", name_field["value"] == "Test")
    price_field = next(f for f in field_groups[1]["fields"] if f["name"] == "price")
    check("price value in group", price_field["value"] == 19.99)

    # ── 3. List filters ───────────────────────────────────────────────────
    print("\n=== List filters ===")

    app2 = HyperApp(title="Filter Test")
    admin2 = HyperAdmin(app2, prefix="/flt")

    config2 = admin2.register(
        EnhAdminProduct,
        slug="product2",
        list_display=["name", "price", "status", "category"],
        list_filter=["status", "category"],
    )

    check("list_filter set", config2.list_filter == ["status", "category"])
    filter_fields = config2.filter_fields
    check("filter fields count", len(filter_fields) == 2)
    check(
        "filter field names", [f.name for f in filter_fields] == ["status", "category"]
    )

    # ── 4. Bulk actions ───────────────────────────────────────────────────
    print("\n=== Bulk actions ===")

    action_log = []

    async def mark_featured(config, request, selected_ids):
        action_log.append(("mark_featured", selected_ids))
        return f"Marked {len(selected_ids)} as featured"

    custom_action = Action(
        name="mark_featured",
        label="Mark as featured",
        handler=mark_featured,
    )

    app3 = HyperApp(title="Action Test")
    admin3 = HyperAdmin(app3, prefix="/act")

    config3 = admin3.register(
        EnhAdminProduct,
        slug="product3",
        actions=[custom_action],
    )

    check("actions count", len(config3.actions) == 2)  # built-in delete + custom
    check("built-in delete action", config3.actions[0].name == "delete_selected")
    check("custom action", config3.actions[1].name == "mark_featured")
    check("custom action label", config3.actions[1].label == "Mark as featured")

    # No-action registration still gets delete_selected
    app4 = HyperApp(title="Default Actions")
    admin4 = HyperAdmin(app4, prefix="/def")
    config4 = admin4.register(EnhAdminProduct, slug="product4")
    check("default has delete action", len(config4.actions) == 1)
    check("default action is delete", config4.actions[0].name == "delete_selected")

    # ── 5. Callable list columns ──────────────────────────────────────────
    print("\n=== Callable list columns ===")

    def total_value(row):
        return f"${row.get('price', 0) * row.get('quantity', 0):.2f}"

    def status_badge(row):
        s = row.get("status", "")
        return f"[{s.upper()}]"

    app5 = HyperApp(title="Callable Test")
    admin5 = HyperAdmin(app5, prefix="/cal")

    config5 = admin5.register(
        EnhAdminProduct,
        slug="product5",
        list_display=["name", "price", "quantity", "total_value", "status_badge"],
        list_display_callables={
            "total_value": total_value,
            "status_badge": status_badge,
        },
    )

    columns = config5.display_columns
    check("5 display columns", len(columns) == 5)
    check("name is not callable", columns[0]["is_callable"] is False)
    check("total_value is callable", columns[3]["is_callable"] is True)
    check("total_value label", columns[3]["label"] == "Total Value")
    check("status_badge is callable", columns[4]["is_callable"] is True)

    # Test callable execution
    test_row = {"name": "Widget", "price": 10.0, "quantity": 5, "status": "active"}
    check("total_value calc", total_value(test_row) == "$50.00")
    check("status_badge calc", status_badge(test_row) == "[ACTIVE]")

    # ── 6. Save/delete hooks ──────────────────────────────────────────────
    print("\n=== Save/delete hooks ===")

    hook_log = []

    async def before_save(values, is_edit):
        hook_log.append(("save", is_edit, values.get("name")))
        return values

    async def before_delete(pk):
        hook_log.append(("delete", pk))

    app6 = HyperApp(title="Hook Test")
    admin6 = HyperAdmin(app6, prefix="/hk")

    config6 = admin6.register(
        EnhAdminProduct,
        slug="product6",
        save_hooks=[before_save],
        delete_hooks=[before_delete],
    )

    check("save hooks registered", len(config6.save_hooks) == 1)
    check("delete hooks registered", len(config6.delete_hooks) == 1)

    # ── 7. List editable ──────────────────────────────────────────────────
    print("\n=== List editable ===")

    app7 = HyperApp(title="Editable Test")
    admin7 = HyperAdmin(app7, prefix="/ed")

    config7 = admin7.register(
        EnhAdminProduct,
        slug="product7",
        list_display=["name", "price", "is_featured"],
        list_editable=["price", "is_featured"],
    )

    check("list_editable set", config7.list_editable == ["price", "is_featured"])

    # ── 8. Readonly fields ────────────────────────────────────────────────
    print("\n=== Readonly fields in fieldsets ===")

    app8 = HyperApp(title="Readonly Test")
    admin8 = HyperAdmin(app8, prefix="/ro")

    config8 = admin8.register(
        EnhAdminProduct,
        slug="product8",
        fieldsets=[
            Fieldset(title="Info", fields=["name", "category"]),
            Fieldset(title="Stats", fields=["price", "quantity"]),
        ],
        readonly_fields=["category"],
    )

    groups = admin8._build_form_field_groups(
        config8, values={"name": "X", "category": "electronics", "price": 5}
    )
    # Find category in the groups
    cat_field = None
    for g in groups:
        for f in g["fields"]:
            if f["name"] == "category":
                cat_field = f
    check(
        "category marked readonly", cat_field is not None and cat_field["is_readonly"]
    )

    # ── 9. Template rendering with new features ───────────────────────────
    print("\n=== Template rendering ===")

    # Dashboard renders with all models
    html = admin._render(
        "{{ admin_title }} models={{ registered_models|length }}",
        {"title": "test"},
    )
    check("render title", "Enhanced Admin" in html)

    # Form with fieldsets renders groups
    from hyperdjango.admin import TEMPLATE_FORM

    field_groups = admin._build_form_field_groups(
        config, values={"name": "Gadget", "price": 29.99}
    )
    html = admin._render(
        TEMPLATE_FORM,
        {
            "title": "Add Product",
            "model_name": "Product",
            "slug": "enhproduct",
            "field_groups": field_groups,
            "is_edit": False,
            "pk": None,
            "error": "",
        },
    )
    check("form has fieldset title", "Basic Info" in html)
    check("form has pricing group", "Pricing" in html)
    check("form has classification group", "Classification" in html)
    check("form has name input", 'name="name"' in html)

    # ── 10. Combined registration ─────────────────────────────────────────
    print("\n=== Combined all features ===")

    app_full = HyperApp(title="Full")
    admin_full = HyperAdmin(app_full, prefix="/full")

    config_full = admin_full.register(
        EnhAdminProduct,
        slug="full_product",
        list_display=["name", "price", "status", "category", "total_value"],
        list_display_callables={"total_value": total_value},
        search_fields=["name", "description"],
        list_filter=["status", "category"],
        fieldsets=[
            Fieldset(title="Product", fields=["name", "description", "category"]),
            Fieldset(title="Pricing", fields=["price", "quantity"]),
            Fieldset(
                title="Status", fields=["status", "is_featured"], classes=["collapse"]
            ),
        ],
        readonly_fields=["id"],
        actions=[custom_action],
        save_hooks=[before_save],
        delete_hooks=[before_delete],
        ordering="-id",
        per_page=10,
    )

    check("full: slug", config_full.slug == "full_product")
    check("full: 5 display cols", len(config_full.display_columns) == 5)
    check("full: callable in display", config_full.display_columns[4]["is_callable"])
    check("full: 2 filters", len(config_full.filter_fields) == 2)
    check("full: 3 fieldsets", len(config_full.fieldsets) == 3)
    check("full: 2 actions", len(config_full.actions) == 2)
    check("full: ordering", config_full.ordering == "-id")
    check("full: per_page", config_full.per_page == 10)
    check("full: save hooks", len(config_full.save_hooks) == 1)
    check("full: delete hooks", len(config_full.delete_hooks) == 1)

    # Verify routes registered
    routes = [(r.method, r.pattern) for r in app_full.router.routes()]
    check("full: list GET", ("GET", "/full/full_product/") in routes)
    check("full: list POST (actions)", ("POST", "/full/full_product/") in routes)
    check("full: add GET", ("GET", "/full/full_product/add/") in routes)
    check("full: add POST", ("POST", "/full/full_product/add/") in routes)
    check("full: edit GET", ("GET", "/full/full_product/{id}/") in routes)
    check("full: delete POST", ("POST", "/full/full_product/{id}/delete/") in routes)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup_tables()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All enhanced admin tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
