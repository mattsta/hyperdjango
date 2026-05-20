"""
DataLoader stats + monitoring tests.

# hyper-test: unit

Tests:
1.  Fresh loader has zero stats
2.  load() increments total_loads
3.  Cache hit vs cache miss are counted separately
4.  Concurrent load_many produces single batch call
5.  Sequential loads produce multiple batch calls
6.  Chunking for keys > max_batch_size
7.  batch_fn error increments errors counter
8.  reset_stats() clears all counters
9.  Deduplicated concurrent loads count as cache hits (not duplicate batches)
10. Hit rate and avg batch size properties
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import contextlib

from hyperdjango.dataloader import DataLoader, DataLoaderStats

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


def run(coro):
    """Run an async test in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_fresh_loader_zero_stats():
    print("=== Fresh Loader Zero Stats ===")

    async def batch(keys):
        return [k for k in keys]

    loader = DataLoader(batch_fn=batch)
    stats = loader.get_stats()
    check("total_loads == 0", stats.total_loads == 0)
    check("cache_hits == 0", stats.cache_hits == 0)
    check("cache_misses == 0", stats.cache_misses == 0)
    check("batch_calls == 0", stats.batch_calls == 0)
    check("keys_batched == 0", stats.keys_batched == 0)
    check("hit_rate == 0.0", stats.hit_rate == 0.0)
    check("avg_batch_size == 0.0", stats.avg_batch_size == 0.0)


def test_load_increments_total_loads():
    print("\n=== load() Increments total_loads ===")

    async def batch(keys):
        return [k * 2 for k in keys]

    loader = DataLoader(batch_fn=batch)

    async def work():
        await loader.load(1)
        await loader.load(2)
        await loader.load(3)

    run(work())
    stats = loader.get_stats()
    check("total_loads == 3", stats.total_loads == 3)
    check("cache_misses == 3", stats.cache_misses == 3)


def test_cache_hits_vs_misses():
    print("\n=== Cache Hits vs Misses ===")

    async def batch(keys):
        return [k * 10 for k in keys]

    loader = DataLoader(batch_fn=batch)

    async def work():
        # First: 3 misses
        await loader.load(1)
        await loader.load(2)
        await loader.load(3)
        # Now: 3 hits (all cached)
        await loader.load(1)
        await loader.load(2)
        await loader.load(3)

    run(work())
    stats = loader.get_stats()
    check("total_loads == 6", stats.total_loads == 6)
    check("cache_hits == 3", stats.cache_hits == 3)
    check("cache_misses == 3", stats.cache_misses == 3)
    check("hit_rate == 0.5", abs(stats.hit_rate - 0.5) < 1e-9)


def test_concurrent_loads_batch():
    print("\n=== Concurrent Load Batching ===")

    async def batch(keys):
        return [k for k in keys]

    loader = DataLoader(batch_fn=batch)

    async def work():
        await asyncio.gather(loader.load(1), loader.load(2), loader.load(3))

    run(work())
    stats = loader.get_stats()
    check("total_loads == 3", stats.total_loads == 3)
    check("batch_calls == 1 (single batch)", stats.batch_calls == 1)
    check("keys_batched == 3", stats.keys_batched == 3)
    check("largest_batch == 3", stats.largest_batch == 3)


def test_sequential_loads_multiple_batches():
    print("\n=== Sequential Loads Multiple Batches ===")

    async def batch(keys):
        return [k for k in keys]

    loader = DataLoader(batch_fn=batch)

    async def work():
        # Each sequential await → separate batch call (different event loop ticks)
        await loader.load(1)
        await loader.load(2)
        await loader.load(3)

    run(work())
    stats = loader.get_stats()
    check("total_loads == 3", stats.total_loads == 3)
    check("batch_calls == 3 (sequential)", stats.batch_calls == 3)
    check("avg_batch_size == 1", stats.avg_batch_size == 1.0)


def test_chunking_beyond_max_batch_size():
    print("\n=== Chunking Beyond max_batch_size ===")

    async def batch(keys):
        return [k for k in keys]

    loader = DataLoader(batch_fn=batch, max_batch_size=5)

    async def work():
        # Load 12 keys concurrently → 3 chunks of sizes 5, 5, 2
        await asyncio.gather(*(loader.load(i) for i in range(12)))

    run(work())
    stats = loader.get_stats()
    check("total_loads == 12", stats.total_loads == 12)
    check("batch_calls == 3 (chunked)", stats.batch_calls == 3)
    check("keys_batched == 12", stats.keys_batched == 12)
    check("largest_batch == 5", stats.largest_batch == 5)


def test_batch_fn_error_counted():
    print("\n=== batch_fn Errors Counted ===")

    async def bad_batch(keys):
        raise RuntimeError("boom")

    loader = DataLoader(batch_fn=bad_batch)

    async def work():
        with contextlib.suppress(RuntimeError):
            await loader.load(1)

    run(work())
    stats = loader.get_stats()
    check("total_loads == 1", stats.total_loads == 1)
    check("errors == 1", stats.errors == 1)


def test_reset_stats():
    print("\n=== reset_stats() Clears Counters ===")

    async def batch(keys):
        return [k for k in keys]

    loader = DataLoader(batch_fn=batch)

    async def work():
        await loader.load(1)
        await loader.load(2)

    run(work())
    assert loader.get_stats().total_loads == 2
    loader.reset_stats()
    stats = loader.get_stats()
    check("reset: total_loads == 0", stats.total_loads == 0)
    check("reset: batch_calls == 0", stats.batch_calls == 0)
    check("reset: cache_misses == 0", stats.cache_misses == 0)


def test_concurrent_same_key_deduplicated():
    print("\n=== Concurrent Same-Key Deduplication ===")

    call_count = [0]

    async def counting_batch(keys):
        call_count[0] += 1
        return [k for k in keys]

    loader = DataLoader(batch_fn=counting_batch)

    async def work():
        # 5 concurrent loads of the same key → 1 batch call with 1 unique key
        await asyncio.gather(*(loader.load(42) for _ in range(5)))

    run(work())
    check("batch called once for 5 same-key loads", call_count[0] == 1)
    stats = loader.get_stats()
    check("total_loads == 5", stats.total_loads == 5)
    # 1 miss + 4 dedupe hits
    check("cache_misses == 1", stats.cache_misses == 1)
    check("cache_hits == 4 (dedupe)", stats.cache_hits == 4)
    check("keys_batched == 1 (unique key)", stats.keys_batched == 1)


def test_stats_dataclass_is_slots():
    print("\n=== DataLoaderStats slots=True ===")
    stats = DataLoaderStats()
    # Adding unknown attr should fail with slots=True
    try:
        stats.random_attr = 42
        check("slots enforced", False)
    except AttributeError, TypeError:
        check("slots enforced (cannot add random attr)", True)


def main():
    test_fresh_loader_zero_stats()
    test_load_increments_total_loads()
    test_cache_hits_vs_misses()
    test_concurrent_loads_batch()
    test_sequential_loads_multiple_batches()
    test_chunking_beyond_max_batch_size()
    test_batch_fn_error_counted()
    test_reset_stats()
    test_concurrent_same_key_deduplicated()
    test_stats_dataclass_is_slots()

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
