"""
Regression tests for HIGH priority fixes.

Tests:
1. QuerySet write operations route to primary (for_write=True)
2. pg_advisory_lock prevents concurrent migrations
3. DDL operations use quoted identifiers for reserved word safety

Usage:
    uv run hyper-test high_priority_fixes
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, set_db
from hyperdjango.migrations import (
    AddColumn,
    AddConstraint,
    AlterColumnNullable,
    AlterColumnType,
    CreateIndex,
    CreateTable,
    DropColumn,
    DropTable,
    ModelColumn,
    _qi,
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
# _qi identifier quoting
# ---------------------------------------------------------------------------


@test("_qi: quotes simple identifier")
def test_qi_simple():
    assert _qi("users") == '"users"'


@test("_qi: quotes reserved word")
def test_qi_reserved():
    assert _qi("order") == '"order"'
    assert _qi("user") == '"user"'
    assert _qi("group") == '"group"'


@test("_qi: rejects an unsafe identifier (validates via sqlident)")
def test_qi_rejects_unsafe():
    # A `"` (or any non-identifier char) can't appear in a real table/column
    # name; _qi now validates via the sqlident authority and REFUSES to build
    # SQL from it, rather than escaping-and-allowing.
    from hyperdjango.sqlident import IdentifierError

    raised = False
    try:
        _qi('my"table')
    except IdentifierError:
        raised = True
    assert raised, "_qi must reject an identifier containing a double-quote"
    # A reserved word stays usable (quoted).
    assert _qi("order") == '"order"'


# ---------------------------------------------------------------------------
# DDL operations use quoted identifiers
# ---------------------------------------------------------------------------


@test("CreateTable: quotes table and column names")
def test_create_table_quoted():
    op = CreateTable(
        table="order",
        columns=[
            ModelColumn(
                name="id",
                type_sql="INTEGER",
                nullable=False,
                is_pk=True,
                is_auto=True,
                is_unique=False,
                has_index=False,
                default_sql=None,
                foreign_key=None,
            ),
            ModelColumn(
                name="user",
                type_sql="VARCHAR(100)",
                nullable=False,
                is_pk=False,
                is_auto=False,
                is_unique=False,
                has_index=False,
                default_sql=None,
                foreign_key=None,
            ),
            ModelColumn(
                name="group",
                type_sql="INTEGER",
                nullable=True,
                is_pk=False,
                is_auto=False,
                is_unique=False,
                has_index=False,
                default_sql=None,
                foreign_key="groups",
            ),
        ],
    )
    sql = op.up_sql()
    assert '"order"' in sql
    assert '"id"' in sql
    assert '"user"' in sql
    assert '"group"' in sql
    assert '"groups"' in sql


@test("AddColumn: quotes table and column")
def test_add_column_quoted():
    op = AddColumn(table="user", column="check", type_sql="BOOLEAN")
    sql = op.up_sql()
    assert '"user"' in sql
    assert '"check"' in sql


@test("DropColumn: quotes table and column")
def test_drop_column_quoted():
    op = DropColumn(table="order", column="group")
    assert '"order"' in op.up_sql()
    assert '"group"' in op.up_sql()


@test("AlterColumnType: quotes table and column")
def test_alter_type_quoted():
    op = AlterColumnType(
        table="user", column="type", old_type="TEXT", new_type="VARCHAR(50)"
    )
    sql = op.up_sql()
    assert '"user"' in sql
    assert '"type"' in sql


@test("AlterColumnNullable: quotes table and column")
def test_alter_nullable_quoted():
    op = AlterColumnNullable(table="check", column="value", nullable=True)
    assert '"check"' in op.up_sql()
    assert '"value"' in op.up_sql()


@test("AddConstraint: quotes table and name")
def test_add_constraint_quoted():
    op = AddConstraint(
        table="order",
        name="fk_order_user",
        sql_clause="FOREIGN KEY (user_id) REFERENCES users(id)",
    )
    assert '"order"' in op.up_sql()
    assert '"fk_order_user"' in op.up_sql()


@test("CreateIndex: quotes table, name, and columns")
def test_create_index_quoted():
    op = CreateIndex(table="user", name="idx_user_name", columns=["name", "email"])
    sql = op.up_sql()
    assert '"user"' in sql
    assert '"idx_user_name"' in sql
    assert '"name"' in sql
    assert '"email"' in sql


@test("DropTable: quotes table")
def test_drop_table_quoted():
    op = DropTable(table="order")
    assert '"order"' in op.up_sql()


@test("DDL: reserved words work end-to-end in PostgreSQL")
async def test_reserved_word_e2e():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Create a table named "order" (reserved word) using quoted identifiers
    op = CreateTable(
        table="order",
        columns=[
            ModelColumn(
                name="id",
                type_sql="INTEGER",
                nullable=False,
                is_pk=True,
                is_auto=True,
                is_unique=False,
                has_index=False,
                default_sql=None,
                foreign_key=None,
            ),
            ModelColumn(
                name="user",
                type_sql="VARCHAR(100)",
                nullable=False,
                is_pk=False,
                is_auto=False,
                is_unique=False,
                has_index=False,
                default_sql=None,
                foreign_key=None,
            ),
            ModelColumn(
                name="check",
                type_sql="BOOLEAN",
                nullable=True,
                is_pk=False,
                is_auto=False,
                is_unique=False,
                has_index=False,
                default_sql=None,
                foreign_key=None,
            ),
        ],
    )

    await db.execute('DROP TABLE IF EXISTS "order" CASCADE')
    await db.execute(op.up_sql().rstrip(";"))

    # Insert and query
    await db.execute(
        'INSERT INTO "order" ("user", "check") VALUES ($1, $2)', "alice", True
    )
    row = await db.query_one(
        'SELECT "user", "check" FROM "order" WHERE "user" = $1', "alice"
    )
    assert row is not None
    assert row["user"] == "alice"
    assert row["check"] is True

    # Cleanup
    await db.execute('DROP TABLE IF EXISTS "order" CASCADE')
    await db.disconnect()


# ---------------------------------------------------------------------------
# QuerySet write routing
# ---------------------------------------------------------------------------


@test("QuerySet._get_db: for_write parameter exists")
def test_get_db_for_write():
    from hyperdjango.query import QuerySet

    sig = inspect.signature(QuerySet._get_db)
    assert "for_write" in sig.parameters


@test("QuerySet: update/delete use for_write=True (code inspection)")
def test_write_routing_code():
    from hyperdjango.query import QuerySet

    update_src = inspect.getsource(QuerySet.update)
    assert "for_write=True" in update_src

    delete_src = inspect.getsource(QuerySet.delete)
    assert "for_write=True" in delete_src

    bulk_src = inspect.getsource(QuerySet.bulk_create)
    assert "for_write=True" in bulk_src


@test("QuerySet: read/write split routes at RUNTIME (writes→primary, reads→replica)")
def test_write_routing_runtime():
    # Source inspection above proves update/delete PASS for_write=True. This
    # proves the runtime consequence: with a configured read/write router,
    # a write query resolves to the primary and a read to the replica. If the
    # QuerySet holds a stale copy of the connection-manager singleton, routing
    # is silently dead and both would fall through to the default db.
    import hyperdjango.multi_db as mdb
    from hyperdjango.models import Field, Model
    from hyperdjango.multi_db import ConnectionManager, DatabaseRouter, set_connections
    from hyperdjango.query import QuerySet

    class _RoutedDummy(Model):
        class Meta:
            table = "routed_dummy_hpf"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(default="")

    class _Spy:
        def __init__(self, label):
            self.label = label

    primary = _Spy("primary")
    replica = _Spy("replica")

    class _RWRouter(DatabaseRouter):
        def db_for_read(self, model):
            return "replica"

        def db_for_write(self, model):
            return "primary"

    mgr = ConnectionManager()
    mgr._databases = {"primary": primary, "replica": replica, "default": primary}
    mgr.router = _RWRouter()

    saved = mdb._connections
    set_connections(mgr)
    try:
        qs = QuerySet(_RoutedDummy)
        write_db = qs._get_db(for_write=True)
        read_db = qs._get_db(for_write=False)
        assert write_db is primary, (
            f"writes must route to primary; got "
            f"{getattr(write_db, 'label', write_db)!r} — routing is dead"
        )
        assert read_db is replica, (
            f"reads must route to replica; got "
            f"{getattr(read_db, 'label', read_db)!r} — routing is dead"
        )
    finally:
        set_connections(saved)


# ---------------------------------------------------------------------------
# pg_advisory_lock
# ---------------------------------------------------------------------------


@test("MigrationEngine.migrate: uses advisory lock")
def test_migrate_advisory_lock():
    from hyperdjango.migrations import MigrationEngine

    # migrate() gates the run on the advisory lock via the _migration_lock
    # context manager (the lock SQL was refactored out of migrate() into that
    # pinned-connection helper so lock and unlock hit the SAME backend session).
    migrate_src = inspect.getsource(MigrationEngine.migrate)
    assert "_migration_lock" in migrate_src

    lock_src = inspect.getsource(MigrationEngine._migration_lock)
    assert "pg_try_advisory_lock" in lock_src
    assert "pg_advisory_unlock" in lock_src


@test("MigrationEngine.migrate: raises on lock contention")
async def test_migrate_lock_contention():
    # Use two separate pools (different max_size forces separate pool handles)
    db1 = Database(DB_URL, max_size=2)
    await db1.connect()

    db2 = Database(DB_URL, max_size=3)
    await db2.connect()
    set_db(db2)

    # Connection 1 acquires the lock
    await db1.execute("SELECT pg_advisory_lock(hashtext('hyper_migrations'))")

    from hyperdjango.migrations import MigrationEngine

    engine = MigrationEngine("nonexistent_dir")

    # Connection 2 should fail to acquire (different session)
    try:
        await engine.migrate(db2)
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "Another migration" in str(e)
    finally:
        await db1.execute("SELECT pg_advisory_unlock(hashtext('hyper_migrations'))")
        await db1.disconnect()
        await db2.disconnect()


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nHIGH Priority Regression Tests ({len(tests)} tests)")
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
