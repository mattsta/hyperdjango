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
from dataclasses import dataclass, field

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


# Below this payload size the body byte rate is not the interesting number —
# the per-request path is. The GB/s tables cover the payloads at or above it,
# where a plateau means the box's memory/loopback bandwidth, not the server.
BANDWIDTH_MIN_BYTES = 16384


def body_gbps(row: dict) -> float | None:
    """Body byte rate (GB/s) for one result row.

    The runner records `body_gbps` on every row at measurement time. Runs
    archived before that field existed are derived from the row's own payload
    size, so the whole history renders on one axis instead of showing a cliff
    where the field was introduced."""
    v = row.get("body_gbps")
    if v is not None:
        return v
    rps, nbytes = row.get("throughput_rps"), row.get("payload_bytes")
    if rps is None or nbytes is None:
        return None
    return rps * nbytes / 1e9


def _cell_value(row: dict, key: str) -> float | None:
    return body_gbps(row) if key == "body_gbps" else row.get(key)


def _fmt_cell(value: float | None, key: str) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}" if key == "body_gbps" else _fmt(value)


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
            cells.append(_fmt_cell(_cell_value(r, key) if r else None, key))
        lines.append(f"| {c} | {' | '.join(cells)} |")
    return "\n".join(lines) + f"\n\n_{unit}_\n"


_BANDWIDTH_NOTE = (
    "Body bytes actually moved (throughput x payload size). Read it as the "
    "ceiling test: once a series' GB/s flattens while rps keeps falling with "
    "payload size, the cell is reporting the box's memory / loopback (and, on a "
    "split-NUMA pin, cross-socket) bandwidth — no server-side change moves it."
)


def _bandwidth_section(payloads, note: str) -> list[str]:
    """Header for the GB/s tables, or nothing when this sweep ran no payload
    large enough for body bandwidth to be the story."""
    if not any(nbytes >= BANDWIDTH_MIN_BYTES for _, nbytes in payloads):
        return []
    return [
        f"## Body bandwidth (GB/s) — payloads >= {BANDWIDTH_MIN_BYTES // 1024} KiB\n",
        note + "\n",
    ]


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
    out.extend(_bandwidth_section(payloads, _BANDWIDTH_NOTE))
    for name, nbytes in payloads:
        if nbytes < BANDWIDTH_MIN_BYTES:
            continue
        out.append(f"### {name} (~{nbytes} B)\n")
        out.append(
            _table(
                results, name, frameworks, concurrencies, "body_gbps", "GB/s of body"
            )
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
            cells.append(_fmt_cell(_cell_value(r, key) if r else None, key))
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
    out.extend(_bandwidth_section(payloads, _BANDWIDTH_NOTE))
    for name, nbytes in payloads:
        if nbytes < BANDWIDTH_MIN_BYTES:
            continue
        out.append(f"### {name} (~{nbytes} B) — GB/s vs W\n")
        out.append(_worker_table(results, name, frameworks, worker_counts, "body_gbps"))
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


def _served_frac_cause(cell: dict) -> list[str]:
    """Interpret a low `served_frac` — but only ever as far as the evidence goes.

    `served_frac` is Little's law over the CLIENT's latency (rps x mean latency
    / connections). It is a genuine starvation detector when the missing
    connections are stuck somewhere the client's latency clock never starts —
    an accept backlog, or a threaded server that pins one worker per connection.
    It is NOT a starvation detector in general: the client's clock also stops
    between "last response byte read" and "next request written", so a server
    that is FASTER than its load generator shows a low served_frac while serving
    every connection perfectly. At large response bodies that turnaround
    dominates, and served_frac sits near a constant no matter what the server
    does.

    So consult the server's own connection census before claiming starvation.
    `parked_unserved` counts connections the server holds armed that have never
    completed a response — the actual signature of a starved set — and
    `queue_depth` says whether work is piling up server-side. When the server
    reports every connection served and no dispatch backlog, the deficit is
    client turnaround and saying "starved" sends the reader hunting a bug that
    is not there.
    """
    sf = cell.get("served_frac")
    if sf is None or sf >= 0.9:
        return []
    conns = cell.get("concurrency")
    served = cell.get("served_conns", 0)
    head = f"only {sf * 100:.0f}% of connections in service (~{served:.0f}/{conns}"
    c = cell.get("contention") or {}
    # Steady-state mean, never the peak: the peak always includes the instant
    # after the client reconnects, when every connection is legitimately armed
    # and not yet served. A starved SET is one that stays unserved, so require
    # it to be a real fraction of the connection count, not a transient.
    unserved = c.get("parked_unserved_mean")
    queued = c.get("queue_depth_mean")
    # Both thresholds scale with the connection count. A handful of connections
    # briefly unserved out of a thousand is reconnect churn, not a starved set;
    # and a dispatch queue holding a few percent of the connections is a server
    # keeping up, not one falling behind (the server-bound cells in this sweep
    # sit at ~90% of connections queued).
    starved_floor = max(1.0, 0.02 * (conns or 0))
    queue_floor = max(4.0, 0.10 * (conns or 0))
    if (
        unserved is not None
        and unserved < starved_floor
        and (queued or 0) < queue_floor
    ):
        # Server-side census says otherwise — this is measurement, not starvation.
        return [
            f"{head} by client latency) — server census reports no unserved "
            "connections and no dispatch backlog, so this is load-generator "
            "turnaround, NOT server starvation"
        ]
    if unserved and unserved >= starved_floor:
        return [f"{head}) — server census confirms {unserved:.0f} unserved connections"]
    return [f"{head}) — rest starved, in reconnect churn, or client turnaround"]


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
    causes.extend(_served_frac_cause(cell))
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


# ── Connection-scaling: capacity headline + client-ceiling annotation ────────
#
# The conn-scaling regime opens N mostly-idle keep-alive connections (a request,
# then ~25 ms of think-time, repeat) and sweeps N. Its question is CAPACITY —
# how many connections a server actually HOLDS IN SERVICE — not peak rps, and
# the two are not the same number:
#
#   * rps in this regime is roughly (connections held) x (1 / think-time). A
#     thread-per-connection server that caps at ~W held connections therefore
#     reports a flat rps for every N past W: that flat number is an
#     ARCHITECTURAL CAP being restated, not a throughput result. Headlining it
#     hides the actual finding (the cap) behind a number that looks merely
#     "lower than the others".
#   * a multiplexing server holds every connection offered, so its rps is
#     bounded by whatever else is slowest — including the LOAD GENERATOR. A
#     server measured at hundreds of thousands of rps in the busy regime cannot
#     suddenly have a ~20k ceiling here; when several architecturally different
#     servers cluster at the same rps with full service and healthy latency,
#     the shared client is the common term.
#
# So the headline is `max_held`: the largest N in the ladder the server held
# with essentially every connection in service AND a p99 still under a stated
# bound. rps stays as a secondary column — a lower bound on request rate, kept
# because its SHAPE (the cap, the plateau) is evidence even when its magnitude
# is the generator's.

# A cell counts as "held" only if it served essentially every connection and
# answered within this p99 bound. 250 ms is the default: an interactive request
# that takes longer than a quarter second at the tail is not being "held" in any
# sense a user would accept. Recorded per-run in meta as `cs_p99_bound_ms`.
CS_P99_BOUND_MS = 250.0
# `served_frac` on a conn-scaling row is a PERCENT (served_conns / N * 100) —
# these rows come only from run_conn_scaling, which records it that way.
CS_SERVED_MIN_PCT = 99.0
# Adjacent-N rps gain below this fraction is a plateau (the series stopped
# converting extra connections into extra requests) ...
CS_PLATEAU_GAIN = 0.05
# ... but a fall this steep is a collapse (a server bending), not a ceiling.
CS_PLATEAU_COLLAPSE = -0.25
# A flat region far below the series' own best is not that series' ceiling.
CS_PLATEAU_MIN_FRAC = 0.75
# Plateau rps values within this spread of each other are "the same number".
CS_CLUSTER_SPREAD = 0.15

CS_CEILING_NOTE = (
    "plateau with full service and healthy latency — load-generator ceiling; "
    "rps is a LOWER BOUND, capacity verdict comes from max_held"
)


@dataclass(frozen=True, slots=True)
class ConnCell:
    """One conn-scaling ladder cell, reduced to the fields the verdict reads."""

    conns: int
    rps: float
    p99_ms: float
    served_pct: float
    shed_pct: float

    def held(self, p99_bound_ms: float) -> bool:
        """Did the server HOLD this many connections: essentially all of them in
        service, answered inside the p99 bound, with actual requests moving."""
        return (
            self.rps > 0
            and self.served_pct >= CS_SERVED_MIN_PCT
            and 0 < self.p99_ms <= p99_bound_ms
        )

    def why_not_held(self, p99_bound_ms: float) -> str:
        if self.rps <= 0:
            return "no throughput (dead cell)"
        if self.served_pct < CS_SERVED_MIN_PCT:
            return f"served {self.served_pct:.0f}% < {CS_SERVED_MIN_PCT:.0f}%"
        if self.p99_ms > p99_bound_ms:
            return f"p99 {self.p99_ms:.0f}ms > {p99_bound_ms:.0f}ms bound"
        return ""


@dataclass(frozen=True, slots=True)
class ConnCapacity:
    """One framework's conn-scaling verdict: what it held, and how to read its rps."""

    framework: str
    max_held: int  # 0 = held nothing in the ladder
    held_rps: float  # rps at max_held
    held_p99_ms: float  # p99 at max_held
    ladder_top: int
    ladder_limited: bool  # held the top of the ladder — real cap is above it
    first_fail: int  # smallest N past max_held that was NOT held (0 = none)
    fail_reason: str
    peak_rps: float
    peak_rps_conns: int
    plateau: bool
    plateau_rps: float
    plateau_from: int
    plateau_to: int

    @property
    def held_label(self) -> str:
        if not self.max_held:
            return "none"
        return f">={self.max_held}" if self.ladder_limited else str(self.max_held)

    @property
    def note(self) -> str:
        return CS_CEILING_NOTE if self.plateau else ""


def _conn_cells(results: list[dict], framework: str) -> list[ConnCell]:
    """This framework's ladder, ordered by connection count."""
    rows = [r for r in results if r.get("framework") == framework]
    cells = [
        ConnCell(
            conns=int(r.get("conns") or r.get("n_conns") or 0),
            rps=float(r.get("throughput_rps") or 0.0),
            p99_ms=float(r.get("p99_ms") or 0.0),
            served_pct=float(r.get("served_frac") or 0.0),
            shed_pct=float(r.get("shed_frac") or 0.0),
        )
        for r in rows
    ]
    return sorted((c for c in cells if c.conns > 0), key=lambda c: c.conns)


def _plateau_span(cells: list[ConnCell], p99_bound_ms: float) -> tuple[int, int, float]:
    """The series' top flat region among HELD cells: (from_N, to_N, rps).

    A plateau is a run of adjacent ladder steps whose rps stops growing while
    service stays complete and latency stays healthy — i.e. extra connections
    stopped producing extra requests for a reason that is NOT the server
    failing. Steep falls are excluded: a collapse is a server bending under
    load, a different finding with a different cause. Returns (0, 0, 0.0) when
    the series has no such region."""
    best: tuple[int, int, float] = (0, 0, 0.0)
    held_peak = max((c.rps for c in cells if c.held(p99_bound_ms)), default=0.0)
    if held_peak <= 0:
        return best
    start: ConnCell | None = None
    span: list[ConnCell] = []

    def close(sp: list[ConnCell]) -> None:
        nonlocal best
        if len(sp) < 2:
            return
        top = max(c.rps for c in sp)
        if top < held_peak * CS_PLATEAU_MIN_FRAC or top <= best[2]:
            return
        best = (sp[0].conns, sp[-1].conns, top)

    for lo, hi in zip(cells, cells[1:]):
        flat = (
            lo.held(p99_bound_ms)
            and hi.held(p99_bound_ms)
            and lo.rps > 0
            and CS_PLATEAU_COLLAPSE < (hi.rps - lo.rps) / lo.rps < CS_PLATEAU_GAIN
        )
        if flat:
            if start is None:
                start, span = lo, [lo]
            span.append(hi)
        else:
            close(span)
            start, span = None, []
    close(span)
    return best


def conn_scaling_capacity(
    results: list[dict], meta: dict, p99_bound_ms: float | None = None
) -> list[ConnCapacity]:
    """Per-framework capacity verdicts for one conn-scaling sweep.

    Computed entirely from fields every archived run already records
    (`conns`, `throughput_rps`, `p99_ms`, `served_frac`), so an existing
    archive re-renders with the new headline without being re-measured."""
    bound = p99_bound_ms or meta.get("cs_p99_bound_ms") or CS_P99_BOUND_MS
    frameworks = meta.get("frameworks") or sorted(
        {r.get("framework", "") for r in results} - {""}
    )
    caps: list[ConnCapacity] = []
    for fw in frameworks:
        cells = _conn_cells(results, fw)
        if not cells:
            continue
        held = [c for c in cells if c.held(bound)]
        top = cells[-1].conns
        last = held[-1] if held else None
        after = [
            c for c in cells if not c.held(bound) and (not last or c.conns > last.conns)
        ]
        peak = max(cells, key=lambda c: c.rps)
        pf, pt, prps = _plateau_span(cells, bound)
        caps.append(
            ConnCapacity(
                framework=fw,
                max_held=last.conns if last else 0,
                held_rps=last.rps if last else 0.0,
                held_p99_ms=last.p99_ms if last else 0.0,
                ladder_top=top,
                ladder_limited=bool(last and last.conns == top),
                first_fail=after[0].conns if after else 0,
                fail_reason=after[0].why_not_held(bound) if after else "",
                peak_rps=peak.rps,
                peak_rps_conns=peak.conns,
                plateau=prps > 0,
                plateau_rps=prps,
                plateau_from=pf,
                plateau_to=pt,
            )
        )
    return caps


def conn_scaling_verdict(
    caps: list[ConnCapacity], p99_bound_ms: float = CS_P99_BOUND_MS
) -> list[str]:
    """The conn-scaling auto-verdict: one capacity line per framework, the
    client-ceiling annotation on every plateaued series, and — when several
    architecturally different servers plateau at the SAME rps — the sweep-level
    shared-generator suspicion. Same shape as `worker_sweep_verdict`: printable
    lines, most important first, empty when there is nothing to say."""
    lines: list[str] = []
    for c in sorted(caps, key=lambda c: c.max_held, reverse=True):
        tail = ""
        if c.ladder_limited:
            tail = (
                f" (held the top of the ladder — real capacity is above "
                f"{c.ladder_top}, this sweep did not find it)"
            )
        elif c.first_fail:
            tail = f" (N={c.first_fail} not held: {c.fail_reason})"
        elif not c.max_held:
            tail = " (held nothing in this ladder)"
        lines.append(
            f"{c.framework}: max_held={c.held_label} conns @ "
            f"{_fmt_rps(c.held_rps)} rps, p99 {c.held_p99_ms:.0f}ms{tail}"
        )
        if c.plateau:
            lines.append(
                f"  ^ N={c.plateau_from}->{c.plateau_to} {_fmt_rps(c.plateau_rps)} rps "
                f"{CS_CEILING_NOTE}"
            )
    flat = [c for c in caps if c.plateau]
    if len(flat) >= 2:
        lo = min(c.plateau_rps for c in flat)
        hi = max(c.plateau_rps for c in flat)
        if lo > 0 and (hi - lo) / lo <= CS_CLUSTER_SPREAD:
            names = ", ".join(
                f"{c.framework} {_fmt_rps(c.plateau_rps)}"
                for c in sorted(flat, key=lambda c: -c.plateau_rps)
            )
            lines.append(
                f"SWEEP NOTE: {len(flat)} frameworks plateau within "
                f"{(hi - lo) / lo:.0%} of each other ({names}) while serving every "
                "connection inside the p99 bound. Architecturally different "
                "servers do not share a throughput ceiling — the SHARED "
                "think-time load generator is the suspect. Read these rps values "
                "as the generator's ceiling and compare the servers on max_held; "
                "re-measure rps with more client shards (--cs-client-procs) "
                f"before quoting any of them. [p99 bound {p99_bound_ms:.0f}ms]"
            )
    return lines


def _conn_table(
    results: list[dict], frameworks: list[str], conns: list[int], key: str
) -> str:
    lines = [
        f"| conns | {' | '.join(frameworks)} |",
        "|" + "---|" * (len(frameworks) + 1),
    ]
    idx = {(r.get("framework"), r.get("conns")): r for r in results}
    for n in conns:
        cells = []
        for fw in frameworks:
            r = idx.get((fw, n))
            v = r.get(key) if r else None
            cells.append("—" if v is None else _fmt(float(v)))
        lines.append(f"| {n} | {' | '.join(cells)} |")
    return "\n".join(lines) + "\n"


def render_conn_scaling_markdown(results: list[dict], meta: dict) -> str:
    """The conn-scaling section: capacity headline first, rps curve beneath."""
    bound = float(meta.get("cs_p99_bound_ms") or CS_P99_BOUND_MS)
    frameworks = meta.get("frameworks") or []
    conns = meta.get("conns") or sorted(
        {r.get("conns") for r in results if r.get("conns")}
    )
    caps = conn_scaling_capacity(results, meta, bound)
    procs = meta.get("cs_client_procs")

    ladder = f"{conns[0]}..{conns[-1]}" if conns else "(no cells)"
    out = ["# Connection scaling — capacity (mostly-idle keep-alive)", ""]
    out.append(
        f"- Regime: {ladder} keep-alive "
        f"connections, each ~{meta.get('think_ms', 25)}ms think-time between "
        f"requests — MOSTLY IDLE, the real-web-traffic shape.\n"
        f"- Server parallelism budget W = **{meta.get('workers', '?')}**; "
        f"client = {meta.get('client', 'asyncio (think-time)')}"
        + (f", {procs} shard processes" if procs else "")
        + ".\n"
        f"- **Headline = `max_held`**: the largest N held with "
        f"served >= {CS_SERVED_MIN_PCT:.0f}% of connections AND "
        f"p99 <= {bound:.0f}ms. rps is secondary here — in this regime it is "
        f"~(connections held) x (1 / think-time), so it restates the connection "
        f"cap rather than measuring throughput, and it is capped by the load "
        f"generator as well as by the server.\n"
    )
    out.append("## Capacity (headline)\n")
    out.append(
        "| framework | max_held (conns) | rps @ max_held | p99 @ max_held | "
        "first N not held | peak rps (N) |"
    )
    out.append("|---|---|---|---|---|---|")
    for c in caps:
        fail = (
            f"{c.first_fail} — {c.fail_reason}"
            if c.first_fail
            else ("— (ladder top)" if c.ladder_limited else "—")
        )
        out.append(
            f"| {c.framework} | **{c.held_label}** | {_fmt_rps(c.held_rps)} | "
            f"{c.held_p99_ms:.1f} ms | {fail} | "
            f"{_fmt_rps(c.peak_rps)} (N={c.peak_rps_conns}) |"
        )
    out.append("")
    notes = [c for c in caps if c.note]
    if notes:
        out.append("Series annotations:\n")
        for c in notes:
            out.append(
                f"- **{c.framework}** — N={c.plateau_from}->{c.plateau_to} at "
                f"~{_fmt_rps(c.plateau_rps)} rps: {c.note}."
            )
        out.append("")
    verdict = conn_scaling_verdict(caps, bound)
    sweep_notes = [line for line in verdict if line.startswith("SWEEP NOTE:")]
    for line in sweep_notes:
        out.append(f"> **{line}**\n")
    out.append("## Throughput (req/s) vs connections — secondary\n")
    out.append(_conn_table(results, frameworks, conns, "throughput_rps"))
    out.append("## p99 latency (ms) vs connections\n")
    out.append(_conn_table(results, frameworks, conns, "p99_ms"))
    out.append("## Connections served (%) vs connections\n")
    out.append(_conn_table(results, frameworks, conns, "served_frac"))
    return "\n".join(out)


def write_conn_scaling_reports(
    results: list[dict], meta: dict, outdir: str
) -> pathlib.Path:
    d = pathlib.Path(outdir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "results_connscaling.json").write_text(
        json.dumps({"meta": meta, "results": results}, indent=2)
    )
    (d / "report_connscaling.md").write_text(
        render_conn_scaling_markdown(results, meta)
    )
    return d / "report_connscaling.md"


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
.headline{margin:14px 2px 0;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:12px}
.headline h3{margin:0 0 3px;font-size:14px}
.headline .hl-sub{color:var(--muted);font-size:12.5px;margin:0 0 10px;line-height:1.5}
.headline table{margin:0}
.headline .sw{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:7px;vertical-align:middle}
.headline .cap{font-weight:700;font-variant-numeric:tabular-nums}
.headline .ann{color:var(--muted);font-size:12px;margin:8px 0 0;line-height:1.5}
.headline .warn{margin:10px 0 0;padding:9px 12px;border-left:3px solid #b9770e;background:var(--bg);border-radius:6px;font-size:12.5px;line-height:1.55}
@media (prefers-color-scheme:dark){.headline .warn{border-left-color:#d9a441}}
:root[data-theme=dark] .headline .warn{border-left-color:#d9a441}
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
            arr = {"t": [], "p50": [], "p90": [], "p99": [], "rss": [], "gbps": []}
            for x in xs:
                r = idx.get((f, pname, x))
                arr["t"].append(r.get("throughput_rps") if r else None)
                arr["p50"].append(r.get("p50_ms") if r else None)
                arr["p90"].append(r.get("p90_ms") if r else None)
                arr["p99"].append(r.get("p99_ms") if r else None)
                arr["rss"].append(r.get("rss_mb") if r else None)
                # Body bandwidth is a recorded field on every row; the derived
                # fallback keeps pre-field runs on the same chart.
                arr["gbps"].append(body_gbps(r) if r else None)
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


def build_sweeps(
    conc=None,
    conc_meta=None,
    work=None,
    work_meta=None,
    extra_sweeps: dict | None = None,
) -> dict:
    """Fold this run's in-memory sweeps into the ARCHIVED entry shape.

    Shared by `save_run` and the completeness verification so the thing that
    gets classified is byte-for-byte the thing that gets archived — a verifier
    reading a different structure than the archive writes is a verifier that
    eventually blesses a file it never saw."""
    sweeps: dict = {}
    if conc is not None and conc_meta:
        sweeps["concurrency"] = {"meta": conc_meta, "results": conc}
    if work is not None and work_meta:
        sweeps["workers"] = {"meta": work_meta, "results": work}
    for key, (res, meta) in (extra_sweeps or {}).items():
        if res is not None and meta:
            sweeps[key] = {"meta": meta, "results": res}
    return sweeps


# A COMPLETE run must carry the full framework x payload matrix in BOTH
# payload sweeps, and must have actually measured the other two regimes.
MATRIX_SWEEPS = ("workers", "concurrency")
REGIME_SWEEPS = ("bounded", "connscaling")


@dataclass(frozen=True, slots=True)
class Completeness:
    """Verdict of verifying an archived entry's RESULTS against the full matrix."""

    complete: bool
    missing: list[str] = field(default_factory=list)


def verify_completeness(
    entry: dict, frameworks: list[str], payload_names: list[str]
) -> Completeness:
    """Verify a run's ARCHIVED RESULTS carry the full comparison matrix.

    Invocation flags state the INTENT to run everything; they cannot state
    that everything ran. A framework whose optional deps are missing is
    skipped mid-sweep (the fixture raises, the loop prints `SKIPPED` and
    carries on), a server can fail to boot at one worker count, a payload can
    die on an OOM — and every one of those leaves a flag-complete run with
    holes in the matrix. Classifying that as COMPLETE puts a partial run in
    the comparison history, where the gate then reads its absent series as
    "not measured" and its present ones as a baseline nobody can reproduce.

    Rules, all read off the results themselves:
      - `workers` and `concurrency` each carry every framework x every payload;
      - a cell counts as measured only with positive throughput (a 0 rps cell
        is a dead server, not a data point);
      - `bounded` and `connscaling` are present and non-empty.

    Returns the verdict with every missing cell named."""
    sweeps = entry.get("sweeps") or {}
    missing: list[str] = []
    for skey in MATRIX_SWEEPS:
        rows = (sweeps.get(skey) or {}).get("results") or []
        if not rows:
            missing.append(f"{skey}: sweep absent or empty (all cells missing)")
            continue
        measured = {
            (r.get("framework"), r.get("payload"))
            for r in rows
            if (r.get("throughput_rps") or 0) > 0
        }
        for fw in frameworks:
            gaps = [p for p in payload_names if (fw, p) not in measured]
            if not gaps:
                continue
            if len(gaps) == len(payload_names):
                missing.append(f"{skey}: framework {fw!r} absent (all payloads)")
            else:
                missing.append(f"{skey}: {fw} missing payloads {', '.join(gaps)}")
    for skey in REGIME_SWEEPS:
        if not ((sweeps.get(skey) or {}).get("results") or []):
            missing.append(f"{skey}: regime sweep absent or empty")
    return Completeness(not missing, missing)


def save_run(
    outdir: str,
    conc=None,
    conc_meta=None,
    work=None,
    work_meta=None,
    label: str = "",
    extra_sweeps: dict | None = None,
    diagnostic: bool = False,
) -> str:
    """Append this benchmark run to the archive and return its run id.

    COMPLETE runs (the full framework × payload × regime matrix) archive
    under ``<outdir>/history/`` — the ONLY archive the comparison dashboard,
    ``load_history``, and the regression gate ever read. A result compares
    everything against everything else, or it is not a result.

    ``diagnostic=True`` runs (restricted frameworks/payloads/modes — fix
    validation, bisection, A/B probes) archive under ``<outdir>/diagnostics/``
    in the same format: preserved for the investigation record, structurally
    invisible to every comparison surface.

    Never overwrites a prior run — each run is one timestamped JSON keyed by
    ``<UTC-ish timestamp>_<git sha>``, plus an ``index.json`` manifest."""
    d = pathlib.Path(outdir)
    hist = d / ("diagnostics" if diagnostic else "history")
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
        "sweeps": build_sweeps(conc, conc_meta, work, work_meta, extra_sweeps),
    }
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


def _capacity_ref(meta: dict) -> dict | None:
    """The capacity reference line for a sweep chart: the PINNED server core
    budget when the run pinned cores (that is the capacity the server actually
    had), falling back to the machine's logical CPU count for unpinned runs.
    A 256-logical-CPU line on a run whose server owned 64 pinned cores is not
    just axis-stretching noise — it is the wrong capacity story."""
    budget = meta.get("server_core_count")
    cores = meta.get("cores")
    if budget and cores and budget < cores:
        return {"v": budget, "label": f"{budget} pinned server cores", "kind": "sys"}
    if cores:
        return {"v": cores, "label": f"{cores} cores", "kind": "sys"}
    return None


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
            cap = _capacity_ref(meta)
            if cap:
                refs.append(cap)
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
            cap = _capacity_ref(meta)
            if cap:
                refs.append(cap)
            maxw = max(meta["worker_counts"]) if meta.get("worker_counts") else 0
            if sc and sc <= maxw:
                refs.append({"v": sc, "label": f"concurrency={sc}", "kind": "cfg"})
            budget = meta.get("server_core_count") or cores
            note = f"fixed concurrency={sc} (saturating) · server cores={budget}" + (
                f" (machine: {cores})" if budget != cores else ""
            )
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
            cap = _capacity_ref(meta)
            if cap:
                refs.append(cap)
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
            cap = _capacity_ref(meta)
            if cap:
                refs.append(cap)
            w = meta.get("workers")
            if w:
                refs.append({"v": w, "label": f"W={w}", "kind": "cfg"})
            bound = float(meta.get("cs_p99_bound_ms") or CS_P99_BOUND_MS)
            note = (
                f"mostly-idle keep-alive connections ({meta.get('think_ms')}ms "
                "think-time) — the connection-CAPACITY workload; headline is "
                f"max_held (served >= {CS_SERVED_MIN_PCT:.0f}%, p99 <= "
                f"{bound:.0f}ms), rps is secondary"
            )
            block = _sweep_block(
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
            # The capacity headline travels WITH the sweep block, so the
            # dashboard states the regime's real answer above the rps curve
            # instead of leaving the reader to infer it from four lines.
            caps = conn_scaling_capacity(res, meta, bound)
            block["capacity"] = [
                {
                    "framework": c.framework,
                    "max_held": c.max_held,
                    "held_label": c.held_label,
                    "held_rps": c.held_rps,
                    "held_p99_ms": c.held_p99_ms,
                    "ladder_limited": c.ladder_limited,
                    "first_fail": c.first_fail,
                    "fail_reason": c.fail_reason,
                    "peak_rps": c.peak_rps,
                    "peak_rps_conns": c.peak_rps_conns,
                    "note": c.note,
                }
                for c in caps
            ]
            block["verdict"] = conn_scaling_verdict(caps, bound)
            block["p99_bound_ms"] = bound
            sweeps["connscaling"] = block
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
    callers should ``save_run()`` first, then this renders from history.

    Also re-renders the conn-scaling markdown (`report_connscaling.md`) for the
    newest archived run that carries that sweep, so BOTH human surfaces rebuild
    from the archive alone — a reporting change never requires re-measuring."""
    from plotly.offline import get_plotlyjs

    d = pathlib.Path(outdir)
    d.mkdir(parents=True, exist_ok=True)

    runs = load_history(outdir)
    for r in reversed(runs):
        cs = (r.get("sweeps") or {}).get("connscaling")
        if cs and cs.get("results"):
            write_conn_scaling_reports(cs["results"], cs["meta"], outdir)
            break
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
            {"key": "gbps", "label": "Body bandwidth", "unit": "GB/s"},
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
<div class="headline hidden" id="headline"></div>
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

  const HOVER_UNIT={t:'%{y:,.0f} req/s', rss:'%{y:.0f} MiB', gbps:'%{y:.2f} GB/s'};
  function hoverTmpl(f,sfx){ const u=HOVER_UNIT[state.metric]||'%{y:.2f} ms'; return f+(sfx||'')+': '+u+'<extra></extra>'; }
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
    const dhi=Math.max.apply(null,sw.xs);
    (sw.refs||[]).forEach((r,ri)=>{
      if(r.v>dhi*1.15) return; // outside the plotted range — see xrange()
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
  function xrange(sw){
    // Axis range comes from the DATA. A reference line only widens it when it
    // sits within 15% past the last data point — a machine-wide 256-core line
    // must not compress a W<=64 sweep into a corner of the plot.
    const vals=sw.xs.slice();
    let dhi=Math.max.apply(null,vals);
    (sw.refs||[]).forEach(r=>{ if(r.v<=dhi*1.15) vals.push(r.v); });
    let lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals);
    if(sw.xlog)return [Math.log10(lo)-0.06,Math.log10(hi)+0.06];
    const pad=(hi-lo)*0.04||1; return [lo-pad,hi+pad]; }
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

  // Capacity headline (conn-scaling only). This regime's answer is "how many
  // connections did the server HOLD in service", not "what rps did the shared
  // think-time client manage" — so max_held leads and the rps curve sits below
  // it. Sweeps that carry no capacity block hide the panel entirely.
  function updateHeadline(){
    const box=document.getElementById('headline'); if(!box) return;
    const sw=S(), caps=(sw&&sw.capacity)||[];
    if(!caps.length){ box.className='headline hidden'; box.innerHTML=''; return; }
    box.className='headline';
    const bound=sw.p99_bound_ms||250;
    const rows=caps.map(c=>{
      const i=sw.frameworks.indexOf(c.framework);
      const fail=c.first_fail?(c.first_fail+' — '+esc(c.fail_reason)):(c.ladder_limited?'— (ladder top)':'—');
      return '<tr><td><span class="sw" style="background:'+colorFor(c.framework,i)+'"></span>'+esc(c.framework)+'</td>'+
        '<td class="cap">'+esc(c.held_label)+'</td>'+
        '<td>'+(c.held_rps>=1000?(c.held_rps/1000).toFixed(0)+'k':c.held_rps.toFixed(0))+'</td>'+
        '<td>'+c.held_p99_ms.toFixed(1)+' ms</td><td>'+fail+'</td>'+
        '<td>'+(c.peak_rps>=1000?(c.peak_rps/1000).toFixed(0)+'k':c.peak_rps.toFixed(0))+' (N='+c.peak_rps_conns+')</td></tr>';
    }).join('');
    const anns=caps.filter(c=>c.note).map(c=>'<p class="ann"><b>'+esc(c.framework)+'</b> — '+esc(c.note)+'.</p>').join('');
    const warns=(sw.verdict||[]).filter(l=>l.indexOf('SWEEP NOTE:')===0)
      .map(l=>'<p class="warn">'+esc(l)+'</p>').join('');
    box.innerHTML='<h3>Capacity headline — max_held connections</h3>'+
      '<p class="hl-sub">The largest ladder step held with <b>served &ge; 99%</b> of connections and '+
      '<b>p99 &le; '+bound.toFixed(0)+' ms</b>. In this mostly-idle regime rps &asymp; (connections held) &times; '+
      '(1 / think-time), so it restates the connection cap rather than measuring throughput — and it is bounded '+
      'by the load generator as well as by the server. Read rps as a lower bound; compare servers on max_held.</p>'+
      '<table><thead><tr><th>framework</th><th>max_held (conns)</th><th>rps @ max_held</th>'+
      '<th>p99 @ max_held</th><th>first N not held</th><th>peak rps (N)</th></tr></thead><tbody>'+rows+'</tbody></table>'+
      anns+warns;
  }

  function render(){ ensureSweep(); ensurePayload(); buildControls(); if(!S()){chartEl.textContent='No data for this run/sweep.';return;} updateRefnote(); updateSetup(); updateHeadline(); if(state.view==='focus')renderFocus(); else renderGrid(); }

  if(window.matchMedia){ try{ window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render); }catch(e){} }
  render();
})();
"""


# ── Run-to-run regression gate ───────────────────────────────────────────────


def peak_cells(run_entry: dict) -> dict[tuple[str, str, str], float]:
    """Fold one archived run into {(sweep, framework, payload): peak_rps}.

    The PEAK cell per series is the gate's unit of comparison: single cells
    are ±10% noisy run-to-run, but a series' peak is its capacity statement —
    the number the docs quote and the number a real regression moves.
    """
    peaks: dict[tuple[str, str, str], float] = {}
    for skey, sd in (run_entry.get("sweeps") or {}).items():
        for r in sd.get("results") or []:
            rps = r.get("throughput_rps") or 0.0
            key = (skey, r.get("framework", "?"), r.get("payload", "?"))
            if rps > peaks.get(key, 0.0):
                peaks[key] = rps
    return peaks


def compare_runs(
    current: dict, baseline: dict, tolerance: float
) -> tuple[list[str], bool]:
    """Compare two archived runs' per-series peaks. Returns (report lines,
    ok). A series regressing more than `tolerance` (fraction) fails the gate;
    series missing from either side are reported but never fail it."""
    cur, base = peak_cells(current), peak_cells(baseline)
    lines: list[str] = []
    ok = True
    for key in sorted(base):
        skey, fw, payload = key
        b = base[key]
        c = cur.get(key)
        if c is None:
            lines.append(f"  ~ {skey}/{fw}/{payload}: not measured in current run")
            continue
        delta = (c - b) / b if b else 0.0
        mark = "✓"
        if delta < -tolerance:
            mark = "✗ REGRESSION"
            ok = False
        lines.append(
            f"  {mark} {skey}/{fw}/{payload}: {b:,.0f} -> {c:,.0f} rps "
            f"({delta * 100:+.1f}%)"
        )
    for key in sorted(set(cur) - set(base)):
        lines.append(f"  + {key[0]}/{key[1]}/{key[2]}: new series (no baseline)")
    return lines, ok


def find_run_by_label(outdir: str, label: str, exclude_id: str = "") -> dict | None:
    """Most recent archived run with `label` (excluding `exclude_id`, so a
    run never compares against itself)."""
    for run in reversed(load_history(outdir)):
        if run.get("label") == label and run.get("id") != exclude_id:
            return run
    return None
