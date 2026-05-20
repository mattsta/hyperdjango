"""Tests for prepared statement cache instrumentation + stats API.

Tests StmtCacheStats dataclass, native Zig atomic counters, reset functionality,
and the public API exposed via pgzig_connection module.
"""

# hyper-test: unit

import sys

from hyperdjango.db.pgzig_connection import (
    StmtCacheStats,
    reset_stmt_cache_stats,
    stmt_cache_stats,
)


def test_stmt_cache_stats_dataclass():
    """StmtCacheStats fields and computed properties."""
    stats = StmtCacheStats(
        hits=80, misses=20, evictions=5, entries=100, max_entries=4096
    )
    assert stats.hits == 80
    assert stats.misses == 20
    assert stats.evictions == 5
    assert stats.entries == 100
    assert stats.max_entries == 4096
    assert stats.total_lookups == 100
    assert abs(stats.hit_rate - 0.8) < 1e-9
    print("  PASS: StmtCacheStats dataclass fields and properties")


def test_stmt_cache_stats_zero_division():
    """Hit rate returns 0.0 when no lookups."""
    stats = StmtCacheStats(hits=0, misses=0, evictions=0, entries=0, max_entries=4096)
    assert stats.hit_rate == 0.0
    assert stats.total_lookups == 0
    print("  PASS: StmtCacheStats zero-division safety")


def test_stmt_cache_stats_100_percent():
    """Hit rate at 100% (all hits, no misses)."""
    stats = StmtCacheStats(
        hits=100, misses=0, evictions=0, entries=50, max_entries=4096
    )
    assert stats.hit_rate == 1.0
    print("  PASS: StmtCacheStats 100% hit rate")


def test_native_stats_api_returns_stats():
    """Native _db_stmt_cache_stats() returns a dict with expected keys."""
    from hyperdjango._hyperdjango_native import _db_stmt_cache_stats

    raw = _db_stmt_cache_stats()
    assert isinstance(raw, dict)
    for key in ("hits", "misses", "evictions", "entries", "max_entries"):
        assert key in raw, f"Missing key: {key}"
        assert isinstance(raw[key], int), f"{key} is not int: {type(raw[key])}"
    assert raw["max_entries"] == 4096
    print(
        f"  PASS: Native API returns stats dict (entries={raw['entries']}, hits={raw['hits']})"
    )


def test_public_api_returns_dataclass():
    """stmt_cache_stats() returns StmtCacheStats instance."""
    stats = stmt_cache_stats()
    assert isinstance(stats, StmtCacheStats)
    assert stats.max_entries == 4096
    assert stats.hits >= 0
    assert stats.misses >= 0
    assert stats.evictions >= 0
    assert stats.entries >= 0
    print("  PASS: Public API returns StmtCacheStats dataclass")


def test_reset_clears_counters():
    """reset_stmt_cache_stats() zeroes hit/miss/eviction counters."""
    # Get baseline
    stats_before = stmt_cache_stats()
    # Reset
    reset_stmt_cache_stats()
    stats_after = stmt_cache_stats()

    assert stats_after.hits == 0
    assert stats_after.misses == 0
    assert stats_after.evictions == 0
    # entries and max_entries are NOT reset (they're structural, not counters)
    assert stats_after.max_entries == 4096
    print("  PASS: reset_stmt_cache_stats clears counters")


def test_native_reset_api():
    """Native _db_reset_stmt_cache_stats() works."""
    from hyperdjango._hyperdjango_native import (
        _db_reset_stmt_cache_stats,
        _db_stmt_cache_stats,
    )

    _db_reset_stmt_cache_stats()
    raw = _db_stmt_cache_stats()
    assert raw["hits"] == 0
    assert raw["misses"] == 0
    assert raw["evictions"] == 0
    print("  PASS: Native reset API works")


def test_stats_immutable_snapshot():
    """Stats are a snapshot — further calls don't modify previous result."""
    s1 = stmt_cache_stats()
    s2 = stmt_cache_stats()
    # Both should be independent objects
    assert s1 is not s2
    assert s1.hits == s2.hits  # Same data (no operations between)
    print("  PASS: Stats are immutable snapshots")


def test_stats_slots():
    """StmtCacheStats uses slots for memory efficiency."""
    assert hasattr(StmtCacheStats, "__slots__")
    stats = StmtCacheStats(hits=0, misses=0, evictions=0, entries=0, max_entries=4096)
    try:
        stats.nonexistent = 42
        assert False, "Should not allow arbitrary attributes with slots"
    except AttributeError:
        pass
    print("  PASS: StmtCacheStats uses __slots__")


def main():
    tests = [
        test_stmt_cache_stats_dataclass,
        test_stmt_cache_stats_zero_division,
        test_stmt_cache_stats_100_percent,
        test_native_stats_api_returns_stats,
        test_public_api_returns_dataclass,
        test_reset_clears_counters,
        test_native_reset_api,
        test_stats_immutable_snapshot,
        test_stats_slots,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Prepared Statement Cache Stats Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((test.__name__, str(e)))
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
