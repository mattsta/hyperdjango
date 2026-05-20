"""
Task #184: in-process cProfile of the REST write/POST hot path on bookstore_api.

Counterpart to profile_list_cprofile.py (read path / List endpoint, v0.14.10 win).
The write path has its own hot code:
  - JSON deserialize (request.json())
  - BookWriteSerializer validation (_validate_field, _coerce, etc.)
  - perform_create hook (FK lookup queries)
  - Model.save() / INSERT
  - response serialization (BookListSerializer)

Outputs:
  logs/profile_write_cprofile.prof   — pstats binary dump
  logs/profile_write_cprofile.txt    — human-readable top-30 by cumulative + self time
  logs/profile_write_cprofile.json   — structured top-30 for parsing

Run: uv run python scripts/profile_write_cprofile.py
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

# Stability rule: each run ≥5s wall time.
# First run measured ~1318 rps in-process → 7500 iters → ~5.7s/run.
# WARMUP primes pool, prepared stmts, caches, request hash caches.
ITERATIONS = 7500
WARMUP = 300
MULTI_RUN = 3


PROFILE_PASSWORD = os.environ.setdefault("HYPER_SEED_PASSWORD", "profile-seed-password")
os.environ.setdefault("HYPER_ADMIN_PASSWORD", PROFILE_PASSWORD)


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    print("=== cProfile: Write/POST hot path (in-process TestClient) ===")
    print(f"  Iterations: {ITERATIONS} (warmup: {WARMUP}, multi_run: {MULTI_RUN})")

    # Fresh DB via subprocess — clean baseline, no prior load test rows.
    print("Setting up database...")
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
        timeout=120,
        env=os.environ.copy(),
    )
    if setup.returncode != 0:
        print("setup FAILED:")
        print(setup.stdout.decode()[-2000:])
        print(setup.stderr.decode()[-2000:])
        sys.exit(1)

    os.environ["HYPER_LOAD_TEST"] = "1"

    from hyperdjango.database import get_db
    from hyperdjango.testing import TestClient
    from services.bookstore_api.app import Author, Category, app

    db = get_db()
    if db._pool_handle is None:
        asyncio.run(db.connect())

    # Resolve seed FK ids — POSTs need real author_id + category_id.
    async def _fetch_fk_ids() -> tuple[int, int]:
        a = await Author.objects.first()
        c = await Category.objects.first()
        if a is None or c is None:
            raise RuntimeError("seed missing Author/Category rows")
        return a.id, c.id

    author_id, category_id = asyncio.run(_fetch_fk_ids())
    print(f"  Using author_id={author_id}, category_id={category_id}")

    client = TestClient(app)

    # Authenticate as the staff seed user — password resolved from HYPER_SEED_PASSWORD.
    login_resp = client.post(
        "/auth/login", json={"username": "admin", "password": PROFILE_PASSWORD}
    )
    if login_resp.status != 200:
        print(
            f"login FAILED: status={login_resp.status} body={login_resp.text()[:300]}"
        )
        sys.exit(1)
    print(f"  Logged in as admin (cookies persisted: {bool(client._cookies)})")

    def make_body(seq: int) -> dict:
        return {
            "title": f"Profile Book {seq:08d}",
            "isbn": f"PROFILE-{seq:08d}",
            "description": "A book created during profiling. " * 4,
            "price": "12.99",
            "pages": 250,
            "author_id": author_id,
            "category_id": category_id,
        }

    # Warmup — primes prepared statements, pool, serializer caches, request hash caches.
    print(f"Warming up ({WARMUP} requests)...")
    for i in range(WARMUP):
        r = client.post("/api/v1/books/", json=make_body(i))
        if r.status not in (200, 201):
            print(f"  warmup: status {r.status}: {r.text()[:300]}")
            sys.exit(1)

    # Profile: MULTI_RUN runs of ITERATIONS each, accumulating into one profiler.
    # Median wall time across runs filters jitter.
    total_seq = WARMUP
    print(f"Profiling {ITERATIONS} × {MULTI_RUN} = {ITERATIONS * MULTI_RUN} POSTs...")
    profiler = cProfile.Profile()
    run_times: list[float] = []
    for run_idx in range(MULTI_RUN):
        start = time.perf_counter()
        profiler.enable()
        for _ in range(ITERATIONS):
            client.post("/api/v1/books/", json=make_body(total_seq))
            total_seq += 1
        profiler.disable()
        run_times.append(time.perf_counter() - start)

    run_times_sorted = sorted(run_times)
    elapsed = run_times_sorted[len(run_times_sorted) // 2]  # median
    rps = ITERATIONS / elapsed
    avg_ms = (elapsed / ITERATIONS) * 1000

    per_run_rps = [ITERATIONS / t for t in run_times]
    jitter_pct = (max(per_run_rps) - min(per_run_rps)) / rps * 100 / 2

    print(f"\n  Median elapsed: {elapsed:.2f}s (per run)")
    print(f"  Per-run rps: {[f'{r:.0f}' for r in per_run_rps]}")
    print(f"  Median rps: {rps:.0f} ± {jitter_pct:.1f}%")
    print(f"  Avg latency: {avg_ms:.2f}ms")

    prof_path = LOGS / "profile_write_cprofile.prof"
    profiler.dump_stats(str(prof_path))
    print(f"\n  Raw profile: {prof_path}")

    # Text report
    stats_buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=stats_buf)
    stats.strip_dirs()

    report_lines: list[str] = []
    report_lines.append(
        "# cProfile Report — Write/POST Endpoint (in-process TestClient)"
    )
    report_lines.append(f"Iterations: {ITERATIONS} × {MULTI_RUN} runs")
    report_lines.append(f"Median elapsed: {elapsed:.2f}s")
    report_lines.append(f"Per-run rps: {per_run_rps}")
    report_lines.append(f"In-process rps: {rps:.0f} ± {jitter_pct:.1f}%")
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

    report_path = LOGS / "profile_write_cprofile.txt"
    report_path.write_text("\n".join(report_lines))
    print(f"  Text report: {report_path}")

    # Structured top-30 by tottime for JSON output
    top_entries: list[dict] = []
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

    json_path = LOGS / "profile_write_cprofile.json"
    json_path.write_text(
        json.dumps(
            {
                "iterations": ITERATIONS,
                "multi_run": MULTI_RUN,
                "elapsed_s": round(elapsed, 3),
                "per_run_rps": [round(r, 1) for r in per_run_rps],
                "rps": round(rps, 1),
                "jitter_pct": round(jitter_pct, 2),
                "avg_ms": round(avg_ms, 3),
                "total_tottime_s": round(total_tt, 3),
                "top_30_by_tottime": top_entries,
            },
            indent=2,
        )
    )
    print(f"  JSON report: {json_path}")

    print("\n=== Top 15 by SELF time ===")
    print(f"  {'tottime(s)':>10} {'cumtime(s)':>10} {'calls':>8}  function")
    for entry in top_entries[:15]:
        print(
            f"  {entry['tottime_s']:>10.3f} {entry['cumtime_s']:>10.3f} "
            f"{entry['call_count']:>8}  {entry['function']}"
        )

    print("\n=== profile_write_cprofile complete ===")


if __name__ == "__main__":
    main()
