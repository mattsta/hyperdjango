#!/usr/bin/env python3
"""Tests for native dict→JSONB parameter support in pg.zig.

Verifies that Python dicts pass through the Zig parameter binding layer
and are automatically serialized to JSON for JSONB columns. This is a
platform-level feature — any model with a dict field gets native JSONB
roundtrips without manual serialization.

Usage:
    uv run hyper-test dict_jsonb
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import time

from hyperdjango.database import Database, set_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model, create_table_for_model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS  {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL  {name}" + (f" — {details}" if details else ""))


# ── Test Model ────────────────────────────────────────────────────────────


class JsonTestModel(TimestampMixin, Model):
    class Meta:
        table = "test_dict_jsonb"
        unlogged = True

    id: int = Field(primary_key=True, auto=True)
    data: dict = Field(default={})
    label: str = Field(default="")


class JsonListModel(TimestampMixin, Model):
    """A model with a JSONB column that holds a JSON *array* (Python list).

    Regression cover for the native list→JSONB bind: pg.zig's extractParams turned a
    Python list into a PG array literal `{a,b}` (invalid JSON) instead of `[a,b]`, so a
    list bound to a JSONB column was rejected. The bind path now coerces it.
    """

    class Meta:
        table = "test_list_jsonb"
        unlogged = True

    id: int = Field(primary_key=True, auto=True)
    items: list = Field(default=[], db_type="JSONB")  # JSONB array column
    label: str = Field(default="")


# ── Tests ─────────────────────────────────────────────────────────────────


async def test_basic_roundtrip(db):
    print("\n=== Basic Dict→JSONB Roundtrip ===")

    original = {"user_id": 42, "username": "alice", "active": True}
    obj = JsonTestModel(data=original, label="basic")
    await obj.save()

    found = await JsonTestModel.objects.filter(id=obj.id).first()
    check("dict saved and loaded", found is not None)
    check("data is dict", isinstance(found.data, dict))
    check("user_id preserved", found.data.get("user_id") == 42)
    check("username preserved", found.data.get("username") == "alice")
    check("active preserved", found.data.get("active") is True)
    check("full roundtrip match", found.data == original)


async def test_nested_dicts(db):
    print("\n=== Nested Dicts ===")

    original = {
        "config": {"debug": True, "level": 3, "nested": {"deep": "value"}},
        "tags": ["a", "b", "c"],
        "metadata": {"counts": [1, 2, 3]},
    }
    obj = JsonTestModel(data=original, label="nested")
    await obj.save()

    found = await JsonTestModel.objects.filter(id=obj.id).first()
    check("nested dict roundtrip", found.data == original)
    check("deep nested access", found.data["config"]["nested"]["deep"] == "value")
    check("list in dict preserved", found.data["tags"] == ["a", "b", "c"])
    check("list in nested dict", found.data["metadata"]["counts"] == [1, 2, 3])


async def test_all_json_types(db):
    print("\n=== All JSON Types ===")

    original = {
        "string": "hello",
        "integer": 42,
        "float": 3.14,
        "bool_true": True,
        "bool_false": False,
        "null_value": None,
        "empty_list": [],
        "empty_dict": {},
        "list_of_ints": [1, 2, 3],
        "list_of_strings": ["a", "b"],
    }
    obj = JsonTestModel(data=original, label="all_types")
    await obj.save()

    found = await JsonTestModel.objects.filter(id=obj.id).first()
    check("string type", found.data["string"] == "hello")
    check("integer type", found.data["integer"] == 42)
    check("float type", abs(found.data["float"] - 3.14) < 0.001)
    check("bool true", found.data["bool_true"] is True)
    check("bool false", found.data["bool_false"] is False)
    check("null value", found.data["null_value"] is None)
    check("empty list", found.data["empty_list"] == [])
    check("empty dict", found.data["empty_dict"] == {})
    check("list of ints", found.data["list_of_ints"] == [1, 2, 3])


async def test_empty_dict(db):
    print("\n=== Empty Dict ===")

    obj = JsonTestModel(data={}, label="empty")
    await obj.save()

    found = await JsonTestModel.objects.filter(id=obj.id).first()
    check("empty dict roundtrip", found.data == {})
    check("empty dict is dict type", isinstance(found.data, dict))


async def test_unicode_and_special_chars(db):
    print("\n=== Unicode & Special Characters ===")

    original = {
        "emoji": "Hello 🌍🔑",
        "japanese": "日本語テスト",
        "arabic": "مرحبا",
        "newlines": "line1\nline2\ttab",
        "quotes": 'He said "hello"',
        "backslash": "path\\to\\file",
    }
    obj = JsonTestModel(data=original, label="unicode")
    await obj.save()

    found = await JsonTestModel.objects.filter(id=obj.id).first()
    check("emoji preserved", found.data["emoji"] == original["emoji"])
    check("japanese preserved", found.data["japanese"] == original["japanese"])
    check("arabic preserved", found.data["arabic"] == original["arabic"])
    check("newlines preserved", found.data["newlines"] == original["newlines"])
    check("quotes preserved", found.data["quotes"] == original["quotes"])
    check("backslash preserved", found.data["backslash"] == original["backslash"])


async def test_large_dict(db):
    print("\n=== Large Dict ===")

    original = {f"key_{i:04d}": f"value_{i}" for i in range(500)}
    obj = JsonTestModel(data=original, label="large")
    await obj.save()

    found = await JsonTestModel.objects.filter(id=obj.id).first()
    check("500-key dict roundtrip", found.data == original)
    check("all keys present", len(found.data) == 500)


async def test_queryset_update_with_dict(db):
    print("\n=== QuerySet.update() with Dict ===")

    obj = JsonTestModel(data={"original": True}, label="update_test")
    await obj.save()

    new_data = {"updated": True, "version": 2}
    await JsonTestModel.objects.filter(id=obj.id).update(data=new_data)

    found = await JsonTestModel.objects.filter(id=obj.id).first()
    check("update with dict works", found.data == new_data)
    check("original key gone", "original" not in found.data)
    check("new key present", found.data.get("updated") is True)


async def test_raw_execute_with_dict(db):
    print("\n=== Raw db.execute() with Dict ===")

    test_dict = {"raw": True, "count": 99}
    await db.execute(
        "INSERT INTO test_dict_jsonb (data, label) VALUES ($1, $2)",
        test_dict,
        "raw_execute",
    )

    rows = await db.query(
        "SELECT data FROM test_dict_jsonb WHERE label = $1",
        "raw_execute",
    )
    check("raw execute with dict", len(rows) > 0)
    check("raw roundtrip match", rows[0]["data"] == test_dict)


async def test_list_jsonb_roundtrip(db):
    print("\n=== List→JSONB Roundtrip (FRAMEWORK-1) ===")

    # Flat string list — the case MESH's interests/participants hit (was rejected as a
    # PG array literal `{a,b}`; must bind as JSON `["a","b"]`).
    obj = JsonListModel(items=["alice", "bob", "carol"], label="flat_str")
    await obj.save()
    found = await JsonListModel.objects.filter(id=obj.id).first()
    check("flat string list saved", found is not None)
    check("flat string list is list", isinstance(found.items, list))
    check("flat string list roundtrip", found.items == ["alice", "bob", "carol"])

    # Flat int list.
    obj2 = JsonListModel(items=[1, 2, 3], label="flat_int")
    await obj2.save()
    found2 = await JsonListModel.objects.filter(id=obj2.id).first()
    check("flat int list roundtrip", found2.items == [1, 2, 3])

    # Strings with commas/quotes/braces — must survive the PG-array→JSON coercion.
    tricky = ["a,b", 'say "hi"', "{not json}", "back\\slash"]
    obj3 = JsonListModel(items=tricky, label="tricky")
    await obj3.save()
    found3 = await JsonListModel.objects.filter(id=obj3.id).first()
    check("tricky string list roundtrip", found3.items == tricky)

    # Empty list.
    obj4 = JsonListModel(items=[], label="empty")
    await obj4.save()
    found4 = await JsonListModel.objects.filter(id=obj4.id).first()
    check("empty list roundtrip", found4.items == [])

    # List of dicts — the nested-container path (json.dumps in extractParams).
    nested = [{"id": 1, "tag": "x"}, {"id": 2, "tag": "y"}]
    obj5 = JsonListModel(items=nested, label="nested")
    await obj5.save()
    found5 = await JsonListModel.objects.filter(id=obj5.id).first()
    check("list-of-dicts roundtrip", found5.items == nested)


async def test_list_jsonb_raw_execute(db):
    print("\n=== Raw db.execute() with List→JSONB (FRAMEWORK-1) ===")

    await db.execute(
        "INSERT INTO test_list_jsonb (items, label) VALUES ($1, $2)",
        ["read", "write", "admin"],
        "raw_list",
    )
    rows = await db.query(
        "SELECT items FROM test_list_jsonb WHERE label = $1",
        "raw_list",
    )
    check("raw execute with flat list", len(rows) > 0)
    check("raw list roundtrip", rows[0]["items"] == ["read", "write", "admin"])


async def test_multiple_dict_params(db):
    print("\n=== Multiple Dict Parameters ===")

    # Verify dicts work when mixed with other param types
    await db.execute(
        "INSERT INTO test_dict_jsonb (data, label) VALUES ($1, $2)",
        {"multi": True},
        "multi_param",
    )
    rows = await db.query(
        "SELECT data, label FROM test_dict_jsonb WHERE label = $1",
        "multi_param",
    )
    check("dict with string param", rows[0]["data"]["multi"] is True)
    check("string param preserved", rows[0]["label"] == "multi_param")


async def test_benchmark(db):
    print("\n=== Dict→JSONB Benchmark ===")

    test_data = {"user_id": 42, "role": "admin", "permissions": ["read", "write"]}
    n = 500

    # Relax throughput floor under parallel test execution — CPU contention
    # from sibling tests can halve single-threaded throughput. We still want
    # a lower bound that catches real regressions (e.g. if a native path is
    # disabled by accident), just not a tight one.
    min_ops = 20 if os.environ.get("HYPER_TEST_PARALLEL") == "1" else 50

    # Insert benchmark
    start = time.perf_counter()
    for i in range(n):
        obj = JsonTestModel(data=test_data, label=f"bench_{i}")
        await obj.save()
    elapsed = time.perf_counter() - start
    ops = n / elapsed
    check(f"Insert {n} dicts: {ops:.0f} ops/sec", ops > min_ops, f"{elapsed:.2f}s")

    # Read benchmark
    start = time.perf_counter()
    for i in range(n):
        await JsonTestModel.objects.filter(label=f"bench_{i}").first()
    elapsed = time.perf_counter() - start
    ops = n / elapsed
    check(f"Read {n} dicts: {ops:.0f} ops/sec", ops > min_ops, f"{elapsed:.2f}s")


# ── Main ──────────────────────────────────────────────────────────────────


async def async_main():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Create tables from model definitions
    await create_table_for_model(JsonTestModel, db=db, drop=True)
    await create_table_for_model(JsonListModel, db=db, drop=True)

    try:
        await test_basic_roundtrip(db)
        await test_nested_dicts(db)
        await test_all_json_types(db)
        await test_empty_dict(db)
        await test_unicode_and_special_chars(db)
        await test_large_dict(db)
        await test_queryset_update_with_dict(db)
        await test_raw_execute_with_dict(db)
        await test_list_jsonb_roundtrip(db)
        await test_list_jsonb_raw_execute(db)
        await test_multiple_dict_params(db)
        await test_benchmark(db)
    finally:
        await db.execute("DROP TABLE IF EXISTS test_dict_jsonb CASCADE")
        await db.execute("DROP TABLE IF EXISTS test_list_jsonb CASCADE")
        await db.disconnect()


def main():
    print("Dict→JSONB Native Parameter Tests")
    print("=" * 60)

    asyncio.run(async_main())

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"{total} tests: {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for e in RESULTS["errors"]:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if RESULTS["failed"] else 0)


if __name__ == "__main__":
    main()
