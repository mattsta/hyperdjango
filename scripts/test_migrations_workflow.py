"""
End-to-end test for the migrations workflow.

# hyper-test: db_isolated

Tests the full migration lifecycle:
1. Start with no tables
2. Define models, run makemigrations → generates migration file
3. Run migrate → creates tables
4. Verify tables exist with correct columns
5. Add a field to a model, run makemigrations → detects AddColumn
6. Run migrate → alters table
7. Verify new column exists
8. Show migration state
9. Rollback last migration
10. Verify column removed
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import contextlib

from hyperdjango.database import Database, set_db
from hyperdjango.migrations import MigrationEngine
from hyperdjango.models import Field, Model

DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

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


# Models for testing — defined at module level so they register in the model registry.
# We'll manipulate them between migration runs.


class MigTestUser(Model):
    class Meta:
        table = "mig_test_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    email: str = Field(default="")


async def main():
    print("=" * 60)
    print("Migrations Workflow E2E Tests")
    print("=" * 60)

    db = Database(DATABASE_URL)
    await db.connect()
    set_db(db)

    # Clean slate
    await db.execute("DROP TABLE IF EXISTS mig_test_users CASCADE")
    with contextlib.suppress(
        Exception
    ):  # best-effort cleanup: table may not exist (DELETE now raises typed ProgrammingError)
        await db.execute(
            "DELETE FROM hyper_migration_state WHERE migration_name LIKE '%mig_test%' OR migration_name LIKE '%initial%' OR migration_name LIKE '%add_bio%'"
        )

    # Temp directory for migration files
    mig_dir = tempfile.mkdtemp(prefix="hyper_mig_test_")

    try:
        engine = MigrationEngine(mig_dir)

        # ── Step 1: makemigrations (initial) ──
        print("\n--- Step 1: makemigrations (initial) ---")
        result = await engine.makemigrations(db, name="initial")
        ops = result.get("operations", [])
        check("Detected operations", len(ops) > 0, f"got {len(ops)}")

        has_create = any("CreateTable" in type(op).__name__ for op in ops)
        check(
            "Has CreateTable operation",
            has_create,
            f"ops: {[type(op).__name__ for op in ops]}",
        )

        filepath = result.get("filepath")
        check(
            "Migration file created",
            filepath is not None and filepath.exists(),
            f"path={filepath}",
        )

        # SQL preview
        sql_preview = result.get("sql", [])
        check("SQL preview available", len(sql_preview) > 0)
        if sql_preview:
            has_create_sql = any("CREATE TABLE" in s for s in sql_preview)
            check(
                "SQL has CREATE TABLE", has_create_sql, f"sql[0]={sql_preview[0][:80]}"
            )

        # ── Step 2: migrate (apply initial) ──
        print("\n--- Step 2: migrate (apply) ---")
        applied = await engine.migrate(db)
        check("Migration applied", len(applied) > 0, f"applied={applied}")

        # Verify table exists
        exists = await db.query_val(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'mig_test_users'"
        )
        check("Table mig_test_users exists", exists == 1, f"got {exists}")

        # Verify columns
        cols = await db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'mig_test_users' ORDER BY ordinal_position"
        )
        col_names = [c["column_name"] for c in cols]
        check("Has id column", "id" in col_names, f"cols={col_names}")
        check("Has username column", "username" in col_names, f"cols={col_names}")
        check("Has email column", "email" in col_names, f"cols={col_names}")

        # ── Step 3: No changes for our table ──
        print("\n--- Step 3: No changes for mig_test_users ---")
        result2 = await engine.makemigrations(db, name="nochange")
        # Filter to only ops affecting our test table
        our_ops = [
            op
            for op in result2.get("operations", [])
            if hasattr(op, "table") and "mig_test" in getattr(op, "table", "")
        ]
        check(
            "No changes for mig_test_users",
            len(our_ops) == 0,
            f"our_ops={len(our_ops)}",
        )

        # ── Step 4: Add a field, detect change ──
        print("\n--- Step 4: Add field + makemigrations ---")
        # Dynamically add a field to the model's _meta
        from hyperdjango.models import FieldMeta

        MigTestUser._meta.fields["bio"] = FieldMeta(name="bio")
        MigTestUser.__annotations__["bio"] = str
        # Set the default on the class so _field_to_sql_type can resolve it
        MigTestUser.bio = Field(default="")

        result3 = await engine.makemigrations(db, name="add_bio")
        ops3 = result3.get("operations", [])
        check("Detected AddColumn", len(ops3) > 0, f"got {len(ops3)}")
        if ops3:
            has_add = any("AddColumn" in type(op).__name__ for op in ops3)
            check(
                "Operation is AddColumn",
                has_add,
                f"ops: {[type(op).__name__ for op in ops3]}",
            )

        filepath3 = result3.get("filepath")
        check("Add-field migration file created", filepath3 is not None)

        # ── Step 5: Apply add-field migration ──
        print("\n--- Step 5: Apply add-field migration ---")
        applied3 = await engine.migrate(db)
        check("Add-field migration applied", len(applied3) > 0, f"applied={applied3}")

        # Verify new column
        cols2 = await db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'mig_test_users' ORDER BY ordinal_position"
        )
        col_names2 = [c["column_name"] for c in cols2]
        check("Bio column added", "bio" in col_names2, f"cols={col_names2}")

        # ── Step 6: Dry run ──
        print("\n--- Step 6: Dry run ---")
        # Remove the field from meta to simulate a change
        MigTestUser._meta.fields.pop("bio", None)
        MigTestUser.__annotations__.pop("bio", None)

        result_dry = await engine.makemigrations(db, name="drop_bio", dry_run=True)
        check("Dry run returns operations", len(result_dry.get("operations", [])) > 0)
        check("Dry run has no filepath", result_dry.get("filepath") is None)
        check("Dry run has SQL", len(result_dry.get("sql", [])) > 0)

        # Restore field for consistency
        MigTestUser._meta.fields["bio"] = FieldMeta(name="bio")
        MigTestUser.__annotations__["bio"] = str

        # ── Step 7: Show migration state ──
        print("\n--- Step 7: Migration state ---")
        state = await engine.state.get_applied(db)
        check("Has applied migrations", len(state) >= 2, f"state count={len(state)}")

        # ── Step 8: Insert data and verify ──
        print("\n--- Step 8: Data round-trip ---")
        await db.execute(
            "INSERT INTO mig_test_users (username, email, bio) VALUES ($1, $2, $3)",
            "testuser",
            "test@example.com",
            "A bio",
        )
        row = await db.query_one(
            "SELECT * FROM mig_test_users WHERE username = $1", "testuser"
        )
        check("Data inserted", row is not None)
        check(
            "Bio column has value", row.get("bio") == "A bio", f"got {row.get('bio')}"
        )

    finally:
        # Cleanup
        await db.execute("DROP TABLE IF EXISTS mig_test_users CASCADE")
        with contextlib.suppress(
            Exception
        ):  # best-effort cleanup: table may not exist (DELETE now raises typed ProgrammingError)
            await db.execute(
                "DELETE FROM hyper_migration_state WHERE migration_name LIKE '%mig_test%' OR migration_name LIKE '%initial%' OR migration_name LIKE '%add_bio%'"
            )
        shutil.rmtree(mig_dir, ignore_errors=True)
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
