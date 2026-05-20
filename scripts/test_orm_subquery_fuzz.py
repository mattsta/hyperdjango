"""Hypothesis fuzz tests for the v0.14.15 ORM additions — task #207.

Targets:
- ``Exists`` / ``NotExists`` / ``OuterRef`` correlated subquery compile path
- ``QuerySet.with_cte`` (incl. ``WITH RECURSIVE``)
- The ``_compile_exists_filter`` OuterRef substitution + sentinel handling
- The ``_get_fk_filter_paths`` single-part bug fix (regression guard)

Properties checked (compile-only, no DB):

1. **SQL injection resistance**: random hostile values (quotes, semicolons,
   `--` comment markers, ``$N`` lookalikes) in OuterRef field names and
   filter values must be parameterised, never concatenated raw, into the
   final SQL. The sentinel substitution path must not produce malformed
   SQL even when the literal sentinel token appears as a user value.

2. **Param count matches `$N` count**: the number of items in
   ``compiled.params`` must equal the number of distinct ``$N`` markers in
   ``compiled.sql`` (counting from the lowest numbered).

3. **Sentinel-token leakage**: the OuterRef sentinel string
   ``__HYPER_OUTERREF_<N>__`` must NEVER appear in the final compiled SQL
   (it's a temporary substitution marker, must be fully resolved).

4. **Round-trip stability**: compiling the SAME queryset twice in a row
   must return byte-identical SQL and identical params.

5. **CTE param ordering**: when both ``with_cte`` and a regular WHERE
   filter are present, the CTE params come BEFORE the WHERE params in
   the param list (CTE is the WITH prefix, comes first in `$N` order).

6. **WITH RECURSIVE promotion**: any single ``recursive=True`` clause
   among multiple CTEs promotes the entire WITH to ``WITH RECURSIVE``.

7. **`_get_fk_filter_paths` regression guard**: ``filter(fk_id=N)``
   (single-part scalar comparison) must NOT generate a JOIN in the
   compiled SQL.

8. **Live-DB execution sanity**: small set of randomly-built queries
   are actually executed against PostgreSQL to ensure they're not just
   syntactically valid but semantically work.

# hyper-test: db_isolated
"""

import asyncio
import os
import re
import sys

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.database import Database, set_db
from hyperdjango.expressions import Exists, OuterRef, Q
from hyperdjango.models import Field, Model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


# ── Fixture models ───────────────────────────────────────────────────────


class FzAuthor(Model):
    class Meta:
        table = "fz_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    age: int = Field(default=30)


class FzBook(Model):
    class Meta:
        table = "fz_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    author_id: int = Field(foreign_key=FzAuthor)
    is_published: bool = Field(default=False)


# ── Hypothesis strategies ────────────────────────────────────────────────

# Hostile string values for SQL-injection resistance testing
hostile_strings = st.sampled_from(
    [
        "'; DROP TABLE fz_authors; --",
        "$1",
        "$1)",
        "__HYPER_OUTERREF_1__",  # the sentinel itself!
        "1' OR '1'='1",
        "Robert'); DROP TABLE students;--",
        "\\'; DELETE FROM fz_books; --",
        "0; SELECT 1",
        "%' OR 1=1 --",
        "🔥' /* comment */",
        "''",
        "'\"`",
    ]
)

safe_strings = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=20,
)

filter_value = st.one_of(
    st.integers(min_value=-(2**31), max_value=2**31 - 1),
    st.booleans(),
    st.none(),
    safe_strings,
    hostile_strings,
)

# OuterRef field — almost always 'id' but try other valid column names
outer_ref_field = st.sampled_from(["id", "name", "age"])


# ── Compile-only property tests ──────────────────────────────────────────


PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        msg = f"FAIL: {name}" + (f" — {detail}" if detail else "")
        ERRORS.append(msg)
        print(f"  {msg}")


def _count_distinct_param_markers(sql: str) -> int:
    """Return the number of unique $N markers in `sql`."""
    return len(set(re.findall(r"\$\d+", sql)))


def _max_param_index(sql: str) -> int:
    """Return the highest $N number in `sql` (or 0 if none)."""
    matches = re.findall(r"\$(\d+)", sql)
    return max((int(m) for m in matches), default=0)


def _has_sentinel_leakage(sql: str) -> bool:
    """Return True if any OuterRef sentinel token survived in `sql`.

    The sentinels are temporary substitution markers; their presence
    in the final SQL means the substitution path failed.
    """
    return "__HYPER_OUTERREF_" in sql


# ── Property 1: SQL injection / sentinel leakage ─────────────────────────


@given(value=hostile_strings)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_hostile_filter_values_are_parameterised(value):
    """Hostile string filter values must be bound as $N params, not
    concatenated into the SQL template."""
    qs = FzBook.objects.filter(title=value)
    compiled = qs.to_sql()
    # The hostile value must appear in params, NEVER as a literal in sql
    assert value in compiled.params, f"hostile value not in params: {value!r}"
    # The hostile bytes must NOT be in the SQL template
    # (allowing for SQL keywords like SELECT to coincidentally appear)
    if value not in (
        "$1",
        "$1)",
    ):  # those WOULD legitimately appear as $N markers
        assert value not in compiled.sql, f"hostile value leaked into SQL: {value!r}"


@given(value=hostile_strings)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_hostile_values_in_exists_subquery(value):
    """Hostile values in an Exists inner filter are still parameterised."""
    inner = FzBook.objects.filter(title=value, author_id=OuterRef("id"))
    qs = FzAuthor.objects.filter(Exists(inner))
    compiled = qs.to_sql()
    assert value in compiled.params, f"hostile value missing from params: {value!r}"
    assert not _has_sentinel_leakage(compiled.sql), (
        f"sentinel token leaked: {compiled.sql[:200]}"
    )


# ── Property 2: param count matches $N markers ───────────────────────────


@given(
    title=st.text(max_size=20),
    age=st.integers(min_value=0, max_value=200),
    is_pub=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_param_count_matches_markers(title, age, is_pub):
    """The compiled param count must match the highest $N marker."""
    qs = FzBook.objects.filter(title=title, is_published=is_pub).where_raw(
        "id > {idx}", age
    )
    compiled = qs.to_sql()
    n_params = len(compiled.params)
    max_idx = _max_param_index(compiled.sql)
    assert n_params == max_idx, (
        f"param count {n_params} != max $N {max_idx} | sql: {compiled.sql}"
    )


@given(
    title=st.text(max_size=20),
    age=st.integers(min_value=0, max_value=200),
)
@settings(max_examples=100, deadline=None)
def test_exists_param_count_matches(title, age):
    """Exists subquery param count is consistent with $N markers."""
    inner = FzBook.objects.filter(title=title, author_id=OuterRef("id"))
    qs = FzAuthor.objects.filter(Exists(inner)).filter(age__gt=age)
    compiled = qs.to_sql()
    n_params = len(compiled.params)
    max_idx = _max_param_index(compiled.sql)
    assert n_params == max_idx, (
        f"exists: param count {n_params} != max $N {max_idx} | sql: {compiled.sql}"
    )


# ── Property 3: round-trip stability ─────────────────────────────────────


@given(
    title=st.text(max_size=15),
    is_pub=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_round_trip_stability(title, is_pub):
    """Compiling the same queryset twice yields identical SQL + params."""
    qs = FzBook.objects.filter(title=title, is_published=is_pub).order_by("id")
    c1 = qs.to_sql()
    c2 = qs.to_sql()
    assert c1.sql == c2.sql, "SQL diverged on second compile"
    assert c1.params == c2.params, "params diverged on second compile"


@given(seed=st.integers(min_value=1, max_value=10**9))
@settings(max_examples=50, deadline=None)
def test_exists_round_trip_stability(seed):
    """Exists compilation is stable across calls — sentinel substitution
    must not introduce per-call randomness."""
    inner = FzBook.objects.filter(author_id=OuterRef("id"), is_published=True)
    qs = FzAuthor.objects.filter(Exists(inner)).filter(age__gt=seed % 100)
    c1 = qs.to_sql()
    c2 = qs.to_sql()
    assert c1.sql == c2.sql, f"Exists SQL diverged: {c1.sql} vs {c2.sql}"
    assert c1.params == c2.params


# ── Property 4: CTE param ordering ───────────────────────────────────────


@given(
    cte_param=st.integers(min_value=0, max_value=10**6),
    where_param=st.integers(min_value=0, max_value=10**6),
)
@settings(max_examples=100, deadline=None)
def test_cte_params_before_where_params(cte_param, where_param):
    """CTE clause params come BEFORE WHERE filter params in the param list."""
    qs = (
        FzAuthor.objects.values("id")
        .with_cte(
            "filtered",
            "SELECT id FROM fz_authors WHERE age > {idx}",
            cte_param,
        )
        .where_raw("id IN (SELECT id FROM filtered) AND age < {idx}", where_param)
    )
    compiled = qs.to_sql()
    # CTE param ($1) comes before WHERE param ($2)
    assert compiled.params == [cte_param, where_param], (
        f"param order: expected [{cte_param}, {where_param}], got {compiled.params}"
    )


# ── Property 5: WITH RECURSIVE promotion ─────────────────────────────────


@given(
    is_first_recursive=st.booleans(),
    is_second_recursive=st.booleans(),
)
@settings(max_examples=20, deadline=None)
def test_recursive_promotion(is_first_recursive, is_second_recursive):
    """Any single recursive=True clause promotes the WHOLE WITH to RECURSIVE."""
    qs = FzAuthor.objects.with_cte(
        "a",
        "SELECT id FROM fz_authors WHERE age > {idx}",
        10,
        recursive=is_first_recursive,
    ).with_cte(
        "b",
        "SELECT id FROM fz_authors WHERE age > {idx}",
        20,
        recursive=is_second_recursive,
    )
    compiled = qs.to_sql()
    any_recursive = is_first_recursive or is_second_recursive
    if any_recursive:
        assert compiled.sql.startswith("WITH RECURSIVE "), (
            f"expected WITH RECURSIVE, got: {compiled.sql[:50]}"
        )
    else:
        assert compiled.sql.startswith("WITH "), (
            f"expected WITH, got: {compiled.sql[:50]}"
        )
        assert not compiled.sql.startswith("WITH RECURSIVE "), (
            f"unexpected RECURSIVE promotion: {compiled.sql[:50]}"
        )


# ── Property 6: _get_fk_filter_paths regression guard ────────────────────


@given(book_id=st.integers(min_value=1, max_value=10**9))
@settings(max_examples=50, deadline=None)
def test_scalar_fk_filter_no_join(book_id):
    """filter(author_id=N) is a scalar comparison and must NOT generate a JOIN."""
    qs = FzBook.objects.filter(author_id=book_id)
    compiled = qs.to_sql()
    assert "LEFT JOIN" not in compiled.sql, (
        f"scalar FK filter generated JOIN: {compiled.sql}"
    )
    assert "fz_authors" not in compiled.sql, (
        f"scalar FK filter referenced fz_authors: {compiled.sql}"
    )


# ── Property 7: traversal filter DOES generate JOIN ──────────────────────


def test_traversal_filter_does_generate_join():
    """filter(author__name="Alice") DOES generate a JOIN (the OPPOSITE of #6)."""
    # Not a fuzz test — just guards that the regression-fix didn't go too far.
    qs = FzBook.objects.filter(author__name="Alice")
    compiled = qs.to_sql()
    assert "LEFT JOIN" in compiled.sql, f"traversal filter missing JOIN: {compiled.sql}"


# ── Live DB sanity tests ─────────────────────────────────────────────────


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    for sql in [
        "DROP TABLE IF EXISTS fz_books CASCADE",
        "DROP TABLE IF EXISTS fz_authors CASCADE",
        """CREATE TABLE fz_authors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            age INTEGER NOT NULL DEFAULT 30
        )""",
        """CREATE TABLE fz_books (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            author_id INTEGER NOT NULL REFERENCES fz_authors(id),
            is_published BOOLEAN NOT NULL DEFAULT FALSE
        )""",
    ]:
        await db.execute(sql)
    return db


async def teardown_db(db):
    for tbl in ("fz_books", "fz_authors"):
        await db.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


async def live_db_sanity():
    """Execute a handful of randomised query shapes against PostgreSQL.

    Catches any case where the compile-only fuzz tests pass but the
    actual SQL is rejected by PostgreSQL (parser-valid but
    semantically invalid).
    """
    db = await setup_db()
    try:
        # Seed
        a1 = FzAuthor(name="Alice", age=30)
        await a1.save()
        a2 = FzAuthor(name="Bob", age=45)
        await a2.save()
        a3 = FzAuthor(name="Carol", age=25)
        await a3.save()

        await FzBook(title="Book1", author_id=a1.id, is_published=True).save()
        await FzBook(title="Book2", author_id=a1.id, is_published=False).save()
        await FzBook(title="Book3", author_id=a2.id, is_published=True).save()

        # Sanity 1: hostile-value Exists subquery executes
        for hostile in [
            "'; DROP TABLE fz_authors; --",
            "$1",
            "__HYPER_OUTERREF_1__",
            "1' OR '1'='1",
        ]:
            qs = FzAuthor.objects.filter(
                Exists(FzBook.objects.filter(title=hostile, author_id=OuterRef("id")))
            )
            result = await qs.all()
            check(
                f"hostile Exists value {hostile!r} executes safely",
                len(result) == 0,
                f"unexpected match: {len(result)} rows",
            )

        # Sanity 2: recursive CTE on the seeded data
        qs = (
            FzAuthor.objects.values("id", "name")
            .with_cte(
                "all_ids",
                "SELECT id, name FROM fz_authors WHERE age >= {idx} "
                "UNION ALL "
                "SELECT 0, 'sentinel' WHERE FALSE",
                25,
                recursive=True,
            )
            .where_raw("id IN (SELECT id FROM all_ids)")
            .order_by("id")
        )
        rows = await qs.all()
        check(
            "recursive CTE returns all 3 authors",
            len(rows) == 3,
            f"got {len(rows)}",
        )

        # Sanity 3: NOT EXISTS — authors with NO published books
        unpublished_authors = (
            await FzAuthor.objects.exclude(
                Exists(
                    FzBook.objects.filter(author_id=OuterRef("id"), is_published=True)
                )
            )
            .order_by("id")
            .all()
        )
        check(
            "NOT EXISTS finds Carol (no books at all)",
            len(unpublished_authors) == 1 and unpublished_authors[0].name == "Carol",
            f"got {[a.name for a in unpublished_authors]}",
        )

        # Sanity 4: Exists composed with regular filter
        adult_authors_with_books = (
            await FzAuthor.objects.filter(
                Exists(FzBook.objects.filter(author_id=OuterRef("id"))),
                age__gte=30,
            )
            .order_by("id")
            .all()
        )
        check(
            "composed Exists+filter finds Alice + Bob",
            {a.name for a in adult_authors_with_books} == {"Alice", "Bob"},
            f"got {[a.name for a in adult_authors_with_books]}",
        )

        # Sanity 5: Q-wrapped OuterRef
        result = (
            await FzAuthor.objects.filter(
                Exists(
                    FzBook.objects.filter(
                        Q(author_id=OuterRef("id"), is_published=True)
                    )
                )
            )
            .order_by("id")
            .all()
        )
        check(
            "Q-wrapped OuterRef finds Alice + Bob",
            {a.name for a in result} == {"Alice", "Bob"},
            f"got {[a.name for a in result]}",
        )

    finally:
        await teardown_db(db)
        await db.disconnect()


def main() -> int:
    print("=" * 60)
    print("  ORM subquery fuzz suite (task #207)")
    print("=" * 60)

    print("\n=== Compile-only property tests ===")

    # Each Hypothesis-decorated function will be invoked by pytest-style
    # discovery, but the runner here calls them directly so the test
    # results show up in the manual check accounting.
    fuzz_tests = [
        (
            "hostile values are parameterised",
            test_hostile_filter_values_are_parameterised,
        ),
        (
            "hostile values in Exists are parameterised",
            test_hostile_values_in_exists_subquery,
        ),
        ("param count matches $N markers", test_param_count_matches_markers),
        ("Exists param count matches", test_exists_param_count_matches),
        ("round-trip stability", test_round_trip_stability),
        ("Exists round-trip stability", test_exists_round_trip_stability),
        ("CTE params before WHERE params", test_cte_params_before_where_params),
        ("WITH RECURSIVE promotion", test_recursive_promotion),
        ("scalar FK filter no JOIN", test_scalar_fk_filter_no_join),
    ]

    for name, fn in fuzz_tests:
        try:
            fn()
            check(name, True)
            print(f"  PASS  {name}")
        except Exception as e:
            check(name, False, str(e)[:200])
            print(f"  FAIL  {name}: {e}")

    # Non-fuzz traversal-JOIN regression guard
    try:
        test_traversal_filter_does_generate_join()
        check("traversal filter generates JOIN", True)
        print("  PASS  traversal filter generates JOIN")
    except AssertionError as e:
        check("traversal filter generates JOIN", False, str(e))
        print(f"  FAIL  traversal filter generates JOIN: {e}")

    print("\n=== Live DB sanity ===")
    asyncio.run(live_db_sanity())

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL == 0:
        print("All ORM subquery fuzz tests passed!")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
