"""Test the exact introspection query that fails during migrate.

Django's get_table_list runs a 3-column SELECT. This test verifies
the query returns correctly-shaped rows through both raw db_pool
and PgZigCursor paths, on a fresh database with tables created.

Run: uv run pytest tests/test_db/test_introspection_query.py -v -s
"""

import os
import subprocess

import pytest
from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_execute,
    _db_query,
)

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


class TestIntrospectionQueryShape:
    """Test that the introspection query returns 3-column rows."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        user = os.environ.get("USER", "postgres")
        subprocess.run(
            ["dropdb", "--if-exists", "hyperdjango_introspect_test"],
            capture_output=True,
        )
        subprocess.run(["createdb", "hyperdjango_introspect_test"], capture_output=True)
        self.handle = _db_configure(
            f"postgresql://{user}:@localhost:5432/hyperdjango_introspect_test", 2
        )
        # Create a table so get_table_list returns results
        _db_execute(
            self.handle,
            "CREATE TABLE test_introspect (id SERIAL PRIMARY KEY, name TEXT)",
            [],
        )
        yield
        _db_close_pool(self.handle)
        subprocess.run(
            ["dropdb", "--if-exists", "hyperdjango_introspect_test"],
            capture_output=True,
        )

    def test_raw_query_returns_3_columns(self):
        rows = _db_query(self.handle, GET_TABLE_LIST_SQL, [])
        assert len(rows) > 0, "No tables found"
        for row in rows:
            assert len(row) == 3, f"Expected 3 columns, got {len(row)}: {row!r}"

    def test_raw_query_column_types(self):
        rows = _db_query(self.handle, GET_TABLE_LIST_SQL, [])
        found_test_table = False
        for row in rows:
            if row[0] == "test_introspect":
                found_test_table = True
                assert row[0] == "test_introspect"  # relname (str)
                assert row[1] == "t"  # type (str)
                assert row[2] is None  # obj_description (None for no comment)
        assert found_test_table, (
            f"test_introspect not found in rows: {[r[0] for r in rows]}"
        )

    def test_cursor_query_returns_3_columns(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_introspect_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()

        assert len(rows) > 0, "No tables found via cursor"
        for row in rows:
            assert len(row) == 3, (
                f"Expected 3 columns via cursor, got {len(row)}: {row!r}"
            )

        cursor.close()
        conn.close()

    def test_cursor_description_has_3_columns(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_introspect_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        desc = cursor.description
        rows = cursor.fetchall()

        assert desc is not None, "No description"
        assert len(desc) == 3, f"Description has {len(desc)} columns: {desc}"
        assert desc[0].name == "relname"

        cursor.close()
        conn.close()

    def test_after_ddl_in_transaction_introspection_works(self):
        """Simulate migration: CREATE TABLE in transaction, commit, then introspect."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_introspect_test", user=user
        )
        conn.connect()

        # Phase 1: DDL in transaction (like schema_editor atomic block)
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS migration_test (id SERIAL PRIMARY KEY)"
        )
        conn.commit()
        cursor.close()

        # Phase 2: After commit, switch to autocommit and run introspection
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()
        print(f"\nAfter DDL+commit introspection rows: {len(rows)}")
        for r in rows[:3]:
            print(f"  row={r!r} len={len(r)}")
        assert len(rows) > 0, "No tables found after DDL"
        for row in rows:
            assert len(row) == 3, f"Expected 3 columns, got {len(row)}: {row!r}"
        cursor.close()

        # Phase 3: Clean up
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS migration_test")
        cursor.close()
        conn.close()

    def test_django_introspection_get_table_list(self):
        """Test through Django's actual introspection code path."""
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
        # Use our PgZigConnection directly to simulate Django's path
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_introspect_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute(GET_TABLE_LIST_SQL)
        rows = cursor.fetchall()

        # Simulate what Django does: TableInfo(*row)
        from collections import namedtuple

        TableInfo = namedtuple("TableInfo", ["name", "type", "comment"])
        for row in rows:
            ti = TableInfo(*row)  # This is what crashes in migrate
            assert ti.name is not None
            assert ti.type in ("t", "v", "p")

        cursor.close()
        conn.close()
