"""Debug test for introspection query that fails during migrate.

The get_table_list query returns 3 columns but our cursor returns 1-column rows.
This test reproduces the exact query path to find the bug.

Run: uv run pytest tests/test_db/test_introspection_debug.py -v
"""

import os

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    # Create a table so introspection queries find something
    db_pool.execute(
        "CREATE TABLE IF NOT EXISTS test_introspection_debug (id SERIAL PRIMARY KEY, name TEXT)"
    )
    yield
    db_pool.execute("DROP TABLE IF EXISTS test_introspection_debug")


class TestIntrospectionQuery:
    """Reproduce the TableInfo(*row) failure."""

    def test_get_table_list_query_returns_3_columns(self):
        """The exact query Django's get_table_list runs."""
        rows = _db.query("""
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
        """)
        print(f"\nRows returned: {len(rows)}")
        if rows:
            print(f"First row: {rows[0]}")
            print(f"First row type: {type(rows[0])}")
            print(f"First row len: {len(rows[0])}")
            for i, col in enumerate(rows[0]):
                print(f"  col[{i}] = {col!r} (type: {type(col).__name__})")
        assert len(rows) > 0, "No tables found"
        assert len(rows[0]) == 3, f"Expected 3 columns, got {len(rows[0])}: {rows[0]}"

    def test_simple_3_column_select(self):
        """Basic 3-column query to verify tuple structure."""
        rows = _db.query("SELECT 1 AS a, 'hello' AS b, true AS c")
        print(f"\nRow: {rows[0]}")
        print(f"Row len: {len(rows[0])}")
        assert len(rows[0]) == 3, f"Expected 3 columns, got {len(rows[0])}: {rows[0]}"
        assert rows[0][0] == 1
        assert rows[0][1] == "hello"
        assert rows[0][2] is True

    def test_cursor_3_column_select(self):
        """3-column query through PgZigCursor (Django's path)."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS a, 'hello' AS b, true AS c")
        rows = cursor.fetchall()
        print(f"\nCursor row: {rows[0]}")
        print(f"Cursor row len: {len(rows[0])}")
        assert len(rows[0]) == 3, f"Expected 3 columns, got {len(rows[0])}: {rows[0]}"

        cursor.close()
        conn.close()

    def test_cursor_get_table_list_query(self):
        """The exact query path that fails during migrate, through cursor."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("""
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
        """)
        rows = cursor.fetchall()
        print(f"\nIntrospection rows: {len(rows)}")
        if rows:
            print(f"First row: {rows[0]}")
            print(f"First row len: {len(rows[0])}")
            assert len(rows[0]) == 3, (
                f"Expected 3 columns, got {len(rows[0])}: {rows[0]}"
            )

        cursor.close()
        conn.close()

    def test_cursor_description_matches_columns(self):
        """Verify cursor.description column count matches row column count."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS a, 'hello' AS b, true AS c")
        desc = cursor.description
        rows = cursor.fetchall()
        print(f"\nDescription: {desc}")
        print(f"Description len: {len(desc)}")
        print(f"Row: {rows[0]}")
        print(f"Row len: {len(rows[0])}")
        assert len(desc) == 3, f"Description has {len(desc)} columns"
        assert len(rows[0]) == 3, f"Row has {len(rows[0])} columns"
        assert desc[0].name == "a"
        assert desc[1].name == "b"
        assert desc[2].name == "c"

        cursor.close()
        conn.close()
