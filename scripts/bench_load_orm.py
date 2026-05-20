"""
E2E production load test: full HTTP → ORM QuerySet → pg.zig → response.

Self-contained benchmark system:
  1. Seeds database (hyper setup --drop --seed)
  2. Starts server via AppRunner (rate limiting disabled)
  3. Runs wrk (compiled C load generator) against each endpoint
  4. Parses wrk output into structured results
  5. Reports formatted table with req/s, latency percentiles
  6. Saves JSON baseline for regression comparison
  7. Falls back to Python threads if wrk not installed

Endpoints tested (full ORM stack through Zig HTTP → pg.zig):
  - /health                    → no DB (raw Zig HTTP baseline)
  - /api/v1/books/             → ORM filter + order + paginate
  - /api/v1/books/1            → select_related + aggregate
  - /api/v1/books/stats        → COUNT + FILTER aggregate
  - /api/v1/reviews/           → CursorPagination (HMAC cursors)
  - /api/v1/books/?search=...  → FullTextSearchFilter

Run standalone:  uv run python scripts/test_e2e_load_orm.py
Run via suite:   uv run hyper-test load_orm

# hyper-test: e2e
"""

import http.client
import json
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from e2e_helper import TEST_PORTS, AppRunner, Session, http_get

PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
HAS_WRK = shutil.which("wrk") is not None
PASS = 0
FAIL = 0

# wrk config — tuned per mode
WRK_THREADS = 8 if PARALLEL else 16
WRK_CONNECTIONS = 50 if PARALLEL else 200
WRK_DURATION = "3s" if PARALLEL else "10s"

# Python fallback config (when wrk not available)
PY_THREADS = 4 if PARALLEL else 8
PY_RPT = 50 if PARALLEL else 500

BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "logs" / "load_orm_baseline.json"
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BenchResult:
    name: str
    url: str
    requests: int = 0
    errors: int = 0
    rps: float = 0.0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    duration_s: float = 0.0
    tool: str = "wrk"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
    return condition


def _parse_wrk_output(output: str, name: str, url: str) -> BenchResult:
    """Parse wrk text output into a BenchResult."""
    r = BenchResult(name=name, url=url, tool="wrk")

    # Requests/sec: 52474.09
    m = re.search(r"Requests/sec:\s+([\d.]+)", output)
    if m:
        r.rps = float(m.group(1))

    # Latency   458.19us  773.15us  37.31ms   90.34%
    m = re.search(r"Latency\s+([\d.]+)(us|ms|s)", output)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        r.avg_ms = val * 0.001 if unit == "us" else val * 1000 if unit == "s" else val

    # Max latency
    m = re.search(
        r"Latency\s+[\d.]+(?:us|ms|s)\s+[\d.]+(?:us|ms|s)\s+([\d.]+)(us|ms|s)", output
    )
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        r.max_ms = val * 0.001 if unit == "us" else val * 1000 if unit == "s" else val

    # NNN requests in Xs
    m = re.search(r"(\d+) requests in ([\d.]+)s", output)
    if m:
        r.requests = int(m.group(1))
        r.duration_s = float(m.group(2))

    # Socket errors
    m = re.search(r"Socket errors:.*read (\d+)", output)
    if m:
        r.errors = int(m.group(1))

    # Non-2xx (if present)
    m = re.search(r"Non-2xx or 3xx responses:\s+(\d+)", output)
    if m:
        r.errors += int(m.group(1))

    return r


def run_wrk(name: str, url: str) -> BenchResult:
    """Run wrk benchmark and return parsed result."""
    result = subprocess.run(
        ["wrk", f"-t{WRK_THREADS}", f"-c{WRK_CONNECTIONS}", f"-d{WRK_DURATION}", url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    r = _parse_wrk_output(output, name, url)

    # Print raw wrk output indented
    for line in output.strip().split("\n"):
        if line.strip():
            print(f"    {line.strip()}")

    return r


# ---------------------------------------------------------------------------
# Python fallback (when wrk not installed)
# ---------------------------------------------------------------------------


def _py_worker(url, count, results, errors):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        for _ in range(count):
            start = time.perf_counter()
            try:
                conn.request("GET", path, headers={"Connection": "keep-alive"})
                resp = conn.getresponse()
                resp.read()
                elapsed = (time.perf_counter() - start) * 1000
                if resp.status == 200:
                    results.append(elapsed)
                else:
                    errors.append(resp.status)
            except Exception:
                errors.append(0)
                conn = http.client.HTTPConnection(
                    parsed.hostname, parsed.port, timeout=10
                )
    finally:
        conn.close()


def run_python(name: str, url: str) -> BenchResult:
    """Python-threaded fallback benchmark."""
    results: list[float] = []
    errors: list[int] = []
    total = PY_THREADS * PY_RPT

    workers = []
    start = time.perf_counter()
    for _ in range(PY_THREADS):
        t = threading.Thread(target=_py_worker, args=(url, PY_RPT, results, errors))
        workers.append(t)
        t.start()
    for t in workers:
        t.join()
    wall = time.perf_counter() - start

    r = BenchResult(name=name, url=url, tool="python")
    r.requests = len(results)
    r.errors = len(errors)
    r.duration_s = wall
    r.rps = len(results) / wall if wall > 0 else 0

    if results:
        results.sort()
        r.avg_ms = statistics.mean(results)
        r.p50_ms = results[len(results) // 2]
        r.p95_ms = results[int(len(results) * 0.95)]
        r.p99_ms = results[int(len(results) * 0.99)]
        r.max_ms = results[-1]

    print(
        f"    {r.requests}/{total} ok, {r.errors} errors | "
        f"{r.rps:.0f} req/s | avg {r.avg_ms:.1f}ms | "
        f"p50 {r.p50_ms:.1f}ms | p99 {r.p99_ms:.1f}ms | wall {wall:.2f}s"
    )

    return r


def run_bench(name: str, url: str) -> BenchResult:
    """Run benchmark using wrk (preferred) or Python fallback."""
    if HAS_WRK:
        return run_wrk(name, url)
    return run_python(name, url)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    tool = "wrk" if HAS_WRK else "python (fallback)"
    mode = "PARALLEL (reduced)" if PARALLEL else "STANDALONE"
    conns = (
        f"{WRK_THREADS}t/{WRK_CONNECTIONS}c/{WRK_DURATION}"
        if HAS_WRK
        else f"{PY_THREADS}t×{PY_RPT}req"
    )

    print("=" * 70)
    print("  E2E Production Load Test: Full ORM Stack")
    print("  Zig HTTP → Python → ORM QuerySet (compiled cache) → pg.zig")
    print(f"  Tool: {tool} | Mode: {mode} | Config: {conns}")
    print("=" * 70)

    port = TEST_PORTS["load_orm"]

    # 1. Setup + seed
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

    # 2. Start server (rate limiting disabled)
    with AppRunner(
        "services.bookstore_api.app:app",
        host="127.0.0.1",
        port=port,
        env={"HYPER_LOAD_TEST": "1"},
    ) as runner:
        base = runner.url()

        # 3. Auth check
        print("\n── Auth ──")
        s = Session(base)
        r = s.post(
            "/auth/register",
            body={
                "username": f"loadtest_{int(time.time())}",
                "password": "loadtest_pass_123",
            },
        )
        ok("register returns 201", r.status == 201, f"got {r.status}: {r.body[:200]}")
        ok("register sets session cookie", "sessionid" in s.cookie_jar)

        # 4. Warmup
        print("\n── Warmup ──")
        for _ in range(20):
            http_get(f"{base}/api/v1/books/")
        print("    20 requests sent (cache + pool primed)")

        # 5. Benchmarks
        results: list[BenchResult] = []

        endpoints = [
            ("Health (no DB)", f"{base}/health"),
            ("List (filter+page)", f"{base}/api/v1/books/"),
            ("Detail (join+agg)", f"{base}/api/v1/books/1"),
            ("Stats (aggregate)", f"{base}/api/v1/books/stats"),
            ("Reviews (cursor)", f"{base}/api/v1/reviews/"),
            ("Search (FTS)", f"{base}/api/v1/books/?search=python"),
        ]

        for name, url in endpoints:
            print(f"\n── {name} ──")
            bench_result = run_bench(name, url)
            results.append(bench_result)
            error_pct = (bench_result.errors / max(bench_result.requests, 1)) * 100
            ok(f"{name} < 1% errors", error_pct < 1, f"{error_pct:.1f}% errors")

        # 6. Latency assertions (non-parallel only)
        if not PARALLEL:
            for r in results[1:]:  # skip health
                ok(f"{r.name} avg < 50ms", r.avg_ms < 50, f"avg={r.avg_ms:.1f}ms")

        # 7. Report table
        print("\n" + "=" * 70)
        print(
            f"  {'Endpoint':<25} {'req/s':>8} {'avg':>8} {'max':>8} {'reqs':>8} {'errs':>6}"
        )
        print("  " + "-" * 62)
        for r in results:
            print(
                f"  {r.name:<25} {r.rps:>7,.0f}  {r.avg_ms:>6.1f}ms {r.max_ms:>6.1f}ms {r.requests:>7,} {r.errors:>5}"
            )
        print("=" * 70)

        # 8. Save baseline JSON
        baseline = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "config": conns,
            "results": [
                {
                    "name": r.name,
                    "rps": round(r.rps, 1),
                    "avg_ms": round(r.avg_ms, 2),
                    "max_ms": round(r.max_ms, 2),
                    "requests": r.requests,
                    "errors": r.errors,
                }
                for r in results
            ],
        }
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2))
        print(f"\n  Baseline saved: {BASELINE_PATH}")

    # Final
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
