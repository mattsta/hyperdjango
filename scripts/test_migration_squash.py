#!/usr/bin/env python3
"""Test migration squashing, data migrations, and SQL generation.

Tests:
1. RunPython operation (data migration forward/reverse)
2. Migration squashing (compress N migrations into 1 + snapshot)
3. SQL generation mode (offline --sql without DB connection)
4. Squash preserves schema state
5. Squash updates hyper_migrations table
6. generate_sql produces valid SQL script

Run: uv run hyper-test migration_squash
Requires: PostgreSQL running, DATABASE_URL or default hyperdjango_test
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import tempfile

from hyperdjango.database import Database, set_db
from hyperdjango.migrations import (
    AddColumn,
    CreateTable,
    DatabaseIntrospector,
    MigrationEngine,
    MigrationFileManager,
    MigrationStateManager,
    ModelColumn,
    RunPython,
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


async def test_run_python():
    """Test RunPython data migration operation."""
    print("\n=== RunPython Operation ===")

    log = []

    async def forward(db):
        log.append("forward")

    async def backward(db):
        log.append("backward")

    op = RunPython(
        forward_func=forward, reverse_func=backward, _description="Populate slugs"
    )

    check("description", op.description() == "Populate slugs")
    check("up_sql is comment", "Python code" in op.up_sql())
    check("down_sql is comment", "Python code" in op.down_sql())

    # Execute forward
    await op.apply(None)
    check("forward executed", "forward" in log)

    # Execute reverse
    await op.revert(None)
    check("reverse executed", "backward" in log)


async def test_run_python_with_db(db):
    """Test RunPython with actual database operations."""
    print("\n=== RunPython with DB ===")

    await db.execute(
        "CREATE TABLE IF NOT EXISTS test_sq_slugs "
        "(id SERIAL PRIMARY KEY, name TEXT, slug TEXT)"
    )
    await db.execute("DELETE FROM test_sq_slugs")
    await db.execute("INSERT INTO test_sq_slugs (name) VALUES ($1)", "Hello World")
    await db.execute("INSERT INTO test_sq_slugs (name) VALUES ($1)", "Foo Bar")

    async def populate_slugs(db):
        rows = await db.query("SELECT id, name FROM test_sq_slugs")
        for row in rows:
            slug = row["name"].lower().replace(" ", "-")
            await db.execute(
                "UPDATE test_sq_slugs SET slug = $1 WHERE id = $2",
                slug,
                row["id"],
            )

    async def clear_slugs(db):
        await db.execute("UPDATE test_sq_slugs SET slug = NULL")

    op = RunPython(forward_func=populate_slugs, reverse_func=clear_slugs)

    # Forward
    await op.apply(db)
    rows = await db.query("SELECT name, slug FROM test_sq_slugs ORDER BY id")
    check("forward populated slugs", len(rows) == 2)
    if rows:
        check("slug 1 correct", rows[0]["slug"] == "hello-world")
        check("slug 2 correct", rows[1]["slug"] == "foo-bar")

    # Reverse
    await op.revert(db)
    rows = await db.query("SELECT slug FROM test_sq_slugs")
    check("reverse cleared slugs", all(r["slug"] is None for r in rows))

    await db.execute("DROP TABLE IF EXISTS test_sq_slugs CASCADE")


async def test_generate_sql():
    """Test offline SQL generation mode."""
    print("\n=== SQL Generation (offline) ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MigrationEngine(tmpdir)
        fm = MigrationFileManager(tmpdir)

        # Write some migrations
        fm.write_migration(
            1,
            "create_users",
            [
                CreateTable(
                    table="users",
                    columns=[
                        ModelColumn(
                            "id", "INTEGER", False, True, True, False, False, None, None
                        ),
                        ModelColumn(
                            "name",
                            "TEXT",
                            False,
                            False,
                            False,
                            False,
                            False,
                            None,
                            None,
                        ),
                    ],
                ),
            ],
        )
        fm.write_migration(
            2,
            "add_email",
            [
                AddColumn(table="users", column="email", type_sql="TEXT"),
            ],
        )

        # Generate SQL
        sql = engine.generate_sql()
        check("sql is string", isinstance(sql, str))
        check("sql has CREATE TABLE", "CREATE TABLE" in sql)
        check("sql has ADD COLUMN", "ADD COLUMN" in sql)
        check(
            "sql has INSERT into hyper_migrations",
            "INSERT INTO hyper_migrations" in sql,
        )
        check("sql has migration names", "create_users" in sql and "add_email" in sql)

        # Generate SQL with target
        sql_partial = engine.generate_sql(target="0001_create_users")
        check("partial sql has CREATE TABLE", "CREATE TABLE" in sql_partial)
        check("partial sql does NOT have ADD COLUMN", "ADD COLUMN" not in sql_partial)


async def test_squash(db):
    """Test migration squashing."""
    print("\n=== Migration Squashing ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = MigrationEngine(tmpdir)

        # Clean up test tables and migration state
        for t in ["test_sq_products", "test_sq_categories"]:
            await db.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        await MigrationStateManager.ensure_table(db)
        await db.execute("DELETE FROM hyper_migrations")

        # Define models
        class SqProduct(Model):
            class Meta:
                table = "test_sq_products"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field(max_length=100)
            price: float = Field(default=0.0)

        class SqCategory(Model):
            class Meta:
                table = "test_sq_categories"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field(max_length=50)

        # Create 3 migrations manually
        fm = MigrationFileManager(tmpdir)
        fm.write_migration(
            1,
            "create_products",
            [
                CreateTable(
                    table="test_sq_products",
                    columns=[
                        ModelColumn(
                            "id", "INTEGER", False, True, True, False, False, None, None
                        ),
                        ModelColumn(
                            "name",
                            "VARCHAR(100)",
                            False,
                            False,
                            False,
                            False,
                            False,
                            None,
                            None,
                        ),
                    ],
                ),
            ],
        )
        fm.write_migration(
            2,
            "add_price",
            [
                AddColumn(
                    table="test_sq_products",
                    column="price",
                    type_sql="DOUBLE PRECISION",
                    default_sql="0.0",
                ),
            ],
        )
        fm.write_migration(
            3,
            "create_categories",
            [
                CreateTable(
                    table="test_sq_categories",
                    columns=[
                        ModelColumn(
                            "id", "INTEGER", False, True, True, False, False, None, None
                        ),
                        ModelColumn(
                            "name",
                            "VARCHAR(50)",
                            False,
                            False,
                            False,
                            False,
                            False,
                            None,
                            None,
                        ),
                    ],
                ),
            ],
        )

        # Apply all 3
        applied = await engine.migrate(db)
        check("3 migrations applied", len(applied) == 3, f"got {len(applied)}")

        # Verify tables exist
        snapshot = await DatabaseIntrospector.introspect(db)
        check("products table exists", "test_sq_products" in snapshot.tables)
        check("categories table exists", "test_sq_categories" in snapshot.tables)

        # Count migration files before squash
        files_before = len(fm.list_migrations())
        check(
            "3 migration files before squash", files_before == 3, f"got {files_before}"
        )

        # Squash!
        result = await engine.squash(db)
        check("squash returns count", result["squashed_count"] == 3, f"got {result}")
        check("squash has snapshot path", "snapshot" in result.get("snapshot_path", ""))

        # Check migration state
        applied_after = await MigrationStateManager.get_applied(db)
        check(
            "squashed migration in state",
            any("squashed" in name for name in applied_after),
            f"applied: {applied_after}",
        )
        check(
            "old migrations removed from state",
            not any("add_price" in name for name in applied_after),
            f"applied: {applied_after}",
        )

        # Tables still exist (squash doesn't touch data)
        snapshot_after = await DatabaseIntrospector.introspect(db)
        check(
            "products still exists after squash",
            "test_sq_products" in snapshot_after.tables,
        )
        check(
            "categories still exists after squash",
            "test_sq_categories" in snapshot_after.tables,
        )

        # Snapshot file exists
        snap = fm.latest_snapshot()
        check("snapshot saved", snap is not None)

        # Cleanup
        for t in ["test_sq_products", "test_sq_categories"]:
            await db.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


async def main():
    global passed, failed

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    try:
        await test_run_python()
        await test_run_python_with_db(db)
        await test_generate_sql()
        await test_squash(db)
    finally:
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All migration squash tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
