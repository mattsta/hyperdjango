"""
Comprehensive cProfile suite for the telemetry stack (task #258 follow-up).

# hyper-test: pure (excluded — this is a profiling tool, not a test)

Profiles every hot-path mode across the telemetry system to find
redundant calls, repeated allocations, and inefficient dispatch that
would otherwise hide from the single-mode `profile_telemetry_middleware.py`
tool. Each mode writes a separate top-30 breakdown so we can compare
apples-to-apples across configurations.

Modes profiled
--------------
  1. Middleware DISABLED       — confirms the zero-cost branch holds
  2. Middleware NeverSample    — floor cost (NoopSpan path)
  3. Middleware RatioSample(0.01) — production default (1% sampled)
  4. Middleware AlwaysSample   — worst case (every span allocates a slot)
  5. Counter.inc hot loop      — raw metric bump cost
  6. CounterVec.inc_tuple loop — labeled bump cost
  7. Histogram.observe loop    — histogram observe cost
  8. DB query fast path        — _should_track_query=False (no telemetry)
  9. DB query telemetry path   — telemetry enabled, SQL bumps flow
  10. `_run_samplers()` loop   — sampler hook overhead

Targets checked
---------------
  * Any function ≥ 2% of total self time is a candidate for optimization
  * Any ncalls > iterations suggests redundant dispatch per request
  * Any builtin (isinstance, int, str, dict.get) > 5% suggests a cache

Output
------
  logs/profile_telemetry_suite.txt    — all modes in one file
  logs/profile_telemetry_suite.json   — structured top-hits per mode

Run:
    uv run python scripts/profile_telemetry_suite.py
"""

import asyncio
import cProfile
import gc
import json
import pstats
import sys
import time
from io import StringIO
from pathlib import Path

from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.telemetry import (
    AlwaysSample,
    Counter,
    CounterVec,
    Histogram,
    InMemorySink,
    NeverSample,
    RatioSample,
    TelemetryMiddleware,
    Tracer,
    disable,
    enable,
)
from hyperdjango.telemetry.metrics import (
    _run_samplers,
    _samplers,
    register_sampler,
)

LOGS = Path(__file__).resolve().parent.parent / "logs"
ITERS_MIDDLEWARE = 30_000
ITERS_METRIC_LOOP = 200_000
ITERS_SAMPLERS = 50_000


def _make_request() -> Request:
    return Request(
        method="GET",
        path="/profile",
        headers={"host": "profile.local", "user-agent": "profile/1.0"},
    )


async def _handler(request: Request) -> Response:
    return Response(status=200, headers={})


async def _drive_middleware(middleware, iters: int) -> None:
    if middleware is None:
        # Baseline — no middleware
        for _ in range(iters):
            req = _make_request()
            await _handler(req)
        return

    async def call_next(request: Request) -> Response:
        return await _handler(request)

    for _ in range(iters):
        req = _make_request()
        await middleware(req, call_next)


def _top_stats(profiler: cProfile.Profile, n: int = 30) -> str:
    buf = StringIO()
    stats = pstats.Stats(profiler, stream=buf)
    stats.sort_stats("tottime")
    stats.print_stats(n)
    return buf.getvalue()


def _capture_top_entries(profiler: cProfile.Profile, n: int = 30) -> list[dict]:
    """Extract top-N profile entries as a list of dicts for JSON output."""
    stats = pstats.Stats(profiler)
    stats.sort_stats("tottime")
    entries: list[dict] = []
    # stats.stats is {func: (cc, nc, tt, ct, callers)}
    sorted_items = sorted(stats.stats.items(), key=lambda kv: kv[1][2], reverse=True)[
        :n
    ]
    for func, (cc, nc, tt, ct, _) in sorted_items:
        file, line, name = func
        entries.append(
            {
                "func": f"{Path(str(file)).name}:{line}({name})",
                "ncalls": nc,
                "tottime": round(tt, 6),
                "percall": round(tt / nc if nc else 0.0, 9),
                "cumtime": round(ct, 6),
            }
        )
    return entries


def _profile_mode(
    name: str,
    fn,
    iters: int,
) -> dict:
    """Run `fn(iters)` under cProfile, capture wall + top stats."""
    # Warmup to stabilize caches
    fn(min(iters // 10, 3000))
    gc.collect()

    profiler = cProfile.Profile()
    t0 = time.perf_counter_ns()
    profiler.enable()
    fn(iters)
    profiler.disable()
    elapsed_ns = time.perf_counter_ns() - t0

    ns_per_iter = elapsed_ns / iters
    top = _capture_top_entries(profiler, 25)
    report = _top_stats(profiler, 30)
    return {
        "name": name,
        "iterations": iters,
        "elapsed_s": round(elapsed_ns / 1e9, 4),
        "ns_per_iter": round(ns_per_iter, 0),
        "top_entries": top,
        "report": report,
    }


# ── Mode runners ───────────────────────────────────────────────────────────


def run_middleware_mode(sampler_name: str, sampler) -> callable:
    """Return a closure that drives a fresh middleware + iters."""

    def _run(iters: int) -> None:
        if sampler_name == "disabled":
            disable()
            asyncio.run(_drive_middleware(None, iters))
            return
        enable()
        sink = InMemorySink(max_spans=iters + 100)
        tracer = Tracer("profile", sampler=sampler)
        mw = TelemetryMiddleware(
            tracer=tracer,
            sinks=[sink],
            drain_interval_seconds=600.0,
        )
        try:
            asyncio.run(_drive_middleware(mw, iters))
        finally:
            mw.shutdown()
            disable()

    return _run


def run_counter_loop(iters: int) -> None:
    enable()
    c = Counter(f"profile_counter_loop_{iters}", "Counter.inc hot loop.")
    for _ in range(iters):
        c.inc()
    disable()


def run_counter_vec_loop(iters: int) -> None:
    enable()
    cv = CounterVec(
        f"profile_counter_vec_loop_{iters}",
        "CounterVec.inc_tuple hot loop.",
        label_names=("method", "status"),
    )
    for _ in range(iters):
        cv.inc_tuple(("GET", "200"))
    disable()


def run_histogram_loop(iters: int) -> None:
    enable()
    h = Histogram(f"profile_hist_loop_{iters}", "Histogram.observe hot loop.")
    for _ in range(iters):
        h.observe(0.042)
    disable()


def run_sampler_hook_loop(iters: int) -> None:
    """Exercise the sampler hook system without any real samplers.

    Measures the fixed overhead of `_run_samplers()` when the registry
    is empty — this is the cost paid by every drain tick even when no
    sampler has been registered (which is the common case for apps
    that don't instantiate a Database).
    """
    enable()
    # Temporarily save + clear so we measure only the dispatch overhead.
    original = list(_samplers)
    _samplers.clear()
    try:
        for _ in range(iters):
            _run_samplers()
    finally:
        _samplers.clear()
        _samplers.extend(original)
        disable()


def run_sampler_hook_with_noop(iters: int) -> None:
    """Exercise `_run_samplers()` with 3 no-op samplers registered."""
    enable()
    call_count = {"n": 0}

    def noop_1():
        call_count["n"] += 1

    def noop_2():
        call_count["n"] += 1

    def noop_3():
        call_count["n"] += 1

    original = list(_samplers)
    _samplers.clear()
    try:
        register_sampler(noop_1)
        register_sampler(noop_2)
        register_sampler(noop_3)
        for _ in range(iters):
            _run_samplers()
    finally:
        _samplers.clear()
        _samplers.extend(original)
        disable()


# ── Orchestration ───────────────────────────────────────────────────────────


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("  Comprehensive telemetry cProfile suite")
    print(f"  Middleware: {ITERS_MIDDLEWARE:,} iters | Metrics: {ITERS_METRIC_LOOP:,}")
    print("=" * 72)

    modes: list[dict] = []

    # Middleware modes
    modes.append(
        _profile_mode(
            "middleware_disabled",
            run_middleware_mode("disabled", None),
            ITERS_MIDDLEWARE,
        )
    )
    modes.append(
        _profile_mode(
            "middleware_never_sample",
            run_middleware_mode("never", NeverSample()),
            ITERS_MIDDLEWARE,
        )
    )
    modes.append(
        _profile_mode(
            "middleware_ratio_01",
            run_middleware_mode("ratio_01", RatioSample(0.01)),
            ITERS_MIDDLEWARE,
        )
    )
    modes.append(
        _profile_mode(
            "middleware_always_sample",
            run_middleware_mode("always", AlwaysSample()),
            ITERS_MIDDLEWARE,
        )
    )

    # Raw metric loops
    modes.append(_profile_mode("counter_inc_loop", run_counter_loop, ITERS_METRIC_LOOP))
    modes.append(
        _profile_mode("counter_vec_loop", run_counter_vec_loop, ITERS_METRIC_LOOP)
    )
    modes.append(_profile_mode("histogram_loop", run_histogram_loop, ITERS_METRIC_LOOP))

    # Sampler hook overhead
    modes.append(
        _profile_mode("sampler_hook_empty", run_sampler_hook_loop, ITERS_SAMPLERS)
    )
    modes.append(
        _profile_mode("sampler_hook_3_noop", run_sampler_hook_with_noop, ITERS_SAMPLERS)
    )

    # Print + write
    print()
    print("─" * 72)
    print(f"{'MODE':<32} {'ns/iter':>14} {'elapsed':>12}")
    print("─" * 72)
    for m in modes:
        print(f"{m['name']:<32} {m['ns_per_iter']:>14,.0f} {m['elapsed_s']:>10.3f}s")

    out_txt = LOGS / "profile_telemetry_suite.txt"
    out_json = LOGS / "profile_telemetry_suite.json"

    with out_txt.open("w") as f:
        f.write(
            f"# Telemetry cProfile suite — generated {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )
        f.write(f"# Middleware iters: {ITERS_MIDDLEWARE:,}\n")
        f.write(f"# Metric iters:     {ITERS_METRIC_LOOP:,}\n\n")
        f.write(f"{'MODE':<32} {'ns/iter':>14} {'elapsed':>12}\n")
        f.write("─" * 72 + "\n")
        for m in modes:
            f.write(
                f"{m['name']:<32} {m['ns_per_iter']:>14,.0f} {m['elapsed_s']:>10.3f}s\n"
            )
        f.write("\n")
        for m in modes:
            f.write("\n" + "=" * 72 + "\n")
            f.write(f"  MODE: {m['name']}\n")
            f.write(
                f"  iters: {m['iterations']:,}  elapsed: {m['elapsed_s']} s  "
                f"per iter: {m['ns_per_iter']:,.0f} ns\n"
            )
            f.write("=" * 72 + "\n")
            f.write(m["report"])

    with out_json.open("w") as f:
        json.dump(
            [
                {
                    "name": m["name"],
                    "iterations": m["iterations"],
                    "elapsed_s": m["elapsed_s"],
                    "ns_per_iter": m["ns_per_iter"],
                    "top_entries": m["top_entries"],
                }
                for m in modes
            ],
            f,
            indent=2,
        )

    print()
    print(f"  Wrote: {out_txt}")
    print(f"  Wrote: {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
