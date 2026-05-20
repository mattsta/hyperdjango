"""Round-trip fidelity for the native list-param serializer (pyListToPgArray).

When a Python list/tuple is passed directly as a single query param, the native
driver serializes it to a PostgreSQL array literal `{...}` element-by-element via
`pyObjToText`. That per-element encoder must stay byte-faithful to the values,
matching the (already-correct) scalar param path:

  * an int wider than int64 must serialize to its EXACT decimal string, not
    truncate to -1 — and must NOT leave an OverflowError set in the interpreter
    (a leaked error poisons the very next native call).
  * non-finite floats must serialize to PG-parseable "NaN"/"Infinity"/"-Infinity".

Regression for the drift where `pyObjToText` used `PyLong_AsLong` with no
overflow check and had no NaN/Inf handling, while the scalar path did both.

Run: uv run pytest tests/test_db/test_array_param_fidelity.py -v
"""

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    yield


class TestBigIntArrayElements:
    """int > int64 inside a list param → exact decimal, no truncation/poison."""

    def test_over_int64_positive(self):
        # 10**30 far exceeds int64 max (~9.2e18). Old code → "-1".
        big = 10**30
        rows = _db.query("SELECT x::text FROM unnest($1::numeric[]) AS x", [[big, 1]])
        assert [r[0] for r in rows] == [str(big), "1"]

    def test_over_int64_negative(self):
        big = -(10**25)
        rows = _db.query("SELECT x::text FROM unnest($1::numeric[]) AS x", [[big]])
        assert rows[0][0] == str(big)

    def test_int64_boundaries(self):
        vals = [2**63 - 1, -(2**63), 0]
        rows = _db.query("SELECT x::text FROM unnest($1::numeric[]) AS x", [vals])
        assert [r[0] for r in rows] == [str(v) for v in vals]

    def test_no_overflowerror_leak_poisons_next_call(self):
        # A big-int array element used to leave OverflowError set. The very next
        # native call would then observe/clear a spurious error. Prove the pool
        # keeps working normally right after a >int64 array param.
        _db.query("SELECT x::text FROM unnest($1::numeric[]) AS x", [[10**40]])
        rows = _db.query("SELECT $1::int AS ok", [7])
        assert rows[0][0] == 7


class TestNonFiniteFloatArrayElements:
    """NaN / +Inf / -Inf inside a list param → PG-parseable literals."""

    def test_nan_inf_neg_inf(self):
        vals = [float("nan"), float("inf"), float("-inf"), 1.5]
        rows = _db.query("SELECT x::text FROM unnest($1::float8[]) AS x", [vals])
        got = [r[0] for r in rows]
        assert got == ["NaN", "Infinity", "-Infinity", "1.5"]

    def test_finite_float_precision_preserved(self):
        # shortest-round-trip decimal must reconstruct the exact double.
        v = 0.1 + 0.2  # 0.30000000000000004
        rows = _db.query("SELECT x FROM unnest($1::float8[]) AS x", [[v]])
        assert rows[0][0] == v


class TestOrdinaryArrayStillWorks:
    """Common cases must be unchanged by the encoder fix."""

    def test_int_and_text_and_null(self):
        rows = _db.query("SELECT x FROM unnest($1::text[]) AS x", [["a", None, "b"]])
        assert [r[0] for r in rows] == ["a", None, "b"]

    def test_any_array_membership(self):
        rows = _db.query("SELECT $1::int = ANY($2::int[]) AS hit", [3, [1, 2, 3]])
        assert rows[0][0] is True
