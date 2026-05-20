#!/usr/bin/env python3
"""
Test the native SIMD JSON parser (json_loads_native) and JSONB→Python via PostgreSQL.

Validates correctness of all JSON types, escape sequences, nested structures,
and end-to-end JSONB column parsing through pg.zig.

Run: uv run hyper-test json_parser
"""

# hyper-test: db_isolated

import json
import os
import traceback
from collections.abc import Callable

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_execute,
    _db_query,
    json_loads_native,
)

from hyperdjango.testkit import check, finish, run_main

# ── json_loads_native correctness ──────────────────────────────────────────────


def test_primitives():
    assert json_loads_native("null") is None
    assert json_loads_native("true") is True
    assert json_loads_native("false") is False
    assert json_loads_native("42") == 42
    assert json_loads_native("-7") == -7
    assert json_loads_native("0") == 0
    assert json_loads_native("3.14") == 3.14
    assert json_loads_native("-0.5") == -0.5
    assert json_loads_native("1e10") == 1e10
    assert json_loads_native("2.5E-3") == 2.5e-3
    assert json_loads_native('"hello"') == "hello"
    assert json_loads_native('""') == ""
    print("  primitives: OK")


def test_containers():
    assert json_loads_native("{}") == {}
    assert json_loads_native("[]") == []
    assert json_loads_native('{"a": 1}') == {"a": 1}
    assert json_loads_native("[1, 2, 3]") == [1, 2, 3]
    assert json_loads_native('[1, "two", true, null, 3.5]') == [
        1,
        "two",
        True,
        None,
        3.5,
    ]
    print("  containers: OK")


def test_nested():
    data = '{"user": {"name": "Alice", "age": 30}, "tags": ["admin", "user"], "active": true}'
    result = json_loads_native(data)
    assert result == {
        "user": {"name": "Alice", "age": 30},
        "tags": ["admin", "user"],
        "active": True,
    }

    deeply_nested = '{"a": {"b": {"c": {"d": [1, [2, [3]]]}}}}'
    result = json_loads_native(deeply_nested)
    assert result["a"]["b"]["c"]["d"] == [1, [2, [3]]]
    print("  nested: OK")


def test_escapes():
    assert json_loads_native(r'"hello\nworld"') == "hello\nworld"
    assert json_loads_native(r'"tab\there"') == "tab\there"
    assert json_loads_native(r'"quote\"inside"') == 'quote"inside'
    assert json_loads_native(r'"back\\slash"') == "back\\slash"
    assert json_loads_native(r'"slash\/ok"') == "slash/ok"
    assert json_loads_native(r'"cr\rand\r\nlf"') == "cr\rand\r\nlf"
    print("  escapes: OK")


def test_unicode_escapes():
    assert json_loads_native(r'"\u0048\u0065\u006C\u006C\u006F"') == "Hello"
    assert json_loads_native(r'"\u00e9"') == "é"
    print("  unicode escapes: OK")


def test_large_objects():
    # Build a JSON object with 100 keys
    obj = {f"key_{i}": i for i in range(100)}
    json_str = json.dumps(obj)
    result = json_loads_native(json_str)
    assert result == obj

    # Build a JSON array with 1000 elements
    arr = list(range(1000))
    json_str = json.dumps(arr)
    result = json_loads_native(json_str)
    assert result == arr
    print("  large objects: OK")


def test_matches_json_loads():
    """Verify our parser matches Python json.loads for various inputs."""
    test_cases = [
        '{"name": "Alice", "age": 30, "scores": [95, 87, 92]}',
        '[{"id": 1}, {"id": 2}, {"id": 3}]',
        '{"nested": {"deep": {"value": true}}}',
        '{"empty_obj": {}, "empty_arr": [], "null_val": null}',
        '{"special": "line1\\nline2\\ttab"}',
        "12345678901234",  # large int
        "-999999999999",
        '{"float": 1.23456789012345e+100}',
    ]
    for case in test_cases:
        native = json_loads_native(case)
        stdlib = json.loads(case)
        assert native == stdlib, (
            f"Mismatch for {case!r}: native={native!r} vs stdlib={stdlib!r}"
        )
    print("  matches json.loads: OK")


# ── JSONB via PostgreSQL ───────────────────────────────────────────────────────


def _conn_str() -> str:
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", os.environ.get("USER", "postgres"))
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def test_jsonb_via_postgres():
    h = _db_configure(_conn_str(), 2)

    _db_execute(h, "DROP TABLE IF EXISTS jsonb_parser_test", [])
    _db_execute(
        h, "CREATE TABLE jsonb_parser_test (id SERIAL PRIMARY KEY, data JSONB)", []
    )

    # Insert various JSONB values
    test_values = [
        ('{"name": "Alice", "age": 30, "tags": ["admin", "user"]}', dict),
        ('{"count": 42, "active": true, "meta": null}', dict),
        ('"just a string"', str),
        ("[1, 2, 3]", list),
        ("42", int),
        ("true", bool),
        ("null", type(None)),
        ("3.14", float),
    ]

    for json_val, _ in test_values:
        _db_execute(
            h, f"INSERT INTO jsonb_parser_test (data) VALUES ('{json_val}'::jsonb)", []
        )

    rows = _db_query(h, "SELECT data FROM jsonb_parser_test ORDER BY id", [])

    for i, ((json_val, expected_type), row) in enumerate(zip(test_values, rows)):
        val = row[0]
        expected = json.loads(json_val)
        assert isinstance(val, expected_type), (
            f"Row {i}: expected type {expected_type.__name__}, got {type(val).__name__}"
        )
        assert val == expected, f"Row {i}: expected {expected!r}, got {val!r}"

    _db_execute(h, "DROP TABLE jsonb_parser_test", [])
    _db_close_pool(h)
    print("  JSONB via PostgreSQL: OK")


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
    print("Testing SIMD JSON parser (json_loads_native):")
    stages: tuple[tuple[str, Callable[[], None]], ...] = (
        ("primitives", test_primitives),
        ("containers", test_containers),
        ("nested", test_nested),
        ("escapes", test_escapes),
        ("unicode escapes", test_unicode_escapes),
        ("large objects", test_large_objects),
        ("matches json.loads", test_matches_json_loads),
        ("JSONB via PostgreSQL", test_jsonb_via_postgres),
    )
    for name, fn in stages:
        if not _run(name, fn):
            return finish()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
