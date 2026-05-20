"""Round-trip fidelity for every _pg_quote_literal type branch.

_pg_quote_literal is the client-side value→SQL-literal binder (the cursor path).
Each supported Python type must round-trip through a live query byte-faithfully.
This locks the encode contract for datetime/time/date, Decimal (incl. high
precision + non-finite), bytes/bytea, UUID, dict/list→jsonb, bool/None.

Run: uv run pytest tests/test_db/test_quote_literal_types.py -v
"""

import datetime
import decimal
import os
import uuid

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest

from hyperdjango.db.pgzig_connection import PgZigConnection

_UTC = datetime.UTC
_CST = datetime.timezone(datetime.timedelta(hours=-6))


@pytest.fixture(scope="module")
def cur():
    conn = PgZigConnection(
        host="localhost",
        port=5432,
        dbname="hyperdjango_test",
        user=os.environ.get("USER", "postgres"),
    )
    conn.connect()
    conn.autocommit = True
    c = conn.cursor()
    # Deterministic session tz so timestamptz round-trips are stable.
    c.execute("SET TIME ZONE 'UTC'")
    yield c
    c.close()
    conn.close()


def _rt(cur, value, cast):
    cur.execute(f"SELECT %s::{cast} AS v", [value])
    return cur.fetchone()[0]


class TestDateTime:
    # CONTRACT: this framework uses a naive-UTC datetime convention throughout
    # (session/apikey expiry etc. compare against naive utcnow()). The native
    # decoder therefore returns timestamp AND timestamptz as NAIVE datetimes in
    # UTC wall-clock — deliberately, not a bug. These tests assert the ENCODE
    # side is faithful: an aware input's instant is preserved, surfacing as its
    # UTC wall clock on read-back.
    def _utc_wall(self, dt):
        return dt.astimezone(_UTC).replace(tzinfo=None)

    def test_aware_datetime_utc_instant_preserved(self, cur):
        dt = datetime.datetime(2024, 3, 1, 12, 30, 45, 123456, tzinfo=_UTC)
        got = _rt(cur, dt, "timestamptz")
        assert got.tzinfo is None  # naive-UTC convention
        assert got == self._utc_wall(dt)

    def test_aware_datetime_nonzero_offset_instant_preserved(self, cur):
        # 06:30:45-06:00 is the same instant as 12:30:45Z
        dt = datetime.datetime(2024, 3, 1, 6, 30, 45, tzinfo=_CST)
        got = _rt(cur, dt, "timestamptz")
        assert got == self._utc_wall(dt) == datetime.datetime(2024, 3, 1, 12, 30, 45)

    def test_naive_datetime(self, cur):
        dt = datetime.datetime(2024, 3, 1, 12, 30, 45, 7)
        assert _rt(cur, dt, "timestamp") == dt

    def test_date(self, cur):
        d = datetime.date(2024, 12, 31)
        assert _rt(cur, d, "date") == d


class TestTime:
    def test_naive_time(self, cur):
        t = datetime.time(23, 59, 58, 500000)
        assert _rt(cur, t, "time") == t

    def test_aware_time_preserves_offset(self, cur):
        # tz-aware time bound into a timetz column must keep its OWN utcoffset,
        # not have it dropped to naive + reinterpreted in the session tz.
        # Use a non-UTC offset so a dropped tz (→ session UTC) is detectable.
        tzp5 = datetime.timezone(datetime.timedelta(hours=5))
        t = datetime.time(12, 0, 0, tzinfo=tzp5)
        cur.execute("DROP TABLE IF EXISTS ql_timetz_test")
        cur.execute("CREATE TABLE ql_timetz_test (tt TIMETZ)")
        try:
            # NB: no ::timetz cast on the param — exercise _pg_quote_literal's
            # OWN cast, which is what a real INSERT relies on.
            cur.execute("INSERT INTO ql_timetz_test (tt) VALUES (%s)", [t])
            cur.execute("SELECT tt FROM ql_timetz_test")
            got = cur.fetchone()[0]
            assert got.utcoffset() == t.utcoffset(), (
                f"tz dropped: got {got!r} ({got.utcoffset()}) want {t!r}"
            )
            assert got == t
        finally:
            cur.execute("DROP TABLE IF EXISTS ql_timetz_test")


class TestDecimal:
    def test_high_precision(self, cur):
        v = decimal.Decimal("1.2345678901234567890123456789")
        assert _rt(cur, v, "numeric") == v

    def test_negative_and_zero_scale(self, cur):
        for v in (decimal.Decimal("-12345.6789"), decimal.Decimal(42)):
            assert _rt(cur, v, "numeric") == v

    def test_nan(self, cur):
        got = _rt(cur, decimal.Decimal("NaN"), "numeric")
        assert got.is_nan()


class TestBytes:
    def test_arbitrary_bytes_incl_null_and_high(self, cur):
        b = bytes(range(256))
        got = _rt(cur, b, "bytea")
        assert bytes(got) == b

    def test_empty_bytes(self, cur):
        got = _rt(cur, b"", "bytea")
        assert bytes(got) == b""


class TestUUID:
    def test_uuid(self, cur):
        u = uuid.uuid4()
        assert str(_rt(cur, u, "uuid")) == str(u)


class TestJson:
    def test_dict_jsonb(self, cur):
        d = {"a": 1, "msg": 'he\'s "here"', "u": "ünï", "n": None}
        got = _rt(cur, d, "jsonb")
        import json

        assert json.loads(got) if isinstance(got, str) else got == d

    def test_nested_list_jsonb(self, cur):
        v = [{"x": [1, 2]}, {"y": 3}]
        got = _rt(cur, v, "jsonb")
        import json

        parsed = json.loads(got) if isinstance(got, str) else got
        assert parsed == v


class TestScalars:
    def test_bool_and_none(self, cur):
        assert _rt(cur, True, "bool") is True
        assert _rt(cur, False, "bool") is False
        cur.execute("SELECT %s AS v", [None])
        assert cur.fetchone()[0] is None
