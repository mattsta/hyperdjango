#!/usr/bin/env python3
"""Test cache framework — LocMemCache + DatabaseCache.

Tests:
1. LocMemCache CRUD (get, set, delete, clear, has)
2. LocMemCache TTL expiry
3. LocMemCache LRU eviction
4. LocMemCache get_or_set
5. DatabaseCache CRUD
6. DatabaseCache TTL expiry
7. DatabaseCache cleanup
8. DatabaseCache UNLOGGED table verification
9. DatabaseCache get_many / set_many / delete_many
10. DatabaseCache atomic incr
11. @cached decorator (sync + async)
12. Cache key generation

Run: uv run hyper-test cache
Requires: PostgreSQL running, DATABASE_URL or default hyperdjango_test
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.cache import (
    DatabaseCache,
    LocMemCache,
    _make_key,
    cached,
    set_cache,
)
from hyperdjango.database import Database, set_db

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://localhost/hyperdjango_test",
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


async def wait_for(pred, timeout: float = 30.0, interval: float = 0.02) -> bool:
    """Poll an async predicate until true or the deadline; condition, not sleep.

    TTL tests are the one place a real duration must elapse, but "has the entry
    expired?" is still a CONDITION — sleeping ttl + a hand-picked margin and
    asserting bets that the margin covers whatever the clock, the DB round-trip
    and a loaded runner add. Polling for the expiry itself is exact on any
    machine, and the generous ceiling means an entry that never expires (a real
    bug) still fails, just later.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await pred():
            return True
        await asyncio.sleep(interval)
    return bool(await pred())


async def test_locmem_crud():
    """Test LocMemCache basic operations."""
    print("\n=== LocMemCache CRUD ===")

    cache = LocMemCache(max_size=100)

    # Set/get
    cache.set("name", "Alice")
    check("set and get", cache.get("name") == "Alice")

    # Get missing
    check("get missing returns None", cache.get("missing") is None)
    check("get missing with default", cache.get("missing", "fallback") == "fallback")

    # Has
    check("has existing key", cache.has("name"))
    check("has missing key", not cache.has("missing"))

    # Delete
    cache.delete("name")
    check("delete removes key", cache.get("name") is None)
    check("delete non-existent", not cache.delete("missing"))

    # Complex values
    cache.set("user", {"id": 1, "name": "Bob", "scores": [1, 2, 3]})
    user = cache.get("user")
    check(
        "complex value preserved", user["name"] == "Bob" and user["scores"] == [1, 2, 3]
    )

    # Clear
    cache.set("a", 1)
    cache.set("b", 2)
    cache.clear()
    check("clear removes all", cache.count() == 0)


async def test_locmem_ttl():
    """Test LocMemCache TTL expiry."""
    print("\n=== LocMemCache TTL ===")

    cache = LocMemCache()

    cache.set("temp", "value", ttl=1)
    # Written in the same breath as the TTL'd key, and never expiring.
    cache.set("permanent", "forever")
    check("value before expiry", cache.get("temp") == "value")

    async def expired() -> bool:
        return cache.get("temp") is None

    check("value expired", await wait_for(expired), "ttl=1 key never expired")
    check("has returns False after expiry", not cache.has("temp"))

    # "No TTL means permanent" was previously asserted after a 0.1s sleep, which
    # proves nothing: no cache expires anything in 100 ms. The claim is only
    # meaningful measured against an expiry that has DEMONSTRABLY happened — the
    # sibling key above, set at the same moment, is now gone.
    check("no TTL means permanent", cache.get("permanent") == "forever")


async def test_locmem_lru():
    """Test LocMemCache LRU eviction."""
    print("\n=== LocMemCache LRU Eviction ===")

    cache = LocMemCache(max_size=3)

    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    check("3 items fit", cache.count() == 3)

    # Adding 4th evicts oldest (a)
    cache.set("d", 4)
    check("oldest evicted", cache.get("a") is None)
    check("newest kept", cache.get("d") == 4)
    check("still 3 items", cache.count() == 3)

    # Access "b" to make it recently used, then add "e" — "c" should be evicted
    cache.get("b")  # touch b
    cache.set("e", 5)
    check("least recently used evicted", cache.get("c") is None)
    check("recently accessed kept", cache.get("b") == 2)


async def test_locmem_get_or_set():
    """Test LocMemCache get_or_set."""
    print("\n=== LocMemCache get_or_set ===")

    cache = LocMemCache()
    call_count = 0

    def expensive():
        nonlocal call_count
        call_count += 1
        return {"computed": True}

    result = cache.get_or_set("computed", expensive, ttl=60)
    check("first call computes", result == {"computed": True})
    check("function called once", call_count == 1)

    result = cache.get_or_set("computed", expensive, ttl=60)
    check("second call uses cache", result == {"computed": True})
    check("function not called again", call_count == 1)


async def test_db_cache_crud(db):
    """Test DatabaseCache basic operations."""
    print("\n=== DatabaseCache CRUD ===")

    await db.execute("DROP TABLE IF EXISTS hyper_cache CASCADE")
    cache = DatabaseCache(db, default_ttl=300)
    await cache.ensure_table()
    await cache.clear()

    # Set/get
    await cache.set("name", "Alice")
    result = await cache.get("name")
    check("set and get", result == "Alice")

    # Get missing
    result = await cache.get("missing")
    check("get missing returns None", result is None)
    result = await cache.get("missing", "fallback")
    check("get missing with default", result == "fallback")

    # Has
    check("has existing key", await cache.has("name"))
    check("has missing key", not await cache.has("missing"))

    # Complex values
    await cache.set("user", {"id": 1, "name": "Bob", "scores": [1, 2, 3]})
    user = await cache.get("user")
    check(
        "complex value preserved", user["name"] == "Bob" and user["scores"] == [1, 2, 3]
    )

    # Delete
    deleted = await cache.delete("name")
    check("delete returns True", deleted)
    check("deleted key gone", await cache.get("name") is None)

    # Count
    await cache.clear()
    await cache.set("a", 1)
    await cache.set("b", 2)
    count = await cache.count()
    check("count returns 2", count == 2, f"got {count}")

    # Clear
    await cache.clear()
    count = await cache.count()
    check("clear removes all", count == 0)


async def test_db_cache_ttl(db):
    """Test DatabaseCache TTL expiry."""
    print("\n=== DatabaseCache TTL ===")

    cache = DatabaseCache(db, default_ttl=300)
    await cache.clear()

    await cache.set("temp", "value", ttl=1)
    # A companion with a long TTL: expiry must be selective, not "the row went
    # away". Asserted after the short-TTL key has demonstrably expired.
    await cache.set("keeper", "value", ttl=300)
    result = await cache.get("temp")
    check("value before expiry", result == "value")

    async def expired() -> bool:
        return await cache.get("temp") is None

    check("value expired", await wait_for(expired), "ttl=1 row never expired")
    check("long-TTL sibling untouched", await cache.get("keeper") == "value")


async def test_db_cache_cleanup(db):
    """Test DatabaseCache cleanup."""
    print("\n=== DatabaseCache Cleanup ===")

    cache = DatabaseCache(db, default_ttl=1)
    await cache.clear()

    await cache.set("x", 1)
    await cache.set("y", 2)
    await cache.set("z", 3)

    # `cleanup` only deletes rows that are ALREADY past `expires_at`, so wait
    # for the expiry (visible through `count()`, which filters on it) rather
    # than sleeping default_ttl plus a guessed margin — on a loaded runner the
    # writes themselves can eat that margin and cleanup would then find nothing
    # to delete and "pass" for the wrong reason.
    async def all_expired() -> bool:
        return await cache.count() == 0

    check("entries expired before cleanup", await wait_for(all_expired))
    await cache.cleanup()

    count = await db.query_val("SELECT COUNT(*) FROM hyper_cache")
    check("cleanup removed expired", count == 0, f"got {count}")


async def test_db_cache_many(db):
    """Test DatabaseCache batch operations."""
    print("\n=== DatabaseCache Batch Operations ===")

    cache = DatabaseCache(db, default_ttl=300)
    await cache.clear()

    # set_many
    await cache.set_many({"k1": "v1", "k2": "v2", "k3": "v3"})
    count = await cache.count()
    check("set_many creates 3 entries", count == 3)

    # get_many
    results = await cache.get_many(["k1", "k2", "k_missing"])
    check("get_many returns found keys", len(results) == 2)
    check("get_many values correct", results.get("k1") == "v1")

    # delete_many
    await cache.delete_many(["k1", "k2"])
    count = await cache.count()
    check("delete_many removes keys", count == 1)


async def test_db_cache_incr(db):
    """Test DatabaseCache atomic increment."""
    print("\n=== DatabaseCache Atomic Increment ===")

    cache = DatabaseCache(db, default_ttl=300)
    await cache.clear()

    # Increment non-existent key (creates with delta)
    val = await cache.incr("counter")
    check("incr creates with 1", val == 1)

    # Increment existing
    val = await cache.incr("counter")
    check("incr to 2", val == 2)

    val = await cache.incr("counter", 5)
    check("incr by 5 to 7", val == 7)


async def test_db_cache_get_or_set(db):
    """Test DatabaseCache get_or_set — race-safe atomic miss path."""
    print("\n=== DatabaseCache get_or_set ===")

    cache = DatabaseCache(db, default_ttl=300)
    await cache.clear()

    # Basic miss-then-hit
    call_count = 0

    def compute():
        nonlocal call_count
        call_count += 1
        return {"computed": True, "count": call_count}

    result = await cache.get_or_set("gos_key", compute, ttl=60)
    check("gos first call computes", result["computed"] is True)
    check("gos function called once", call_count == 1)

    result = await cache.get_or_set("gos_key", compute, ttl=60)
    check("gos second call uses cache", result["computed"] is True)
    check("gos function not called again", call_count == 1)

    # Different keys compute separately
    result2 = await cache.get_or_set("gos_key2", compute, ttl=60)
    check("gos different key computes", call_count == 2)

    # None value handling — None is a valid cached value
    def return_none():
        return None

    result = await cache.get_or_set("gos_none", return_none, ttl=60)
    check("gos None value stored", result is None)

    # Verify None is actually cached (not re-computed)
    none_calls = 0

    def counted_none():
        nonlocal none_calls
        none_calls += 1
        return None

    # First call stores None
    await cache.get_or_set("gos_counted_none", counted_none, ttl=60)
    check("gos None computed once", none_calls == 1)
    # Second call should return cached None without computing
    await cache.get_or_set("gos_counted_none", counted_none, ttl=60)
    check("gos cached None not recomputed", none_calls == 1)

    # Expired key replacement
    await cache.set("gos_expiring", "old_value", ttl=1)
    await asyncio.sleep(1.5)

    def fresh_value():
        return "fresh"

    result = await cache.get_or_set("gos_expiring", fresh_value, ttl=60)
    check("gos expired key replaced", result == "fresh")

    # After incr, get_or_set should return counter value
    await cache.clear()
    await cache.incr("gos_counter", 42)
    result = await cache.get_or_set("gos_counter", lambda: "should not use", ttl=60)
    check("gos respects counter column", result == 42)

    # Complex value roundtrip
    def complex_val():
        return {"users": [{"id": 1, "name": "Alice"}], "total": 1}

    result = await cache.get_or_set("gos_complex", complex_val, ttl=60)
    check(
        "gos complex value roundtrip",
        result["users"][0]["name"] == "Alice" and result["total"] == 1,
    )


async def test_db_cache_get_or_set_concurrent(db):
    """Test DatabaseCache get_or_set under concurrent access."""
    print("\n=== DatabaseCache get_or_set Concurrent ===")

    cache = DatabaseCache(db, default_ttl=300)
    await cache.clear()

    compute_count = 0

    def expensive():
        nonlocal compute_count
        compute_count += 1
        return {"winner": compute_count}

    # Fire 10 concurrent get_or_set calls for the same key
    tasks = [cache.get_or_set("race_key", expensive, ttl=60) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    # All should return a valid value
    check("concurrent all return values", all(isinstance(r, dict) for r in results))

    # The stored value should be consistent
    stored = await cache.get("race_key")
    check("concurrent stored value exists", stored is not None)
    check("concurrent stored is dict", isinstance(stored, dict))

    # Count should be exactly 1 — only the first miss path computes,
    # but in async Python the event loop is single-threaded so all 10
    # serialize. The point is no data corruption or duplicates.
    count = await cache.count()
    check("concurrent no duplicate keys", count == 1, f"got {count}")


async def test_db_cache_upsert(db):
    """Test DatabaseCache upsert behavior."""
    print("\n=== DatabaseCache Upsert ===")

    cache = DatabaseCache(db, default_ttl=300)
    await cache.clear()

    await cache.set("key", "first")
    await cache.set("key", "second")
    result = await cache.get("key")
    check("upsert overwrites", result == "second")

    count = await cache.count()
    check("upsert doesn't duplicate", count == 1)


async def test_db_cache_unlogged(db):
    """Verify the cache table is UNLOGGED."""
    print("\n=== UNLOGGED Table Verification ===")

    row = await db.query_one(
        "SELECT relpersistence FROM pg_class WHERE relname = 'hyper_cache'"
    )
    if row:
        persistence = row.get("relpersistence", "")
        check(
            "cache table is UNLOGGED",
            persistence == "u",
            f"relpersistence={persistence}",
        )
    else:
        check("hyper_cache table exists", False)


async def test_cached_decorator():
    """Test @cached decorator."""
    print("\n=== @cached Decorator ===")

    cache = LocMemCache()
    set_cache(cache)

    call_count = 0

    @cached(ttl=60, key_prefix="test_func", cache=cache)
    def compute(x, y):
        nonlocal call_count
        call_count += 1
        return x + y

    result = compute(1, 2)
    check("first call computes", result == 3)
    check("function called once", call_count == 1)

    result = compute(1, 2)
    check("second call cached", result == 3)
    check("function not called again", call_count == 1)

    result = compute(3, 4)
    check("different args compute", result == 7)
    check("function called for new args", call_count == 2)


async def test_cached_async_decorator():
    """Test @cached with async function."""
    print("\n=== @cached Async Decorator ===")

    cache = LocMemCache()
    call_count = 0

    @cached(ttl=60, cache=cache)
    async def async_compute(x):
        nonlocal call_count
        call_count += 1
        return x * 2

    result = await async_compute(5)
    check("async first call", result == 10)
    check("async computed once", call_count == 1)

    result = await async_compute(5)
    check("async cached", result == 10)
    check("async not recomputed", call_count == 1)


async def test_cache_key_generation():
    """Test cache key generation."""
    print("\n=== Cache Key Generation ===")

    key1 = _make_key("func", (1, 2), {"x": "y"})
    check("key includes prefix", "func" in key1)
    check("key includes args", "1" in key1 and "2" in key1)
    check("key includes kwargs", "x='y'" in key1)

    # Same args produce same key
    key2 = _make_key("func", (1, 2), {"x": "y"})
    check("deterministic keys", key1 == key2)

    # Different args produce different keys
    key3 = _make_key("func", (3, 4), {})
    check("different args different keys", key1 != key3)

    # INJECTIVITY: distinct (args, kwargs) must NEVER collide. The old
    # ":".join(str(arg)) scheme collided across argument boundaries and types,
    # so one call could return another call's cached value.
    check(
        "arg-boundary no collision",
        _make_key("fn", ("a:b",), {}) != _make_key("fn", ("a", "b"), {}),
    )
    check(
        "type no collision (int vs str)",
        _make_key("fn", (1,), {}) != _make_key("fn", ("1",), {}),
    )
    check(
        "arg-vs-kwarg no collision",
        _make_key("fn", ("x=1",), {}) != _make_key("fn", (), {"x": "1"}),
    )
    check(
        "arg-count no collision",
        _make_key("fn", (1,), {}) != _make_key("fn", (1, ""), {}),
    )

    # Long keys get hashed
    long_key = _make_key("func", tuple(range(100)), {})
    check("long key hashed", len(long_key) < 255)


async def main():
    global passed, failed

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    try:
        # Drop and recreate
        await db.execute("DROP TABLE IF EXISTS hyper_cache CASCADE")

        await test_locmem_crud()
        await test_locmem_ttl()
        await test_locmem_lru()
        await test_locmem_get_or_set()
        await test_db_cache_crud(db)
        await test_db_cache_ttl(db)
        await test_db_cache_cleanup(db)
        await test_db_cache_many(db)
        await test_db_cache_incr(db)
        await test_db_cache_get_or_set(db)
        await test_db_cache_get_or_set_concurrent(db)
        await test_db_cache_upsert(db)
        await test_db_cache_unlogged(db)
        await test_cached_decorator()
        await test_cached_async_decorator()
        await test_cache_key_generation()
    finally:
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All cache tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
