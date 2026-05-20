"""Tests for DATABASE-level column defaults via Field(db_default=...).

A ``db_default`` is evaluated by PostgreSQL on INSERT (distinct from the
Python-side ``default``). It accepts either a Python literal (SQL-quoted
automatically) or a ``DatabaseDefault(<raw sql>)`` marker for a DB-side
expression such as ``gen_random_uuid()`` or ``now()``.

Covers:
  (a) generate_ddl_for_model emits DEFAULT for a non-PK literal column and for
      a PK column carrying a DatabaseDefault expression.
  (b) inserting a row WITHOUT specifying the db_default column lets PostgreSQL
      fill it; the saved instance reflects the DB value (read-back).
  (c) a UUID primary key defaulting to a DB expression works end to end:
      model defined -> table created -> Model(...).save() without pk ->
      row has a server-generated UUID pk.
  (d) is_persisted / unsaved detection is correct before and after save.

This is a self-contained live-DB test: it builds its own tables from model
definitions via the ORM DDL path and runs against the database supplied in
DATABASE_URL.

# hyper-test: db_isolated

Usage:
    uv run hyper-test db_default
"""

import asyncio
import inspect
import os
import sys
import traceback
import uuid

from hyperdjango.database import Database, set_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import (
    DatabaseDefault,
    Field,
    generate_ddl_for_model,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


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
# Test models — every model uses TimestampMixin per platform convention.
# ---------------------------------------------------------------------------


class Widget(TimestampMixin):
    """Integer SERIAL PK with several db_default columns (literals + raw SQL)."""

    class Meta:
        table = "test_dbdefault_widget"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    # Literal db_default -> DEFAULT 0 (SQL-quoted/escaped by the literal path).
    quantity: int = Field(db_default=0)
    # Literal string db_default -> DEFAULT 'active'.
    status: str = Field(db_default="active")
    # Literal bool db_default -> DEFAULT TRUE.
    enabled: bool = Field(db_default=True)
    # Raw SQL expression db_default -> DEFAULT gen_random_uuid().
    token: uuid.UUID = Field(db_default=DatabaseDefault("gen_random_uuid()"))


class Document(TimestampMixin):
    """UUID primary key supplied by a DB-side expression default."""

    class Meta:
        table = "test_dbdefault_document"

    id: uuid.UUID = Field(
        primary_key=True,
        db_default=DatabaseDefault("gen_random_uuid()"),
    )
    title: str = Field()


# ---------------------------------------------------------------------------
# (a) DDL generation
# ---------------------------------------------------------------------------


@test("DDL: non-PK literal db_default emits DEFAULT 0")
def test_ddl_literal_int():
    create_sql = generate_ddl_for_model(Widget)[0]
    # NOT NULL is inferred from the non-optional annotation; DEFAULT follows it.
    assert "quantity INTEGER NOT NULL DEFAULT 0" in create_sql, create_sql


@test("DDL: non-PK string literal db_default emits DEFAULT 'active'")
def test_ddl_literal_str():
    create_sql = generate_ddl_for_model(Widget)[0]
    assert "status TEXT NOT NULL DEFAULT 'active'" in create_sql, create_sql


@test("DDL: non-PK bool literal db_default emits DEFAULT TRUE")
def test_ddl_literal_bool():
    create_sql = generate_ddl_for_model(Widget)[0]
    assert "enabled BOOLEAN NOT NULL DEFAULT TRUE" in create_sql, create_sql


@test("DDL: non-PK DatabaseDefault emits raw SQL expression")
def test_ddl_raw_expr_nonpk():
    create_sql = generate_ddl_for_model(Widget)[0]
    assert "token UUID NOT NULL DEFAULT gen_random_uuid()" in create_sql, create_sql


@test("DDL: PK column with DatabaseDefault emits DEFAULT (no PK skip)")
def test_ddl_pk_db_default():
    create_sql = generate_ddl_for_model(Document)[0]
    # The PK keeps its PRIMARY KEY marker AND gets a DEFAULT clause.
    assert "id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in create_sql, create_sql


@test("DDL: literal db_default is SQL-quoted, not interpolated unsafely")
def test_ddl_literal_quoting():
    # A string default with an embedded quote must be escaped, not injected.
    class Tricky(TimestampMixin):
        class Meta:
            table = "test_dbdefault_tricky"

        id: int = Field(primary_key=True, auto=True)
        label: str = Field(db_default="O'Brien")

    create_sql = generate_ddl_for_model(Tricky)[0]
    assert "label TEXT NOT NULL DEFAULT 'O''Brien'" in create_sql, create_sql


# ---------------------------------------------------------------------------
# Live-DB setup
# ---------------------------------------------------------------------------

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/postgres")
_db = None


async def _get_db():
    global _db
    if _db is None:
        _db = Database(DB_URL)
        await _db.connect()
        set_db(_db)
    return _db


@test("DB: create tables from model definitions via DDL path")
async def test_create_tables():
    db = await _get_db()
    # gen_random_uuid() ships in PostgreSQL core (pgcrypto/pg13+); ensure ext
    # exists for older servers without failing if already present.
    await db.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    await db.execute("DROP TABLE IF EXISTS test_dbdefault_widget CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_dbdefault_document CASCADE")
    for sql in generate_ddl_for_model(Widget):
        await db.execute(sql)
    for sql in generate_ddl_for_model(Document):
        await db.execute(sql)


# ---------------------------------------------------------------------------
# (d) is_persisted before save
# ---------------------------------------------------------------------------


@test("is_persisted: unsaved instance with db_default PK is NOT persisted")
def test_not_persisted_before_save():
    doc = Document(title="draft")
    assert doc.is_persisted is False
    # PK resolves to None before the DB assigns it.
    assert doc.pk is None


@test("is_persisted: unsaved instance with SERIAL PK is NOT persisted")
def test_not_persisted_serial_before_save():
    w = Widget(name="thing")
    assert w.is_persisted is False


# ---------------------------------------------------------------------------
# (b) read-back: DB fills the db_default columns; instance reflects DB value
# ---------------------------------------------------------------------------


@test("DB: insert without db_default columns lets PostgreSQL fill them")
async def test_db_fills_defaults():
    db = await _get_db()
    w = Widget(name="alpha")
    await w.save(db=db)

    # SERIAL id was assigned.
    assert w.id is not None
    assert isinstance(w.id, int)

    # Verify the DB applied the literal defaults.
    row = await db.query_one(
        "SELECT quantity, status, enabled, token "
        "FROM test_dbdefault_widget WHERE id = $1",
        w.id,
    )
    assert row is not None
    assert row["quantity"] == 0
    assert row["status"] == "active"
    assert row["enabled"] is True
    # gen_random_uuid() produced a real UUID for the non-PK token column.
    assert isinstance(row["token"], uuid.UUID)


@test("DB: explicit value overrides the db_default")
async def test_explicit_overrides_default():
    db = await _get_db()
    w = Widget(name="beta", quantity=99, status="archived")
    await w.save(db=db)

    row = await db.query_one(
        "SELECT quantity, status FROM test_dbdefault_widget WHERE id = $1",
        w.id,
    )
    assert row["quantity"] == 99
    assert row["status"] == "archived"


# ---------------------------------------------------------------------------
# (c) UUID PK from a DB expression, end to end
# ---------------------------------------------------------------------------


@test("DB: UUID PK from db_default is server-generated on save()")
async def test_uuid_pk_end_to_end():
    db = await _get_db()
    doc = Document(title="report")

    # Before save: no pk, not persisted.
    assert doc.pk is None
    assert doc.is_persisted is False

    await doc.save(db=db)

    # After save: server-generated UUID pk, instance reflects it.
    assert doc.pk is not None
    assert isinstance(doc.id, uuid.UUID), f"id is {type(doc.id)}: {doc.id!r}"
    assert doc.is_persisted is True

    # The generated pk actually exists in the DB.
    row = await db.query_one(
        "SELECT id, title FROM test_dbdefault_document WHERE id = $1",
        doc.id,
    )
    assert row is not None
    assert row["id"] == doc.id
    assert row["title"] == "report"


@test("DB: two UUID-PK rows get distinct server-generated pks")
async def test_uuid_pk_distinct():
    db = await _get_db()
    a = Document(title="one")
    b = Document(title="two")
    await a.save(db=db)
    await b.save(db=db)
    assert a.id != b.id
    assert isinstance(a.id, uuid.UUID)
    assert isinstance(b.id, uuid.UUID)


@test("DB: explicit UUID pk is honored (overrides db_default)")
async def test_uuid_pk_explicit():
    db = await _get_db()
    fixed = uuid.uuid4()
    doc = Document(id=fixed, title="pinned")
    await doc.save(db=db)
    assert doc.id == fixed
    row = await db.query_one(
        "SELECT title FROM test_dbdefault_document WHERE id = $1",
        fixed,
    )
    assert row is not None
    assert row["title"] == "pinned"


# ---------------------------------------------------------------------------
# (d) is_persisted after save + round-trip
# ---------------------------------------------------------------------------


@test("is_persisted: True after save, and loaded rows are persisted")
async def test_persisted_after_save_and_load():
    db = await _get_db()
    doc = Document(title="loaded")
    await doc.save(db=db)
    assert doc.is_persisted is True

    fetched = await Document.objects.using(db).get(id=doc.id)
    assert fetched.is_persisted is True
    assert fetched.id == doc.id
    assert fetched.title == "loaded"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


@test("cleanup: drop test tables")
async def test_cleanup():
    db = await _get_db()
    await db.execute("DROP TABLE IF EXISTS test_dbdefault_widget CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_dbdefault_document CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_dbdefault_tricky CASCADE")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nDatabase Default (db_default) Tests ({len(tests)} tests)")
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

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
