"""
Tests for transparent query cache with write-through invalidation.

Tests QueryCacheManager, version-based invalidation, FK dependency cascading,
signal-driven auto-invalidation, QuerySet.cache() integration, Meta.cache_ttl,
multi-table JOIN cache keys, cache warming, and enable/disable toggle.

Usage:
    uv run hyper-test query_cache
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.cache import LocMemCache
from hyperdjango.database import Database, get_db, set_db
from hyperdjango.models import Field, Model
from hyperdjango.query_cache import (
    CacheStats,
    DependencyTracker,
    QueryCacheManager,
    configure_query_cache,
    get_query_cache,
    set_query_cache,
)

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
# Test models (module-level)
# ---------------------------------------------------------------------------


class QcAuthor(Model):
    class Meta:
        table = "test_qc_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class QcBook(Model):
    class Meta:
        table = "test_qc_books"
        cache_ttl = 120  # Default 2-minute cache

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    author_id: int = Field(foreign_key=QcAuthor)
    price: int = Field(default=0)


# ---------------------------------------------------------------------------
# DB setup / teardown
# ---------------------------------------------------------------------------

CREATE_TABLES = [
    "DROP TABLE IF EXISTS test_qc_books CASCADE",
    "DROP TABLE IF EXISTS test_qc_authors CASCADE",
    """CREATE TABLE test_qc_authors (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL
    )""",
    """CREATE TABLE test_qc_books (
        id SERIAL PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        author_id INTEGER REFERENCES test_qc_authors(id) ON DELETE CASCADE,
        price INTEGER DEFAULT 0
    )""",
]


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    for sql in CREATE_TABLES:
        await db.execute(sql)
    return db


async def teardown_db(db):
    for sql in [
        "DROP TABLE IF EXISTS test_qc_books CASCADE",
        "DROP TABLE IF EXISTS test_qc_authors CASCADE",
    ]:
        await db.execute(sql)
    await db.disconnect()


async def clean_data():
    """Clean test data between DB tests."""
    db = get_db()
    await db.execute("DELETE FROM test_qc_books")
    await db.execute("DELETE FROM test_qc_authors")
    get_query_cache().clear()


# ---------------------------------------------------------------------------
# Unit Tests: CacheStats
# ---------------------------------------------------------------------------


@test("CacheStats: initial zeroes")
def test_cache_stats_initial():
    stats = CacheStats()
    assert stats.hits == 0
    assert stats.misses == 0
    assert stats.total_requests == 0
    assert stats.hit_rate == 0.0


@test("CacheStats: hit rate calculation")
def test_cache_stats_hit_rate():
    stats = CacheStats()
    stats.hits = 7
    stats.misses = 3
    assert stats.total_requests == 10
    assert abs(stats.hit_rate - 0.7) < 0.001
    stats.reset()
    assert stats.hits == 0
    assert stats.hit_rate == 0.0


# ---------------------------------------------------------------------------
# Unit Tests: DependencyTracker
# ---------------------------------------------------------------------------


@test("DependencyTracker: FK dependencies")
def test_dependency_tracker():
    dt = DependencyTracker()
    dt.register_dependency("books", "authors")
    dt.register_dependency("orders", "users")
    dt.register_dependency("reviews", "books")

    assert dt.get_dependents("authors") == {"books"}
    assert dt.get_dependents("books") == {"reviews"}
    assert dt.get_dependents("users") == {"orders"}
    assert dt.get_dependents("nonexistent") == set()

    affected = dt.get_all_affected_tables("authors")
    assert affected == {"authors", "books"}

    affected = dt.get_all_affected_tables("books")
    assert affected == {"books", "reviews"}

    dt.clear()
    assert dt.get_dependents("authors") == set()


# ---------------------------------------------------------------------------
# Unit Tests: QueryCacheManager
# ---------------------------------------------------------------------------


@test("Manager: basic get/set")
def test_manager_get_set():
    mgr = QueryCacheManager(default_ttl=60)
    key = mgr.make_key("users", "SELECT * FROM users", ())
    assert mgr.get(key) is None
    assert mgr.stats.misses == 1

    mgr.set(key, [{"id": 1, "name": "Alice"}])
    result = mgr.get(key)
    assert result == [{"id": 1, "name": "Alice"}]
    assert mgr.stats.hits == 1
    assert mgr.stats.sets == 1


@test("Manager: version-based invalidation")
def test_manager_version_invalidation():
    mgr = QueryCacheManager(default_ttl=60)
    key = mgr.make_key("users", "SELECT * FROM users", ())
    mgr.set(key, [{"id": 1}])
    assert mgr.get(key) is not None

    mgr.invalidate_table("users")

    # New key differs (version bumped)
    new_key = mgr.make_key("users", "SELECT * FROM users", ())
    assert new_key != key
    assert mgr.get(new_key) is None


@test("Manager: row invalidation")
def test_manager_row_invalidation():
    mgr = QueryCacheManager(default_ttl=60)
    key = mgr.make_key("users", "SELECT * FROM users WHERE id=$1", (42,))
    mgr.set(key, [{"id": 42}])
    assert mgr.get(key) is not None

    mgr.invalidate_row("users", 42)
    assert mgr.stats.row_invalidations == 1

    new_key = mgr.make_key("users", "SELECT * FROM users WHERE id=$1", (42,))
    assert new_key != key
    assert mgr.get(new_key) is None


@test("Manager: FK cascade invalidation")
def test_manager_fk_cascade_invalidation():
    mgr = QueryCacheManager(default_ttl=60)
    mgr.dependencies.register_dependency("books", "authors")

    books_key = mgr.make_key("books", "SELECT * FROM books", ())
    mgr.set(books_key, [{"id": 1, "title": "Book1"}])
    assert mgr.get(books_key) is not None

    # Write to authors — cascades to books
    mgr.invalidate_table("authors")

    new_books_key = mgr.make_key("books", "SELECT * FROM books", ())
    assert new_books_key != books_key
    assert mgr.get(new_books_key) is None


@test("Manager: multi-table key")
def test_manager_multi_table_key():
    mgr = QueryCacheManager(default_ttl=60)
    key1 = mgr.make_multi_table_key(["books", "authors"], "SELECT ...", ())
    mgr.set(key1, [{"result": 1}])
    assert mgr.get(key1) is not None

    mgr.invalidate_table("authors")

    key2 = mgr.make_multi_table_key(["books", "authors"], "SELECT ...", ())
    assert key2 != key1
    assert mgr.get(key2) is None


@test("Manager: invalidate_all")
def test_manager_invalidate_all():
    mgr = QueryCacheManager(default_ttl=60)
    # Touch tables so they're tracked
    mgr.invalidate_table("users")
    mgr.invalidate_table("books")

    k1 = mgr.make_key("users", "SELECT 1", ())
    k2 = mgr.make_key("books", "SELECT 2", ())
    mgr.set(k1, "a")
    mgr.set(k2, "b")

    mgr.invalidate_all()

    nk1 = mgr.make_key("users", "SELECT 1", ())
    nk2 = mgr.make_key("books", "SELECT 2", ())
    assert nk1 != k1
    assert nk2 != k2


@test("Manager: invalidate_all clears version-0 tables")
def test_manager_invalidate_all_version_zero():
    # Regression: a table that was cached but NEVER invalidated stays at
    # version 0 and is absent from _table_versions. The old invalidate_all
    # only bumped tables already present, so its entries survived — a silent
    # no-op that served stale data.
    mgr = QueryCacheManager(default_ttl=60)

    key = mgr.make_key("pristine", "SELECT 1", ())
    mgr.set(key, "stale")
    assert mgr.get(key) == "stale"

    mgr.invalidate_all()

    # Old entry must be gone (backend cleared) ...
    assert mgr.get(key) is None
    # ... and a freshly-computed key for the same query must differ (generation
    # advanced), so a re-populated old key can't collide either.
    new_key = mgr.make_key("pristine", "SELECT 1", ())
    assert new_key != key


@test("Manager: disabled")
def test_manager_disabled():
    mgr = QueryCacheManager(default_ttl=60)
    mgr.enabled = False

    key = mgr.make_key("users", "SELECT 1", ())
    mgr.set(key, "data")
    assert mgr.get(key) is None
    assert mgr.stats.hits == 0
    assert mgr.stats.misses == 0


@test("Manager: warm")
def test_manager_warm():
    mgr = QueryCacheManager(default_ttl=60)
    key = mgr.make_key("products", "SELECT * FROM products", ())
    mgr.warm(key, [{"id": 1, "name": "Widget"}], ttl=300)

    result = mgr.get(key)
    assert result == [{"id": 1, "name": "Widget"}]
    assert mgr.stats.hits == 1


@test("Manager: clear resets everything")
def test_manager_clear():
    mgr = QueryCacheManager(default_ttl=60)
    key = mgr.make_key("users", "SELECT 1", ())
    mgr.set(key, "data")
    mgr.stats.hits = 10

    mgr.clear()
    assert mgr.stats.hits == 0
    assert mgr.get_table_versions() == {}


@test("Manager: repr")
def test_manager_repr():
    mgr = QueryCacheManager(default_ttl=60)
    r = repr(mgr)
    assert "QueryCacheManager" in r
    assert "LocMemCache" in r


@test("Global: get/set/configure")
def test_global_singleton():
    original = get_query_cache()

    mgr = configure_query_cache(default_ttl=30)
    assert get_query_cache() is mgr
    assert mgr.default_ttl == 30

    set_query_cache(original)


@test("Global: configure disabled")
def test_configure_disabled():
    original = get_query_cache()
    mgr = configure_query_cache(enabled=False)
    assert not mgr.enabled
    set_query_cache(original)


@test("Keys: different params → different keys")
def test_different_params_different_keys():
    mgr = QueryCacheManager()
    k1 = mgr.make_key("users", "SELECT * FROM users WHERE id=$1", (1,))
    k2 = mgr.make_key("users", "SELECT * FROM users WHERE id=$1", (2,))
    assert k1 != k2


@test("TTL: expiry works")
def test_ttl_expiry():
    # Advance the backend's clock instead of sleeping past the TTL: the sleep
    # asserted "1.1s of wall time exceeds a 1s TTL", which a loaded runner can
    # answer either way near the boundary. Moving the clock states both halves
    # exactly — inside the TTL it is still a hit, past it a miss.
    now = [1000.0]
    mgr = QueryCacheManager(
        backend=LocMemCache(max_size=100, clock=lambda: now[0]), default_ttl=60
    )
    key = mgr.make_key("users", "SELECT 1", ())
    mgr.set(key, "data", ttl=60)
    assert mgr.get(key) == "data"
    now[0] += 59
    assert mgr.get(key) == "data", "entry must survive to the last second of its TTL"
    now[0] += 2
    assert mgr.get(key) is None


@test("Versions: tracking accumulates")
def test_table_versions_tracking():
    mgr = QueryCacheManager()
    assert mgr.get_table_versions() == {}

    mgr.invalidate_table("users")
    mgr.invalidate_table("users")
    mgr.invalidate_table("books")

    versions = mgr.get_table_versions()
    assert versions["users"] == 2
    assert versions["books"] == 1


@test("Stats: accumulate correctly")
def test_stats_tracking():
    mgr = QueryCacheManager(default_ttl=60)
    key = mgr.make_key("t", "SELECT 1", ())
    mgr.get(key)  # miss
    mgr.set(key, "x")
    mgr.get(key)  # hit
    mgr.get(key)  # hit
    mgr.invalidate_table("t")
    mgr.invalidate_row("t", 1)

    assert mgr.stats.misses == 1
    assert mgr.stats.hits == 2
    assert mgr.stats.sets == 1
    assert mgr.stats.invalidations == 2
    assert mgr.stats.table_invalidations >= 1
    assert mgr.stats.row_invalidations == 1


# ---------------------------------------------------------------------------
# DB Integration Tests
# ---------------------------------------------------------------------------


@test("DB: QuerySet.cache() hit")
async def test_queryset_cache_hit():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()
    await QcBook(title="Book2", author_id=alice.id, price=20).save()

    qc.clear()

    # First query — miss
    books = await QcBook.objects.cache(ttl=60).filter(author_id=alice.id).all()
    assert len(books) == 2
    hits_before = qc.stats.hits

    # Second query — hit
    books2 = await QcBook.objects.cache(ttl=60).filter(author_id=alice.id).all()
    assert len(books2) == 2
    assert qc.stats.hits == hits_before + 1


@test("DB: invalidate on save")
async def test_queryset_cache_invalidate_on_save():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()

    qc.clear()

    books = await QcBook.objects.cache(ttl=60).all()
    assert len(books) == 1

    # Save new book → post_save signal → cache invalidation
    await QcBook(title="Book2", author_id=alice.id, price=20).save()

    books2 = await QcBook.objects.cache(ttl=60).all()
    assert len(books2) == 2  # New data!
    assert qc.stats.invalidations >= 1


@test("DB: invalidate on delete")
async def test_queryset_cache_invalidate_on_delete():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    book = QcBook(title="Book1", author_id=alice.id, price=10)
    await book.save()

    qc.clear()

    books = await QcBook.objects.cache(ttl=60).all()
    assert len(books) == 1

    await book.delete()

    books2 = await QcBook.objects.cache(ttl=60).all()
    assert len(books2) == 0


@test("DB: bulk update invalidates")
async def test_queryset_bulk_update_invalidates():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()
    await QcBook(title="Book2", author_id=alice.id, price=20).save()

    qc.clear()

    books = await QcBook.objects.cache(ttl=60).all()
    assert len(books) == 2

    await QcBook.objects.filter(author_id=alice.id).update(price=99)

    books2 = await QcBook.objects.cache(ttl=60).all()
    assert all(b.price == 99 for b in books2)


@test("DB: bulk delete invalidates")
async def test_queryset_bulk_delete_invalidates():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()

    qc.clear()

    books = await QcBook.objects.cache(ttl=60).all()
    assert len(books) == 1

    await QcBook.objects.filter(author_id=alice.id).delete()

    books2 = await QcBook.objects.cache(ttl=60).all()
    assert len(books2) == 0


@test("DB: Meta.cache_ttl auto-caches")
async def test_meta_cache_ttl():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()

    qc.clear()

    # QcBook has cache_ttl=120 — auto-cached without .cache()
    books = await QcBook.objects.all()
    hits_before = qc.stats.hits

    books2 = await QcBook.objects.all()
    assert qc.stats.hits == hits_before + 1


@test("DB: .cache(False) disables caching")
async def test_cache_false_disables():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()

    qc.clear()

    # Explicitly disable despite Meta.cache_ttl
    books = await QcBook.objects.cache(False).all()
    hits_before = qc.stats.hits
    misses_before = qc.stats.misses

    books2 = await QcBook.objects.cache(False).all()
    assert qc.stats.hits == hits_before
    assert qc.stats.misses == misses_before


@test("DB: FK dependency cascade")
async def test_fk_dependency_cascade_db():
    await clean_data()
    qc = get_query_cache()
    qc.dependencies.register_dependency("test_qc_books", "test_qc_authors")

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()

    qc.clear()
    qc.dependencies.register_dependency("test_qc_books", "test_qc_authors")

    books = await QcBook.objects.cache(ttl=60).all()
    assert len(books) == 1

    # Update author — cascades to books
    alice.name = "Alice Updated"
    await alice.save()

    books2 = await QcBook.objects.cache(ttl=60).all()
    assert len(books2) == 1
    # Should have re-queried (miss, not hit)
    assert qc.stats.misses >= 2


@test("DB: pre_save signal fires")
async def test_signal_pre_save_fires():
    from hyperdjango.signals import pre_save

    await clean_data()
    fired = []

    @pre_save.connect
    async def on_pre_save(sender, **kwargs):
        fired.append(("pre_save", sender.__name__, kwargs.get("created")))

    try:
        alice = QcAuthor(name="Alice")
        await alice.save()

        author_signals = [f for f in fired if f[1] == "QcAuthor"]
        assert len(author_signals) >= 1
        assert author_signals[0][0] == "pre_save"
    finally:
        pre_save.disconnect(on_pre_save)


@test("DB: post_delete signal fires")
async def test_signal_post_delete_fires():
    from hyperdjango.signals import post_delete

    await clean_data()
    fired = []

    @post_delete.connect
    async def on_post_delete(sender, **kwargs):
        fired.append(("post_delete", sender.__name__))

    try:
        alice = QcAuthor(name="Alice")
        await alice.save()
        await alice.delete()

        author_signals = [f for f in fired if f[1] == "QcAuthor"]
        assert len(author_signals) >= 1
    finally:
        post_delete.disconnect(on_post_delete)


@test("DB: cache warming")
async def test_cache_warming_db():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()
    await QcBook(title="Book1", author_id=alice.id, price=10).save()

    qc.clear()

    # Build cache key and warm it
    qs = QcBook.objects.cache(ttl=60)
    sql, params = qs._build_select()
    key = qc.make_key("test_qc_books", sql, tuple(params))
    qc.warm(key, [{"id": 999, "title": "Warmed", "author_id": 1, "price": 0}])

    result = await qs.all()
    assert result == [{"id": 999, "title": "Warmed", "author_id": 1, "price": 0}]
    assert qc.stats.hits >= 1


@test("DB: no cache without opt-in")
async def test_no_cache_without_opt_in():
    await clean_data()
    qc = get_query_cache()

    # QcAuthor has NO cache_ttl
    alice = QcAuthor(name="Alice")
    await alice.save()

    qc.clear()

    authors = await QcAuthor.objects.all()
    sets_before = qc.stats.sets

    authors2 = await QcAuthor.objects.all()
    assert qc.stats.sets == sets_before  # Nothing cached


@test("DB: concurrent saves all invalidate")
async def test_concurrent_invalidation():
    await clean_data()
    qc = get_query_cache()

    alice = QcAuthor(name="Alice")
    await alice.save()

    qc.clear()

    # Create 5 books concurrently
    tasks = []
    for i in range(5):

        async def create_book(n=i):
            await QcBook(title=f"Book{n}", author_id=alice.id, price=n * 10).save()

        tasks.append(create_book())

    await asyncio.gather(*tasks)

    assert qc.stats.invalidations >= 5

    books = await QcBook.objects.cache(ttl=60).all()
    assert len(books) == 5


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    # Collect all tests
    all_tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            all_tests.append(obj)

    # Separate unit vs DB tests by name
    unit_tests = [t for t in all_tests if not t.__name__.startswith("DB:")]
    db_tests = [t for t in all_tests if t.__name__.startswith("DB:")]

    print("\n═══ Unit Tests ═══")
    for t in unit_tests:
        await t()

    print("\n═══ DB Integration Tests ═══")
    try:
        db = await setup_db()
        try:
            for t in db_tests:
                await t()
        finally:
            await teardown_db(db)
    except Exception as e:
        print(f"\n  ⚠ Database connection failed ({e}), skipping integration tests")

    # Summary
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
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
