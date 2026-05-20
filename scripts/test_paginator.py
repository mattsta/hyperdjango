#!/usr/bin/env python3
"""
Tests for Paginator utility class.

Unit tests (Page dataclass) + integration tests (live PostgreSQL with QuerySet).

Usage:
    uv run hyper-test paginator
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.models import Field, Model
from hyperdjango.paginator import (
    EmptyPage,
    InvalidPage,
    Page,
    PageNotAnInteger,
    Paginator,
    clear_count_cache,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Test model
# ---------------------------------------------------------------------------


class PagItem(Model):
    class Meta:
        table = "test_pag_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    value: int = Field(default=0)


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TESTS — Page dataclass (no database)
# ═══════════════════════════════════════════════════════════════════════════


@test("Page: has_next/has_previous for middle page")
def test_page_middle():
    page = Page(items=["a", "b", "c"], number=2, num_pages=5, count=25, per_page=5)
    assert page.has_next
    assert page.has_previous
    assert page.next_page_number == 3
    assert page.previous_page_number == 1


@test("Page: first page has no previous")
def test_page_first():
    page = Page(items=["a", "b"], number=1, num_pages=3, count=10, per_page=5)
    assert page.has_next
    assert not page.has_previous
    try:
        page.previous_page_number
        assert False, "Should raise InvalidPage"
    except InvalidPage:
        pass


@test("Page: last page has no next")
def test_page_last():
    page = Page(items=["a"], number=3, num_pages=3, count=11, per_page=5)
    assert not page.has_next
    assert page.has_previous
    try:
        page.next_page_number
        assert False, "Should raise InvalidPage"
    except InvalidPage:
        pass


@test("Page: single page")
def test_page_single():
    page = Page(items=["a", "b"], number=1, num_pages=1, count=2, per_page=10)
    assert not page.has_next
    assert not page.has_previous


@test("Page: start_index / end_index")
def test_page_indices():
    page = Page(items=list(range(10)), number=3, num_pages=5, count=50, per_page=10)
    assert page.start_index == 21
    assert page.end_index == 30


@test("Page: start_index / end_index for partial last page")
def test_page_indices_partial():
    page = Page(items=list(range(3)), number=4, num_pages=4, count=33, per_page=10)
    assert page.start_index == 31
    assert page.end_index == 33


@test("Page: empty page indices")
def test_page_indices_empty():
    page = Page(items=[], number=1, num_pages=1, count=0, per_page=10)
    assert page.start_index == 0
    assert page.end_index == 0


@test("Page: page_range")
def test_page_range():
    page = Page(items=[], number=1, num_pages=5, count=50, per_page=10)
    assert list(page.page_range) == [1, 2, 3, 4, 5]


@test("Page: len, iter, getitem, bool")
def test_page_protocols():
    page = Page(items=["a", "b", "c"], number=1, num_pages=1, count=3, per_page=10)
    assert len(page) == 3
    assert list(page) == ["a", "b", "c"]
    assert page[0] == "a"
    assert page[2] == "c"
    assert bool(page) is True

    empty = Page(items=[], number=1, num_pages=1, count=0, per_page=10)
    assert bool(empty) is False


# --- Paginator validation ---


@test("Paginator: validate_number rejects non-integer")
def test_validate_non_int():
    p = Paginator(None, per_page=10)
    try:
        p._validate_number("abc")
        assert False, "Should raise PageNotAnInteger"
    except PageNotAnInteger:
        pass


@test("Paginator: validate_number rejects zero")
def test_validate_zero():
    p = Paginator(None, per_page=10)
    try:
        p._validate_number(0)
        assert False, "Should raise EmptyPage"
    except EmptyPage:
        pass


@test("Paginator: validate_number rejects negative")
def test_validate_negative():
    p = Paginator(None, per_page=10)
    try:
        p._validate_number(-1)
        assert False, "Should raise EmptyPage"
    except EmptyPage:
        pass


@test("Paginator: validate_number accepts string int")
def test_validate_string_int():
    p = Paginator(None, per_page=10)
    assert p._validate_number("3") == 3


@test("Paginator: validate_number accepts float int")
def test_validate_float_int():
    p = Paginator(None, per_page=10)
    assert p._validate_number(3.0) == 3


@test("Paginator: validate_number rejects float non-int")
def test_validate_float_non_int():
    p = Paginator(None, per_page=10)
    try:
        p._validate_number(3.5)
        assert False, "Should raise PageNotAnInteger"
    except PageNotAnInteger:
        pass


@test("Paginator: _compute_num_pages basic")
def test_compute_pages():
    p = Paginator(None, per_page=10)
    assert p._compute_num_pages(0) == 1  # allow_empty_first_page
    assert p._compute_num_pages(1) == 1
    assert p._compute_num_pages(10) == 1
    assert p._compute_num_pages(11) == 2
    assert p._compute_num_pages(20) == 2
    assert p._compute_num_pages(21) == 3
    assert p._compute_num_pages(100) == 10


@test("Paginator: _compute_num_pages with orphans")
def test_compute_pages_orphans():
    p = Paginator(None, per_page=10, orphans=3)
    # With orphans=3: if last page would have <= 3 items, merge with previous
    assert p._compute_num_pages(10) == 1  # 10 items, 1 page of 10
    assert p._compute_num_pages(13) == 1  # 13-3=10 items, ceil(10/10) = 1
    assert p._compute_num_pages(14) == 2  # 14-3=11, ceil(11/10) = 2
    assert p._compute_num_pages(23) == 2  # 23-3=20, ceil(20/10) = 2
    assert p._compute_num_pages(24) == 3  # 24-3=21, ceil(21/10) = 3


@test("Paginator: per_page minimum is 1")
def test_per_page_minimum():
    p = Paginator(None, per_page=0)
    assert p.per_page == 1
    p2 = Paginator(None, per_page=-5)
    assert p2.per_page == 1


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — live PostgreSQL
# ═══════════════════════════════════════════════════════════════════════════


@test("DB: setup test table")
async def test_db_setup():
    db = get_db()
    await db.execute("DROP TABLE IF EXISTS test_pag_items CASCADE")
    await db.execute("""
        CREATE TABLE test_pag_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            value INTEGER DEFAULT 0
        )
    """)
    # Insert 53 items
    for i in range(1, 54):
        await db.execute(
            "INSERT INTO test_pag_items (name, value) VALUES ($1, $2)",
            f"item_{i:03d}",
            i,
        )


@test("DB: paginate 53 items with per_page=10")
async def test_db_paginate():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=10)
    count = await paginator.get_count()
    assert count == 53, f"Expected 53, got {count}"

    page1 = await paginator.page(1)
    assert len(page1) == 10
    assert page1.number == 1
    assert page1.has_next
    assert not page1.has_previous
    assert page1.num_pages == 6
    assert page1.start_index == 1
    assert page1.end_index == 10
    assert page1.items[0].name == "item_001"

    page6 = await paginator.page(6)
    assert len(page6) == 3  # 53 - 50 = 3 remaining
    assert page6.number == 6
    assert not page6.has_next
    assert page6.has_previous
    assert page6.start_index == 51
    assert page6.end_index == 53


@test("DB: paginate with orphans merges last page")
async def test_db_orphans():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=10, orphans=3)
    page_count = paginator._compute_num_pages(53)
    # 53 - 3 = 50, ceil(50/10) = 5
    assert page_count == 5

    page5 = await paginator.page(5)
    # Last page gets remaining: 53 - 40 = 13 items (merged orphans)
    assert len(page5) == 13
    assert not page5.has_next


@test("DB: page beyond range raises EmptyPage")
async def test_db_empty_page():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=10)
    try:
        await paginator.page(100)
        assert False, "Should raise EmptyPage"
    except EmptyPage:
        pass


@test("DB: empty queryset with allow_empty_first_page")
async def test_db_empty_queryset():
    paginator = Paginator(
        PagItem.objects.filter(value__gt=9999),
        per_page=10,
        allow_empty_first_page=True,
    )
    page = await paginator.page(1)
    assert len(page) == 0
    assert page.number == 1
    assert page.count == 0
    assert page.num_pages == 1


@test("DB: empty queryset without allow_empty raises on page 1")
async def test_db_empty_no_allow():
    paginator = Paginator(
        PagItem.objects.filter(value__gt=9999),
        per_page=10,
        allow_empty_first_page=False,
    )
    try:
        await paginator.page(1)
        assert False, "Should raise EmptyPage"
    except EmptyPage:
        pass


@test("DB: page with filter")
async def test_db_with_filter():
    # Filter items with value > 40 (items 41-53 = 13 items)
    paginator = Paginator(
        PagItem.objects.filter(value__gt=40).order_by("id"),
        per_page=5,
    )
    count = await paginator.get_count()
    assert count == 13

    page1 = await paginator.page(1)
    assert len(page1) == 5
    assert page1.items[0].name == "item_041"

    page3 = await paginator.page(3)
    assert len(page3) == 3  # 13 - 10 = 3
    assert page3.items[-1].name == "item_053"


@test("DB: count is cached")
async def test_db_count_cached():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=10)
    c1 = await paginator.get_count()
    c2 = await paginator.get_count()
    assert c1 == c2 == 53
    assert paginator._count == 53


@test("DB: empty page skips the LIMIT 0 data query")
async def test_db_empty_page_skips_query():
    db = get_db()
    paginator = Paginator(
        PagItem.objects.filter(value__gt=9999),
        per_page=10,
        allow_empty_first_page=True,
    )
    # Pre-warm the count (this runs one COUNT query).
    assert await paginator.get_count() == 0

    # Fetching the (empty) page must NOT issue any further query — no wasted
    # `... LIMIT 0`.
    seen: list[str] = []

    def record(execute, sql, params):
        seen.append(sql)
        return execute(sql, params)

    with db.execute_wrapper(record):
        page = await paginator.page(1)

    assert len(page) == 0
    assert seen == [], f"expected no query for empty page, got {seen}"


@test("DB: count_ttl reuses COUNT across paginator instances")
async def test_db_count_ttl_shared():
    db = get_db()
    clear_count_cache()

    qs = PagItem.objects.order_by("id")

    # First paginator computes and caches the count.
    p1 = Paginator(qs, per_page=10, count_ttl=30)
    assert await p1.get_count() == 53

    # A fresh paginator for the SAME query must reuse the cached count with no
    # COUNT query at all.
    p2 = Paginator(qs, per_page=10, count_ttl=30)
    seen: list[str] = []

    def record(execute, sql, params):
        seen.append(sql)
        return execute(sql, params)

    with db.execute_wrapper(record):
        c2 = await p2.get_count()

    assert c2 == 53
    assert seen == [], f"expected cached count (no query), got {seen}"

    clear_count_cache()


@test("DB: page_range property")
async def test_db_page_range():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=10)
    pr = await paginator.page_range
    assert list(pr) == [1, 2, 3, 4, 5, 6]


@test("DB: async iteration over all pages")
async def test_db_async_iter():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=20)
    all_items = []
    page_numbers = []
    async for page in paginator:
        page_numbers.append(page.number)
        all_items.extend(page.items)
    assert page_numbers == [1, 2, 3]
    assert len(all_items) == 53
    assert all_items[0].name == "item_001"
    assert all_items[-1].name == "item_053"


@test("DB: per_page=1 gives 53 pages")
async def test_db_per_page_1():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=1)
    count = await paginator.get_count()
    assert count == 53
    page1 = await paginator.page(1)
    assert len(page1) == 1
    assert page1.num_pages == 53

    page53 = await paginator.page(53)
    assert len(page53) == 1
    assert page53.items[0].name == "item_053"


@test("DB: per_page larger than total gives 1 page")
async def test_db_per_page_large():
    paginator = Paginator(PagItem.objects.order_by("id"), per_page=100)
    page = await paginator.page(1)
    assert len(page) == 53
    assert page.num_pages == 1
    assert not page.has_next


@test("DB: cleanup")
async def test_db_cleanup():
    db = get_db()
    await db.execute("DROP TABLE IF EXISTS test_pag_items CASCADE")


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            tests.append(obj)

    unit_tests = [t for t in tests if "DB:" not in t.__name__]
    db_tests = [t for t in tests if "DB:" in t.__name__]

    print("\n═══ Unit Tests: Paginator ═══")
    for t in unit_tests:
        await t()

    print("\n═══ Integration Tests: Live PostgreSQL ═══")
    try:
        db = Database(DB_URL)
        set_db(db)
        await db.connect()
        for t in db_tests:
            await t()
    except Exception as e:
        print(f"\n  ⚠ Database connection failed ({e}), skipping integration tests")

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
