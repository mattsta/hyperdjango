"""Integration tests for native type conversions through pg.zig.

Tests that all PostgreSQL types are correctly converted to Python types
when queried through the native Zig extension. Requires PostgreSQL running.

Run: uv run pytest tests/test_db/test_native_types.py -v
"""

import datetime
import decimal
import os
import uuid

import pytest

# Module-level db pool — set by db_setup, used by all tests
_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    """Set up test database using shared db_pool fixture."""
    global _db
    _db = db_pool

    # Create test table with all types
    _db.execute("DROP TABLE IF EXISTS type_test", [])
    _db.execute(
        """CREATE TABLE type_test (
        id SERIAL PRIMARY KEY,
        int2_col SMALLINT,
        int4_col INTEGER,
        int8_col BIGINT,
        float4_col REAL,
        float8_col DOUBLE PRECISION,
        bool_col BOOLEAN,
        text_col TEXT,
        varchar_col VARCHAR(100),
        char_col CHAR(10),
        numeric_col NUMERIC(10, 3),
        uuid_col UUID,
        ts_col TIMESTAMP,
        tstz_col TIMESTAMPTZ,
        date_col DATE,
        time_col TIME,
        json_col JSON,
        jsonb_col JSONB,
        bytea_col BYTEA
    )""",
        [],
    )

    # Insert test row
    _db.execute(
        """INSERT INTO type_test (
        int2_col, int4_col, int8_col, float4_col, float8_col, bool_col,
        text_col, varchar_col, char_col, numeric_col, uuid_col,
        ts_col, tstz_col, date_col, time_col,
        json_col, jsonb_col, bytea_col
    ) VALUES (
        32767, 2147483647, 9223372036854775807, 3.14, 2.718281828,
        true, 'hello world', 'varchar test', 'char test',
        123.456, '550e8400-e29b-41d4-a716-446655440000',
        '2024-06-15 14:30:00', '2024-06-15 14:30:00+00',
        '2024-06-15', '14:30:00',
        '{"key": "value"}', '{"nested": {"a": 1}}',
        E'\\\\x48454c4c4f'
    )""",
        [],
    )

    yield

    _db.execute("DROP TABLE IF EXISTS type_test", [])


class TestIntegerTypes:
    def test_int2(self):
        rows = _db.query("SELECT int2_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], int)
        assert rows[0][0] == 32767

    def test_int4(self):
        rows = _db.query("SELECT int4_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], int)
        assert rows[0][0] == 2147483647

    def test_int8(self):
        rows = _db.query("SELECT int8_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], int)
        assert rows[0][0] == 9223372036854775807

    def test_select_literal_int(self):
        rows = _db.query("SELECT 42", [])
        assert isinstance(rows[0][0], int)
        assert rows[0][0] == 42


class TestFloatTypes:
    def test_float4(self):
        rows = _db.query("SELECT float4_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], float)
        assert abs(rows[0][0] - 3.14) < 0.01

    def test_float8(self):
        rows = _db.query("SELECT float8_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], float)
        assert abs(rows[0][0] - 2.718281828) < 0.0001


class TestBoolType:
    def test_bool_true(self):
        rows = _db.query("SELECT bool_col FROM type_test LIMIT 1", [])
        assert rows[0][0] is True

    def test_bool_false(self):
        rows = _db.query("SELECT false::boolean", [])
        assert rows[0][0] is False


class TestStringTypes:
    def test_text(self):
        rows = _db.query("SELECT text_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], str)
        assert rows[0][0] == "hello world"

    def test_varchar(self):
        rows = _db.query("SELECT varchar_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], str)
        assert rows[0][0] == "varchar test"

    def test_char(self):
        rows = _db.query("SELECT char_col FROM type_test LIMIT 1", [])
        assert isinstance(rows[0][0], str)
        # CHAR pads with spaces
        assert rows[0][0].strip() == "char test"


class TestTimestampTypes:
    def test_timestamp(self):
        rows = _db.query("SELECT ts_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, datetime.datetime)
        assert val.year == 2024
        assert val.month == 6
        assert val.day == 15
        # Hour depends on local timezone — just verify it's a valid datetime
        assert 0 <= val.hour <= 23
        assert val.minute == 30

    def test_timestamptz(self):
        rows = _db.query("SELECT tstz_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, datetime.datetime)
        assert val.year == 2024

    def test_now(self):
        rows = _db.query("SELECT NOW()::timestamp", [])
        val = rows[0][0]
        assert isinstance(val, datetime.datetime)
        assert val.year >= 2024


class TestDateTimeTypes:
    def test_date(self):
        rows = _db.query("SELECT date_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, datetime.date)
        assert val.year == 2024
        assert val.month == 6
        assert val.day == 15

    def test_time(self):
        rows = _db.query("SELECT time_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, datetime.time)
        assert val.hour == 14
        assert val.minute == 30


class TestNumericType:
    def test_numeric(self):
        rows = _db.query("SELECT numeric_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, decimal.Decimal)
        assert val == decimal.Decimal("123.456")

    def test_numeric_zero(self):
        rows = _db.query("SELECT 0::numeric", [])
        val = rows[0][0]
        assert isinstance(val, decimal.Decimal)
        assert val == decimal.Decimal(0)

    def test_numeric_negative(self):
        rows = _db.query("SELECT -99.99::numeric", [])
        val = rows[0][0]
        assert isinstance(val, decimal.Decimal)
        assert val == decimal.Decimal("-99.99")


class TestUuidType:
    def test_uuid(self):
        rows = _db.query("SELECT uuid_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, uuid.UUID)
        assert str(val) == "550e8400-e29b-41d4-a716-446655440000"

    def test_uuid_gen(self):
        rows = _db.query("SELECT gen_random_uuid()", [])
        val = rows[0][0]
        assert isinstance(val, uuid.UUID)
        assert len(str(val)) == 36


class TestJsonTypes:
    def test_json(self):
        rows = _db.query("SELECT json_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        # JSON/JSONB are parsed directly into Python objects by native SIMD parser
        assert isinstance(val, dict)
        assert val == {"key": "value"}

    def test_jsonb(self):
        rows = _db.query("SELECT jsonb_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, dict)
        assert val == {"nested": {"a": 1}}


class TestByteaType:
    def test_bytea(self):
        rows = _db.query("SELECT bytea_col FROM type_test LIMIT 1", [])
        val = rows[0][0]
        assert isinstance(val, bytes)
        assert val == b"HELLO"


class TestNullHandling:
    def test_null_int(self):
        rows = _db.query("SELECT NULL::integer", [])
        assert rows[0][0] is None

    def test_null_text(self):
        rows = _db.query("SELECT NULL::text", [])
        assert rows[0][0] is None

    def test_null_timestamp(self):
        rows = _db.query("SELECT NULL::timestamp", [])
        assert rows[0][0] is None

    def test_null_uuid(self):
        rows = _db.query("SELECT NULL::uuid", [])
        assert rows[0][0] is None

    def test_null_numeric(self):
        rows = _db.query("SELECT NULL::numeric", [])
        assert rows[0][0] is None

    def test_mixed_nulls(self):
        rows = _db.query("SELECT NULL::integer, 'hello'::text, NULL::boolean", [])
        assert rows[0][0] is None
        assert rows[0][1] == "hello"
        assert rows[0][2] is None


class TestArrayTypes:
    def test_int_array(self):
        rows = _db.query("SELECT ARRAY[1, 2, 3]::integer[]", [])
        val = rows[0][0]
        assert isinstance(val, list)
        assert val == [1, 2, 3]

    def test_int2_array(self):
        rows = _db.query("SELECT ARRAY[10, 20]::smallint[]", [])
        val = rows[0][0]
        assert isinstance(val, list)
        assert val == [10, 20]

    def test_int8_array(self):
        rows = _db.query("SELECT ARRAY[100000000000]::bigint[]", [])
        val = rows[0][0]
        assert isinstance(val, list)
        assert val == [100000000000]

    def test_text_array(self):
        rows = _db.query("SELECT ARRAY['hello', 'world']::text[]", [])
        val = rows[0][0]
        assert isinstance(val, list)
        assert val == ["hello", "world"]

    def test_empty_array(self):
        rows = _db.query("SELECT ARRAY[]::integer[]", [])
        val = rows[0][0]
        assert isinstance(val, list)
        assert val == []

    def test_array_with_null(self):
        rows = _db.query("SELECT ARRAY[1, NULL, 3]::integer[]", [])
        val = rows[0][0]
        assert isinstance(val, list)
        assert val[0] == 1
        assert val[1] is None
        assert val[2] == 3


class TestIntervalType:
    def test_interval_days_hours(self):
        rows = _db.query("SELECT '1 day 2 hours 30 minutes'::interval", [])
        val = rows[0][0]
        assert isinstance(val, datetime.timedelta)
        assert val.days == 1
        assert val.total_seconds() == 86400 + 7200 + 1800  # 1d + 2h + 30m

    def test_interval_negative(self):
        rows = _db.query("SELECT '-3 days'::interval", [])
        val = rows[0][0]
        assert isinstance(val, datetime.timedelta)
        assert val.days == -3

    def test_interval_zero(self):
        rows = _db.query("SELECT '0 seconds'::interval", [])
        val = rows[0][0]
        assert isinstance(val, datetime.timedelta)
        assert val.total_seconds() == 0

    def test_interval_microseconds(self):
        rows = _db.query("SELECT '1.5 seconds'::interval", [])
        val = rows[0][0]
        assert isinstance(val, datetime.timedelta)
        assert val.total_seconds() == 1.5

    def test_interval_negative_subsecond(self):
        # Regression: negative sub-second intervals were rendered 1s off by a
        # @divTrunc/@mod sign mismatch (fixed with @divFloor). Must equal Python.
        for expr, secs in (
            ("-0.5 seconds", -0.5),
            ("-1.25 seconds", -1.25),
            ("-0.000001 seconds", -0.000001),
        ):
            rows = _db.query(f"SELECT '{expr}'::interval", [])
            val = rows[0][0]
            assert isinstance(val, datetime.timedelta)
            assert val == datetime.timedelta(seconds=secs), f"{expr}: {val}"


class TestTupleParams:
    """Test that params can be passed as tuples (not just lists)."""

    def test_query_with_tuple_params(self):
        rows = _db.query("SELECT $1::integer + $2::integer", ("10", "20"))
        assert rows[0][0] == 30

    def test_query_with_list_params(self):
        rows = _db.query("SELECT $1::integer + $2::integer", ["10", "20"])
        assert rows[0][0] == 30

    def test_execute_with_tuple_params(self):
        _db.execute("DROP TABLE IF EXISTS tuple_test", ())
        _db.execute("CREATE TABLE tuple_test (id SERIAL PRIMARY KEY, name TEXT)", ())
        _db.execute("INSERT INTO tuple_test (name) VALUES ($1)", ("hello",))
        rows = _db.query("SELECT name FROM tuple_test", ())
        assert rows[0][0] == "hello"
        _db.execute("DROP TABLE tuple_test", ())

    def test_empty_tuple(self):
        rows = _db.query("SELECT 1", ())
        assert rows[0][0] == 1

    def test_empty_list(self):
        rows = _db.query("SELECT 1", [])
        assert rows[0][0] == 1


class TestColumnMetadata:
    def test_get_last_columns_returns_name_oid_tuples(self):
        from hyperdjango._hyperdjango_native import _db_get_last_columns

        _db.query("SELECT int4_col, text_col, bool_col FROM type_test LIMIT 1", [])
        cols = _db_get_last_columns()
        assert len(cols) == 3
        # Each col is (name, oid) tuple
        assert isinstance(cols[0], tuple)
        assert cols[0][0] == "int4_col"
        assert cols[0][1] == 23  # OID for int4
        assert cols[1][0] == "text_col"
        assert cols[1][1] == 25  # OID for text
        assert cols[2][0] == "bool_col"
        assert cols[2][1] == 16  # OID for bool

    def test_cursor_description_has_type_code(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 42::integer AS val, 'hello'::text AS name, true::boolean AS flag"
        )
        desc = cursor.description
        assert desc is not None
        assert len(desc) == 3
        # (name, type_code, display_size, internal_size, precision, scale, null_ok)
        assert desc[0][0] == "val"
        assert desc[0][1] == 23  # int4 OID
        assert desc[1][0] == "name"
        assert desc[1][1] == 25  # text OID
        assert desc[2][0] == "flag"
        assert desc[2][1] == 16  # bool OID
        cursor.close()
        conn.close()

    def test_cursor_description_none_for_dml(self):
        from hyperdjango.db.pgzig_connection import PgZigConnection

        user = os.environ.get("USER", "postgres")
        conn = PgZigConnection(
            host="localhost", port=5432, dbname="hyperdjango_test", user=user
        )
        conn.connect()
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS desc_test")
        assert cursor.description is None
        cursor.execute("CREATE TABLE desc_test (id SERIAL PRIMARY KEY)")
        assert cursor.description is None
        cursor.execute("DROP TABLE desc_test")
        cursor.close()
        conn.close()
