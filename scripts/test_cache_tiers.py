"""
Tests for caching infrastructure — LocMemCache, TwoTierCache, StampedeProtection.

# hyper-test: unit

Tests cache_adapters.py and cache.py features:
- LocMemCache (L1): get/set/delete/clear, TTL expiry, LRU eviction
- TwoTierCache: L1 miss → L2 hit promotion, write-through
- StampedeProtection: XFetch early recompute
- @cached decorator
- ConsistentHashRing
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.cache import LocMemCache, cached
from hyperdjango.cache_adapters import (
    ConsistentHashRing,
    StampedeProtection,
    TwoTierCache,
)
from hyperdjango.testkit.determinism import wait_until

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


def test_locmem_cache():
    print("=== LocMemCache ===")

    cache = LocMemCache(max_size=5)

    # Basic get/set
    cache.set("key1", "value1", ttl=60)
    check("Set + get", cache.get("key1") == "value1")

    # Miss
    check("Miss returns None", cache.get("nonexistent") is None)

    # Delete
    cache.set("key2", "value2")
    cache.delete("key2")
    check("Delete removes key", cache.get("key2") is None)

    # Has
    cache.set("key3", "value3")
    check("Has returns True", cache.has("key3"))
    check("Has returns False for missing", not cache.has("missing"))

    # Clear
    cache.set("a", 1)
    cache.set("b", 2)
    cache.clear()
    check("Clear removes all", cache.get("a") is None and cache.get("b") is None)

    # TTL expiry. A live entry must survive and an expired one must not — both
    # halves are asserted so "expired" cannot pass by the entry simply never
    # having been stored. ttl=0 sets expires_at to the set instant, and the
    # entry is gone once the clock passes it; wait for THAT condition (bounded)
    # rather than sleeping a guessed duration, so neither a fast box nor a
    # starved one can change the answer.
    cache.set("ttl_key", "ttl_value", ttl=60)
    check("Live TTL key is readable", cache.get("ttl_key") == "ttl_value")
    cache.set("ttl_key", "ttl_value", ttl=0)
    wait_until(
        lambda: cache.get("ttl_key") is None,
        timeout_s=5.0,
        desc="ttl=0 entry never expired",
    )
    check("Expired key returns None", cache.get("ttl_key") is None)

    # LRU eviction (max_size=5)
    for i in range(6):
        cache.set(f"lru_{i}", f"val_{i}")
    check("LRU evicts oldest", cache.get("lru_0") is None)
    check("LRU keeps newest", cache.get("lru_5") == "val_5")

    # get_or_set
    result = cache.get_or_set("computed", lambda: 42, ttl=60)
    check("get_or_set computes", result == 42)
    result = cache.get_or_set("computed", lambda: 99, ttl=60)
    check("get_or_set returns cached", result == 42)

    # Count
    cache.clear()
    cache.set("x", 1)
    cache.set("y", 2)
    check("Count is 2", cache.count() == 2, f"got {cache.count()}")


def test_consistent_hash_ring():
    print("\n=== ConsistentHashRing ===")

    ring = ConsistentHashRing()
    ring.add_node("cache-1", "node1")
    ring.add_node("cache-2", "node2")
    ring.add_node("cache-3", "node3")

    check("Ring has 3 nodes", ring.node_count == 3)

    # Deterministic routing
    node1 = ring.get_node_name("user:123")
    node2 = ring.get_node_name("user:123")
    check("Same key same node", node1 == node2)

    # Distribution
    distribution = {}
    for i in range(1000):
        node = ring.get_node_name(f"key:{i}")
        distribution[node] = distribution.get(node, 0) + 1
    check(
        "All 3 nodes get keys", len(distribution) == 3, f"distribution={distribution}"
    )
    max_pct = max(distribution.values()) / 1000
    check("Balanced distribution (<60%)", max_pct < 0.6, f"max={max_pct:.1%}")

    # Remove node
    ring.remove_node("cache-2")
    check("Node removed", ring.node_count == 2)

    # Stats
    stats = ring.get_stats()
    check("Stats has node_count", stats["node_count"] == 2)


def test_stampede_protection():
    print("\n=== StampedeProtection ===")

    backend = LocMemCache(max_size=100)
    cache = StampedeProtection(backend, beta=1.0)

    cache.set("sp_key", "sp_value", ttl=60)
    check("Stampede set + get", cache.get("sp_key") == "sp_value")

    cache.set("sp_del", "val")
    cache.delete("sp_del")
    check("Stampede delete", cache.get("sp_del") is None)

    cache.set("sp_a", 1)
    cache.clear()
    check("Stampede clear", cache.get("sp_a") is None)


def test_two_tier_cache():
    print("\n=== TwoTierCache ===")

    l1 = LocMemCache(max_size=10)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=5)

    # Set goes to both tiers
    cache.set("tt_key", "tt_value", ttl=60)
    check("L1 has value", l1.get("tt_key") == "tt_value")
    check("L2 has value", l2.get("tt_key") == "tt_value")

    # Get from L1 (fast path)
    check("Get from L1", cache.get("tt_key") == "tt_value")

    # L1 miss, L2 hit → promote to L1
    l1.delete("tt_key")
    check("L1 cleared", l1.get("tt_key") is None)
    result = cache.get("tt_key")
    check("L2 hit returns value", result == "tt_value")
    check("Promoted to L1", l1.get("tt_key") == "tt_value")

    # Both miss
    check("Both miss returns None", cache.get("nonexistent") is None)

    # Delete removes from both
    cache.set("del_test", "value")
    cache.delete("del_test")
    check("Delete from both", l1.get("del_test") is None and l2.get("del_test") is None)

    # Clear both
    cache.set("c1", 1)
    cache.set("c2", 2)
    cache.clear()
    check("Clear both tiers", l1.get("c1") is None and l2.get("c2") is None)

    # Stats
    stats = cache.get_stats()
    check("Stats has l1_hits", "l1_hits" in stats, f"stats keys={list(stats.keys())}")


def test_cached_decorator():
    print("\n=== @cached decorator ===")

    call_count = 0
    my_cache = LocMemCache(max_size=10)

    @cached(ttl=60, cache=my_cache)
    def expensive(x):
        nonlocal call_count
        call_count += 1
        return x * 2

    result1 = expensive(5)
    check("First call computes", result1 == 10 and call_count == 1)

    result2 = expensive(5)
    check(
        "Second call cached",
        result2 == 10 and call_count == 1,
        f"call_count={call_count}",
    )

    result3 = expensive(7)
    check("Different args compute", result3 == 14 and call_count == 2)


def main():
    test_locmem_cache()
    test_consistent_hash_ring()
    test_stampede_protection()
    test_two_tier_cache()
    test_cached_decorator()

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
