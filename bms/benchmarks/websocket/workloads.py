"""Workload matrix: payload sizes, frame types, and concurrency levels.

`--quick` trims every axis to a fast smoke-sized matrix (for CI / iterating
on the harness itself); the full matrix is what backs a real report.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Payload sizes in bytes. 24 matches the RFC 6455 spec example size class;
# 262144 (256 KiB) stays comfortably under both servers' configured 16 MB
# max frame size while still exercising multi-syscall writes.
# Dense power-of-two ladder from 32 B to 256 KiB — enough points to see the
# per-payload curve, not just endpoints. Drives throughput-vs-payload AND
# latency-vs-payload (both index on payload_sizes).
FULL_PAYLOAD_SIZES = [
    32,
    64,
    128,
    256,
    512,
    1024,
    2048,
    4096,
    8192,
    16384,
    32768,
    65536,
    131072,
    262144,
]
QUICK_PAYLOAD_SIZES = [32, 1024, 16384, 262144]

# Dense concurrency ladder — drives throughput-vs-concurrency AND connection-
# scaling (both index on concurrency_levels).
FULL_CONCURRENCY_LEVELS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
QUICK_CONCURRENCY_LEVELS = [1, 8, 64, 256]

FRAME_TYPES = ("text", "binary")

FULL_THROUGHPUT_DURATION_S = 2.0
QUICK_THROUGHPUT_DURATION_S = 1.0

FULL_LATENCY_SAMPLES = 500
QUICK_LATENCY_SAMPLES = 100


@dataclass(frozen=True)
class Matrix:
    payload_sizes: list[int]
    concurrency_levels: list[int]
    throughput_duration_s: float
    latency_samples: int


def build_matrix(quick: bool) -> Matrix:
    if quick:
        return Matrix(
            payload_sizes=QUICK_PAYLOAD_SIZES,
            concurrency_levels=QUICK_CONCURRENCY_LEVELS,
            throughput_duration_s=QUICK_THROUGHPUT_DURATION_S,
            latency_samples=QUICK_LATENCY_SAMPLES,
        )
    return Matrix(
        payload_sizes=FULL_PAYLOAD_SIZES,
        concurrency_levels=FULL_CONCURRENCY_LEVELS,
        throughput_duration_s=FULL_THROUGHPUT_DURATION_S,
        latency_samples=FULL_LATENCY_SAMPLES,
    )


def make_payload(size: int, frame_type: str, seed: int = 0) -> str | bytes:
    """Deterministic pseudo-random payload of exactly `size` bytes/chars."""
    rng = random.Random(seed * 1_000_003 + size)
    if frame_type == "binary":
        return bytes(rng.getrandbits(8) for _ in range(size))
    # Printable ASCII text payload (JSON-safe-ish, no quoting needed for our use).
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
    return "".join(rng.choice(alphabet) for _ in range(size))
