#!/usr/bin/env python3
"""Test template override system for HyperAdmin.

Tests:
1. 3-level template resolution (model → admin → built-in default)
2. Explicit template override via register()
3. Inline template string override
4. Per-model CSS/JS media injection
5. formfield_overrides — widget type and attrs override
6. Template with custom blocks
7. Default fallback when no override exists
8. Media appears in rendered HTML
9. Multiple models with different overrides
10. Override interaction with fieldsets

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys
import tempfile
from pathlib import Path

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

from django.db import connection

from hyperdjango.admin import (
    TEMPLATE_FORM,
    TEMPLATE_LIST,
    Fieldset,
    HyperAdmin,
)
from hyperdjango.app import HyperApp
from hyperdjango.models import Field, Model

# ── Test Model ────────────────────────────────────────────────────────────


class TmplProduct(Model):
    class Meta:
        table = "tmpl_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    description: str = Field(max_length=2000, default="")
    price: float = Field(ge=0.0, default=0.0)
    quantity: int = Field(ge=0, default=0)
    is_active: bool = Field(default=True)


# ── Setup ─────────────────────────────────────────────────────────────────


def setup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS tmpl_products CASCADE")
        cursor.execute("""
            CREATE TABLE tmpl_products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                description TEXT DEFAULT '',
                price DOUBLE PRECISION DEFAULT 0.0,
                quantity INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)


def cleanup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS tmpl_products CASCADE")


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

    # ── 1. Default resolution (no override) ───────────────────────────────
    print("\n=== Default template resolution ===")

    app1 = HyperApp(title="Default")
    admin1 = HyperAdmin(app1, prefix="/def")
    config1 = admin1.register(TmplProduct)

    resolved = admin1._resolve_template(config1, "list", TEMPLATE_LIST)
    check("default returns built-in", resolved == TEMPLATE_LIST)

    resolved_form = admin1._resolve_template(config1, "form", TEMPLATE_FORM)
    check("default form returns built-in", resolved_form == TEMPLATE_FORM)

    # ── 2. Explicit template string override ──────────────────────────────
    print("\n=== Explicit template string override ===")

    app2 = HyperApp(title="Override")
    admin2 = HyperAdmin(app2, prefix="/ovr")
    config2 = admin2.register(
        TmplProduct,
        slug="product2",
        list_template="<h1>Custom List for {{ model_name }}</h1>",
        form_template="<h1>Custom Form for {{ model_name }}</h1>",
    )

    resolved_list = admin2._resolve_template(config2, "list", TEMPLATE_LIST)
    check("list override applied", "Custom List" in resolved_list)
    check("list override not default", resolved_list != TEMPLATE_LIST)

    resolved_form = admin2._resolve_template(config2, "form", TEMPLATE_FORM)
    check("form override applied", "Custom Form" in resolved_form)

    # Render with context
    html = admin2._render(
        resolved_list, {"model_name": "Product", "title": "test"}, config2
    )
    check("override renders correctly", "Custom List for Product" in html)

    # ── 3. Filesystem template override (3-level cascade) ─────────────────
    print("\n=== Filesystem template cascade ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create per-model template
        model_dir = Path(tmpdir) / "product3"
        model_dir.mkdir(parents=True)
        (model_dir / "list.html").write_text("<h1>Model-level: {{ model_name }}</h1>")

        # Create admin-level template
        admin_dir = Path(tmpdir) / "admin"
        admin_dir.mkdir(parents=True)
        (admin_dir / "form.html").write_text(
            "<h1>Admin-level form: {{ model_name }}</h1>"
        )

        app3 = HyperApp(title="FS", templates=tmpdir)
        admin3 = HyperAdmin(app3, prefix="/fs")
        config3 = admin3.register(TmplProduct, slug="product3")

        # Per-model list template found
        resolved = admin3._resolve_template(config3, "list", TEMPLATE_LIST)
        check("per-model list found", "Model-level" in resolved)

        # Admin-level form template found
        resolved_form = admin3._resolve_template(config3, "form", TEMPLATE_FORM)
        check("admin-level form found", "Admin-level form" in resolved_form)

        # Delete template not overridden → falls back to default
        resolved_del = admin3._resolve_template(config3, "delete", "DEFAULT_DELETE")
        check("no override → default", resolved_del == "DEFAULT_DELETE")

    # ── 4. Per-model CSS/JS media ─────────────────────────────────────────
    print("\n=== Per-model media injection ===")

    app4 = HyperApp(title="Media")
    admin4 = HyperAdmin(app4, prefix="/med")
    config4 = admin4.register(
        TmplProduct,
        slug="product4",
        media_css=["/static/products/style.css", "/static/products/extra.css"],
        media_js=["/static/products/charts.js"],
    )

    check(
        "media_css stored",
        config4.media_css
        == ["/static/products/style.css", "/static/products/extra.css"],
    )
    check("media_js stored", config4.media_js == ["/static/products/charts.js"])

    # Render and check media injection
    html = admin4._render(
        "{{ extra_media|safe }}<h1>test</h1>", {"title": "test"}, config4
    )
    check("CSS injected", "/static/products/style.css" in html)
    check("CSS extra injected", "/static/products/extra.css" in html)
    check("JS injected", "/static/products/charts.js" in html)
    check("CSS is link tag", '<link rel="stylesheet"' in html)
    check("JS is script tag", "<script src=" in html)

    # No media → no injection
    html_no_media = admin4._render(
        "{{ extra_media|safe }}<h1>test</h1>", {"title": "test"}
    )
    check("no config → no media", "/static/" not in html_no_media)

    # ── 5. formfield_overrides ────────────────────────────────────────────
    print("\n=== formfield_overrides ===")

    app5 = HyperApp(title="Overrides")
    admin5 = HyperAdmin(app5, prefix="/ffo")
    config5 = admin5.register(
        TmplProduct,
        slug="product5",
        formfield_overrides={
            str: {"widget": "textarea", "attrs": {"rows": "5"}},
            float: {"widget": "number", "attrs": {"step": "0.001"}},
            int: {"attrs": {"class": "custom-int"}},
        },
    )

    check("overrides stored", len(config5.formfield_overrides) == 3)

    # Build form fields and check overrides applied
    form_fields = admin5._build_form_fields(
        config5, values={"name": "Test", "price": 9.99}
    )

    name_field = next(f for f in form_fields if f["name"] == "name")
    check("str → textarea override", name_field["widget"] == "textarea")
    check("str attrs merged", name_field["attrs"].get("rows") == "5")
    check("str maxlength preserved", name_field["attrs"].get("maxlength") == 200)

    price_field = next(f for f in form_fields if f["name"] == "price")
    check("float step override", price_field["attrs"].get("step") == "0.001")

    qty_field = next(f for f in form_fields if f["name"] == "quantity")
    check("int class override", qty_field["attrs"].get("class") == "custom-int")

    # Fields without overrides unchanged
    active_field = next(f for f in form_fields if f["name"] == "is_active")
    check("bool not overridden", active_field["widget"] == "checkbox")

    # ── 6. Override + fieldsets combined ───────────────────────────────────
    print("\n=== Override + fieldsets combined ===")

    app6 = HyperApp(title="Combined")
    admin6 = HyperAdmin(app6, prefix="/cmb")
    config6 = admin6.register(
        TmplProduct,
        slug="product6",
        fieldsets=[
            Fieldset(title="Info", fields=["name", "description"]),
            Fieldset(title="Pricing", fields=["price", "quantity"]),
        ],
        formfield_overrides={str: {"widget": "textarea"}},
    )

    groups = admin6._build_form_field_groups(config6, values={"name": "Widget"})
    check("fieldsets with overrides", len(groups) >= 2)
    # Name should be textarea (override) in Info group
    info_group = groups[0]
    name_in_group = next(f for f in info_group["fields"] if f["name"] == "name")
    check("override applies in fieldset", name_in_group["widget"] == "textarea")

    # ── 7. Multiple models different overrides ────────────────────────────
    print("\n=== Multiple models ===")

    class TmplArticle(Model):
        class Meta:
            table = "tmpl_articles"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(max_length=300)
        body: str = Field(max_length=50000, default="")

    app7 = HyperApp(title="Multi")
    admin7 = HyperAdmin(app7, prefix="/mul")

    config_p = admin7.register(
        TmplProduct,
        slug="prod",
        media_css=["/static/prod.css"],
        formfield_overrides={str: {"widget": "text"}},
    )
    config_a = admin7.register(
        TmplArticle,
        slug="article",
        media_js=["/static/editor.js"],
        formfield_overrides={str: {"widget": "textarea", "attrs": {"rows": "10"}}},
    )

    # Each model has its own media
    html_p = admin7._render("{{ extra_media|safe }}", {"title": "t"}, config_p)
    html_a = admin7._render("{{ extra_media|safe }}", {"title": "t"}, config_a)
    check("prod has its CSS", "/static/prod.css" in html_p)
    check("prod no article JS", "/static/editor.js" not in html_p)
    check("article has its JS", "/static/editor.js" in html_a)
    check("article no prod CSS", "/static/prod.css" not in html_a)

    # Each model has its own widget overrides
    prod_fields = admin7._build_form_fields(config_p, values={})
    art_fields = admin7._build_form_fields(config_a, values={})
    prod_name = next(f for f in prod_fields if f["name"] == "name")
    art_title = next(f for f in art_fields if f["name"] == "title")
    check("prod name is text", prod_name["widget"] == "text")
    check("article title is textarea", art_title["widget"] == "textarea")
    check("article textarea rows", art_title["attrs"].get("rows") == "10")

    # ── 8. Template override attributes on ModelConfig ────────────────────
    print("\n=== ModelConfig template attrs ===")

    check("list_template None by default", config1.list_template is None)
    check("form_template None by default", config1.form_template is None)
    check("delete_template None by default", config1.delete_template is None)
    check(
        "list_template set",
        config2.list_template == "<h1>Custom List for {{ model_name }}</h1>",
    )

    # ── 9. _resolve_template with None config ─────────────────────────────
    print("\n=== Resolve with None config ===")

    resolved = admin1._resolve_template(None, "list", "DEFAULT")
    check("None config → default", resolved == "DEFAULT")

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All template override tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
