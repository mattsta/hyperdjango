"""Adapt WebSocket benchmark results (benchmarks/websocket/out/results.json)
into the unified `benchmarks.core` suite schema, so the native-vs-`websockets`
comparison shows up in the shared dashboard next to the HTTP suite — and feed
that suite into the shared, non-destructive run history.

This module is the WebSocket counterpart of `benchmarks.http.suite`: it is a
pure adapter over an already-measured result set (plus the archive call), so it
imports nothing from the measurement path. That is what lets an EXISTING
``results.json`` be re-fed into the unified record without re-running the
benchmark (see `benchmarks.websocket.refeed`).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from benchmarks.core import results as R
from benchmarks.core.dashboard import write_dashboard
from benchmarks.core.results import DIAGNOSTICS_DIR, HISTORY_DIR
from benchmarks.core.results import save_run as core_save_run

# The unified multi-suite dashboard/history dir — the page with the Suite
# selector where WebSocket sits next to HTTP. The WebSocket-only report under
# benchmarks/websocket/out/ remains for deep WebSocket slicing.
UNIFIED_OUTDIR = "benchmarks/out"
SUITE_KEY = "websocket"

# Variants. The native server's DEFAULT connection model is the shared
# event-loop pool, so `hyperdjango` IS the shared model everywhere except the
# connection-model sweep, where the thread-per-connection opt-out is measured
# as its own variant against it.
_NV = "hyperdjango"
_RV = "websockets"
_TV = "hyperdjango (thread-per-conn)"

_COLORS = {_NV: "#0072B2", _RV: "#E69F00", _TV: "#009E73"}

# Units are declared the same way the HTTP suite declares its metrics: one
# entry per recorded series, with `lower_is_better` set for the cost metrics so
# the dashboard's leaderboard ranks them the right way round. `gbps` mirrors the
# HTTP suite's body-bandwidth metric exactly (decimal GB/s, throughput x payload
# bytes) — here x2, because a WebSocket echo puts every payload on the wire
# twice (client send + server echo).
_METRICS = [
    R.metric("mps", "Throughput", "msgs/s"),
    R.metric("gbps", "Wire bandwidth", "GB/s"),
    R.metric("p50_us", "p50 latency", "µs", lower_is_better=True),
    R.metric("p99_us", "p99 latency", "µs", lower_is_better=True),
    R.metric("mean_us", "mean latency", "µs", lower_is_better=True),
    R.metric("connect_ms", "Connect time", "ms", lower_is_better=True),
    R.metric("connected", "Connections established", "conns"),
    R.metric("rss", "Peak RSS", "MiB", lower_is_better=True),
    R.metric("threads", "Peak threads", "threads", lower_is_better=True),
]

_CONFIGS = {
    _NV: "native Zig WebSocket · DEFAULT shared event-loop pool (one loop per core)",
    _RV: "websockets PyPI library · single asyncio loop, one task per connection",
    _TV: "native Zig WebSocket · WEBSOCKET_CONCURRENCY=thread opt-out (pool_size=24)",
}

# One extractor per recorded metric: (result-side dict) -> value.
_Extract = Callable[[dict], float | None]


def _gbps(side: dict) -> float:
    """Wire bandwidth in decimal GB/s — the same shape as the HTTP suite's
    ``body_gbps`` (throughput x payload bytes / 1e9), x2 because each logical
    echo message crosses the wire twice (send + echo back)."""
    return side["msgs_per_sec"] * side["payload_size"] * 2 / 1e9


_THROUGHPUT_SERIES: dict[str, _Extract] = {
    "mps": lambda s: s["msgs_per_sec"],
    "gbps": _gbps,
}
_LATENCY_SERIES: dict[str, _Extract] = {
    "p50_us": lambda s: s["p50_us"],
    "p99_us": lambda s: s["p99_us"],
    "mean_us": lambda s: s["mean_us"],
}
_CONNECTION_SERIES: dict[str, _Extract] = {
    "connect_ms": lambda s: s["connect_time_s"] * 1000,
    "connected": lambda s: s["connected"],
}
_CONNMODEL_SERIES: dict[str, _Extract] = {
    "mps": lambda s: s["msgs_per_sec"],
    "connected": lambda s: s["connected"],
    "rss": lambda s: s["peak_rss_mb"],
    "threads": lambda s: s["peak_threads"],
}


def _xs(rows: list[dict], xkey: str) -> list:
    return sorted({r[xkey] for r in rows})


def _series(
    rows: list[dict], xs: list, xkey: str, side: str, series: dict[str, _Extract]
) -> dict[str, list[float | None]]:
    """One variant's metric arrays, aligned to `xs` (missing cells -> None so the
    dashboard leaves a gap instead of inventing a point)."""
    by = {r[xkey]: r for r in rows}
    return {
        mkey: [extract(by[x][side]) if x in by else None for x in xs]
        for mkey, extract in series.items()
    }


def _throughput_sweep(rows: list[dict]) -> dict | None:
    """Native vs reference msgs/sec (and wire GB/s) per payload size, faceted by
    frame type. Frame type MUST be a group: text and binary rows share a payload
    size, so collapsing on payload alone silently drops one of them."""
    if not rows:
        return None
    xs = _xs(rows, "payload_size")
    frames = list(dict.fromkeys(r["frame_type"] for r in rows))
    data: dict[str, dict[str, list[float | None]]] = {}
    for ft in frames:
        sub = [r for r in rows if r["frame_type"] == ft]
        data[f"{_NV}|{ft}"] = _series(
            sub, xs, "payload_size", "native", _THROUGHPUT_SERIES
        )
        data[f"{_RV}|{ft}"] = _series(
            sub, xs, "payload_size", "reference", _THROUGHPUT_SERIES
        )
    return R.sweep(
        key="throughput",
        label="Throughput vs payload",
        xtitle="payload size (bytes)",
        xs=xs,
        variants=[_NV, _RV],
        data=data,
        groups=[R.group(ft, f"{ft} frames") for ft in frames],
        groups_label="frame type",
        xlog=True,
        note="multi-process load generator · fixed connection count",
        desc=(
            "Echo message round-trips per second vs payload size, driven by a "
            "MULTI-PROCESS load generator so the client is never the limiter. "
            "Wire bandwidth (GB/s) counts every payload twice — send + echo — so "
            "it reads as the bytes actually moved across the socket."
        ),
        metrics=["mps", "gbps"],
    )


def _concurrency_sweep(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    xs = _xs(rows, "concurrency")
    data = {
        f"{_NV}|": _series(rows, xs, "concurrency", "native", _THROUGHPUT_SERIES),
        f"{_RV}|": _series(rows, xs, "concurrency", "reference", _THROUGHPUT_SERIES),
    }
    return R.sweep(
        key="concurrency",
        label="Throughput vs concurrency",
        xtitle="concurrent connections",
        xs=xs,
        variants=[_NV, _RV],
        data=data,
        xlog=True,
        note="4 KiB text payload",
        desc=(
            "Aggregate echo throughput as concurrent WebSocket connections scale "
            "(fixed 4 KiB payload). The native shared event-loop pool spreads "
            "connections across cores; the reference runs one asyncio task per "
            "connection on a single loop."
        ),
        metrics=["mps", "gbps"],
    )


def _latency_sweep(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    xs = _xs(rows, "payload_size")
    data = {
        f"{_NV}|": _series(rows, xs, "payload_size", "native", _LATENCY_SERIES),
        f"{_RV}|": _series(rows, xs, "payload_size", "reference", _LATENCY_SERIES),
    }
    return R.sweep(
        key="latency",
        label="Latency vs payload",
        xtitle="payload size (bytes)",
        xs=xs,
        variants=[_NV, _RV],
        data=data,
        xlog=True,
        note="single connection · unpipelined · warmed up",
        desc=(
            "Per-message round-trip latency vs payload size, measured on a single "
            "unpipelined connection. Lower is better; watch the p99 tail as frames "
            "grow."
        ),
        metrics=["p50_us", "p99_us", "mean_us"],
    )


def _connection_scaling_sweep(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    xs = _xs(rows, "target")
    data = {
        f"{_NV}|": _series(rows, xs, "target", "native", _CONNECTION_SERIES),
        f"{_RV}|": _series(rows, xs, "target", "reference", _CONNECTION_SERIES),
    }
    return R.sweep(
        key="connection_scaling",
        label="Connection setup vs count",
        xtitle="target connections",
        xs=xs,
        variants=[_NV, _RV],
        data=data,
        xlog=True,
        desc=(
            "Time to establish simultaneous connections as the connection count "
            "grows, alongside how many of them actually came up. Read connect time "
            "next to Connections established: a fast connect that dropped "
            "connections is not a win."
        ),
        metrics=["connect_ms", "connected"],
    )


def _connection_model_sweep(model: dict) -> dict | None:
    """The connection-model comparison: the DEFAULT shared event-loop pool vs the
    thread-per-connection opt-out, at one high connection count. Expressed as a
    sweep whose variants are the two models (the sweep declares its own variant
    list, so the reference server is not drawn as an empty series here)."""
    shared, thread = model.get("shared"), model.get("thread")
    if not shared and not thread:
        return None
    target = int((shared or thread)["target_conns"])
    xs = [target]
    data: dict[str, dict[str, list[float | None]]] = {}
    for variant, side in ((_NV, shared), (_TV, thread)):
        data[f"{variant}|"] = {
            mkey: [extract(side) if side else None]
            for mkey, extract in _CONNMODEL_SERIES.items()
        }
    return R.sweep(
        key="connection_model",
        label="Connection model (shared vs thread)",
        xtitle="offered connections",
        xs=xs,
        variants=[_NV, _TV],
        data=data,
        note=f"{target} offered connections · 4 KiB text payload",
        desc=(
            "Native connection model head-to-head at one high connection count: "
            "the DEFAULT shared event-loop pool vs the WEBSOCKET_CONCURRENCY=thread "
            "opt-out. The shared pool lifts the connection ceiling and keeps "
            "multi-core throughput while holding memory and thread count down — "
            "compare Throughput, Connections established, Peak RSS and Peak threads."
        ),
        metrics=["mps", "connected", "rss", "threads"],
    )


def build_websocket_suite(res: dict) -> dict:
    """Convert a WebSocket ``results.json`` dict into a core ``suite`` block."""
    sweeps: dict = {}
    for sw in (
        _throughput_sweep(res.get("throughput", [])),
        _concurrency_sweep(res.get("concurrency_scaling_throughput", [])),
        _latency_sweep(res.get("latency", [])),
        _connection_scaling_sweep(res.get("connection_scaling", [])),
        _connection_model_sweep(res.get("connection_model", {})),
    ):
        if sw is not None:
            sweeps[sw["key"]] = sw

    variants = [_NV, _RV]
    if "connection_model" in sweeps:
        variants.append(_TV)

    return R.suite(
        key=SUITE_KEY,
        label="WebSocket servers",
        variants=variants,
        metrics=_METRICS,
        sweeps=sweeps,
        colors=_COLORS,
        configs={v: _CONFIGS[v] for v in variants},
        note=(
            "hyperdjango native vs the websockets library · "
            "latency & memory lower-is-better"
        ),
    )


# ── Complete run vs diagnostic ───────────────────────────────────────────────
#
# The same doctrine the HTTP suite applies to its own archive: a RESULT compares
# everything against everything else. A `--quick` smoke matrix, or a full run
# whose measurement half-failed (a section empty, one arm of the connection-model
# comparison skipped), is a DIAGNOSTIC — worth keeping for the investigation
# record, never a record anyone can compare against. Flags gate the intent;
# RESULTS gate the classification, so both are checked.
_HEADLINE_SECTIONS = (
    "throughput",
    "concurrency_scaling_throughput",
    "latency",
    "connection_scaling",
)
_CONNECTION_MODEL_ARMS = ("shared", "thread")


@dataclass(frozen=True, slots=True)
class WsCompleteness:
    """Whether a measured WebSocket run may enter the comparison history, and
    every reason it may not."""

    complete: bool
    missing: tuple[str, ...]


def websocket_completeness(
    results: dict, *, full_matrix: bool = True
) -> WsCompleteness:
    """Classify one measured WebSocket result set. `full_matrix` is the run's
    INTENT (``--full``); the sections below are what it actually MEASURED."""
    missing: list[str] = []
    if not full_matrix:
        missing.append(
            "quick smoke matrix: restricted payload/concurrency ladder (use --full)"
        )
    for key in _HEADLINE_SECTIONS:
        if not results.get(key):
            missing.append(f"{key}: section absent or empty")
    model = results.get("connection_model") or {}
    for arm in _CONNECTION_MODEL_ARMS:
        if not model.get(arm):
            missing.append(f"connection_model: {arm} arm not measured")
    return WsCompleteness(not missing, tuple(missing))


@dataclass(frozen=True, slots=True)
class UnifiedFeed:
    """What one feed of the unified record produced: the archived run id, which
    archive it landed in, and the regenerated dashboard path (empty when the
    render was skipped)."""

    run_id: str
    dashboard: str
    diagnostic: bool = False

    @property
    def archive_dir(self) -> str:
        return DIAGNOSTICS_DIR if self.diagnostic else HISTORY_DIR


def feed_unified(
    results: dict,
    *,
    label: str = "",
    outdir: str = UNIFIED_OUTDIR,
    cores: int | None = None,
    render: bool = True,
    diagnostic: bool = False,
    expected_suites: list[str] | None = None,
) -> UnifiedFeed:
    """Archive this WebSocket run into the shared cross-suite history and
    regenerate the unified dashboard — the same feed `benchmarks.http.run`
    performs for the HTTP suite, so one page covers both subsystems.

    `diagnostic` quarantines a restricted / partially-measured run under
    ``<outdir>/diagnostics/``: archived for the record, invisible to the
    dashboard, the index and the merge-target lookup. `expected_suites` declares
    the coverage the record is supposed to reach (a `bench-all` invocation
    declares both suites, so a record still missing HTTP reads as incomplete)."""
    run_id = core_save_run(
        outdir,
        {SUITE_KEY: build_websocket_suite(results)},
        label=label,
        cores=cores if cores is not None else os.cpu_count(),
        diagnostic=diagnostic,
        expected_suites=expected_suites,
    )
    dash = ""
    # A diagnostic is invisible to the dashboard by construction; re-rendering
    # would only reproduce the page it is deliberately absent from.
    if render and not diagnostic:
        try:
            dash = str(write_dashboard(outdir))
        except ModuleNotFoundError as exc:
            # The run is SAFE (archived above) — only the render needs plotly.
            print(f"Unified dashboard render skipped ({exc}) — the run is archived.")
    return UnifiedFeed(run_id=run_id, dashboard=dash, diagnostic=diagnostic)
