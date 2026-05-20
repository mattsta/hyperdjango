#!/usr/bin/env python3
"""Test admin query acceleration — comprehensive auto-prefetch across all admin surfaces.

Tests HyperModelAdmin auto-prefetch detection across:
1. list_display — FK and M2M columns
2. search_fields — nested FK traversals (author__name, author__profile__bio)
3. list_filter — FK filter fields
4. raw_id_fields — FK fields
5. autocomplete_fields — FK fields
6. inlines — reverse FK prefetch
7. Nested FK chains — multi-level select_related paths
8. Mixed scenarios — all surfaces combined
9. get_list_select_related override — computed vs explicit
10. Query count verification — prove N+1 elimination with real DB

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys
import time

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

from django.contrib.admin import ModelAdmin, TabularInline
from django.db import connection, models
from django.test import RequestFactory

from hyperdjango.serving.admin import (
    HyperAdminSite,
    HyperModelAdmin,
    _collect_admin_relations,
    _resolve_field_relations,
)

# ── Test Models (unique names to avoid clashes) ────────────────────────────


class AccelPublisher(models.Model):
    name = models.CharField(max_length=100)
    country = models.CharField(max_length=50, default="US")

    class Meta:
        app_label = "admin_app"
        db_table = "accel_publisher"

    def __str__(self):
        return self.name


class AccelAuthor(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField(default="")
    publisher = models.ForeignKey(
        AccelPublisher,
        on_delete=models.CASCADE,
        null=True,
        related_name="accel_authors",
    )

    class Meta:
        app_label = "admin_app"
        db_table = "accel_author"

    def __str__(self):
        return self.name


class AccelTag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "admin_app"
        db_table = "accel_tag"

    def __str__(self):
        return self.name


class AccelBook(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(
        AccelAuthor, on_delete=models.CASCADE, related_name="accel_books"
    )
    publisher = models.ForeignKey(
        AccelPublisher, on_delete=models.CASCADE, null=True, related_name="accel_books"
    )
    tags = models.ManyToManyField(AccelTag, blank=True, related_name="accel_books")
    price = models.IntegerField(default=0)
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "admin_app"
        db_table = "accel_book"

    def __str__(self):
        return self.title


class AccelReview(models.Model):
    book = models.ForeignKey(
        AccelBook, on_delete=models.CASCADE, related_name="accel_reviews"
    )
    reviewer = models.CharField(max_length=100)
    rating = models.IntegerField(default=5)
    text = models.TextField(default="")

    class Meta:
        app_label = "admin_app"
        db_table = "accel_review"

    def __str__(self):
        return f"{self.reviewer}: {self.rating}"


# ── Create tables ──────────────────────────────────────────────────────────


def create_tables():
    """Create test tables directly via SQL."""
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accel_publisher (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                country VARCHAR(50) NOT NULL DEFAULT 'US'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accel_author (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(254) NOT NULL DEFAULT '',
                publisher_id INTEGER REFERENCES accel_publisher(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accel_tag (
                id SERIAL PRIMARY KEY,
                name VARCHAR(50) UNIQUE NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accel_book (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                author_id INTEGER NOT NULL REFERENCES accel_author(id) ON DELETE CASCADE,
                publisher_id INTEGER REFERENCES accel_publisher(id) ON DELETE CASCADE,
                price INTEGER NOT NULL DEFAULT 0,
                is_published BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accel_book_tags (
                id SERIAL PRIMARY KEY,
                accelbook_id INTEGER NOT NULL REFERENCES accel_book(id) ON DELETE CASCADE,
                acceltag_id INTEGER NOT NULL REFERENCES accel_tag(id) ON DELETE CASCADE,
                UNIQUE(accelbook_id, acceltag_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accel_review (
                id SERIAL PRIMARY KEY,
                book_id INTEGER NOT NULL REFERENCES accel_book(id) ON DELETE CASCADE,
                reviewer VARCHAR(100) NOT NULL,
                rating INTEGER NOT NULL DEFAULT 5,
                text TEXT NOT NULL DEFAULT ''
            )
        """)


def seed_data():
    """Insert test data for query count verification."""
    with connection.cursor() as cursor:
        # Clean
        cursor.execute("DELETE FROM accel_review")
        cursor.execute("DELETE FROM accel_book_tags")
        cursor.execute("DELETE FROM accel_book")
        cursor.execute("DELETE FROM accel_author")
        cursor.execute("DELETE FROM accel_tag")
        cursor.execute("DELETE FROM accel_publisher")

        # Publishers
        cursor.execute(
            "INSERT INTO accel_publisher (id, name, country) VALUES (1, 'OReilly', 'US'), (2, 'Manning', 'US'), (3, 'Packt', 'UK') RETURNING id"
        )

        # Authors (with FK to publisher)
        cursor.execute(
            "INSERT INTO accel_author (id, name, email, publisher_id) VALUES (1, 'Alice', 'alice@test.com', 1), (2, 'Bob', 'bob@test.com', 2), (3, 'Charlie', 'charlie@test.com', 3) RETURNING id"
        )

        # Tags
        cursor.execute(
            "INSERT INTO accel_tag (id, name) VALUES (1, 'python'), (2, 'django'), (3, 'zig') RETURNING id"
        )

        # Books (FK to author + publisher)
        for i in range(1, 21):
            author_id = (i % 3) + 1
            pub_id = (i % 3) + 1
            cursor.execute(
                "INSERT INTO accel_book (id, title, author_id, publisher_id, price, is_published) VALUES (%s, %s, %s, %s, %s, %s)",
                [i, f"Book {i}", author_id, pub_id, i * 10, i % 2 == 0],
            )

        # Tags for books
        for i in range(1, 21):
            tag_id = (i % 3) + 1
            cursor.execute(
                "INSERT INTO accel_book_tags (accelbook_id, acceltag_id) VALUES (%s, %s)",
                [i, tag_id],
            )

        # Reviews (FK to book)
        for i in range(1, 21):
            for j in range(1, 4):
                cursor.execute(
                    "INSERT INTO accel_review (book_id, reviewer, rating, text) VALUES (%s, %s, %s, %s)",
                    [i, f"Reviewer{j}", j + 2, f"Review {j} for book {i}"],
                )


# ── Test runner ────────────────────────────────────────────────────────────


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

    # Set up
    print("Setting up test database...")
    create_tables()
    seed_data()

    factory = RequestFactory()
    request = factory.get("/admin/")
    # Django admin needs request.user
    from django.contrib.auth.models import AnonymousUser

    request.user = AnonymousUser()

    site = HyperAdminSite(name="accel_test")

    # ── 1. _resolve_field_relations ────────────────────────────────────────
    print("\n=== _resolve_field_relations ===")

    # Direct FK
    result = _resolve_field_relations(AccelBook, "author")
    check("direct FK → select", result == ("select", "author"), f"got {result}")

    # Nested FK chain: author__publisher
    result = _resolve_field_relations(AccelBook, "author__publisher")
    check(
        "nested FK → select chain",
        result == ("select", "author__publisher"),
        f"got {result}",
    )

    # FK then scalar: author__name → select only the FK part
    result = _resolve_field_relations(AccelBook, "author__name")
    check("FK+scalar → select FK only", result == ("select", "author"), f"got {result}")

    # Deep nested: author__publisher__country → select the FK chain
    result = _resolve_field_relations(AccelBook, "author__publisher__country")
    check(
        "deep FK+scalar → select chain",
        result == ("select", "author__publisher"),
        f"got {result}",
    )

    # M2M field
    result = _resolve_field_relations(AccelBook, "tags")
    check(
        "M2M → prefetch",
        result is not None and result[0] == "prefetch",
        f"got {result}",
    )

    # Scalar field (no relation)
    result = _resolve_field_relations(AccelBook, "title")
    check("scalar → None", result is None, f"got {result}")

    # Non-existent field
    result = _resolve_field_relations(AccelBook, "nonexistent")
    check("nonexistent → None", result is None, f"got {result}")

    # Direct FK on author → publisher
    result = _resolve_field_relations(AccelAuthor, "publisher")
    check(
        "author.publisher FK → select",
        result == ("select", "publisher"),
        f"got {result}",
    )

    # ── 2. list_display auto-prefetch ──────────────────────────────────────
    print("\n=== list_display auto-prefetch ===")

    class BookAdmin1(HyperModelAdmin):
        list_display = ["title", "author", "publisher", "price"]

    admin1 = BookAdmin1(AccelBook, site)
    sel, pref, greedy = _collect_admin_relations(AccelBook, admin1, request)
    check("list_display FK author", "author" in sel, f"select={sel}")
    check("list_display FK publisher", "publisher" in sel, f"select={sel}")
    check("list_display no prefetch", len(pref) == 0, f"prefetch={pref}")

    # ── 3. search_fields nested FK ─────────────────────────────────────────
    print("\n=== search_fields nested FK ===")

    class BookAdmin2(HyperModelAdmin):
        list_display = ["title", "price"]
        search_fields = ["title", "author__name", "author__publisher__name"]

    admin2 = BookAdmin2(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin2, request)
    check("search author → select", "author" in sel, f"select={sel}")
    check(
        "search author__publisher → select chain",
        "author__publisher" in sel,
        f"select={sel}",
    )

    # ── 4. list_filter FK fields ───────────────────────────────────────────
    print("\n=== list_filter FK fields ===")

    class BookAdmin3(HyperModelAdmin):
        list_display = ["title"]
        list_filter = ["is_published", "author", "publisher"]

    admin3 = BookAdmin3(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin3, request)
    check("filter author → select", "author" in sel, f"select={sel}")
    check("filter publisher → select", "publisher" in sel, f"select={sel}")

    # ── 5. raw_id_fields ──────────────────────────────────────────────────
    print("\n=== raw_id_fields ===")

    class BookAdmin4(HyperModelAdmin):
        list_display = ["title"]
        raw_id_fields = ["author"]

    admin4 = BookAdmin4(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin4, request)
    check("raw_id author → select", "author" in sel, f"select={sel}")

    # ── 6. autocomplete_fields ─────────────────────────────────────────────
    print("\n=== autocomplete_fields ===")

    class BookAdmin5(HyperModelAdmin):
        list_display = ["title"]
        autocomplete_fields = ["publisher"]

    admin5 = BookAdmin5(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin5, request)
    check("autocomplete publisher → select", "publisher" in sel, f"select={sel}")

    # ── 7. inlines reverse prefetch ────────────────────────────────────────
    print("\n=== inlines reverse prefetch ===")

    class ReviewInline(TabularInline):
        model = AccelReview
        extra = 0

    class BookAdmin6(HyperModelAdmin):
        list_display = ["title", "author"]
        inlines = [ReviewInline]

    admin6 = BookAdmin6(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin6, request)
    check("inline reverse → prefetch", "accel_reviews" in pref, f"prefetch={pref}")
    check("inline + list_display FK", "author" in sel, f"select={sel}")

    # ── 8. M2M in list_display ─────────────────────────────────────────────
    print("\n=== M2M in list_display ===")

    class BookAdmin7(HyperModelAdmin):
        list_display = ["title", "author", "tags"]

    admin7 = BookAdmin7(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin7, request)
    check("M2M tags → prefetch", "tags" in pref, f"prefetch={pref}")
    check("FK author → select", "author" in sel, f"select={sel}")

    # ── 9. Combined all surfaces ───────────────────────────────────────────
    print("\n=== Combined all surfaces ===")

    class BookAdminFull(HyperModelAdmin):
        list_display = ["title", "author", "publisher", "is_published"]
        search_fields = ["title", "author__name", "author__publisher__name"]
        list_filter = ["is_published", "author"]
        raw_id_fields = ["publisher"]
        inlines = [ReviewInline]

    admin_full = BookAdminFull(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin_full, request)
    check("combined: author", "author" in sel, f"select={sel}")
    check("combined: publisher", "publisher" in sel, f"select={sel}")
    check(
        "combined: author__publisher from search",
        "author__publisher" in sel,
        f"select={sel}",
    )
    check("combined: reviews from inline", "accel_reviews" in pref, f"prefetch={pref}")

    # ── 10. get_list_select_related override ───────────────────────────────
    print("\n=== get_list_select_related ===")

    # Explicit list_select_related = True
    class BookAdminGreedy(HyperModelAdmin):
        list_display = ["title", "author"]
        list_select_related = True

    admin_greedy = BookAdminGreedy(AccelBook, site)
    lsr = admin_greedy.get_list_select_related(request)
    check("greedy list_select_related", lsr is True, f"got {lsr}")

    # Explicit list_select_related = ['author']
    class BookAdminExplicit(HyperModelAdmin):
        list_display = ["title", "author", "publisher"]
        list_select_related = ["author"]

    admin_explicit = BookAdminExplicit(AccelBook, site)
    lsr = admin_explicit.get_list_select_related(request)
    check("explicit list_select_related", lsr == ["author"], f"got {lsr}")

    # Computed (default — False)
    class BookAdminComputed(HyperModelAdmin):
        list_display = ["title", "author", "publisher"]
        search_fields = ["author__publisher__name"]

    admin_computed = BookAdminComputed(AccelBook, site)
    lsr = admin_computed.get_list_select_related(request)
    check("computed list_select_related has author", "author" in lsr, f"got {lsr}")
    check(
        "computed list_select_related has publisher", "publisher" in lsr, f"got {lsr}"
    )
    check(
        "computed list_select_related has chain",
        "author__publisher" in lsr,
        f"got {lsr}",
    )

    # ── 11. get_queryset applies relations ─────────────────────────────────
    print("\n=== get_queryset integration ===")

    class BookAdminQS(HyperModelAdmin):
        list_display = ["title", "author", "publisher"]
        search_fields = ["author__name"]

    admin_qs = BookAdminQS(AccelBook, site)
    qs = admin_qs.get_queryset(request)
    qs_str = str(qs.query)
    check("queryset has JOIN", "JOIN" in qs_str.upper(), f"query={qs_str[:200]}")

    # ── 12. Query count verification — prove N+1 elimination ───────────────
    print("\n=== Query count verification (live DB) ===")

    from django.db import reset_queries

    # Without HyperModelAdmin (plain ModelAdmin) — causes N+1
    class PlainBookAdmin(ModelAdmin):
        list_display = ["title", "author", "publisher"]

    plain_admin = PlainBookAdmin(AccelBook, site)

    reset_queries()
    plain_qs = plain_admin.get_queryset(request)
    plain_books = list(plain_qs[:20])
    queries_before_access = len(connection.queries)
    # Access FK fields — this triggers N+1 on plain admin
    for book in plain_books:
        _ = str(book.author)
        _ = str(book.publisher)
    queries_after_plain = len(connection.queries)
    plain_fk_queries = queries_after_plain - queries_before_access
    check(
        "plain admin causes N+1",
        plain_fk_queries > 0,
        f"expected >0 FK queries, got {plain_fk_queries}",
    )
    print(
        f"    → Plain admin: {plain_fk_queries} extra FK queries for {len(plain_books)} books"
    )

    # With HyperModelAdmin — zero N+1
    class HyperBookAdmin(HyperModelAdmin):
        list_display = ["title", "author", "publisher"]

    hyper_admin = HyperBookAdmin(AccelBook, site)

    reset_queries()
    hyper_qs = hyper_admin.get_queryset(request)
    hyper_books = list(hyper_qs[:20])
    queries_before_access = len(connection.queries)
    # Access FK fields — should be free (already joined)
    for book in hyper_books:
        _ = str(book.author)
        _ = str(book.publisher)
    queries_after_hyper = len(connection.queries)
    hyper_fk_queries = queries_after_hyper - queries_before_access
    check(
        "hyper admin zero N+1",
        hyper_fk_queries == 0,
        f"expected 0 FK queries, got {hyper_fk_queries}",
    )
    print(
        f"    → Hyper admin: {hyper_fk_queries} extra FK queries for {len(hyper_books)} books"
    )

    # ── 13. Nested FK query count — author.publisher access ────────────────
    print("\n=== Nested FK query count ===")

    class HyperBookAdminNested(HyperModelAdmin):
        list_display = ["title", "author"]
        search_fields = ["author__publisher__name"]

    nested_admin = HyperBookAdminNested(AccelBook, site)

    reset_queries()
    nested_qs = nested_admin.get_queryset(request)
    nested_books = list(nested_qs[:20])
    queries_before = len(connection.queries)
    for book in nested_books:
        _ = str(book.author)
        _ = str(book.author.publisher)
    queries_after = len(connection.queries)
    nested_fk_queries = queries_after - queries_before
    check(
        "nested FK zero N+1",
        nested_fk_queries == 0,
        f"expected 0 FK queries, got {nested_fk_queries}",
    )
    print(
        f"    → Nested admin: {nested_fk_queries} extra FK queries for {len(nested_books)} books"
    )

    # ── 14. Search prefix stripping ────────────────────────────────────────
    print("\n=== Search prefix stripping ===")

    class BookAdminPrefixed(HyperModelAdmin):
        list_display = ["title"]
        search_fields = ["=title", "^author__name", "@author__publisher__name"]

    admin_pfx = BookAdminPrefixed(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin_pfx, request)
    check("prefixed search: author", "author" in sel, f"select={sel}")
    check(
        "prefixed search: author__publisher",
        "author__publisher" in sel,
        f"select={sel}",
    )

    # ── 15. Empty config — no crash ────────────────────────────────────────
    print("\n=== Empty config ===")

    class BookAdminEmpty(HyperModelAdmin):
        list_display = ["title", "price"]

    admin_empty = BookAdminEmpty(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin_empty, request)
    check("empty: no select", len(sel) == 0, f"select={sel}")
    check("empty: no prefetch", len(pref) == 0, f"prefetch={pref}")
    qs = admin_empty.get_queryset(request)
    check("empty: queryset works", qs.count() == 20, f"count={qs.count()}")

    # ── 16. Benchmark: prefetched vs plain ─────────────────────────────────
    print("\n=== Benchmark: HyperModelAdmin vs plain ModelAdmin ===")

    iterations = 50

    # Plain — N+1
    reset_queries()
    t0 = time.perf_counter()
    for _ in range(iterations):
        plain_qs = plain_admin.get_queryset(request)
        for book in plain_qs[:20]:
            _ = str(book.author)
            _ = str(book.publisher)
    plain_time = time.perf_counter() - t0
    plain_query_count = len(connection.queries)

    # Hyper — zero N+1
    reset_queries()
    t0 = time.perf_counter()
    for _ in range(iterations):
        hyper_qs = hyper_admin.get_queryset(request)
        for book in hyper_qs[:20]:
            _ = str(book.author)
            _ = str(book.publisher)
    hyper_time = time.perf_counter() - t0
    hyper_query_count = len(connection.queries)

    speedup = plain_time / hyper_time if hyper_time > 0 else 0
    query_reduction = (
        plain_query_count / hyper_query_count if hyper_query_count > 0 else 0
    )

    print(f"    Plain:  {plain_time:.3f}s, {plain_query_count} queries")
    print(f"    Hyper:  {hyper_time:.3f}s, {hyper_query_count} queries")
    print(f"    Speedup: {speedup:.1f}x faster")
    print(f"    Query reduction: {query_reduction:.1f}x fewer queries")

    check(
        "hyper faster than plain",
        hyper_time < plain_time,
        f"hyper={hyper_time:.3f}s, plain={plain_time:.3f}s",
    )
    check(
        "hyper fewer queries",
        hyper_query_count < plain_query_count,
        f"hyper={hyper_query_count}, plain={plain_query_count}",
    )

    # ── 17. list_filter with tuple (field, filter_class) ───────────────────
    print("\n=== list_filter tuple format ===")

    from django.contrib.admin import SimpleListFilter

    class StatusFilter(SimpleListFilter):
        title = "status"
        parameter_name = "status"

        def lookups(self, request, model_admin):
            return [("published", "Published")]

        def queryset(self, request, queryset):
            return queryset

    class BookAdminTupleFilter(HyperModelAdmin):
        list_display = ["title"]
        list_filter = [("author", StatusFilter), "publisher"]

    admin_tf = BookAdminTupleFilter(AccelBook, site)
    sel, pref, _ = _collect_admin_relations(AccelBook, admin_tf, request)
    check("tuple filter: author", "author" in sel, f"select={sel}")
    check("tuple filter: publisher", "publisher" in sel, f"select={sel}")

    # ── Cleanup ────────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS accel_review CASCADE")
        cursor.execute("DROP TABLE IF EXISTS accel_book_tags CASCADE")
        cursor.execute("DROP TABLE IF EXISTS accel_book CASCADE")
        cursor.execute("DROP TABLE IF EXISTS accel_author CASCADE")
        cursor.execute("DROP TABLE IF EXISTS accel_tag CASCADE")
        cursor.execute("DROP TABLE IF EXISTS accel_publisher CASCADE")
    print("  Tables dropped.")

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All admin acceleration tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
