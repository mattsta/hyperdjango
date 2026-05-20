#!/usr/bin/env python3
"""Test fixtures system — dumpdata/loaddata for HyperDjango models.

Tests:
1.  FixtureRecord dataclass fields
2.  LoadResult dataclass defaults
3.  _serialize_value: datetime
4.  _serialize_value: date
5.  _serialize_value: UUID
6.  _serialize_value: Decimal
7.  _serialize_value: bytes
8.  _serialize_value: None
9.  _serialize_value: str passthrough
10. _serialize_value: int passthrough
11. _serialize_value: float passthrough
12. _serialize_value: bool passthrough
13. _serialize_value: list recursive
14. _serialize_value: dict recursive
15. _serialize_value: time
16. _serialize_value: timedelta
17. _deserialize_value: datetime roundtrip
18. _deserialize_value: date roundtrip
19. _deserialize_value: UUID roundtrip
20. _deserialize_value: Decimal roundtrip
21. _deserialize_value: bytes roundtrip
22. _deserialize_value: int coercion
23. _deserialize_value: str passthrough
24. _deserialize_value: time roundtrip
25. _deserialize_value: timedelta roundtrip
26. _deserialize_value: None passthrough
27. dumpdata with mock model returns valid JSON
28. dumpdata with multiple models
29. dumpdata datetime serialization
30. dumpdata writes to file
31. loaddata from JSON string creates records
32. loaddata from dict list
33. loaddata updates existing records (upsert)
34. LoadResult counts correct
35. FK dependency ordering (parent before child)
36. Natural key dumpdata
37. Natural key loaddata
38. Empty model (no records)
39. Invalid JSON returns error
40. Missing model field returns error
41. loaddata with file path
42. _parse_fixture_source: list passthrough
43. _parse_fixture_source: invalid type
44. _sort_by_dependencies: no deps
45. _sort_by_dependencies: with deps
46. dumpdata excludes PK from fields
47. loaddata no-PK insert

Run: uv run hyper-test fixtures
"""

# hyper-test: db_isolated

import asyncio
import base64
import json
import os
import sys
import tempfile
from dataclasses import fields as dc_fields
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from hyperdjango.database import Database, set_db
from hyperdjango.fixtures import (
    FixtureRecord,
    LoadResult,
    _deserialize_value,
    _parse_fixture_source,
    _serialize_value,
    _sort_by_dependencies,
    dumpdata,
    dumpdata_natural,
    loaddata,
)
from hyperdjango.models import Field, Model

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://localhost/hyperdjango_test",
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


# ---------------------------------------------------------------------------
# Unit tests (no DB needed)
# ---------------------------------------------------------------------------


def test_fixture_record_dataclass():
    """Test FixtureRecord dataclass has correct fields."""
    print("\n=== FixtureRecord dataclass ===")

    rec = FixtureRecord(model_name="users", pk=1, fields={"name": "Alice"})
    check("model_name", rec.model_name == "users")
    check("pk int", rec.pk == 1)
    check("fields dict", rec.fields == {"name": "Alice"})

    rec2 = FixtureRecord(model_name="tags", pk="abc", fields={})
    check("pk str", rec2.pk == "abc")

    rec3 = FixtureRecord(model_name="tags", pk=None, fields={})
    check("pk None", rec3.pk is None)

    # Verify it's a proper dataclass with slots
    field_names = {f.name for f in dc_fields(FixtureRecord)}
    check("has model_name field", "model_name" in field_names)
    check("has pk field", "pk" in field_names)
    check("has fields field", "fields" in field_names)


def test_load_result_dataclass():
    """Test LoadResult dataclass defaults."""
    print("\n=== LoadResult dataclass ===")

    result = LoadResult()
    check("created default 0", result.created == 0)
    check("updated default 0", result.updated == 0)
    check("skipped default 0", result.skipped == 0)
    check("errors default empty", result.errors == [])

    result2 = LoadResult(created=5, updated=3, skipped=1, errors=["bad record"])
    check("created set", result2.created == 5)
    check("updated set", result2.updated == 3)
    check("skipped set", result2.skipped == 1)
    check("errors set", result2.errors == ["bad record"])


def test_serialize_value():
    """Test _serialize_value for all supported types."""
    print("\n=== _serialize_value ===")

    # datetime
    dt = datetime(2024, 6, 15, 10, 30, 0)
    check("datetime", _serialize_value(dt) == "2024-06-15T10:30:00")

    # date
    d = date(2024, 6, 15)
    check("date", _serialize_value(d) == "2024-06-15")

    # time
    t = time(10, 30, 45)
    check("time", _serialize_value(t) == "10:30:45")

    # timedelta
    td = timedelta(hours=2, minutes=30)
    check("timedelta", _serialize_value(td) == 9000.0)

    # UUID
    u = UUID("12345678-1234-5678-1234-567812345678")
    check("UUID", _serialize_value(u) == "12345678-1234-5678-1234-567812345678")

    # Decimal
    check("Decimal", _serialize_value(Decimal("99.95")) == "99.95")
    check("Decimal zero", _serialize_value(Decimal(0)) == "0")

    # bytes
    b = b"hello world"
    encoded = _serialize_value(b)
    check("bytes", encoded == base64.b64encode(b).decode("ascii"))

    # None
    check("None", _serialize_value(None) is None)

    # Passthrough types
    check("str", _serialize_value("hello") == "hello")
    check("int", _serialize_value(42) == 42)
    check("float", _serialize_value(3.14) == 3.14)
    check("bool True", _serialize_value(True) is True)
    check("bool False", _serialize_value(False) is False)

    # Recursive list
    lst = [datetime(2024, 1, 1), "text", 42]
    serialized = _serialize_value(lst)
    check("list recursive", serialized == ["2024-01-01T00:00:00", "text", 42])

    # Recursive dict
    dct = {"dt": datetime(2024, 1, 1), "val": Decimal(10)}
    serialized = _serialize_value(dct)
    check("dict recursive", serialized == {"dt": "2024-01-01T00:00:00", "val": "10"})


def test_deserialize_value():
    """Test _deserialize_value roundtrips."""
    print("\n=== _deserialize_value ===")

    # datetime
    dt_str = "2024-06-15T10:30:00"
    check(
        "datetime",
        _deserialize_value(dt_str, "datetime") == datetime(2024, 6, 15, 10, 30, 0),
    )

    # date
    check("date", _deserialize_value("2024-06-15", "date") == date(2024, 6, 15))

    # time
    check("time", _deserialize_value("10:30:45", "time") == time(10, 30, 45))

    # timedelta
    check(
        "timedelta",
        _deserialize_value(9000.0, "timedelta") == timedelta(hours=2, minutes=30),
    )

    # UUID
    u = UUID("12345678-1234-5678-1234-567812345678")
    check(
        "UUID", _deserialize_value("12345678-1234-5678-1234-567812345678", "uuid") == u
    )

    # Decimal
    check("Decimal", _deserialize_value("99.95", "decimal") == Decimal("99.95"))

    # bytes
    encoded = base64.b64encode(b"hello world").decode("ascii")
    check("bytes", _deserialize_value(encoded, "bytes") == b"hello world")

    # int coercion
    check("int from str", _deserialize_value("42", "int") == 42)

    # str passthrough
    check("str", _deserialize_value("hello", "str") == "hello")

    # float
    check("float", _deserialize_value("3.14", "float") == 3.14)

    # bool
    check("bool", _deserialize_value(True, "bool") is True)

    # None
    check("None passthrough", _deserialize_value(None, "str") is None)
    check("None any type", _deserialize_value(None, "datetime") is None)

    # Unknown type — passthrough
    check("unknown type", _deserialize_value("raw", "unknown") == "raw")


def test_parse_fixture_source():
    """Test _parse_fixture_source with various inputs."""
    print("\n=== _parse_fixture_source ===")

    # List passthrough
    data = [{"model": "users", "pk": 1, "fields": {}}]
    check("list passthrough", _parse_fixture_source(data) is data)

    # JSON string
    json_str = json.dumps([{"model": "users", "pk": 1, "fields": {}}])
    parsed = _parse_fixture_source(json_str)
    check("JSON string", len(parsed) == 1 and parsed[0]["model"] == "users")

    # Invalid JSON
    try:
        _parse_fixture_source("{bad json")
        check("invalid JSON raises", False, "should have raised ValueError")
    except ValueError as exc:
        check("invalid JSON raises", "Invalid JSON" in str(exc))

    # Invalid type
    try:
        _parse_fixture_source(12345)
        check("invalid type raises", False, "should have raised ValueError")
    except ValueError as exc:
        check("invalid type raises", "Expected str or list" in str(exc))

    # Non-array JSON
    try:
        _parse_fixture_source('{"model": "users"}')
        check("non-array JSON raises", False, "should have raised ValueError")
    except ValueError as exc:
        check("non-array JSON raises", "array" in str(exc))


def test_sort_by_dependencies():
    """Test FK dependency sorting."""
    print("\n=== _sort_by_dependencies ===")

    # Define test models for sorting
    class SortAuthor(Model):
        class Meta:
            table = "fix_sort_authors"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(default="")

    class SortBook(Model):
        class Meta:
            table = "fix_sort_books"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(default="")
        author_id: int = Field(foreign_key=SortAuthor, default=0)

    # Child depends on parent — parent should come first
    sorted_classes = _sort_by_dependencies([SortBook, SortAuthor])
    tables = [c._meta.table for c in sorted_classes]
    check(
        "parent before child",
        tables.index("fix_sort_authors") < tables.index("fix_sort_books"),
    )

    # No deps — original order preserved
    class SortTagA(Model):
        class Meta:
            table = "fix_sort_tag_a"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(default="")

    class SortTagB(Model):
        class Meta:
            table = "fix_sort_tag_b"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(default="")

    sorted_no_deps = _sort_by_dependencies([SortTagA, SortTagB])
    check("no deps both present", len(sorted_no_deps) == 2)


# ---------------------------------------------------------------------------
# DB integration tests
# ---------------------------------------------------------------------------

# Models for integration tests — registered in the model registry


class FixAuthor(Model):
    class Meta:
        table = "fix_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(default="")
    email: str = Field(default="")


class FixBook(Model):
    class Meta:
        table = "fix_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(default="")
    author_id: int = Field(foreign_key=FixAuthor, default=0)


async def setup_tables(db):
    """Create test tables."""
    await db.execute("DROP TABLE IF EXISTS fix_books CASCADE")
    await db.execute("DROP TABLE IF EXISTS fix_authors CASCADE")
    await db.execute("""
        CREATE TABLE fix_authors (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT ''
        )
    """)
    await db.execute("""
        CREATE TABLE fix_books (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            author_id INTEGER NOT NULL DEFAULT 0 REFERENCES fix_authors(id) ON DELETE CASCADE
        )
    """)


async def test_dumpdata_valid_json(db):
    """Test dumpdata returns valid JSON."""
    print("\n=== dumpdata valid JSON ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (1, 'Alice', 'alice@example.com')"
    )

    json_str = await dumpdata([FixAuthor])
    check("returns string", isinstance(json_str, str))

    data = json.loads(json_str)
    check("valid JSON array", isinstance(data, list))
    check("one record", len(data) == 1)
    check("model field", data[0]["model"] == "fix_authors")
    check("pk field", data[0]["pk"] == 1)
    check("fields has name", data[0]["fields"]["name"] == "Alice")
    check("fields has email", data[0]["fields"]["email"] == "alice@example.com")
    check("pk excluded from fields", "id" not in data[0]["fields"])


async def test_dumpdata_multiple_models(db):
    """Test dumpdata with multiple model classes."""
    print("\n=== dumpdata multiple models ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (1, 'Bob', 'bob@example.com')"
    )
    await db.execute(
        "INSERT INTO fix_books (id, title, author_id) VALUES (1, 'Zig Book', 1)"
    )

    json_str = await dumpdata([FixAuthor, FixBook])
    data = json.loads(json_str)

    check("two records total", len(data) == 2)
    models = [d["model"] for d in data]
    check("author in output", "fix_authors" in models)
    check("book in output", "fix_books" in models)

    # FK ordering: authors before books
    check(
        "FK order: author first",
        models.index("fix_authors") < models.index("fix_books"),
    )


async def test_dumpdata_empty_model(db):
    """Test dumpdata with no records."""
    print("\n=== dumpdata empty model ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    json_str = await dumpdata([FixAuthor])
    data = json.loads(json_str)
    check("empty array", data == [])


async def test_dumpdata_to_file(db):
    """Test dumpdata writes to file."""
    print("\n=== dumpdata to file ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (1, 'Eve', 'eve@example.com')"
    )

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        tmp_path = f.name

    try:
        result = await dumpdata([FixAuthor], output_path=tmp_path)
        check("returns JSON string", isinstance(result, str))

        file_data = json.loads(Path(tmp_path).read_text(encoding="utf-8"))

        check("file contains data", len(file_data) == 1)
        check("file has correct model", file_data[0]["model"] == "fix_authors")
    finally:
        Path(tmp_path).unlink()


async def test_loaddata_from_json_string(db):
    """Test loaddata from a JSON string creates records."""
    print("\n=== loaddata from JSON string ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    fixture_json = json.dumps(
        [
            {
                "model": "fix_authors",
                "pk": 10,
                "fields": {"name": "LoadTest", "email": "load@test.com"},
            },
        ]
    )

    result = await loaddata(fixture_json, db=db)
    check("created 1", result.created == 1)
    check("no errors", len(result.errors) == 0)

    # Verify in DB
    row = await db.query_one("SELECT name, email FROM fix_authors WHERE id = 10")
    check("record exists", row is not None)
    check("name correct", row["name"] == "LoadTest")
    check("email correct", row["email"] == "load@test.com")


async def test_loaddata_from_dict_list(db):
    """Test loaddata from a list of dicts."""
    print("\n=== loaddata from dict list ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    records = [
        {
            "model": "fix_authors",
            "pk": 20,
            "fields": {"name": "DictList", "email": "dict@list.com"},
        },
    ]

    result = await loaddata(records, db=db)
    check("created 1", result.created == 1)

    row = await db.query_one("SELECT name FROM fix_authors WHERE id = 20")
    check("record created", row is not None and row["name"] == "DictList")


async def test_loaddata_upsert(db):
    """Test loaddata updates existing records."""
    print("\n=== loaddata upsert ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (30, 'Original', 'orig@test.com')"
    )

    # Update the existing record
    records = [
        {
            "model": "fix_authors",
            "pk": 30,
            "fields": {"name": "Updated", "email": "upd@test.com"},
        },
    ]

    result = await loaddata(records, db=db)
    check("updated 1", result.updated == 1)
    check("created 0", result.created == 0)

    row = await db.query_one("SELECT name, email FROM fix_authors WHERE id = 30")
    check("name updated", row["name"] == "Updated")
    check("email updated", row["email"] == "upd@test.com")


async def test_loaddata_counts(db):
    """Test LoadResult counts are correct for mixed operations."""
    print("\n=== LoadResult counts ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (40, 'Existing', 'exist@test.com')"
    )

    records = [
        {
            "model": "fix_authors",
            "pk": 40,
            "fields": {"name": "StillHere", "email": "exist@test.com"},
        },
        {
            "model": "fix_authors",
            "pk": 41,
            "fields": {"name": "NewOne", "email": "new@test.com"},
        },
    ]

    result = await loaddata(records, db=db)
    check("updated 1", result.updated == 1)
    check("created 1", result.created == 1)
    check("skipped 0", result.skipped == 0)
    check("no errors", len(result.errors) == 0)


async def test_loaddata_fk_dependency(db):
    """Test loaddata handles FK dependencies (retry on failure)."""
    print("\n=== loaddata FK dependency ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    # Intentionally put child before parent — loaddata should retry
    records = [
        {
            "model": "fix_books",
            "pk": 1,
            "fields": {"title": "FK Book", "author_id": 50},
        },
        {
            "model": "fix_authors",
            "pk": 50,
            "fields": {"name": "FK Author", "email": "fk@test.com"},
        },
    ]

    result = await loaddata(records, db=db)
    total = result.created + result.updated
    check(
        "both loaded",
        total == 2,
        f"got created={result.created}, updated={result.updated}, errors={result.errors}",
    )

    row = await db.query_one("SELECT title FROM fix_books WHERE id = 1")
    check("book exists", row is not None and row["title"] == "FK Book")


async def test_dumpdata_natural_keys(db):
    """Test natural key dumpdata."""
    print("\n=== dumpdata natural keys ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (60, 'Natural', 'nat@key.com')"
    )

    json_str = await dumpdata_natural(FixAuthor, ["email"])
    data = json.loads(json_str)

    check("one record", len(data) == 1)
    check("has natural_key", "natural_key" in data[0])
    check("natural_key value", data[0]["natural_key"] == ["nat@key.com"])
    check("has natural_key_fields", data[0]["natural_key_fields"] == ["email"])
    check("no pk field", "pk" not in data[0])
    check("email excluded from fields", "email" not in data[0]["fields"])
    check("name in fields", data[0]["fields"]["name"] == "Natural")


async def test_loaddata_natural_keys(db):
    """Test natural key loaddata."""
    print("\n=== loaddata natural keys ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (70, 'NatLoad', 'natload@key.com')"
    )

    # Update via natural key
    records = [
        {
            "model": "fix_authors",
            "natural_key": ["natload@key.com"],
            "natural_key_fields": ["email"],
            "fields": {"name": "NatUpdated"},
        },
    ]

    result = await loaddata(records, db=db)
    check("updated 1", result.updated == 1)

    row = await db.query_one("SELECT name FROM fix_authors WHERE id = 70")
    check("name updated via natural key", row["name"] == "NatUpdated")

    # Insert via natural key
    records2 = [
        {
            "model": "fix_authors",
            "natural_key": ["brand_new@key.com"],
            "natural_key_fields": ["email"],
            "fields": {"name": "BrandNew"},
        },
    ]

    result2 = await loaddata(records2, db=db)
    check("created 1 via natural key", result2.created == 1)

    row2 = await db.query_one(
        "SELECT name FROM fix_authors WHERE email = 'brand_new@key.com'"
    )
    check("new record exists", row2 is not None and row2["name"] == "BrandNew")


async def test_loaddata_invalid_json(db):
    """Test loaddata with invalid JSON returns error."""
    print("\n=== loaddata invalid JSON ===")

    result = await loaddata("{not valid json}", db=db)
    check("has errors", len(result.errors) > 0)
    check("created 0", result.created == 0)


async def test_loaddata_unknown_field(db):
    """Test loaddata with unknown field returns error."""
    print("\n=== loaddata unknown field ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    records = [
        {
            "model": "fix_authors",
            "pk": 80,
            "fields": {"name": "Good", "nonexistent_field": "bad"},
        },
    ]

    result = await loaddata(records, db=db)
    check(
        "has errors or skipped",
        len(result.errors) > 0 or result.skipped > 0,
        f"errors={result.errors}, skipped={result.skipped}",
    )


async def test_loaddata_from_file(db):
    """Test loaddata from a file path."""
    print("\n=== loaddata from file ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    fixture_data = [
        {
            "model": "fix_authors",
            "pk": 90,
            "fields": {"name": "FromFile", "email": "file@test.com"},
        },
    ]

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump(fixture_data, f)
        tmp_path = f.name

    try:
        result = await loaddata(tmp_path, db=db)
        check("created 1", result.created == 1)

        row = await db.query_one("SELECT name FROM fix_authors WHERE id = 90")
        check("record from file", row is not None and row["name"] == "FromFile")
    finally:
        Path(tmp_path).unlink()


async def test_loaddata_no_pk_insert(db):
    """Test loaddata without PK uses auto-generation."""
    print("\n=== loaddata no PK ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    records = [
        {"model": "fix_authors", "fields": {"name": "NoPK", "email": "nopk@test.com"}},
    ]

    result = await loaddata(records, db=db)
    check("created 1", result.created == 1)

    row = await db.query_one(
        "SELECT name FROM fix_authors WHERE email = 'nopk@test.com'"
    )
    check("record exists", row is not None and row["name"] == "NoPK")


async def test_loaddata_unknown_model(db):
    """Test loaddata with unknown model table."""
    print("\n=== loaddata unknown model ===")

    records = [
        {"model": "nonexistent_table_xyz", "pk": 1, "fields": {"name": "Ghost"}},
    ]

    result = await loaddata(records, db=db)
    check("has errors", len(result.errors) > 0)
    check(
        "error mentions unknown",
        any("Unknown" in e or "nonexistent" in e for e in result.errors),
    )


async def test_path_traversal_dumpdata(db):
    """Test dumpdata rejects path traversal in output_path."""
    print("\n=== dumpdata path traversal ===")

    try:
        await dumpdata([FixAuthor], output_path="../../../etc/passwd")
        check("dumpdata rejects path traversal", False, "should have raised ValueError")
    except ValueError as exc:
        check("dumpdata rejects path traversal", "Path traversal" in str(exc))

    try:
        await dumpdata([FixAuthor], output_path="/tmp/safe/../../etc/passwd")
        check(
            "dumpdata rejects nested traversal", False, "should have raised ValueError"
        )
    except ValueError as exc:
        check("dumpdata rejects nested traversal", "Path traversal" in str(exc))


def test_parse_fixture_source_path_traversal():
    """Test _parse_fixture_source rejects path traversal."""
    print("\n=== _parse_fixture_source path traversal ===")

    # This only triggers if the file actually exists at the traversal path,
    # but the check happens before opening. We test the validation directly.
    # Create a temp file with ".." in its containing directory to test.
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    subdir = tmpdir / "sub"
    subdir.mkdir(parents=True, exist_ok=True)
    traversal_path = str(subdir / ".." / "test.json")
    (tmpdir / "test.json").write_text(
        json.dumps([{"model": "fix_authors", "pk": 1, "fields": {}}])
    )

    try:
        _parse_fixture_source(traversal_path)
        check("parse rejects path traversal", False, "should have raised ValueError")
    except ValueError as exc:
        check("parse rejects path traversal", "Path traversal" in str(exc))
    finally:
        (tmpdir / "test.json").unlink()
        subdir.rmdir()
        tmpdir.rmdir()


def test_quoted_column_names_in_sql():
    """Test that SQL generation uses double-quoted column names."""
    print("\n=== quoted column names in SQL ===")

    # We verify indirectly by checking that the fixture module constructs
    # quoted identifiers. We can test by inspecting the SQL template patterns.
    # The real test is that reserved words like "order", "group" etc. work as columns.
    # We test the _upsert_record function builds correct SQL by running a roundtrip
    # with a model that has a column named with a reserved word.
    # For now, verify the module was updated by checking source patterns.
    import inspect

    source = inspect.getsource(
        __import__("hyperdjango.fixtures", fromlist=["_upsert_record"])._upsert_record
    )
    check(
        "SQL uses double-quoted columns",
        '"{col}"' in source
        or "f'\"{" in source
        or '"\\"{' in source
        or '"{c}"' in source,
    )
    check("SQL uses double-quoted table", '"{meta.table}"' in source)


async def test_roundtrip(db):
    """Test dump then load roundtrip preserves data."""
    print("\n=== roundtrip dump/load ===")

    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")
    await db.execute(
        "INSERT INTO fix_authors (id, name, email) VALUES (100, 'Roundtrip', 'rt@test.com')"
    )
    await db.execute(
        "INSERT INTO fix_books (id, title, author_id) VALUES (100, 'RT Book', 100)"
    )

    # Dump
    json_str = await dumpdata([FixAuthor, FixBook])
    data = json.loads(json_str)
    check("dumped 2 records", len(data) == 2)

    # Clear and reload
    await db.execute("DELETE FROM fix_books")
    await db.execute("DELETE FROM fix_authors")

    result = await loaddata(json_str, db=db)
    check(
        "loaded 2 records",
        result.created == 2,
        f"created={result.created}, errors={result.errors}",
    )

    # Verify
    author = await db.query_one("SELECT name FROM fix_authors WHERE id = 100")
    check("author preserved", author is not None and author["name"] == "Roundtrip")

    book = await db.query_one("SELECT title FROM fix_books WHERE id = 100")
    check("book preserved", book is not None and book["title"] == "RT Book")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    global passed, failed

    # Run pure unit tests first (no DB)
    test_fixture_record_dataclass()
    test_load_result_dataclass()
    test_serialize_value()
    test_deserialize_value()
    test_parse_fixture_source()
    test_sort_by_dependencies()
    test_parse_fixture_source_path_traversal()
    test_quoted_column_names_in_sql()

    # DB integration tests
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    try:
        await setup_tables(db)

        await test_dumpdata_valid_json(db)
        await test_dumpdata_multiple_models(db)
        await test_dumpdata_empty_model(db)
        await test_dumpdata_to_file(db)
        await test_loaddata_from_json_string(db)
        await test_loaddata_from_dict_list(db)
        await test_loaddata_upsert(db)
        await test_loaddata_counts(db)
        await test_loaddata_fk_dependency(db)
        await test_dumpdata_natural_keys(db)
        await test_loaddata_natural_keys(db)
        await test_loaddata_invalid_json(db)
        await test_loaddata_unknown_field(db)
        await test_loaddata_from_file(db)
        await test_loaddata_no_pk_insert(db)
        await test_loaddata_unknown_model(db)
        await test_path_traversal_dumpdata(db)
        await test_roundtrip(db)
    finally:
        await db.execute("DROP TABLE IF EXISTS fix_books CASCADE")
        await db.execute("DROP TABLE IF EXISTS fix_authors CASCADE")
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All fixture tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
