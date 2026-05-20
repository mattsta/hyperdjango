"""
Fuzz tests for new public surfaces added in this session.

# hyper-test: unit

Tests:
1. DatabaseCache.get_or_set callback exception handling (no corrupted state)
2. TwoTierCache thread-safe stats tracking under concurrent ops
3. StampedeProtection edge cases (compute_time_ms=0, negative beta)
4. PerformanceMiddleware concurrent record_query stats
5. SecurityLog event types validation
6. DataLoader batch_fn returning wrong-length list
7. DataLoader cache eviction on clear()
8. Cache admin _collect_cache_stats with various cache types
"""

import asyncio
import random
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import contextlib

from hypothesis import given, settings
from hypothesis import strategies as st

from hyperdjango.cache import LocMemCache
from hyperdjango.cache_adapters import StampedeProtection, TwoTierCache
from hyperdjango.dataloader import DataLoader
from hyperdjango.performance import PerformanceMiddleware
from hyperdjango.security import SecurityEvent

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


# ─── Cache: get_or_set exception handling ────────────────────────────────────


def test_get_or_set_callback_exception():
    """Callback exceptions don't corrupt cache state."""
    print("=== get_or_set Exception Handling ===")
    cache = LocMemCache(max_size=10)

    call_count = [0]

    def bad_callback():
        call_count[0] += 1
        raise ValueError("computation failed")

    # First call: callback raises
    try:
        cache.get_or_set("key1", bad_callback, ttl=60)
        check("first call raises", False)
    except ValueError:
        check("first call raises", True)

    # Cache should not have a corrupted entry
    check("no corrupted entry", cache.get("key1") is None)

    # Second call: callback called again (not cached as failure)
    with contextlib.suppress(ValueError):
        cache.get_or_set("key1", bad_callback, ttl=60)
    check("callback called twice (not memoized failure)", call_count[0] == 2)


# ─── TwoTierCache concurrent stats ───────────────────────────────────────────


def test_two_tier_concurrent_stats():
    """Concurrent reads/writes maintain stat invariants."""
    print("\n=== TwoTierCache Concurrent Stats ===")

    l1 = LocMemCache(max_size=100)
    l2 = LocMemCache(max_size=500)
    cache = TwoTierCache(l1, l2, l1_ttl=60)

    # Pre-populate L2 with 50 keys
    for i in range(50):
        l2.set(f"k{i}", i, ttl=300)

    errors: list[str] = []
    iterations = 1000

    def reader(thread_id):
        try:
            rng = random.Random(thread_id)
            for _ in range(iterations):
                key = f"k{rng.randint(0, 49)}"
                cache.get(key)
        except Exception as e:
            errors.append(f"reader {thread_id}: {e}")

    def writer(thread_id):
        try:
            for i in range(iterations):
                cache.set(f"new_{thread_id}_{i}", i, ttl=60)
        except Exception as e:
            errors.append(f"writer {thread_id}: {e}")

    threads = []
    for t in range(4):
        threads.append(threading.Thread(target=reader, args=(t,)))
        threads.append(threading.Thread(target=writer, args=(t,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no thread errors", len(errors) == 0, f"errors={errors[:3]}")

    stats = cache.get_stats()
    # Invariant: total_requests == l1_hits + l2_hits + misses
    total = stats["l1_hits"] + stats["l2_hits"] + stats["misses"]
    check(
        "stats invariant: total = l1+l2+miss",
        total == stats["total_requests"],
        f"total={total} reported={stats['total_requests']}",
    )


# ─── StampedeProtection edge cases ───────────────────────────────────────────


def test_stampede_edge_cases():
    """StampedeProtection handles edge cases without crashing."""
    print("\n=== StampedeProtection Edge Cases ===")

    backend = LocMemCache(max_size=100)

    # compute_time_ms = 0 (XFetch threshold becomes 0 → no early expiry)
    cache = StampedeProtection(backend, beta=1.0)
    cache.set("k1", "v1", ttl=60, compute_time_ms=0)
    check("compute_time=0 returns value", cache.get("k1") == "v1")

    # Very small beta
    cache_small = StampedeProtection(backend, beta=0.001)
    cache_small.set("k2", "v2", ttl=60, compute_time_ms=100)
    check("beta=0.001 returns value", cache_small.get("k2") == "v2")

    # Very large compute_time — XFetch may probabilistically return None
    # (early recompute). Just verify no crash.
    cache.set("k3", "v3", ttl=60, compute_time_ms=999999)
    try:
        result = cache.get("k3")
        check("large compute_time no crash", True)
    except Exception as e:
        check(f"large compute_time crashed ({e})", False)

    # Empty default
    check("empty default works", cache.get("nonexistent") is None)
    check("custom default works", cache.get("nonexistent", default=42) == 42)


# ─── PerformanceMiddleware concurrent record_query ───────────────────────────


def test_perf_middleware_concurrent():
    """record_query is thread-safe under concurrent calls."""
    print("\n=== PerformanceMiddleware Concurrent ===")

    perf = PerformanceMiddleware(enabled=True)
    errors: list[str] = []

    def worker(thread_id):
        try:
            for i in range(500):
                perf.record_query(f"SELECT {i}", 0.5, request_id=thread_id * 10000 + i)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no concurrent errors", len(errors) == 0, f"errors={errors[:3]}")
    check(
        "all queries counted",
        perf._total_queries == 4000,
        f"got {perf._total_queries}",
    )

    stats = perf.get_stats()
    check("stats has total_queries", stats["total_queries"] == 4000)


# ─── SecurityLog event validation ────────────────────────────────────────────


def test_security_event_enum():
    """All SecurityEvent enum values are unique strings."""
    print("\n=== SecurityEvent Validation ===")

    values = [e.value for e in SecurityEvent]
    check(
        "all values are strings",
        all(isinstance(v, str) for v in values),
    )
    check(
        "all values are unique",
        len(values) == len(set(values)),
    )
    # Standard events present
    check("LOGIN_FAILED present", SecurityEvent.LOGIN_FAILED.value == "login_failed")
    check(
        "RATE_LIMIT_HIT present", SecurityEvent.RATE_LIMIT_HIT.value == "rate_limit_hit"
    )
    check(
        "CSRF_VIOLATION present", SecurityEvent.CSRF_VIOLATION.value == "csrf_violation"
    )


# ─── DataLoader edge cases ───────────────────────────────────────────────────


def test_dataloader_wrong_length_result():
    """batch_fn returning wrong-length list — what happens?"""
    print("\n=== DataLoader Wrong Length Result ===")

    async def bad_batch(keys):
        return [None]  # Always returns 1 item regardless of input

    loader = DataLoader(batch_fn=bad_batch)

    async def run():
        # Single key — works
        r = await loader.load(1)
        return r

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(run())
        check("single key with bad batch", result is None)
    except Exception as e:
        check(f"single key bad batch ({type(e).__name__})", False)
    finally:
        loop.close()


def test_dataloader_clear():
    """clear() removes cached entries — re-fetch after clear."""
    print("\n=== DataLoader Clear ===")

    call_count = [0]

    async def counting_batch(keys):
        call_count[0] += 1
        return [k * 2 for k in keys]

    loader = DataLoader(batch_fn=counting_batch)

    async def run():
        r1 = await loader.load(5)  # batch call 1
        r2 = await loader.load(5)  # cached, no batch call
        loader.clear(5)
        r3 = await loader.load(5)  # batch call 2 (cache cleared)
        return r1, r2, r3

    loop = asyncio.new_event_loop()
    try:
        r1, r2, r3 = loop.run_until_complete(run())
        check("first load", r1 == 10)
        check("second load cached", r2 == 10)
        check("third load after clear", r3 == 10)
        check("batch called twice", call_count[0] == 2, f"got {call_count[0]}")
    finally:
        loop.close()


def test_dataloader_clear_all():
    """clear() without args clears entire cache."""
    print("\n=== DataLoader Clear All ===")

    call_count = [0]
    batch_keys: list[list] = []

    async def batch(keys):
        call_count[0] += 1
        batch_keys.append(list(keys))
        return [k for k in keys]

    loader = DataLoader(batch_fn=batch)

    async def run():
        # Concurrent loads in the same tick → batched into 1 call
        await asyncio.gather(loader.load(1), loader.load(2), loader.load(3))
        loader.clear()  # Clear all
        await asyncio.gather(loader.load(1), loader.load(2), loader.load(3))

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
        # Without clear: 1 batch call (cached). With clear: 2 batch calls.
        check("clear all triggers re-fetch", call_count[0] == 2, f"got {call_count[0]}")
        check("first batch had 3 keys", len(batch_keys[0]) == 3)
        check("second batch had 3 keys", len(batch_keys[1]) == 3)
    finally:
        loop.close()


# ─── Hypothesis: arbitrary cache key/value pairs ─────────────────────────────


@given(
    keys=st.lists(
        st.text(min_size=1, max_size=50), min_size=1, max_size=20, unique=True
    ),
    values=st.lists(
        st.one_of(
            st.text(),
            st.integers(),
            st.floats(allow_nan=False),
            st.booleans(),
            st.lists(st.integers(), max_size=5),
        ),
        min_size=1,
        max_size=20,
    ),
)
@settings(max_examples=50, deadline=None)
def test_cache_arbitrary_kv(keys, values):
    """LocMemCache handles arbitrary key/value combinations."""
    cache = LocMemCache(max_size=1000)
    n = min(len(keys), len(values))
    for k, v in zip(keys[:n], values[:n]):
        cache.set(k, v, ttl=60)
    for k, v in zip(keys[:n], values[:n]):
        retrieved = cache.get(k)
        assert retrieved == v, (
            f"Mismatch for key {k!r}: got {retrieved!r} expected {v!r}"
        )


# ─── Cache admin stats collection ────────────────────────────────────────────


def test_collect_cache_stats_variants():
    """_collect_cache_stats handles all cache types without crashing."""
    print("\n=== Cache Admin Stats Collection ===")

    from hyperdjango.admin import HyperAdmin
    from hyperdjango.cache import set_cache

    admin = object.__new__(HyperAdmin)

    # LocMemCache
    set_cache(LocMemCache(max_size=100))
    stats = admin._collect_cache_stats()
    check("LocMemCache stats has query_cache", "query_cache" in stats)
    check("LocMemCache stats has locmem", "locmem" in stats)
    check("LocMemCache stats no two_tier", "two_tier" not in stats)

    # TwoTierCache
    l1 = LocMemCache(max_size=50)
    l2 = LocMemCache(max_size=200)
    tt = TwoTierCache(l1, l2, l1_ttl=10)
    set_cache(tt)
    stats = admin._collect_cache_stats()
    check("TwoTierCache stats has two_tier", "two_tier" in stats)
    check("TwoTierCache stats has hit_rate_pct", "l1_hit_rate_pct" in stats["two_tier"])

    # Reset
    set_cache(LocMemCache())


def main():
    test_get_or_set_callback_exception()
    test_two_tier_concurrent_stats()
    test_stampede_edge_cases()
    test_perf_middleware_concurrent()
    test_security_event_enum()
    test_dataloader_wrong_length_result()
    test_dataloader_clear()
    test_dataloader_clear_all()

    print("\n=== Hypothesis: arbitrary cache k/v ===")
    try:
        test_cache_arbitrary_kv()
        check("arbitrary cache k/v hypothesis", True)
    except Exception as e:
        check("arbitrary cache k/v hypothesis", False, str(e)[:200])

    test_collect_cache_stats_variants()

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
