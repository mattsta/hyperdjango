"""
TwoTierCache integration showcase — realistic workload patterns.

# hyper-test: unit

Tests:
1.  Cold cache L2→L1 promotion
2.  L1 TTL expiry and re-promotion from L2
3.  Promotion preserves complex values
4.  Read-heavy Zipfian workload (hot keys stay in L1)
5.  Write-through consistency across both tiers
6.  Cache warming then serve pattern
7.  Mixed hot/cold key access pattern
8.  Stats accuracy with controlled operations
9.  Stats reset on clear
10. Promotion count equals L2 hits
11. L1 eviction under pressure (LRU + re-promotion)
12. Delete removes from both tiers
13. Has checks both tiers
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.cache import LocMemCache
from hyperdjango.cache_adapters import TwoTierCache

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


# ─── Phase 1: Basic L1→L2 promotion ──────────────────────────────────────────


def test_cold_cache_promotion():
    """Write to L2 only. Read through TwoTierCache. Verify L1 promotion."""
    print("\n=== Cold Cache L2→L1 Promotion ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=1000)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    num_keys = 10
    # Populate L2 directly (simulating a shared cache warmed by another server)
    for i in range(num_keys):
        l2.set(f"cold:{i}", {"index": i, "data": f"value_{i}"}, ttl=300)

    # First pass: all reads should be L2 hits (L1 empty)
    for i in range(num_keys):
        result = cache.get(f"cold:{i}")
        check(
            f"cold read {i} returns value", result is not None and result["index"] == i
        )

    stats = cache.get_stats()
    check(
        "first pass: all L2 hits",
        stats["l2_hits"] == num_keys,
        f"l2_hits={stats['l2_hits']}",
    )
    check(
        "first pass: zero L1 hits",
        stats["l1_hits"] == 0,
        f"l1_hits={stats['l1_hits']}",
    )

    # Second pass: all reads should be L1 hits (promoted)
    for i in range(num_keys):
        result = cache.get(f"cold:{i}")
        check(f"warm read {i} from L1", result is not None and result["index"] == i)

    stats = cache.get_stats()
    check(
        "second pass: L1 hits = num_keys",
        stats["l1_hits"] == num_keys,
        f"l1_hits={stats['l1_hits']}",
    )
    print(
        f"  Stats: L1={stats['l1_hits']}, L2={stats['l2_hits']}, miss={stats['misses']}"
    )


def test_l1_ttl_expiry_re_promotion():
    """L1 TTL expires, but L2 still has value. Re-promotion occurs."""
    print("\n=== L1 TTL Expiry + Re-Promotion ===")

    # Both tiers read the same injected clock, so "L1's short TTL elapsed while
    # L2's long one did not" is stated by advancing time rather than by sleeping
    # past a 0-second TTL and hoping the wall clock ticked. Nothing here depends
    # on how fast the machine is.
    now = [1000.0]
    l1 = LocMemCache(max_size=100, clock=lambda: now[0])
    l2 = LocMemCache(max_size=100, clock=lambda: now[0])
    cache = TwoTierCache(l1, l2, l1_ttl=5)

    # Write through cache (both tiers get value)
    cache.set("expire_test", "precious_data", ttl=300)
    check("L1 serves before its TTL", cache.get("expire_test") == "precious_data")
    check("that read was an L1 hit", cache.get_stats()["l1_hits"] == 1)

    # Past L1's 5s TTL, still far inside L2's 300s one.
    now[0] += 6

    # Read should hit L2 and re-promote
    result = cache.get("expire_test")
    check("re-promotion returns value", result == "precious_data")

    stats = cache.get_stats()
    check(
        "L2 hit from re-promotion", stats["l2_hits"] == 1, f"l2_hits={stats['l2_hits']}"
    )
    check(
        "re-promoted entry serves from L1 again",
        cache.get("expire_test") == "precious_data"
        and cache.get_stats()["l1_hits"] == 2,
        f"l1_hits={cache.get_stats()['l1_hits']}",
    )


def test_promotion_preserves_complex_values():
    """Ensure promoted values are identical to L2 originals."""
    print("\n=== Promotion Preserves Complex Values ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    complex_values = [
        {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], "total": 2},
        [1, 2, 3, [4, 5], {"nested": True}],
        "simple_string",
        42,
        True,
        {"deep": {"nested": {"value": [1, 2, 3]}}},
    ]

    for i, val in enumerate(complex_values):
        key = f"complex:{i}"
        l2.set(key, val, ttl=300)

    for i, expected in enumerate(complex_values):
        key = f"complex:{i}"
        result = cache.get(key)  # L2 hit → promote
        check(f"complex value {i} promoted correctly", result == expected)

        # Verify L1 now has it
        l1_val = l1.get(key)
        check(f"complex value {i} in L1", l1_val == expected)


# ─── Phase 2: Realistic workload simulation ──────────────────────────────────


def test_read_heavy_workload():
    """Simulate 80% reads / 20% writes with Zipfian key distribution."""
    print("\n=== Read-Heavy Zipfian Workload ===")

    l1 = LocMemCache(max_size=50)
    l2 = LocMemCache(max_size=500)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    num_keys = 100
    num_ops = 1000

    # Seed all keys in L2
    for i in range(num_keys):
        l2.set(f"wl:{i}", {"val": i}, ttl=300)

    rng = random.Random(42)  # deterministic

    reads = writes = 0
    for _ in range(num_ops):
        if rng.random() < 0.8:
            # Read: Zipfian — lower keys accessed more
            key_idx = int(rng.paretovariate(1.5)) % num_keys
            cache.get(f"wl:{key_idx}")
            reads += 1
        else:
            key_idx = rng.randint(0, num_keys - 1)
            cache.set(f"wl:{key_idx}", {"val": key_idx, "updated": True}, ttl=300)
            writes += 1

    stats = cache.get_stats()
    l1_rate = stats["l1_hit_rate"]
    overall_rate = stats["overall_hit_rate"]

    print(f"  Operations: {reads} reads, {writes} writes")
    print(f"  L1 hits: {stats['l1_hits']} ({l1_rate:.1%})")
    print(f"  L2 hits: {stats['l2_hits']} ({stats['l2_hit_rate']:.1%})")
    print(f"  Misses: {stats['misses']}")
    print(f"  Overall hit rate: {overall_rate:.1%}")

    check(
        "L1 hit rate > 40% for hot keys",
        l1_rate > 0.40,
        f"l1_rate={l1_rate:.1%}",
    )
    check(
        "Overall hit rate > 80%",
        overall_rate > 0.80,
        f"overall_rate={overall_rate:.1%}",
    )


def test_write_through_consistency():
    """Write via TwoTierCache, verify both tiers are consistent."""
    print("\n=== Write-Through Consistency ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Write 50 keys
    for i in range(50):
        cache.set(f"wt:{i}", {"id": i, "name": f"item_{i}"}, ttl=300)

    # Verify both tiers have identical values
    all_consistent = True
    for i in range(50):
        l1_val = l1.get(f"wt:{i}")
        l2_val = l2.get(f"wt:{i}")
        if l1_val != l2_val or l1_val is None:
            all_consistent = False
            break

    check("all 50 keys consistent across tiers", all_consistent)

    # Update 25 keys
    for i in range(25):
        cache.set(f"wt:{i}", {"id": i, "name": f"updated_{i}"}, ttl=300)

    # Verify updated keys are consistent
    updates_consistent = True
    for i in range(25):
        l1_val = l1.get(f"wt:{i}")
        l2_val = l2.get(f"wt:{i}")
        if l1_val != l2_val or l1_val["name"] != f"updated_{i}":
            updates_consistent = False
            break

    check("updated keys consistent across tiers", updates_consistent)


def test_cache_warming_then_serve():
    """Pre-populate L2 (cache warming), then serve from L1 after promotion."""
    print("\n=== Cache Warming Then Serve ===")

    l1 = LocMemCache(max_size=200)
    l2 = LocMemCache(max_size=500)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Warm L2 with 200 keys (simulating app startup cache warming)
    for i in range(200):
        l2.set(f"warm:{i}", {"data": f"warmed_{i}"}, ttl=600)

    rng = random.Random(99)

    # First pass: 500 reads — all L2 hits with promotion
    for _ in range(500):
        key_idx = rng.randint(0, 199)
        result = cache.get(f"warm:{key_idx}")
        assert result is not None

    stats_after_first = cache.get_stats()

    # Second pass: 500 more reads — mostly L1 hits now
    for _ in range(500):
        key_idx = rng.randint(0, 199)
        result = cache.get(f"warm:{key_idx}")
        assert result is not None

    stats_after_second = cache.get_stats()
    overall = stats_after_second["overall_hit_rate"]

    print("  After warming + 1000 reads:")
    print(f"  L1 hits: {stats_after_second['l1_hits']}")
    print(f"  L2 hits: {stats_after_second['l2_hits']}")
    print(f"  Misses: {stats_after_second['misses']}")
    print(f"  Overall hit rate: {overall:.1%}")

    check("overall hit rate > 90%", overall > 0.90, f"{overall:.1%}")
    check("zero misses (all pre-warmed)", stats_after_second["misses"] == 0)


def test_mixed_hot_cold_keys():
    """Hot keys get high L1 hit rate, cold keys lower."""
    print("\n=== Mixed Hot/Cold Key Access ===")

    l1 = LocMemCache(max_size=30)  # Only fits 30 keys
    l2 = LocMemCache(max_size=500)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Seed 200 keys in L2
    for i in range(200):
        l2.set(f"hc:{i}", {"val": i}, ttl=300)

    rng = random.Random(77)
    hot_keys = list(range(20))  # 20 hot keys
    cold_keys = list(range(20, 200))  # 180 cold keys

    # 2000 reads: 80% hot, 20% cold
    for _ in range(2000):
        key_idx = rng.choice(hot_keys) if rng.random() < 0.8 else rng.choice(cold_keys)
        cache.get(f"hc:{key_idx}")

    stats = cache.get_stats()
    l1_rate = stats["l1_hit_rate"]

    print(f"  L1 hits: {stats['l1_hits']} ({l1_rate:.1%})")
    print(f"  L2 hits: {stats['l2_hits']} ({stats['l2_hit_rate']:.1%})")
    print(f"  Misses: {stats['misses']}")

    # Hot keys (20) fit in L1 (max 30), so L1 hit rate should be high
    check(
        "L1 hit rate > 60% (hot working set fits)",
        l1_rate > 0.60,
        f"l1_rate={l1_rate:.1%}",
    )
    check("zero misses (all in L2)", stats["misses"] == 0)


# ─── Phase 3: Metrics verification ───────────────────────────────────────────


def test_stats_accuracy():
    """Perform exact N operations and verify stats match."""
    print("\n=== Stats Accuracy ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Setup: 5 keys in both tiers
    for i in range(5):
        cache.set(f"sa:{i}", i, ttl=300)

    cache.clear()  # Reset stats

    # Re-seed L2 only
    for i in range(5):
        l2.set(f"sa:{i}", i, ttl=300)

    # 5 reads → all L2 hits (L1 was cleared)
    for i in range(5):
        cache.get(f"sa:{i}")

    stats = cache.get_stats()
    check(
        "5 L2 hits after L1 cleared", stats["l2_hits"] == 5, f"got {stats['l2_hits']}"
    )
    check("0 L1 hits", stats["l1_hits"] == 0, f"got {stats['l1_hits']}")

    # 5 more reads → all L1 hits (promoted)
    for i in range(5):
        cache.get(f"sa:{i}")

    stats = cache.get_stats()
    check("5 L1 hits after promotion", stats["l1_hits"] == 5, f"got {stats['l1_hits']}")
    check("still 5 L2 hits", stats["l2_hits"] == 5, f"got {stats['l2_hits']}")

    # 3 misses
    for i in range(3):
        cache.get(f"nonexistent:{i}")

    stats = cache.get_stats()
    check("3 misses", stats["misses"] == 3, f"got {stats['misses']}")
    check("total = 13", stats["total_requests"] == 13, f"got {stats['total_requests']}")


def test_stats_reset_on_clear():
    """Clear resets all stats to zero."""
    print("\n=== Stats Reset on Clear ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Generate some stats
    cache.set("sr:1", "val", ttl=300)
    cache.get("sr:1")  # L1 hit
    cache.get("missing")  # miss

    stats = cache.get_stats()
    check("stats non-zero before clear", stats["total_requests"] > 0)

    cache.clear()
    stats = cache.get_stats()
    check("l1_hits reset", stats["l1_hits"] == 0)
    check("l2_hits reset", stats["l2_hits"] == 0)
    check("misses reset", stats["misses"] == 0)
    check("total reset", stats["total_requests"] == 0)


def test_promotion_count_equals_l2_hits():
    """Every L2 hit triggers a promotion. Verify counts match."""
    print("\n=== Promotion Count = L2 Hits ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Seed 20 keys in L2 only
    for i in range(20):
        l2.set(f"promo:{i}", i, ttl=300)

    # Read all 20 → each should be an L2 hit + promotion
    for i in range(20):
        cache.get(f"promo:{i}")

    stats = cache.get_stats()
    check("20 L2 hits = 20 promotions", stats["l2_hits"] == 20)

    # Verify all 20 are now in L1
    all_in_l1 = all(l1.get(f"promo:{i}") is not None for i in range(20))
    check("all 20 promoted to L1", all_in_l1)


# ─── Phase 4: Edge cases ─────────────────────────────────────────────────────


def test_l1_eviction_under_pressure():
    """L1 has small max_size. Verify evicted keys re-promote from L2."""
    print("\n=== L1 Eviction Under Pressure ===")

    l1 = LocMemCache(max_size=5)  # Only holds 5 keys
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Seed 20 keys in L2
    for i in range(20):
        l2.set(f"ev:{i}", i, ttl=300)

    # Read all 20 → only last 5 stay in L1 (LRU eviction)
    for i in range(20):
        cache.get(f"ev:{i}")

    stats = cache.get_stats()
    check("20 L2 hits on first pass", stats["l2_hits"] == 20)

    # Read the last 5 again → should be L1 hits
    for i in range(15, 20):
        cache.get(f"ev:{i}")

    stats = cache.get_stats()
    check("last 5 are L1 hits", stats["l1_hits"] == 5, f"got {stats['l1_hits']}")

    # Read early keys → should re-promote from L2 (were evicted from L1)
    for i in range(5):
        result = cache.get(f"ev:{i}")
        check(f"evicted key {i} re-promotes", result == i)

    stats = cache.get_stats()
    # Original 20 L2 hits + 5 re-promotions = 25
    check(
        "re-promoted keys counted as L2 hits",
        stats["l2_hits"] == 25,
        f"got {stats['l2_hits']}",
    )


def test_delete_removes_from_both_tiers():
    """Delete removes key from both L1 and L2."""
    print("\n=== Delete Removes From Both Tiers ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    cache.set("del:1", "value1", ttl=300)
    cache.set("del:2", "value2", ttl=300)

    # Verify both tiers have values
    check("L1 has del:1", l1.get("del:1") == "value1")
    check("L2 has del:1", l2.get("del:1") == "value1")

    # Delete via TwoTierCache
    cache.delete("del:1")
    check("L1 del:1 gone", l1.get("del:1") is None)
    check("L2 del:1 gone", l2.get("del:1") is None)
    check("del:2 still exists", cache.get("del:2") == "value2")


def test_has_checks_both_tiers():
    """has() checks L1 first, then L2."""
    print("\n=== Has Checks Both Tiers ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=100)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Key in both tiers
    cache.set("has:both", "val", ttl=300)
    check("has returns True for both-tier key", cache.has("has:both"))

    # Key only in L2
    l2.set("has:l2only", "val", ttl=300)
    check("has returns True for L2-only key", cache.has("has:l2only"))

    # Key in neither
    check("has returns False for missing", not cache.has("has:missing"))


def main():
    test_cold_cache_promotion()
    test_l1_ttl_expiry_re_promotion()
    test_promotion_preserves_complex_values()
    test_read_heavy_workload()
    test_write_through_consistency()
    test_cache_warming_then_serve()
    test_mixed_hot_cold_keys()
    test_stats_accuracy()
    test_stats_reset_on_clear()
    test_promotion_count_equals_l2_hits()
    test_l1_eviction_under_pressure()
    test_delete_removes_from_both_tiers()
    test_has_checks_both_tiers()

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
