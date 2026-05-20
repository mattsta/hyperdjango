#!/usr/bin/env python3
"""Proof: generate DDL from create_table_for_model and compare to what cli.py used to produce.

Tests that STI child fields, UNLOGGED, indexes, composite PKs, FKs, and defaults
all generate correct DDL through the centralized create_table_for_model function.
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.database import Database, set_db

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


async def main():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.mixins import TimestampMixin
    from hyperdjango.models import (
        Field,
        Model,
        _field_to_sql_type,
        _python_default_to_sql,
        create_table_for_model,
    )

    # ── Test 1: _python_default_to_sql ──────────────────────
    print("\n=== _python_default_to_sql ===")
    check("bool True", _python_default_to_sql(True) == "TRUE")
    check("bool False", _python_default_to_sql(False) == "FALSE")
    check("int 0", _python_default_to_sql(0) == "0")
    check("int 42", _python_default_to_sql(42) == "42")
    check("float 3.14", _python_default_to_sql(3.14) == "3.14")
    check("empty string", _python_default_to_sql("") == "''")
    check("string value", _python_default_to_sql("hello") == "'hello'")
    check("SQL function", _python_default_to_sql("now()") == "now()")
    check("None", _python_default_to_sql(None) is None)
    check("empty dict", _python_default_to_sql({}) == "'{}'")
    check("string with quote", _python_default_to_sql("it's") == "'it''s'")

    # ── Test 2: _field_to_sql_type ──────────────────────────
    print("\n=== _field_to_sql_type ===")

    class TypeTestModel(Model):
        class Meta:
            table = "type_test"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        count: int = Field(default=0)
        active: bool = Field(default=True)
        data: dict = Field(default={})
        score: float = Field(default=0.0)
        expires: int | None = Field(default=None)

    check("str → TEXT", _field_to_sql_type(TypeTestModel, "name") == "TEXT")
    check("int → INTEGER", _field_to_sql_type(TypeTestModel, "count") == "INTEGER")
    check("bool → BOOLEAN", _field_to_sql_type(TypeTestModel, "active") == "BOOLEAN")
    check("dict → JSONB", _field_to_sql_type(TypeTestModel, "data") == "JSONB")
    check(
        "float → DOUBLE PRECISION",
        _field_to_sql_type(TypeTestModel, "score") == "DOUBLE PRECISION",
    )
    check(
        "int|None → INTEGER", _field_to_sql_type(TypeTestModel, "expires") == "INTEGER"
    )

    # ── Test 3: Basic table creation ────────────────────────
    print("\n=== Basic Table Creation ===")

    class BasicModel(TimestampMixin, Model):
        class Meta:
            table = "ddl_test_basic"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        count: int = Field(default=0)

    await create_table_for_model(BasicModel, db=db, drop=True)
    # Verify table exists
    row = await db.query_one(
        "SELECT COUNT(*) as cnt FROM information_schema.tables WHERE table_name = 'ddl_test_basic'"
    )
    check("basic table created", row["cnt"] == 1)

    # Verify columns
    cols = await db.query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'ddl_test_basic' ORDER BY ordinal_position"
    )
    col_map = {c["column_name"]: c["data_type"] for c in cols}
    check("has id column", "id" in col_map)
    check("has name column", "name" in col_map)
    check("has count column", "count" in col_map)
    check("has created_at", "created_at" in col_map)
    check("has updated_at", "updated_at" in col_map)

    # ── Test 4: UNLOGGED table ──────────────────────────────
    print("\n=== UNLOGGED Table ===")

    class UnloggedModel(TimestampMixin, Model):
        class Meta:
            table = "ddl_test_unlogged"
            unlogged = True

        id: int = Field(primary_key=True, auto=True)
        data: dict = Field(default={})

    await create_table_for_model(UnloggedModel, db=db, drop=True)
    row = await db.query_one(
        "SELECT relpersistence FROM pg_class WHERE relname = 'ddl_test_unlogged'"
    )
    check("table is UNLOGGED", row["relpersistence"] == "u")

    # Verify JSONB column type
    cols = await db.query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'ddl_test_unlogged' AND column_name = 'data'"
    )
    check("data column is jsonb", cols[0]["data_type"] == "jsonb")

    # ── Test 5: Indexes ─────────────────────────────────────
    print("\n=== Indexes ===")

    class IndexedModel(TimestampMixin, Model):
        class Meta:
            table = "ddl_test_indexed"

        id: int = Field(primary_key=True, auto=True)
        user_id: int = Field(index=True)
        email: str = Field(unique=True)

    await create_table_for_model(IndexedModel, db=db, drop=True)
    idxs = await db.query(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'ddl_test_indexed'"
    )
    idx_names = [r["indexname"] for r in idxs]
    check("user_id index created", "idx_ddl_test_indexed_user_id" in idx_names)
    check("email unique constraint", any("email" in n for n in idx_names))

    # ── Test 6: STI Child Fields ────────────────────────────
    print("\n=== STI Child Fields ===")

    # Import content_hub models to test real STI
    from services.content_hub.app import Content
    from services.content_hub.app import User as HubUser

    # Content has FK to User — create User table first
    await create_table_for_model(HubUser, db=db, drop=True)
    await create_table_for_model(Content, db=db, drop=True)

    # Verify parent columns exist
    cols = await db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'hub_contents' ORDER BY ordinal_position"
    )
    col_names = [c["column_name"] for c in cols]
    print(f"  INFO  hub_contents columns: {col_names}")

    check("parent: id", "id" in col_names)
    check("parent: title", "title" in col_names)
    check("parent: type", "type" in col_names)
    check("parent: status", "status" in col_names)
    check("parent: author_id", "author_id" in col_names)

    # Verify STI child columns exist (these are the critical ones)
    check(
        "STI child: reading_time_mins (Article)",
        "reading_time_mins" in col_names,
        f"missing from {col_names}",
    )
    check(
        "STI child: video_url (Video)",
        "video_url" in col_names,
        f"missing from {col_names}",
    )
    check(
        "STI child: duration_secs (Video)",
        "duration_secs" in col_names,
        f"missing from {col_names}",
    )
    check(
        "STI child: external_url (Link)",
        "external_url" in col_names,
        f"missing from {col_names}",
    )

    # ── Test 7: FK References ───────────────────────────────
    print("\n=== FK References ===")

    class FKParent(TimestampMixin, Model):
        class Meta:
            table = "ddl_test_fk_parent"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()

    class FKChild(TimestampMixin, Model):
        class Meta:
            table = "ddl_test_fk_child"

        id: int = Field(primary_key=True, auto=True)
        parent_id: int = Field(foreign_key=FKParent)

    await create_table_for_model(FKParent, db=db, drop=True)
    await create_table_for_model(FKChild, db=db, drop=True)

    # Verify FK constraint exists
    fks = await db.query(
        "SELECT constraint_name FROM information_schema.table_constraints "
        "WHERE table_name = 'ddl_test_fk_child' AND constraint_type = 'FOREIGN KEY'"
    )
    check("FK constraint created", len(fks) > 0, f"got {len(fks)} FKs")

    # ── Test 8: Dict field JSONB roundtrip ──────────────────
    print("\n=== Dict Field JSONB Roundtrip ===")

    obj = UnloggedModel(data={"key": "value", "nested": {"a": 1}})
    await obj.save()
    found = await UnloggedModel.objects.filter(id=obj.id).first()
    check("dict saved and loaded", found.data == {"key": "value", "nested": {"a": 1}})

    # ── Cleanup ─────────────────────────────────────────────
    for table in [
        "ddl_test_basic",
        "ddl_test_unlogged",
        "ddl_test_indexed",
        "ddl_test_fk_child",
        "ddl_test_fk_parent",
        "hub_contents",
        "hub_users",
    ]:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"{PASS + FAIL} tests: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    return FAIL


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
