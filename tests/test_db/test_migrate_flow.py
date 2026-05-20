"""Reproduce the exact Django migration executor flow in-process.

The manage.py migrate subprocess crashes with:
  TypeError: TableInfo.__new__() missing 2 required positional arguments: 'type' and 'comment'

This means get_table_list's introspection query returns 1-column rows instead of 3.
This test simulates the exact flow to reproduce in-process where Zig traces are visible.

Flow:
  1. Connect (autocommit=True)
  2. Enter transaction (autocommit=False → BEGIN)
  3. DDL: CREATE TABLE (through pinned connection)
  4. Still in transaction: run introspection query (get_table_list)
  5. Commit
  6. Run introspection query again in autocommit mode

Run: uv run pytest tests/test_db/test_migrate_flow.py -v -s
"""

import contextlib
import os
import subprocess
from collections import namedtuple

import pytest
from hyperdjango._hyperdjango_native import _db_close_pool

DB_NAME = "hyperdjango_migrate_flow_test"

GET_TABLE_LIST_SQL = """
    SELECT
        c.relname,
        CASE
            WHEN c.relispartition THEN 'p'
            WHEN c.relkind IN ('m', 'v') THEN 'v'
            ELSE 't'
        END,
        obj_description(c.oid, 'pg_class')
    FROM pg_catalog.pg_class c
    LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('f', 'm', 'p', 'r', 'v')
        AND n.nspname NOT IN ('pg_catalog', 'pg_toast')
        AND pg_catalog.pg_table_is_visible(c.oid)
"""

TableInfo = namedtuple("TableInfo", ["name", "type", "comment"])


def _force_drop_db(dbname, user):
    """Terminate connections and drop database."""
    # Terminate all connections to this database
    subprocess.run(
        [
            "psql",
            "-U",
            user,
            "-d",
            "postgres",
            "-c",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{dbname}'",
        ],
        capture_output=True,
    )
    subprocess.run(["dropdb", "--if-exists", dbname], capture_output=True)


@pytest.fixture
def migrate_db():
    """Create a fresh database for migration flow testing."""
    user = os.environ.get("USER", "postgres")
    _force_drop_db(DB_NAME, user)
    subprocess.run(["createdb", DB_NAME], capture_output=True, check=True)
    yield user
    _force_drop_db(DB_NAME, user)


class TestMigrateFlowInProcess:
    """Simulate the exact Django migration executor flow using PgZigConnection."""

    def _make_conn(self, user):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        conn = PgZigConnection(host="localhost", port=5432, dbname=DB_NAME, user=user)
        conn.connect()
        return conn

    def _close_conn(self, conn):
        """Close connection AND its pool (for test cleanup)."""
        pool_handle = conn._pool_handle
        conn.close()
        if pool_handle is not None:
            with contextlib.suppress(Exception):
                _db_close_pool(pool_handle)

    def test_introspection_in_autocommit(self, migrate_db):
        """Baseline: introspection query works in autocommit mode."""
        conn = self._make_conn(migrate_db)
        conn.autocommit = True

        # Create a table first
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test_auto (id SERIAL PRIMARY KEY, name TEXT)")
        cursor.close()

        # Run introspection
        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(f"\nAutocommit rows: {len(rows)}")
        for r in rows[:3]:
            print(f"  row={r!r} len={len(r)}")

        assert len(rows) > 0
        for row in rows:
            assert len(row) == 3, f"Expected 3 columns, got {len(row)}: {row!r}"
            ti = TableInfo(*row)  # This is what crashes in migrate
            assert ti.name is not None

        cursor.close()
        self._close_conn(conn)

    def test_introspection_in_transaction(self, migrate_db):
        """Introspection inside a transaction (autocommit=False)."""
        conn = self._make_conn(migrate_db)
        conn.autocommit = True

        # Create a table in autocommit
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test_txn (id SERIAL PRIMARY KEY)")
        cursor.close()

        # Now enter transaction mode
        conn.autocommit = False

        # Run introspection inside transaction
        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(f"\nIn-transaction rows: {len(rows)}")
        for r in rows[:3]:
            print(f"  row={r!r} len={len(r)}")

        assert len(rows) > 0
        for row in rows:
            assert len(row) == 3, f"Expected 3 columns in txn, got {len(row)}: {row!r}"
            ti = TableInfo(*row)
            assert ti.name is not None

        cursor.close()
        conn.commit()
        self._close_conn(conn)

    def test_introspection_after_ddl_in_transaction(self, migrate_db):
        """DDL in transaction, then introspection — the exact migration path.

        This is the core test: Django's migration executor does:
        1. autocommit=False (BEGIN)
        2. CREATE TABLE (DDL)
        3. record_applied → ensure_schema → has_table → get_table_list
        4. COMMIT
        """
        conn = self._make_conn(migrate_db)

        # Phase 1: Enter transaction
        conn.autocommit = False
        print(f"\nautocommit={conn.autocommit} pinned={conn._pinned_handle}")

        # Phase 2: DDL through pinned connection
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE django_migrations (id SERIAL PRIMARY KEY, app VARCHAR(255), name VARCHAR(255))"
        )
        cursor.close()
        print(f"After DDL: pinned={conn._pinned_handle} in_txn={conn._in_transaction}")

        # Phase 3: Introspection (still in same transaction, same pinned connection)
        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(f"After-DDL introspection rows: {len(rows)}")
        for r in rows:
            print(f"  row={r!r} len={len(r)}")

        assert len(rows) > 0, "No tables found after DDL"
        for row in rows:
            assert len(row) == 3, (
                f"Expected 3 columns after DDL, got {len(row)}: {row!r}"
            )
            ti = TableInfo(*row)
            assert ti.name is not None

        cursor.close()
        conn.commit()
        self._close_conn(conn)

    def test_full_migration_executor_flow(self, migrate_db):
        """Full migration executor simulation:

        1. autocommit=False (BEGIN)
        2. CREATE TABLE (schema change)
        3. COMMIT + release pinned
        4. autocommit=True (Django atomic exit path)
        5. autocommit=False (new atomic block for record_applied)
        6. INSERT into django_migrations
        7. Run introspection (ensure_schema → has_table → get_table_list)
        8. COMMIT
        """
        conn = self._make_conn(migrate_db)

        # === Atomic block 1: Apply migration (DDL) ===
        conn.autocommit = False
        print(f"\n[1] Enter atomic: pinned={conn._pinned_handle}")

        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE django_migrations (id SERIAL PRIMARY KEY, app VARCHAR(255), name VARCHAR(255))"
        )
        cursor.close()
        print(f"[2] After DDL: pinned={conn._pinned_handle}")

        # Atomic block commit
        conn.commit()
        print(f"[3] After commit: pinned={conn._pinned_handle}")

        # Exit atomic → autocommit=True
        conn.autocommit = True
        print(f"[4] autocommit=True: pinned={conn._pinned_handle}")

        # === Atomic block 2: record_applied (or same block, after commit) ===
        conn.autocommit = False
        print(f"[5] Enter atomic 2: pinned={conn._pinned_handle}")

        # INSERT migration record
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO django_migrations (app, name) VALUES ('myapp', '0001_initial')"
        )
        cursor.close()

        # Introspection (ensure_schema → has_table → get_table_list)
        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(f"[6] Introspection rows: {len(rows)}")
        for r in rows:
            print(f"  row={r!r} len={len(r)}")

        assert len(rows) > 0
        for row in rows:
            assert len(row) == 3, f"Expected 3 columns, got {len(row)}: {row!r}"
            ti = TableInfo(*row)
            assert ti.name is not None

        cursor.close()
        conn.commit()
        conn.autocommit = True
        self._close_conn(conn)

    def test_django_atomic_with_savepoint(self, migrate_db):
        """Simulate nested atomic blocks using savepoints.

        Django's atomic() can nest. The outer atomic does BEGIN,
        inner atomics use SAVEPOINT. This tests introspection after
        DDL inside a savepoint.
        """
        conn = self._make_conn(migrate_db)

        # Outer atomic: BEGIN
        conn.autocommit = False
        print(f"\n[1] Outer atomic: pinned={conn._pinned_handle}")

        # DDL
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test_savepoint (id SERIAL PRIMARY KEY)")
        cursor.close()

        # Inner atomic: SAVEPOINT
        cursor = conn.cursor()
        cursor.execute("SAVEPOINT sp1")
        cursor.close()

        # Introspection inside savepoint
        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(f"[2] In savepoint introspection: {len(rows)} rows")
        for r in rows:
            print(f"  row={r!r} len={len(r)}")

        assert len(rows) > 0
        for row in rows:
            assert len(row) == 3, (
                f"Expected 3 columns in savepoint, got {len(row)}: {row!r}"
            )
            ti = TableInfo(*row)
            assert ti.name is not None

        # Release savepoint
        cursor = conn.cursor()
        cursor.execute("RELEASE SAVEPOINT sp1")
        cursor.close()

        # Commit outer
        conn.commit()
        conn.autocommit = True
        self._close_conn(conn)

    def test_multiple_connections_interleaved(self, migrate_db):
        """Simulate Django creating multiple connections (migration + introspection).

        Django sometimes creates temporary connections. This tests that
        the active_pool_handle global state doesn't corrupt results.
        """
        conn1 = self._make_conn(migrate_db)
        conn1.autocommit = True
        print(f"\n[1] conn1 pool={conn1._pool_handle}")

        # Create table on conn1
        cursor = conn1.cursor()
        cursor.execute("CREATE TABLE test_multi (id SERIAL PRIMARY KEY)")
        cursor.close()

        # Create second connection (simulates Django temporary_connection)
        conn2 = self._make_conn(migrate_db)
        conn2.autocommit = True
        print(f"[2] conn2 pool={conn2._pool_handle}")

        # Introspection on conn1 (after conn2 was created — active_pool_handle changed!)
        cursor = conn1.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(f"[3] conn1 introspection: {len(rows)} rows")
        for r in rows:
            print(f"  row={r!r} len={len(r)}")

        assert len(rows) > 0
        for row in rows:
            assert len(row) == 3, (
                f"Expected 3 columns on conn1, got {len(row)}: {row!r}"
            )
            ti = TableInfo(*row)
            assert ti.name is not None

        # Now try introspection in transaction on conn1
        conn1.autocommit = False
        cursor = conn1.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(
            f"[4] conn1 txn introspection: {len(rows)} rows, pinned={conn1._pinned_handle}"
        )

        assert len(rows) > 0
        for row in rows:
            assert len(row) == 3, (
                f"Expected 3 columns on conn1 txn, got {len(row)}: {row!r}"
            )

        cursor.close()
        conn1.commit()
        conn1.autocommit = True

        self._close_conn(conn2)
        self._close_conn(conn1)
