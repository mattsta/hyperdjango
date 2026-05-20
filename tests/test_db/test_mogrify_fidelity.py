"""Round-trip fidelity for client-side param binding (_pg_quote_literal / _mogrify).

The native cursor path binds params client-side: each Python value is quoted to
a SQL literal by `_pg_quote_literal` and substituted into the query by `_mogrify`
(psycopg ClientCursor style). Two values used to serialize wrong:

  * non-finite floats (inf/-inf/nan) → repr() yields bare tokens `inf`/`nan`,
    which PostgreSQL parses as (nonexistent) column identifiers → the query
    ERRORS instead of binding the value. Now emit 'Infinity'/'-Infinity'/'NaN'
    ::float8, matching the native extractParams path.
  * timedelta → total_seconds() is a float and loses microsecond precision for
    large intervals (silent corruption). Now emit exact .days/.seconds/
    .microseconds components, which PG interval sums additively.

Both cover scalar params and list→ARRAY[...] element recursion.

Run: uv run pytest tests/test_db/test_mogrify_fidelity.py -v
"""

import datetime
import math

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest

from hyperdjango.db.pgzig_connection import PgZigConnection, _pg_quote_literal

# ── Pure literal-level checks (no DB) ────────────────────────────────────────


class TestQuoteLiteralNonFiniteFloat:
    def test_positive_infinity(self):
        assert _pg_quote_literal(float("inf")) == "'Infinity'::float8"

    def test_negative_infinity(self):
        assert _pg_quote_literal(float("-inf")) == "'-Infinity'::float8"

    def test_nan(self):
        assert _pg_quote_literal(float("nan")) == "'NaN'::float8"

    def test_finite_floats_unchanged(self):
        # shortest-round-trip repr for finite values, no cast noise
        assert _pg_quote_literal(1.5) == "1.5"
        assert _pg_quote_literal(0.1 + 0.2) == "0.30000000000000004"
        assert _pg_quote_literal(1e308) == "1e+308"

    def test_non_finite_inside_array(self):
        # list → ARRAY[...] recurses through _pg_quote_literal per element
        got = _pg_quote_literal([float("inf"), 1.5, float("nan")])
        assert got == "ARRAY['Infinity'::float8,1.5,'NaN'::float8]"


class TestQuoteLiteralTimedelta:
    def test_large_interval_microsecond_exact(self):
        td = datetime.timedelta(days=100000, microseconds=1)
        assert (
            _pg_quote_literal(td) == "'100000 days 0 seconds 1 microseconds'::interval"
        )

    def test_negative_timedelta_components(self):
        # Python normalizes to days<0 but seconds/micros >= 0
        td = datetime.timedelta(seconds=-1)
        assert td.days == -1 and td.seconds == 86399
        assert (
            _pg_quote_literal(td) == "'-1 days 86399 seconds 0 microseconds'::interval"
        )


# ── Live round-trip through the native cursor path ───────────────────────────

_db = None


@pytest.fixture(scope="module", autouse=True)
def _pool(db_pool):
    global _db
    _db = db_pool
    yield


def _cursor():
    import os

    conn = PgZigConnection(
        host="localhost",
        port=5432,
        dbname="hyperdjango_test",
        user=os.environ.get("USER", "postgres"),
    )
    conn.connect()
    conn.autocommit = True
    return conn


class TestNonFiniteFloatRoundTrip:
    def test_inf_binds_not_errors(self):
        conn = _cursor()
        try:
            cur = conn.cursor()
            cur.execute("SELECT %s::float8 AS v", [float("inf")])
            assert cur.fetchone()[0] == float("inf")
            cur.execute("SELECT %s::float8 AS v", [float("-inf")])
            assert cur.fetchone()[0] == float("-inf")
            cur.execute("SELECT %s::float8 AS v", [float("nan")])
            assert math.isnan(cur.fetchone()[0])
            cur.close()
        finally:
            conn.close()

    def test_inf_in_array_param(self):
        conn = _cursor()
        try:
            cur = conn.cursor()
            cur.execute("SELECT unnest(%s::float8[]) AS v", [[float("inf"), 2.5]])
            rows = [r[0] for r in cur.fetchall()]
            assert rows == [float("inf"), 2.5]
            cur.close()
        finally:
            conn.close()


class TestTimedeltaRoundTrip:
    def test_large_interval_microsecond_precise(self):
        td = datetime.timedelta(days=100000, microseconds=1)
        conn = _cursor()
        try:
            cur = conn.cursor()
            cur.execute("SELECT %s::interval AS v", [td])
            assert cur.fetchone()[0] == td
            cur.close()
        finally:
            conn.close()

    def test_negative_and_sub_second(self):
        for td in (
            datetime.timedelta(seconds=-1),
            datetime.timedelta(microseconds=-1),
            datetime.timedelta(days=-5, hours=3, microseconds=7),
        ):
            conn = _cursor()
            try:
                cur = conn.cursor()
                cur.execute("SELECT %s::interval AS v", [td])
                assert cur.fetchone()[0] == td, f"mismatch for {td!r}"
                cur.close()
            finally:
                conn.close()


class TestExecutemanyBatchInheritsFix:
    """The batch multi-row INSERT builder shares _pg_quote_literal, so the
    inf/timedelta fixes must flow through executemany too (single roundtrip)."""

    def test_batch_insert_non_finite_and_timedelta(self):
        td = datetime.timedelta(days=100000, microseconds=1)
        conn = _cursor()
        try:
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS mog_batch_test")
            cur.execute("CREATE TABLE mog_batch_test (ratio FLOAT8, span INTERVAL)")
            cur.executemany(
                "INSERT INTO mog_batch_test (ratio, span) VALUES (%s, %s)",
                [(float("inf"), td), (float("-inf"), td), (1.5, td)],
            )
            assert cur.rowcount == 3
            cur.execute("SELECT ratio, span FROM mog_batch_test ORDER BY ratio")
            rows = cur.fetchall()
            # -inf, 1.5, inf after ORDER BY
            assert rows[0][0] == float("-inf")
            assert rows[1][0] == 1.5
            assert rows[2][0] == float("inf")
            assert all(r[1] == td for r in rows)
            cur.execute("DROP TABLE mog_batch_test")
            cur.close()
        finally:
            conn.close()
