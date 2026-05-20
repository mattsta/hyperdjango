"""Adapt WebSocket benchmark results (benchmarks/websocket/out/results.json)
into the unified `benchmarks.core` suite schema, so the native-vs-`websockets`
comparison shows up in the shared dashboard next to the HTTP suite."""

from __future__ import annotations

from benchmarks.core import results as R

_NV, _RV = "hyperdjango", "websockets"
_METRICS = [
    R.metric("mps", "Throughput", "msgs/s"),
    R.metric("p50_us", "p50 latency", "µs", lower_is_better=True),
    R.metric("p99_us", "p99 latency", "µs", lower_is_better=True),
    R.metric("mean_us", "mean latency", "µs", lower_is_better=True),
    R.metric("connect_ms", "connect time", "ms", lower_is_better=True),
]
_COLORS = {_NV: "#0072B2", _RV: "#E69F00"}


def _aligned(rows, xkey, valfn):
    xs = sorted({r[xkey] for r in rows})
    return xs, {x: r for r in rows for x in [r[xkey]]}, xs


def build_ws_suite(res: dict) -> dict:
    """Convert a WebSocket ``results.json`` dict into a core ``suite`` block."""
    sweeps: dict = {}

    def add(
        key, label, xtitle, rows, xkey, metric_map, *, xlog=False, note="", desc=""
    ):
        if not rows:
            return
        xs = sorted({r[xkey] for r in rows})
        by = {r[xkey]: r for r in rows}
        data = {f"{_NV}|": {}, f"{_RV}|": {}}
        for mkey, (side_key, extract) in metric_map.items():
            data[f"{_NV}|"][mkey] = [
                extract(by[x]["native"]) if x in by else None for x in xs
            ]
            data[f"{_RV}|"][mkey] = [
                extract(by[x]["reference"]) if x in by else None for x in xs
            ]
        sw = R.sweep(
            key=key,
            label=label,
            xtitle=xtitle,
            xs=xs,
            variants=[_NV, _RV],
            data=data,
            xlog=xlog,
            note=note,
            desc=desc,
        )
        sw["metrics"] = list(metric_map.keys())
        sweeps[key] = sw

    add(
        "throughput",
        "Throughput vs payload",
        "payload size (bytes)",
        res.get("throughput", []),
        "payload_size",
        {"mps": (None, lambda s: s["msgs_per_sec"])},
        xlog=True,
        desc="Echo message round-trips per second vs payload size (fixed concurrency). "
        "How raw message throughput holds up as frames grow.",
    )
    add(
        "concurrency",
        "Throughput vs concurrency",
        "concurrent connections",
        res.get("concurrency_scaling_throughput", []),
        "concurrency",
        {"mps": (None, lambda s: s["msgs_per_sec"])},
        xlog=True,
        desc="Aggregate echo throughput as concurrent WebSocket connections scale "
        "(fixed 4 KiB payload). Native's shared event-loop pool vs one asyncio "
        "task per connection.",
    )
    add(
        "latency",
        "Latency vs payload",
        "payload size (bytes)",
        res.get("latency", []),
        "payload_size",
        {
            "p50_us": (None, lambda s: s["p50_us"]),
            "p99_us": (None, lambda s: s["p99_us"]),
            "mean_us": (None, lambda s: s["mean_us"]),
        },
        xlog=True,
        desc="Per-message round-trip latency vs payload size. Lower is better; "
        "watch the p99 tail as frames grow.",
    )
    add(
        "connection",
        "Connection setup vs count",
        "target connections",
        res.get("connection_scaling", []),
        "target",
        {"connect_ms": (None, lambda s: s["connect_time_s"] * 1000)},
        xlog=True,
        desc="Time to establish + tear down connections as the number of simultaneous "
        "connections grows. Lower is better.",
    )

    return R.suite(
        key="websocket",
        label="WebSocket servers",
        variants=[_NV, _RV],
        metrics=_METRICS,
        sweeps=sweeps,
        colors=_COLORS,
        configs={
            _NV: "native Zig WebSocket · shared event-loop worker pool",
            _RV: "websockets PyPI library · asyncio, one task per connection",
        },
        note="hyperdjango native vs the websockets library · latency lower-is-better",
    )
