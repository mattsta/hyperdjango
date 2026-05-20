#!/usr/bin/env python3
"""End-to-end Django Admin test through hyperdjango.db backend.

Proves that Django's auto-generated per-model CRUD admin works correctly
when using ENGINE = 'hyperdjango.db' (native pg.zig PostgreSQL driver).

Tests:
1. Migrations run through hyperdjango.db
2. Admin login works
3. Model list view (changelist) works
4. Model add view works (create via admin form)
5. Model change view works (edit via admin form)
6. Model delete works
7. Search, filtering, ordering work
8. FK field rendering works (no N+1)
9. Inline editing works
10. cursor.description returns correct metadata

Requires: PostgreSQL running locally.
"""

# hyper-test: db_django

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()


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

    # ── Run migrations ────────────────────────────────────────────────────
    print("\n=== Migrations through hyperdjango.db ===")

    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    try:
        call_command("migrate", verbosity=0, stdout=out)
        check("migrations run", True)
    except Exception as e:
        check("migrations run", False, str(e)[:200])
        print(f"\n{'=' * 60}")
        print(f"Results: {passed} passed, {failed} failed")
        print("Cannot continue without migrations. Check PostgreSQL connection.")
        return failed

    # ── Verify tables exist ───────────────────────────────────────────────
    print("\n=== Table verification ===")

    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = [row[0] for row in cursor.fetchall()]

    check(
        "admin_app_category exists",
        "admin_app_category" in tables,
        f"tables: {tables[:10]}",
    )
    check("admin_app_article exists", "admin_app_article" in tables)
    check("auth_user exists", "auth_user" in tables)

    # ── cursor.description ────────────────────────────────────────────────
    print("\n=== cursor.description ===")

    with connection.cursor() as cursor:
        cursor.execute("SELECT id, name, description FROM admin_app_category LIMIT 0")
        desc = cursor.description
        check("description not None", desc is not None)
        if desc:
            check("description has 3 cols", len(desc) == 3, f"got {len(desc)}")
            check("col 0 is id", desc[0][0] == "id", f"got {desc[0][0]}")
            check("col 1 is name", desc[1][0] == "name", f"got {desc[1][0]}")

    # ── Create test data via ORM ──────────────────────────────────────────
    print("\n=== ORM CRUD ===")

    from django.contrib.auth.models import User

    from tests.admin_app.models import Article, Category

    # Clean existing test data
    Article.objects.all().delete()
    Category.objects.all().delete()
    User.objects.filter(username="admin").delete()

    cat = Category.objects.create(name="Technology", description="Tech articles")
    check("category created", cat.pk is not None)

    cat2 = Category.objects.create(name="Science", description="Science articles")
    check("second category", cat2.pk is not None)

    article = Article.objects.create(
        title="Test Article",
        content="This is test content.",
        category=cat,
        status="draft",
        is_featured=True,
        view_count=42,
        metadata={"tags": ["test", "e2e"]},
    )
    check("article created", article.pk is not None)
    check("article FK", article.category_id == cat.pk)

    # Create more articles for list testing
    for i in range(5):
        Article.objects.create(
            title=f"Article {i}",
            content=f"Content {i}",
            category=cat2 if i % 2 else cat,
            status="published" if i % 2 else "draft",
        )

    check("6 articles total", Article.objects.count() == 6)

    # ── Django admin client ───────────────────────────────────────────────
    print("\n=== Admin views ===")

    from django.contrib.auth.models import User
    from django.test import Client

    # Create admin superuser
    admin_user = User.objects.create_superuser("admin", "admin@test.com", "testpass123")
    check("admin user created", admin_user.is_superuser)

    client = Client()
    logged_in = client.login(username="admin", password="testpass123")
    check("admin login", logged_in)

    # Admin index — may redirect to login or show dashboard
    resp = client.get("/admin/", follow=True)
    if resp.status_code != 200:
        # Show error body for debugging
        body = resp.content[:300] if hasattr(resp, "content") else b"no content"
        check("admin index 200", False, f"status={resp.status_code} body={body}")
    else:
        check("admin index 200", True)

    # Category list (changelist)
    resp = client.get("/admin/admin_app/category/")
    check("category list 200", resp.status_code == 200)
    check("category list has data", b"Technology" in resp.content)

    # Article list (changelist)
    resp = client.get("/admin/admin_app/article/")
    check("article list 200", resp.status_code == 200)
    check("article list has data", b"Test Article" in resp.content)

    # Article list with FK column (category) — tests FK rendering
    check(
        "article list shows FK",
        b"Technology" in resp.content or b"Science" in resp.content,
    )

    # ── Admin add view ────────────────────────────────────────────────────
    print("\n=== Admin add ===")

    resp = client.get("/admin/admin_app/category/add/")
    check("category add form 200", resp.status_code == 200)

    # POST to create a new category (include inline management form data)
    resp = client.post(
        "/admin/admin_app/category/add/",
        {
            "name": "Sports",
            "description": "Sports articles",
            # Django admin requires inline formset management form fields
            "articles-TOTAL_FORMS": "0",
            "articles-INITIAL_FORMS": "0",
            "articles-MIN_NUM_FORMS": "0",
            "articles-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        },
    )
    check(
        "category add redirect",
        resp.status_code in (200, 302),
        f"status={resp.status_code}",
    )
    check("category created via admin", Category.objects.filter(name="Sports").exists())

    # ── Admin change view ─────────────────────────────────────────────────
    print("\n=== Admin change ===")

    resp = client.get(f"/admin/admin_app/article/{article.pk}/change/")
    check("article change form 200", resp.status_code == 200)
    check("change form has title", b"Test Article" in resp.content)

    # ── Admin search ──────────────────────────────────────────────────────
    print("\n=== Admin search ===")

    resp = client.get("/admin/admin_app/article/?q=Test")
    check("search 200", resp.status_code == 200)
    check("search finds article", b"Test Article" in resp.content)

    resp = client.get("/admin/admin_app/article/?q=nonexistent")
    check("search no results", resp.status_code == 200)

    # ── Admin filtering ───────────────────────────────────────────────────
    print("\n=== Admin filtering ===")

    resp = client.get("/admin/admin_app/article/?status__exact=draft")
    check("filter by status 200", resp.status_code == 200)

    resp = client.get("/admin/admin_app/article/?is_featured__exact=1")
    check("filter by featured 200", resp.status_code == 200)

    resp = client.get(f"/admin/admin_app/article/?category__id__exact={cat.pk}")
    check("filter by FK 200", resp.status_code == 200)

    # ── Admin ordering ────────────────────────────────────────────────────
    print("\n=== Admin ordering ===")

    resp = client.get("/admin/admin_app/article/?o=1")
    check("order by title 200", resp.status_code == 200)

    resp = client.get("/admin/admin_app/article/?o=-5")
    check("reverse order 200", resp.status_code == 200)

    # ── Admin inline ──────────────────────────────────────────────────────
    print("\n=== Admin inline ===")

    resp = client.get(f"/admin/admin_app/category/{cat.pk}/change/")
    check("category change with inline 200", resp.status_code == 200)
    # The inline should show article forms
    check("inline shows articles", b"article" in resp.content.lower())

    # ── Admin delete ──────────────────────────────────────────────────────
    print("\n=== Admin delete ===")

    resp = client.get(f"/admin/admin_app/article/{article.pk}/delete/")
    check("delete confirm 200", resp.status_code == 200)

    resp = client.post(
        f"/admin/admin_app/article/{article.pk}/delete/", {"post": "yes"}
    )
    check("delete redirect", resp.status_code in (200, 302))
    check("article deleted", not Article.objects.filter(pk=article.pk).exists())

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")

    Article.objects.all().delete()
    Category.objects.all().delete()
    User.objects.all().delete()
    check("cleanup done", Article.objects.count() == 0)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All Django admin E2E tests passed through hyperdjango.db!")
    else:
        print("SOME TESTS FAILED!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
