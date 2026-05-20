"""
Round-11 ORM query-EXECUTION correctness regressions.

Covers four confirmed/verified bugs fixed in hyperdjango/query.py and
hyperdjango/database.py:

  1. prefetch_related() + .cache() served STALE related objects because the
     cache key was versioned only by the main (+ select_related) tables — a
     child insert bumped the child table's version but not any table the key
     depended on, so the fully-hydrated parent stayed cached until TTL.
     FIX: _get_involved_tables() now folds in every prefetch_related / M2M
     target table (or returns None → cache skipped when unresolvable).

  3. annotate(<col>=...) with an alias equal to a real model column silently
     clobbered the column (dict keeps one key). FIX: reject at annotate() time.

  4. _prefetch_m2m() projected the junction source column WITHOUT an alias, so
     a name collision with a target column corrupted the grouping key and the
     hydrated target. FIX: alias it `AS __hyper_m2m_src` and read it by that key.

  LOW: count() ignored a slice (_limit/_offset); now clamps like Django.

Most tests are PURE-PYTHON (no live DB). A DB-backed end-to-end prefetch+cache
invalidation test runs only if DATABASE_URL is reachable.

Usage:
    uv run python scripts/test_query_exec_r11.py
    uv run hyper-test query_exec_r11
"""

# hyper-test: unit

import asyncio
import inspect
import os
import sys
import traceback
from typing import ClassVar

from hyperdjango.expressions import Count
from hyperdjango.models import Field, ManyToManyField, Model
from hyperdjango.query import QuerySet
from hyperdjango.query_cache import QueryCacheManager, set_query_cache

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class R11Author(Model):
    class Meta:
        table = "test_r11_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class R11Book(Model):
    class Meta:
        table = "test_r11_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    author_id: int = Field(foreign_key=R11Author)


# Make the reverse-relation resolvable by a friendly prefetch name.
R11Author._reverse_relations = {"books": R11Book}


# M2M with a source-column name that COLLIDES with a target column name.
# source table "test_r11_users" -> source_col "user_id"; the target model
# below deliberately declares its own "user_id" column (an owner FK), so the
# pre-fix un-aliased junction SELECT produced two "user_id" keys.
class R11Group(Model):
    class Meta:
        table = "test_r11_groups"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    # Decoy column whose name equals the M2M source_col ("user_id").
    user_id: int = Field(default=0)


class R11User(Model):
    class Meta:
        table = "test_r11_users"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    groups: ClassVar = ManyToManyField("test_r11_groups")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDB:
    """Minimal async DB stub: returns canned rows / value and records SQL."""

    def __init__(self, rows=None, val=None):
        self.rows = rows if rows is not None else []
        self.val = val
        self.queries = []

    async def query(self, sql, *args):
        self.queries.append((sql, args))
        return self.rows

    async def query_val(self, sql, *args):
        self.queries.append((sql, args))
        return self.val


# ---------------------------------------------------------------------------
# #1 prefetch_related + cache invalidation
# ---------------------------------------------------------------------------


@test("#1 involved-tables includes reverse-FK prefetch target")
def test_involved_tables_reverse_fk():
    qs = QuerySet(R11Author).prefetch_related("books")
    tables = qs._get_involved_tables()
    assert tables is not None, "expected resolvable tables"
    assert R11Author._meta.table in tables
    assert R11Book._meta.table in tables, (
        f"prefetch child table missing from {tables!r} — cache key would not "
        f"version against child inserts (STALE bug)"
    )


@test("#1 involved-tables includes M2M junction + target tables")
def test_involved_tables_m2m():
    qs = QuerySet(R11User).prefetch_related("groups")
    tables = qs._get_involved_tables()
    assert tables is not None
    desc = R11User.__dict__["groups"]
    assert desc._junction_table in tables, tables
    assert R11Group._meta.table in tables, tables


@test("#1 unresolvable prefetch -> None (cache skipped, fail-safe)")
def test_involved_tables_unresolvable():
    qs = QuerySet(R11Author).prefetch_related("nonexistent_relation")
    assert qs._get_involved_tables() is None


@test("#1 child insert changes the multi-table cache key (was invariant)")
def test_child_insert_bumps_key():
    qc = QueryCacheManager()
    qc.register_model(R11Book)  # Book FK -> author dependency
    qs = QuerySet(R11Author).prefetch_related("books").cache(60)
    tables = qs._get_involved_tables()
    sql = "SELECT * FROM test_r11_authors"

    key_before = qc.make_multi_table_key(tables, sql, ())
    # Simulate a child (Book) insert -> post_save -> invalidate_row(books).
    qc.invalidate_row(R11Book._meta.table, 1)
    key_after = qc.make_multi_table_key(tables, sql, ())
    assert key_before != key_after, (
        "child insert must change the cache key so the cached parent misses"
    )

    # Contrast: the OLD single-table (author-only) key would NOT change — the
    # exact staleness the fix removes.
    a_key1 = qc.make_key(R11Author._meta.table, sql, ())
    qc.invalidate_row(R11Book._meta.table, 2)
    a_key2 = qc.make_key(R11Author._meta.table, sql, ())
    assert a_key1 == a_key2, "sanity: author-only key is blind to book writes"


@test("#1 end-to-end: cached prefetch result re-fetches after child insert")
async def test_prefetch_cache_reads_fresh_after_insert():
    qc = QueryCacheManager()
    set_query_cache(qc)
    qc.register_model(R11Book)

    author_rows = [{"id": 1, "name": "Ada"}]
    book_rows = [{"id": 10, "title": "T1", "author_id": 1}]

    class RoutingDB:
        def __init__(self):
            self.select_count = 0

        async def query(self, sql, *args):
            if "FROM test_r11_books" in sql:
                return list(book_rows)
            self.select_count += 1
            return list(author_rows)

        async def query_val(self, sql, *args):
            return None

    db = RoutingDB()

    async def run():
        return await (
            QuerySet(R11Author).prefetch_related("books").cache(60).using(db).all()
        )

    r1 = await run()
    assert r1[0].books[0].id == 10
    n_after_first = db.select_count

    # Second call with no writes -> cache hit -> no new author SELECT.
    await run()
    assert db.select_count == n_after_first, "expected cache HIT (no re-select)"

    # A new book arrives.
    book_rows.append({"id": 11, "title": "T2", "author_id": 1})
    qc.invalidate_row(R11Book._meta.table, 11)

    r3 = await run()
    assert db.select_count == n_after_first + 1, (
        "expected cache MISS after child insert (STALE-bug regression)"
    )
    assert {b.id for b in r3[0].books} == {10, 11}


# ---------------------------------------------------------------------------
# #3 annotate alias colliding with a real column
# ---------------------------------------------------------------------------


@test("#3 annotate() alias == column name raises ValueError")
def test_annotate_column_collision():
    try:
        QuerySet(R11Author).annotate(name=Count("id"))
    except ValueError as e:
        assert "conflicts" in str(e), str(e)
        assert "name" in str(e)
    else:
        raise AssertionError("expected ValueError for annotate(name=...)")


@test("#3 non-colliding annotate alias still works")
def test_annotate_ok():
    qs = QuerySet(R11Author).annotate(book_count=Count("id"))
    assert "book_count" in qs._annotations


# ---------------------------------------------------------------------------
# #4 M2M prefetch source-column alias
# ---------------------------------------------------------------------------


@test("#4 _prefetch_m2m aliases source col & groups by the true source PK")
async def test_m2m_source_alias_grouping():
    desc = R11User.__dict__["groups"]
    desc._ensure_target()
    source_col = desc._source_col  # "user_id" — collides with R11Group.user_id

    # Rows as pg.zig would yield AFTER the alias fix: the junction source PK is
    # under "__hyper_m2m_src"; the target's own colliding "user_id" is distinct.
    rows = [
        {"__hyper_m2m_src": 1, "id": 100, "name": "G1", "user_id": 999},
        {"__hyper_m2m_src": 2, "id": 200, "name": "G2", "user_id": 888},
    ]
    db = FakeDB(rows=rows)

    u1 = R11User.from_record({"id": 1, "name": "A"})
    u2 = R11User.from_record({"id": 2, "name": "B"})
    qs = QuerySet(R11User)
    await qs._prefetch_m2m([u1, u2], "groups", desc, db)

    # SQL must alias the source column.
    sql = db.queries[0][0]
    assert "AS __hyper_m2m_src" in sql, sql
    assert source_col == "user_id", source_col

    # Grouping keyed on the parent PK (1/2), NOT the colliding target user_id.
    g1 = u1._groups_cache
    g2 = u2._groups_cache
    assert [g.id for g in g1] == [100], g1
    assert [g.id for g in g2] == [200], g2
    # Target's own colliding column survived intact (not overwritten by source).
    assert g1[0].user_id == 999
    assert g2[0].user_id == 888


# ---------------------------------------------------------------------------
# LOW: count() honors slice
# ---------------------------------------------------------------------------


@test("LOW count() clamps to slice bounds (Django parity)")
async def test_count_slice():
    # Raw COUNT(*) returns 100; slicing narrows the counted set.
    db = FakeDB(val=100)

    async def cnt(qs):
        return await qs.using(db).count()

    assert await cnt(QuerySet(R11Author)) == 100
    assert await cnt(QuerySet(R11Author).limit(10)) == 10  # [:10]
    # offset 90 -> 10 remaining, limit 20 -> min(10,20)=10  ([90:110])
    q = QuerySet(R11Author)
    q._offset, q._limit = 90, 20
    assert await cnt(q) == 10
    # offset 5, limit 10 -> [5:15] -> 10
    q2 = QuerySet(R11Author)
    q2._offset, q2._limit = 5, 10
    assert await cnt(q2) == 10
    # offset beyond total -> 0
    q3 = QuerySet(R11Author)
    q3._offset = 200
    assert await cnt(q3) == 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    all_tests = [
        obj
        for _, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print("\n═══ query_exec_r11 ═══")
    for t in all_tests:
        await t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)
    return RESULTS["failed"] == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
