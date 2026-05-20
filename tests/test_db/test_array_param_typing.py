"""Regression tests for Parse-time parameter type inference.

`col = ANY(ARRAY[$1, $2, ...])` used to fail with `operator does not exist:
integer = text`: the native driver serialized every param as text with NO
declared type, so PostgreSQL's Parse-time inference resolved the ARRAY[]
element type to `text` (unknown → text) before the outer `= ANY(...)` could
propagate the column's integer type.

The native binding now declares inferred parameter type OIDs in the Parse
message (int→int8, float→float8, bool→bool; everything else stays "unknown")
BUT only for SQL that uses an `ARRAY[...]` constructor — so scalar comparisons
and every other shape keep the historical unknown-typed behavior untouched.

This is the shape emitted by ORM M2M prefetch:
    SELECT j.src, t.* FROM j JOIN t ON j.tgt = t.id
    WHERE j.src = ANY(ARRAY[$1, $2, ...])

Run: uv run pytest tests/test_db/test_array_param_typing.py -v
"""

import hyperdjango._hyperdjango_native  # noqa: F401
import pytest

_db = None


@pytest.fixture(scope="module", autouse=True)
def db_setup(db_pool):
    global _db
    _db = db_pool
    _db.execute("DROP TABLE IF EXISTS apt_typing", [])
    _db.execute(
        """CREATE TABLE apt_typing (
            id        INTEGER,
            big_id    BIGINT,
            small_id  SMALLINT,
            label     TEXT,
            ratio     DOUBLE PRECISION,
            flag      BOOLEAN
        )""",
        [],
    )
    _db.execute(
        "INSERT INTO apt_typing VALUES "
        "(1, 100, 10, 'a', 1.5, TRUE), "
        "(2, 200, 20, 'b', 2.5, FALSE), "
        "(3, 300, 30, 'c', 3.5, TRUE)",
        [],
    )
    yield
    _db.execute("DROP TABLE IF EXISTS apt_typing", [])


class TestAnyArrayIntBinds:
    """The bug: integer column = ANY(ARRAY[<int params>])."""

    def test_int4_any_array(self):
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE id = ANY(ARRAY[$1, $2]) ORDER BY id",
            [1, 3],
        )
        assert [r[0] for r in rows] == [1, 3]

    def test_int8_any_array(self):
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE big_id = ANY(ARRAY[$1, $2]) ORDER BY id",
            [100, 300],
        )
        assert [r[0] for r in rows] == [1, 3]

    def test_int2_any_array(self):
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE small_id = ANY(ARRAY[$1, $2]) ORDER BY id",
            [10, 30],
        )
        assert [r[0] for r in rows] == [1, 3]

    def test_single_int_in_array(self):
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE id = ANY(ARRAY[$1]) ORDER BY id",
            [2],
        )
        assert [r[0] for r in rows] == [2]

    def test_float_any_array(self):
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE ratio = ANY(ARRAY[$1, $2]) ORDER BY id",
            [1.5, 3.5],
        )
        assert [r[0] for r in rows] == [1, 3]

    def test_bool_any_array(self):
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE flag = ANY(ARRAY[$1]) ORDER BY id",
            [True],
        )
        assert [r[0] for r in rows] == [1, 3]


class TestAnyArrayTextBinds:
    """Text params in ANY(ARRAY[...]) must keep working (unchanged)."""

    def test_text_any_array(self):
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE label = ANY(ARRAY[$1, $2]) ORDER BY id",
            ["a", "c"],
        )
        assert [r[0] for r in rows] == [1, 3]

    def test_mixed_int_and_text_array_queries(self):
        # int ARRAY and text ARRAY in the same session (exercises the
        # per-SQL prepared-statement cache with different declared OIDs).
        int_rows = _db.query(
            "SELECT id FROM apt_typing WHERE id = ANY(ARRAY[$1, $2]) ORDER BY id",
            [1, 2],
        )
        txt_rows = _db.query(
            "SELECT id FROM apt_typing WHERE label = ANY(ARRAY[$1, $2]) ORDER BY id",
            ["b", "c"],
        )
        assert [r[0] for r in int_rows] == [1, 2]
        assert [r[0] for r in txt_rows] == [2, 3]


class TestM2MJoinShape:
    """The exact JOIN + ANY(ARRAY) shape the ORM M2M prefetch emits."""

    def test_join_any_array(self):
        _db.execute("DROP TABLE IF EXISTS apt_junction", [])
        _db.execute("DROP TABLE IF EXISTS apt_target", [])
        _db.execute("CREATE TABLE apt_target (id INTEGER, name TEXT)", [])
        _db.execute("CREATE TABLE apt_junction (src INTEGER, tgt INTEGER)", [])
        _db.execute("INSERT INTO apt_target VALUES (10,'x'),(20,'y')", [])
        _db.execute("INSERT INTO apt_junction VALUES (1,10),(1,20),(2,10)", [])
        try:
            rows = _db.query(
                "SELECT apt_junction.src, apt_target.id, apt_target.name "
                "FROM apt_junction "
                "JOIN apt_target ON apt_junction.tgt = apt_target.id "
                "WHERE apt_junction.src = ANY(ARRAY[$1, $2]) "
                "ORDER BY apt_junction.src, apt_target.id",
                [1, 2],
            )
            assert [(r[0], r[2]) for r in rows] == [
                (1, "x"),
                (1, "y"),
                (2, "x"),
            ]
        finally:
            _db.execute("DROP TABLE IF EXISTS apt_junction", [])
            _db.execute("DROP TABLE IF EXISTS apt_target", [])


class TestNonArrayShapesUntouched:
    """Scalar / non-ARRAY shapes must keep the historical unknown-typed
    behavior (no OID declaration), including text-vs-int coercion."""

    def test_scalar_int_lookup(self):
        rows = _db.query("SELECT label FROM apt_typing WHERE id = $1", [2])
        assert [r[0] for r in rows] == ["b"]

    def test_scalar_text_lookup(self):
        rows = _db.query("SELECT id FROM apt_typing WHERE label = $1", ["a"])
        assert [r[0] for r in rows] == [1]

    def test_text_col_compared_to_int_still_coerces(self):
        # No ARRAY[] → params stay unknown-typed → PostgreSQL coerces the
        # int to text for the comparison (historical behavior preserved).
        rows = _db.query("SELECT id FROM apt_typing WHERE label = $1", [1])
        assert rows == []  # no text label equals "1", but the query must not error

    def test_any_single_array_param_infers_from_column(self):
        # `= ANY($1)` with a single array param (not ARRAY[...]) already
        # infers the element type from the column; must remain correct.
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE id = ANY($1) ORDER BY id",
            [[1, 3]],
        )
        assert [r[0] for r in rows] == [1, 3]


class TestMixedArrayAndNonArrayPositions:
    """Only the $N positions LEXICALLY inside ARRAY[...] get a declared OID;
    every other position stays unknown-typed. Regression for over-declaration
    (F1): gating on "SQL contains ARRAY[ anywhere" wrongly typed unrelated
    int params, breaking e.g. `text_col = <int>` alongside an ARRAY literal."""

    def test_int_on_text_col_with_unrelated_array_literal(self):
        # $1 (Python int) compares to a TEXT column; the query ALSO contains
        # an unrelated ARRAY[...] literal with no params. $1 must stay
        # unknown-typed (text coercion), not become bigint.
        rows = _db.query(
            "SELECT id FROM apt_typing "
            "WHERE label = $1 AND 'x' = ANY(ARRAY['x','y']) ORDER BY id",
            [1],  # int on a text column
        )
        assert rows == []  # no error; coerces int→text, matches nothing

    def test_int_inside_array_with_text_param_outside(self):
        # $1 text OUTSIDE the array (text column), $2/$3 int INSIDE ARRAY[...].
        # $1 stays unknown (coerces), $2/$3 get int8 so the ARRAY types right.
        rows = _db.query(
            "SELECT id FROM apt_typing "
            "WHERE label = $1 OR id = ANY(ARRAY[$2, $3]) ORDER BY id",
            ["a", 2, 3],
        )
        assert [r[0] for r in rows] == [1, 2, 3]  # 'a'→row1, ids 2,3→rows 2,3

    def test_array_bracket_inside_string_literal_ignored(self):
        # A literal 'ARRAY[' inside a quoted string must NOT arm array typing,
        # so the int $1 on the text column stays unknown-typed.
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE label = $1 OR label = 'ARRAY[x]' "
            "ORDER BY id",
            [2],  # int on text column
        )
        assert rows == []  # no error; nothing equals '2' or 'ARRAY[x]'

    def test_text_param_inside_array_still_works(self):
        # All-string ARRAY[...] declares no concrete OID (any=false) → stays on
        # the historical path; must still work.
        rows = _db.query(
            "SELECT id FROM apt_typing WHERE label = ANY(ARRAY[$1, $2]) ORDER BY id",
            ["a", "b"],
        )
        assert [r[0] for r in rows] == [1, 2]
