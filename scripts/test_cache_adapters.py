"""
Tests for distributed cache adapter system.

Tests ConsistentHashRing, StampedeProtection, TwoTierCache,
CacheMiddleware, and adapter registry.

Usage:
    uv run hyper-test cache_adapters
"""

# hyper-test: unit

import asyncio
import inspect
import sys
import traceback

from hyperdjango.cache import LocMemCache
from hyperdjango.cache_adapters import (
    CacheMiddleware,
    ConsistentHashRing,
    StampedeProtection,
    TwoTierCache,
    get_adapter,
    list_adapters,
    register_adapter,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


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
# ConsistentHashRing
# ---------------------------------------------------------------------------


@test("HashRing: deterministic routing")
def test_ring_deterministic():
    c1, c2 = LocMemCache(), LocMemCache()
    ring = ConsistentHashRing(nodes={"node1": c1, "node2": c2})

    # Same key always routes to same node
    node_a = ring.get_node_name("user:42")
    node_b = ring.get_node_name("user:42")
    assert node_a == node_b


@test("HashRing: distributes across nodes")
def test_ring_distribution():
    c1, c2, c3 = LocMemCache(), LocMemCache(), LocMemCache()
    ring = ConsistentHashRing(nodes={"a": c1, "b": c2, "c": c3})

    # Count distribution over 1000 keys
    counts = {"a": 0, "b": 0, "c": 0}
    for i in range(1000):
        node = ring.get_node_name(f"key:{i}")
        counts[node] += 1

    # Each node should get roughly 1/3 (allow 15-50% range)
    for name, count in counts.items():
        assert 150 < count < 500, f"{name} got {count}/1000 keys"


@test("HashRing: add/remove node")
def test_ring_add_remove():
    c1, c2 = LocMemCache(), LocMemCache()
    ring = ConsistentHashRing(nodes={"a": c1, "b": c2})
    assert ring.node_count == 2

    c3 = LocMemCache()
    ring.add_node("c", c3)
    assert ring.node_count == 3

    ring.remove_node("b")
    assert ring.node_count == 2
    assert "b" not in ring.node_names


@test("HashRing: single node")
def test_ring_single():
    c1 = LocMemCache()
    ring = ConsistentHashRing(nodes={"only": c1})

    for i in range(100):
        assert ring.get_node_name(f"key:{i}") == "only"


@test("HashRing: empty raises")
def test_ring_empty():
    ring = ConsistentHashRing()
    try:
        ring.get_node("key")
        assert False, "Should have raised"
    except RuntimeError:
        pass


@test("HashRing: minimal remapping on node add")
def test_ring_minimal_remap():
    c1, c2 = LocMemCache(), LocMemCache()
    ring = ConsistentHashRing(nodes={"a": c1, "b": c2})

    # Record routing for 1000 keys
    before = {}
    for i in range(1000):
        before[f"key:{i}"] = ring.get_node_name(f"key:{i}")

    # Add a third node
    c3 = LocMemCache()
    ring.add_node("c", c3)

    # Count how many keys changed
    changed = 0
    for i in range(1000):
        after = ring.get_node_name(f"key:{i}")
        if before[f"key:{i}"] != after:
            changed += 1

    # Should remap ~1/3 of keys (not all)
    assert changed < 500, f"Too many remapped: {changed}/1000"
    assert changed > 100, f"Too few remapped: {changed}/1000 (expected ~333)"


# ---------------------------------------------------------------------------
# StampedeProtection
# ---------------------------------------------------------------------------


@test("Stampede: basic set/get")
def test_stampede_basic():
    cache = StampedeProtection(backend=LocMemCache(), beta=1.0)
    cache.set("key", "value", ttl=300, compute_time_ms=10)

    result = cache.get("key")
    assert result == "value"


@test("Stampede: expired returns None")
def test_stampede_expired():
    cache = StampedeProtection(backend=LocMemCache(), beta=1.0)

    # A value inside its window is served...
    cache.set("key", "value", ttl=300, compute_time_ms=0)
    assert cache.get("key") == "value"

    # ...and one whose expiry has been reached is not. StampedeProtection
    # expires on `now >= expires_at`, and ttl=0 puts expires_at AT the set
    # instant, so the very next get is at-or-past it on any machine — no clock
    # has to advance for the assertion to hold. compute_time_ms=0 keeps the
    # XFetch early-recompute branch out of it, so this tests real expiry only.
    # This previously slept 1.1s past a 1s TTL, which made the result a
    # function of how long the machine actually slept.
    cache.set("key", "value", ttl=0, compute_time_ms=0)
    assert cache.get("key") is None


@test("Stampede: delete works")
def test_stampede_delete():
    cache = StampedeProtection(backend=LocMemCache(), beta=1.0)
    cache.set("key", "value", ttl=300)
    assert cache.get("key") == "value"

    cache.delete("key")
    assert cache.get("key") is None


@test("Stampede: clear works")
def test_stampede_clear():
    cache = StampedeProtection(backend=LocMemCache(), beta=1.0)
    cache.set("a", 1, ttl=300)
    cache.set("b", 2, ttl=300)
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None


@test("Stampede: has() reflects state")
def test_stampede_has():
    cache = StampedeProtection(backend=LocMemCache(), beta=1.0)
    assert not cache.has("key")
    cache.set("key", "value", ttl=300, compute_time_ms=0)
    assert cache.has("key")


@test("Stampede: high beta causes early expiry")
def test_stampede_early_expiry():
    cache = StampedeProtection(backend=LocMemCache(), beta=100.0)
    # Set with very high compute_time relative to TTL
    cache.set("key", "value", ttl=5, compute_time_ms=5000)

    # With beta=100 and compute_time=5s, many gets near expiry should return None
    none_count = 0
    for _ in range(100):
        if cache.get("key") is None:
            none_count += 1

    # At least some should trigger early recompute
    # (probabilistic, but with these extreme params it should happen often)
    assert none_count >= 1, f"Expected some early expiry, got {none_count}/100 None"


# ---------------------------------------------------------------------------
# TwoTierCache
# ---------------------------------------------------------------------------


@test("TwoTier: L1 hit")
def test_two_tier_l1_hit():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2, l1_ttl=10)

    cache.set("key", "value", ttl=300)
    result = cache.get("key")
    assert result == "value"

    stats = cache.get_stats()
    assert stats["l1_hits"] == 1


@test("TwoTier: L2 hit promotes to L1")
def test_two_tier_l2_promote():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2, l1_ttl=10)

    # Write only to L2
    l2.set("key", "from_l2", 300)

    result = cache.get("key")
    assert result == "from_l2"

    stats = cache.get_stats()
    assert stats["l2_hits"] == 1

    # Should now be in L1
    assert l1.get("key") == "from_l2"

    # Second get should hit L1
    result2 = cache.get("key")
    assert result2 == "from_l2"
    assert cache.get_stats()["l1_hits"] == 1


@test("TwoTier: miss returns default")
def test_two_tier_miss():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2)

    result = cache.get("nonexistent", "default_val")
    assert result == "default_val"

    stats = cache.get_stats()
    assert stats["misses"] == 1


@test("TwoTier: delete removes from both")
def test_two_tier_delete():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2)

    cache.set("key", "value", ttl=300)
    assert l1.has("key")
    assert l2.has("key")

    cache.delete("key")
    assert not l1.has("key")
    assert not l2.has("key")


@test("TwoTier: clear resets both tiers and stats")
def test_two_tier_clear():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2)

    cache.set("a", 1, ttl=300)
    cache.get("a")

    cache.clear()
    assert not l1.has("a")
    assert not l2.has("a")

    stats = cache.get_stats()
    assert stats["l1_hits"] == 0


@test("TwoTier: has checks both tiers")
def test_two_tier_has():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2)

    assert not cache.has("key")

    l2.set("key", "value", 300)
    assert cache.has("key")


@test("TwoTier: stats hit rates")
def test_two_tier_stats():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2, l1_ttl=10)

    cache.set("a", 1, ttl=300)
    cache.get("a")  # L1 hit
    cache.get("a")  # L1 hit

    l2.set("b", 2, 300)
    cache.get("b")  # L2 hit

    cache.get("c")  # Miss

    stats = cache.get_stats()
    assert stats["l1_hits"] == 2
    assert stats["l2_hits"] == 1
    assert stats["misses"] == 1
    assert abs(stats["overall_hit_rate"] - 0.75) < 0.01


@test("TwoTier: async get/set")
async def test_two_tier_async():
    l1 = LocMemCache()
    l2 = LocMemCache()
    cache = TwoTierCache(l1=l1, l2=l2, l1_ttl=10)

    await cache.aset("key", "value", ttl=300)
    result = await cache.aget("key")
    assert result == "value"


# ---------------------------------------------------------------------------
# Cache Middleware
# ---------------------------------------------------------------------------


@test("CacheMiddleware: caches GET response")
async def test_middleware_cache_get():
    cache = LocMemCache()
    mw = CacheMiddleware(cache, ttl=60)

    call_count = 0

    class FakeRequest:
        method = "GET"
        path = "/products"
        query_string = ""
        headers = {}
        user = None

    class FakeResponse:
        status_code = 200
        body = b"<h1>Products</h1>"
        headers = {"content-type": "text/html"}

    async def call_next(req):
        nonlocal call_count
        call_count += 1
        return FakeResponse()

    # First request — MISS
    resp1 = await mw(FakeRequest(), call_next)
    assert call_count == 1
    assert resp1.headers.get("X-Cache") == "MISS"

    # Second request — HIT (should NOT call handler again)
    resp2 = await mw(FakeRequest(), call_next)
    assert call_count == 1  # Not called again!
    assert resp2.headers.get("X-Cache") == "HIT"


@test("CacheMiddleware: skips POST")
async def test_middleware_skip_post():
    cache = LocMemCache()
    mw = CacheMiddleware(cache, ttl=60)

    class FakeRequest:
        method = "POST"
        path = "/api/orders"
        headers = {}

    class FakeResponse:
        status_code = 201
        headers = {}

    call_count = 0

    async def call_next(req):
        nonlocal call_count
        call_count += 1
        return FakeResponse()

    await mw(FakeRequest(), call_next)
    await mw(FakeRequest(), call_next)
    assert call_count == 2  # Both calls went through


@test("CacheMiddleware: respects exclude paths")
async def test_middleware_exclude():
    cache = LocMemCache()
    mw = CacheMiddleware(cache, ttl=60, exclude=["/admin", "/api/auth"])

    class FakeRequest:
        method = "GET"
        path = "/admin/users"
        query_string = ""
        headers = {}
        user = None

    class FakeResponse:
        status_code = 200
        body = b"admin page"
        headers = {"content-type": "text/html"}

    call_count = 0

    async def call_next(req):
        nonlocal call_count
        call_count += 1
        return FakeResponse()

    await mw(FakeRequest(), call_next)
    await mw(FakeRequest(), call_next)
    assert call_count == 2  # Not cached because excluded


# ---------------------------------------------------------------------------
# Adapter Registry
# ---------------------------------------------------------------------------


@test("Registry: register and get adapter")
def test_registry_register():
    class FakeAdapter:
        pass

    register_adapter("fake", FakeAdapter)
    assert get_adapter("fake") is FakeAdapter
    assert "fake" in list_adapters()


@test("Registry: unknown adapter returns None")
def test_registry_unknown():
    assert get_adapter("nonexistent_xyz") is None


@test("Registry: list adapters")
def test_registry_list():
    class Adapter1:
        pass

    class Adapter2:
        pass

    register_adapter("adapter1", Adapter1)
    register_adapter("adapter2", Adapter2)
    names = list_adapters()
    assert "adapter1" in names
    assert "adapter2" in names


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    all_tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            all_tests.append(obj)

    print("\n═══ Cache Adapter Tests ═══")
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
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
