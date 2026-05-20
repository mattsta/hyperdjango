"""Render HTTP benchmark results to JSON (machine), Markdown (human), and a
self-contained interactive HTML dashboard (Plotly, embedded inline).

Runs are archived non-destructively under ``<outdir>/history/`` so a new run
never wipes prior results; the dashboard reads the whole history and can compare
any two runs (improvements over time)."""

from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import socket
import subprocess

# Okabe–Ito colourblind-safe categorical palette (fixed hue per framework).
_PALETTE = {
    "hyperdjango-threaded": "#0072B2",  # blue
    "hyperdjango-reactor": "#009E73",  # green
    "fastapi": "#E69F00",  # orange
    "flask": "#D55E00",  # vermillion
}
_FALLBACK = ["#0072B2", "#009E73", "#E69F00", "#D55E00", "#CC79A7", "#56B4E9"]


def _color(name: str, i: int) -> str:
    return _PALETTE.get(name, _FALLBACK[i % len(_FALLBACK)])


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:.1f}"


def _table(results, payload, frameworks, concurrencies, key, unit):
    lines = [
        f"| conns | {' | '.join(frameworks)} |",
        "|" + "---|" * (len(frameworks) + 1),
    ]
    for c in concurrencies:
        cells = []
        for fw in frameworks:
            r = next(
                (
                    x
                    for x in results
                    if x["framework"] == fw
                    and x["payload"] == payload
                    and x["concurrency"] == c
                ),
                None,
            )
            cells.append(_fmt(r[key]) if r else "—")
        lines.append(f"| {c} | {' | '.join(cells)} |")
    return "\n".join(lines) + f"\n\n_{unit}_\n"


def render_markdown(results: list[dict], meta: dict) -> str:
    frameworks = meta["frameworks"]
    payloads = meta["payloads"]  # list of (name, bytes)
    concurrencies = meta["concurrencies"]

    out = ["# HTTP framework benchmark", ""]
    out.append(
        f"- Workers/threads per server (W): **{meta['workers']}** "
        f"(so the threaded/sync ceiling is ~W connections)\n"
        f"- Measurement: {meta['duration_s']}s after {meta['warmup_s']}s warmup, "
        f"keep-alive, closed-loop; single machine over loopback.\n"
        f"- Frameworks: hyperdjango native (threaded / reactor), "
        f"FastAPI (async/uvicorn), Flask (sync/gunicorn-gthread).\n"
    )
    out.append(
        "## How to read this\n\n"
        "Watch each column as **conns** grows past W. Threaded/sync models "
        "plateau or cliff at ~W (a connection pins a worker); reactor/async "
        "keep scaling (a connection holds a worker only during a request). "
        "Below W, threaded is typically fastest (no multiplexing overhead) — "
        "the crossover.\n"
    )
    for name, nbytes in payloads:
        out.append(f"## Payload: {name} (~{nbytes} B)\n")
        out.append("### Throughput (requests/sec — higher is better)\n")
        out.append(
            _table(
                results,
                name,
                frameworks,
                concurrencies,
                "throughput_rps",
                "requests/sec",
            )
        )
        out.append("### Latency p99 (ms — lower is better)\n")
        out.append(
            _table(results, name, frameworks, concurrencies, "p99_ms", "milliseconds")
        )
    # Memory: report peak RSS per framework at the highest concurrency.
    out.append("## Peak server memory (RSS, MiB) at max concurrency\n")
    top = max(concurrencies)
    out.append("| framework | RSS MiB |\n|---|---|")
    for fw in frameworks:
        rows = [x for x in results if x["framework"] == fw and x["concurrency"] == top]
        rss = max((x.get("rss_mb", 0.0) for x in rows), default=0.0)
        out.append(f"| {fw} | {rss:.1f} |")
    out.append("")
    return "\n".join(out)


def _worker_table(results, payload, frameworks, worker_counts, key):
    lines = [
        f"| W (workers) | {' | '.join(frameworks)} |",
        "|" + "---|" * (len(frameworks) + 1),
    ]
    for w in worker_counts:
        cells = []
        for fw in frameworks:
            r = next(
                (
                    x
                    for x in results
                    if x["framework"] == fw
                    and x["payload"] == payload
                    and x["workers"] == w
                ),
                None,
            )
            cells.append(_fmt(r[key]) if r else "—")
        lines.append(f"| {w} | {' | '.join(cells)} |")
    return "\n".join(lines) + "\n"


def render_worker_markdown(results: list[dict], meta: dict) -> str:
    frameworks = meta["frameworks"]
    payloads = meta["payloads"]
    worker_counts = meta["worker_counts"]
    cores = meta.get("cores", "?")
    conc = meta["sweep_concurrency"]

    out = ["# HTTP worker-count scaling", ""]
    out.append(
        f"- Machine cores: **{cores}**. Fixed client concurrency: **{conc}** "
        f"(saturating). Sweep the per-server worker/parallelism budget W.\n"
        f"- W means W native threads (hyperdjango), W gthread threads (Flask), "
        f"or W uvicorn worker processes (FastAPI) — the same parallelism budget.\n"
        f"- Watch throughput rise with W up to ~{cores} cores, then plateau or "
        f"degrade as W over-subscribes the cores (context-switch / contention).\n"
    )
    for name, nbytes in payloads:
        out.append(f"## Payload: {name} (~{nbytes} B) — throughput (req/s) vs W\n")
        out.append(
            _worker_table(results, name, frameworks, worker_counts, "throughput_rps")
        )
        out.append(f"### p99 latency (ms) vs W — {name}\n")
        out.append(_worker_table(results, name, frameworks, worker_counts, "p99_ms"))
    return "\n".join(out)


def write_reports(results: list[dict], meta: dict, outdir: str) -> pathlib.Path:
    d = pathlib.Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(
        json.dumps({"meta": meta, "results": results}, indent=2)
    )
    (d / "report.md").write_text(render_markdown(results, meta))
    return d


def write_worker_reports(results: list[dict], meta: dict, outdir: str) -> pathlib.Path:
    d = pathlib.Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "results_workers.json").write_text(
        json.dumps({"meta": meta, "results": results}, indent=2)
    )
    (d / "report_workers.md").write_text(render_worker_markdown(results, meta))
    return d


# A rps drop of at least this fraction between adjacent W steps in one series is
# flagged as a negative-scaling collapse — the signature of shared-state
# contention that worsens with worker count.
NEGATIVE_SCALING_DROP_FRAC = 0.08


def _fmt_rps(v: float | None) -> str:
    if not v:
        return "0"
    return f"{v / 1000:.0f}k" if v >= 1000 else f"{v:.0f}"


def _contention_causes(lo: dict, hi: dict) -> list[str]:
    """Describe how contention counters moved from the higher-rps cell to the
    collapsed one — the suspected cause attached to a flagged drop. Reads the
    perf counters (nested under profile["counters"]) and the in-flight gauge
    (contention["active_peak"]); any absent signal is simply skipped."""
    causes: list[str] = []
    lp = (lo.get("profile") or {}).get("counters") or {}
    hp = (hi.get("profile") or {}).get("counters") or {}
    lc, hc = lo.get("contention") or {}, hi.get("contention") or {}
    signals = [
        ("context-switches", lp.get("context-switches"), hp.get("context-switches")),
        ("cache-misses", lp.get("cache-misses"), hp.get("cache-misses")),
        ("cpu-migrations", lp.get("cpu-migrations"), hp.get("cpu-migrations")),
        ("active-gauge", lc.get("active_peak"), hc.get("active_peak")),
    ]
    for label, a, b in signals:
        if a is None or b is None or a <= 0 or b == a:
            continue
        causes.append(f"{label} {b / a:+.1f}x")
    p0, p1 = lo.get("p99_ms"), hi.get("p99_ms")
    if p0 and p1 and p1 > p0:
        causes.append(f"p99 {p1 / p0:+.1f}x")
    causes.extend(_degradation_causes(hi))
    return causes


def _degradation_causes(cell: dict) -> list[str]:
    """Client-observed degradation in one cell: shed/error responses, socket
    errors, starvation timeouts, and oversubscription (W beyond the server's
    pinned core budget). These name WHY a cell's service throughput fell —
    e.g. a 503-shedding threaded server or 128 conns served while 896 starve —
    where the raw counters alone read as a mystery collapse."""
    causes: list[str] = []
    req = cell.get("requests") or 0
    non2xx = cell.get("non2xx") or 0
    if req and non2xx / req > 0.005:
        causes.append(f"non-2xx {non2xx / req * 100:.0f}% of responses (shedding)")
    tmo = cell.get("err_timeout") or 0
    if tmo:
        causes.append(f"{tmo} timeouts (starved connections)")
    sf = cell.get("served_frac")
    if sf is not None and sf < 0.9:
        causes.append(
            f"only {sf * 100:.0f}% of connections in service "
            f"(~{cell.get('served_conns', 0):.0f}/{cell.get('concurrency')} — "
            "rest starved or in reconnect churn)"
        )
    sock_err = (
        (cell.get("err_read") or 0)
        + (cell.get("err_write") or 0)
        + (cell.get("err_connect") or 0)
    )
    if req and sock_err > req * 0.005:
        causes.append(f"{sock_err} socket errors (close/reconnect churn)")
    budget = cell.get("server_core_budget") or 0
    if budget and (cell.get("workers") or 0) > budget:
        causes.append(f"oversubscribed (W>{budget} pinned cores)")
    return causes


def worker_sweep_verdict(results: list[dict]) -> list[str]:
    """Auto-flag every adjacent-W step where rps DROPS within a series, naming
    the drop and its suspected contention cause. A series is one
    (framework, payload, reactor_count) curve ordered by worker count. Returns
    the flagged lines, most-severe first (empty if scaling is monotonic)."""
    by_series: dict[tuple, list[dict]] = {}
    for r in results:
        key = (r["framework"], r.get("payload"), r.get("reactor_count"))
        by_series.setdefault(key, []).append(r)

    flags: list[tuple[float, str]] = []
    for (fw, payload, rc), series in by_series.items():
        series.sort(key=lambda r: r["workers"])
        label = f"{fw} {payload}" + (f" rc={rc}" if rc is not None else "")
        for lo, hi in zip(series, series[1:]):
            r0, r1 = lo.get("throughput_rps") or 0, hi.get("throughput_rps") or 0
            if r0 <= 0 or r1 >= r0 * (1 - NEGATIVE_SCALING_DROP_FRAC):
                continue
            drop = (r0 - r1) / r0
            causes = _contention_causes(lo, hi)
            cause = f"  [{'; '.join(causes)}]" if causes else ""
            flags.append(
                (
                    drop,
                    f"W={lo['workers']}->{hi['workers']} {label}: "
                    f"{_fmt_rps(r0)}->{_fmt_rps(r1)} ({-drop * 100:+.0f}%){cause}",
                )
            )
    flags.sort(key=lambda f: f[0], reverse=True)
    return [line for _, line in flags]


# ── HTML report with inline SVG line charts (self-contained, no CDN) ─────────

_CSS = """
:root{--bg:#fff;--surface:#f6f8fa;--ink:#1a1f26;--muted:#5b6570;--grid:#e2e6ea;--border:#d7dde3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--surface:#171b21;--ink:#e6eaef;--muted:#9aa4af;--grid:#2a3038;--border:#2a3038}}
:root[data-theme=dark]{--bg:#0f1216;--surface:#171b21;--ink:#e6eaef;--muted:#9aa4af;--grid:#2a3038;--border:#2a3038}
:root[data-theme=light]{--bg:#fff;--surface:#f6f8fa;--ink:#1a1f26;--muted:#5b6570;--grid:#e2e6ea;--border:#d7dde3}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px;max-width:1180px;margin:0 auto}
h1{font-size:26px}h2{font-size:20px;margin-top:34px;border-top:1px solid var(--border);padding-top:18px}
.meta{color:var(--muted);font-size:13.5px}
.chart{position:relative;background:var(--surface);border-radius:12px;padding:14px 18px;margin:18px 0;overflow-x:auto}
.chart-title{font-weight:600;margin-bottom:6px}
svg{max-width:100%;height:auto}
.grid{stroke:var(--grid);stroke-width:1}
.vline{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:5 4;opacity:.8}
.vline.cfg{stroke:#8a6d3b;stroke-dasharray:2 3;opacity:.85}
:root[data-theme=dark] .vline.cfg,@media (prefers-color-scheme:dark){.vline.cfg{stroke:#c9a76a}}
.vlabel{fill:var(--muted);font-size:10.5px;font-weight:600}
.vlabel.cfg{fill:#8a6d3b}
:root[data-theme=dark] .vlabel.cfg,@media (prefers-color-scheme:dark){.vlabel.cfg{fill:#c9a76a}}
.crosshair{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:.55}
.hl-dot{stroke:var(--surface);stroke-width:1.5}
.tooltip{position:fixed;pointer-events:none;display:none;z-index:20;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:7px 9px;font-size:12px;box-shadow:0 6px 20px rgba(0,0,0,.22);min-width:130px}
.tooltip .tt-x{color:var(--muted);font-size:11px;margin-bottom:4px}
.tooltip .tt-row{display:flex;align-items:center;gap:7px;white-space:nowrap;line-height:1.7}
.tooltip .tt-sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
.tooltip .tt-val{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
.ytick,.xtick{fill:var(--muted);font-size:11px}
.axlabel{fill:var(--muted);font-size:12px}
.legend{fill:var(--ink);font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 2px}
th,td{border:1px solid var(--border);padding:4px 8px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:var(--surface);color:var(--muted);font-weight:600}
details{margin:2px 0 10px}summary{cursor:pointer;color:var(--muted);font-size:12.5px}
h1{font-size:24px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin:2px 0 16px}
.controls{display:flex;flex-wrap:wrap;gap:12px 22px;align-items:center;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:12px;position:sticky;top:0;z-index:5}
.ctl{display:flex;align-items:center;gap:8px}
.ctl>.lbl{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.seg{display:inline-flex;background:var(--bg);border:1px solid var(--border);border-radius:9px;overflow:hidden}
.seg button{border:0;border-left:1px solid var(--border);background:transparent;color:var(--muted);padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
.seg button:first-child{border-left:0}
.seg button:hover{color:var(--ink)}
.seg button.on{background:#0072B2;color:#fff;font-weight:600}
@media (prefers-color-scheme:dark){.seg button.on{background:#3a9bdc}}
select{background:var(--bg);color:var(--ink);border:1px solid var(--border);border-radius:9px;padding:6px 10px;font:inherit;font-size:13px}
.chk{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;cursor:pointer;user-select:none}
.refnote{color:var(--muted);font-size:12.5px;margin:12px 2px 0;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center}
.refnote .tag{display:inline-flex;align-items:center;gap:7px}
.refnote .dash{width:22px;border-top:2px dashed var(--muted)}
.refnote .dash.cfg{border-top-style:dotted;border-top-color:#b9770e}
@media (prefers-color-scheme:dark){.refnote .dash.cfg{border-top-color:#d9a441}}
#chart{width:100%;height:640px;margin-top:8px}
.foot{color:var(--muted);font-size:12px;margin-top:14px}
.hidden{display:none!important}
.setup{margin:8px 2px 0}
.setup summary{cursor:pointer;color:var(--muted);font-size:12.5px;font-weight:600;padding:4px 0}
.setup .cfg-list{display:flex;flex-direction:column;gap:5px;margin:8px 0 6px;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:10px}
.setup .cfg-row{display:flex;align-items:baseline;gap:9px;font-size:12.5px;flex-wrap:wrap}
.setup .cfg-row .sw{width:11px;height:11px;border-radius:3px;flex:0 0 auto;align-self:center}
.setup .cfg-row b{min-width:170px}
.setup .cfg-desc{color:var(--muted)}
.setup .cfg-note{color:var(--muted);font-size:12px;margin:2px 4px 0;line-height:1.55}
"""


def _sweep_block(results, meta, xkey, xs, refs, note, xlog, label, xtitle):
    """Fold a sweep's result rows into a compact, JS-friendly block: per
    (framework|payload) arrays of each metric aligned to `xs`, plus the reference
    lines (configured limit + core count) that annotate this sweep's x-axis."""
    frameworks = meta["frameworks"]
    payloads = [(p[0], p[1]) for p in meta["payloads"]]
    idx = {(r["framework"], r["payload"], r.get(xkey)): r for r in results}
    data = {}
    for f in frameworks:
        for pname, _ in payloads:
            arr = {"t": [], "p50": [], "p90": [], "p99": [], "rss": []}
            for x in xs:
                r = idx.get((f, pname, x))
                arr["t"].append(r.get("throughput_rps") if r else None)
                arr["p50"].append(r.get("p50_ms") if r else None)
                arr["p90"].append(r.get("p90_ms") if r else None)
                arr["p99"].append(r.get("p99_ms") if r else None)
                arr["rss"].append(r.get("rss_mb") if r else None)
            data[f"{f}|{pname}"] = arr
    return {
        "label": label,
        "xkey": xkey,
        "xtitle": xtitle,
        "xlog": xlog,
        "xs": xs,
        "frameworks": frameworks,
        "payloads": [p[0] for p in payloads],
        "payload_bytes": {p[0]: p[1] for p in payloads},
        "refs": refs,
        "note": note,
        "data": data,
    }


# ── Non-destructive run history ──────────────────────────────────────────────


def _git_info() -> dict:
    def g(args):
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    return {
        "sha": g(["rev-parse", "--short", "HEAD"]) or "nogit",
        "branch": g(["rev-parse", "--abbrev-ref", "HEAD"]),
        "subject": g(["log", "-1", "--pretty=%s"]),
    }


def save_run(
    outdir: str,
    conc=None,
    conc_meta=None,
    work=None,
    work_meta=None,
    label: str = "",
    extra_sweeps: dict | None = None,
) -> str:
    """Append this benchmark run to ``<outdir>/history/`` and return its run id.

    Never overwrites a prior run — each run is one timestamped JSON keyed by
    ``<UTC-ish timestamp>_<git sha>``, plus an ``index.json`` manifest. This is
    the archive the dashboard reads; the top-level results.json/.md remain a
    convenience 'latest' copy."""
    d = pathlib.Path(outdir)
    hist = d / "history"
    hist.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    git = _git_info()
    # Millisecond suffix keeps ids unique even for rapid successive runs on the
    # same commit (seconds granularity + same sha would otherwise collide).
    run_id = (
        now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}_" + git["sha"]
    )
    cores = (conc_meta or work_meta or {}).get("cores")
    entry = {
        "id": run_id,
        "ts": now.isoformat(timespec="seconds"),
        "sha": git["sha"],
        "branch": git["branch"],
        "subject": git["subject"],
        "host": socket.gethostname(),
        "cores": cores,
        "label": label or "",
        "sweeps": {},
    }
    if conc is not None and conc_meta:
        entry["sweeps"]["concurrency"] = {"meta": conc_meta, "results": conc}
    if work is not None and work_meta:
        entry["sweeps"]["workers"] = {"meta": work_meta, "results": work}
    for key, (res, meta) in (extra_sweeps or {}).items():
        if res is not None and meta:
            entry["sweeps"][key] = {"meta": meta, "results": res}
    (hist / f"{run_id}.json").write_text(json.dumps(entry, indent=2))

    idx_path = hist / "index.json"
    index = []
    if idx_path.exists():
        try:
            index = json.loads(idx_path.read_text())
        except Exception:  # noqa: BLE001
            index = []
    index = [e for e in index if e.get("id") != run_id]
    summary = {
        k: entry[k]
        for k in ("id", "ts", "sha", "branch", "subject", "host", "cores", "label")
    }
    summary["sweeps"] = list(entry["sweeps"].keys())
    index.append(summary)
    index.sort(key=lambda e: e.get("ts", ""))
    idx_path.write_text(json.dumps(index, indent=2))
    return run_id


def load_history(outdir: str) -> list[dict]:
    """Load every saved run (oldest first), each a full entry with sweep results."""
    hist = pathlib.Path(outdir) / "history"
    idx_path = hist / "index.json"
    if not idx_path.exists():
        return []
    try:
        index = json.loads(idx_path.read_text())
    except Exception:  # noqa: BLE001
        return []
    runs = []
    for e in index:
        p = hist / f"{e['id']}.json"
        if p.exists():
            with contextlib.suppress(Exception):
                runs.append(json.loads(p.read_text()))
    runs.sort(key=lambda r: r.get("ts", ""))
    return runs


def _run_sweeps(run: dict) -> dict:
    """Build the JS sweep blocks (with reference lines) for one archived run."""
    sweeps = {}
    for skey, sd in run.get("sweeps", {}).items():
        meta, res = sd["meta"], sd["results"]
        cores = meta.get("cores")
        if skey == "concurrency":
            w = meta.get("workers")
            refs = []
            if w:
                refs.append({"v": w, "label": f"W={w}", "kind": "cfg"})
            if cores:
                refs.append({"v": cores, "label": f"{cores} cores", "kind": "sys"})
            note = (
                f"W={w} workers · {meta.get('duration_s')}s window · "
                f"client={meta.get('client', 'wrk')}"
            )
            sweeps["concurrency"] = _sweep_block(
                res,
                meta,
                "concurrency",
                meta["concurrencies"],
                refs,
                note,
                True,
                "Concurrency sweep",
                "concurrent connections",
            )
        elif skey == "workers":
            sc = meta.get("sweep_concurrency")
            refs = []
            if cores:
                refs.append({"v": cores, "label": f"{cores} cores", "kind": "sys"})
            maxw = max(meta["worker_counts"]) if meta.get("worker_counts") else 0
            if sc and sc <= maxw:
                refs.append({"v": sc, "label": f"concurrency={sc}", "kind": "cfg"})
            note = f"fixed concurrency={sc} (saturating) · cores={cores}"
            sweeps["workers"] = _sweep_block(
                res,
                meta,
                "workers",
                meta["worker_counts"],
                refs,
                note,
                False,
                "Worker-count sweep",
                "workers (W)",
            )
        elif skey == "bounded":
            refs = []
            if cores:
                refs.append({"v": cores, "label": f"{cores} cores", "kind": "sys"})
            note = (
                "bounded connections (each series' c is derived from W) — the "
                "threaded model's design regime; c=W exposes the wakeup-latency "
                "floor, c=2W the saturated ceiling"
            )
            sweeps["bounded"] = _sweep_block(
                res,
                meta,
                "workers",
                meta["worker_counts"],
                refs,
                note,
                False,
                "Bounded-connections sweep (threaded vs reactor)",
                "workers (W)",
            )
        elif skey == "connscaling":
            refs = []
            if cores:
                refs.append({"v": cores, "label": f"{cores} cores", "kind": "sys"})
            w = meta.get("workers")
            if w:
                refs.append({"v": w, "label": f"W={w}", "kind": "cfg"})
            note = (
                f"mostly-idle keep-alive connections ({meta.get('think_ms')}ms "
                "think-time) — the connection-capacity workload; blocking "
                "models plateau at ~their slot count"
            )
            sweeps["connscaling"] = _sweep_block(
                res,
                meta,
                "conns",
                meta["conns"],
                refs,
                note,
                True,
                "Connection-scaling sweep (idle keep-alive)",
                "keep-alive connections",
            )
    return sweeps


def write_html(
    outdir: str, conc=None, conc_meta=None, work=None, work_meta=None
) -> pathlib.Path:
    """Render the interactive dashboard from the full run history (all saved runs).

    Reads ``<outdir>/history/`` and embeds every archived run, so nothing a prior
    run measured is lost. A control layer slices live — run (which archived run),
    compare-vs (overlay a baseline run to see change over time), sweep, metric,
    payload (focus / small-multiples), framework (legend toggle), linear/log Y.
    Configured-limit and CPU-core reference lines annotate each x-axis.

    ``conc``/``work`` args are accepted for signature compatibility but ignored —
    callers should ``save_run()`` first, then this renders from history."""
    from plotly.offline import get_plotlyjs

    d = pathlib.Path(outdir)
    d.mkdir(parents=True, exist_ok=True)

    runs = load_history(outdir)
    run_blocks = []
    for r in runs:
        configs, interp = {}, ""
        for sd in r.get("sweeps", {}).values():
            mt = sd.get("meta", {})
            for k, v in (mt.get("configs") or {}).items():
                configs.setdefault(k, v)
            interp = interp or mt.get("interpreter", "")
        run_blocks.append(
            {
                "id": r["id"],
                "label": r.get("label", ""),
                "ts": r.get("ts", ""),
                "sha": r.get("sha", ""),
                "branch": r.get("branch", ""),
                "subject": r.get("subject", ""),
                "cores": r.get("cores"),
                "configs": configs,
                "interpreter": interp,
                "sweeps": _run_sweeps(r),
            }
        )

    data = {
        "cores": runs[-1].get("cores") if runs else None,
        "colors": _PALETTE,
        "metrics": [
            {"key": "t", "label": "Throughput", "unit": "req/s"},
            {"key": "p50", "label": "p50 latency", "unit": "ms"},
            {"key": "p90", "label": "p90 latency", "unit": "ms"},
            {"key": "p99", "label": "p99 latency", "unit": "ms"},
            {"key": "rss", "label": "Server RSS", "unit": "MiB"},
        ],
        "runs": run_blocks,
        "default_run": run_blocks[-1]["id"] if run_blocks else None,
    }

    doc = (
        _HTML_SHELL.replace("/*CSS*/", _CSS)
        .replace("/*DATA*/", json.dumps(data))
        .replace("/*DASH*/", _DASH_JS)
        .replace("/*PLOTLY*/", get_plotlyjs())
    )
    (d / "report.html").write_text(doc)
    return d / "report.html"


_HTML_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>hyperdjango — HTTP benchmark</title>
<style>/*CSS*/</style>
<script>/*PLOTLY*/</script>
</head><body>
<h1>HTTP framework benchmark</h1>
<p class="sub" id="subtitle"></p>
<div class="controls" id="controls"></div>
<div class="refnote" id="refnote"></div>
<div class="setup" id="setup"></div>
<div id="chart"></div>
<p class="foot">Interactive: hover for values · drag to zoom · double-click to reset · click a legend entry to toggle a framework · the camera icon exports a PNG. Rendered with Plotly (embedded inline — no network needed).</p>
<script>const DATA=/*DATA*/;</script>
<script>/*DASH*/</script>
</body></html>"""


_DASH_JS = r"""
(function(){
  const M={}; DATA.metrics.forEach(m=>M[m.key]=m);
  const RUNS=DATA.runs||[];
  const chartEl=document.getElementById('chart');
  if(!RUNS.length){ chartEl.textContent='No runs yet — run:  hyper-bench --mode both'; return; }

  const runById=id=>RUNS.find(r=>r.id===id)||RUNS[RUNS.length-1];
  const sweepsOf=r=>(r&&r.sweeps)||{};
  const state={ run:DATA.default_run||RUNS[RUNS.length-1].id, compare:'', sweep:null,
                metric:'t', view:'focus', payload:null, logy:false };
  (function(){ const sw=Object.keys(sweepsOf(runById(state.run))); state.sweep=sw.includes('concurrency')?'concurrency':(sw[0]||null); })();

  const FALL=['#0072B2','#009E73','#E69F00','#D55E00','#CC79A7','#56B4E9'];
  function colorFor(f,i){ return (DATA.colors&&DATA.colors[f])||FALL[i%FALL.length]; }
  function isDark(){ const t=document.documentElement.getAttribute('data-theme'); if(t)return t==='dark'; return !!(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches); }
  function theme(){ return isDark()
    ? {paper:'#0f1216',plot:'#0f1216',font:'#e6eaef',grid:'#2a3038',zero:'#3a424c',muted:'#9aa4af',cfg:'#d9a441',tag:'#171b21'}
    : {paper:'#ffffff',plot:'#ffffff',font:'#1a1f26',grid:'#e6ebef',zero:'#c7ced5',muted:'#5b6570',cfg:'#b9770e',tag:'#f2f5f8'}; }
  function el(tag,attrs,kids){ const e=document.createElement(tag); if(attrs)for(const k in attrs){ if(k==='class')e.className=attrs[k]; else if(k==='text')e.textContent=attrs[k]; else e.setAttribute(k,attrs[k]);} (kids||[]).forEach(k=>e.appendChild(k)); return e; }
  function seg(items,cur,onPick){ const box=el('div',{class:'seg'}); items.forEach(it=>{ const b=el('button',{text:it.label}); if(it.key===cur)b.classList.add('on'); b.onclick=()=>onPick(it.key); box.appendChild(b);}); return box; }
  function ctl(label,node,id){ const c=el('div',{class:'ctl'},[el('span',{class:'lbl',text:label}),node]); if(id)c.id=id; return c; }

  function curSweeps(){ return sweepsOf(runById(state.run)); }
  function S(){ return curSweeps()[state.sweep]||null; }
  function baseSweep(){ if(!state.compare)return null; const r=runById(state.compare); return r?(sweepsOf(r)[state.sweep]||null):null; }
  function ensureSweep(){ const sw=Object.keys(curSweeps()); if(!sw.includes(state.sweep)) state.sweep=sw.includes('concurrency')?'concurrency':(sw[0]||null); }
  function ensurePayload(){ const s=S(); if(s&&!s.payloads.includes(state.payload)) state.payload=s.payloads[0]; }
  function payloadLabel(s,p){ const b=s.payload_bytes[p]; return (b&&b>0)?p:p+' (baseline)'; }
  function runLabel(r){ const l=r.label?r.label+' · ':''; const t=(r.ts||'').replace('T',' ').slice(0,16); return l+(r.sha||'?')+(t?' · '+t:''); }

  function buildControls(){
    ensureSweep();
    const c=document.getElementById('controls'); c.innerHTML='';
    const runSel=el('select');
    RUNS.slice().reverse().forEach(r=>{ const o=el('option',{value:r.id,text:runLabel(r)}); if(r.id===state.run)o.selected=true; runSel.appendChild(o); });
    runSel.onchange=()=>{ state.run=runSel.value; if(state.compare===state.run)state.compare=''; ensureSweep(); render(); };
    c.appendChild(ctl('Run', runSel));
    if(RUNS.length>1){
      const cmp=el('select'); cmp.appendChild(el('option',{value:'',text:'— none —'}));
      RUNS.slice().reverse().forEach(r=>{ if(r.id===state.run)return; const o=el('option',{value:r.id,text:runLabel(r)}); if(r.id===state.compare)o.selected=true; cmp.appendChild(o); });
      cmp.onchange=()=>{ state.compare=cmp.value; render(); };
      c.appendChild(ctl('Compare vs', cmp));
    }
    const sw=Object.keys(curSweeps());
    c.appendChild(ctl('Sweep', seg(sw.map(k=>({key:k,label:curSweeps()[k].label})), state.sweep, k=>{state.sweep=k; ensurePayload(); render();})));
    c.appendChild(ctl('Metric', seg(DATA.metrics.map(m=>({key:m.key,label:m.label})), state.metric, k=>{state.metric=k; render();})));
    c.appendChild(ctl('View', seg([{key:'focus',label:'Focus'},{key:'grid',label:'All payloads'}], state.view, k=>{state.view=k; render();})));
    const s=S(); const sel=el('select');
    if(s) s.payloads.forEach(p=>{ const o=el('option',{value:p,text:payloadLabel(s,p)}); if(p===state.payload)o.selected=true; sel.appendChild(o); });
    sel.onchange=()=>{ state.payload=sel.value; render(); };
    const payCtl=ctl('Payload',sel,'pay-ctl'); if(state.view!=='focus')payCtl.classList.add('hidden'); c.appendChild(payCtl);
    const chk=el('label',{class:'chk'}); const box=el('input'); box.type='checkbox'; box.checked=state.logy;
    box.onchange=()=>{state.logy=box.checked; render();};
    chk.appendChild(box); chk.appendChild(document.createTextNode(' log scale (Y)')); c.appendChild(el('div',{class:'ctl'},[chk]));
  }

  function hoverTmpl(f,sfx){ const u=state.metric==='t'?'%{y:,.0f} req/s':(state.metric==='rss'?'%{y:.0f} MiB':'%{y:.2f} ms'); return f+(sfx||'')+': '+u+'<extra></extra>'; }
  function seriesTraces(sw,payload,ax,showlegend,dashed){
    return sw.frameworks.map((f,i)=>{
      const arr=(sw.data[f+'|'+payload]||{})[state.metric]||[]; const col=colorFor(f,i);
      return { type:'scatter', mode:'lines+markers', name:f+(dashed?' (base)':''), x:sw.xs, y:arr, connectgaps:false,
        legendgroup:f, showlegend:showlegend&&!dashed,
        line:{color:col,width:dashed?1.8:2.7,dash:dashed?'dot':'solid'},
        marker:{color:col,size:dashed?5:7,symbol:dashed?'circle-open':'circle'},
        opacity:dashed?0.7:1, hovertemplate:hoverTmpl(f,dashed?' (base)':''), xaxis:ax.x, yaxis:ax.y };
    });
  }
  function scan(sw,payloads,agg){ payloads.forEach(p=> sw.frameworks.forEach(f=>{ ((sw.data[f+'|'+p]||{})[state.metric]||[]).forEach(v=>{ if(v!=null)agg(v); }); })); }
  function dataMax(sw,payloads){ let mx=0; scan(sw,payloads,v=>{if(v>mx)mx=v;}); const b=baseSweep(); if(b)scan(b,payloads,v=>{if(v>mx)mx=v;}); return mx||1; }
  function dataMin(sw,payloads){ let mn=Infinity; scan(sw,payloads,v=>{if(v>0&&v<mn)mn=v;}); const b=baseSweep(); if(b)scan(b,payloads,v=>{if(v>0&&v<mn)mn=v;}); return mn===Infinity?1:mn; }

  // Reference lines. Labels are STAGGERED near the top; the y-axis carries extra
  // headroom (below) so labels sit in empty space above the curves, not on them.
  function refs(sw,xref,yref,showLabels){
    const th=theme(),shapes=[],annots=[];
    (sw.refs||[]).forEach((r,ri)=>{
      const col=r.kind==='cfg'?th.cfg:th.muted;
      shapes.push({type:'line',xref:xref,yref:yref+' domain',x0:r.v,x1:r.v,y0:0,y1:1,line:{color:col,width:1.6,dash:r.kind==='cfg'?'dot':'dash'},layer:'below'});
      if(showLabels) annots.push({xref:xref,yref:yref+' domain',x:r.v,y:(ri%2?0.995:0.92),yanchor:'top',xanchor:'center',
        text:r.label,showarrow:false,font:{size:10,color:col},bgcolor:th.tag,borderpad:1,opacity:0.96});
    });
    return {shapes,annots};
  }
  function baseLayout(rightLegend){
    const th=theme();
    const L={ paper_bgcolor:th.paper, plot_bgcolor:th.plot,
      font:{color:th.font,family:'-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif',size:12.5},
      hovermode:'x unified', hoverlabel:{bgcolor:th.paper,bordercolor:th.grid,font:{color:th.font}},
      shapes:[], annotations:[] };
    if(rightLegend){ L.margin={l:68,r:158,t:18,b:52}; L.legend={orientation:'v',x:1.02,xanchor:'left',y:1,font:{size:12.5}}; }
    else { L.margin={l:60,r:16,t:56,b:48}; L.legend={orientation:'h',y:1.17,x:0,font:{size:12}}; }
    return L;
  }
  function axis(th,title,islog){ return { gridcolor:th.grid,zerolinecolor:th.zero,linecolor:th.grid,tickcolor:th.grid,title:{text:title,font:{size:12,color:th.muted}},tickfont:{size:11,color:th.muted},tickangle:0,automargin:true,type:islog?'log':'linear' }; }
  function xrange(sw){ const vals=sw.xs.slice(); (sw.refs||[]).forEach(r=>vals.push(r.v)); let lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals); if(sw.xlog)return [Math.log10(lo)-0.06,Math.log10(hi)+0.06]; const pad=(hi-lo)*0.04||1; return [lo-pad,hi+pad]; }
  function setYRange(ax,sw,payloads,head){
    const mx=dataMax(sw,payloads);
    if(state.logy){ ax.range=[Math.log10(Math.max(dataMin(sw,payloads)*0.8,1e-4)), Math.log10(mx*(head||1.9))]; }
    else { ax.range=[0, mx*(head||1.28)]; }
  }

  function renderFocus(){
    const sw=S(),th=theme(),m=M[state.metric];
    chartEl.style.height='640px';
    const L=baseLayout(true);
    L.xaxis=Object.assign(axis(th,sw.xtitle,sw.xlog),{tickvals:sw.xs,ticktext:sw.xs.map(String),range:xrange(sw)});
    L.yaxis=axis(th,m.label+' ('+m.unit+')',state.logy); setYRange(L.yaxis,sw,[state.payload],1.28);
    const r=refs(sw,'x','y',true); L.shapes=r.shapes; L.annotations=r.annots;
    let traces=seriesTraces(sw,state.payload,{x:'x',y:'y'},true,false);
    const b=baseSweep(); if(b&&b.payloads.includes(state.payload)) traces=traces.concat(seriesTraces(b,state.payload,{x:'x',y:'y'},false,true));
    Plotly.react('chart',traces,L,CONFIG);
  }
  function renderGrid(){
    const sw=S(),th=theme(),m=M[state.metric];
    const ps=sw.payloads,n=ps.length,cols=Math.min(3,n),rows=Math.ceil(n/cols);
    const L=baseLayout(false);
    L.grid={rows:rows,columns:cols,pattern:'independent',roworder:'top to bottom'};
    L.margin={l:60,r:16,t:58,b:48}; L.height=Math.max(560,rows*300);
    let traces=[],shapes=[],annots=[]; const b=baseSweep();
    ps.forEach((p,i)=>{
      const sfx=i===0?'':(i+1),xa='x'+sfx,ya='y'+sfx;
      traces=traces.concat(seriesTraces(sw,p,{x:xa,y:ya},i===0,false));
      if(b&&b.payloads.includes(p)) traces=traces.concat(seriesTraces(b,p,{x:xa,y:ya},false,true));
      L['xaxis'+sfx]=Object.assign(axis(th,(i>=n-cols)?sw.xtitle:'',sw.xlog),{nticks:5,range:xrange(sw)});
      L['yaxis'+sfx]=axis(th,(i%cols===0)?m.label:'',state.logy); setYRange(L['yaxis'+sfx],sw,[p],1.18);
      annots.push({xref:xa+' domain',yref:ya+' domain',x:0.5,y:1.06,yanchor:'bottom',xanchor:'center',text:'<b>'+payloadLabel(sw,p)+'</b>',showarrow:false,font:{size:12,color:th.font}});
      const r=refs(sw,xa,ya,false); shapes=shapes.concat(r.shapes); annots=annots.concat(r.annots);
    });
    L.shapes=shapes; L.annotations=annots;
    chartEl.style.height=L.height+'px';
    Plotly.react('chart',traces,L,CONFIG);
  }

  const CONFIG={responsive:true, displaylogo:false, modeBarButtonsToRemove:['lasso2d','select2d'],
    toImageButtonOptions:{format:'png',scale:2,filename:'hyperdjango-http-benchmark'}};

  function updateRefnote(){
    const sw=S(),run=runById(state.run);
    document.getElementById('subtitle').textContent =
      sw.label+' · '+sw.note+' · hyperdjango (threaded/reactor) vs FastAPI vs Flask · latency & memory are lower-is-better.';
    const box=document.getElementById('refnote'); box.innerHTML='';
    const ri=el('span',{class:'tag'}); ri.textContent='run: '+runLabel(run)+(run.subject?('  —  '+run.subject):''); box.appendChild(ri);
    if(state.compare){ const bR=runById(state.compare); const t=el('span',{class:'tag'}); t.appendChild(el('span',{class:'dash'})); t.appendChild(document.createTextNode('dotted = baseline: '+runLabel(bR))); box.appendChild(t); }
    (sw.refs||[]).forEach(r=>{ const tag=el('span',{class:'tag'}); tag.appendChild(el('span',{class:'dash'+(r.kind==='cfg'?' cfg':'')})); tag.appendChild(document.createTextNode(r.label+(r.kind==='cfg'?' — configured limit':' — CPU cores'))); box.appendChild(tag); });
  }

  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function updateSetup(){
    const box=document.getElementById('setup'); if(!box) return;
    const run=runById(state.run), sw=S(), cfgs=run.configs||{};
    const fws=(sw&&sw.frameworks)||Object.keys(cfgs);
    if(!Object.keys(cfgs).length){ box.innerHTML=''; return; }
    const rows=fws.map((f,i)=>'<div class="cfg-row"><span class="sw" style="background:'+colorFor(f,i)+'"></span><b>'+esc(f)+'</b><span class="cfg-desc">'+esc(cfgs[f]||'')+'</span></div>').join('');
    box.innerHTML='<details><summary>⚙ server configuration &amp; interpreter</summary>'+
      '<div class="cfg-list">'+rows+'</div>'+
      '<p class="cfg-note">Interpreter: <b>'+esc(run.interpreter||'—')+'</b> · same parallelism budget W for all · loopback · client=wrk (closed-loop, keep-alive). '+
      'Flask &amp; FastAPI run on the free-threaded build (not their usual interpreter) so their absolute numbers are conservative; sync WSGI (Flask) is also inherently slower per request. W = native threads (hyperdjango) or worker processes (uvicorn/gunicorn).</p></details>';
  }

  function render(){ ensureSweep(); ensurePayload(); buildControls(); if(!S()){chartEl.textContent='No data for this run/sweep.';return;} updateRefnote(); updateSetup(); if(state.view==='focus')renderFocus(); else renderGrid(); }

  if(window.matchMedia){ try{ window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render); }catch(e){} }
  render();
})();
"""
