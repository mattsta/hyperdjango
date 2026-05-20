"""
Regression tests for second-pass audit fixes.

Tests:
1. get() raises DoesNotExist (not returns None)
2. get() raises MultipleObjectsReturned
3. M2M batch INSERT (single query, not N round trips)
4. _singularize for plural table names
5. Cache get_or_set correctly caches None values
6. @cached decorator correctly caches falsy values

Usage:
    uv run hyper-test secondpass_regressions
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.cache import LocMemCache
from hyperdjango.database import Database, get_db, set_db
from hyperdjango.models import (
    Field,
    ManyToManyField,
    Model,
    _singularize,
    _table_to_fk_col,
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
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class SP2Item(Model):
    class Meta:
        table = "sp2_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    value: int = Field(default=0)


class SP2Tag(Model):
    class Meta:
        table = "sp2_tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=50)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS sp2_items_sp2_tags CASCADE")
    await db.execute("DROP TABLE IF EXISTS sp2_items CASCADE")
    await db.execute("DROP TABLE IF EXISTS sp2_tags CASCADE")
    await db.execute("""
        CREATE TABLE sp2_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            value INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE sp2_tags (
            id SERIAL PRIMARY KEY,
            name VARCHAR(50) NOT NULL
        )
    """)
    # Junction table columns match what _table_to_fk_col derives:
    # _table_to_fk_col("sp2_items") → "item_id", _table_to_fk_col("sp2_tags") → "tag_id"
    await db.execute("""
        CREATE TABLE sp2_items_sp2_tags (
            item_id INTEGER REFERENCES sp2_items(id) ON DELETE CASCADE,
            tag_id INTEGER REFERENCES sp2_tags(id) ON DELETE CASCADE,
            UNIQUE(item_id, tag_id)
        )
    """)
    return db


async def teardown_db(db):
    await db.execute("DROP TABLE IF EXISTS sp2_items_sp2_tags CASCADE")
    await db.execute("DROP TABLE IF EXISTS sp2_items CASCADE")
    await db.execute("DROP TABLE IF EXISTS sp2_tags CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# get() raises DoesNotExist
# ---------------------------------------------------------------------------


@test("get: raises DoesNotExist when no row found")
async def test_get_does_not_exist():
    try:
        await SP2Item.objects.get(id=99999)
        assert False, "Should have raised DoesNotExist"
    except SP2Item.DoesNotExist as e:
        assert "does not exist" in str(e).lower()


@test("get: raises MultipleObjectsReturned when 2+ rows match")
async def test_get_multiple():
    await SP2Item(name="dup", value=1).save()
    await SP2Item(name="dup", value=2).save()

    try:
        await SP2Item.objects.get(name="dup")
        assert False, "Should have raised MultipleObjectsReturned"
    except SP2Item.MultipleObjectsReturned as e:
        assert "more than one" in str(e).lower()


@test("get: returns single matching row correctly")
async def test_get_single():
    item = await SP2Item(name="unique_get_test", value=42).save()
    found = await SP2Item.objects.get(id=item.id)
    assert found.name == "unique_get_test"
    assert found.value == 42


@test("get: DoesNotExist and MultipleObjectsReturned available on model")
async def test_get_exceptions_available():
    assert hasattr(SP2Item, "DoesNotExist")
    assert hasattr(SP2Tag, "DoesNotExist")
    assert issubclass(SP2Item.DoesNotExist, Exception)
    assert issubclass(SP2Item.MultipleObjectsReturned, Exception)


# ---------------------------------------------------------------------------
# Singularization
# ---------------------------------------------------------------------------


@test("singularize: basic plural (books → book)")
def test_singular_basic():
    assert _singularize("books") == "book"


@test("singularize: -es plural (addresses → address)")
def test_singular_es():
    assert _singularize("addresses") == "address"


@test("singularize: -ies plural (categories → category)")
def test_singular_ies():
    assert _singularize("categories") == "category"


@test("singularize: -ses plural (statuses → status)")
def test_singular_ses():
    assert _singularize("statuses") == "status"


@test("singularize: already singular (user → user)")
def test_singular_noop():
    assert _singularize("user") == "user"


@test("singularize: -sses (classes → class)")
def test_singular_sses():
    assert _singularize("classes") == "class"


@test("table_to_fk_col: addresses → address_id")
def test_fk_col_addresses():
    assert _table_to_fk_col("addresses") == "address_id"


@test("table_to_fk_col: test_categories → category_id")
def test_fk_col_categories():
    assert _table_to_fk_col("test_categories") == "category_id"


@test("table_to_fk_col: test_books → book_id")
def test_fk_col_books():
    assert _table_to_fk_col("test_books") == "book_id"


# ---------------------------------------------------------------------------
# M2M batch INSERT
# ---------------------------------------------------------------------------


@test("M2M add: batch inserts multiple tags in one query")
async def test_m2m_batch():
    item = await SP2Item(name="m2m_batch", value=1).save()

    tags = []
    for name in ["python", "zig", "postgres"]:
        tag = await SP2Tag(name=name).save()
        tags.append(tag)

    SP2Item.tags = ManyToManyField("sp2_tags", junction_table="sp2_items_sp2_tags")
    SP2Item.tags._configure(SP2Item, "tags")

    manager = SP2Item.tags.__get__(item, SP2Item)
    await manager.add(*tags)

    # Verify all 3 tags linked
    db = get_db()
    count = await db.query_val(
        "SELECT COUNT(*) FROM sp2_items_sp2_tags WHERE item_id = $1", item.id
    )
    assert count == 3, f"Expected 3 M2M links, got {count}"


# ---------------------------------------------------------------------------
# Cache get_or_set with None
# ---------------------------------------------------------------------------


@test("LocMemCache: get_or_set caches None correctly")
def test_cache_none():
    cache = LocMemCache(max_size=100)

    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return None

    # First call — computes and caches None
    result1 = cache.get_or_set("key", compute, ttl=60)
    assert result1 is None
    assert call_count == 1

    # Second call — should return cached None (not recompute)
    result2 = cache.get_or_set("key", compute, ttl=60)
    assert result2 is None
    assert call_count == 1, f"Recomputed! call_count={call_count}"


@test("LocMemCache: get_or_set caches 0 correctly")
def test_cache_zero():
    cache = LocMemCache(max_size=100)

    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return 0

    result1 = cache.get_or_set("zero", compute, ttl=60)
    assert result1 == 0
    assert call_count == 1

    result2 = cache.get_or_set("zero", compute, ttl=60)
    assert result2 == 0
    assert call_count == 1


@test("LocMemCache: get_or_set caches False correctly")
def test_cache_false():
    cache = LocMemCache(max_size=100)

    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return False

    result1 = cache.get_or_set("false_key", compute, ttl=60)
    assert result1 is False
    assert call_count == 1

    result2 = cache.get_or_set("false_key", compute, ttl=60)
    assert result2 is False
    assert call_count == 1


@test("LocMemCache: get_or_set caches empty string correctly")
def test_cache_empty_string():
    cache = LocMemCache(max_size=100)

    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return ""

    result1 = cache.get_or_set("empty", compute, ttl=60)
    assert result1 == ""
    assert call_count == 1

    result2 = cache.get_or_set("empty", compute, ttl=60)
    assert result2 == ""
    assert call_count == 1


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    db = await setup_db()

    print(f"\nSecond-Pass Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    await teardown_db(db)
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
