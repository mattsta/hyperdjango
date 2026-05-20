"""End-to-end: native _db_query_json must render NUMERIC / TIMESTAMP /
TIMESTAMPTZ / DATE / TIME / UUID / JSONB byte-identically to the Python path
(query() → dicts of Python objects → fast_json_dumps).

Before the native encoder learned these binary formats, its `else` branch
quoted the raw PG binary bytes (garbage) for these OIDs; a strict SQL-type
allow-list gated them off. This test proves the fast path now covers them
without corruption, and stays byte-for-byte identical to the Python path so
the two are interchangeable.

Run: uv run pytest tests/test_db/test_native_json_render.py -v
"""

import datetime
import decimal

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest
from hyperdjango._hyperdjango_native import _db_query_dicts, _db_query_json

from hyperdjango.native import fast_json_dumps

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    _db.execute("DROP TABLE IF EXISTS njr", [])
    _db.execute(
        """CREATE TABLE njr (
            id        INT,
            ts        TIMESTAMP,
            tstz      TIMESTAMPTZ,
            d         DATE,
            t         TIME,
            n1        NUMERIC(10, 3),
            n2        NUMERIC,
            n3        NUMERIC(14, 4),
            u         UUID,
            jb        JSONB,
            js        JSON,
            txt       TEXT
        )""",
        [],
    )
    _db.execute(
        """INSERT INTO njr VALUES
        (1, '2024-01-15 10:30:45.123456', '2024-01-15 10:30:45.123456+00',
         '2024-01-15', '10:30:45.123456', 123.456, 100, 1000.5000,
         '550e8400-e29b-41d4-a716-446655440000',
         '{"a": 1, "b": [2, 3], "c": "x y"}', '{"raw": true}', 'hi'),
        (2, '2000-01-01 00:00:00', '2000-06-15 23:59:59+00',
         '1999-12-31', '00:00:00', 0, -99.99, -0.0010,
         '00000000-0000-0000-0000-000000000000',
         '[1, 2, {"k": "v"}]', '42', 'x'),
        (3, '2038-02-28 23:59:59.000009', '2038-02-28 23:59:59.000009+00',
         '2038-02-28', '23:59:59.000009', 0.001, 12345678.90, 9999999999.9999,
         'ffffffff-ffff-ffff-ffff-ffffffffffff',
         '{"nested": {"deep": [true, false, null]}}', 'null', 'z'),
        (4, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'nulls')""",
        [],
    )
    yield
    _db.execute("DROP TABLE IF EXISTS njr", [])


_SQL = "SELECT id, ts, tstz, d, t, n1, n2, n3, u, jb, js, txt FROM njr ORDER BY id"


def test_query_json_byte_identical_to_python_path():
    native = _db_query_json(_db.handle, _SQL, [])
    py_rows = _db_query_dicts(_db.handle, _SQL, [])
    py_json = fast_json_dumps(py_rows)
    assert native == py_json, f"\nnative: {native.decode()}\npython: {py_json.decode()}"


def test_individual_type_columns_byte_identical():
    # Column-by-column so a failure pinpoints the offending OID.
    for col in ("ts", "tstz", "d", "t", "n1", "n2", "n3", "u", "jb", "js"):
        sql = f"SELECT {col} FROM njr ORDER BY id"
        native = _db_query_json(_db.handle, sql, [])
        py_json = fast_json_dumps(_db_query_dicts(_db.handle, sql, []))
        assert native == py_json, f"column {col}: {native!r} != {py_json!r}"


def test_values_are_semantically_correct():
    # Guard against the two paths agreeing on the WRONG value: check the native
    # JSON parses back to the expected Python-visible values.
    import json

    rows = json.loads(_db_query_json(_db.handle, _SQL, []))
    r0 = rows[0]
    assert r0["ts"] == "2024-01-15T10:30:45.123456"
    assert r0["tstz"] == "2024-01-15T10:30:45.123456"  # naive UTC (no offset)
    assert r0["d"] == "2024-01-15"
    assert r0["t"] == "10:30:45.123456"
    assert r0["n1"] == "123.456"
    assert r0["n2"] == "100"
    assert r0["n3"] == "1000.5000"  # scale padded to 4
    assert r0["u"] == "550e8400-e29b-41d4-a716-446655440000"
    assert r0["jb"] == {"a": 1, "b": [2, 3], "c": "x y"}
    # NULL row
    assert all(v is None for k, v in rows[3].items() if k != "id" and k != "txt")


def test_no_fractional_seconds_when_zero():
    # isoformat() omits the fractional part when microseconds == 0.
    import json

    rows = json.loads(_db_query_json(_db.handle, _SQL, []))
    assert rows[1]["ts"] == "2000-01-01T00:00:00"  # no ".000000"
    assert rows[1]["t"] == "00:00:00"


# Small-magnitude / large-magnitude NUMERIC regressions (ws15 items 1 & 2).
# Before the pg_render.zig fix, any |value| < 0.0001 rendered 10^4× too large
# (weight <= -2 dropped the implied leading zero groups), and 10000.00 / very
# wide integer NUMERICs tripped a range/OOB bug. These assert the native
# _db_query_json path is BOTH correct AND identical to the Decimal object path.
_NUMERIC_CASES = [
    "0.00001234",
    "0.000000005",
    "0.0001",
    "0.00000001",
    "0.000012",
    "10000.00",
    "12345678901234567890123456789012.50",
    "123.456",
    "-99.99",
    "-0.0010",
    "0",
    "0.00000000000001",
]


def test_small_numeric_not_corrupted_and_matches_decimal_path():
    import decimal
    import json

    _db.execute("DROP TABLE IF EXISTS njr_small", [])
    _db.execute("CREATE TABLE njr_small (id INT, v NUMERIC)", [])
    try:
        for i, lit in enumerate(_NUMERIC_CASES):
            _db.execute(f"INSERT INTO njr_small VALUES ({i}, {lit})", [])
        sql = "SELECT id, v FROM njr_small ORDER BY id"

        native_rows = json.loads(_db_query_json(_db.handle, sql, []))
        dict_rows = _db_query_dicts(_db.handle, sql, [])  # v → Decimal objects

        for i, lit in enumerate(_NUMERIC_CASES):
            native_str = native_rows[i]["v"]
            db_decimal = dict_rows[i]["v"]  # Decimal from the object path
            # Native render matches the object path's str exactly...
            assert native_str == str(db_decimal), (
                f"{lit}: native {native_str!r} != object-path {str(db_decimal)!r}"
            )
            # ...and both equal the actual value — the corruption bug rendered
            # e.g. 0.000000005 as 0.500000000 (10^8× off), which this catches.
            assert decimal.Decimal(native_str) == decimal.Decimal(lit), (
                f"{lit}: native value {native_str!r} != {lit}"
            )

        # Whole-result byte-identity between the native JSON encoder and the
        # Python object path (fast_json_dumps over the Decimal objects).
        native = _db_query_json(_db.handle, sql, [])
        py_json = fast_json_dumps(_db_query_dicts(_db.handle, sql, []))
        assert native == py_json, (
            f"\nnative: {native.decode()}\npython: {py_json.decode()}"
        )
    finally:
        _db.execute("DROP TABLE IF EXISTS njr_small", [])


# ── ws22 pg-decode fidelity re-audit ─────────────────────────────────────────
# Three pre-existing binary-decode bugs, verified against PG ground truth and
# str(Decimal)/datetime.isoformat(). Each case below is checked BOTH ways:
#   * native _db_query_json string == object-path str(Decimal/datetime)
#   * both == the value Python computes from the SQL literal (no shared-wrong)

# R3: negative timestamps with a sub-second fraction rendered one second late
# (@divTrunc toward zero mixed with a flooring @mod). Every case is pre-1970 or
# pre-2000 with a fractional part — the exact class that regressed.
_TIMESTAMP_CASES = [
    ("1999-12-31 23:59:59.5", datetime.datetime(1999, 12, 31, 23, 59, 59, 500000)),
    ("1969-07-20 20:17:40.123456", datetime.datetime(1969, 7, 20, 20, 17, 40, 123456)),
    ("1950-01-01 00:00:00.123456", datetime.datetime(1950, 1, 1, 0, 0, 0, 123456)),
    ("1900-01-01 12:30:15.000007", datetime.datetime(1900, 1, 1, 12, 30, 15, 7)),
    # zero-fraction negative + positive: must remain unchanged.
    ("1999-12-31 23:59:59", datetime.datetime(1999, 12, 31, 23, 59, 59)),
    ("2024-01-15 10:30:45.123456", datetime.datetime(2024, 1, 15, 10, 30, 45, 123456)),
]


def test_negative_fractional_timestamps_match_datetime_and_object_path():
    import json

    _db.execute("DROP TABLE IF EXISTS njr_ts", [])
    _db.execute("CREATE TABLE njr_ts (id INT, ts TIMESTAMP)", [])
    try:
        for i, (lit, _expected) in enumerate(_TIMESTAMP_CASES):
            _db.execute(f"INSERT INTO njr_ts VALUES ({i}, '{lit}')", [])
        sql = "SELECT id, ts FROM njr_ts ORDER BY id"

        native_rows = json.loads(_db_query_json(_db.handle, sql, []))
        dict_rows = _db_query_dicts(_db.handle, sql, [])  # ts → datetime objects

        for i, (lit, expected) in enumerate(_TIMESTAMP_CASES):
            native_str = native_rows[i]["ts"]
            db_dt = dict_rows[i]["ts"]
            # object path decodes to the correct datetime (was 1s late before)
            assert db_dt == expected, f"{lit}: object path {db_dt} != {expected}"
            # native JSON string == datetime.isoformat() == object path
            assert native_str == expected.isoformat(), (
                f"{lit}: native {native_str!r} != {expected.isoformat()!r}"
            )
            assert native_str == db_dt.isoformat()

        native = _db_query_json(_db.handle, sql, [])
        py_json = fast_json_dumps(_db_query_dicts(_db.handle, sql, []))
        assert native == py_json, (
            f"\nnative: {native.decode()}\npython: {py_json.decode()}"
        )
    finally:
        _db.execute("DROP TABLE IF EXISTS njr_ts", [])


# R1: zero-valued NUMERIC with a nonzero scale must honor dscale
# (str(Decimal("0.0000"))=="0.0000", str(Decimal("0E-7"))=="0E-7"), not "0".
# R2: a 64-group (256-digit) NUMERIC must round-trip without truncation.
# Each tuple is (SQL literal, expected str(Decimal)).
_R1_R2_CASES = [
    ("CAST(0 AS NUMERIC(10,0))", "0"),
    ("CAST(0 AS NUMERIC(10,4))", "0.0000"),
    ("CAST(0 AS NUMERIC(12,6))", "0.000000"),
    ("CAST(0 AS NUMERIC(12,7))", "0E-7"),
    ("CAST(0 AS NUMERIC(15,10))", "0E-10"),
    # 256-digit integer NUMERIC (64 base-10000 groups), negative.
    (
        "CAST('-" + "9" * 256 + "' AS NUMERIC)",
        "-" + "9" * 256,
    ),
    # very large + zero-with-scale together already covered; add a big negative
    # with fractional scale for good measure.
    ("CAST('-12345678901234567890.1234' AS NUMERIC)", "-12345678901234567890.1234"),
]


def test_zero_scale_and_wide_numeric_match_decimal_and_object_path():
    import json

    _db.execute("DROP TABLE IF EXISTS njr_r12", [])
    _db.execute("CREATE TABLE njr_r12 (id INT, v NUMERIC)", [])
    try:
        for i, (lit, _expected) in enumerate(_R1_R2_CASES):
            _db.execute(f"INSERT INTO njr_r12 VALUES ({i}, {lit})", [])
        sql = "SELECT id, v FROM njr_r12 ORDER BY id"

        native_rows = json.loads(_db_query_json(_db.handle, sql, []))
        dict_rows = _db_query_dicts(_db.handle, sql, [])  # v → Decimal objects

        for i, (lit, expected) in enumerate(_R1_R2_CASES):
            native_str = native_rows[i]["v"]
            db_decimal = dict_rows[i]["v"]
            # object path str(Decimal) is the ground-truth canonical form
            assert str(db_decimal) == expected, (
                f"{lit}: object path {str(db_decimal)!r} != {expected!r}"
            )
            # native string matches str(Decimal) exactly
            assert native_str == expected, (
                f"{lit}: native {native_str!r} != {expected!r}"
            )
            # and both equal the true numeric value
            assert decimal.Decimal(native_str) == db_decimal

        native = _db_query_json(_db.handle, sql, [])
        py_json = fast_json_dumps(_db_query_dicts(_db.handle, sql, []))
        assert native == py_json, (
            f"\nnative: {native.decode()}\npython: {py_json.decode()}"
        )
    finally:
        _db.execute("DROP TABLE IF EXISTS njr_r12", [])
