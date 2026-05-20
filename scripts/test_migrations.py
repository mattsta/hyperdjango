#!/usr/bin/env python3
"""Test the HyperUltimateMigrationSystem end-to-end.

Tests:
1. Database introspection (read live schema from pg_catalog)
2. Model schema extraction (read Model._meta definitions)
3. Schema diffing (compare models vs live DB, produce operations)
4. Operation SQL generation (forward + reverse DDL)
5. Migration file management (write/read/parse SQL files)
6. Migration state tracking (hyper_migrations table)
7. Full migration lifecycle (makemigrations → migrate → rollback)
8. Schema verification (detect drift)
9. Schema snapshots (save/load checkpoints)
10. Deployment safety analysis (flag dangerous operations)
11. Type equivalence (INTEGER ↔ int4, etc.)
12. M2M junction table support
13. FK constraint detection
14. Index management

Run: uv run hyper-test migrations
Requires: PostgreSQL running, DATABASE_URL or default hyperdjango_test
"""

# hyper-test: db_isolated

import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path

from hyperdjango.database import Database, set_db
from hyperdjango.migrations import (
    AddColumn,
    AddConstraint,
    AlterColumnType,
    CreateIndex,
    CreateTable,
    DatabaseIntrospector,
    DbTable,
    DropTable,
    MigrationEngine,
    MigrationFileManager,
    MigrationStateManager,
    ModelColumn,
    ModelExtractor,
    RunSQL,
    SafetyAnalyzer,
    SchemaDiffer,
    SchemaSnapshot,
    _normalize_pg_type,
    _sql_literal,
    _types_equivalent,
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


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    return db


async def cleanup_tables(db, tables):
    for t in tables:
        with contextlib.suppress(Exception):
            await db.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


async def test_type_equivalence():
    """Test type normalization and equivalence."""
    print("\n=== Type Equivalence ===")

    check("int4 → INTEGER", _normalize_pg_type("int4") == "INTEGER")
    check("int8 → BIGINT", _normalize_pg_type("int8") == "BIGINT")
    check(
        "float8 → DOUBLE PRECISION", _normalize_pg_type("float8") == "DOUBLE PRECISION"
    )
    check("bool → BOOLEAN", _normalize_pg_type("bool") == "BOOLEAN")
    check("varchar(100)", _normalize_pg_type("varchar", 100) == "VARCHAR(100)")
    check("timestamptz", _normalize_pg_type("timestamptz") == "TIMESTAMPTZ")

    check("INTEGER ≡ INT4", _types_equivalent("INTEGER", "INT4"))
    check("INTEGER ≡ INTEGER", _types_equivalent("INTEGER", "INTEGER"))
    check("BOOLEAN ≡ BOOL", _types_equivalent("BOOLEAN", "BOOL"))
    check("SERIAL ≡ INTEGER", _types_equivalent("SERIAL", "INTEGER"))
    check("TEXT ≠ INTEGER", not _types_equivalent("TEXT", "INTEGER"))
    check(
        "VARCHAR(100) ≡ VARCHAR(100)", _types_equivalent("VARCHAR(100)", "VARCHAR(100)")
    )
    check(
        "VARCHAR(100) ≠ VARCHAR(200)",
        not _types_equivalent("VARCHAR(100)", "VARCHAR(200)"),
    )


async def test_sql_literal():
    """Test SQL literal conversion."""
    print("\n=== SQL Literals ===")

    check("bool True", _sql_literal(True) == "TRUE")
    check("bool False", _sql_literal(False) == "FALSE")
    check("int", _sql_literal(42) == "42")
    check("float", _sql_literal(3.14) == "3.14")
    check("str", _sql_literal("hello") == "'hello'")
    check("str with quote", _sql_literal("it's") == "'it''s'")
    check("None", _sql_literal(None) == "NULL")


async def test_operation_sql():
    """Test operation forward/reverse SQL generation."""
    print("\n=== Operation SQL ===")

    # CreateTable
    op = CreateTable(
        table="test_items",
        columns=[
            ModelColumn("id", "INTEGER", False, True, True, False, False, None, None),
            ModelColumn(
                "name", "VARCHAR(100)", False, False, False, False, False, None, None
            ),
            ModelColumn(
                "price",
                "DOUBLE PRECISION",
                True,
                False,
                False,
                False,
                False,
                "'0.0'",
                None,
            ),
        ],
    )
    up = op.up_sql()
    check("CreateTable up has CREATE TABLE", "CREATE TABLE" in up)
    check("CreateTable up has SERIAL", "SERIAL" in up)
    check("CreateTable up has VARCHAR(100)", "VARCHAR(100)" in up)
    check("CreateTable down has DROP TABLE", "DROP TABLE" in op.down_sql())

    # AddColumn
    op = AddColumn(table="test_items", column="weight", type_sql="DOUBLE PRECISION")
    # AddColumn (identifiers are now quoted)
    check("AddColumn up", 'ADD COLUMN "weight"' in op.up_sql())
    check("AddColumn down", "DROP COLUMN" in op.down_sql())

    # AddColumn NOT NULL with safety warning
    op = AddColumn(table="test_items", column="code", type_sql="TEXT", nullable=False)
    warnings = op.safety_warnings(None)
    check("AddColumn NOT NULL warns", len(warnings) > 0)
    check(
        "AddColumn NOT NULL mentions table rewrite",
        any("NOT NULL" in w for w in warnings),
    )

    # AlterColumnType (quoted identifiers)
    op = AlterColumnType(
        table="test_items", column="price", old_type="REAL", new_type="DOUBLE PRECISION"
    )
    check(
        "AlterColumnType up",
        'ALTER COLUMN "price" TYPE DOUBLE PRECISION' in op.up_sql(),
    )
    check("AlterColumnType down", "TYPE REAL" in op.down_sql())

    # CreateIndex (quoted identifiers)
    op = CreateIndex(table="test_items", name="idx_items_name", columns=["name"])
    check("CreateIndex up", '"idx_items_name" ON "test_items"' in op.up_sql())
    check("CreateIndex down", "DROP INDEX" in op.down_sql())

    # CreateIndex CONCURRENTLY safety
    op_no_conc = CreateIndex(
        table="test_items", name="idx_big", columns=["name"], concurrently=False
    )
    warnings = op_no_conc.safety_warnings(500_000)
    check("CreateIndex non-concurrent warns on large table", len(warnings) > 0)

    op_conc = CreateIndex(
        table="test_items", name="idx_big", columns=["name"], concurrently=True
    )
    check("CreateIndex CONCURRENTLY", "CONCURRENTLY" in op_conc.up_sql())

    # RunSQL
    op = RunSQL(
        forward="INSERT INTO test VALUES (1)", reverse="DELETE FROM test WHERE id = 1"
    )
    check("RunSQL up", op.up_sql() == "INSERT INTO test VALUES (1)")
    check("RunSQL down", op.down_sql() == "DELETE FROM test WHERE id = 1")

    # AddConstraint / DropConstraint (quoted identifiers)
    op = AddConstraint(
        table="test_items",
        name="fk_items_user",
        sql_clause="FOREIGN KEY (user_id) REFERENCES users(id)",
    )
    check("AddConstraint up", '"fk_items_user"' in op.up_sql())
    check("AddConstraint down", "DROP CONSTRAINT" in op.down_sql())


async def test_introspection(db):
    """Test live database introspection."""
    print("\n=== Database Introspection ===")

    # Create test tables — match model definitions (NOT NULL where model expects it)
    await db.execute(
        "CREATE TABLE IF NOT EXISTS test_mig_authors ("
        "  id SERIAL PRIMARY KEY,"
        "  name VARCHAR(100) NOT NULL,"
        "  email TEXT NOT NULL UNIQUE,"
        "  active BOOLEAN NOT NULL DEFAULT TRUE"
        ")"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS test_mig_books ("
        "  id SERIAL PRIMARY KEY,"
        "  title VARCHAR(200) NOT NULL,"
        "  author_id INTEGER NOT NULL REFERENCES test_mig_authors(id) ON DELETE CASCADE,"
        "  pages INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_books_title ON test_mig_books (title)"
    )

    snapshot = await DatabaseIntrospector.introspect(db)

    check("introspect returns SchemaSnapshot", isinstance(snapshot, SchemaSnapshot))
    check("found test_mig_authors", "test_mig_authors" in snapshot.tables)
    check("found test_mig_books", "test_mig_books" in snapshot.tables)

    authors = snapshot.tables["test_mig_authors"]
    check("authors has id column", "id" in authors.columns)
    check("authors has name column", "name" in authors.columns)
    check("authors has email column", "email" in authors.columns)
    check("authors id is serial", authors.columns["id"].is_serial)
    check("authors name is NOT NULL", not authors.columns["name"].nullable)
    check("authors email is NOT NULL", not authors.columns["email"].nullable)
    check("authors active has default", authors.columns["active"].has_default)
    check(
        "authors name type is VARCHAR(100)",
        authors.columns["name"].type_display == "VARCHAR(100)",
        f"got {authors.columns['name'].type_display}",
    )

    books = snapshot.tables["test_mig_books"]
    check("books has author_id", "author_id" in books.columns)

    # Check FK constraint
    fk_constraints = books.get_fk_constraints()
    check(
        "books has FK constraint",
        len(fk_constraints) > 0,
        f"got {len(fk_constraints)} constraints",
    )
    if fk_constraints:
        fk = fk_constraints[0]
        check(
            "FK references test_mig_authors",
            fk.fk_table == "test_mig_authors",
            f"got {fk.fk_table}",
        )

    # Check index — pg.zig may return indkey differently
    all_idx_names = [i.name for i in books.indexes]
    check(
        "books has title index",
        any("title" in n for n in all_idx_names),
        f"index names: {all_idx_names}",
    )

    # Check PK
    pk_cols = authors.get_pk_columns()
    check("authors PK is id", pk_cols == ["id"], f"got {pk_cols}")

    # Snapshot checksum
    checksum = snapshot.compute_checksum()
    check("checksum is 16 chars", len(checksum) == 16)

    # Snapshot serialization round-trip
    data = snapshot.to_dict()
    restored = SchemaSnapshot.from_dict(data)
    check(
        "snapshot round-trip tables match",
        set(restored.tables.keys()) == set(snapshot.tables.keys()),
    )
    check(
        "snapshot round-trip columns match",
        set(restored.tables["test_mig_authors"].columns.keys())
        == set(snapshot.tables["test_mig_authors"].columns.keys()),
    )


async def test_model_extraction():
    """Test extracting schema from Model classes."""
    print("\n=== Model Schema Extraction ===")

    # Define test models
    class MigAuthor(Model):
        class Meta:
            table = "test_mig_authors"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)
        email: str = Field(unique=True)
        active: bool = Field(default=True)

    schema = ModelExtractor.extract(MigAuthor)
    check("table name", schema.table == "test_mig_authors")
    check("has id column", "id" in schema.columns)
    check("has name column", "name" in schema.columns)
    check("id is PK", schema.columns["id"].is_pk)
    check("id is auto", schema.columns["id"].is_auto)
    check(
        "name type is VARCHAR(100)",
        schema.columns["name"].type_sql == "VARCHAR(100)",
        f"got {schema.columns['name'].type_sql}",
    )
    check("email is unique", schema.columns["email"].is_unique)
    check(
        "active default is TRUE",
        schema.columns["active"].default_sql == "TRUE",
        f"got {schema.columns['active'].default_sql}",
    )


async def test_schema_diff(db):
    """Test diffing models against live DB."""
    print("\n=== Schema Diffing ===")

    # Define models that match existing tables (from test_introspection)
    class DiffAuthor(Model):
        class Meta:
            table = "test_mig_authors"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)
        email: str = Field(unique=True)
        active: bool = Field(default=True)

    class DiffBook(Model):
        class Meta:
            table = "test_mig_books"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(max_length=200)
        author_id: int = Field(foreign_key=DiffAuthor)
        pages: int = Field(default=0)

    # Introspect live DB
    snapshot = await DatabaseIntrospector.introspect(db)

    # Extract model schemas
    author_schema = ModelExtractor.extract(DiffAuthor)
    book_schema = ModelExtractor.extract(DiffBook)

    # Diff — should be empty since models match DB
    ops = SchemaDiffer.diff([author_schema, book_schema], snapshot)

    # Filter to only ops on our test tables
    test_ops = [
        op for op in ops if hasattr(op, "table") and op.table.startswith("test_mig_")
    ]
    check(
        "no diff when models match DB",
        len(test_ops) == 0,
        f"got {len(test_ops)} ops: {[op.description() for op in test_ops]}",
    )

    # Now add a new column to the model that doesn't exist in DB
    class DiffAuthorV2(Model):
        class Meta:
            table = "test_mig_authors"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)
        email: str = Field(unique=True)
        active: bool = Field(default=True)
        bio: str = Field(default="")  # NEW column

    author_v2_schema = ModelExtractor.extract(DiffAuthorV2)
    ops = SchemaDiffer.diff([author_v2_schema, book_schema], snapshot)
    add_ops = [op for op in ops if isinstance(op, AddColumn) and op.column == "bio"]
    check(
        "detects new column 'bio'",
        len(add_ops) == 1,
        f"got {len(add_ops)} AddColumn ops",
    )
    if add_ops:
        check(
            "bio AddColumn type is TEXT",
            add_ops[0].type_sql == "TEXT",
            f"got {add_ops[0].type_sql}",
        )

    # Test detecting a new table
    class DiffTag(Model):
        class Meta:
            table = "test_mig_tags_new"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=50)

    tag_schema = ModelExtractor.extract(DiffTag)
    ops = SchemaDiffer.diff([author_schema, book_schema, tag_schema], snapshot)
    create_ops = [
        op
        for op in ops
        if isinstance(op, CreateTable) and op.table == "test_mig_tags_new"
    ]
    check("detects new table", len(create_ops) == 1)


async def test_system_tables_never_dropped():
    """makemigrations must NEVER emit DROP for framework `hyper_*` tables.

    Framework model modules are lazily imported, so their tables can be
    absent from the model registry on any given run. If the differ dropped
    tables that aren't in the registry, it would emit
    `DROP TABLE hyper_security_log CASCADE` (etc.) against LIVE data. All
    `hyper_*` tables are protected; genuinely-unmanaged tables still drop.
    """
    print("\n=== System Tables Never Dropped ===")

    # A snapshot with framework tables (none imported into the registry here)
    # plus one genuinely orphaned application table.
    framework_tables = [
        "hyper_security_log",
        "hyper_meter_events",  # hyper_meter_* — covered by prefix, not the set
        "hyper_rate_limit_rules",
        "hyper_tenants",
        "hyper_rbac_audit",
        "hyper_object_permissions",
        "hyper_field_permissions",
        "hyper_status_events",
    ]
    snapshot = SchemaSnapshot(
        tables={t: DbTable(name=t) for t in [*framework_tables, "legacy_junk"]},
        timestamp="2026-07-18T00:00:00",
    )

    # Diff against NO models (simulating unimported framework modules).
    ops = SchemaDiffer.diff([], snapshot)
    dropped = {op.table for op in ops if isinstance(op, DropTable)}

    for t in framework_tables:
        check(f"no DROP for framework table {t}", t not in dropped)

    # Sanity: a genuinely unmanaged, non-hyper_ table is still dropped.
    check("orphaned app table still dropped", "legacy_junk" in dropped)


async def test_parse_dollar_quoted():
    """parse_migration must not split on `;` inside dollar-quoted bodies."""
    print("\n=== Dollar-Quoted Statement Parsing ===")

    migration = (
        "-- UP\n"
        "CREATE TABLE widgets (id SERIAL PRIMARY KEY, n INT);\n"
        "DO $$\n"
        "BEGIN\n"
        "  INSERT INTO widgets(n) VALUES (1);\n"
        "  INSERT INTO widgets(n) VALUES (2);\n"
        "END\n"
        "$$;\n"
        "CREATE INDEX idx_widgets_n ON widgets(n);\n"
        "\n"
        "-- DOWN\n"
        "DROP TABLE widgets;\n"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        fm = MigrationFileManager(tmpdir)
        path = Path(tmpdir) / "0001_dollar.sql"
        path.write_text(migration)

        up, down = fm.parse_migration(path)

        # 3 UP statements: CREATE TABLE, the whole DO $$...$$ block, CREATE INDEX
        check("3 UP statements parsed", len(up) == 3, f"got {len(up)}: {up}")
        do_stmts = [s for s in up if s.startswith("DO $$")]
        check("DO block kept as ONE statement", len(do_stmts) == 1)
        if do_stmts:
            check(
                "DO block contains both INSERTs",
                do_stmts[0].count("INSERT INTO widgets") == 2,
                do_stmts[0],
            )
            check("DO block not split on inner ;", "$$;" in do_stmts[0])
        check("1 DOWN statement parsed", len(down) == 1, f"got {down}")


async def test_migration_file_management():
    """Test writing, reading, and parsing migration files."""
    print("\n=== Migration File Management ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        fm = MigrationFileManager(tmpdir)

        check("next number starts at 1", fm.next_number() == 1)

        # Write a migration
        ops = [
            CreateTable(
                table="users",
                columns=[
                    ModelColumn(
                        "id", "INTEGER", False, True, True, False, False, None, None
                    ),
                    ModelColumn(
                        "name", "TEXT", False, False, False, False, False, None, None
                    ),
                ],
            ),
            CreateIndex(table="users", name="idx_users_name", columns=["name"]),
        ]
        filepath = fm.write_migration(1, "initial", ops)
        check("migration file created", filepath.exists())
        check("migration filename has 0001", "0001" in filepath.name)

        # Parse it back
        up_stmts, down_stmts = fm.parse_migration(filepath)
        check(
            "parsed 2 up statements",
            len(up_stmts) == 2,
            f"got {len(up_stmts)}: {up_stmts}",
        )
        check(
            "parsed 2 down statements",
            len(down_stmts) == 2,
            f"got {len(down_stmts)}: {down_stmts}",
        )
        check("up[0] is CREATE TABLE", "CREATE TABLE" in up_stmts[0])
        check("up[1] is CREATE INDEX", "CREATE INDEX" in up_stmts[1])
        check("down[0] is DROP INDEX", "DROP INDEX" in down_stmts[0])
        check("down[1] is DROP TABLE", "DROP TABLE" in down_stmts[1])

        # Next number
        check("next number is 2", fm.next_number() == 2)

        # List migrations
        migs = fm.list_migrations()
        check("list returns 1 migration", len(migs) == 1)

        # Snapshot management
        snapshot = SchemaSnapshot(tables={}, timestamp="2026-03-22T12:00:00")
        snap_path = fm.write_snapshot(snapshot, "0001")
        check("snapshot file created", snap_path.exists())

        loaded = fm.load_snapshot("0001")
        check("snapshot loaded", loaded is not None)
        check("snapshot timestamp preserved", loaded.timestamp == "2026-03-22T12:00:00")


async def test_migration_state(db):
    """Test migration state tracking in hyper_migrations table."""
    print("\n=== Migration State Tracking ===")

    msm = MigrationStateManager

    # Ensure table
    await msm.ensure_table(db)

    # Clean up any existing test entries
    with contextlib.suppress(Exception):
        await db.execute("DELETE FROM hyper_migrations WHERE name LIKE 'test_state_%'")

    # Record applied
    await msm.record_applied(db, "test_state_0001_initial")
    applied = await msm.get_applied(db)
    check("recorded migration", "test_state_0001_initial" in applied)

    # Record another
    await msm.record_applied(db, "test_state_0002_add_email")
    applied = await msm.get_applied(db)
    check("two migrations applied", "test_state_0002_add_email" in applied)

    # Get ordered
    ordered = await msm.get_applied_ordered(db)
    test_ordered = [m for m in ordered if m["name"].startswith("test_state_")]
    check("ordered returns correct count", len(test_ordered) == 2)

    # Unapply
    await msm.record_unapplied(db, "test_state_0002_add_email")
    applied = await msm.get_applied(db)
    check("unapplied migration removed", "test_state_0002_add_email" not in applied)
    check("first migration still applied", "test_state_0001_initial" in applied)

    # Cleanup
    await db.execute("DELETE FROM hyper_migrations WHERE name LIKE 'test_state_%'")


async def test_full_lifecycle(db):
    """Test full makemigrations → migrate → verify → rollback lifecycle."""
    print("\n=== Full Migration Lifecycle ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MigrationEngine(tmpdir)

        # Clean up any existing test tables
        await cleanup_tables(
            db,
            [
                "test_lifecycle_articles",
                "test_lifecycle_categories",
                "test_lifecycle_articles_test_lifecycle_categories",
            ],
        )

        # Define models
        class LifecycleCategory(Model):
            class Meta:
                table = "test_lifecycle_categories"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field(max_length=100)

        class LifecycleArticle(Model):
            class Meta:
                table = "test_lifecycle_articles"

            id: int = Field(primary_key=True, auto=True)
            title: str = Field(max_length=200)
            body: str = Field(default="")

        # 1. makemigrations
        result = await engine.makemigrations(db, name="initial")
        check(
            "makemigrations produces operations",
            len(result["operations"]) > 0,
            f"got {len(result['operations'])}",
        )
        check("makemigrations creates file", result["filepath"] is not None)

        # Filter to just our test operations
        test_ops = [
            op
            for op in result["operations"]
            if hasattr(op, "table") and "lifecycle" in op.table
        ]
        check(
            "has lifecycle table operations", len(test_ops) >= 2, f"got {len(test_ops)}"
        )

        # 2. migrate
        applied = await engine.migrate(db)
        check("migrate applies migrations", len(applied) > 0)

        # 3. Verify tables exist
        snapshot = await DatabaseIntrospector.introspect(db)
        check(
            "categories table created", "test_lifecycle_categories" in snapshot.tables
        )
        check("articles table created", "test_lifecycle_articles" in snapshot.tables)

        # 4. Verify columns
        if "test_lifecycle_articles" in snapshot.tables:
            arts = snapshot.tables["test_lifecycle_articles"]
            check("articles has title column", "title" in arts.columns)
            check("articles has body column", "body" in arts.columns)

        # 5. Verify — should match now
        verify_result = await engine.verify(db)
        lifecycle_drift = [d for d in verify_result["drift"] if "lifecycle" in d]
        check(
            "verify shows no drift for lifecycle tables",
            len(lifecycle_drift) == 0,
            f"drift: {lifecycle_drift}",
        )

        # 6. showmigrations
        show = await engine.showmigrations(db)
        check("showmigrations returns entries", len(show) > 0)
        check("migration is marked applied", show[0]["applied"])

        # 7. Rollback
        rolled_back = await engine.rollback(db)
        check("rollback returns names", len(rolled_back) > 0)

        # 8. Verify tables dropped
        snapshot_after = await DatabaseIntrospector.introspect(db)
        check(
            "categories table dropped after rollback",
            "test_lifecycle_categories" not in snapshot_after.tables,
        )
        check(
            "articles table dropped after rollback",
            "test_lifecycle_articles" not in snapshot_after.tables,
        )


async def test_snapshot(db):
    """Test schema snapshot save/load."""
    print("\n=== Schema Snapshots ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MigrationEngine(tmpdir)

        # Save snapshot
        filepath = await engine.snapshot(db)
        check("snapshot file created", filepath.exists())

        # Load it back
        fm = MigrationFileManager(tmpdir)
        loaded = fm.latest_snapshot()
        check("latest snapshot loaded", loaded is not None)
        check("snapshot has tables", len(loaded.tables) > 0)
        check("snapshot has checksum", loaded.checksum is not None)


async def test_safety_analysis(db):
    """Test deployment safety analysis."""
    print("\n=== Deployment Safety Analysis ===")

    ops = [
        AddColumn(table="large_table", column="email", type_sql="TEXT", nullable=False),
        CreateIndex(
            table="large_table", name="idx_email", columns=["email"], concurrently=False
        ),
        AlterColumnType(
            table="large_table", column="name", old_type="VARCHAR(100)", new_type="TEXT"
        ),
        DropTable(table="old_table"),
    ]

    reports = await SafetyAnalyzer.analyze(ops)
    check("safety analysis returns reports", len(reports) > 0)

    # Check specific warnings
    not_null_warns = [r for r in reports if "NOT NULL" in r["operation"]]
    check(
        "NOT NULL column flagged",
        len(not_null_warns) > 0
        or any("NOT NULL" in str(r["warnings"]) for r in reports),
    )

    drop_warns = [r for r in reports if "Drop" in r["operation"]]
    check("DROP TABLE flagged", len(drop_warns) > 0)


async def test_dry_run(db):
    """Test dry-run mode (show SQL without applying)."""
    print("\n=== Dry Run ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MigrationEngine(tmpdir)

        # Clean up
        await cleanup_tables(db, ["test_dryrun_items"])

        # Define model
        class DryrunItem(Model):
            class Meta:
                table = "test_dryrun_items"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field(max_length=50)

        result = await engine.makemigrations(db, name="dryrun", dry_run=True)
        test_ops = [
            op
            for op in result["operations"]
            if hasattr(op, "table") and "dryrun" in op.table
        ]
        check("dry run produces operations", len(test_ops) > 0)
        check("dry run produces SQL", len(result["sql"]) > 0)
        check("dry run does NOT write file", result["filepath"] is None)

        # Verify table was NOT created
        snapshot = await DatabaseIntrospector.introspect(db)
        check(
            "dry run did not create table", "test_dryrun_items" not in snapshot.tables
        )


async def test_concurrent_index_migration(db):
    """#129: a migration containing CREATE INDEX CONCURRENTLY must apply.

    The framework generates concurrent-index DDL and its safety analysis
    recommends it, but the apply loop used to wrap EVERY statement in a
    transaction — where CONCURRENTLY is illegal ('cannot run inside a transaction
    block'). Such migrations now auto-run non-transactionally.
    """
    print("\n=== Concurrent-index migration (non-atomic) ===")
    await cleanup_tables(db, ["test_concurrent_idx"])

    with tempfile.TemporaryDirectory() as tmpdir:
        fm = MigrationFileManager(tmpdir)
        engine = MigrationEngine(tmpdir)
        path = Path(tmpdir) / "0001_concurrent.sql"
        path.write_text(
            "-- UP\n"
            "CREATE TABLE test_concurrent_idx (id INTEGER PRIMARY KEY, name TEXT);\n"
            "CREATE INDEX CONCURRENTLY idx_tci_name ON test_concurrent_idx (name);\n"
            "-- DOWN\n"
            "DROP INDEX IF EXISTS idx_tci_name;\n"
            "DROP TABLE IF EXISTS test_concurrent_idx;\n"
        )

        # Detection: CONCURRENTLY → non-atomic.
        up, _ = fm.parse_migration(path)
        check(
            "CONCURRENTLY migration detected non-atomic",
            not fm.migration_is_atomic(path, up),
        )

        # Apply — must SUCCEED (previously raised the transaction-block error).
        try:
            applied = await engine.migrate(db)
            check(
                "concurrent-index migration applied",
                "0001_concurrent" in applied,
                f"applied={applied}",
            )
        except Exception as e:
            check("concurrent-index migration applied", False, f"raised: {e}")

        # Index really exists.
        rows = await db.query(
            "SELECT 1 FROM pg_indexes WHERE indexname = $1", "idx_tci_name"
        )
        check("concurrent index exists in catalog", len(rows) == 1, f"rows={rows}")

    await cleanup_tables(db, ["test_concurrent_idx"])


async def test_migration_atomicity_detection():
    """#129: migration_is_atomic — CONCURRENTLY + explicit marker → non-atomic."""
    print("\n=== Migration atomicity detection ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        fm = MigrationFileManager(tmpdir)
        p = Path(tmpdir)

        normal = p / "a.sql"
        normal.write_text("-- UP\nCREATE TABLE t (id int);\n-- DOWN\nDROP TABLE t;\n")
        check("normal migration is atomic", fm.migration_is_atomic(normal))

        conc = p / "b.sql"
        conc.write_text(
            "-- UP\nCREATE INDEX CONCURRENTLY i ON t (c);\n-- DOWN\nDROP INDEX i;\n"
        )
        check("CONCURRENTLY → non-atomic", not fm.migration_is_atomic(conc))

        marked = p / "c.sql"
        marked.write_text("-- hyper:atomic = false\n-- UP\nVACUUM;\n-- DOWN\n")
        check("explicit marker → non-atomic", not fm.migration_is_atomic(marked))


async def main():
    global passed, failed

    db = await setup_db()

    try:
        await test_migration_atomicity_detection()
        await test_concurrent_index_migration(db)
        await test_type_equivalence()
        await test_sql_literal()
        await test_operation_sql()
        await test_introspection(db)
        await test_model_extraction()
        await test_schema_diff(db)
        await test_system_tables_never_dropped()
        await test_parse_dollar_quoted()
        await test_migration_file_management()
        await test_migration_state(db)
        await test_full_lifecycle(db)
        await test_snapshot(db)
        await test_safety_analysis(db)
        await test_dry_run(db)
    finally:
        # Cleanup test tables
        await cleanup_tables(
            db,
            [
                "test_mig_books",
                "test_mig_authors",
                "test_lifecycle_articles",
                "test_lifecycle_categories",
                "test_lifecycle_articles_test_lifecycle_categories",
                "test_dryrun_items",
                "test_mig_tags_new",
            ],
        )
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All migration tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
