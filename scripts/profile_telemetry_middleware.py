"""
cProfile the telemetry middleware hot path (task #258).

# hyper-test: pure (excluded — this is a profiling tool, not a test)

Drives 30K requests through TelemetryMiddleware in NeverSample mode (so
every span is a NoopSpan — exercising ONLY the per-request floor cost
without measuring slot claim/release) and dumps the cProfile top-30 by
cumulative time.

Why NeverSample, not RatioSample(0.01)?
  Sampling at 1% means 99% of requests take the noop path and 1% take
  the slot-allocation path. cProfile averages those, blurring the floor
  signal. NeverSample gives a clean floor profile — every call is the
  same code path.

Why no "disabled" baseline?
  The disabled path bails at the first `is_enabled()` branch — there's
  nothing to optimize there. We profile the path that matters.

Expected top hits (before optimization):
  - asynccontextmanager `__aenter__`/`__aexit__` plus the underlying
    `Tracer.start_span` generator frame
  - `_make_span` ContextVar set/reset + `_NOOP_SPAN` returns
  - `parse_traceparent(None)` early-return
  - `_make_request` (the bench artifact, ignored by readers)
  - `time.monotonic_ns` x2
  - `_metric_counter_vec_inc` + `_metric_histogram_vec_observe` FFI

Run:
    uv run python scripts/profile_telemetry_middleware.py [iters]
"""

import asyncio
import cProfile
import pstats
import sys
import time
from io import StringIO
from pathlib import Path

from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.telemetry import (
    InMemorySink,
    NeverSample,
    TelemetryMiddleware,
    Tracer,
    disable,
    enable,
)

LOGS = Path(__file__).resolve().parent.parent / "logs"


def _make_request() -> Request:
    return Request(
        method="GET",
        path="/profile",
        headers={"host": "profile.local", "user-agent": "profile/1.0"},
    )


async def _handler(request: Request) -> Response:
    return Response(status=200, headers={})


async def _drive(middleware: TelemetryMiddleware, iters: int) -> None:
    async def call_next(request: Request) -> Response:
        return await _handler(request)

    for _ in range(iters):
        req = _make_request()
        await middleware(req, call_next)


def main() -> int:
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 30_000
    LOGS.mkdir(parents=True, exist_ok=True)

    enable()
    sink = InMemorySink(max_spans=1024)
    tracer = Tracer("profile", sampler=NeverSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[sink],
        drain_interval_seconds=600.0,  # never tick during profile
    )

    print("=" * 72)
    print(f"  cProfile: TelemetryMiddleware floor ({iters:,} requests, NeverSample)")
    print("=" * 72)

    # WARMUP — let the JIT/cache warm before measuring
    asyncio.run(_drive(mw, 1000))

    profiler = cProfile.Profile()
    t0 = time.perf_counter_ns()
    profiler.enable()
    asyncio.run(_drive(mw, iters))
    profiler.disable()
    elapsed_ns = time.perf_counter_ns() - t0

    ns_per_req = elapsed_ns / iters
    print(f"\n  Wall time:    {elapsed_ns / 1e9:.3f} s")
    print(f"  Per request:  {ns_per_req:,.0f} ns ({ns_per_req / 1000:.2f} μs)")
    print()

    buf = StringIO()
    stats = pstats.Stats(profiler, stream=buf)
    stats.sort_stats("cumulative")
    stats.print_stats(40)
    cumulative = buf.getvalue()

    buf2 = StringIO()
    stats2 = pstats.Stats(profiler, stream=buf2)
    stats2.sort_stats("tottime")
    stats2.print_stats(40)
    tottime = buf2.getvalue()

    out_path = LOGS / "profile_telemetry_middleware.txt"
    with out_path.open("w") as f:
        f.write(
            f"# cProfile of TelemetryMiddleware ({iters:,} requests, NeverSample)\n"
        )
        f.write(f"# Wall time: {elapsed_ns / 1e9:.3f} s\n")
        f.write(f"# Per request: {ns_per_req:,.0f} ns ({ns_per_req / 1000:.2f} μs)\n\n")
        f.write("=" * 72 + "\n")
        f.write("BY CUMULATIVE TIME\n")
        f.write("=" * 72 + "\n")
        f.write(cumulative)
        f.write("\n" + "=" * 72 + "\n")
        f.write("BY TOTAL TIME (self time)\n")
        f.write("=" * 72 + "\n")
        f.write(tottime)

    print(f"  Wrote: {out_path}")
    print()
    print("─" * 72)
    print("TOP-25 BY TOTAL TIME (self time — what to actually optimize)")
    print("─" * 72)
    # Print top-25 of tottime to stdout for quick triage
    short = StringIO()
    short_stats = pstats.Stats(profiler, stream=short)
    short_stats.sort_stats("tottime")
    short_stats.print_stats(25)
    print(short.getvalue())

    mw.shutdown()
    disable()
    return 0


if __name__ == "__main__":
    sys.exit(main())
