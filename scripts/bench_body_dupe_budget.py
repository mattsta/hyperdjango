#!/usr/bin/env python3
"""Per-request CPU budget of the native server at the large-body payloads.

WHY
---
`zig/src/server.zig:callPythonHandler` transfers ownership of the Python
response body into Zig with `allocator.dupe(u8, body_slice)`, freed in
`PythonResponse.deinit` after the (GIL-released) send. The open question was
whether replacing that malloc/free pair with a per-thread retained buffer is
worth anything. `zig/bench/bench_body_alloc.zig` measures the COMPONENT (what
the malloc/free pair costs). This script measures the DENOMINATOR (what a whole
request costs), because a percentage needs both.

Measuring the denominator as CPU-seconds per request — not as wall time, not as
rps — is the point. rps at the 64 KiB cell carries a ±4 percentage-point
run-to-run noise floor (load generator turnaround, NUMA placement, connection
service fraction), which is why the end-to-end A/B could never settle this.
utime+stime of the server process divided by requests served is a RATIO of two
directly counted quantities: it does not care how many connections the client
kept in service, and it is stable to well under a percent across repetitions.

WHAT IT REPORTS
---------------
For each payload/route: server CPU microseconds per request, and the share of
that budget a given component cost (--component-ns, default = the measured
64 KiB body dupe delta) would represent. That share is the ceiling on any
end-to-end win from removing the component, which is the number the decision
actually turns on.

Run (on the bench box):
    uv run python scripts/bench_body_dupe_budget.py \
        --server-cores 0-63 --client-cores 64-127 --numa \
        --workers 16 --concurrency 1024 --duration 10 --reps 3
Writes logs/bench_body_dupe_budget_<label>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.http.fixtures import ServerFixture
from benchmarks.http.loadgen import run_load_wrk

CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


@dataclass(slots=True, frozen=True)
class CpuSample:
    """utime+stime of a process, in seconds, summed over ALL its threads.

    /proc/<pid>/stat fields 14 (utime) and 15 (stime) are process-wide
    aggregates, so one read covers every worker thread the native server runs.
    """

    utime_s: float
    stime_s: float

    @property
    def total_s(self) -> float:
        return self.utime_s + self.stime_s


def read_cpu(pid: int) -> CpuSample:
    raw = Path(f"/proc/{pid}/stat").read_text()
    # comm may contain spaces/parens; everything after the last ')' is stable.
    tail = raw[raw.rindex(")") + 2 :].split()
    # tail[0] is field 3 (state), so field 14 (utime) is tail[11].
    return CpuSample(
        utime_s=int(tail[11]) / CLOCK_TICKS, stime_s=int(tail[12]) / CLOCK_TICKS
    )


@dataclass(slots=True, frozen=True)
class CellResult:
    route: str
    payload_bytes: int
    rps: float
    requests: int
    cpu_s: float
    wall_s: float
    cpu_us_per_req: float
    cpu_cores_busy: float
    served_frac: float
    non2xx: int


@dataclass(slots=True, frozen=True)
class CellSummary:
    route: str
    payload_bytes: int
    reps: int
    cpu_us_per_req_median: float
    cpu_us_per_req_min: float
    cpu_us_per_req_max: float
    cpu_spread_pct: float
    rps_median: float
    rps_min: float
    rps_max: float
    rps_spread_pct: float
    component_share_pct: float


def _spread(vals: list[float]) -> float:
    med = statistics.median(vals)
    return ((max(vals) - min(vals)) / med * 100.0) if med > 0 else 0.0


def measure(
    fixture: ServerFixture,
    route: str,
    payload_bytes: int,
    concurrency: int,
    duration: float,
    client_cores: str | None,
    numa: bool,
) -> CellResult:
    pid = fixture.proc.pid
    before = read_cpu(pid)
    t0 = time.monotonic()
    res = run_load_wrk(
        fixture.host,
        fixture.port,
        route,
        concurrency,
        duration_s=duration,
        warmup_s=0.0,
        cpu_cores=client_cores,
        numa=numa,
    )
    wall = time.monotonic() - t0
    after = read_cpu(pid)
    cpu_s = after.total_s - before.total_s
    reqs = max(res["requests"] - res["non2xx"], 1)
    return CellResult(
        route=route,
        payload_bytes=payload_bytes,
        rps=res["throughput_rps"],
        requests=reqs,
        cpu_s=cpu_s,
        wall_s=wall,
        cpu_us_per_req=cpu_s / reqs * 1e6,
        cpu_cores_busy=cpu_s / wall if wall > 0 else 0.0,
        served_frac=res["served_frac"],
        non2xx=res["non2xx"],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--framework", default="hyperdjango-reactor")
    ap.add_argument("--server-cores", default=None)
    ap.add_argument("--client-cores", default=None)
    ap.add_argument("--numa", action="store_true")
    ap.add_argument("--concurrency", type=int, default=1024)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument(
        "--payloads",
        default="65536,262144",
        help="comma-separated body sizes for /json and /jsoncached",
    )
    ap.add_argument(
        "--component-ns",
        type=float,
        default=26.7,
        help="measured cost of the component under evaluation, ns/request "
        "(default: the 64 KiB body-dupe malloc/free delta from "
        "zig/bench/bench_body_alloc.zig at T=16 on the bench box)",
    )
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    if not Path("/proc/self/stat").exists():
        print("this instrument needs /proc (Linux) — run it on the bench box")
        return 2

    sizes = [int(s) for s in args.payloads.split(",") if s.strip()]
    routes = [(f"/json?n={n}", n) for n in sizes] + [
        (f"/jsoncached?n={n}", n) for n in sizes
    ]
    routes.append(("/plaintext", 0))

    cells: list[CellResult] = []
    with ServerFixture(
        args.framework,
        args.host,
        args.port,
        args.workers,
        cpu_cores=args.server_cores,
        numa=args.numa,
    ) as fx:
        # One untimed pass so the JIT-free-but-still-warming paths (prepared
        # buffers, page tables, connection pool) are steady before any sample.
        run_load_wrk(
            fx.host,
            fx.port,
            routes[0][0],
            args.concurrency,
            duration_s=3,
            warmup_s=0.0,
            cpu_cores=args.client_cores,
            numa=args.numa,
        )
        for route, n in routes:
            for rep in range(args.reps):
                c = measure(
                    fx,
                    route,
                    n,
                    args.concurrency,
                    args.duration,
                    args.client_cores,
                    args.numa,
                )
                cells.append(c)
                print(
                    f"{route:<24} rep{rep} rps={c.rps:>10.0f} "
                    f"cpu={c.cpu_us_per_req:>8.1f} us/req "
                    f"cores_busy={c.cpu_cores_busy:>5.1f} "
                    f"served_frac={c.served_frac:.2f} non2xx={c.non2xx}"
                )

    summaries: list[CellSummary] = []
    for route, n in routes:
        group = [c for c in cells if c.route == route]
        if not group:
            continue
        cpus = [c.cpu_us_per_req for c in group]
        rpss = [c.rps for c in group]
        med_cpu = statistics.median(cpus)
        summaries.append(
            CellSummary(
                route=route,
                payload_bytes=n,
                reps=len(group),
                cpu_us_per_req_median=med_cpu,
                cpu_us_per_req_min=min(cpus),
                cpu_us_per_req_max=max(cpus),
                cpu_spread_pct=_spread(cpus),
                rps_median=statistics.median(rpss),
                rps_min=min(rpss),
                rps_max=max(rpss),
                rps_spread_pct=_spread(rpss),
                component_share_pct=(args.component_ns / 1000.0) / med_cpu * 100.0,
            )
        )

    print("\n== per-request CPU budget ==")
    print(
        f"{'route':<24}{'cpu us/req':>12}{'spread':>9}{'rps':>12}{'rps spread':>12}"
        f"{'component share':>18}"
    )
    for s in summaries:
        print(
            f"{s.route:<24}{s.cpu_us_per_req_median:>12.1f}{s.cpu_spread_pct:>8.2f}%"
            f"{s.rps_median:>12.0f}{s.rps_spread_pct:>11.2f}%"
            f"{s.component_share_pct:>17.4f}%"
        )
    print(
        f"\ncomponent under evaluation: {args.component_ns:.1f} ns/request "
        f"(body dupe malloc/free)"
    )

    out = Path("logs") / f"bench_body_dupe_budget_{args.label}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "args": vars(args),
                "cells": [asdict(c) for c in cells],
                "summaries": [asdict(s) for s in summaries],
            },
            indent=1,
        )
    )
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
