"""
Production benchmark profiler — wraps bench_load_orm with profiling tools.

NOT for CI — manual profiling after major milestones.

Usage:
    # Profile with py-spy (Python-level flame graph)
    uv run python scripts/bench_profile.py --tool py-spy

    # Profile with per-endpoint timing (X-Profile headers)
    uv run python scripts/bench_profile.py --tool headers

    # Just run the benchmark with detailed output
    uv run python scripts/bench_profile.py

    # Custom wrk duration
    uv run python scripts/bench_profile.py --duration 30

Output:
    logs/profile_*.svg         — py-spy flame graph
    logs/profile_report.txt    — per-endpoint timing breakdown
    logs/load_orm_baseline.json — standard benchmark baseline
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from e2e_helper import TEST_PORTS, AppRunner, http_get

LOGS = Path(__file__).resolve().parent.parent / "logs"
PORT = TEST_PORTS["load_orm"]
HOST = "127.0.0.1"


def _find_server_pid(port: int) -> int | None:
    """Find the PID of the server listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = result.stdout.strip().split("\n")
        if pids and pids[0]:
            return int(pids[0])
    except subprocess.SubprocessError, ValueError:
        pass
    return None


def _run_wrk(url: str, duration: int, threads: int = 16, connections: int = 200) -> str:
    """Run wrk and return raw output."""
    result = subprocess.run(
        ["wrk", f"-t{threads}", f"-c{connections}", f"-d{duration}s", url],
        capture_output=True,
        text=True,
        timeout=duration + 30,
    )
    return result.stdout + result.stderr


def profile_with_pyspy(base: str, duration: int):
    """Attach py-spy to the server process and generate flame graph during load."""
    pid = _find_server_pid(PORT)
    if pid is None:
        print("  ERROR: Could not find server PID")
        return

    print(f"  Server PID: {pid}")
    svg_path = LOGS / f"profile_pyspy_{int(time.time())}.svg"
    speedscope_path = LOGS / f"profile_speedscope_{int(time.time())}.json"

    if not shutil.which("py-spy"):
        print("  ERROR: py-spy not installed. Run: uv pip install py-spy")
        return

    # Start py-spy recording in background
    print(f"  Starting py-spy record for {duration}s...")
    pyspy_proc = subprocess.Popen(
        [
            "py-spy",
            "record",
            "--pid",
            str(pid),
            "--output",
            str(svg_path),
            "--format",
            "speedscope",
            "--duration",
            str(duration),
            "--rate",
            "100",
            "--subprocesses",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Run wrk load concurrently
    print(f"  Running wrk load ({duration}s)...")
    endpoints = [
        f"{base}/health",
        f"{base}/api/v1/books/",
        f"{base}/api/v1/books/1",
        f"{base}/api/v1/books/stats",
    ]

    for name, url in [
        ("health", endpoints[0]),
        ("list", endpoints[1]),
        ("detail", endpoints[2]),
        ("stats", endpoints[3]),
    ]:
        print(f"\n  --- {name}: {url} ---")
        output = _run_wrk(url, max(duration // 4, 3))
        for line in output.strip().split("\n"):
            if line.strip():
                print(f"    {line.strip()}")

    # Wait for py-spy to finish
    pyspy_proc.wait(timeout=duration + 10)
    stderr = pyspy_proc.stderr.read().decode()
    if pyspy_proc.returncode == 0:
        print(f"\n  Flame graph saved: {svg_path}")
    else:
        print(f"  py-spy error (exit {pyspy_proc.returncode}): {stderr[:500]}")


def profile_with_headers(base: str, duration: int):
    """Hit each endpoint and report X-Query-Count / X-Query-Time headers."""
    print(
        f"\n  Per-endpoint profiling via response headers ({duration}s load first)..."
    )

    # Warmup
    for _ in range(20):
        http_get(f"{base}/api/v1/books/")

    endpoints = [
        ("Health (no DB)", "/health"),
        ("List (filter+page)", "/api/v1/books/"),
        ("Detail (join+agg)", "/api/v1/books/1"),
        ("Stats (aggregate)", "/api/v1/books/stats"),
        ("Reviews (cursor)", "/api/v1/reviews/"),
        ("Search (FTS)", "/api/v1/books/?search=python"),
        ("Enriched (DataLoader)", "/api/v1/books/enriched"),
        ("Cached Stats (XFetch)", "/api/v1/books/cached-stats"),
        ("Two-Tier Stats", "/api/v1/books/two-tier-stats"),
    ]

    # Run wrk to generate load and populate perf stats
    if shutil.which("wrk"):
        for name, path in endpoints[:4]:
            _run_wrk(f"{base}{path}", max(duration // 4, 2))

    report_lines = []
    report_lines.append(
        f"{'Endpoint':<30} {'Status':>6} {'X-Query-Count':>14} {'X-Query-Time':>13} {'X-Timing':>10}"
    )
    report_lines.append("-" * 78)

    for name, path in endpoints:
        r = http_get(f"{base}{path}")
        headers = {k.lower(): v for k, v in r.headers.items()}
        qc = headers.get("x-query-count", "-")
        qt = headers.get("x-query-time", "-")
        timing = headers.get("x-timing", "-")
        report_lines.append(
            f"  {name:<28} {r.status:>6} {qc:>14} {qt:>13} {timing:>10}"
        )

    # Print report
    print("\n  " + "=" * 78)
    print("  Response Header Profiling")
    print("  " + "=" * 78)
    for line in report_lines:
        print(f"  {line}")
    print("  " + "=" * 78)

    # Check performance dashboard
    r = http_get(f"{base}/debug/performance/json")
    if r.status == 200:
        stats = r.json
        print("\n  Performance Dashboard Stats:")
        print(f"    Total requests: {stats.get('total_requests', 0)}")
        print(f"    Total queries: {stats.get('total_queries', 0)}")
        print(f"    Avg queries/req: {stats.get('avg_queries_per_request', 0)}")
        print(f"    Slow queries: {stats.get('slow_query_count', 0)}")
        print(f"    N+1 patterns: {stats.get('n_plus_one_count', 0)}")
        if stats.get("recent_slow_queries"):
            print("    Recent slow queries:")
            for q in stats["recent_slow_queries"][:5]:
                print(f"      {q['duration_ms']}ms: {q['sql'][:80]}")

    # Save report
    report_path = LOGS / "profile_report.txt"
    report_path.write_text("\n".join(report_lines))
    print(f"\n  Report saved: {report_path}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark profiler")
    parser.add_argument(
        "--tool",
        choices=["py-spy", "headers", "both"],
        default="headers",
        help="Profiling tool (default: headers)",
    )
    parser.add_argument("--duration", type=int, default=10, help="Duration in seconds")
    args = parser.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  HyperDjango Benchmark Profiler")
    print(f"  Tool: {args.tool} | Duration: {args.duration}s")
    print("=" * 70)

    # Setup
    print("\n  Setting up database...")
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

    print("  Starting server...")
    with AppRunner(
        "services.bookstore_api.app:app",
        host=HOST,
        port=PORT,
        env={"HYPER_LOAD_TEST": "1"},
    ) as runner:
        base = runner.url()
        print(f"  Server at {base}")

        # Warmup
        print("  Warming up (20 requests)...")
        for _ in range(20):
            http_get(f"{base}/api/v1/books/")

        if args.tool in ("py-spy", "both"):
            print("\n── py-spy Profiling ──")
            profile_with_pyspy(base, args.duration)

        if args.tool in ("headers", "both"):
            print("\n── Header Profiling ──")
            profile_with_headers(base, args.duration)

        # Also run the standard benchmark
        if shutil.which("wrk"):
            print("\n── Standard Benchmark ──")
            endpoints = [
                ("Health (no DB)", f"{base}/health"),
                ("List (filter+page)", f"{base}/api/v1/books/"),
                ("Detail (join+agg)", f"{base}/api/v1/books/1"),
                ("Stats (aggregate)", f"{base}/api/v1/books/stats"),
            ]
            print(f"\n  {'Endpoint':<25} {'req/s':>10} {'avg_ms':>10}")
            print("  " + "-" * 48)
            for name, url in endpoints:
                output = _run_wrk(url, args.duration)
                import re

                rps_m = re.search(r"Requests/sec:\s+([\d.]+)", output)
                lat_m = re.search(r"Latency\s+([\d.]+)(us|ms|s)", output)
                rps = float(rps_m.group(1)) if rps_m else 0
                if lat_m:
                    val = float(lat_m.group(1))
                    unit = lat_m.group(2)
                    avg = (
                        val * 0.001
                        if unit == "us"
                        else val * 1000
                        if unit == "s"
                        else val
                    )
                else:
                    avg = 0
                print(f"  {name:<25} {rps:>9,.0f}  {avg:>8.2f}ms")

    print("\n" + "=" * 70)
    print("  Profiling complete. Check logs/ for results.")
    print("=" * 70)


if __name__ == "__main__":
    main()
