"""Tests for QuerySet.to_sql() debug chainable — task #206.

Verifies the no-DB-access compiled-SQL preview returned by
QuerySet.to_sql() across SELECT / UPDATE / DELETE kinds and across
all the recent ORM additions: Exists, with_cte, join_related.

Tests cover:
1. Plain SELECT preview
2. SELECT with WHERE filters + correct $N param numbering
3. SELECT with select_related (JOINed columns)
4. SELECT with join_related (sibling-attribute JOIN)
5. SELECT with Exists / OuterRef
6. SELECT with with_cte (recursive)
7. UPDATE preview with returning columns
8. DELETE preview
9. Error cases: bad kind, missing update_values
10. CompiledQuery.inlined() preview produces readable output
11. CompiledQuery.__str__ produces multi-line dump

Usage:
    uv run hyper-test to_sql_debug
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.expressions import Exists, OuterRef
from hyperdjango.models import Field, Model
from hyperdjango.query import CompiledQuery

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


class TsAuthor(Model):
    class Meta:
        table = "ts_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class TsBook(Model):
    class Meta:
        table = "ts_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    author_id: int = Field(foreign_key=TsAuthor)
    is_published: bool = Field(default=False)


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    for sql in [
        "DROP TABLE IF EXISTS ts_books CASCADE",
        "DROP TABLE IF EXISTS ts_authors CASCADE",
        """CREATE TABLE ts_authors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL
        )""",
        """CREATE TABLE ts_books (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            author_id INTEGER NOT NULL REFERENCES ts_authors(id),
            is_published BOOLEAN NOT NULL DEFAULT FALSE
        )""",
    ]:
        await db.execute(sql)
    return db


async def teardown_db(db):
    for tbl in ("ts_books", "ts_authors"):
        await db.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


async def main() -> int:
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    db = await setup_db()
    try:
        # ── Test 1: Plain SELECT preview ──────────────────────────────
        print("\n=== Plain SELECT to_sql() ===")

        compiled = TsBook.objects.to_sql()
        check("returns CompiledQuery", isinstance(compiled, CompiledQuery))
        check("kind is SELECT", compiled.kind == "SELECT")
        check("sql starts with SELECT", compiled.sql.startswith("SELECT"))
        check("params is empty list", compiled.params == [])

        # ── Test 2: SELECT with filter — param numbering ─────────────
        print("\n=== SELECT with filter ===")

        compiled = TsBook.objects.filter(is_published=True, title="Foo").to_sql()
        check("two-param query has 2 params", len(compiled.params) == 2)
        check("$1 in sql", "$1" in compiled.sql)
        check("$2 in sql", "$2" in compiled.sql)
        check(
            "params contain True and 'Foo'",
            True in compiled.params and "Foo" in compiled.params,
        )

        # ── Test 3: SELECT with select_related JOIN ──────────────────
        print("\n=== SELECT with select_related ===")

        compiled = TsBook.objects.select_related("author_id").to_sql()
        check("contains LEFT JOIN", "LEFT JOIN" in compiled.sql)
        check("contains ts_authors", "ts_authors" in compiled.sql)

        # ── Test 4: SELECT with join_related ─────────────────────────
        print("\n=== SELECT with join_related ===")

        compiled = TsBook.objects.join_related(author="author_id").to_sql()
        check(
            "join_related also emits LEFT JOIN",
            "LEFT JOIN" in compiled.sql,
        )

        # ── Test 5: SELECT with Exists / OuterRef ────────────────────
        print("\n=== SELECT with Exists / OuterRef ===")

        compiled = TsAuthor.objects.exclude(
            Exists(
                TsBook.objects.filter(
                    author_id=OuterRef("id"),
                    is_published=True,
                )
            )
        ).to_sql()
        check(
            "contains NOT EXISTS",
            "NOT EXISTS" in compiled.sql,
            f"sql: {compiled.sql[:200]}",
        )
        check(
            "OuterRef resolved to ts_authors.id",
            "ts_authors.id" in compiled.sql,
        )
        check(
            "Exists subquery includes is_published filter param",
            True in compiled.params,
        )

        # ── Test 6: SELECT with with_cte ─────────────────────────────
        print("\n=== SELECT with with_cte (recursive) ===")

        compiled = (
            TsAuthor.objects.values("id")
            .with_cte(
                "author_tree",
                "SELECT id FROM ts_authors WHERE id = {idx} "
                "UNION ALL "
                "SELECT a.id FROM ts_authors a JOIN author_tree t ON a.id = t.id",
                1,
                recursive=True,
            )
            .where_raw("id IN (SELECT id FROM author_tree)")
            .to_sql()
        )
        check(
            "WITH RECURSIVE prefix",
            compiled.sql.startswith("WITH RECURSIVE author_tree AS"),
            f"sql start: {compiled.sql[:80]!r}",
        )
        check(
            "CTE param numbered as $1",
            "$1" in compiled.sql,
        )
        check(
            "outer SELECT after CTE",
            "SELECT" in compiled.sql.split("AS (", 1)[1]
            if "AS (" in compiled.sql
            else False,
        )

        # ── Test 7: UPDATE preview ───────────────────────────────────
        print("\n=== UPDATE preview ===")

        compiled = TsBook.objects.filter(id=1).to_sql(
            kind="update",
            update_values={"is_published": True},
            update_returning=["id", "title"],
        )
        check("kind is UPDATE", compiled.kind == "UPDATE")
        check("starts with UPDATE", compiled.sql.startswith("UPDATE"))
        check("contains SET", "SET" in compiled.sql)
        check("contains RETURNING", "RETURNING" in compiled.sql)

        # ── Test 8: DELETE preview ───────────────────────────────────
        print("\n=== DELETE preview ===")

        compiled = TsBook.objects.filter(is_published=False).to_sql(kind="delete")
        check("kind is DELETE", compiled.kind == "DELETE")
        check("starts with DELETE FROM", compiled.sql.startswith("DELETE FROM"))

        # ── Test 9: Error cases ──────────────────────────────────────
        print("\n=== Error cases ===")

        try:
            TsBook.objects.to_sql(kind="update")
            check("update without values raises", False, "no exception")
        except ValueError as e:
            check("update without values raises ValueError", "update_values" in str(e))

        try:
            TsBook.objects.to_sql(kind="bogus")
            check("bogus kind raises", False, "no exception")
        except ValueError as e:
            check(
                "bogus kind raises ValueError", "select" in str(e) or "kind" in str(e)
            )

        # ── Test 10: inlined() preview ───────────────────────────────
        print("\n=== inlined() preview ===")

        compiled = TsBook.objects.filter(is_published=True, title="Foo").to_sql()
        inlined = compiled.inlined()
        check("inlined: $N markers gone", "$1" not in inlined and "$2" not in inlined)
        check("inlined: TRUE substituted", "TRUE" in inlined)
        check("inlined: 'Foo' substituted", "'Foo'" in inlined)

        # NULL handling
        compiled_null = TsBook.objects.filter(title=None).to_sql()
        inlined_null = compiled_null.inlined()
        check("inlined: None → NULL", "NULL" in inlined_null)

        # ── Test 11: __str__ multi-line dump ─────────────────────────
        print("\n=== str(CompiledQuery) format ===")

        compiled = TsBook.objects.filter(is_published=True).to_sql()
        s = str(compiled)
        check("str: includes -- SELECT comment", "-- SELECT" in s)
        check("str: includes 1 params", "1 params" in s)
        check("str: lists $1 = True", "$1 = True" in s)

        # ── Test 12: round-trip stability ────────────────────────────
        print("\n=== Round-trip stability ===")

        qs = TsBook.objects.filter(is_published=True).order_by("id").limit(10)
        c1 = qs.to_sql()
        c2 = qs.to_sql()
        check("identical SQL across calls", c1.sql == c2.sql)
        check("identical params across calls", c1.params == c2.params)

    finally:
        await teardown_db(db)
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All to_sql() debug tests passed!")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
