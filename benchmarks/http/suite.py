"""Adapt HTTP benchmark results into the unified `benchmarks.core` suite schema,
so the HTTP framework comparison shows up in the shared multi-suite dashboard
alongside WebSocket (and any future subsystem)."""

from __future__ import annotations

from benchmarks.core import results as R
from benchmarks.http.report import (
    _PALETTE,
    CS_P99_BOUND_MS,
    _run_sweeps,
    conn_scaling_capacity,
    conn_scaling_verdict,
)

_SWEEP_DESC = {
    "concurrency": (
        "Maximum-throughput saturation test: a fixed W-worker pool driven "
        "by more and more ALWAYS-BUSY keep-alive connections. This is the "
        "peak-performance measure — the workload where the threaded model's "
        "directness wins and the reactor's per-request dispatch is overhead."
    ),
    "bounded": (
        "Bounded-connections regime (c=W and c=2W): the threaded model's design "
        "workload — every connection has a dedicated worker, no shedding, no "
        "starvation. c=2W is the saturated ceiling (threaded matches the reactor's "
        "peak with far lower p99); c=W exposes the wakeup-latency floor (one "
        "idle-wake per request), so read it as a latency cell, not capacity."
    ),
    "connscaling": (
        "Mostly-idle keep-alive connections with think-time — the connection-"
        "capacity workload. Headline is max_held (largest connection count held "
        "with >=99% served inside the p99 bound), not peak rps: here rps is "
        "~(connections held) x (1 / think-time) and is capped by the shared "
        "think-time generator as well. Blocking models cap at ~their slot count; "
        "multiplexing models hold far more."
    ),
    "workers": (
        "Scale the worker / parallelism budget W under saturating load (128 busy "
        "connections). Throughput climbs to ~the CPU-core count and then plateaus; "
        "past cores, over-subscription adds context-switch latency."
    ),
}

_HTTP_METRICS = [
    R.metric("t", "Throughput", "req/s"),
    R.metric("p50", "p50 latency", "ms", lower_is_better=True),
    R.metric("p90", "p90 latency", "ms", lower_is_better=True),
    R.metric("p99", "p99 latency", "ms", lower_is_better=True),
    R.metric("rss", "Server RSS", "MiB", lower_is_better=True),
    R.metric("gbps", "Body bandwidth", "GB/s"),
    R.metric("served", "Served connections", "%"),
    R.metric("shed", "Shed (503) connections", "%", lower_is_better=True),
]


def _conn_scaling_sweep(cs: dict) -> dict:
    """Build the connection-scaling core sweep from the conn-scaling results."""
    meta, rows = cs["meta"], cs["results"]
    conns, fws = meta["conns"], meta["frameworks"]
    idx = {(r["framework"], r["conns"]): r for r in rows}
    data = {}
    for f in fws:
        arr = {"t": [], "p99": [], "served": [], "shed": []}
        for n in conns:
            r = idx.get((f, n))
            arr["t"].append(r.get("throughput_rps") if r else None)
            arr["p99"].append(r.get("p99_ms") if r else None)
            arr["served"].append(r.get("served_frac") if r else None)
            arr["shed"].append(r.get("shed_frac") if r else None)
        data[f"{f}|"] = arr
    w = meta["workers"]
    # The capacity headline is the comparison; peak rps in this regime is
    # ~(connections held) x (1 / think-time) AND is capped by the shared
    # think-time generator, so it travels as supporting evidence only.
    bound = float(meta.get("cs_p99_bound_ms") or CS_P99_BOUND_MS)
    caps = conn_scaling_capacity(rows, meta, bound)
    headline = " · ".join(f"{c.framework} {c.held_label}" for c in caps)
    sweep_notes = [
        line
        for line in conn_scaling_verdict(caps, bound)
        if line.startswith("SWEEP NOTE:")
    ]
    return R.sweep(
        key="conn_scaling",
        label="Connection scaling (idle keep-alive)",
        xtitle=f"concurrent keep-alive connections (~{meta['think_ms']:.0f}ms think-time)",
        xs=conns,
        variants=fws,
        data=data,
        xlog=True,
        refs=[{"v": w, "label": f"W={w} (blocking cap)", "kind": "cfg"}],
        note=(
            f"W={w} · ~{meta['think_ms']:.0f}ms think-time · asyncio client"
            + (f" · max_held: {headline}" if headline else "")
        ),
        desc=(
            "Real web traffic: many MOSTLY-IDLE keep-alive connections (think-time "
            "between requests). The headline is CAPACITY — max_held, the largest "
            "connection count held with >=99% of connections served and p99 "
            f"<={bound:.0f}ms. Blocking models (threaded, Flask slots) cap at ~W "
            "connections no matter how many arrive; multiplexing models (reactor, "
            "FastAPI async) hold far more. Read the rps curve as a LOWER BOUND: in "
            "this regime rps restates the connection cap, and the shared think-time "
            "generator caps it too."
            + ("  " + " ".join(sweep_notes) if sweep_notes else "")
        ),
        metrics=["t", "p99", "served", "shed"],
    )


def build_http_suite(run_entry: dict) -> dict:
    """Convert one archived HTTP run (``{"sweeps": {concurrency|workers: {meta,
    results}}}``) into a core ``suite`` block."""
    old = _run_sweeps(run_entry)  # {sweep_key: legacy sweep block}
    variants: list[str] = []
    sweeps: dict = {}
    for sk, ob in old.items():
        variants = ob["frameworks"]
        pb = ob.get("payload_bytes", {})
        groups = [
            R.group(p, p if pb.get(p) else f"{p} (baseline)", pb.get(p))
            for p in ob["payloads"]
        ]
        sweeps[sk] = R.sweep(
            key=sk,
            label=ob["label"],
            xtitle=ob["xtitle"],
            xs=ob["xs"],
            variants=ob["frameworks"],
            data=ob["data"],
            groups=groups,
            groups_label="payload",
            xlog=ob["xlog"],
            refs=ob["refs"],
            note=ob["note"],
            desc=_SWEEP_DESC.get(sk, ""),
            # These sweeps measure throughput/latency/RSS for every cell — but
            # NOT served-connection fraction (that's only meaningful in the idle
            # connection-scaling test). Declaring the sweep's own metrics keeps
            # the metric selector from offering "Served connections" here (empty
            # charts). Body bandwidth rides along on every payload sweep — the
            # same recorded field the HTTP report charts, passed through the
            # generic schema untouched (a metric key whose arrays already sit in
            # `data`).
            metrics=["t", "p50", "p90", "p99", "rss", "gbps"],
        )

    if run_entry.get("conn_scaling"):
        sweeps["conn_scaling"] = _conn_scaling_sweep(run_entry["conn_scaling"])
        if not variants:
            variants = run_entry["conn_scaling"]["meta"]["frameworks"]

    configs, interp = {}, ""
    metas = [sd.get("meta", {}) for sd in run_entry.get("sweeps", {}).values()]
    if run_entry.get("conn_scaling"):
        metas.append(run_entry["conn_scaling"].get("meta", {}))
    for mt in metas:
        for k, v in (mt.get("configs") or {}).items():
            configs.setdefault(k, v)
        interp = interp or mt.get("interpreter", "")

    return R.suite(
        key="http",
        label="HTTP frameworks",
        variants=variants,
        metrics=_HTTP_METRICS,
        sweeps=sweeps,
        colors=_PALETTE,
        configs=configs,
        interpreter=interp,
        note="hyperdjango (threaded/reactor) vs FastAPI vs Flask · latency & memory lower-is-better",
    )
