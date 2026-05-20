"""
HyperAdmin in-process cProfile — find Python hotspots in admin renders.

Targets the bookstore_api admin (auto-CRUD over Book/Author/Category/Review).
Authenticates via /admin/login/ then profiles the changelist + dashboard.

Stability rule (matches profile_hypernews_cprofile.py):
  - Each run ≥ 5 seconds wall-clock
  - N=3 runs per endpoint, median rps reported, jitter tracked
  - WARMUP primes caches, prepared statements, template bytecode

Outputs:
  logs/profile_admin_<endpoint>.prof
  logs/profile_admin_<endpoint>.txt
  logs/profile_admin.json

Run: uv run python scripts/profile_admin_cprofile.py
"""

import asyncio
import cProfile
import io
import json
import os
import pstats
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

LOGS = Path(__file__).resolve().parent.parent / "logs"

WARMUP = 200
MULTI_RUN = 3

# Per-endpoint iteration counts — admin pages are slower than API endpoints
# (template-heavy with introspection), so 2-4K iters typically suffices for ≥5s.
ENDPOINT_ITERATIONS: dict[str, int] = {
    "dashboard": 3000,  # /admin/        ~600-1000 rps
    "book_list": 2000,  # /admin/book/   slower (full changelist)
    "book_add": 3000,  # /admin/book/add/ form render
    "login_page": 6000,  # /admin/login/  template only
}


PROFILE_PASSWORD = os.environ.setdefault(
    "HYPER_ADMIN_PASSWORD", "profile-admin-password"
)
os.environ.setdefault("HYPER_SEED_PASSWORD", PROFILE_PASSWORD)


def _setup_db():
    print("Setting up database...")
    env = os.environ.copy()
    r = subprocess.run(
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
        env=env,
    )
    if r.returncode != 0:
        print(f"  setup failed: {r.stderr[-500:]}")
        sys.exit(1)


def _login_admin(client) -> None:
    """Login as admin via /admin/login/. TestClient handles cookies automatically."""
    # Fetch the login page to get the CSRF token cookie
    r = client.get("/admin/login/")
    if r.status != 200:
        print(f"  login page returned {r.status}: {r.text()[:200]}")
        sys.exit(1)

    body = r.text()
    csrf_match = re.search(r'name="_csrf_token"\s+value="([^"]+)"', body)
    csrf_token = csrf_match.group(1) if csrf_match else ""

    # POST credentials — TestClient auto-attaches the csrftoken cookie
    r = client.post(
        "/admin/login/",
        data={
            "username": "admin",
            "password": PROFILE_PASSWORD,
            "_csrf_token": csrf_token,
        },
    )
    if r.status not in (200, 302, 303):
        print(f"  login POST returned {r.status}: {r.text()[:200]}")
        sys.exit(1)


def _run_and_profile(
    client, path: str, label: str, iterations: int
) -> tuple[cProfile.Profile, float, int, list[float]]:
    """Run an endpoint multiple times under cProfile, return median timing."""
    # Warmup (not profiled)
    for _ in range(WARMUP):
        r = client.get(path)
    if r.status != 200:
        print(f"  WARN: {label} returned {r.status}")

    profiler = cProfile.Profile()
    last_status = 0
    run_times: list[float] = []

    for run_idx in range(MULTI_RUN):
        start = time.perf_counter()
        profiler.enable()
        for _ in range(iterations):
            r = client.get(path)
            last_status = r.status
        profiler.disable()
        elapsed = time.perf_counter() - start
        run_times.append(elapsed)

    run_times_sorted = sorted(run_times)
    median_elapsed = run_times_sorted[len(run_times_sorted) // 2]
    return profiler, median_elapsed, last_status, run_times


def _extract_top(profiler, n=30):
    stats = pstats.Stats(profiler)
    stats.strip_dirs()
    ranked = sorted(stats.stats.items(), key=lambda kv: kv[1][2], reverse=True)[:n]
    total_tt = sum(v[2] for v in stats.stats.values())
    entries = []
    for (fname, lineno, func), (cc, nc, tt, ct, _) in ranked:
        entries.append(
            {
                "function": f"{fname}:{lineno}:{func}",
                "call_count": cc,
                "tottime_s": round(tt, 4),
                "cumtime_s": round(ct, 4),
                "pct_of_total": round(tt / total_tt * 100, 2) if total_tt else 0,
            }
        )
    return entries, total_tt


def _write_text_report(profiler, path, title):
    buf = io.StringIO()
    buf.write(f"# {title}\n")
    buf.write("=" * 80 + "\n")
    buf.write("Top 30 by SELF (TOTAL) time\n")
    buf.write("=" * 80 + "\n")
    stats = pstats.Stats(profiler, stream=buf)
    stats.strip_dirs()
    stats.sort_stats("tottime")
    stats.print_stats(30)
    buf.write("\n" + "=" * 80 + "\n")
    buf.write("Top 30 by CUMULATIVE time\n")
    buf.write("=" * 80 + "\n")
    stats.sort_stats("cumulative")
    stats.print_stats(30)
    path.write_text(buf.getvalue())


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("  HyperAdmin cProfile — bookstore_api admin page hotspots")
    print(
        "  Iterations: "
        + ", ".join(f"{k}={v}" for k, v in ENDPOINT_ITERATIONS.items())
        + f" (warmup: {WARMUP}, runs: {MULTI_RUN})"
    )
    print("  Stability rule: every run ≥ 5s wall-clock")
    print("=" * 70)

    _setup_db()

    os.environ["HYPER_LOAD_TEST"] = "1"
    os.environ["RATE_LIMIT"] = "0"

    from hyperdjango.database import get_db
    from hyperdjango.testing import TestClient
    from services.bookstore_api.app import app

    db = get_db()
    if db._pool_handle is None:
        asyncio.run(db.connect())

    client = TestClient(app)

    # Authenticate to admin (TestClient persists cookies automatically)
    print("\nLogging in to admin...")
    _login_admin(client)

    # Verify auth worked by hitting the dashboard
    r = client.get("/admin/")
    if r.status != 200:
        print(f"  admin / returned {r.status} after login")
        sys.exit(1)
    print(f"  authenticated, dashboard returned {r.status}")

    # Endpoints — slug names match HyperAdmin.register() calls in app.py
    # bookstore registers: Book, Author, Category, Review
    targets = [
        ("dashboard", "/admin/", "GET /admin/ (dashboard)"),
        ("book_list", "/admin/book/", "GET /admin/book/ (changelist)"),
        ("book_add", "/admin/book/add/", "GET /admin/book/add/ (form render)"),
        ("login_page", "/admin/login/", "GET /admin/login/ (template only)"),
    ]

    all_results: dict[str, dict] = {}
    for slug, path, label in targets:
        iters = ENDPOINT_ITERATIONS[slug]
        print(f"\n── {label} ── (iters={iters})")
        profiler, median_elapsed, status, run_times = _run_and_profile(
            client, path, label, iters
        )
        rps = iters / median_elapsed if median_elapsed > 0 else 0
        avg_ms = (median_elapsed / iters) * 1000
        run_rps = [(iters / t if t > 0 else 0.0) for t in run_times]
        jitter_pct = ((max(run_rps) - min(run_rps)) / rps * 100 / 2) if rps > 0 else 0
        shortest_run = min(run_times) if run_times else 0
        warn = " ⚠️ SHORT RUN <5s" if shortest_run < 5.0 else ""

        print(
            f"  status: {status} | median rps: {rps:.0f} | avg: {avg_ms:.2f}ms | "
            f"per-run: [{', '.join(f'{r:.0f}' for r in run_rps)}] | "
            f"jitter: ±{jitter_pct:.1f}% | shortest: {shortest_run:.1f}s{warn}"
        )

        entries, total_tt = _extract_top(profiler, n=30)
        prof_path = LOGS / f"profile_admin_{slug}.prof"
        profiler.dump_stats(str(prof_path))
        txt_path = LOGS / f"profile_admin_{slug}.txt"
        _write_text_report(profiler, txt_path, f"HyperAdmin — {label}")

        all_results[slug] = {
            "label": label,
            "path": path,
            "status": status,
            "iterations_per_run": iters,
            "runs": MULTI_RUN,
            "median_rps": round(rps, 1),
            "per_run_rps": [round(r, 1) for r in run_rps],
            "jitter_pct": round(jitter_pct, 2),
            "avg_ms": round(avg_ms, 3),
            "total_tottime_s": round(total_tt, 3),
            "top_15": entries[:15],
        }

        print("\n  Top 10 by SELF time:")
        print(f"  {'tottime(ms)':>12} {'cumtime(ms)':>12} {'calls':>10}  function")
        for e in entries[:10]:
            print(
                f"  {e['tottime_s'] * 1000:>12.2f} {e['cumtime_s'] * 1000:>12.2f} "
                f"{e['call_count']:>10}  {e['function']}"
            )

    json_path = LOGS / "profile_admin.json"
    json_path.write_text(json.dumps(all_results, indent=2))
    print(f"\n  Combined JSON: {json_path}")

    print("\n" + "=" * 70)
    print("  HyperAdmin profiling complete")
    print("=" * 70)


if __name__ == "__main__":
    main()
