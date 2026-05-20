"""
Cross-app cProfile profiling suite.

Profiles key endpoints across all DB-backed and non-DB services using
TestClient + cProfile. Each app runs in its OWN subprocess to avoid import
contamination. The main process aggregates per-app results into a
consolidated cross-app hotspot report.

Usage:
    uv run python scripts/profile_all_apps.py            # Profile all apps
    uv run python scripts/profile_all_apps.py --app rest_api  # Profile one app (subprocess mode)

Outputs:
    logs/profile_all_apps.txt   -- consolidated human-readable report
    logs/profile_all_apps.json  -- structured JSON for diffing
"""

import asyncio
import cProfile
import importlib
import json
import os
import pstats
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

LOGS = Path(__file__).resolve().parent.parent / "logs"
SCRIPT_PATH = Path(__file__).resolve()

# Per-app iteration counts tuned so each run takes >= 5s (Stability Rule).
# Warmup primes LRU caches, prepared stmts, pool, template cache.
WARMUP = 100
ITERATIONS = 1000
MULTI_RUN = 1  # Single run for the all-app sweep; dedicated scripts do 3-run median


@dataclass(slots=True)
class AppTarget:
    """Configuration for one app to profile."""

    name: str
    app_module: str  # e.g. "services.rest_api.app:app"
    seed_module: str  # e.g. "services.rest_api.seed:run" or "" for no-DB
    endpoint: str  # e.g. "/api/posts"
    needs_db: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    # Some endpoints require login first
    login_path: str = ""
    login_data: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ProfileResult:
    """Result from profiling one app endpoint."""

    name: str
    endpoint: str
    status: int
    iterations: int
    elapsed_s: float
    rps: float
    avg_ms: float
    top5: list[dict[str, float | str | int]]
    error: str = ""


# All app targets
DB_APPS: list[AppTarget] = [
    AppTarget(
        name="rest_api",
        app_module="services.rest_api.app:app",
        seed_module="services.rest_api.seed:run",
        endpoint="/api/posts",
    ),
    AppTarget(
        name="notes_api",
        app_module="services.notes_api.app:app",
        seed_module="services.notes_api.seed:run",
        endpoint="/api/notes",
    ),
    AppTarget(
        name="notes_api_categories",
        app_module="services.notes_api.app:app",
        seed_module="services.notes_api.seed:run",
        endpoint="/api/categories",
    ),
    AppTarget(
        name="content_hub",
        app_module="services.content_hub.app:app",
        seed_module="services.content_hub.seed:run",
        endpoint="/api/articles",
    ),
    AppTarget(
        name="bookstore_api",
        app_module="services.bookstore_api.app:app",
        seed_module="services.bookstore_api.seed:run",
        endpoint="/api/v1/books/",
    ),
    AppTarget(
        name="full_stack",
        app_module="services.full_stack.app:app",
        seed_module="services.full_stack.seed:run",
        endpoint="/",
        login_path="/login",
        login_data={"username": "alice", "password": "password123"},
    ),
    AppTarget(
        name="forms_demo",
        app_module="services.forms_demo.app:app",
        seed_module="services.forms_demo.seed:run",
        endpoint="/contact",
    ),
    AppTarget(
        name="hypernews",
        app_module="services.hypernews.app:app",
        seed_module="services.hypernews.setup:seed",
        endpoint="/",
    ),
    AppTarget(
        name="hyperai",
        app_module="services.hyperai.app:app",
        seed_module="services.hyperai.seed:run",
        endpoint="/health",
    ),
    AppTarget(
        name="semantic_search",
        app_module="services.semantic_search.app:app",
        seed_module="services.semantic_search.seed:run",
        endpoint="/",
    ),
    AppTarget(
        name="websocket_chat",
        app_module="services.websocket_chat.app:app",
        seed_module="services.websocket_chat.seed:run",
        endpoint="/health",
    ),
    AppTarget(
        name="task_queue",
        app_module="services.task_queue.app:app",
        seed_module="services.task_queue.seed:run",
        endpoint="/health",
    ),
    AppTarget(
        name="multi_tenant",
        app_module="services.multi_tenant.app:app",
        seed_module="services.multi_tenant.seed:run",
        endpoint="/api/projects/",
        headers={"X-Tenant-ID": "1"},
    ),
    AppTarget(
        name="hyperticket",
        app_module="services.hyperticket.app:app",
        seed_module="services.hyperticket.seed:run",
        endpoint="/health",
    ),
    AppTarget(
        name="deployment",
        app_module="services.deployment.app:app",
        seed_module="services.deployment.seed:run",
        endpoint="/api/items",
    ),
]

NO_DB_APPS: list[AppTarget] = [
    AppTarget(
        name="hello",
        app_module="services.hello.app:app",
        seed_module="",
        endpoint="/",
        needs_db=False,
    ),
    AppTarget(
        name="benchmark_app",
        app_module="services.benchmark_app.app:app",
        seed_module="",
        endpoint="/json",
        needs_db=False,
    ),
]

ALL_APPS: list[AppTarget] = DB_APPS + NO_DB_APPS


def _find_target(name: str) -> AppTarget:
    """Find an AppTarget by name."""
    for t in ALL_APPS:
        if t.name == name:
            return t
    print(f"ERROR: unknown app name '{name}'")
    print(f"  known: {[t.name for t in ALL_APPS]}")
    sys.exit(1)


def _extract_top(
    profiler: cProfile.Profile, n: int = 5
) -> tuple[list[dict[str, float | str | int]], float]:
    """Extract top-N entries by tottime and return (entries, total_tottime_s)."""
    stats = pstats.Stats(profiler)
    stats.strip_dirs()
    ranked = sorted(stats.stats.items(), key=lambda kv: kv[1][2], reverse=True)[:n]
    total_tt = sum(v[2] for v in stats.stats.values())
    entries: list[dict[str, float | str | int]] = []
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


def _profile_single_app(target: AppTarget) -> ProfileResult:
    """Profile a single app in the current process. Called from subprocess."""
    os.environ["HYPER_LOAD_TEST"] = "1"
    os.environ["RATE_LIMIT"] = "0"

    # DB setup via subprocess
    if target.needs_db and target.seed_module:
        print(f"  Setting up database for {target.name}...")
        setup_args = [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            target.app_module,
            "--drop",
            "--seed",
            target.seed_module,
        ]
        r = subprocess.run(
            setup_args,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            return ProfileResult(
                name=target.name,
                endpoint=target.endpoint,
                status=0,
                iterations=0,
                elapsed_s=0.0,
                rps=0.0,
                avg_ms=0.0,
                top5=[],
                error=f"setup failed (exit {r.returncode}): {r.stderr[-300:]}",
            )

    # Import the app module dynamically
    # The app_module is like "services.rest_api.app:app"
    module_path, attr_name = target.app_module.split(":")
    mod = importlib.import_module(module_path)
    app = mod.__dict__[attr_name]

    # Connect DB if needed
    if target.needs_db:
        from hyperdjango.database import get_db

        db = get_db()
        if db._pool_handle is None:
            asyncio.run(db.connect())

    from hyperdjango.testing import TestClient

    client = TestClient(app)

    # Login if needed
    if target.login_path and target.login_data:
        print(f"  Logging in via {target.login_path}...")
        login_r = client.post(target.login_path, data=target.login_data)
        if login_r.status >= 400:
            print(f"  WARN: login returned {login_r.status}")

    # Build request kwargs
    get_kwargs: dict[str, dict[str, str]] = {}
    if target.headers:
        get_kwargs["headers"] = target.headers

    # Warmup
    print(f"  Warming up ({WARMUP} requests)...")
    for _ in range(WARMUP):
        r = client.get(target.endpoint, **get_kwargs)
    warmup_status = r.status
    if warmup_status >= 500:
        return ProfileResult(
            name=target.name,
            endpoint=target.endpoint,
            status=warmup_status,
            iterations=0,
            elapsed_s=0.0,
            rps=0.0,
            avg_ms=0.0,
            top5=[],
            error=f"warmup returned status {warmup_status}: {r.text[:200]}",
        )

    # Profile
    print(f"  Profiling {ITERATIONS} requests to {target.endpoint}...")
    profiler = cProfile.Profile()
    last_status = 0

    start = time.perf_counter()
    profiler.enable()
    for _ in range(ITERATIONS):
        resp = client.get(target.endpoint, **get_kwargs)
        last_status = resp.status
    profiler.disable()
    elapsed = time.perf_counter() - start

    rps = ITERATIONS / elapsed if elapsed > 0 else 0
    avg_ms = (elapsed / ITERATIONS) * 1000

    entries, total_tt = _extract_top(profiler, n=5)

    # Save raw profile
    prof_path = LOGS / f"profile_allapp_{target.name}.prof"
    profiler.dump_stats(str(prof_path))

    print(
        f"  status={last_status} | {rps:.0f} rps | {avg_ms:.2f}ms avg | {elapsed:.1f}s total"
    )
    for e in entries[:5]:
        print(
            f"    {e['tottime_s']:>8.3f}s  {e['pct_of_total']:>5.1f}%  {e['function']}"
        )

    return ProfileResult(
        name=target.name,
        endpoint=target.endpoint,
        status=last_status,
        iterations=ITERATIONS,
        elapsed_s=round(elapsed, 3),
        rps=round(rps, 1),
        avg_ms=round(avg_ms, 3),
        top5=entries,
    )


def _run_single_app_subprocess(target: AppTarget) -> ProfileResult:
    """Run profiling of a single app in a subprocess, return parsed result."""
    print(f"\n{'=' * 60}")
    print(f"  Profiling: {target.name} -> {target.endpoint}")
    print(f"{'=' * 60}")

    r = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--app", target.name],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(SCRIPT_PATH.parent.parent),
    )

    # Parse the JSON result from the subprocess stdout
    # The subprocess prints the JSON as the last line after a marker
    stdout_lines = r.stdout.strip().split("\n")
    stderr_text = r.stderr.strip()

    # Find the JSON result marker
    json_line = ""
    for line in reversed(stdout_lines):
        if line.startswith("__RESULT_JSON__:"):
            json_line = line[len("__RESULT_JSON__:") :]
            break

    if not json_line:
        # Print subprocess output for debugging
        print(f"  subprocess stdout (last 500):\n{r.stdout[-500:]}")
        if stderr_text:
            print(f"  subprocess stderr (last 300):\n{stderr_text[-300:]}")
        return ProfileResult(
            name=target.name,
            endpoint=target.endpoint,
            status=0,
            iterations=0,
            elapsed_s=0.0,
            rps=0.0,
            avg_ms=0.0,
            top5=[],
            error=f"subprocess exit={r.returncode}, no JSON result found",
        )

    data = json.loads(json_line)
    return ProfileResult(
        name=data["name"],
        endpoint=data["endpoint"],
        status=data["status"],
        iterations=data["iterations"],
        elapsed_s=data["elapsed_s"],
        rps=data["rps"],
        avg_ms=data["avg_ms"],
        top5=data["top5"],
        error=data.get("error", ""),
    )


def _result_to_dict(
    result: ProfileResult,
) -> dict[str, float | str | int | list[dict[str, float | str | int]]]:
    """Convert ProfileResult to a serializable dict."""
    return {
        "name": result.name,
        "endpoint": result.endpoint,
        "status": result.status,
        "iterations": result.iterations,
        "elapsed_s": result.elapsed_s,
        "rps": result.rps,
        "avg_ms": result.avg_ms,
        "top5": result.top5,
        "error": result.error,
    }


def _write_report(
    results: list[ProfileResult], report_path: Path, json_path: Path
) -> None:
    """Write consolidated text + JSON reports."""
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("  Cross-App cProfile Report")
    lines.append(f"  Iterations per app: {ITERATIONS} (warmup: {WARMUP})")
    lines.append(f"  Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    # Summary table
    lines.append("SUMMARY")
    lines.append("-" * 80)
    lines.append(
        f"{'App':<22} {'Endpoint':<22} {'Status':>6} {'RPS':>8} {'Avg ms':>8}  Top Hot Function"
    )
    lines.append("-" * 80)
    for r in results:
        if r.error:
            lines.append(
                f"{r.name:<22} {r.endpoint:<22} {'ERR':>6} {'---':>8} {'---':>8}  {r.error[:40]}"
            )
        else:
            top_fn = r.top5[0]["function"] if r.top5 else "-"
            # Truncate long function names
            if len(top_fn) > 40:
                top_fn = top_fn[:37] + "..."
            lines.append(
                f"{r.name:<22} {r.endpoint:<22} {r.status:>6} {r.rps:>8.0f} {r.avg_ms:>8.2f}  {top_fn}"
            )
    lines.append("")

    # Per-app details
    for r in results:
        lines.append(f"\n{'=' * 60}")
        lines.append(f"  {r.name}: {r.endpoint}")
        lines.append(f"{'=' * 60}")
        if r.error:
            lines.append(f"  ERROR: {r.error}")
            continue

        lines.append(f"  Status: {r.status}")
        lines.append(f"  Iterations: {r.iterations}")
        lines.append(f"  Elapsed: {r.elapsed_s}s")
        lines.append(f"  RPS: {r.rps}")
        lines.append(f"  Avg latency: {r.avg_ms}ms")
        lines.append("")
        lines.append("  Top 5 by SELF time:")
        lines.append(
            f"  {'tottime(s)':>10} {'cumtime(s)':>10} {'calls':>8} {'%':>6}  function"
        )
        for e in r.top5:
            lines.append(
                f"  {e['tottime_s']:>10.4f} {e['cumtime_s']:>10.4f} "
                f"{e['call_count']:>8} {e['pct_of_total']:>5.1f}%  {e['function']}"
            )

    # Cross-app aggregated hotspots
    lines.append(f"\n\n{'=' * 80}")
    lines.append("  CROSS-APP TOP 10 HOTTEST FUNCTIONS (aggregated self-time)")
    lines.append(f"{'=' * 80}")

    # Aggregate by function name
    fn_totals: dict[str, float] = {}
    fn_apps: dict[str, list[str]] = {}
    for r in results:
        if r.error:
            continue
        for e in r.top5:
            fn = e["function"]
            tt = e["tottime_s"]
            if fn not in fn_totals:
                fn_totals[fn] = 0.0
                fn_apps[fn] = []
            fn_totals[fn] += tt
            fn_apps[fn].append(r.name)

    # Sort by aggregated tottime
    sorted_fns = sorted(fn_totals.items(), key=lambda kv: kv[1], reverse=True)[:10]
    lines.append(f"  {'agg_time(s)':>11} {'apps':>5}  function (apps)")
    lines.append(f"  {'-' * 70}")
    for fn, agg_tt in sorted_fns:
        apps_list = ", ".join(sorted(set(fn_apps[fn])))
        n_apps = len(set(fn_apps[fn]))
        lines.append(f"  {agg_tt:>11.4f} {n_apps:>5}  {fn}")
        lines.append(f"  {'':>11} {'':>5}  -> {apps_list}")

    lines.append("")
    report_text = "\n".join(lines)
    report_path.write_text(report_text)
    print(f"\n  Text report: {report_path}")

    # JSON output
    json_data = {
        "iterations": ITERATIONS,
        "warmup": WARMUP,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "apps": [_result_to_dict(r) for r in results],
        "cross_app_top10": [
            {
                "function": fn,
                "aggregated_tottime_s": round(agg_tt, 4),
                "app_count": len(set(fn_apps[fn])),
                "apps": sorted(set(fn_apps[fn])),
            }
            for fn, agg_tt in sorted_fns
        ],
    }
    json_path.write_text(json.dumps(json_data, indent=2))
    print(f"  JSON report: {json_path}")


def main_single_app(app_name: str) -> None:
    """Profile a single app and output JSON result. Called in subprocess mode."""
    LOGS.mkdir(parents=True, exist_ok=True)
    target = _find_target(app_name)
    result = _profile_single_app(target)
    # Print the JSON result with a marker so the parent can parse it
    result_json = json.dumps(_result_to_dict(result))
    print(f"__RESULT_JSON__:{result_json}")


def main() -> None:
    """Main entry point: profile all apps in subprocesses, aggregate results."""
    LOGS.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  Cross-App cProfile Suite")
    print(
        f"  Apps: {len(ALL_APPS)} ({len(DB_APPS)} DB-backed, {len(NO_DB_APPS)} no-DB)"
    )
    print(f"  Iterations: {ITERATIONS} per app (warmup: {WARMUP})")
    print("=" * 70)

    results: list[ProfileResult] = []
    for target in ALL_APPS:
        result = _run_single_app_subprocess(target)
        results.append(result)
        if result.error:
            print(f"  FAILED: {result.error}")
        else:
            print(f"  OK: {result.rps:.0f} rps, {result.avg_ms:.2f}ms avg")

    # Write consolidated reports
    report_path = LOGS / "profile_all_apps.txt"
    json_path = LOGS / "profile_all_apps.json"
    _write_report(results, report_path, json_path)

    # Print summary
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    ok_count = sum(1 for r in results if not r.error)
    fail_count = sum(1 for r in results if r.error)
    print(f"  Profiled: {ok_count}/{len(results)} apps")
    if fail_count:
        print(f"  Failed: {fail_count}")
        for r in results:
            if r.error:
                print(f"    - {r.name}: {r.error[:60]}")
    print(f"\n  Report: {report_path}")
    print(f"  JSON:   {json_path}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    if "--app" in sys.argv:
        idx = sys.argv.index("--app")
        if idx + 1 < len(sys.argv):
            main_single_app(sys.argv[idx + 1])
        else:
            print("ERROR: --app requires an app name")
            sys.exit(1)
    else:
        main()
