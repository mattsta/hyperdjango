"""Test client-side parameter binding (mogrify).

Tests that _mogrify and _pg_quote_literal correctly escape and substitute
parameters into SQL strings, matching psycopg3's ClientCursor behavior.

Run: uv run pytest tests/test_db/test_client_binding.py -v
"""

import datetime
import decimal
import uuid

import pytest

from hyperdjango.db.pgzig_connection import _mogrify, _pg_quote_literal


class TestPgQuoteLiteral:
    def test_none(self):
        assert _pg_quote_literal(None) == "NULL"

    def test_bool_true(self):
        assert _pg_quote_literal(True) == "true"

    def test_bool_false(self):
        assert _pg_quote_literal(False) == "false"

    def test_int(self):
        assert _pg_quote_literal(42) == "42"
        assert _pg_quote_literal(-1) == "-1"
        assert _pg_quote_literal(0) == "0"

    def test_float(self):
        result = _pg_quote_literal(3.14)
        assert "3.14" in result

    def test_string(self):
        assert _pg_quote_literal("hello") == "'hello'"

    def test_string_with_quotes(self):
        assert _pg_quote_literal("it's") == "'it''s'"

    def test_string_with_backslash(self):
        # With standard_conforming_strings=on (PostgreSQL default),
        # backslash is a regular character — NOT escaped.
        result = _pg_quote_literal("path\\to")
        assert result == "'path\\to'"

    def test_bytes(self):
        result = _pg_quote_literal(b"HELLO")
        assert "48454c4c4f" in result.lower()
        assert "bytea" in result

    def test_list(self):
        result = _pg_quote_literal([1, 2, 3])
        assert result == "ARRAY[1,2,3]"

    def test_datetime(self):
        dt = datetime.datetime(2024, 6, 15, 14, 30, 0)
        result = _pg_quote_literal(dt)
        assert "2024-06-15" in result
        assert "14:30" in result

    def test_date(self):
        d = datetime.date(2024, 6, 15)
        result = _pg_quote_literal(d)
        assert "2024-06-15" in result
        assert "date" in result

    def test_decimal(self):
        result = _pg_quote_literal(decimal.Decimal("123.456"))
        assert "123.456" in result
        assert "numeric" in result

    def test_uuid(self):
        u = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
        result = _pg_quote_literal(u)
        assert "550e8400" in result
        assert "uuid" in result


class TestMogrify:
    def test_no_params(self):
        assert _mogrify("SELECT 1", None) == "SELECT 1"

    def test_positional_int(self):
        result = _mogrify("SELECT %s + %s", [42, 42])
        assert result == "SELECT 42 + 42"

    def test_positional_string(self):
        result = _mogrify("SELECT * FROM t WHERE name = %s", ["alice"])
        assert result == "SELECT * FROM t WHERE name = 'alice'"

    def test_positional_none(self):
        result = _mogrify("SELECT * FROM t WHERE name = %s", [None])
        assert result == "SELECT * FROM t WHERE name = NULL"

    def test_positional_mixed(self):
        result = _mogrify(
            "INSERT INTO t (a, b, c) VALUES (%s, %s, %s)", [1, "hello", None]
        )
        assert result == "INSERT INTO t (a, b, c) VALUES (1, 'hello', NULL)"

    def test_named_params(self):
        result = _mogrify(
            "SELECT * FROM t WHERE id = %(id)s AND name = %(name)s",
            {"id": 42, "name": "alice"},
        )
        assert "42" in result
        assert "'alice'" in result

    def test_named_duplicate(self):
        result = _mogrify(
            "SELECT * FROM t WHERE a = %(val)s AND b = %(val)s", {"val": 42}
        )
        assert result == "SELECT * FROM t WHERE a = 42 AND b = 42"

    def test_sql_injection_escaped(self):
        result = _mogrify("SELECT * FROM t WHERE name = %s", ["'; DROP TABLE t; --"])
        assert "''; DROP TABLE t; --'" in result  # quotes escaped
        assert "DROP" in result  # the text is there but safely quoted

    def test_list_param(self):
        result = _mogrify("SELECT * FROM UNNEST(%s::int[])", [[1, 2, 3]])
        assert "ARRAY[1,2,3]" in result


class TestClientBindingIntegration:
    """Test client-side binding works with actual PostgreSQL queries."""

    @pytest.fixture(autouse=True)
    def setup_db(self, db_pool):
        try:
            import hyperdjango._hyperdjango_native  # noqa: F401

            self._db = db_pool
            self._has_native = True
        except ImportError:
            self._has_native = False

    def test_int_arithmetic(self):
        if not self._has_native:
            pytest.skip("Native extension not compiled")
        # This was the failing case: $1 + $2 with text params
        # With client-side binding: SELECT 42 + 42 — no ambiguity
        sql = _mogrify("SELECT %s + %s", [42, 42])
        rows = self._db.query(sql)
        assert rows[0][0] == 84

    def test_string_with_special_chars(self):
        if not self._has_native:
            pytest.skip("Native extension not compiled")
        self._db.execute("DROP TABLE IF EXISTS cb_test")
        self._db.execute("CREATE TABLE cb_test (id SERIAL PRIMARY KEY, name TEXT)")
        sql = _mogrify("INSERT INTO cb_test (name) VALUES (%s)", ["it's a test"])
        self._db.execute(sql)
        rows = self._db.query("SELECT name FROM cb_test")
        assert rows[0][0] == "it's a test"
        self._db.execute("DROP TABLE cb_test")

    def test_cursor_end_to_end(self):
        if not self._has_native:
            pytest.skip("Native extension not compiled")
        import os

        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True

        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS cb_cursor_test")
        cursor.execute(
            "CREATE TABLE cb_cursor_test (id SERIAL PRIMARY KEY, val INTEGER)"
        )
        # This uses client-side binding — no $1/$2 type ambiguity
        cursor.execute("INSERT INTO cb_cursor_test (val) VALUES (%s)", [42])
        cursor.execute("SELECT val + %s FROM cb_cursor_test WHERE val = %s", [8, 42])
        rows = cursor.fetchall()
        assert rows[0][0] == 50
        cursor.execute("DROP TABLE cb_cursor_test")
        cursor.close()
        conn.close()
