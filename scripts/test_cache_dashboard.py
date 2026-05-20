"""
Cache admin dashboard tests — template, stats collection, JSON API.

# hyper-test: unit

Tests:
1. TEMPLATE_CACHE_DASHBOARD exists and has key variable references
2. TEMPLATE_DASHBOARD has cache link
3. HyperAdmin has register_cache_dashboard method
4. HyperAdmin has _collect_cache_stats method
5. _collect_cache_stats returns correct structure with empty caches
6. _collect_cache_stats returns correct values after query cache operations
7. TwoTierCache stats appear when cache is TwoTierCache
8. Auto-refresh meta tag in template
9. JSON API link in template
10. Stats hit rate formatting
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.admin.templates import (
    TEMPLATE_CACHE_DASHBOARD,
    TEMPLATE_DASHBOARD,
)
from hyperdjango.cache import LocMemCache, set_cache
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


def test_template_exists():
    print("=== Template Existence ===")
    check("TEMPLATE_CACHE_DASHBOARD exists", TEMPLATE_CACHE_DASHBOARD is not None)
    check("template is a string", isinstance(TEMPLATE_CACHE_DASHBOARD, str))
    check("template has content", len(TEMPLATE_CACHE_DASHBOARD) > 100)


def test_template_variables():
    print("\n=== Template Variable References ===")
    t = TEMPLATE_CACHE_DASHBOARD
    check("has query_cache.hit_rate", "stats.query_cache.hit_rate" in t)
    check("has query_cache.total_requests", "stats.query_cache.total_requests" in t)
    check("has query_cache.invalidations", "stats.query_cache.invalidations" in t)
    check("has query_cache.table_count", "stats.query_cache.table_count" in t)
    check("has query_cache.hits", "stats.query_cache.hits" in t)
    check("has query_cache.misses", "stats.query_cache.misses" in t)
    check("has query_cache.sets", "stats.query_cache.sets" in t)
    check("has table_versions loop", "stats.table_versions" in t)
    check("has two_tier conditional", "stats.two_tier" in t)
    check("has locmem conditional", "stats.locmem" in t)
    check("has l1_hits", "stats.two_tier.l1_hits" in t)
    check("has l2_hits", "stats.two_tier.l2_hits" in t)
    check("has overall_hit_rate_pct", "stats.two_tier.overall_hit_rate_pct" in t)


def test_dashboard_link():
    print("\n=== Dashboard Link ===")
    check("TEMPLATE_DASHBOARD has cache link", "/cache/" in TEMPLATE_DASHBOARD)
    check("cache link has teal color", "#14b8a6" in TEMPLATE_DASHBOARD)
    check("cache link has text", "Cache Dashboard" in TEMPLATE_DASHBOARD)


def test_auto_refresh():
    print("\n=== Auto-Refresh ===")
    t = TEMPLATE_CACHE_DASHBOARD
    check("has meta refresh", 'http-equiv="refresh"' in t)
    check("refresh interval = 5", 'content="5"' in t)


def test_json_api_link():
    print("\n=== JSON API Link ===")
    t = TEMPLATE_CACHE_DASHBOARD
    check("has JSON API link", "/cache/json" in t)


def test_admin_methods():
    print("\n=== HyperAdmin Methods ===")
    from hyperdjango.admin import HyperAdmin

    check(
        "has register_cache_dashboard",
        callable(getattr(HyperAdmin, "register_cache_dashboard", None)),
    )
    check(
        "has _collect_cache_stats",
        callable(getattr(HyperAdmin, "_collect_cache_stats", None)),
    )
    check(
        "has _make_cache_view",
        callable(getattr(HyperAdmin, "_make_cache_view", None)),
    )
    check(
        "has _make_cache_json",
        callable(getattr(HyperAdmin, "_make_cache_json", None)),
    )


def test_collect_cache_stats_empty():
    print("\n=== Stats Collection (Empty) ===")
    from hyperdjango.admin import HyperAdmin

    # Create a minimal admin instance for testing _collect_cache_stats
    # We need to call the method without a full app setup
    admin = object.__new__(HyperAdmin)

    # Ensure cache is LocMemCache
    lm = LocMemCache(max_size=100)
    set_cache(lm)

    stats = admin._collect_cache_stats()

    check("has query_cache key", "query_cache" in stats)
    check("has table_versions key", "table_versions" in stats)

    qc = stats["query_cache"]
    check("query_cache has hits", "hits" in qc)
    check("query_cache has misses", "misses" in qc)
    check("query_cache has hit_rate", "hit_rate" in qc)
    check("query_cache has invalidations", "invalidations" in qc)
    check("query_cache has table_count", "table_count" in qc)

    # LocMemCache stats
    check("has locmem key", "locmem" in stats)
    if "locmem" in stats:
        check("locmem has entry_count", "entry_count" in stats["locmem"])
        check("locmem has max_size", "max_size" in stats["locmem"])
        check("locmem has utilization", "utilization" in stats["locmem"])
        check("locmem max_size = 100", stats["locmem"]["max_size"] == 100)


def test_collect_cache_stats_two_tier():
    print("\n=== Stats Collection (TwoTierCache) ===")
    from hyperdjango.admin import HyperAdmin

    admin = object.__new__(HyperAdmin)

    l1 = LocMemCache(max_size=50)
    l2 = LocMemCache(max_size=200)
    tt = TwoTierCache(l1, l2, l1_ttl=10)

    # Generate some stats
    tt.set("k1", "v1", ttl=60)
    tt.get("k1")  # L1 hit
    l1.delete("k1")
    tt.get("k1")  # L2 hit + promotion
    tt.get("missing")  # miss

    set_cache(tt)
    stats = admin._collect_cache_stats()

    check("has two_tier key", "two_tier" in stats)
    if "two_tier" in stats:
        tt_stats = stats["two_tier"]
        check("two_tier has l1_hits", tt_stats["l1_hits"] == 1)
        check("two_tier has l2_hits", tt_stats["l2_hits"] == 1)
        check("two_tier has misses", tt_stats["misses"] == 1)
        check("two_tier has l1_hit_rate_pct", "l1_hit_rate_pct" in tt_stats)
        check("two_tier has l2_hit_rate_pct", "l2_hit_rate_pct" in tt_stats)
        check("two_tier has overall_hit_rate_pct", "overall_hit_rate_pct" in tt_stats)

    # Reset to LocMemCache for other tests
    set_cache(LocMemCache())


def test_hit_rate_formatting():
    print("\n=== Hit Rate Formatting ===")
    from hyperdjango.admin import HyperAdmin

    admin = object.__new__(HyperAdmin)
    set_cache(LocMemCache())

    stats = admin._collect_cache_stats()
    hit_rate = stats["query_cache"]["hit_rate"]
    check("hit_rate is formatted string", isinstance(hit_rate, str))
    check("hit_rate ends with %", hit_rate.endswith("%"))


def main():
    test_template_exists()
    test_template_variables()
    test_dashboard_link()
    test_auto_refresh()
    test_json_api_link()
    test_admin_methods()
    test_collect_cache_stats_empty()
    test_collect_cache_stats_two_tier()
    test_hit_rate_formatting()

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
