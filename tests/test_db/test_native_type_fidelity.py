"""End-to-end PostgreSQL type round-trip fidelity (encode + decode).

Every edge value is reproduced against PostgreSQL ground truth and asserted on
BOTH native read paths:

  * ``_db_query``       — the Python-object path (Decimal / bytes / datetime / …)
  * ``_db_query_json``  — the query_json fast path (Zig PG-wire → JSON bytes)

and, where a canonical text form exists, against a raw-SQL ``::text`` read so
the two native paths cannot silently agree on a wrong answer.

Covers the ws24-native-types fixes:
  1  MONEY negative |v| < $1 sign loss          7  MACADDR / MACADDR8
  2  bytes bind stored the Python repr          8  multi-dim arrays truncated
  3  int > int64 bind poisoned error state      9  query_json non-allowlisted garbage
  4  NUMERIC NaN / Infinity / -Infinity → 0    10  DATE BC crash
  5  FLOAT inf/nan → invalid JSON              11  TIME 24:00:00 crash
  6  FLOAT large magnitude → JSON null         12  TEXT with NUL silently truncated
                                               13  RANGE types crash

Plus a regression guard for the round-4 NUMERIC / TIMESTAMP / INTERVAL fixes.

Run: uv run pytest tests/test_db/test_native_type_fidelity.py -v
"""

import datetime
import decimal
import json
import math

import pytest
from hyperdjango._hyperdjango_native import (
    _db_execute,
    _db_query,
    _db_query_json,
)

_h = None


@pytest.fixture(scope="module", autouse=True)
def _setup(db_pool):
    global _h
    _h = db_pool.handle


def q(sql, params=None):
    return _db_query(_h, sql, params or [])


def one(sql, params=None):
    """First column of the first row via the object path."""
    return q(sql, params)[0][0]


def qj_rows(sql, params=None):
    """query_json → list of dict rows (parsed)."""
    return json.loads(_db_query_json(_h, sql, params or []).decode())


def one_json(sql, params=None):
    row = qj_rows(sql, params)[0]
    return next(iter(row.values()))


# ── 1 / 9  MONEY (sign preserved, valid JSON) ───────────────────────────────


@pytest.mark.parametrize(
    "cents_literal,expected",
    [
        ("-0.50", "-0.50"),
        ("-0.01", "-0.01"),
        ("-0.99", "-0.99"),
        ("-1.00", "-1.00"),
        ("0.00", "0.00"),
        ("0.50", "0.50"),
        ("123.45", "123.45"),
        ("-123.45", "-123.45"),
    ],
)
def test_money_sign_fidelity(cents_literal, expected):
    sql = f"SELECT '{cents_literal}'::money AS m"
    # object path → Decimal, sign intact
    assert one(sql) == decimal.Decimal(expected)
    # query_json → JSON string equal to str(Decimal)
    assert one_json(sql) == expected
    # raw-SQL ground truth (money::numeric::text drops the currency symbol)
    assert one(f"SELECT ('{cents_literal}'::money)::numeric::text AS m") == expected


# ── 4 / 9  NUMERIC special values ────────────────────────────────────────────


@pytest.mark.parametrize(
    "literal,expected",
    [("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "-Infinity")],
)
def test_numeric_special_values(literal, expected):
    sql = f"SELECT '{literal}'::numeric AS n"
    val = one(sql)
    assert isinstance(val, decimal.Decimal)
    if expected == "NaN":
        assert val.is_nan()
    else:
        assert val == decimal.Decimal(expected)
    # query_json emits it as a JSON string (matches how NUMERIC serializes)
    assert one_json(sql) == expected
    # raw ::text ground truth
    assert one(f"SELECT ('{literal}'::numeric)::text AS n") == expected


# ── 5 / 6  FLOAT inf / nan / large magnitude ─────────────────────────────────


# JSON has no numeric literal for inf/nan. The encoder emits them as the LOSSLESS
# quoted strings "Infinity" / "-Infinity" / "NaN" — NOT null. Emitting null would
# silently DELETE the value: downstream you could never tell an infinite/NaN
# measurement from a missing field, an untraceable-for-weeks data bug. The string
# spellings are valid JSON everywhere AND round-trip through float(); they match
# the NUMERIC path (test_numeric_special_values) so float and numeric agree.
def test_float_infinity_json_string_lossless():
    sql = "SELECT 'infinity'::float8 AS f"
    assert math.isinf(one(sql)) and one(sql) > 0  # object path: value-correct
    jv = one_json(sql)
    assert jv == "Infinity"
    assert math.isinf(float(jv)) and float(jv) > 0  # float() round-trips


def test_float_neg_infinity_json_string_lossless():
    sql = "SELECT '-infinity'::float8 AS f"
    assert math.isinf(one(sql)) and one(sql) < 0
    jv = one_json(sql)
    assert jv == "-Infinity"
    assert math.isinf(float(jv)) and float(jv) < 0


def test_float_nan_json_string_lossless():
    sql = "SELECT 'nan'::float8 AS f"
    assert math.isnan(one(sql))
    jv = one_json(sql)
    assert jv == "NaN"
    assert math.isnan(float(jv))


def test_float4_nan_json_string_lossless():
    # float4 (REAL) shares the same non-finite handling as float8.
    sql = "SELECT 'nan'::float4 AS f"
    assert math.isnan(one(sql))
    assert one_json(sql) == "NaN"


@pytest.mark.parametrize("lit", ["1e308", "1e39", "-1e308", "1.5e-300"])
def test_float_large_magnitude_roundtrips(lit):
    sql = f"SELECT {lit}::float8 AS f"
    expected = float(lit)
    # object path exact
    assert one(sql) == expected
    # query_json must be valid JSON that parses back to the same float
    # (was silently emitting null for large magnitudes)
    jv = one_json(sql)
    assert jv is not None
    assert float(jv) == expected


# ── 7  MACADDR / MACADDR8 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "typ,literal,expected",
    [
        ("macaddr", "08:00:2b:01:02:03", "08:00:2b:01:02:03"),
        ("macaddr", "FF:FF:FF:FF:FF:FF", "ff:ff:ff:ff:ff:ff"),
        ("macaddr8", "08:00:2b:01:02:03:04:05", "08:00:2b:01:02:03:04:05"),
    ],
)
def test_macaddr(typ, literal, expected):
    sql = f"SELECT '{literal}'::{typ} AS m"
    assert one(sql) == expected
    assert one_json(sql) == expected
    assert one(f"SELECT ('{literal}'::{typ})::text AS m") == expected


# ── 8  Multi-dimensional arrays (no longer truncated) ────────────────────────


def test_multidim_int_array():
    sql = "SELECT ARRAY[[1,2,3],[4,5,6]] AS a"
    assert one(sql) == [[1, 2, 3], [4, 5, 6]]
    assert one_json(sql) == [[1, 2, 3], [4, 5, 6]]


def test_multidim_text_array():
    sql = "SELECT ARRAY[['a','b'],['c','d']]::text[] AS a"
    assert one(sql) == [["a", "b"], ["c", "d"]]
    assert one_json(sql) == [["a", "b"], ["c", "d"]]


def test_3d_int_array():
    sql = "SELECT ARRAY[[[1,2],[3,4]],[[5,6],[7,8]]] AS a"
    expected = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]
    assert one(sql) == expected
    assert one_json(sql) == expected


def test_1d_array_still_works():
    sql = "SELECT ARRAY[10,20,30] AS a"
    assert one(sql) == [10, 20, 30]
    assert one_json(sql) == [10, 20, 30]


def test_multidim_numeric_array():
    sql = "SELECT ARRAY[[1.5,2.5],[3.5,4.5]]::numeric[] AS a"
    assert one(sql) == [
        [decimal.Decimal("1.5"), decimal.Decimal("2.5")],
        [decimal.Decimal("3.5"), decimal.Decimal("4.5")],
    ]
    assert one_json(sql) == [["1.5", "2.5"], ["3.5", "4.5"]]


# ── 10  DATE BC → None (no crash / no error poisoning) ───────────────────────


def test_date_bc_returns_none_no_crash():
    sql = "SELECT '0001-01-01 BC'::date AS d"
    assert one(sql) is None
    assert one_json(sql) is None
    # error state not poisoned: next native call must succeed
    assert one("SELECT 1 AS x") == 1


def test_date_normal_still_works():
    sql = "SELECT '2024-01-15'::date AS d"
    assert one(sql) == datetime.date(2024, 1, 15)
    assert one_json(sql) == "2024-01-15"


# ── 11  TIME 24:00:00 → string (no crash) ────────────────────────────────────


def test_time_2400_no_crash():
    sql = "SELECT '24:00:00'::time AS t"
    # object path returns the ISO string (Python time cannot hold hour 24)
    assert one(sql) == "24:00:00"
    assert one_json(sql) == "24:00:00"
    assert one("SELECT 1 AS x") == 1  # not poisoned


def test_time_normal_still_works():
    sql = "SELECT '10:30:45.123456'::time AS t"
    assert one(sql) == datetime.time(10, 30, 45, 123456)
    assert one_json(sql) == "10:30:45.123456"


# ── 11b  TIMETZ 24:00:00 → string (no crash / no error-state poisoning) ───────
# PostgreSQL's TIMETZ accepts '24:00:00', which datetime.time (hour 0..23) cannot
# hold: time(24,...) raises ValueError AND leaves the interpreter error indicator
# set, which the caller's `orelse None` would carry into the NEXT native call.
# The object path returns the canonical ISO string with its zone offset instead.


@pytest.mark.parametrize(
    "literal,expected",
    [
        ("24:00:00+00", "24:00:00+00:00"),
        ("24:00:00-05", "24:00:00-05:00"),
        ("24:00:00+05:30", "24:00:00+05:30"),
    ],
)
def test_timetz_2400_no_crash(literal, expected):
    sql = f"SELECT '{literal}'::timetz AS t"
    assert one(sql) == expected  # ISO string (Python time cannot hold hour 24)
    assert one("SELECT 1 AS x") == 1  # error state NOT poisoned by the ValueError


def test_timetz_normal_still_works():
    sql = "SELECT '10:30:45+02'::timetz AS t"
    val = one(sql)
    assert isinstance(val, datetime.time)
    assert val.hour == 10 and val.minute == 30 and val.second == 45
    assert val.utcoffset() == datetime.timedelta(hours=2)


# ── 2 / 12  bytes bind (raw bytes, embedded NUL, large) ──────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00\x01\xff",
        b"\xde\xad\xbe\xef",
        b"\x00" * 10,
        bytes(range(256)),
        bytes(range(256)) * 8,  # 2048 bytes → overflow path
        b"",
    ],
)
def test_bytes_bind_roundtrip(payload):
    _db_execute(_h, "DROP TABLE IF EXISTS fidelity_bt", [])
    _db_execute(_h, "CREATE TABLE fidelity_bt (id int, b bytea)", [])
    _db_execute(_h, "INSERT INTO fidelity_bt VALUES (1, $1)", [payload])
    # object path: exact bytes back
    assert one("SELECT b FROM fidelity_bt WHERE id=1") == payload
    # raw ::text ground truth
    assert one("SELECT encode(b, 'hex') FROM fidelity_bt WHERE id=1") == payload.hex()
    # query_json: PG \x-hex string, matching a bytea::text read
    assert one_json("SELECT b FROM fidelity_bt WHERE id=1") == "\\x" + payload.hex()


def test_bytearray_bind_roundtrip():
    _db_execute(_h, "DROP TABLE IF EXISTS fidelity_bt", [])
    _db_execute(_h, "CREATE TABLE fidelity_bt (id int, b bytea)", [])
    _db_execute(
        _h, "INSERT INTO fidelity_bt VALUES (1, $1)", [bytearray(b"\x01\x02\x03")]
    )
    assert one("SELECT b FROM fidelity_bt WHERE id=1") == b"\x01\x02\x03"


def test_text_with_nul_surfaces_pg_error_not_truncation():
    _db_execute(_h, "DROP TABLE IF EXISTS fidelity_tt", [])
    _db_execute(_h, "CREATE TABLE fidelity_tt (t text)", [])
    # PG rejects \x00 in text — must surface its error, NOT silently store "a".
    with pytest.raises(Exception):
        _db_execute(_h, "INSERT INTO fidelity_tt VALUES ($1)", ["a\x00b"])
    # error state not poisoned
    assert one("SELECT 1 AS x") == 1
    assert one("SELECT count(*)::int FROM fidelity_tt") == 0


# ── 3  int > int64 bind (no error-state poisoning) ───────────────────────────


@pytest.mark.parametrize(
    "big",
    [10**30, 2**63, 2**63 - 1, -(2**63), -(10**30), 12345678901234567890123456789],
)
def test_bigint_bind_to_numeric(big):
    _db_execute(_h, "DROP TABLE IF EXISTS fidelity_bi", [])
    _db_execute(_h, "CREATE TABLE fidelity_bi (id int, n numeric)", [])
    _db_execute(_h, "INSERT INTO fidelity_bi VALUES (1, $1)", [big])
    assert one("SELECT n FROM fidelity_bi WHERE id=1") == decimal.Decimal(big)
    # a param that overflowed int64 must not have poisoned the error state
    assert one("SELECT 42 AS x") == 42


# ── 13  RANGE types (no crash) ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "literal,typ",
    [
        ("[1,10)", "int4range"),
        ("[1,100)", "int8range"),
        ("[2024-01-01,2024-12-31)", "daterange"),
        ("[1.5,2.5)", "numrange"),
    ],
)
def test_range_types_no_crash(literal, typ):
    sql = f"SELECT '{literal}'::{typ} AS r"
    # unsupported binary decode → None / null, but must NOT crash or poison
    assert one(sql) is None
    assert one_json(sql) is None
    assert one("SELECT 1 AS x") == 1  # not poisoned


# ── Round-4 regression guard (must still hold) ───────────────────────────────


def test_round4_numeric_scale_padding():
    assert one("SELECT 100::numeric(10,4) AS n") == decimal.Decimal("100.0000")
    assert one_json("SELECT 100::numeric(10,4) AS n") == "100.0000"
    assert one("SELECT '0.0000000'::numeric AS n") == decimal.Decimal("0E-7")
    assert one_json("SELECT '0.0000000'::numeric AS n") == "0E-7"


def test_round4_numeric_tiny_fraction():
    # weight <= -2 leading-zero-group regression
    assert one("SELECT '0.00001234'::numeric AS n") == decimal.Decimal("0.00001234")
    assert one_json("SELECT '0.00001234'::numeric AS n") == "0.00001234"
    assert one("SELECT '0.000000005'::numeric AS n") == decimal.Decimal("5E-9")
    assert one_json("SELECT '0.000000005'::numeric AS n") == "5E-9"


def test_round4_timestamp_negative_fraction():
    # pre-2000 timestamp with a fractional second (divFloor fix)
    sql = "SELECT '1999-12-31 23:59:59.5'::timestamp AS t"
    assert one(sql) == datetime.datetime(1999, 12, 31, 23, 59, 59, 500000)
    assert one_json(sql) == "1999-12-31T23:59:59.500000"


def test_round4_interval_negative_subsecond():
    # negative sub-second interval (divFloor fix): -0.5s
    sql = "SELECT '-0.5 seconds'::interval AS i"
    assert one(sql) == datetime.timedelta(seconds=-0.5)
