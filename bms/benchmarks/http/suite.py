"""Adapt HTTP benchmark results into the unified `benchmarks.core` suite schema,
so the HTTP framework comparison shows up in the shared multi-suite dashboard
alongside WebSocket (and any future subsystem)."""

from __future__ import annotations

from benchmarks.core import results as R
from benchmarks.http.report import _PALETTE, _run_sweeps

_SWEEP_DESC = {
    "concurrency": (
        "Maximum-throughput saturation test: a fixed W-worker pool driven "
        "by more and more ALWAYS-BUSY keep-alive connections. This is the "
        "peak-performance measure — the workload where the threaded model's "
        "directness wins and the reactor's per-request dispatch is overhead."
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
    sw = R.sweep(
        key="conn_scaling",
        label="Connection scaling (idle keep-alive)",
        xtitle=f"concurrent keep-alive connections (~{meta['think_ms']:.0f}ms think-time)",
        xs=conns,
        variants=fws,
        data=data,
        xlog=True,
        refs=[{"v": w, "label": f"W={w} (blocking cap)", "kind": "cfg"}],
        note=f"W={w} · ~{meta['think_ms']:.0f}ms think-time · asyncio client",
        desc=(
            "Real web traffic: many MOSTLY-IDLE keep-alive connections (think-time "
            "between requests). Exposes the connection-model ceiling — blocking models "
            "(threaded, Flask slots) plateau at ~W connections no matter how many "
            "arrive, while multiplexing models (reactor, FastAPI async) scale with "
            "connections until cores / OS limits bend them. This is the workload where "
            "the reactor wins — see 'Served connections' for who actually keeps up."
        ),
    )
    sw["metrics"] = ["t", "p99", "served", "shed"]
    return sw


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
        sw = R.sweep(
            key=sk,
            label=ob["label"],
            xtitle=ob["xtitle"],
            xs=ob["xs"],
            variants=ob["frameworks"],
            data=ob["data"],
            groups=groups,
            xlog=ob["xlog"],
            refs=ob["refs"],
            note=ob["note"],
            desc=_SWEEP_DESC.get(sk, ""),
        )
        sw["groupsLabel"] = "payload"
        # These sweeps measure throughput/latency/RSS for every cell — but NOT
        # served-connection fraction (that's only meaningful in the idle
        # connection-scaling test). Declaring the sweep's own metrics keeps the
        # metric selector from offering "Served connections" here (empty charts).
        sw["metrics"] = ["t", "p50", "p90", "p99", "rss"]
        sweeps[sk] = sw

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
