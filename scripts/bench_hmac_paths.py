"""
Benchmark HMAC-SHA256 paths to confirm:
1. OpenSSL backend uses hardware SHA-NI / NEON (should be ~1GB/s on modern CPUs)
2. hmac.digest() one-shot path vs hmac.new().hexdigest() overhead
3. Whether our public_id._compute_hmac can use the fast path

Run: uv run python scripts/bench_hmac_paths.py
"""

import hashlib
import hmac
import time

KEY = b"hypernews-dev-hmac-key-2026-abc123xyz456"
MESSAGE = b"hn_posts:abc123def456"  # typical short input from public_id
ITERATIONS = 1_000_000  # 1M ops at ~1μs = ~1 second per run
RUNS = 5  # median across 5 runs


def time_fn(name: str, fn: object) -> tuple[float, list[float]]:
    """Run `fn` ITERATIONS × RUNS times, report (median_ns, per_run_ns).

    Warmup before each measurement; median across multiple runs filters
    CPU jitter. Single-run micro-benchmarks at 200K iters have been
    observed to vary ±30% between invocations due to CPU frequency
    scaling, GC, and scheduler noise.
    """
    # Warmup
    for _ in range(10_000):
        fn()
    run_ns: list[float] = []
    for _ in range(RUNS):
        start = time.perf_counter_ns()
        for _ in range(ITERATIONS):
            fn()
        elapsed = time.perf_counter_ns() - start
        run_ns.append(elapsed / ITERATIONS)
    median = sorted(run_ns)[len(run_ns) // 2]
    return median, run_ns


def _old_hmac_hexdigest():
    """Current pattern before unification — hmac.new() + hexdigest()[:16]."""
    mac = hmac.new(KEY, MESSAGE, hashlib.sha256)
    return mac.hexdigest()[:16]


def _digest_hex_slice():
    """hmac.digest() + .hex()[:16] — one-shot via _hashlib."""
    return hmac.digest(KEY, MESSAGE, "sha256").hex()[:16]


def _digest_slice_then_hex():
    """hmac.digest()[:8].hex() — slice bytes first, then hex-encode."""
    return hmac.digest(KEY, MESSAGE, "sha256")[:8].hex()


def _raw_sha256():
    """Raw hashlib.sha256 — no HMAC, just hashing."""
    return hashlib.sha256(MESSAGE).hexdigest()


def main() -> None:
    print("=" * 70)
    print("  HMAC-SHA256 path benchmark")
    print(f"  Iterations: {ITERATIONS:,} × {RUNS} runs (median reported)")
    print(f"  Key: {len(KEY)} bytes")
    print(f"  Message: {len(MESSAGE)} bytes")
    print("=" * 70)

    import ssl

    print(f"\n  OpenSSL: {ssl.OPENSSL_VERSION}")
    print(
        f"  Hardware SHA (SHA-NI/NEON) via OpenSSL: "
        f"{'likely ENABLED (OpenSSL 3.x on modern CPU)' if ssl.OPENSSL_VERSION.startswith('OpenSSL 3') else 'unknown'}"
    )

    paths = [
        ("hmac.new(...).hexdigest()[:16] (old)", _old_hmac_hexdigest),
        ("hmac.digest(...).hex()[:16] (one-shot)", _digest_hex_slice),
        ("hmac.digest(...)[:8].hex() (slice first)", _digest_slice_then_hex),
        ("hashlib.sha256(...).hexdigest() (no hmac)", _raw_sha256),
    ]

    print(f"\n  {'Path':<45} {'ns/op':>10} {'jitter':>10}  per-run(ns)")
    print("  " + "-" * 82)

    results: dict[str, float] = {}
    for label, fn in paths:
        median, per_run = time_fn(label, fn)
        results[label] = median
        lo, hi = min(per_run), max(per_run)
        jitter_pct = ((hi - lo) / median * 100 / 2) if median else 0
        per_run_str = " ".join(f"{r:.0f}" for r in per_run)
        print(f"  {label:<45} {median:>10.0f} ±{jitter_pct:>8.1f}%  [{per_run_str}]")

    print("\n  Throughput test (1 MB blocks, 1,000 iters):")
    # Longer throughput test for stable GB/s number
    data = b"A" * (1024 * 1024)
    iters = 1000
    start = time.perf_counter_ns()
    for _ in range(iters):
        hashlib.sha256(data).digest()
    elapsed_s = (time.perf_counter_ns() - start) / 1e9
    gbs = iters / (elapsed_s * 1024)
    print(f"    hashlib.sha256: {gbs:.2f} GB/s over {elapsed_s:.3f}s ({iters} iters)")
    print(
        f"    {'(>=1 GB/s = hardware SHA-NI active)' if gbs >= 1.0 else '(software fallback)'}"
    )

    print("\n" + "=" * 70)
    old_ns = results["hmac.new(...).hexdigest()[:16] (old)"]
    fast_ns = results["hmac.digest(...).hex()[:16] (one-shot)"]
    speedup = old_ns / fast_ns if fast_ns else 0
    saved_per_call = old_ns - fast_ns
    print(
        f"  hmac.digest() fast path is {speedup:.2f}x faster "
        f"({saved_per_call:.0f}ns saved per call)"
    )


if __name__ == "__main__":
    main()
