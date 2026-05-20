"""Test PostgreSQL array parameter conversion.

Tests that Python lists are correctly converted to PostgreSQL array literals
for UNNEST-based bulk_create and other array operations.

Run: uv run pytest tests/test_db/test_array_params.py -v
"""

import os

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    yield


class TestPgArrayLiteral:
    """Test _pg_array_literal conversion."""

    def test_int_array(self):
        from hyperdjango.db.pgzig_connection import _pg_array_literal

        assert _pg_array_literal([1, 2, 3]) == "{1,2,3}"

    def test_str_array(self):
        from hyperdjango.db.pgzig_connection import _pg_array_literal

        result = _pg_array_literal(["hello", "world"])
        assert result == '{"hello","world"}'

    def test_mixed_array(self):
        from hyperdjango.db.pgzig_connection import _pg_array_literal

        result = _pg_array_literal([1, "hello", None])
        assert result == '{1,"hello",NULL}'

    def test_empty_array(self):
        from hyperdjango.db.pgzig_connection import _pg_array_literal

        assert _pg_array_literal([]) == "{}"

    def test_str_with_quotes(self):
        from hyperdjango.db.pgzig_connection import _pg_array_literal

        result = _pg_array_literal(['has "quotes"'])
        assert '\\"' in result

    def test_bool_array(self):
        from hyperdjango.db.pgzig_connection import _pg_array_literal

        assert _pg_array_literal([True, False]) == "{true,false}"


class TestBatchInsert:
    """Test executemany batch INSERT optimization."""

    def test_executemany_batch(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS batch_test")
        cursor.execute(
            "CREATE TABLE batch_test (id SERIAL PRIMARY KEY, name TEXT, value INTEGER)"
        )

        # executemany with multiple rows — should batch into single INSERT
        cursor.executemany(
            "INSERT INTO batch_test (name, value) VALUES (%s, %s)",
            [("alice", 1), ("bob", 2), ("charlie", 3)],
        )
        assert cursor.rowcount == 3

        cursor.execute("SELECT COUNT(*) FROM batch_test")
        rows = cursor.fetchall()
        assert rows[0][0] == 3

        cursor.execute("SELECT name, value FROM batch_test ORDER BY value")
        rows = cursor.fetchall()
        assert rows[0] == ("alice", 1)
        assert rows[1] == ("bob", 2)
        assert rows[2] == ("charlie", 3)

        cursor.execute("DROP TABLE batch_test")
        cursor.close()
        conn.close()

    def test_executemany_single_row(self):
        """Single row should work normally."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS batch_single")
        cursor.execute("CREATE TABLE batch_single (id SERIAL PRIMARY KEY, name TEXT)")
        cursor.executemany("INSERT INTO batch_single (name) VALUES (%s)", [("only",)])
        cursor.execute("SELECT COUNT(*) FROM batch_single")
        assert cursor.fetchone()[0] == 1
        cursor.execute("DROP TABLE batch_single")
        cursor.close()
        conn.close()

    def test_executemany_empty(self):
        """Empty param list should be a no-op."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.executemany("INSERT INTO nonexistent (a) VALUES (%s)", [])
        assert cursor.rowcount == 0
        cursor.close()
        conn.close()


class TestArrayParameterSQL:
    """Test array parameters work in actual SQL queries."""

    def test_unnest_int_array(self):
        rows = _db.query("SELECT * FROM UNNEST($1::integer[])", ["{1,2,3}"])
        assert len(rows) == 3
        values = [r[0] for r in rows]
        assert 1 in values
        assert 2 in values
        assert 3 in values

    def test_unnest_text_array(self):
        rows = _db.query("SELECT * FROM UNNEST($1::text[])", ['{"hello","world"}'])
        assert len(rows) == 2
        values = [r[0] for r in rows]
        assert "hello" in values
        assert "world" in values

    def test_unnest_insert_returning(self):
        """Test the pattern Django uses for bulk_create."""
        _db.execute("DROP TABLE IF EXISTS arr_test", [])
        _db.execute(
            "CREATE TABLE arr_test (id SERIAL PRIMARY KEY, name TEXT, value INTEGER)",
            [],
        )

        rows = _db.query(
            "INSERT INTO arr_test (name, value) "
            "SELECT * FROM UNNEST($1::text[], $2::integer[]) "
            "RETURNING arr_test.id",
            ['{"alice","bob","charlie"}', "{10,20,30}"],
        )
        assert len(rows) == 3
        # All returned IDs should be integers
        for row in rows:
            assert isinstance(row[0], int)

        # Verify data
        check = _db.query("SELECT name, value FROM arr_test ORDER BY id", [])
        assert len(check) == 3
        assert check[0] == ("alice", 10)
        assert check[1] == ("bob", 20)
        assert check[2] == ("charlie", 30)

        _db.execute("DROP TABLE arr_test", [])

    def test_cursor_array_params(self):
        """Test array params through PgZigCursor (Django's path)."""
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS arr_cursor_test")
        cursor.execute(
            "CREATE TABLE arr_cursor_test (id SERIAL PRIMARY KEY, name TEXT)"
        )

        # This is what Django's bulk_create does via UNNEST
        cursor.execute(
            "INSERT INTO arr_cursor_test (name) "
            "SELECT * FROM UNNEST(%s::text[]) RETURNING arr_cursor_test.id",
            [["alice", "bob", "charlie"]],
        )
        rows = cursor.fetchall()
        assert len(rows) == 3

        cursor.execute("DROP TABLE arr_cursor_test")
        cursor.close()
        conn.close()
