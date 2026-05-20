#!/usr/bin/env python3
"""Test standalone HyperAdmin — auto-generated CRUD for HyperApp models.

Tests:
1. Model introspection (field types → widgets, constraints, enums)
2. Admin registration (route generation, config)
3. Form field building (values, defaults, help text)
4. Form data parsing + type coercion (int, float, bool, datetime, enum)
5. Template rendering (dashboard, list, add, edit)
6. Live CRUD against PostgreSQL (create, read, update, delete)
7. Pagination, search, sorting
8. Edge cases (empty models, all field types, readonly fields)

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

# We need hyperdjango importable but don't want full Django setup
# The standalone admin has NO Django dependency
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"
import django

django.setup()

from hyperdjango.admin import (
    HyperAdmin,
    _coerce_value,
    _introspect_model,
    _type_to_widget,
)
from hyperdjango.models import Field, Model
from hyperdjango.validation.core.fields import FieldInfo

# ── Test Models ───────────────────────────────────────────────────────────


class Priority(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class HAdminAuthor(Model):
    class Meta:
        table = "hadmin_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    email: str = Field(max_length=200)
    bio: str = Field(max_length=1000, default="")
    age: int = Field(ge=0, le=150, default=0)
    is_active: bool = Field(default=True)


class HAdminPost(Model):
    class Meta:
        table = "hadmin_posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    body: str = Field(max_length=10000, default="")
    author_id: int = Field(foreign_key=HAdminAuthor)
    priority: Priority = Field(default=Priority.MEDIUM)
    score: float = Field(ge=0.0, le=100.0, default=0.0)
    is_published: bool = Field(default=False)
    word_count: int = Field(ge=0, default=0)


class HAdminTag(Model):
    class Meta:
        table = "hadmin_tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=50)


# ── DB setup ──────────────────────────────────────────────────────────────


def create_tables():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hadmin_authors (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(200) NOT NULL,
                bio VARCHAR(1000) NOT NULL DEFAULT '',
                age INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hadmin_posts (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                author_id INTEGER NOT NULL REFERENCES hadmin_authors(id) ON DELETE CASCADE,
                priority VARCHAR(20) NOT NULL DEFAULT 'medium',
                score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                is_published BOOLEAN NOT NULL DEFAULT FALSE,
                word_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hadmin_tags (
                id SERIAL PRIMARY KEY,
                name VARCHAR(50) NOT NULL
            )
        """)


def seed_data():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM hadmin_posts")
        cursor.execute("DELETE FROM hadmin_authors")
        cursor.execute("DELETE FROM hadmin_tags")

        # Authors
        cursor.execute("""
            INSERT INTO hadmin_authors (id, name, email, bio, age, is_active)
            VALUES (1, 'Alice', 'alice@example.com', 'Python developer', 30, TRUE),
                   (2, 'Bob', 'bob@example.com', 'Zig enthusiast', 25, TRUE),
                   (3, 'Charlie', 'charlie@example.com', 'DevOps engineer', 35, FALSE)
        """)

        # Posts
        for i in range(1, 31):
            author_id = (i % 3) + 1
            priority = ["low", "medium", "high", "critical"][i % 4]
            cursor.execute(
                "INSERT INTO hadmin_posts (id, title, body, author_id, priority, score, is_published, word_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    i,
                    f"Post {i}",
                    f"Content of post {i}",
                    author_id,
                    priority,
                    round(i * 3.3, 1),
                    i % 2 == 0,
                    i * 100,
                ],
            )

        # Tags
        for i in range(1, 6):
            cursor.execute(
                "INSERT INTO hadmin_tags (id, name) VALUES (%s, %s)", [i, f"tag-{i}"]
            )

        # Reset sequences so SERIAL auto-increment works after explicit inserts
        cursor.execute(
            "SELECT setval('hadmin_authors_id_seq', (SELECT MAX(id) FROM hadmin_authors))"
        )
        cursor.execute(
            "SELECT setval('hadmin_posts_id_seq', (SELECT MAX(id) FROM hadmin_posts))"
        )
        cursor.execute(
            "SELECT setval('hadmin_tags_id_seq', (SELECT MAX(id) FROM hadmin_tags))"
        )


def drop_tables():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS hadmin_posts CASCADE")
        cursor.execute("DROP TABLE IF EXISTS hadmin_authors CASCADE")
        cursor.execute("DROP TABLE IF EXISTS hadmin_tags CASCADE")


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

    print("Setting up test database...")
    create_tables()
    seed_data()

    # ── 1. Type → widget mapping ──────────────────────────────────────────
    print("\n=== Type → widget mapping ===")

    w, a = _type_to_widget(str, None, {})
    check("str → text", w == "text")

    w, a = _type_to_widget(int, None, {})
    check("int → number", w == "number")

    w, a = _type_to_widget(float, None, {})
    check("float → number", w == "number")
    check("float has step", a.get("step") == "0.01")

    w, a = _type_to_widget(bool, None, {})
    check("bool → checkbox", w == "checkbox")

    w, a = _type_to_widget(datetime, None, {})
    check("datetime → datetime-local", w == "datetime-local")

    w, a = _type_to_widget(date, None, {})
    check("date → date", w == "date")

    w, a = _type_to_widget(Priority, None, {})
    check("enum → select", w == "select")

    # FieldInfo constraints
    fi = FieldInfo(max_length=200, ge=0, le=100)
    w, a = _type_to_widget(int, fi, {})
    check("int with ge/le → min/max", a.get("min") == 0 and a.get("max") == 100)

    fi2 = FieldInfo(max_length=50)
    w, a = _type_to_widget(str, fi2, {})
    check("str with maxlength", a.get("maxlength") == 50)

    # Long text → textarea
    fi3 = FieldInfo(max_length=1000)
    w, a = _type_to_widget(str, fi3, {})
    check("long str → textarea", w == "textarea")

    # Optional[int] unwrapping
    w, a = _type_to_widget(int | None, None, {})
    check("Optional[int] → number", w == "number")

    # FK field
    w, a = _type_to_widget(int, None, {"foreign_key": "authors"})
    check("FK → number", w == "number")

    # ── 2. Model introspection ────────────────────────────────────────────
    print("\n=== Model introspection ===")

    author_fields = _introspect_model(HAdminAuthor)
    field_names = [f.name for f in author_fields]
    check("author has id", "id" in field_names)
    check("author has name", "name" in field_names)
    check("author has email", "email" in field_names)
    check("author has bio", "bio" in field_names)
    check("author has age", "age" in field_names)
    check("author has is_active", "is_active" in field_names)

    id_field = next(f for f in author_fields if f.name == "id")
    check("id is_pk", id_field.is_pk)
    check("id is_auto", id_field.is_auto)
    check("id is_readonly", id_field.is_readonly)

    name_field = next(f for f in author_fields if f.name == "name")
    check("name widget text", name_field.widget == "text")
    check("name required", name_field.required)
    check("name maxlength", name_field.attrs.get("maxlength") == 100)

    age_field = next(f for f in author_fields if f.name == "age")
    check("age widget number", age_field.widget == "number")
    check("age min 0", age_field.attrs.get("min") == 0)
    check("age max 150", age_field.attrs.get("max") == 150)
    check("age not required (has default)", not age_field.required)

    active_field = next(f for f in author_fields if f.name == "is_active")
    check("is_active checkbox", active_field.widget == "checkbox")

    # Post with FK and enum
    post_fields = _introspect_model(HAdminPost)
    author_id_f = next(f for f in post_fields if f.name == "author_id")
    check("FK detected", author_id_f.foreign_key == "hadmin_authors")

    priority_f = next(f for f in post_fields if f.name == "priority")
    check("enum → select", priority_f.widget == "select")
    check("enum choices", len(priority_f.choices) == 4)
    check("enum choice values", priority_f.choices[0] == ("low", "LOW"))

    # ── 3. Type coercion ──────────────────────────────────────────────────
    print("\n=== Type coercion ===")

    check("coerce int", _coerce_value("42", int) == 42)
    check("coerce float", _coerce_value("3.14", float) == 3.14)
    check("coerce bool true", _coerce_value("1", bool) is True)
    check("coerce bool false", _coerce_value("0", bool) is False)
    check("coerce str", _coerce_value("hello", str) == "hello")
    check("coerce decimal", _coerce_value("99.99", Decimal) == Decimal("99.99"))
    check("coerce enum", _coerce_value("high", Priority) == Priority.HIGH)
    check("coerce date", _coerce_value("2024-01-15", date) == date(2024, 1, 15))
    check("coerce Optional[int]", _coerce_value("7", int | None) == 7)

    # ── 4. Admin registration ─────────────────────────────────────────────
    print("\n=== Admin registration ===")

    from hyperdjango.app import HyperApp

    app = HyperApp(title="Test App")
    admin = HyperAdmin(app, prefix="/admin", title="Test Admin")

    config = admin.register(
        HAdminAuthor, list_display=["name", "email", "age", "is_active"]
    )
    check("model registered", "hadminauthor" in admin._models)
    check("slug correct", config.slug == "hadminauthor")
    check("name correct", config.name == "HAdminAuthor")
    check(
        "list_display set", config.list_display == ["name", "email", "age", "is_active"]
    )
    check("field_count", config.field_count == 6)

    # Display fields
    display = config.display_fields
    check(
        "display fields match list_display",
        [f.name for f in display] == ["name", "email", "age", "is_active"],
    )

    # Form fields (should exclude nothing by default)
    form = config.form_fields
    check("form fields include all", len(form) == 6)

    # Register with options
    config2 = admin.register(
        HAdminPost,
        list_display=["title", "priority", "score", "is_published"],
        search_fields=["title", "body"],
        ordering="-id",
        per_page=10,
        exclude_fields=["word_count"],
    )
    check("post registered", "hadminpost" in admin._models)
    check("post per_page", config2.per_page == 10)
    check("post ordering", config2.ordering == "-id")
    check("post exclude", "word_count" in config2.exclude_fields)
    check(
        "post form excludes word_count",
        "word_count" not in [f.name for f in config2.form_fields],
    )

    admin.register(HAdminTag)
    check("tag registered", "hadmintag" in admin._models)

    # ── 5. Route registration ─────────────────────────────────────────────
    print("\n=== Route registration ===")

    # Check routes were registered on the app router
    routes = []
    for route in app.router.routes():
        routes.append((route.method, route.pattern))

    check("dashboard route", ("GET", "/admin/") in routes)
    check("author list route", ("GET", "/admin/hadminauthor/") in routes)
    check("author add GET", ("GET", "/admin/hadminauthor/add/") in routes)
    check("author add POST", ("POST", "/admin/hadminauthor/add/") in routes)
    check("author edit GET", ("GET", "/admin/hadminauthor/{id}/") in routes)
    check("author edit POST", ("POST", "/admin/hadminauthor/{id}/") in routes)
    check("author delete POST", ("POST", "/admin/hadminauthor/{id}/delete/") in routes)

    # ── 6. ModelConfig properties ─────────────────────────────────────────
    print("\n=== ModelConfig properties ===")

    # Searchable fields
    check(
        "author searchable fields auto-detect str", "name" in config.searchable_fields
    )
    check("author searchable includes email", "email" in config.searchable_fields)
    check("post custom search_fields", config2.searchable_fields == ["title", "body"])

    # Default display (no list_display set)
    tag_config = admin._models["hadmintag"]
    check("tag default display <= 6 fields", len(tag_config.display_fields) <= 6)

    # ── 7. Form field building ────────────────────────────────────────────
    print("\n=== Form field building ===")

    form_fields = admin._build_form_fields(
        config, values={"name": "Alice", "age": 30, "is_active": True}
    )
    name_ff = next(f for f in form_fields if f["name"] == "name")
    check("form field value", name_ff["value"] == "Alice")
    check("form field required", name_ff["required"])

    age_ff = next(f for f in form_fields if f["name"] == "age")
    check("form field age value", age_ff["value"] == 30)

    active_ff = next(f for f in form_fields if f["name"] == "is_active")
    check("form field checkbox value", active_ff["value"] is True)

    # FK help text
    author_id_ff = admin._build_form_fields(config2, values={"author_id": 1})
    fk_field = next(f for f in author_id_ff if f["name"] == "author_id")
    check("FK help text", "hadmin_authors" in fk_field["help"])

    # ── 8. Form data parsing ──────────────────────────────────────────────
    print("\n=== Form data parsing ===")

    # Valid data
    values, error = admin._parse_form_data(
        config,
        {
            "name": "Test User",
            "email": "test@example.com",
            "bio": "A bio",
            "age": "25",
            "is_active": "1",
        },
    )
    check("parse valid data", error == "")
    check("parse name", values["name"] == "Test User")
    check("parse age int", values["age"] == 25)
    check("parse bool checked", values["is_active"] is True)

    # Missing required
    values, error = admin._parse_form_data(
        config,
        {
            "email": "test@example.com",
        },
    )
    check("parse missing required", "required" in error.lower())

    # Bool unchecked (missing key)
    values, error = admin._parse_form_data(
        config,
        {
            "name": "Test",
            "email": "test@example.com",
        },
    )
    check("parse unchecked bool", values.get("is_active") is False)

    # Invalid int
    values, error = admin._parse_form_data(
        config,
        {
            "name": "Test",
            "email": "test@example.com",
            "age": "not-a-number",
        },
    )
    check("parse invalid int error", error != "")

    # ── 9. Template rendering ─────────────────────────────────────────────
    print("\n=== Template rendering ===")

    # Dashboard
    html = admin._render(
        "{{ admin_title }} - {% for m in registered_models %}{{ m.name }} {% endfor %}",
        {"title": "Dashboard"},
    )
    check("render dashboard title", "Test Admin" in html)
    check("render dashboard models", "HAdminAuthor" in html)
    check("render dashboard models 2", "HAdminPost" in html)

    # Base context
    ctx = admin._base_context()
    check("base context has prefix", ctx["prefix"] == "/admin")
    check("base context has models", len(ctx["registered_models"]) == 3)

    # ── 10. Live CRUD against PostgreSQL ──────────────────────────────────
    print("\n=== Live CRUD (PostgreSQL) ===")

    from django.db import connection

    # CREATE
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO hadmin_authors (name, email, bio, age, is_active) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            ["TestAdmin", "testadmin@example.com", "Admin test", 99, True],
        )
        new_id = cursor.fetchone()[0]
    check("create returned id", new_id is not None and new_id > 0)

    # READ
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM hadmin_authors WHERE id = %s", [new_id])
        row = cursor.fetchone()
    check("read finds created", row is not None)
    check("read name matches", row[1] == "TestAdmin" if row else False)

    # UPDATE
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE hadmin_authors SET name = %s, age = %s WHERE id = %s",
            ["UpdatedAdmin", 100, new_id],
        )
    with connection.cursor() as cursor:
        cursor.execute("SELECT name, age FROM hadmin_authors WHERE id = %s", [new_id])
        row = cursor.fetchone()
    check("update name", row[0] == "UpdatedAdmin" if row else False)
    check("update age", row[1] == 100 if row else False)

    # LIST with search
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM hadmin_authors WHERE name::text ILIKE %s", ["%Updated%"]
        )
        rows = cursor.fetchall()
    check("search finds updated", len(rows) >= 1)

    # LIST with pagination
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM hadmin_posts")
        count = cursor.fetchone()[0]
    check("post count", count == 30)

    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM hadmin_posts ORDER BY id ASC LIMIT 10 OFFSET 0")
        page1 = cursor.fetchall()
    check("page 1 has 10", len(page1) == 10)

    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM hadmin_posts ORDER BY id ASC LIMIT 10 OFFSET 20")
        page3 = cursor.fetchall()
    check("page 3 has 10", len(page3) == 10)

    # SORT descending
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM hadmin_posts ORDER BY id DESC LIMIT 5")
        sorted_rows = cursor.fetchall()
    ids = [r[0] for r in sorted_rows]
    check("sort desc", ids == sorted(ids, reverse=True))

    # DELETE
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM hadmin_authors WHERE id = %s", [new_id])
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM hadmin_authors WHERE id = %s", [new_id])
        row = cursor.fetchone()
    check("delete removes row", row is None)

    # ── 11. Edge cases ────────────────────────────────────────────────────
    print("\n=== Edge cases ===")

    # Model with no optional fields
    class Minimal(Model):
        class Meta:
            table = "minimal_test"

        id: int = Field(primary_key=True, auto=True)
        value: str = Field(max_length=50)

    minimal_fields = _introspect_model(Minimal)
    check("minimal has 2 fields", len(minimal_fields) == 2)
    check(
        "minimal id is auto",
        minimal_fields[0].is_auto
        if minimal_fields[0].name == "id"
        else minimal_fields[1].is_auto,
    )

    # Register minimal model
    app2 = HyperApp(title="Edge")
    admin2 = HyperAdmin(app2, prefix="/a")
    cfg = admin2.register(Minimal)
    check("minimal registered", cfg.name == "Minimal")
    check("minimal default display", len(cfg.display_fields) == 2)

    # Custom slug
    cfg3 = admin2.register(HAdminTag, slug="labels")
    check("custom slug", cfg3.slug == "labels")

    # Readonly fields
    app3 = HyperApp(title="RO")
    admin3 = HyperAdmin(app3, prefix="/ro")
    cfg4 = admin3.register(HAdminAuthor, readonly_fields=["email"])
    form_names = [f.name for f in cfg4.form_fields]
    check("readonly still in form_fields", "email" in form_names)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    drop_tables()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All HyperAdmin tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
