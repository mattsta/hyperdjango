"""Orchestrate the HTTP framework benchmark matrix and write reports.

    uv run hyper-bench                                   # full matrix
    uv run hyper-bench --quick                           # fast smoke
    uv run hyper-bench --mode workers --reactor-counts 1,auto,8 --profile
    uv run hyper-bench --frameworks hyperdjango-threaded,hyperdjango-reactor

For each framework the server starts once, then every (payload, concurrency)
cell is measured against it (with server RSS sampled during the load), and the
results are written to benchmarks/http/out/ as results.json + report.md.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import resource
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from benchmarks.http.affinity import core_count, describe, preflight
from benchmarks.http.connscaling import run_conn_scaling
from benchmarks.http.contention import GAUGE_ACTIVE, ContentionSample, scrape
from benchmarks.http.fixtures import ServerFixture, config_summary
from benchmarks.http.loadgen import run_load, run_load_wrk, wrk_available
from benchmarks.http.profile import WindowProfiler
from benchmarks.http.report import (
    save_run,
    worker_sweep_verdict,
    write_html,
    write_reports,
    write_worker_reports,
)
from benchmarks.http.topology import detect_auto_pin

# Only the native hyperdjango app exposes /metrics for contention scraping.
_METRICS_FRAMEWORKS = ("hyperdjango-threaded", "hyperdjango-reactor")

HOST = "127.0.0.1"
PORT = 18981

ALL_FRAMEWORKS = [
    "hyperdjango-threaded",
    "hyperdjango-reactor",
    "fastapi",
    "flask",
]
# Payload ladder: a plaintext baseline (small fixed body) then JSON bodies in
# power-of-two steps from 64 B to 256 KiB, so response-size scaling is visible.
PAYLOADS = [
    ("plaintext", 0),
    ("64B", 64),
    ("256B", 256),
    ("1KiB", 1024),
    ("4KiB", 4096),
    ("16KiB", 16384),
    ("64KiB", 65536),
    ("256KiB", 262144),
]
CONCURRENCIES = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]


CLIENT = "python"  # set in main(): "wrk" (lightweight, preferred) or "python"


def _fmt_count(n: int) -> str:
    """Compact count for the degradation columns (1234 -> '1.2k')."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ── Machine preparation (suite mode) ─────────────────────────────────────────


def _read_governor() -> str | None:
    p = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    try:
        return p.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _set_governor(gov: str) -> bool:
    """Set the cpufreq governor on every CPU via passwordless sudo. Returns
    True on success; never raises (a box without sudo just keeps its governor
    and the recorded meta says so)."""
    try:
        r = subprocess.run(
            f"echo {gov} | sudo -n tee "
            "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
            shell=True,
            capture_output=True,
            timeout=20,
        )
        return r.returncode == 0
    except OSError, subprocess.SubprocessError:
        return False


def _prepare_machine(args) -> str | None:
    """Suite-mode preflight: raise the fd limit, set the `performance`
    governor when passwordless sudo allows it, and verify wrk. Returns the
    previous governor when it was changed (caller restores it), else None."""
    with contextlib.suppress(OSError, ValueError):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            print(f"[machine] RLIMIT_NOFILE raised {soft} -> {hard}")
    if not shutil.which("wrk"):
        print("[machine] wrk not installed — falling back to the python client")
    # Comparison deps live in the optional `benchmark-comparison` group; a
    # plain `uv sync --group dev` removes them. Catch that HERE, not 40
    # minutes later as skipped cells and a dead plotly import at render time.
    missing = [
        mod
        for mod in ("fastapi", "uvicorn", "flask", "gunicorn", "psutil", "plotly")
        if importlib.util.find_spec(mod) is None
    ]
    if missing:
        print(
            f"[machine] comparison deps missing: {', '.join(missing)} — "
            "run the suite via `make bench-http` (or "
            "`uv run --group benchmark-comparison hyper-bench ...`); "
            "affected frameworks/report features will be skipped"
        )
    prev = _read_governor()
    if prev is not None and prev != "performance":
        if _set_governor("performance"):
            print(f"[machine] cpufreq governor {prev} -> performance (will restore)")
            return prev
        print(
            f"[machine] governor is '{prev}' and passwordless sudo is unavailable "
            "— numbers will be noisier (see docs/benchmarks.md)"
        )
    return None


def _apply_auto_pin(args) -> None:
    """Fill --server-cores/--client-cores from the machine topology when the
    caller didn't pin explicitly. Whole physical cores, SMT siblings idle,
    NUMA-node split when there are 2+ nodes — the validated manual layout,
    derived instead of hand-written."""
    if args.server_cores or args.client_cores:
        return
    pin = detect_auto_pin()
    if pin is None:
        print("[auto-pin] no usable topology split (small box or non-Linux) — unpinned")
        return
    args.server_cores = pin.server_cores
    args.client_cores = pin.client_cores
    args.numa = args.numa or pin.numa_nodes >= 2
    print(f"[auto-pin] {pin.description}")
    print(
        f"[auto-pin] --server-cores {pin.server_cores} "
        f"--client-cores {pin.client_cores}" + (" --numa" if args.numa else "")
    )


def _worker_ladder(budget: int) -> list[int]:
    """Power-of-two worker ladder up to the server core budget, always ending
    exactly at the budget (the peak cell), e.g. 64 -> [8, 16, 32, 64]."""
    ladder = []
    w = 8
    while w < budget:
        ladder.append(w)
        w *= 2
    if budget >= 8:
        ladder.append(budget)
    return sorted(set(ladder)) or [budget]


def _bench_cell(
    fx: ServerFixture,
    path: str,
    concurrency: int,
    duration: float,
    warmup: float,
    client_procs: int | None = None,
    sample_contention: bool = False,
    profile_outdir: str | None = None,
    profile_tag: str = "",
    client_cores: str | None = None,
    client_threads: int | None = None,
    numa: bool = False,
) -> dict:
    """Run one load cell while sampling the server's peak RSS, and — for the
    native app — the peak in-flight gauge plus a before/after /metrics scrape,
    and (optionally) an OS profiler around the window. The extra signals are
    what turn a plateau into an attributed contention verdict."""
    peak = {"rss": 0.0, "active": 0.0}
    stop = threading.Event()
    cont = ContentionSample()
    if sample_contention:
        cont.before = scrape(HOST, PORT)
        cont.pool_exposed = bool(cont.before)

    def sampler():
        while not stop.is_set():
            peak["rss"] = max(peak["rss"], fx.rss_mb())
            if sample_contention:
                m = scrape(HOST, PORT)
                if m:
                    peak["active"] = max(peak["active"], m.get(GAUGE_ACTIVE, 0.0))
            time.sleep(0.1)

    def drive() -> dict:
        if CLIENT == "wrk":
            return run_load_wrk(
                HOST,
                PORT,
                path,
                concurrency,
                duration_s=duration,
                warmup_s=warmup,
                threads=client_threads,
                cpu_cores=client_cores,
                numa=numa,
            )
        return run_load(
            HOST,
            PORT,
            path,
            concurrency,
            duration_s=duration,
            warmup_s=warmup,
            procs=client_procs,
        )

    t = threading.Thread(target=sampler)
    t.start()
    profile = None
    try:
        if profile_outdir is not None and fx.proc is not None:
            with WindowProfiler(
                fx.proc.pid, duration + warmup, profile_outdir, profile_tag
            ) as prof:
                result = drive()
            profile = prof.result.to_dict()
        else:
            result = drive()
    finally:
        stop.set()
        t.join()
    result["rss_mb"] = peak["rss"]
    if sample_contention:
        cont.after = scrape(HOST, PORT)
        cont.active_peak = peak["active"]
        result["contention"] = cont.to_dict()
    if profile is not None:
        result["profile"] = profile
    return result


def _config_meta(frameworks) -> dict:
    """Per-framework launch config + interpreter, for the report's setup panel."""
    gilfree = hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()
    ver = ".".join(map(str, sys.version_info[:3]))
    interp = f"CPython {ver} " + ("free-threaded, no GIL" if gilfree else "(GIL)")
    return {
        "configs": {fw: config_summary(fw) for fw in frameworks},
        "interpreter": interp,
    }


def _cpu_governor() -> str | None:
    """The active cpufreq governor (Linux), or None where inapplicable. A
    non-`performance` governor (schedutil/powersave) makes many-core sweeps
    non-monotonic: partially-loaded cores idle at min frequency while
    oversubscribed runs peg every core to max — observed here as W=64 slower
    than W=96 on the same 64-core pin. Recorded so every archived run states
    the frequency policy it ran under."""
    gov_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    try:
        return gov_path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _machine_meta(args) -> dict:
    """Machine/topology provenance for the archived run: the exact core pins,
    their sizes, and the frequency governor — the three knobs that decide
    whether two runs of the same sweep are even comparable."""
    gov = _cpu_governor()
    server_n = core_count(args.server_cores) or (os.cpu_count() or 0)
    meta = {
        "governor": gov,
        "server_cores": args.server_cores,
        "client_cores": args.client_cores,
        "server_core_count": server_n,
        "client_core_count": core_count(args.client_cores) or None,
        "numa": args.numa,
    }
    if gov is not None and gov != "performance":
        print(
            f"[machine] cpufreq governor is '{gov}', not 'performance' — "
            "partially-loaded worker counts will run at reduced clocks and the "
            "W-sweep can look non-monotonic. For stable numbers: "
            "echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
        )
    return meta


def _concurrency_sweep(frameworks, args) -> None:
    payloads = [("tiny", 0), ("1k", 1024)] if args.quick else PAYLOADS
    concurrencies = [1, 8, 64] if args.quick else CONCURRENCIES
    meta = {
        "frameworks": frameworks,
        "payloads": payloads,
        "concurrencies": concurrencies,
        "workers": args.workers,
        "cores": os.cpu_count(),
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "client": CLIENT,
        **_config_meta(frameworks),
    }
    results: list[dict] = []
    for fw in frameworks:
        print(f"\n=== [concurrency] {fw} (W={args.workers}) ===")
        try:
            with ServerFixture(fw, HOST, PORT, args.workers) as fx:
                for pname, pbytes in payloads:
                    path = "/plaintext" if pbytes == 0 else f"/json?n={pbytes}"
                    for c in concurrencies:
                        cell = _bench_cell(
                            fx, path, c, args.duration, args.warmup, args.client_procs
                        )
                        cell.update(framework=fw, payload=pname, payload_bytes=pbytes)
                        results.append(cell)
                        print(
                            f"  {pname:5s} c={c:<4d} {cell['throughput_rps']:>10,.0f} rps  "
                            f"p99={cell['p99_ms']:.1f}ms  rss={cell['rss_mb']:.0f}MiB"
                        )
        except Exception as e:  # noqa: BLE001
            print(f"  SKIPPED {fw}: {e}")
    out = write_reports(results, meta, args.outdir)
    print(f"\n[concurrency] reports -> {out}/report.md, results.json")
    return results, meta


def _reactor_counts_for(fw: str, args) -> list[int | None]:
    """Reactor-shard axis for one framework. Only the reactor model shards; a
    bare `--reactor-counts` value of `auto` (the default) means one pass at the
    server's own capacity-scaled default (env unset). An explicit list pins
    HYPER_HTTP_REACTOR_COUNT so the queue-sharding effect is a report column."""
    if fw != "hyperdjango-reactor" or not args.reactor_counts:
        return [None]
    out: list[int | None] = []
    for tok in args.reactor_counts.split(","):
        tok = tok.strip()
        if not tok:
            continue
        out.append(None if tok == "auto" else int(tok))
    return out or [None]


def _worker_sweep(frameworks, args):
    worker_counts = [int(w) for w in args.worker_counts.split(",") if w.strip()]
    # Full payload ladder (payloads loop within one server fixture, so more
    # payloads add no extra restarts — only the W axis restarts the server).
    payloads = [("plaintext", 0), ("16KiB", 16384)] if args.quick else PAYLOADS
    meta = {
        "frameworks": frameworks,
        "payloads": payloads,
        "worker_counts": worker_counts,
        "sweep_concurrency": args.sweep_concurrency,
        "cores": os.cpu_count(),
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "client": CLIENT,
        "profile": args.profile,
        **_config_meta(frameworks),
        **_machine_meta(args),
    }
    server_core_budget = meta["server_core_count"]
    results: list[dict] = []
    for fw in frameworks:
        scrape_contention = fw in _METRICS_FRAMEWORKS
        for rc in _reactor_counts_for(fw, args):
            for w in worker_counts:
                rc_note = "" if rc is None else f" rc={rc}"
                print(
                    f"\n=== [workers] {fw} W={w}{rc_note} "
                    f"(cores={os.cpu_count()}, c={args.sweep_concurrency}) ==="
                )
                try:
                    with ServerFixture(
                        fw,
                        HOST,
                        PORT,
                        w,
                        reactor_count=rc,
                        cpu_cores=args.server_cores,
                        numa=args.numa,
                    ) as fx:
                        for pname, pbytes in payloads:
                            path = "/plaintext" if pbytes == 0 else f"/json?n={pbytes}"
                            cell = _bench_cell(
                                fx,
                                path,
                                args.sweep_concurrency,
                                args.duration,
                                args.warmup,
                                args.client_procs,
                                sample_contention=scrape_contention,
                                profile_outdir=args.outdir if args.profile else None,
                                profile_tag=f"{fw}_w{w}_rc{rc}_{pname}",
                                client_cores=args.client_cores,
                                client_threads=args.client_threads,
                                numa=args.numa,
                            )
                            cell.update(
                                framework=fw,
                                payload=pname,
                                payload_bytes=pbytes,
                                workers=w,
                                reactor_count=rc,
                                server_core_budget=server_core_budget,
                                oversubscribed=bool(
                                    server_core_budget and w > server_core_budget
                                ),
                            )
                            results.append(cell)
                            active = (cell.get("contention") or {}).get("active_peak")
                            act = f"  active={active:.0f}" if active else ""
                            # Degradation columns: shed/error responses and
                            # socket errors are part of the story, not noise.
                            bad = ""
                            non2xx = cell.get("non2xx", 0)
                            if non2xx:
                                bad += f"  non2xx={_fmt_count(non2xx)}"
                            errsum = (
                                cell.get("err_connect", 0)
                                + cell.get("err_read", 0)
                                + cell.get("err_write", 0)
                            )
                            tmo = cell.get("err_timeout", 0)
                            if errsum:
                                bad += f"  sockerr={_fmt_count(errsum)}"
                            if tmo:
                                bad += f"  timeout={_fmt_count(tmo)}"
                            sf = cell.get("served_frac")
                            if sf is not None and sf < 0.9:
                                bad += f"  served={sf * 100:.0f}%"
                            print(
                                f"  {pname:5s} {cell['throughput_rps']:>10,.0f} rps  "
                                f"p99={cell['p99_ms']:.1f}ms  rss={cell['rss_mb']:.0f}MiB{act}{bad}"
                            )
                except Exception as e:  # noqa: BLE001
                    print(f"  SKIPPED {fw} W={w}{rc_note}: {e}")

    # Auto-verdict: flag any adjacent-W step where rps drops, with the contention
    # delta as the suspected cause — the headline the plateau chart can't state.
    verdict = worker_sweep_verdict(results)
    meta["negative_scaling_flags"] = verdict
    print("\n=== AUTO-VERDICT: worker scaling ===")
    if verdict:
        for line in verdict:
            print(f"  ⚠ {line}")
    else:
        print(
            "  ✓ no negative-scaling steps — rps is monotonic or flat across every W series"
        )

    out = write_worker_reports(results, meta, args.outdir)
    print(f"\n[workers] reports -> {out}/report_workers.md, results_workers.json")
    return results, meta


def _bounded_sweep(args):
    """Bounded-connections regime: threaded vs reactor at c=W and c=2W.

    This is the threaded model's DESIGN workload (every connection has a
    dedicated worker: no shedding, no starvation) and the regime where it
    earns its keep — same machine ceiling as the reactor at c=2W with ~6x
    lower p99. c=W is also measured because it exposes the wakeup-latency
    floor (one idle-wake per request); the report shows both so neither
    number gets quoted outside its meaning. Series are encoded as
    `<framework> (c=W|c=2W)` so the standard report machinery renders them
    as separate lines over the worker-count x-axis."""
    budget = core_count(args.server_cores) or (os.cpu_count() or 8)
    ladder = _worker_ladder(min(budget, 64) if budget > 64 else budget)
    payloads = [("plaintext", 0), ("16KiB", 16384)]
    hd_frameworks = ["hyperdjango-threaded", "hyperdjango-reactor"]
    regimes = [("c=W", 1), ("c=2W", 2)]
    meta = {
        "frameworks": [f"{fw} ({rn})" for fw in hd_frameworks for rn, _ in regimes],
        "payloads": payloads,
        "worker_counts": ladder,
        "cores": os.cpu_count(),
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "client": CLIENT,
        **_config_meta(hd_frameworks),
        **_machine_meta(args),
    }
    results: list[dict] = []
    for fw in hd_frameworks:
        for w in ladder:
            print(f"\n=== [bounded] {fw} W={w} (c={w} and c={2 * w}) ===")
            try:
                with ServerFixture(
                    fw,
                    HOST,
                    PORT,
                    w,
                    cpu_cores=args.server_cores,
                    numa=args.numa,
                ) as fx:
                    for rname, mult in regimes:
                        conc = w * mult
                        for pname, pbytes in payloads:
                            path = "/plaintext" if pbytes == 0 else f"/json?n={pbytes}"
                            cell = _bench_cell(
                                fx,
                                path,
                                conc,
                                args.duration,
                                args.warmup,
                                args.client_procs,
                                client_cores=args.client_cores,
                                client_threads=min(conc, args.client_threads or conc),
                                numa=args.numa,
                            )
                            cell.update(
                                framework=f"{fw} ({rname})",
                                payload=pname,
                                payload_bytes=pbytes,
                                workers=w,
                            )
                            results.append(cell)
                            print(
                                f"  {pname:9s} {rname:5s} "
                                f"{cell['throughput_rps']:>10,.0f} rps  "
                                f"p99={cell['p99_ms']:.2f}ms"
                            )
            except Exception as e:  # noqa: BLE001
                print(f"  SKIPPED {fw} W={w}: {e}")
    return results, meta


def _conn_scaling_sweep(frameworks, args):
    """Connection-scaling: sweep the number of keep-alive connections, each MOSTLY
    IDLE (think-time between requests) — the real-web-traffic workload. Exposes the
    connection-model ceiling the busy max-throughput test can't: blocking models
    (threaded, Flask slots) plateau at ~their slot count no matter how many
    connections arrive; multiplexing models (reactor, FastAPI async) scale with
    connections until cores / OS limits finally bend them. Fixed W; async client."""
    conns = (
        [8, 64, 512] if args.quick else [8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096]
    )
    w = args.cs_workers
    think = args.cs_think_ms
    meta = {
        "frameworks": frameworks,
        "conns": conns,
        "payloads": [("plaintext", 0)],
        "workers": w,
        "think_ms": think,
        "cores": os.cpu_count(),
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "client": "asyncio (think-time)",
        "path": "/plaintext",
        **_config_meta(frameworks),
    }
    results: list[dict] = []
    for fw in frameworks:
        print(f"\n=== [conn-scaling] {fw} (W={w}, think={think}ms) ===")
        try:
            with ServerFixture(fw, HOST, PORT, w):
                for n in conns:
                    r = run_conn_scaling(
                        HOST,
                        PORT,
                        "/plaintext",
                        n,
                        think_ms=think,
                        duration_s=args.duration,
                        warmup_s=args.warmup,
                    )
                    r.update(framework=fw, conns=n, payload="plaintext")
                    results.append(r)
                    print(
                        f"  N={n:>5}  {r['throughput_rps']:>9,.0f} rps  "
                        f"served={r['served_frac']:>5.0f}%  shed={r.get('shed_frac', 0):>4.0f}%  "
                        f"p99={r['p99_ms']:.1f}ms  err={r['errors']}"
                    )
        except Exception as e:  # noqa: BLE001
            print(f"  SKIPPED {fw}: {e}")
    return results, meta


def main() -> int:
    # Line-buffer stdout even when redirected (nohup/log-file runs): a 40-min
    # suite whose progress sits invisible in an 8 KB block buffer until exit
    # is indistinguishable from a hung one.
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode",
        choices=["concurrency", "workers", "both", "bounded", "conn", "all"],
        default="concurrency",
        help="which sweep(s) to run. `all` is the one-command suite: machine "
        "prep (governor/fd limits), topology auto-pin, then workers + "
        "bounded + concurrency + connection-scaling sweeps archived as ONE "
        "run in the combined report.",
    )
    ap.add_argument(
        "--auto-pin",
        action="store_true",
        help="derive --server-cores/--client-cores from the machine topology "
        "(disjoint physical cores, NUMA-node split, SMT siblings idle) when "
        "not pinned explicitly. Implied by --mode all.",
    )
    ap.add_argument("--frameworks", default=",".join(ALL_FRAMEWORKS))
    ap.add_argument(
        "--workers", type=int, default=8, help="fixed W for the concurrency sweep"
    )
    ap.add_argument(
        "--worker-counts",
        default="4,8,12,16,18,24,32,50",
        help="W values for the worker sweep",
    )
    ap.add_argument(
        "--sweep-concurrency",
        type=int,
        default=128,
        help="fixed (saturating) client concurrency for the worker sweep",
    )
    ap.add_argument(
        "--reactor-counts",
        default="",
        help="reactor-shard axis for hyperdjango-reactor: comma list pinning "
        "HYPER_HTTP_REACTOR_COUNT (e.g. '1,4,16'), or 'auto' for the "
        "capacity-scaled default. Empty = single auto pass.",
    )
    ap.add_argument(
        "--profile",
        action="store_true",
        help="attribute each worker-sweep cell with an OS profiler around the "
        "load window: perf stat on Linux (context-switches/cache-misses/"
        "cpu-migrations), py-spy flamegraph on macOS; skipped cleanly if absent",
    )
    ap.add_argument(
        "--server-cores",
        default=None,
        help="taskset-style core list to pin the SERVER to (e.g. '0-63'). "
        "Isolate it from the client's cores so they don't steal cores and "
        "workers don't migrate across NUMA nodes. Linux-only; no-op elsewhere.",
    )
    ap.add_argument(
        "--client-cores",
        default=None,
        help="taskset-style core list to pin the LOAD GENERATOR to (e.g. "
        "'64-127'), DISJOINT from --server-cores. When set, wrk's thread "
        "count defaults to this set's size (the 4-thread default caps a big run).",
    )
    ap.add_argument(
        "--client-threads",
        type=int,
        default=None,
        help="wrk thread count (overrides the auto default). Raise this on a "
        "many-core box so the client can actually saturate the server.",
    )
    ap.add_argument(
        "--numa",
        action="store_true",
        help="use numactl (--physcpubind + --localalloc) instead of taskset for "
        "--server-cores/--client-cores, keeping each process's memory node-local",
    )
    ap.add_argument(
        "--cs-workers",
        type=int,
        default=os.cpu_count() or 8,
        help="fixed W for the connection-scaling sweep",
    )
    ap.add_argument(
        "--cs-think-ms",
        type=float,
        default=25.0,
        help="per-connection think-time (ms) for the connection-scaling sweep",
    )
    ap.add_argument("--duration", type=float, default=2.0)
    ap.add_argument("--warmup", type=float, default=0.5)
    ap.add_argument(
        "--client",
        choices=["auto", "wrk", "python"],
        default="auto",
        help="load generator: wrk (lightweight, preferred) or the built-in "
        "python one; auto picks wrk if installed",
    )
    ap.add_argument(
        "--client-procs",
        type=int,
        default=None,
        help="python load-generator process count (single-machine tuning)",
    )
    ap.add_argument(
        "--quick", action="store_true", help="small matrix for a smoke test"
    )
    ap.add_argument(
        "--label",
        default="",
        help="human label for this run in the history (e.g. 'fast-path')",
    )
    ap.add_argument("--outdir", default="benchmarks/http/out")
    args = ap.parse_args()

    global CLIENT
    if args.client == "wrk" or (args.client == "auto" and wrk_available()):
        CLIENT = "wrk"
    else:
        CLIENT = "python"
    print(f"load generator: {CLIENT}")

    # Validate CPU pinning BEFORE the sweep: a wrapped command that fails leaves
    # empty output that parses as 0 rps for every cell (a silent, confusing
    # "dead server"). Abort here with the launcher's real error instead.
    for label, cores in (
        ("--server-cores", args.server_cores),
        ("--client-cores", args.client_cores),
    ):
        err = preflight(cores, args.numa)
        if err:
            print(f"\n[affinity] {label} {cores!r} cannot be applied:\n  {err}")
            print(
                "[affinity] Fix the core list, install taskset/numactl "
                "(util-linux / numactl), or drop the pin flags. Refusing to run "
                "so the sweep doesn't report a silent 0 rps for every cell."
            )
            return 2
        if cores:
            print(f"[affinity] {label} -> {describe(cores, args.numa)}")

    frameworks = [f.strip() for f in args.frameworks.split(",") if f.strip()]
    conc = conc_meta = work = work_meta = None
    extra_sweeps: dict = {}
    prev_governor: str | None = None

    if args.mode == "all":
        # The one-command suite. Every phase archives into the SAME run entry
        # so the report shows one coherent, comparable snapshot: the c>>W
        # worker sweep (where the reactor + process models compete), the
        # bounded c<=2W sweep (where the threaded model competes), the
        # concurrency curve at peak W, and connection-scaling (idle
        # keep-alive capacity).
        prev_governor = _prepare_machine(args)
        _apply_auto_pin(args)
        budget = core_count(args.server_cores) or (os.cpu_count() or 8)
        args.worker_counts = ",".join(str(w) for w in _worker_ladder(budget))
        args.workers = budget  # concurrency sweep runs at peak parallelism
        print(
            f"\n[suite] phases: workers (W={args.worker_counts}, "
            f"c={args.sweep_concurrency}) -> bounded (c=W/2W) -> "
            f"concurrency (W={budget}) -> conn-scaling"
        )
    elif args.auto_pin:
        _apply_auto_pin(args)

    try:
        if args.mode in ("concurrency", "both"):
            conc, conc_meta = _concurrency_sweep(frameworks, args)
        if args.mode in ("workers", "both", "all"):
            work, work_meta = _worker_sweep(frameworks, args)
        if args.mode in ("bounded", "all"):
            b_res, b_meta = _bounded_sweep(args)
            extra_sweeps["bounded"] = (b_res, b_meta)
        if args.mode == "all":
            conc, conc_meta = _concurrency_sweep(frameworks, args)
        if args.mode in ("conn", "all"):
            cs_res, cs_meta = _conn_scaling_sweep(frameworks, args)
            extra_sweeps["connscaling"] = (cs_res, cs_meta)
    finally:
        if prev_governor is not None:
            if _set_governor(prev_governor):
                print(f"[machine] cpufreq governor restored -> {prev_governor}")
            else:
                print(f"[machine] could not restore governor '{prev_governor}'")

    # Archive this run non-destructively, then rebuild the dashboard from the
    # FULL history so nothing a prior run measured is ever lost.
    run_id = save_run(
        args.outdir,
        conc,
        conc_meta,
        work,
        work_meta,
        label=args.label,
        extra_sweeps=extra_sweeps,
    )
    print(f"\nArchived run -> {args.outdir}/history/{run_id}.json")
    try:
        html_path = write_html(args.outdir)
    except ModuleNotFoundError as exc:
        # The run is SAFE (archived above) — only the render needs plotly.
        print(
            f"HTML report skipped ({exc}). Install the dashboard deps and "
            "re-render without re-measuring:\n"
            "    uv sync --group dev --group benchmark-comparison\n"
            "    uv run python -c 'from benchmarks.http.report import "
            f'write_html; write_html("{args.outdir}")\''
        )
    else:
        print(f"HTML report (all {len(_history_ids(args.outdir))} runs) -> {html_path}")
    return 0


def _history_ids(outdir):
    from benchmarks.http.report import load_history

    return load_history(outdir)


if __name__ == "__main__":
    raise SystemExit(main())
