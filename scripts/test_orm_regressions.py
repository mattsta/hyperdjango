#!/usr/bin/env python3
"""
Regression tests for DB/ORM fixes discovered during full-platform audit.

Tests:
1. save() insert vs update detection (was always inserting for auto PK)
2. refresh_from_db dict indexing (was crashing with KeyError)
3. LIKE/ILIKE metacharacter escaping (% and _ were not escaped)
4. exists() uses efficient SELECT 1 LIMIT 1 (was using COUNT(*))
5. _loaded_from_db tracking flag set correctly on query results

Usage:
    uv run hyper-test orm_regressions
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.lookups import _escape_like
from hyperdjango.models import Field, Model

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


class RegItem(Model):
    class Meta:
        table = "regression_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    description: str = Field(max_length=500, default="")
    value: int = Field(default=0)


class RegManualPK(Model):
    class Meta:
        table = "regression_manual_pk"

    id: int = Field(primary_key=True)
    name: str = Field(max_length=100)


# ---------------------------------------------------------------------------
# DB setup / teardown
# ---------------------------------------------------------------------------


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS regression_items CASCADE")
    await db.execute("DROP TABLE IF EXISTS regression_manual_pk CASCADE")
    await db.execute("""
        CREATE TABLE regression_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            description VARCHAR(500) DEFAULT '',
            value INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE regression_manual_pk (
            id INTEGER PRIMARY KEY,
            name VARCHAR(100) NOT NULL
        )
    """)
    return db


async def teardown_db(db):
    await db.execute("DROP TABLE IF EXISTS regression_items CASCADE")
    await db.execute("DROP TABLE IF EXISTS regression_manual_pk CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Regression: save() insert vs update (was always inserting for auto PK)
# ---------------------------------------------------------------------------


@test("save: new instance inserts (auto PK)")
async def test_save_insert():
    item = RegItem(name="new item", value=10)
    await item.save()
    assert item.id is not None
    assert item.id > 0

    # Verify it's in DB
    db = get_db()
    row = await db.query_one("SELECT * FROM regression_items WHERE id = $1", item.id)
    assert row is not None
    assert row["name"] == "new item"
    assert row["value"] == 10


@test("save: loaded instance updates (NOT duplicate insert)")
async def test_save_update_not_insert():
    # Create an item
    item = RegItem(name="original", value=1)
    await item.save()
    original_id = item.id

    # Load it from DB
    loaded = await RegItem.objects.get(id=original_id)
    assert loaded.id == original_id
    assert loaded.name == "original"

    # Modify and save — this MUST UPDATE, not INSERT
    loaded.name = "modified"
    loaded.value = 99
    await loaded.save()

    # Verify same ID (no duplicate)
    assert loaded.id == original_id

    # Verify only ONE row with this ID exists
    db = get_db()
    count = await db.query_val(
        "SELECT COUNT(*) FROM regression_items WHERE id = $1", original_id
    )
    assert count == 1, f"Expected 1 row, got {count} — save() inserted a duplicate!"

    # Verify the row was actually updated
    row = await db.query_one(
        "SELECT * FROM regression_items WHERE id = $1", original_id
    )
    assert row["name"] == "modified"
    assert row["value"] == 99


@test("save: second save on same instance also updates")
async def test_save_double_update():
    item = RegItem(name="v1", value=1)
    await item.save()
    pk = item.id

    # First update
    item.name = "v2"
    await item.save()

    # Second update
    item.name = "v3"
    item.value = 300
    await item.save()

    # Still same row
    db = get_db()
    count = await db.query_val(
        "SELECT COUNT(*) FROM regression_items WHERE id = $1", pk
    )
    assert count == 1
    row = await db.query_one("SELECT * FROM regression_items WHERE id = $1", pk)
    assert row["name"] == "v3"
    assert row["value"] == 300


@test("save: manual PK model inserts then updates correctly")
async def test_save_manual_pk():
    # Manual PK — first save should INSERT
    item = RegManualPK(id=42, name="manual")
    await item.save()

    db = get_db()
    row = await db.query_one("SELECT * FROM regression_manual_pk WHERE id = $1", 42)
    assert row is not None
    assert row["name"] == "manual"


@test("save: _loaded_from_db flag set on query results")
async def test_loaded_from_db_flag():
    item = RegItem(name="flagtest", value=5)
    await item.save()

    # New instance should NOT have _loaded_from_db (before first save)
    fresh = RegItem(name="unsaved")
    assert not getattr(fresh, "_loaded_from_db", False)

    # After save, it should be set
    assert getattr(item, "_loaded_from_db", False) is True

    # Loaded from QuerySet should have it
    loaded = await RegItem.objects.get(id=item.id)
    assert getattr(loaded, "_loaded_from_db", False) is True


@test("save: QuerySet.all() results have _loaded_from_db")
async def test_loaded_flag_on_all():
    await RegItem(name="all_test_1", value=1).save()
    await RegItem(name="all_test_2", value=2).save()

    results = await RegItem.objects.filter(name__startswith="all_test").all()
    assert len(results) >= 2
    for r in results:
        assert getattr(r, "_loaded_from_db", False) is True, (
            f"Missing _loaded_from_db on {r.name}"
        )


# ---------------------------------------------------------------------------
# Regression: refresh_from_db (was crashing with KeyError on dict[int])
# ---------------------------------------------------------------------------


@test("refresh_from_db: reloads updated values without crashing")
async def test_refresh_from_db():
    item = RegItem(name="before_refresh", value=10)
    await item.save()
    pk = item.id

    # Update directly in DB (bypass ORM)
    db = get_db()
    await db.execute(
        "UPDATE regression_items SET name = $1, value = $2 WHERE id = $3",
        "after_refresh",
        999,
        pk,
    )

    # Refresh — this was crashing with KeyError
    await item.refresh_from_db()

    assert item.name == "after_refresh"
    assert item.value == 999
    assert item.id == pk


@test("refresh_from_db: works on freshly saved instance")
async def test_refresh_fresh():
    item = RegItem(name="fresh", value=42)
    await item.save()
    await item.refresh_from_db()
    assert item.name == "fresh"
    assert item.value == 42


@test("refresh_from_db: raises on unsaved instance")
async def test_refresh_unsaved():
    item = RegItem(name="unsaved")
    try:
        await item.refresh_from_db()
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "unsaved" in str(e).lower() or "Cannot refresh" in str(e)


# ---------------------------------------------------------------------------
# Regression: LIKE/ILIKE metacharacter escaping
# ---------------------------------------------------------------------------


@test("_escape_like: escapes percent")
def test_escape_percent():
    assert _escape_like("100%") == "100\\%"


@test("_escape_like: escapes underscore")
def test_escape_underscore():
    assert _escape_like("file_name") == "file\\_name"


@test("_escape_like: escapes backslash")
def test_escape_backslash():
    assert _escape_like("path\\to") == "path\\\\to"


@test("_escape_like: no change for safe strings")
def test_escape_safe():
    assert _escape_like("hello world") == "hello world"


@test("_escape_like: escapes all metacharacters together")
def test_escape_all():
    assert _escape_like("100%_test\\") == "100\\%\\_test\\\\"


@test("contains: literal % in filter value matches literally")
async def test_contains_percent_literal():
    # Create items — one with literal %, one without
    await RegItem(name="100% complete", value=1).save()
    await RegItem(name="100 complete", value=2).save()
    await RegItem(name="completely done", value=3).save()

    # Filter for "100%" — should match ONLY the item with literal %
    results = await RegItem.objects.filter(name__contains="100%").all()
    names = [r.name for r in results]
    assert "100% complete" in names
    # "100 complete" should NOT match because % is escaped
    assert "100 complete" not in names


@test("contains: literal _ in filter value matches literally")
async def test_contains_underscore_literal():
    await RegItem(name="file_v2.txt", value=1).save()
    await RegItem(name="filexv2.txt", value=2).save()

    # Filter for "file_" — should match only the literal underscore
    results = await RegItem.objects.filter(name__contains="file_").all()
    names = [r.name for r in results]
    assert "file_v2.txt" in names
    # "filexv2.txt" should NOT match because _ is escaped (doesn't match any single char)
    assert "filexv2.txt" not in names


@test("startswith: literal % in value")
async def test_startswith_percent():
    await RegItem(name="%special", value=1).save()
    await RegItem(name="special", value=2).save()

    results = await RegItem.objects.filter(name__startswith="%").all()
    names = [r.name for r in results]
    assert "%special" in names
    assert "special" not in names


@test("icontains: case-insensitive with escaping")
async def test_icontains_escaped():
    await RegItem(name="Score: 100%!", value=1).save()
    await RegItem(name="Score: 100 points", value=2).save()

    results = await RegItem.objects.filter(name__icontains="100%").all()
    names = [r.name for r in results]
    assert "Score: 100%!" in names
    assert "Score: 100 points" not in names


# ---------------------------------------------------------------------------
# Regression: exists() efficiency
# ---------------------------------------------------------------------------


@test("exists: returns True when rows match")
async def test_exists_true():
    await RegItem(name="exists_test", value=1).save()
    result = await RegItem.objects.filter(name="exists_test").exists()
    assert result is True


@test("exists: returns False when no rows match")
async def test_exists_false():
    result = await RegItem.objects.filter(name="nonexistent_xyz_abc").exists()
    assert result is False


@test("exists: works with filters")
async def test_exists_filtered():
    await RegItem(name="exists_filtered", value=42).save()
    assert (
        await RegItem.objects.filter(name="exists_filtered", value=42).exists() is True
    )
    assert (
        await RegItem.objects.filter(name="exists_filtered", value=999).exists()
        is False
    )


@test("exists: works on empty table")
async def test_exists_empty():
    # Clear all items and check
    db = get_db()
    await db.execute("DELETE FROM regression_manual_pk")
    result = await RegManualPK.objects.exists()
    assert result is False


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

    print(f"\nORM Regression Tests ({len(tests)} tests)")
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
