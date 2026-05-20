#!/usr/bin/env python3
"""
Comprehensive PostgreSQL OID coverage test.

Creates a table with every supported PostgreSQL type, inserts test data,
queries it back, and verifies Python types and values match expectations.

Run: uv run hyper-test all_oids
"""

# hyper-test: db_isolated

import ipaddress
import os
import traceback
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_execute,
    _db_query,
)

from hyperdjango.testkit import check, finish, run_main


def _conn_str() -> str:
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", os.environ.get("USER", "postgres"))
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


h = _db_configure(_conn_str(), 2)


def setup():
    _db_execute(h, "DROP TABLE IF EXISTS oid_test", [])
    _db_execute(
        h,
        """CREATE TABLE oid_test (
        -- Integer types
        col_int2 SMALLINT,
        col_int4 INTEGER,
        col_int8 BIGINT,
        -- Float types
        col_float4 REAL,
        col_float8 DOUBLE PRECISION,
        -- Boolean
        col_bool BOOLEAN,
        -- Text types
        col_text TEXT,
        col_varchar VARCHAR(100),
        col_char CHAR(10),
        -- Numeric/Decimal
        col_numeric NUMERIC(10, 2),
        -- UUID
        col_uuid UUID,
        -- Date/Time
        col_timestamp TIMESTAMP,
        col_timestamptz TIMESTAMPTZ,
        col_date DATE,
        col_time TIME,
        col_timetz TIMETZ,
        col_interval INTERVAL,
        -- Binary
        col_bytea BYTEA,
        -- JSON
        col_json JSON,
        col_jsonb JSONB,
        -- Network
        col_inet INET,
        col_cidr CIDR,
        -- Money
        col_money MONEY,
        -- Bit
        col_bit BIT(8),
        col_varbit VARBIT(16),
        -- XML
        col_xml XML,
        -- Full-text search
        col_tsvector TSVECTOR,
        col_tsquery TSQUERY,
        -- Arrays
        col_int_arr INTEGER[],
        col_text_arr TEXT[],
        col_bool_arr BOOLEAN[],
        col_float_arr DOUBLE PRECISION[]
    )""",
        [],
    )

    _db_execute(
        h,
        """INSERT INTO oid_test VALUES (
        32767, 2147483647, 9223372036854775807,
        3.14, 2.718281828,
        true,
        'hello world', 'varchar test', 'char test',
        123.45,
        '550e8400-e29b-41d4-a716-446655440000',
        '2024-06-15 14:30:00', '2024-06-15 14:30:00+00',
        '2024-06-15', '14:30:00', '14:30:00+05:30',
        '1 year 2 months 3 days 4 hours 5 minutes 6 seconds',
        E'\\\\x48454c4c4f',
        '{"key": "value"}', '{"nested": {"a": 1}}',
        '192.168.1.1', '10.0.0.0/8',
        '$1234.56',
        B'10110011', B'1010',
        '<root>hello</root>',
        'fat & cat', 'fat & cat',
        ARRAY[1, 2, 3], ARRAY['a', 'b', 'c'],
        ARRAY[true, false, true], ARRAY[1.1, 2.2, 3.3]
    )""",
        [],
    )


def test_scalar_types():
    rows = _db_query(h, "SELECT col_int2, col_int4, col_int8 FROM oid_test", [])
    assert rows[0][0] == 32767, f"int2: {rows[0][0]}"
    assert rows[0][1] == 2147483647, f"int4: {rows[0][1]}"
    assert rows[0][2] == 9223372036854775807, f"int8: {rows[0][2]}"
    print("  int2/4/8: OK")

    rows = _db_query(h, "SELECT col_float4, col_float8 FROM oid_test", [])
    assert abs(rows[0][0] - 3.14) < 0.01, f"float4: {rows[0][0]}"
    assert abs(rows[0][1] - 2.718281828) < 0.0001, f"float8: {rows[0][1]}"
    print("  float4/8: OK")

    rows = _db_query(h, "SELECT col_bool FROM oid_test", [])
    assert rows[0][0] is True, f"bool: {rows[0][0]}"
    print("  bool: OK")

    rows = _db_query(h, "SELECT col_text, col_varchar, col_char FROM oid_test", [])
    assert rows[0][0] == "hello world", f"text: {rows[0][0]}"
    assert rows[0][1] == "varchar test", f"varchar: {rows[0][1]}"
    assert rows[0][2].strip() == "char test", f"char: {rows[0][2]!r}"
    print("  text/varchar/char: OK")

    rows = _db_query(h, "SELECT col_numeric FROM oid_test", [])
    assert isinstance(rows[0][0], (Decimal, float, str)), (
        f"numeric type: {type(rows[0][0])}"
    )
    print(f"  numeric: OK (type={type(rows[0][0]).__name__}, val={rows[0][0]})")

    rows = _db_query(h, "SELECT col_uuid FROM oid_test", [])
    val = rows[0][0]
    assert isinstance(val, (UUID, str)), f"uuid type: {type(val)}"
    print(f"  uuid: OK (type={type(val).__name__})")


def test_datetime_types():
    rows = _db_query(h, "SELECT col_timestamp, col_timestamptz FROM oid_test", [])
    assert isinstance(rows[0][0], datetime), f"timestamp type: {type(rows[0][0])}"
    assert isinstance(rows[0][1], datetime), f"timestamptz type: {type(rows[0][1])}"
    print(f"  timestamp/tz: OK ({rows[0][0]}, {rows[0][1]})")

    rows = _db_query(h, "SELECT col_date FROM oid_test", [])
    assert isinstance(rows[0][0], date), f"date type: {type(rows[0][0])}"
    assert rows[0][0] == date(2024, 6, 15)
    print(f"  date: OK ({rows[0][0]})")

    rows = _db_query(h, "SELECT col_time FROM oid_test", [])
    assert isinstance(rows[0][0], time), f"time type: {type(rows[0][0])}"
    print(f"  time: OK ({rows[0][0]})")

    rows = _db_query(h, "SELECT col_timetz FROM oid_test", [])
    val = rows[0][0]
    assert isinstance(val, time), f"timetz type: {type(val)}"
    assert val.tzinfo is not None, "timetz should have tzinfo"
    print(f"  timetz: OK ({val})")

    rows = _db_query(h, "SELECT col_interval FROM oid_test", [])
    assert isinstance(rows[0][0], timedelta), f"interval type: {type(rows[0][0])}"
    print(f"  interval: OK ({rows[0][0]})")


def test_binary_types():
    rows = _db_query(h, "SELECT col_bytea FROM oid_test", [])
    assert isinstance(rows[0][0], bytes), f"bytea type: {type(rows[0][0])}"
    assert rows[0][0] == b"HELLO"
    print("  bytea: OK")


def test_json_types():
    rows = _db_query(h, "SELECT col_json, col_jsonb FROM oid_test", [])
    assert isinstance(rows[0][0], dict), f"json type: {type(rows[0][0])}"
    assert rows[0][0] == {"key": "value"}
    assert isinstance(rows[0][1], dict), f"jsonb type: {type(rows[0][1])}"
    assert rows[0][1] == {"nested": {"a": 1}}
    print("  json/jsonb: OK (native Python dict)")


def test_network_types():
    rows = _db_query(h, "SELECT col_inet FROM oid_test", [])
    val = rows[0][0]
    assert isinstance(val, (ipaddress.IPv4Address, ipaddress.IPv6Address, str)), (
        f"inet type: {type(val)}"
    )
    print(f"  inet: OK (type={type(val).__name__}, val={val})")

    rows = _db_query(h, "SELECT col_cidr FROM oid_test", [])
    val = rows[0][0]
    assert isinstance(val, (ipaddress.IPv4Network, ipaddress.IPv6Network, str)), (
        f"cidr type: {type(val)}"
    )
    print(f"  cidr: OK (type={type(val).__name__}, val={val})")


def test_money_type():
    rows = _db_query(h, "SELECT col_money FROM oid_test", [])
    val = rows[0][0]
    assert isinstance(val, Decimal), f"money type: {type(val)}, expected Decimal"
    assert val == Decimal("1234.56"), f"money val: {val}"
    print(f"  money: OK ({val!r})")


def test_bit_types():
    rows = _db_query(h, "SELECT col_bit, col_varbit FROM oid_test", [])
    assert isinstance(rows[0][0], int), f"bit type: {type(rows[0][0])}, expected int"
    assert rows[0][0] == 0b10110011, f"bit val: {rows[0][0]} (expected {0b10110011})"
    assert isinstance(rows[0][1], int), f"varbit type: {type(rows[0][1])}, expected int"
    assert rows[0][1] == 0b1010, f"varbit val: {rows[0][1]} (expected {0b1010})"
    # Verify bitwise operations work
    assert rows[0][0] & 0xFF == 0b10110011
    assert rows[0][1] >> 2 == 0b10
    print(f"  bit/varbit: OK ({rows[0][0]:#010b}, {rows[0][1]:#06b})")


def test_xml_type():
    rows = _db_query(h, "SELECT col_xml FROM oid_test", [])
    assert isinstance(rows[0][0], str), f"xml type: {type(rows[0][0])}"
    assert "<root>" in rows[0][0]
    print("  xml: OK")


def test_fulltext_types():
    # Native binary parsing — no ::text cast needed
    rows = _db_query(h, "SELECT col_tsvector, col_tsquery FROM oid_test", [])
    # tsvector → list[tuple[str, list[int]]]
    tsv = rows[0][0]
    assert isinstance(tsv, list), f"tsvector type: {type(tsv)}, expected list"
    # Each element is (lexeme, positions)
    lexemes = {item[0] for item in tsv}
    assert "cat" in lexemes and "fat" in lexemes, f"tsvector lexemes: {lexemes}"
    for item in tsv:
        assert isinstance(item, tuple), f"tsvector item type: {type(item)}"
        assert isinstance(item[0], str), f"tsvector lexeme type: {type(item[0])}"
        assert isinstance(item[1], list), f"tsvector positions type: {type(item[1])}"
    print(f"  tsvector: OK ({tsv!r})")

    # tsquery → str (reconstructed from binary tree)
    tsq = rows[0][1]
    assert isinstance(tsq, str), f"tsquery type: {type(tsq)}, expected str"
    assert "fat" in tsq and "cat" in tsq, f"tsquery content: {tsq}"
    print(f"  tsquery: OK ({tsq!r})")


def test_array_types():
    rows = _db_query(h, "SELECT col_int_arr FROM oid_test", [])
    assert isinstance(rows[0][0], list), f"int[]: {type(rows[0][0])}"
    assert rows[0][0] == [1, 2, 3], f"int[]: {rows[0][0]}"
    print(f"  int[]: OK ({rows[0][0]})")

    rows = _db_query(h, "SELECT col_text_arr FROM oid_test", [])
    assert isinstance(rows[0][0], list), f"text[]: {type(rows[0][0])}"
    assert rows[0][0] == ["a", "b", "c"], f"text[]: {rows[0][0]}"
    print(f"  text[]: OK ({rows[0][0]})")

    rows = _db_query(h, "SELECT col_bool_arr FROM oid_test", [])
    assert isinstance(rows[0][0], list), f"bool[]: {type(rows[0][0])}"
    assert rows[0][0] == [True, False, True], f"bool[]: {rows[0][0]}"
    print(f"  bool[]: OK ({rows[0][0]})")

    rows = _db_query(h, "SELECT col_float_arr FROM oid_test", [])
    assert isinstance(rows[0][0], list), f"float[]: {type(rows[0][0])}"
    assert len(rows[0][0]) == 3
    assert abs(rows[0][0][0] - 1.1) < 0.01
    print(f"  float[]: OK ({rows[0][0]})")


def test_typed_arrays():
    """Test that typed arrays return native Python types, not strings."""
    # timestamp[]
    rows = _db_query(
        h,
        "SELECT ARRAY['2024-01-01 12:00:00'::timestamp, '2024-06-15 14:30:00'::timestamp]",
        [],
    )
    arr = rows[0][0]
    assert isinstance(arr, list) and len(arr) == 2
    assert isinstance(arr[0], datetime), f"timestamp[] element type: {type(arr[0])}"
    print(f"  timestamp[]: OK ({[type(x).__name__ for x in arr]})")

    # date[]
    rows = _db_query(h, "SELECT ARRAY['2024-01-01'::date, '2024-06-15'::date]", [])
    arr = rows[0][0]
    assert isinstance(arr[0], date), f"date[] element type: {type(arr[0])}"
    assert arr[0] == date(2024, 1, 1)
    print(f"  date[]: OK ({arr})")

    # time[]
    rows = _db_query(h, "SELECT ARRAY['12:00:00'::time, '14:30:00'::time]", [])
    arr = rows[0][0]
    assert isinstance(arr[0], time), f"time[] element type: {type(arr[0])}"
    print(f"  time[]: OK ({arr})")

    # numeric[]
    rows = _db_query(h, "SELECT ARRAY[1.23::numeric, 4.56::numeric, 7.89::numeric]", [])
    arr = rows[0][0]
    assert isinstance(arr[0], Decimal), f"numeric[] element type: {type(arr[0])}"
    assert arr[0] == Decimal("1.23")
    print(f"  numeric[]: OK ({arr})")

    # uuid[]
    rows = _db_query(
        h, "SELECT ARRAY['550e8400-e29b-41d4-a716-446655440000'::uuid]", []
    )
    arr = rows[0][0]
    assert isinstance(arr[0], UUID), f"uuid[] element type: {type(arr[0])}"
    print(f"  uuid[]: OK ({arr})")

    # bytea[]
    rows = _db_query(
        h, "SELECT ARRAY[E'\\\\x48454c4c4f'::bytea, E'\\\\x574f524c44'::bytea]", []
    )
    arr = rows[0][0]
    assert isinstance(arr[0], bytes), f"bytea[] element type: {type(arr[0])}"
    assert arr[0] == b"HELLO"
    print(f"  bytea[]: OK ({arr})")

    # jsonb[]
    rows = _db_query(h, """SELECT ARRAY['{"a":1}'::jsonb, '{"b":2}'::jsonb]""", [])
    arr = rows[0][0]
    assert isinstance(arr[0], dict), f"jsonb[] element type: {type(arr[0])}"
    assert arr[0] == {"a": 1}
    assert arr[1] == {"b": 2}
    print(f"  jsonb[]: OK ({arr})")

    # json[]
    rows = _db_query(h, """SELECT ARRAY['{"x":10}'::json, '[1,2,3]'::json]""", [])
    arr = rows[0][0]
    assert isinstance(arr[0], dict), f"json[] element type: {type(arr[0])}"
    assert arr[0] == {"x": 10}
    assert arr[1] == [1, 2, 3]
    print(f"  json[]: OK ({arr})")


def test_null_handling():
    _db_execute(
        h,
        """INSERT INTO oid_test (col_int2, col_text, col_bool, col_jsonb, col_inet)
                      VALUES (NULL, NULL, NULL, NULL, NULL)""",
        [],
    )
    rows = _db_query(
        h,
        "SELECT col_int2, col_text, col_bool, col_jsonb, col_inet FROM oid_test WHERE col_int2 IS NULL",
        [],
    )
    assert rows[0][0] is None
    assert rows[0][1] is None
    assert rows[0][2] is None
    assert rows[0][3] is None
    assert rows[0][4] is None
    print("  NULL handling: OK (all types return None)")


def _run(name: str, fn: Callable[[], None]) -> bool:
    """Run one assert-battery function and record a single pass/fail.

    The asserts inside each function abort that function on the first bad
    value — that is this file's contract — so a failure is reported once, at
    function granularity, and the caller stops.
    """
    try:
        fn()
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False
    return check(name, True)


def main() -> bool:
    print("Comprehensive PostgreSQL OID Coverage Test:")
    print("=" * 50)
    stages: tuple[tuple[str, Callable[[], None]], ...] = (
        ("setup", setup),
        ("scalar types", test_scalar_types),
        ("datetime types", test_datetime_types),
        ("binary types", test_binary_types),
        ("json types", test_json_types),
        ("network types", test_network_types),
        ("money type", test_money_type),
        ("bit types", test_bit_types),
        ("xml type", test_xml_type),
        ("fulltext types", test_fulltext_types),
        ("array types", test_array_types),
        ("typed arrays", test_typed_arrays),
        ("null handling", test_null_handling),
    )
    for name, fn in stages:
        if not _run(name, fn):
            return finish()

    _db_execute(h, "DROP TABLE oid_test", [])
    _db_close_pool(h)
    print("=" * 50)
    return finish()


if __name__ == "__main__":
    run_main(main)
