#!/usr/bin/env python3
"""Regression tests for migration-correctness fixes (H1-H4, M1-M4, L1).

Every fix is verified against a LIVE PostgreSQL database: the generated DDL is
applied to a scratch schema and the resulting pg_catalog state is introspected
and compared to hand-verified expectations, with idempotency checked where it
matters (a 2nd diff of an unchanged model must produce no operations).

Run: uv run hyper-test migration_correctness
Requires: PostgreSQL running (DATABASE_URL or default), pgvector extension.
"""

# hyper-test: db_isolated

import asyncio
import contextlib
import os
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from hyperdjango.database import Database, set_db
from hyperdjango.migrations import (
    AddColumn,
    AddConstraint,
    AlterColumnType,
    CreateTable,
    CreateVectorIndex,
    DatabaseIntrospector,
    DropColumn,
    MigrationFileManager,
    ModelExtractor,
    RenameColumn,
    SchemaDiffer,
    SchemaSnapshot,
    _line_code_before_comment,
    _normalize_pg_type,
    _types_equivalent,
)
from hyperdjango.models import DatabaseDefault, Field, Index, Model, VectorField

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

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


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    with contextlib.suppress(Exception):
        await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return db


async def cleanup(db, tables):
    for t in tables:
        with contextlib.suppress(Exception):
            await db.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


EMPTY = SchemaSnapshot(tables={})


async def apply_ops(db, ops):
    """Apply every op's forward SQL, in order, against the live DB."""
    for op in ops:
        await db.execute(op.up_sql())


def ops_for(ops, table):
    """All ops touching `table` (matches .table attr and RunSQL descriptions)."""
    out = []
    for op in ops:
        t = getattr(op, "table", None)
        if t == table or table in op.description():
            out.append(op)
    return out


# ── H1: db_default honored in generated CREATE ──────────────────────────────


async def test_h1_db_default(db):
    print("\n=== H1: db_default in CREATE TABLE ===")
    await cleanup(db, ["test_mc_h1"])

    class H1Doc(Model):
        class Meta:
            table = "test_mc_h1"

        id: UUID = Field(
            primary_key=True, db_default=DatabaseDefault("gen_random_uuid()")
        )
        created_at: datetime = Field(db_default=DatabaseDefault("now()"))
        title: str = Field(default="untitled")

    schema = ModelExtractor.extract(H1Doc)
    check(
        "id default_sql = gen_random_uuid()",
        schema.columns["id"].default_sql == "gen_random_uuid()",
        f"got {schema.columns['id'].default_sql!r}",
    )
    check(
        "created_at default_sql = now()",
        schema.columns["created_at"].default_sql == "now()",
        f"got {schema.columns['created_at'].default_sql!r}",
    )

    ops = SchemaDiffer.diff([schema], EMPTY)
    create = next(op for op in ops if isinstance(op, CreateTable))
    ddl = create.up_sql()
    check("CREATE has DEFAULT gen_random_uuid()", "DEFAULT gen_random_uuid()" in ddl)
    check("CREATE has DEFAULT now()", "DEFAULT now()" in ddl)

    await apply_ops(db, ops)
    # Insert omitting BOTH db_default columns — must succeed and be filled.
    await db.execute("INSERT INTO test_mc_h1 (title) VALUES ($1)", "hello")
    row = await db.query("SELECT id, created_at, title FROM test_mc_h1")
    check("insert omitting db_default columns succeeded", len(row) == 1)
    if row:
        check("id filled by DB", row[0]["id"] is not None)
        check("created_at filled by DB", row[0]["created_at"] is not None)


# ── H2: indexes created at initial table creation ──────────────────────────


async def test_h2_initial_indexes(db):
    print("\n=== H2: secondary indexes at initial deploy ===")
    await cleanup(db, ["test_mc_h2"])

    class H2Item(Model):
        class Meta:
            table = "test_mc_h2"

        id: int = Field(primary_key=True, auto=True)
        slug: str = Field(index=True)
        embedding: list[float] = VectorField(dimensions=8, index=True)

    schema = ModelExtractor.extract(H2Item)
    ops = SchemaDiffer.diff([schema], EMPTY)
    check(
        "diff emits a CreateVectorIndex for fresh table",
        any(isinstance(op, CreateVectorIndex) for op in ops),
    )

    await apply_ops(db, ops)
    snap = await DatabaseIntrospector.introspect(db)
    idx_names = {i.name for i in snap.tables["test_mc_h2"].indexes}
    check(
        "btree index on slug exists",
        "idx_test_mc_h2_slug" in idx_names,
        f"got {idx_names}",
    )
    check(
        "hnsw vector index exists",
        "idx_test_mc_h2_embedding_hnsw" in idx_names,
        f"got {idx_names}",
    )

    # Idempotency: 2nd diff of the same model must be a no-op for this table.
    redo = ops_for(SchemaDiffer.diff([schema], snap), "test_mc_h2")
    check(
        "2nd makemigrations = No changes (H2)",
        not redo,
        f"got {[o.description() for o in redo]}",
    )


# ── H3: vector(N) introspection idempotency ────────────────────────────────


async def test_h3_vector_idempotent(db):
    print("\n=== H3: vector(N) no false drift ===")
    await cleanup(db, ["test_mc_h3"])

    class H3Emb(Model):
        class Meta:
            table = "test_mc_h3"

        id: int = Field(primary_key=True, auto=True)
        vec: list[float] = VectorField(dimensions=8, index=False)

    schema = ModelExtractor.extract(H3Emb)
    await apply_ops(db, SchemaDiffer.diff([schema], EMPTY))

    snap = await DatabaseIntrospector.introspect(db)
    disp = snap.tables["test_mc_h3"].columns["vec"].type_display
    check(
        "vector dimension introspected as VECTOR(8)",
        disp == "VECTOR(8)",
        f"got {disp!r}",
    )
    check("VECTOR(8) ≡ VECTOR(8)", _types_equivalent("VECTOR(8)", disp))

    redo = ops_for(SchemaDiffer.diff([schema], snap), "test_mc_h3")
    alters = [o for o in redo if isinstance(o, AlterColumnType)]
    check(
        "no spurious ALTER COLUMN TYPE for vector",
        not alters,
        f"got {[o.description() for o in alters]}",
    )
    check(
        "2nd makemigrations = No changes (H3)",
        not redo,
        f"got {[o.description() for o in redo]}",
    )


# ── H4: table dependency ordering (cycles + forward refs) ──────────────────


async def test_h4_mutual_fk(db):
    print("\n=== H4: mutual FK (cycle) ordering ===")
    await cleanup(db, ["test_mc_h4_a", "test_mc_h4_b"])

    class H4A(Model):
        class Meta:
            table = "test_mc_h4_a"

        id: int = Field(primary_key=True, auto=True)
        b_id: int | None = Field(foreign_key="test_mc_h4_b")

    class H4B(Model):
        class Meta:
            table = "test_mc_h4_b"

        id: int = Field(primary_key=True, auto=True)
        a_id: int | None = Field(foreign_key="test_mc_h4_a")

    ops = SchemaDiffer.diff(
        [ModelExtractor.extract(H4A), ModelExtractor.extract(H4B)], EMPTY
    )
    check(
        "cycle broken into a deferred AddConstraint",
        any(isinstance(op, AddConstraint) for op in ops),
    )
    # Every CreateTable must precede any deferred FK AddConstraint.
    last_create = max(i for i, op in enumerate(ops) if isinstance(op, CreateTable))
    first_addfk = min(
        (i for i, op in enumerate(ops) if isinstance(op, AddConstraint)),
        default=len(ops),
    )
    check("all CREATE TABLE before deferred FK", last_create < first_addfk)

    ok = True
    try:
        await apply_ops(db, ops)
    except Exception as e:  # noqa: BLE001
        ok = False
        check("mutual-FK migration applied cleanly", False, str(e))
    if ok:
        snap = await DatabaseIntrospector.introspect(db)
        check("table a created", "test_mc_h4_a" in snap.tables)
        check("table b created", "test_mc_h4_b" in snap.tables)
        fks_a = [c for c in snap.tables["test_mc_h4_a"].constraints if c.type == "f"]
        fks_b = [c for c in snap.tables["test_mc_h4_b"].constraints if c.type == "f"]
        check("a has its FK to b", len(fks_a) == 1, f"got {len(fks_a)}")
        check("b has its FK to a", len(fks_b) == 1, f"got {len(fks_b)}")


async def test_h4_forward_ref(db):
    print("\n=== H4: forward-reference FK reordering ===")
    await cleanup(db, ["test_mc_h4_parent", "test_mc_h4_child"])

    # Parent is DEFINED first but references a table created later.
    class H4Parent(Model):
        class Meta:
            table = "test_mc_h4_parent"

        id: int = Field(primary_key=True, auto=True)
        child_id: int | None = Field(foreign_key="test_mc_h4_child")

    class H4Child(Model):
        class Meta:
            table = "test_mc_h4_child"

        id: int = Field(primary_key=True, auto=True)

    ops = SchemaDiffer.diff(
        [ModelExtractor.extract(H4Parent), ModelExtractor.extract(H4Child)], EMPTY
    )
    creates = [op.table for op in ops if isinstance(op, CreateTable)]
    check(
        "child created before parent (topo reorder)",
        creates.index("test_mc_h4_child") < creates.index("test_mc_h4_parent"),
        f"order: {creates}",
    )
    ok = True
    try:
        await apply_ops(db, ops)
    except Exception as e:  # noqa: BLE001
        ok = False
        check("forward-ref migration applied cleanly", False, str(e))
    if ok:
        snap = await DatabaseIntrospector.introspect(db)
        check(
            "parent + child both created",
            "test_mc_h4_parent" in snap.tables and "test_mc_h4_child" in snap.tables,
        )


# ── M1: adding an FK column yields exactly one FK constraint ────────────────


async def test_m1_single_fk(db):
    print("\n=== M1: no duplicate FK on added column ===")
    await cleanup(db, ["test_mc_m1_books", "test_mc_m1_authors"])

    class M1Author(Model):
        class Meta:
            table = "test_mc_m1_authors"

        id: int = Field(primary_key=True, auto=True)

    class M1BookV1(Model):
        class Meta:
            table = "test_mc_m1_books"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(default="")

    await apply_ops(
        db,
        SchemaDiffer.diff(
            [ModelExtractor.extract(M1Author), ModelExtractor.extract(M1BookV1)], EMPTY
        ),
    )

    class M1BookV2(Model):
        class Meta:
            table = "test_mc_m1_books"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(default="")
        author_id: int = Field(foreign_key="test_mc_m1_authors")

    snap = await DatabaseIntrospector.introspect(db)
    ops = ops_for(
        SchemaDiffer.diff(
            [ModelExtractor.extract(M1Author), ModelExtractor.extract(M1BookV2)], snap
        ),
        "test_mc_m1_books",
    )
    add_cols = [o for o in ops if isinstance(o, AddColumn) and o.column == "author_id"]
    add_fks = [o for o in ops if isinstance(o, AddConstraint)]
    check("emits AddColumn for author_id", len(add_cols) == 1)
    check(
        "does NOT independently emit an AddConstraint FK for the new column",
        len(add_fks) == 0,
        f"got {[o.description() for o in add_fks]}",
    )

    await apply_ops(db, ops)
    snap2 = await DatabaseIntrospector.introspect(db)
    fks = [c for c in snap2.tables["test_mc_m1_books"].constraints if c.type == "f"]
    check("exactly ONE fk constraint after add", len(fks) == 1, f"got {len(fks)}")


# ── M2: Meta.indexes + unique_together ─────────────────────────────────────


async def test_m2_composite(db):
    print("\n=== M2: Meta.indexes + unique_together ===")
    await cleanup(db, ["test_mc_m2"])

    class M2Model(Model):
        class Meta:
            table = "test_mc_m2"
            unique_together = [("tenant_id", "slug")]
            indexes = [
                Index(fields=("tenant_id", "status")),
                Index(fields=("slug",), unique=True, name="uq_m2_slug_idx"),
            ]

        id: int = Field(primary_key=True, auto=True)
        tenant_id: int = Field()
        slug: str = Field()
        status: str = Field(default="active")

    schema = ModelExtractor.extract(M2Model)
    await apply_ops(db, SchemaDiffer.diff([schema], EMPTY))

    snap = await DatabaseIntrospector.introspect(db)
    t = snap.tables["test_mc_m2"]
    uniques = {tuple(c.columns) for c in t.constraints if c.type == "u"}
    check(
        "unique_together (tenant_id, slug) present",
        ("tenant_id", "slug") in uniques,
        f"got {uniques}",
    )
    idx_names = {i.name for i in t.indexes}
    check(
        "composite Meta index present",
        "idx_test_mc_m2_tenant_id_status" in idx_names,
        f"got {idx_names}",
    )
    check(
        "named unique Meta index present",
        "uq_m2_slug_idx" in idx_names,
        f"got {idx_names}",
    )

    redo = ops_for(SchemaDiffer.diff([schema], snap), "test_mc_m2")
    check(
        "idempotent after Meta.indexes/unique_together",
        not redo,
        f"got {[o.description() for o in redo]}",
    )


# ── M3: NUMERIC(precision, scale) ──────────────────────────────────────────


async def test_m3_numeric(db):
    print("\n=== M3: Decimal precision/scale ===")
    await cleanup(db, ["test_mc_m3"])

    class M3Model(Model):
        class Meta:
            table = "test_mc_m3"

        id: int = Field(primary_key=True, auto=True)
        price: Decimal = Field(max_digits=10, decimal_places=2)

    schema = ModelExtractor.extract(M3Model)
    check(
        "type_sql = NUMERIC(10, 2)",
        schema.columns["price"].type_sql == "NUMERIC(10, 2)",
        f"got {schema.columns['price'].type_sql!r}",
    )

    await apply_ops(db, SchemaDiffer.diff([schema], EMPTY))
    snap = await DatabaseIntrospector.introspect(db)
    disp = snap.tables["test_mc_m3"].columns["price"].type_display
    check("introspected NUMERIC(10, 2)", disp == "NUMERIC(10, 2)", f"got {disp!r}")

    redo = ops_for(SchemaDiffer.diff([schema], snap), "test_mc_m3")
    check(
        "idempotent NUMERIC(10, 2)", not redo, f"got {[o.description() for o in redo]}"
    )


# ── M4: column rename preserves data ───────────────────────────────────────


async def test_m4_rename(db):
    print("\n=== M4: column rename (no data loss) ===")
    await cleanup(db, ["test_mc_m4"])

    class M4V1(Model):
        class Meta:
            table = "test_mc_m4"

        id: int = Field(primary_key=True, auto=True)
        label: str = Field(default="")

    await apply_ops(db, SchemaDiffer.diff([ModelExtractor.extract(M4V1)], EMPTY))
    await db.execute("INSERT INTO test_mc_m4 (label) VALUES ($1)", "keepme")

    class M4V2(Model):
        class Meta:
            table = "test_mc_m4"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(default="")  # renamed from label

    snap = await DatabaseIntrospector.introspect(db)
    ops = ops_for(SchemaDiffer.diff([ModelExtractor.extract(M4V2)], snap), "test_mc_m4")
    check("emits RenameColumn", any(isinstance(o, RenameColumn) for o in ops))
    check("does NOT emit DropColumn", not any(isinstance(o, DropColumn) for o in ops))
    check("does NOT emit AddColumn", not any(isinstance(o, AddColumn) for o in ops))

    await apply_ops(db, ops)
    snap2 = await DatabaseIntrospector.introspect(db)
    cols = snap2.tables["test_mc_m4"].columns
    check("title column exists", "title" in cols)
    check("label column gone", "label" not in cols)
    row = await db.query("SELECT title FROM test_mc_m4")
    check(
        "data preserved across rename",
        row and row[0]["title"] == "keepme",
        f"got {row}",
    )


# ── L1: parse_migration flushes on ';' + inline comment ────────────────────


async def test_l1_parse_inline_comment(db):
    print("\n=== L1: statement terminator with trailing comment ===")

    # Pure-function sanity for the helper.
    check(
        "code-before-comment strips trailing --",
        _line_code_before_comment("INSERT INTO t VALUES (1); -- note").rstrip()
        == "INSERT INTO t VALUES (1);",
    )
    check(
        "-- inside a string literal is NOT a comment",
        _line_code_before_comment("SELECT 'a--b';").rstrip() == "SELECT 'a--b';",
    )

    with tempfile.TemporaryDirectory() as tmp:
        fm = MigrationFileManager(tmp)
        fm.ensure_dir()
        path = fm.dir / "0001_x.sql"
        path.write_text(
            "-- UP\n"
            "INSERT INTO foo VALUES (1); -- first row\n"
            "INSERT INTO foo VALUES (2);\n"
            "\n"
            "-- DOWN\n"
            "DELETE FROM foo;\n"
        )
        up, down = fm.parse_migration(path)
        check("two UP statements (not merged by comment)", len(up) == 2, f"got {up}")
        check(
            "first stmt did not swallow the second",
            "(2)" not in up[0],
            f"got {up[0]!r}",
        )
        check("one DOWN statement", len(down) == 1, f"got {down}")


# ── extra pure-unit coverage for the introspection normalizer ──────────────


async def test_normalizer_units(db):
    print("\n=== Normalizer units ===")
    check(
        "vector typmod → VECTOR(8)",
        _normalize_pg_type("vector", None, 8) == "VECTOR(8)",
    )
    check(
        "vector no typmod → VECTOR", _normalize_pg_type("vector", None, -1) == "VECTOR"
    )
    # numeric atttypmod: ((precision << 16) | scale) + 4
    tm = ((10 << 16) | 2) + 4
    check(
        "numeric typmod → NUMERIC(10, 2)",
        _normalize_pg_type("numeric", None, tm) == "NUMERIC(10, 2)",
    )
    check("bare VECTOR ≡ VECTOR(8)", _types_equivalent("VECTOR", "VECTOR(8)"))
    check("VECTOR(8) ≠ VECTOR(16)", not _types_equivalent("VECTOR(8)", "VECTOR(16)"))


async def main():
    global passed, failed
    db = await setup_db()
    tables = [
        "test_mc_h1",
        "test_mc_h2",
        "test_mc_h3",
        "test_mc_h4_a",
        "test_mc_h4_b",
        "test_mc_h4_parent",
        "test_mc_h4_child",
        "test_mc_m1_books",
        "test_mc_m1_authors",
        "test_mc_m2",
        "test_mc_m3",
        "test_mc_m4",
    ]
    try:
        await test_h1_db_default(db)
        await test_h2_initial_indexes(db)
        await test_h3_vector_idempotent(db)
        await test_h4_mutual_fk(db)
        await test_h4_forward_ref(db)
        await test_m1_single_fk(db)
        await test_m2_composite(db)
        await test_m3_numeric(db)
        await test_m4_rename(db)
        await test_l1_parse_inline_comment(db)
        await test_normalizer_units(db)
    finally:
        await cleanup(db, tables)
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
