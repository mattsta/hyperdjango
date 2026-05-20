#!/usr/bin/env python3
"""WebSocket benchmark + interop-validation suite: hyperdjango native vs.
the `websockets` PyPI library.

Runs both servers as real subprocesses, drives them with the identical
`websockets` async client, and produces JSON + Markdown + a
self-contained HTML report covering interop correctness, throughput
(by payload size, text/binary), latency, connection-scaling (the
native thread-pool ceiling vs. the reference server's single-loop
model), memory/CPU/thread usage, and per-connection object overhead.

Usage:
    uv run --group benchmark-comparison python benchmarks/websocket/run.py
    uv run --group benchmark-comparison python benchmarks/websocket/run.py --full
    uv run --group benchmark-comparison python benchmarks/websocket/run.py --full --profile
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
import time
from pathlib import Path

from benchmarks.websocket import client, interop, loadgen, metrics, report
from benchmarks.websocket.fixtures import (
    measure_startup_latency,
    native_fixture,
    reference_fixture,
)
from benchmarks.websocket.workloads import FRAME_TYPES, build_matrix, make_payload

OUT_DIR = Path(__file__).resolve().parent / "out"
NATIVE_PORT = 19901
REFERENCE_PORT = 19902


def _asdict(obj) -> dict:
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj


async def _run_interop(native_url: str, ref_url: str) -> dict:
    native_results = await interop.run_all_checks(native_url)
    ref_results = await interop.run_all_checks(ref_url)
    return {
        "native": [_asdict(r) for r in native_results],
        "reference": [_asdict(r) for r in ref_results],
    }


# Throughput is measured with the multi-process load generator, not a
# single asyncio client: one client process does the same per-message
# work as a single-threaded server and so cannot saturate a multi-core
# server — it becomes the bottleneck and undercounts the server. See
# loadgen.py. We spread a fixed total offered connection count across
# several OS processes so the *client* is never the limiter.
_LOAD_PROCS = min(6, max(2, (os.cpu_count() or 4) // 3))
_LOAD_TOTAL_CONNS = (
    24  # saturates the native default thread pool; ample for the reference too
)


async def _mp_throughput(uri: str, size: int, frame_type: str, duration_s: float):
    conns_per_proc = max(1, _LOAD_TOTAL_CONNS // _LOAD_PROCS)
    return await asyncio.to_thread(
        loadgen.run_multiprocess_throughput,
        uri,
        _LOAD_PROCS,
        conns_per_proc,
        duration_s,
        payload_size=size,
        frame_type=frame_type,
    )


async def _run_throughput(
    native_url: str, ref_url: str, matrix, concurrency: int
) -> list[dict]:
    rows = []
    for frame_type in FRAME_TYPES:
        for size in matrix.payload_sizes:
            native_r = await _mp_throughput(
                native_url, size, frame_type, matrix.throughput_duration_s
            )
            # Settle: let the previous cell's connections fully tear down
            # before the next cell opens pool-size connections again.
            await asyncio.sleep(0.5)
            ref_r = await _mp_throughput(
                ref_url, size, frame_type, matrix.throughput_duration_s
            )
            await asyncio.sleep(0.5)
            rows.append(
                {
                    "payload_size": size,
                    "frame_type": frame_type,
                    "concurrency": native_r.total_conns,
                    "native": _asdict(native_r),
                    "reference": _asdict(ref_r),
                }
            )
            warn = ""
            if (
                native_r.crashed_procs
                or native_r.failed_conns
                or ref_r.crashed_procs
                or ref_r.failed_conns
            ):
                warn = f"  [!] native failed={native_r.failed_conns} crashed={native_r.crashed_procs} ref failed={ref_r.failed_conns} crashed={ref_r.crashed_procs}"
            print(
                f"  throughput  {frame_type:6s} {size:>7}B  "
                f"native={native_r.msgs_per_sec:>9.0f} msg/s  ref={ref_r.msgs_per_sec:>9.0f} msg/s{warn}"
            )
    return rows


async def _run_concurrency_scaling_throughput(
    native_url: str, ref_url: str, matrix
) -> list[dict]:
    """Sweep total offered connections (spread across load procs) at a fixed
    mid-size payload — shows how each server's aggregate throughput scales
    with concurrency once the client is no longer the bottleneck."""
    rows = []
    for conc in matrix.concurrency_levels:
        n_procs = min(_LOAD_PROCS, conc)
        conns_per_proc = max(1, conc // n_procs)
        native_r = await asyncio.to_thread(
            loadgen.run_multiprocess_throughput,
            native_url,
            n_procs,
            conns_per_proc,
            matrix.throughput_duration_s,
            4096,
            "text",
        )
        ref_r = await asyncio.to_thread(
            loadgen.run_multiprocess_throughput,
            ref_url,
            n_procs,
            conns_per_proc,
            matrix.throughput_duration_s,
            4096,
            "text",
        )
        rows.append(
            {
                "concurrency": conc,
                "native": _asdict(native_r),
                "reference": _asdict(ref_r),
            }
        )
        print(
            f"  conc-scaling  offered={conc:>4} ({n_procs}p x{conns_per_proc})  "
            f"native msg/s={native_r.msgs_per_sec:>9.0f}  ref msg/s={ref_r.msgs_per_sec:>9.0f}"
        )
    return rows


async def _run_latency(native_url: str, ref_url: str, matrix) -> list[dict]:
    rows = []
    sizes = (
        matrix.payload_sizes[:1]
        + matrix.payload_sizes[
            len(matrix.payload_sizes) // 2 : len(matrix.payload_sizes) // 2 + 1
        ]
        + matrix.payload_sizes[-1:]
    )
    for size in dict.fromkeys(sizes):  # dedupe, preserve order
        payload = make_payload(size, "text")
        native_r = await client.latency_test(
            native_url, payload, matrix.latency_samples
        )
        ref_r = await client.latency_test(ref_url, payload, matrix.latency_samples)
        rows.append(
            {
                "payload_size": size,
                "native": {
                    "mean_us": native_r.mean_us,
                    "p50_us": native_r.percentile_us(0.5),
                    "p99_us": native_r.percentile_us(0.99),
                },
                "reference": {
                    "mean_us": ref_r.mean_us,
                    "p50_us": ref_r.percentile_us(0.5),
                    "p99_us": ref_r.percentile_us(0.99),
                },
            }
        )
        print(
            f"  latency  {size:>7}B  native p50={native_r.percentile_us(0.5):>7.0f}us  "
            f"ref p50={ref_r.percentile_us(0.5):>7.0f}us"
        )
    return rows


async def _run_connection_scaling(native_url: str, ref_url: str, matrix) -> list[dict]:
    rows = []
    for target in matrix.concurrency_levels:
        native_r = await client.connection_scaling_test(native_url, target)
        ref_r = await client.connection_scaling_test(ref_url, target)
        rows.append(
            {"target": target, "native": _asdict(native_r), "reference": _asdict(ref_r)}
        )
        print(
            f"  conn-scaling  target={target:>4}  "
            f"native={native_r.connected}/{target} ref={ref_r.connected}/{target}"
        )
    return rows


async def _sample_resources(
    url: str, sampler_pid: int, duration_s: float = 4.0
) -> dict:
    # Sample the server's RSS/CPU/threads while it's genuinely saturated
    # across cores by the multi-process load generator (not a single
    # client that can only load one core's worth).
    sampler = metrics.ResourceSampler(pid=sampler_pid, interval_s=0.1)
    sampler.start()
    conns_per_proc = max(1, _LOAD_TOTAL_CONNS // _LOAD_PROCS)
    await asyncio.to_thread(
        loadgen.run_multiprocess_throughput,
        url,
        _LOAD_PROCS,
        conns_per_proc,
        duration_s,
        4096,
        "text",
    )
    return sampler.stop()


async def _amain(args: argparse.Namespace) -> dict:
    matrix = build_matrix(quick=not args.full)

    print("== Startup latency (process spawn -> /health ready, 5 trials each) ==")
    startup_trials = 3 if not args.full else 5
    native_startup = measure_startup_latency(
        lambda: native_fixture(NATIVE_PORT), trials=startup_trials
    )
    ref_startup = measure_startup_latency(
        lambda: reference_fixture(REFERENCE_PORT), trials=startup_trials
    )
    print(
        f"  native:    median={native_startup['median_s'] * 1000:.1f}ms  (min={native_startup['min_s'] * 1000:.1f}ms max={native_startup['max_s'] * 1000:.1f}ms)"
    )
    print(
        f"  reference: median={ref_startup['median_s'] * 1000:.1f}ms  (min={ref_startup['min_s'] * 1000:.1f}ms max={ref_startup['max_s'] * 1000:.1f}ms)"
    )

    native = native_fixture(NATIVE_PORT)
    ref = reference_fixture(REFERENCE_PORT)

    print(f"\nStarting servers (native :{NATIVE_PORT}, reference :{REFERENCE_PORT})...")
    native.start()
    ref.start()
    try:
        native_url, ref_url = native.ws_url, ref.ws_url

        print("\n== Interop / correctness checks ==")
        interop_results = await _run_interop(native_url, ref_url)
        for name_key, checks in interop_results.items():
            failed = [c for c in checks if not c["passed"]]
            print(
                f"  {name_key}: {len(checks) - len(failed)}/{len(checks)} passed"
                + (f"  FAILED: {[c['name'] for c in failed]}" if failed else "")
            )

        print("\n== Throughput (fixed concurrency=8) ==")
        throughput_conc = min(
            8, matrix.concurrency_levels[0] if matrix.concurrency_levels[0] > 1 else 8
        )
        throughput_rows = await _run_throughput(
            native_url, ref_url, matrix, throughput_conc
        )

        print("\n== Throughput vs. concurrency (4096B text payload) ==")
        conc_scaling_rows = await _run_concurrency_scaling_throughput(
            native_url, ref_url, matrix
        )

        print("\n== Latency ==")
        latency_rows = await _run_latency(native_url, ref_url, matrix)

        print("\n== Connection scaling ==")
        connection_scaling_rows = await _run_connection_scaling(
            native_url, ref_url, matrix
        )

        print("\n== Resource usage under sustained load ==")
        native_res = await _sample_resources(native_url, native.pid)
        ref_res = await _sample_resources(ref_url, ref.pid)
        print(
            f"  native:    peak_rss={native_res['peak_rss_mb']:.1f}MB  threads={native_res['peak_threads']}"
        )
        print(
            f"  reference: peak_rss={ref_res['peak_rss_mb']:.1f}MB  threads={ref_res['peak_threads']}"
        )

        flamegraphs = {}
        if args.profile:
            print("\n== Flamegraph capture (py-spy, best-effort) ==")
            fg_native = metrics.capture_flamegraph(
                native.pid, 3.0, OUT_DIR / "flamegraph_native.svg"
            )
            fg_ref = metrics.capture_flamegraph(
                ref.pid, 3.0, OUT_DIR / "flamegraph_reference.svg"
            )
            flamegraphs = {
                "native": dataclasses.asdict(fg_native),
                "reference": dataclasses.asdict(fg_ref),
            }
            for name, fg in flamegraphs.items():
                print(
                    f"  {name}: {'ok -> ' + fg['path'] if fg['ok'] else 'skipped (' + fg['reason'] + ')'}"
                )

    finally:
        print("\nStopping servers...")
        native.stop()
        ref.stop()

    # ── Connection-model comparison: default (thread-per-connection) vs the
    # opt-in shared event-loop pool. Demonstrates the shared pool lifts the
    # connection ceiling AND keeps multi-core throughput at low memory.
    print(
        "\n== Connection model: shared event-loop pool (default) vs thread opt-out =="
    )
    connection_model = {}
    high_conn_target = 96
    # Distinct ports (not the just-freed main ports) to avoid a bind race with
    # sockets still in TIME_WAIT from the runs above. Both modes set
    # explicitly so the comparison is unambiguous regardless of the framework
    # default.
    for label, fixture in (
        (
            "shared",
            native_fixture(NATIVE_PORT + 11, shared_loops=min(6, os.cpu_count() or 4)),
        ),
        (
            "thread",
            native_fixture(NATIVE_PORT + 10, pool_size=24, concurrency="thread"),
        ),
    ):
        # Entirely best-effort: this comparison must never abort the report.
        try:
            fixture.start()
            try:
                url = fixture.ws_url
                scale = await client.connection_scaling_test(url, high_conn_target)
                sampler = metrics.ResourceSampler(pid=fixture.pid, interval_s=0.1)
                sampler.start()
                n_procs = min(8, high_conn_target)
                tput = await asyncio.to_thread(
                    loadgen.run_multiprocess_throughput,
                    url,
                    n_procs,
                    max(1, high_conn_target // n_procs),
                    matrix.throughput_duration_s,
                    4096,
                    "text",
                )
                res = sampler.stop()
                connection_model[label] = {
                    "target_conns": high_conn_target,
                    "connected": scale.connected,
                    "timed_out": scale.timed_out,
                    "msgs_per_sec": tput.msgs_per_sec,
                    "peak_rss_mb": res["peak_rss_mb"],
                    "peak_threads": res["peak_threads"],
                }
                print(
                    f"  {label:>7}: {scale.connected:>3}/{high_conn_target} conns  "
                    f"{tput.msgs_per_sec:>9.0f} msg/s  peak_rss={res['peak_rss_mb']:.0f}MB  threads={res['peak_threads']}"
                )
            finally:
                fixture.stop()
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"  {label:>7}: skipped ({type(e).__name__}: {e})")

    object_overhead = metrics.object_overhead_report()
    executor_overhead = await metrics.measure_executor_thread_hop_overhead()
    print(
        f"\n== Receive-path thread-hop overhead (isolated microbenchmark) ==\n"
        f"  run_in_executor round trip: {executor_overhead['thread_hop_us']:.2f}us  "
        f"(direct call: {executor_overhead['direct_call_us']:.3f}us)"
    )

    methodology = (
        "Throughput is driven by a MULTI-PROCESS load generator "
        f"({_LOAD_PROCS} client processes x {max(1, _LOAD_TOTAL_CONNS // _LOAD_PROCS)} "
        "connections each), not a single asyncio client. This is deliberate and "
        "load-bearing: one asyncio client process does the same per-message work "
        "as a single-threaded server, so it cannot saturate a multi-core server "
        "and silently caps the measured throughput at the client's ceiling. "
        "hyperdjango's native server runs the DEFAULT WebSocket model "
        "(WEBSOCKET_CONCURRENCY=shared): connections are multiplexed over a small "
        "event-loop pool (one per core) for real multi-core parallelism under "
        "free-threaded Python 3.14t with no thread-pool connection ceiling; the "
        "`websockets` reference is a single-process asyncio event loop (one core). "
        "Every connection also does an out-of-band warmup burst before the shared "
        "timed window opens, so setup/slow-start/lazy-init costs are excluded. "
        "Latency is measured single-connection, unpipelined, with its own warmup. "
        "The Connection model section explicitly compares the shared default against "
        "the WEBSOCKET_CONCURRENCY=thread opt-out. "
        f"Matrix: {'FULL' if args.full else 'QUICK'} "
        f"({len(matrix.payload_sizes)} payload sizes, "
        f"{len(matrix.concurrency_levels)} concurrency levels, "
        f"{matrix.throughput_duration_s}s/throughput sample, "
        f"{matrix.latency_samples} latency samples)."
    )

    return {
        "methodology": methodology,
        "startup_latency": {"native": native_startup, "reference": ref_startup},
        "interop": interop_results,
        "throughput": throughput_rows,
        "concurrency_scaling_throughput": conc_scaling_rows,
        "latency": latency_rows,
        "connection_scaling": connection_scaling_rows,
        "resources": {"native": native_res, "reference": ref_res},
        "connection_model": connection_model,
        "object_overhead": object_overhead,
        "executor_overhead": executor_overhead,
        "flamegraphs": flamegraphs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the full (slower) workload matrix instead of the quick smoke matrix.",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Also attempt py-spy flamegraph capture (usually needs sudo).",
    )
    parser.add_argument(
        "--out", default=str(OUT_DIR), help="Output directory for reports."
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    start = time.monotonic()
    results = asyncio.run(_amain(args))
    elapsed = time.monotonic() - start

    report.write_json(results, out_dir / "results.json")
    report.write_markdown(results, out_dir / "report.md")
    report.write_html(results, out_dir / "report.html")

    print(f"\nDone in {elapsed:.1f}s. Reports written to {out_dir}/")
    print(f"  {out_dir / 'results.json'}")
    print(f"  {out_dir / 'report.md'}")
    print(f"  {out_dir / 'report.html'}")

    any_interop_failure = any(
        not c["passed"]
        for c in results["interop"]["native"] + results["interop"]["reference"]
    )
    return 1 if any_interop_failure else 0


if __name__ == "__main__":
    sys.exit(main())
