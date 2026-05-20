"""Large-body ``fast_json_dumps`` microbench — isolates the per-request
allocation path exercised by the ``/json`` benchmark route.

The HTTP A/B (``/json`` vs ``/jsoncached``) showed a 10-16% throughput gap that
WIDENS with worker count — the signature of allocator contention rather than a
constant per-request cost. This bench reproduces that signature in-process, with
no sockets involved, so the serializer's allocation behaviour can be measured
directly and a fix validated without a full wire run.

Modes (per payload size):
  serialize  — ``fast_json_dumps(obj)`` on a PREBUILT dict (pure serializer)
  build      — ``fast_json_dumps({"data": "x" * n})`` (what ``/json`` does)
  cached     — dict lookup of a prebuilt body (what ``/jsoncached`` does)

Each mode runs at several thread counts. The number that matters is
``scale_eff`` = aggregate_ops_per_sec(T) / (ops_per_sec(1) * T). A serializer
whose allocations are thread-local holds scale_eff near 1.0; one that churns a
process-wide allocator decays as T grows.

Run: uv run python scripts/bench_json_dumps_large.py
     uv run python scripts/bench_json_dumps_large.py --label after
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from hyperdjango.native import fast_json_dumps

LOGS = Path(__file__).resolve().parent.parent / "logs"

# 64KiB is the box benchmark's payload; the neighbours bracket it so the trend
# across sizes separates "fixed overhead" from "scales with payload".
PAYLOAD_SIZES: tuple[int, ...] = (1024, 16384, 65536, 262144)
THREAD_COUNTS: tuple[int, ...] = (1, 2, 4, 8)
RUNS = 5


@dataclass(slots=True)
class ModeResult:
    """One (mode, payload, threads) cell: median aggregate throughput."""

    mode: str
    payload: int
    threads: int
    ops_per_sec: float
    ns_per_op: float
    samples: list[float] = field(default_factory=list)


@dataclass(slots=True)
class BenchReport:
    label: str
    python: str
    results: list[ModeResult] = field(default_factory=list)


def _iters_for(payload: int) -> int:
    """Iteration count per thread, tuned so every cell runs >= ~0.4s."""
    if payload <= 1024:
        return 120_000
    if payload <= 16384:
        return 60_000
    if payload <= 65536:
        return 30_000
    return 8_000


def _run_threaded(worker, threads: int) -> float:
    """Run ``worker(barrier)`` on ``threads`` threads; return wall seconds."""
    barrier = threading.Barrier(threads + 1)
    workers = [
        threading.Thread(target=worker, args=(barrier,), daemon=True)
        for _ in range(threads)
    ]
    for t in workers:
        t.start()
    barrier.wait()  # all threads armed
    started = time.perf_counter()
    for t in workers:
        t.join()
    return time.perf_counter() - started


def _bench_cell(mode: str, payload: int, threads: int) -> ModeResult:
    iters = _iters_for(payload)
    filler = "x" * payload
    prebuilt_obj = {"data": filler}
    prebuilt_body = fast_json_dumps(prebuilt_obj)
    cache: dict[int, bytes] = {payload: prebuilt_body}

    if mode == "serialize":

        def work() -> None:
            dumps = fast_json_dumps
            obj = prebuilt_obj
            for _ in range(iters):
                dumps(obj)

    elif mode == "build":

        def work() -> None:
            dumps = fast_json_dumps
            n = payload
            for _ in range(iters):
                dumps({"data": "x" * n})

    elif mode == "cached":

        def work() -> None:
            n = payload
            c = cache
            for _ in range(iters):
                body = c.get(n)
                if body is None:  # pragma: no cover - warm cache
                    body = fast_json_dumps({"data": "x" * n})
                    c[n] = body

    else:  # pragma: no cover - guarded by caller
        raise ValueError(f"unknown mode {mode}")

    def worker(barrier: threading.Barrier) -> None:
        barrier.wait()
        work()

    samples: list[float] = []
    for _ in range(RUNS):
        elapsed = _run_threaded(worker, threads)
        samples.append((iters * threads) / elapsed)

    ops = statistics.median(samples)
    return ModeResult(
        mode=mode,
        payload=payload,
        threads=threads,
        ops_per_sec=ops,
        ns_per_op=1e9 / ops * threads,
        samples=samples,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    report = BenchReport(label=args.label, python=sys.version)

    print(f"=== bench_json_dumps_large ({args.label}) ===")
    # Warm up: first call per thread may lazily size internal buffers.
    for size in PAYLOAD_SIZES:
        fast_json_dumps({"data": "x" * size})

    for size in PAYLOAD_SIZES:
        print(f"\n--- payload {size} bytes ---")
        header = (
            f"{'mode':<10} {'thr':>4} {'ops/s':>12} {'ns/op':>10} {'scale_eff':>10}"
        )
        print(header)
        for mode in ("serialize", "build", "cached"):
            single: float | None = None
            for threads in THREAD_COUNTS:
                cell = _bench_cell(mode, size, threads)
                report.results.append(cell)
                if threads == 1:
                    single = cell.ops_per_sec
                eff = cell.ops_per_sec / (single * threads) if single else 0.0
                print(
                    f"{mode:<10} {threads:>4} {cell.ops_per_sec:>12,.0f} "
                    f"{cell.ns_per_op:>10,.0f} {eff:>10.3f}"
                )

    out = LOGS / f"bench_json_dumps_large_{args.label}.json"
    out.write_text(
        json.dumps(
            {
                "label": report.label,
                "python": report.python,
                "results": [
                    {
                        "mode": r.mode,
                        "payload": r.payload,
                        "threads": r.threads,
                        "ops_per_sec": r.ops_per_sec,
                        "ns_per_op": r.ns_per_op,
                        "samples": r.samples,
                    }
                    for r in report.results
                ],
            },
            indent=2,
        )
    )
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
