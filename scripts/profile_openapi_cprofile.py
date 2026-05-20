"""
Task #185: cProfile the OpenAPI schema generation path.

Uses the bookstore_api app (bookstore_api has the largest OpenAPI
surface area of the services — ~90 routes, ModelSerializer,
ViewSets, CursorPagination, nested routers, bulk ops).

Two scenarios:
  1. `generate_openapi(app)` called directly N times — pure Python
     schema construction, no HTTP, no DB.
  2. `GET /openapi.json` via in-process TestClient — measures the
     wire path including `Response.json` serialization.

Outputs:
  logs/profile_openapi_cprofile.txt — human-readable top-30 per scenario
  logs/profile_openapi_cprofile.json — structured top-30 per scenario

Run: uv run python scripts/profile_openapi_cprofile.py
"""

import asyncio
import cProfile
import json
import os
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

LOGS = Path(__file__).resolve().parent.parent / "logs"

# The schema is static per-process — caller-side iterations to amortize
# jitter. Minimum 5s per run.
DIRECT_ITERS = 3000  # generate_openapi() directly — likely ~600/s so ~5s
HTTP_ITERS = 2000  # /openapi.json via TestClient — slower due to JSON dump + HTTP
MULTI_RUN = 3


def _profile_block(label: str, iters: int, run_fn) -> dict:
    profiler = cProfile.Profile()
    run_times: list[float] = []
    for _ in range(MULTI_RUN):
        t0 = time.perf_counter()
        profiler.enable()
        run_fn(iters)
        profiler.disable()
        run_times.append(time.perf_counter() - t0)

    run_times_sorted = sorted(run_times)
    elapsed = run_times_sorted[len(run_times_sorted) // 2]
    rps = iters / elapsed if elapsed > 0 else 0
    per_run_rps = [iters / t for t in run_times]
    jitter_pct = ((max(per_run_rps) - min(per_run_rps)) / rps * 100 / 2) if rps else 0

    stats = pstats.Stats(profiler)
    stats.strip_dirs()

    total_tt = sum(v[2] for v in stats.stats.values())
    top_entries: list[dict] = []
    ranked = sorted(stats.stats.items(), key=lambda kv: kv[1][2], reverse=True)[:30]
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

    return {
        "label": label,
        "iterations": iters,
        "multi_run": MULTI_RUN,
        "median_elapsed_s": round(elapsed, 3),
        "median_rps": round(rps, 1),
        "per_run_rps": [round(r, 1) for r in per_run_rps],
        "jitter_pct": round(jitter_pct, 2),
        "total_tottime_s": round(total_tt, 3),
        "top_30_by_tottime": top_entries,
    }


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("  Task #185: OpenAPI schema generation cProfile")
    print("=" * 70)

    import subprocess

    print("\nSetting up bookstore_api DB (for app import)...")
    setup = subprocess.run(
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
        text=True,
        timeout=120,
    )
    if setup.returncode != 0:
        print(f"setup failed: {setup.stderr[-500:]}")
        sys.exit(1)

    os.environ["HYPER_LOAD_TEST"] = "1"

    from hyperdjango.database import get_db
    from hyperdjango.openapi import generate_openapi
    from hyperdjango.testing import TestClient
    from services.bookstore_api.app import app

    db = get_db()
    if db._pool_handle is None:
        asyncio.run(db.connect())

    # Warmup — prime any lazy imports / caches
    print("Warming up...")
    for _ in range(50):
        _ = generate_openapi(app, title="bench", version="1.0", description="")

    # Scenario 1: direct call
    def run_direct(n: int):
        for _ in range(n):
            _ = generate_openapi(app, title="bench", version="1.0", description="")

    print("\n── generate_openapi() direct call ──")
    direct_result = _profile_block("direct", DIRECT_ITERS, run_direct)
    print(
        f"  rps: {direct_result['median_rps']:,.0f} | "
        f"per-run: {[f'{r:,.0f}' for r in direct_result['per_run_rps']]} | "
        f"jitter: ±{direct_result['jitter_pct']:.1f}% | "
        f"elapsed: {direct_result['median_elapsed_s']}s"
    )
    print("  Top 12 by SELF time:")
    for entry in direct_result["top_30_by_tottime"][:12]:
        print(
            f"    {entry['tottime_s']:>8.3f}s  {entry['call_count']:>8}  "
            f"{entry['pct_of_total']:>5.1f}%  {entry['function']}"
        )

    # Scenario 2: HTTP /openapi.json via TestClient
    client = TestClient(app)
    # Warmup the HTTP path too
    for _ in range(10):
        r = client.get("/openapi.json")
        if r.status != 200:
            print(f"  HTTP warmup failed: {r.status}")
            sys.exit(1)

    def run_http(n: int):
        for _ in range(n):
            client.get("/openapi.json")

    print("\n── GET /openapi.json via TestClient ──")
    http_result = _profile_block("http", HTTP_ITERS, run_http)
    print(
        f"  rps: {http_result['median_rps']:,.0f} | "
        f"per-run: {[f'{r:,.0f}' for r in http_result['per_run_rps']]} | "
        f"jitter: ±{http_result['jitter_pct']:.1f}% | "
        f"elapsed: {http_result['median_elapsed_s']}s"
    )
    print("  Top 12 by SELF time:")
    for entry in http_result["top_30_by_tottime"][:12]:
        print(
            f"    {entry['tottime_s']:>8.3f}s  {entry['call_count']:>8}  "
            f"{entry['pct_of_total']:>5.1f}%  {entry['function']}"
        )

    (LOGS / "profile_openapi_cprofile.json").write_text(
        json.dumps({"direct": direct_result, "http": http_result}, indent=2)
    )
    print(f"\n  JSON: {LOGS / 'profile_openapi_cprofile.json'}")
    print("\n" + "=" * 70)
    print("  Audit complete")
    print("=" * 70)


if __name__ == "__main__":
    main()
