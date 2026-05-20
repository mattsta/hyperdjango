#!/usr/bin/env python3
"""Controlled A/B benchmark for the #37 request hot-path redesign.

Compares two commits by ALTERNATING build+bench rounds (so machine-load drift
cancels), building each ReleaseFast (production), and running the native HTTP
throughput bench. Reports median throughput_rps + latency per ref and the delta.

    BASE = the commit BEFORE #37 (parent)      -> b6d32ab
    OPT  = the #37 commit                       -> f4229cd

Isolated: BASE/OPT differ ONLY by the hot-path change, so this measures #37's
effect specifically. Restores the working branch + a ReleaseSafe build at the end.

Usage: uv run python scripts/_ab_bench_hotpath.py [--rounds 2] [--duration 5]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
BASE = "b6d32ab"
OPT = "f4229cd"
RESTORE_BRANCH = "mesh-u3-db-listen-dsn-fix"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, **kw)


def checkout(ref: str) -> None:
    r = run(["git", "checkout", "--quiet", ref])
    if r.returncode != 0:
        raise RuntimeError(f"git checkout {ref} failed: {r.stderr}")


def build(mode: str) -> None:
    flag = "--release" if mode == "fast" else "--safe"
    r = run(["uv", "run", "python", "zig/build_hyperdjango.py", flag, "--install"])
    if r.returncode != 0:
        raise RuntimeError(
            f"build {mode} failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
        )


def bench(outdir: Path, duration: float) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    r = run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "benchmarks.http.run",
            "--frameworks",
            "hyperdjango-threaded",
            "--mode",
            "concurrency",
            "--quick",
            "--duration",
            str(duration),
            "--warmup",
            "1",
            "--outdir",
            str(outdir),
        ]
    )
    rj = outdir / "results.json"
    if not rj.exists():
        raise RuntimeError(
            f"bench produced no results.json:\n{r.stdout[-2000:]}\n{r.stderr[-1500:]}"
        )
    data = json.loads(rj.read_text())
    # results.json is {"meta": ..., "results": [cells]} (or a bare list in older runs).
    return data["results"] if isinstance(data, dict) else data


def summarize(cells: list[dict]) -> dict:
    rps = [c["throughput_rps"] for c in cells if c.get("throughput_rps")]
    p50 = [c["p50_ms"] for c in cells if c.get("p50_ms")]
    p99 = [c["p99_ms"] for c in cells if c.get("p99_ms")]
    return {
        "peak_rps": max(rps) if rps else 0.0,
        "median_rps": median(rps) if rps else 0.0,
        "median_p50_ms": median(p50) if p50 else 0.0,
        "median_p99_ms": median(p99) if p99 else 0.0,
        "cells": len(cells),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--duration", type=float, default=5.0)
    args = ap.parse_args()

    tmp = ROOT / "benchmarks" / "http" / "out" / "ab37"
    samples: dict[str, list[dict]] = {"BASE": [], "OPT": []}
    order = []
    for i in range(args.rounds):
        order += [("OPT", OPT), ("BASE", BASE)]  # alternate to cancel drift

    try:
        for idx, (label, ref) in enumerate(order):
            print(
                f"[{idx + 1}/{len(order)}] {label} ({ref}): checkout+build+bench",
                flush=True,
            )
            checkout(ref)
            build("fast")
            cells = bench(tmp / f"{label}_{idx}", args.duration)
            s = summarize(cells)
            samples[label].append(s)
            print(
                f"    peak={s['peak_rps']:,.0f} rps  median={s['median_rps']:,.0f} rps  "
                f"p50={s['median_p50_ms']:.2f}ms p99={s['median_p99_ms']:.2f}ms",
                flush=True,
            )
    finally:
        print(f"\nRestoring {RESTORE_BRANCH} + ReleaseSafe build ...", flush=True)
        checkout(RESTORE_BRANCH)
        build("safe")

    def agg(label: str, key: str) -> float:
        vals = [s[key] for s in samples[label]]
        return median(vals) if vals else 0.0

    base_peak, opt_peak = agg("BASE", "peak_rps"), agg("OPT", "peak_rps")
    base_p99, opt_p99 = agg("BASE", "median_p99_ms"), agg("OPT", "median_p99_ms")
    print("\n================ A/B RESULT (#37 hot-path) ================")
    print(
        f"BASE {BASE}: peak {base_peak:,.0f} rps, p99 {base_p99:.2f} ms  (n={len(samples['BASE'])})"
    )
    print(
        f"OPT  {OPT}: peak {opt_peak:,.0f} rps, p99 {opt_p99:.2f} ms  (n={len(samples['OPT'])})"
    )
    if base_peak:
        print(f"Δ throughput: {(opt_peak - base_peak) / base_peak * 100:+.1f}%")
    if base_p99:
        print(
            f"Δ p99 latency: {(opt_p99 - base_p99) / base_p99 * 100:+.1f}% (negative = faster)"
        )
    print("===========================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
