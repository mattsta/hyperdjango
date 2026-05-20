"""
Benchmark: Native Zig hash ring vs Python uhashring vs pure Python fallback.

Usage:
    uv run python scripts/bench_hashring.py
"""

import os
import sys
import time
from pathlib import Path

ITERATIONS = 1_000_000
NUM_NODES = 10


def bench_native_hashring():
    """Benchmark the native Zig ConsistentHashRing."""
    from hyperdjango.cache import LocMemCache
    from hyperdjango.cache_adapters import ConsistentHashRing

    nodes = {f"node{i}": LocMemCache() for i in range(NUM_NODES)}
    ring = ConsistentHashRing(nodes=nodes)

    # Warmup
    for i in range(1000):
        ring.get_node_name(f"key:{i}")

    start = time.perf_counter()
    for i in range(ITERATIONS):
        ring.get_node_name(f"key:{i}")
    elapsed = time.perf_counter() - start

    print(
        f"  Native Zig hash ring:  {elapsed:.3f}s ({ITERATIONS / elapsed:.0f} lookups/sec)"
    )
    return elapsed


def bench_uhashring():
    """Benchmark uhashring (Python library)."""
    try:
        # uhashring is an optional pip-installable comparison lib. Point
        # UHASHRING_SRC at a local checkout to bench against it, otherwise
        # fall back to whatever is importable on sys.path.
        uhashring_src = os.environ.get("UHASHRING_SRC")
        if uhashring_src:
            sys.path.insert(0, str(Path(uhashring_src).expanduser()))
        from uhashring import HashRing

        nodes = {f"node{i}": {"instance": i} for i in range(NUM_NODES)}
        ring = HashRing(nodes=nodes)

        # Warmup
        for i in range(1000):
            ring.get_node(f"key:{i}")

        start = time.perf_counter()
        for i in range(ITERATIONS):
            ring.get_node(f"key:{i}")
        elapsed = time.perf_counter() - start

        print(
            f"  uhashring (Python):    {elapsed:.3f}s ({ITERATIONS / elapsed:.0f} lookups/sec)"
        )
        return elapsed
    except ImportError:
        print("  uhashring: not available (skipped)")
        return None


def bench_native_hash_only():
    """Benchmark just the native hash function (no ring lookup)."""
    from hyperdjango.cache_adapters import ConsistentHashRing

    # Warmup
    for i in range(1000):
        ConsistentHashRing.hash_key(f"key:{i}")

    start = time.perf_counter()
    for i in range(ITERATIONS):
        ConsistentHashRing.hash_key(f"key:{i}")
    elapsed = time.perf_counter() - start

    print(
        f"  Native hash_key only:  {elapsed:.3f}s ({ITERATIONS / elapsed:.0f} hashes/sec)"
    )
    return elapsed


def bench_python_md5():
    """Benchmark Python MD5 for comparison."""
    import hashlib

    # Warmup
    for i in range(1000):
        int(hashlib.md5(f"key:{i}".encode(), usedforsecurity=False).hexdigest()[:8], 16)

    start = time.perf_counter()
    for i in range(ITERATIONS):
        int(hashlib.md5(f"key:{i}".encode(), usedforsecurity=False).hexdigest()[:8], 16)
    elapsed = time.perf_counter() - start

    print(
        f"  Python MD5 hash only:  {elapsed:.3f}s ({ITERATIONS / elapsed:.0f} hashes/sec)"
    )
    return elapsed


if __name__ == "__main__":
    print(f"Hash Ring Benchmark ({ITERATIONS:,} iterations, {NUM_NODES} nodes)")
    print("=" * 60)

    print("\nFull lookup (hash + ring search):")
    native_time = bench_native_hashring()
    uhash_time = bench_uhashring()

    if uhash_time:
        speedup = uhash_time / native_time
        print(f"\n  Speedup: {speedup:.1f}x faster than uhashring")

    print("\nHash function only:")
    bench_native_hash_only()
    bench_python_md5()

    print()
