"""Latency/throughput summary helpers for the HTTP benchmark."""

from __future__ import annotations


def percentile(sorted_samples: list[float], p: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list (seconds)."""
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    k = (len(sorted_samples) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_samples) - 1)
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * (k - lo)


def summarize(latencies: list[float], duration_s: float) -> dict:
    """Turn raw per-request latencies (seconds) into a metrics dict (ms)."""
    s = sorted(latencies)
    n = len(s)
    return {
        "requests": n,
        "throughput_rps": (n / duration_s) if duration_s > 0 else 0.0,
        "mean_ms": (sum(s) / n * 1000.0) if n else 0.0,
        "p50_ms": percentile(s, 50) * 1000.0,
        "p90_ms": percentile(s, 90) * 1000.0,
        "p99_ms": percentile(s, 99) * 1000.0,
        "max_ms": (s[-1] * 1000.0) if n else 0.0,
    }
