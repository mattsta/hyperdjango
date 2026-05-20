"""
Regression tests for LOW priority fixes.

Tests:
1. StaticFilesMiddleware LRU eviction (evicts oldest when cache full)
2. inspectdb topological sort (FK-referenced tables before referencing)
3. inspectdb --include-views support
4. Channel subscriber introspection

Usage:
    uv run hyper-test low_priority_fixes
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from hyperdjango.database import Database, set_db
from hyperdjango.request import Request
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

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
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# StaticFilesMiddleware LRU eviction
# ---------------------------------------------------------------------------


@test("static cache: LRU evicts oldest entries when full")
async def test_lru_eviction():
    from hyperdjango.staticfiles import StaticFilesMiddleware

    tmpdir = tempfile.mkdtemp()
    try:
        # Create 5 files, each ~100 bytes
        for i in range(5):
            path = Path(tmpdir) / f"file{i}.txt"
            path.write_text(f"Content for file {i}" + "x" * 80)

        # Cache max = 300 bytes — only holds ~3 files
        mw = StaticFilesMiddleware(
            static_dirs=[tmpdir],
            prefix="/static/",
            max_cache_bytes=300,
            use_cache=True,
        )

        async def noop(req):
            return Response(body=b"404", status=404)

        # Request files 0-4 — cache should evict oldest as it fills
        for i in range(5):
            req = Request(method="GET", path=f"/static/file{i}.txt")
            resp = await mw(req, noop)
            assert resp.status == 200

        # Cache should not exceed max_cache_bytes
        assert mw._cache_bytes <= 300, f"Cache exceeded max: {mw._cache_bytes}"

        # Most recent files should be cached, oldest evicted
        assert "file4.txt" in mw._file_cache  # Most recent
        assert "file3.txt" in mw._file_cache  # Recent
        # file0.txt was evicted (oldest)
        assert "file0.txt" not in mw._file_cache

    finally:
        shutil.rmtree(tmpdir)


@test("static cache: LRU promotes on access (recently used stays)")
async def test_lru_promote():
    from hyperdjango.staticfiles import StaticFilesMiddleware

    tmpdir = tempfile.mkdtemp()
    try:
        for i in range(4):
            path = Path(tmpdir) / f"f{i}.txt"
            path.write_text(f"Content {i}" + "x" * 80)

        mw = StaticFilesMiddleware(
            static_dirs=[tmpdir],
            prefix="/static/",
            max_cache_bytes=300,
            use_cache=True,
        )

        async def noop(req):
            return Response(body=b"404", status=404)

        # Load files 0, 1, 2
        for i in range(3):
            req = Request(method="GET", path=f"/static/f{i}.txt")
            await mw(req, noop)

        # Re-access file 0 (promotes it in LRU)
        req = Request(method="GET", path="/static/f0.txt")
        await mw(req, noop)

        # Load file 3 — should evict file 1 (least recently used), not file 0
        req = Request(method="GET", path="/static/f3.txt")
        await mw(req, noop)

        assert "f0.txt" in mw._file_cache, (
            "f0 was recently accessed, should not be evicted"
        )
        assert "f3.txt" in mw._file_cache, "f3 is newest, should be cached"

    finally:
        shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# inspectdb topological sort
# ---------------------------------------------------------------------------


@test("topological sort: referenced tables come first")
def test_topo_sort():
    from hyperdjango.cli import _topological_sort_tables
    from hyperdjango.migrations import DbConstraint, DbTable

    # authors has no FK, books references authors, reviews references books
    authors = DbTable(name="authors")
    books = DbTable(name="books")
    books.constraints.append(
        DbConstraint(name="fk", type="f", columns=["author_id"], fk_table="authors")
    )
    reviews = DbTable(name="reviews")
    reviews.constraints.append(
        DbConstraint(name="fk", type="f", columns=["book_id"], fk_table="books")
    )

    tables = {"reviews": reviews, "authors": authors, "books": books}
    sorted_names = _topological_sort_tables(tables)

    assert sorted_names.index("authors") < sorted_names.index("books")
    assert sorted_names.index("books") < sorted_names.index("reviews")


@test("topological sort: handles circular dependencies")
def test_topo_sort_circular():
    from hyperdjango.cli import _topological_sort_tables
    from hyperdjango.migrations import DbConstraint, DbTable

    a = DbTable(name="a")
    b = DbTable(name="b")
    a.constraints.append(
        DbConstraint(name="fk", type="f", columns=["b_id"], fk_table="b")
    )
    b.constraints.append(
        DbConstraint(name="fk", type="f", columns=["a_id"], fk_table="a")
    )

    tables = {"a": a, "b": b}
    sorted_names = _topological_sort_tables(tables)
    # Both should appear (no crash)
    assert set(sorted_names) == {"a", "b"}


@test("topological sort: independent tables all included")
def test_topo_sort_independent():
    from hyperdjango.cli import _topological_sort_tables
    from hyperdjango.migrations import DbTable

    tables = {
        "zebra": DbTable(name="zebra"),
        "alpha": DbTable(name="alpha"),
        "mid": DbTable(name="mid"),
    }
    sorted_names = _topological_sort_tables(tables)
    assert set(sorted_names) == {"alpha", "mid", "zebra"}


# ---------------------------------------------------------------------------
# inspectdb --include-views
# ---------------------------------------------------------------------------


@test("inspectdb: introspect views when include_views=True")
async def test_inspectdb_views():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.migrations import DatabaseIntrospector

    await db.execute("DROP VIEW IF EXISTS test_inspectdb_view CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_inspectdb_src CASCADE")
    await db.execute(
        "CREATE TABLE test_inspectdb_src (id SERIAL PRIMARY KEY, name TEXT)"
    )
    await db.execute(
        "CREATE VIEW test_inspectdb_view AS SELECT id, name FROM test_inspectdb_src"
    )

    # Without views
    snapshot_no_views = await DatabaseIntrospector.introspect(db, include_views=False)
    assert "test_inspectdb_src" in snapshot_no_views.tables
    assert "test_inspectdb_view" not in snapshot_no_views.tables

    # With views
    snapshot_with_views = await DatabaseIntrospector.introspect(db, include_views=True)
    assert "test_inspectdb_src" in snapshot_with_views.tables
    assert "test_inspectdb_view" in snapshot_with_views.tables

    await db.execute("DROP VIEW IF EXISTS test_inspectdb_view CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_inspectdb_src CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Channel subscriber introspection
# ---------------------------------------------------------------------------


@test("channels: layer.stats() aggregates all channels")
def test_channel_layer_stats():
    from hyperdjango.channels import InMemoryChannelLayer

    layer = InMemoryChannelLayer()
    ch1 = layer.channel("a")
    ch2 = layer.channel("b")
    ch1.subscribe(lambda msg: None)
    ch1.subscribe(lambda msg: None)
    ch2.subscribe(lambda msg: None)
    ch1.join("user1")

    stats = layer.stats()
    assert stats["channels"] == 2
    assert stats["total_subscribers"] == 3
    assert stats["total_presence"] == 1


@test("channels: channel.stats() returns correct info")
def test_channel_stats():
    from hyperdjango.channels import InMemoryChannelLayer

    layer = InMemoryChannelLayer()
    ch = layer.channel("test", max_history=50)
    ch.subscribe(lambda msg: None)
    ch.join("user1")

    stats = ch.stats()
    assert stats["name"] == "test"
    assert stats["subscribers"] == 1
    assert stats["presence"] == 1
    assert stats["max_history"] == 50


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nLOW Priority Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
