"""
Phase B alternative of task #156: in-process cProfile of the List endpoint.

Avoids py-spy/sudo requirement. Uses TestClient to invoke the bookstore
API list endpoint N times under cProfile, then analyzes pstats to find
where Python time is actually spent.

Outputs:
  logs/profile_list_cprofile.prof   — pstats binary dump (load with pstats.Stats)
  logs/profile_list_cprofile.txt    — human-readable top-30 by cumulative and self time
  logs/profile_list_cprofile.json   — structured top-30 for parsing

Run: uv run python scripts/profile_list_cprofile.py
"""

import asyncio
import cProfile
import io
import json
import os
import pstats
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

LOGS = Path(__file__).resolve().parent.parent / "logs"

# Stability rule (matches profile_hypernews_cprofile.py): each run ≥5s.
# At post-#174 ~1500 rps the bookstore List endpoint needs ≥7500 iters
# to hit 5s. Use 10000 for cushion.
ITERATIONS = 10000  # Requests per run (~6-10s of profiling time)
WARMUP = 500  # Warmup requests — prime LRU caches, prepared stmts, pool
MULTI_RUN = 3  # Number of runs; median across runs smooths jitter


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    print("=== Phase B (cProfile): List endpoint in-process ===")
    print(f"  Iterations: {ITERATIONS} (warmup: {WARMUP})")

    # Setup DB via subprocess — cleaner than importing everything early
    print("Setting up database...")
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.bookstore_api.app:app",
            "--drop",
            "--seed",
            "services.bookstore_api.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    # Now import the app and TestClient
    os.environ["HYPER_LOAD_TEST"] = "1"

    # Reset the query cache and test config BEFORE importing the app module
    from hyperdjango.database import get_db
    from hyperdjango.testing import TestClient
    from services.bookstore_api.app import app

    # Connect the DB (normally done by app.run())
    db = get_db()
    if db._pool_handle is None:
        asyncio.run(db.connect())

    client = TestClient(app)

    # Warmup — do not profile
    print(f"Warming up ({WARMUP} requests)...")
    for _ in range(WARMUP):
        r = client.get("/api/v1/books/")
        if r.status != 200:
            print(f"  warmup: got status {r.status}: {r.text[:200]}")
            sys.exit(1)

    # Profile: MULTI_RUN runs of ITERATIONS each, accumulating into one
    # profiler. Report the median wall time across runs so CPU jitter
    # and run-to-run noise are visible and filtered.
    print(
        f"Profiling {ITERATIONS} × {MULTI_RUN} = {ITERATIONS * MULTI_RUN} requests..."
    )
    profiler = cProfile.Profile()
    run_times: list[float] = []
    for run_idx in range(MULTI_RUN):
        start = time.perf_counter()
        profiler.enable()
        for _ in range(ITERATIONS):
            client.get("/api/v1/books/")
        profiler.disable()
        run_times.append(time.perf_counter() - start)

    run_times_sorted = sorted(run_times)
    elapsed = run_times_sorted[len(run_times_sorted) // 2]  # median
    rps = ITERATIONS / elapsed
    avg_ms = (elapsed / ITERATIONS) * 1000

    # Jitter visibility
    per_run_rps = [ITERATIONS / t for t in run_times]
    jitter_pct = (max(per_run_rps) - min(per_run_rps)) / rps * 100 / 2

    print(f"\n  Median elapsed: {elapsed:.2f}s (per run)")
    print(f"  Per-run rps: {[f'{r:.0f}' for r in per_run_rps]}")
    print(f"  Median rps: {rps:.0f} ± {jitter_pct:.1f}%")
    print(f"  Avg latency: {avg_ms:.2f}ms")

    # Save profile
    prof_path = LOGS / "profile_list_cprofile.prof"
    profiler.dump_stats(str(prof_path))
    print(f"\n  Raw profile: {prof_path}")

    # Generate text report
    stats_buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=stats_buf)
    stats.strip_dirs()

    report_lines: list[str] = []
    report_lines.append("# cProfile Report — List Endpoint (in-process TestClient)")
    report_lines.append(f"Iterations: {ITERATIONS}")
    report_lines.append(f"Elapsed: {elapsed:.2f}s")
    report_lines.append(f"In-process rps: {rps:.0f}")
    report_lines.append(f"Avg latency: {avg_ms:.2f}ms per request")
    report_lines.append("")
    report_lines.append("=" * 80)
    report_lines.append("Top 30 by CUMULATIVE time")
    report_lines.append("=" * 80)

    stats.sort_stats("cumulative")
    stats.print_stats(30)
    report_lines.append(stats_buf.getvalue())

    stats_buf2 = io.StringIO()
    stats2 = pstats.Stats(profiler, stream=stats_buf2)
    stats2.strip_dirs()
    stats2.sort_stats("tottime")
    report_lines.append("=" * 80)
    report_lines.append("Top 30 by SELF (TOTAL) time")
    report_lines.append("=" * 80)
    stats2.print_stats(30)
    report_lines.append(stats_buf2.getvalue())

    # Dump text report
    report_path = LOGS / "profile_list_cprofile.txt"
    report_path.write_text("\n".join(report_lines))
    print(f"  Text report: {report_path}")

    # Extract structured top-30 by tottime for JSON output
    top_entries: list[dict[str, float | str | int]] = []
    # stats.stats is a dict of {(file, line, name): (cc, nc, tt, ct, callers)}
    ranked = sorted(
        stats.stats.items(),
        key=lambda kv: kv[1][2],  # tottime
        reverse=True,
    )[:30]
    total_tt = sum(v[2] for v in stats.stats.values())
    for (fname, lineno, func), (cc, nc, tt, ct, _) in ranked:
        top_entries.append(
            {
                "function": f"{fname}:{lineno}:{func}",
                "call_count": cc,
                "tottime_s": round(tt, 4),
                "cumtime_s": round(ct, 4),
                "pct_of_total": round(tt / total_tt * 100, 2) if total_tt else 0,
            }
        )

    json_path = LOGS / "profile_list_cprofile.json"
    json_path.write_text(
        json.dumps(
            {
                "iterations": ITERATIONS,
                "elapsed_s": round(elapsed, 3),
                "rps": round(rps, 1),
                "avg_ms": round(avg_ms, 3),
                "total_tottime_s": round(total_tt, 3),
                "top_30_by_tottime": top_entries,
            },
            indent=2,
        )
    )
    print(f"  JSON report: {json_path}")

    # Print top 15 to stdout
    print("\n=== Top 15 by SELF time ===")
    print(f"  {'tottime(s)':>10} {'cumtime(s)':>10} {'calls':>8}  function")
    for entry in top_entries[:15]:
        print(
            f"  {entry['tottime_s']:>10.3f} {entry['cumtime_s']:>10.3f} "
            f"{entry['call_count']:>8}  {entry['function']}"
        )

    print("\n=== Phase B complete ===")


if __name__ == "__main__":
    main()
