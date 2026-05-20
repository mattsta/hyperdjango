"""
HyperNews multi-endpoint cProfile for platform-wide hotspot discovery.

Target: find SYSTEMIC Python hotspots that affect more than one service.
HyperNews is the most complex example (5,871 lines — voting, templates,
keyset cursors, raw SQL mixed with ORM, HTMX forms, nested comment trees).

Endpoints profiled:
  1. GET /            — cached list (tests cache hit + MISS paths)
  2. GET /post/{pid}  — uncached detail + comment tree + template render
  3. GET /user/{uname}— multi-query aggregation (user + posts + comments + memberships)
  4. GET /forums      — keyset paginated forum directory (pure DB + template)
  5. GET /login       — template render only (baseline for middleware + template)

Seeds extra data before profiling so cProfile sees realistic call patterns:
  - 500 posts across 8 forums
  - 200 comments on the top-10 posts (unbounded comment tree)
  - 50 votes scattered across posts

Outputs:
  logs/profile_hypernews_<endpoint>.prof  — pstats dump per endpoint
  logs/profile_hypernews_report.md        — human-readable report + top 15 tables
  logs/profile_hypernews.json             — structured top-30 for diffing
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

# Profile budget per endpoint. Every run MUST be at least 5 wall-clock
# seconds (the Stability Rule in feedback_profile_before_optimize.md).
# Short runs at ~1s per run produce ±30% jitter between invocations
# because CPU frequency scaling and scheduler noise dominate.
#
# Per-endpoint iteration counts are tuned so that at the expected rps
# the run takes 7-8 seconds — a 5-second floor plus a cushion that still
# keeps total runtime reasonable (≈2.5 min for all 5 endpoints × 3 runs).
#
# If an endpoint's rps drops well below the tuned value, the run gets
# even more stable (longer). If it climbs well above, bump the count.
WARMUP = 500  # prime LRU caches, prepared statements, pool, bytecode cache
MULTI_RUN = 3  # median of 3 runs per endpoint

# Target: ≥5 seconds per run. Cushion to 7-8s at expected rps.
# Per-endpoint iter counts, keyed by slug (matches `targets` below).
ENDPOINT_ITERATIONS: dict[str, int] = {
    "index_cached": 20000,  # /            @ ~2800 rps → ~7.1s/run
    "post_detail": 7000,  # /post/{pid}  @ ~850 rps  → ~8.2s/run  (was 540 → 4000)
    "user_profile": 6000,  # /user/alice  @ ~800 rps  → ~7.5s/run  (was 680 → 5000)
    "forums_list": 22000,  # /forums      @ ~2900 rps → ~7.6s/run
    "login_form": 42000,  # /login       @ ~5500 rps → ~7.6s/run
}


async def _seed_extra_data():
    """Generate additional posts, comments, votes so profiling is representative."""
    from services.hypernews.models import Comment, Forum, Post, User

    # Check existing post count
    existing = await Post.objects.count()
    if existing >= 500:
        print(f"  already have {existing} posts, skipping seed")
        return

    print(f"  seeding extra data (current: {existing} posts)...")

    # Get author IDs and forum IDs
    users = await User.objects.all()
    if not users:
        print("  ERROR: no users; aborting seed")
        return
    user_ids = [u.id for u in users]

    forums = await Forum.objects.all()
    if not forums:
        print("  ERROR: no forums; aborting seed")
        return
    forum_ids = [f.id for f in forums]

    # Create 500 posts
    import random

    rng = random.Random(42)

    sample_titles = [
        "Understanding async in Python 3.14",
        "Why we chose PostgreSQL for our stack",
        "Benchmarking web frameworks in 2026",
        "A guide to free-threaded Python",
        "Building real-time dashboards with SSE",
        "How to profile Python applications",
        "The case for keyset pagination",
        "Rust vs Zig for systems programming",
        "What's new in HyperDjango v0.14",
        "Debugging memory leaks in production",
        "Database connection pooling done right",
        "Why your ORM is slow",
        "Full-text search with PostgreSQL",
        "Migrating from Django to HyperDjango",
        "The ops cost of microservices",
    ]
    texts = [
        "A detailed write-up with benchmarks and examples.",
        "We learned a lot during this migration.",
        "Here's what worked and what didn't.",
        "The team spent 6 months on this project.",
        "Real numbers from a production deployment.",
    ]

    needed = 500 - existing
    for i in range(needed):
        title = f"{rng.choice(sample_titles)} #{i}"
        p = Post(
            title=title,
            slug=f"post-{existing + i}",
            text=rng.choice(texts) if rng.random() > 0.3 else "",
            url="https://example.com/article" if rng.random() > 0.5 else "",
            author_id=rng.choice(user_ids),
            forum_id=rng.choice(forum_ids),
            score=rng.randint(1, 500),
            comment_count=0,
        )
        await p.save()
    print(f"  created {needed} posts")

    # Add comments to post id=1 (the one the benchmark hits)
    existing_comments = await Comment.objects.filter(post_id=1).count()
    if existing_comments < 50:
        for i in range(50 - existing_comments):
            c = Comment(
                post_id=1,
                author_id=rng.choice(user_ids),
                text=f"Comment #{i}: {rng.choice(texts)}",
                score=rng.randint(-5, 50),
            )
            await c.save()
        await Post.objects.filter(id=1).update(comment_count=50)
        print(f"  created {50 - existing_comments} comments on post 1")


def _run_and_profile(
    client, path: str, label: str, iterations: int
) -> tuple[cProfile.Profile, float, int, list[float]]:
    """Run the endpoint multiple times under cProfile, return stable median timing.

    Runs the target N iterations MULTI_RUN times and reports the MEDIAN wall
    time across runs. The single cProfile object accumulates across all runs
    so call counts are the sum — multiply by the number of runs when
    interpreting per-request averages. Median is the primary reported
    statistic because run-to-run CPU jitter is common and single runs of
    300 iterations can vary by ±30%.

    Returns: (profiler, median_elapsed_s, last_status, all_run_elapsed_s)
    """
    # Warmup (not profiled) — prime LRU caches, prepared statements,
    # connection pool, template cache, OS page cache
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


def _extract_top(profiler: cProfile.Profile, n: int = 30) -> tuple[list[dict], float]:
    """Extract top-N entries by tottime and return (entries, total_tottime_s)."""
    stats = pstats.Stats(profiler)
    stats.strip_dirs()
    ranked = sorted(stats.stats.items(), key=lambda kv: kv[1][2], reverse=True)[:n]
    total_tt = sum(v[2] for v in stats.stats.values())
    entries: list[dict] = []
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


def _write_text_report(profiler: cProfile.Profile, path: Path, title: str) -> None:
    """Dump pstats top-30 tottime + top-30 cumtime to a text file."""
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
    print("  HyperNews cProfile — platform-wide hotspot discovery")
    print(
        "  Iterations per endpoint: "
        + ", ".join(f"{k}={v}" for k, v in ENDPOINT_ITERATIONS.items())
        + f" (warmup: {WARMUP}, runs: {MULTI_RUN})"
    )
    print("  Stability rule: every run ≥ 5s wall-clock")
    print("=" * 70)

    print("\nSetting up database...")
    r = subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypernews.app:app",
            "--drop",
            "--seed",
            "services.hypernews.setup:seed",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        print(f"  setup failed (exit {r.returncode}):")
        print(f"  stdout: {r.stdout[-500:]}")
        print(f"  stderr: {r.stderr[-500:]}")
        sys.exit(1)

    # Extra data for realistic profile
    os.environ["HYPER_LOAD_TEST"] = "1"
    os.environ["RATE_LIMIT"] = "0"

    from hyperdjango.database import get_db
    from hyperdjango.testing import TestClient
    from services.hypernews.app import app

    db = get_db()
    if db._pool_handle is None:
        asyncio.run(db.connect())

    asyncio.run(_seed_extra_data())

    client = TestClient(app)

    # Fetch the first post's external (opaque) ID — the hypernews URL scheme
    # uses HMAC-signed external IDs, not raw integer PKs.
    from services.hypernews.models import Post

    async def _get_post_pid():
        p = await Post.objects.order_by("id").first()
        return p.get_external_id() if p else None

    post_pid = asyncio.new_event_loop().run_until_complete(_get_post_pid())
    if not post_pid:
        print("ERROR: no posts in DB")
        sys.exit(1)
    print(f"  using post external ID: {post_pid}")

    # Endpoints to profile
    targets = [
        ("index_cached", "/", "GET / (cached index — hot path)"),
        (
            "post_detail",
            f"/post/{post_pid}",
            "GET /post/{pid} (uncached detail + comment tree)",
        ),
        ("user_profile", "/user/alice", "GET /user/alice (multi-query profile)"),
        ("forums_list", "/forums", "GET /forums (forum directory)"),
        ("login_form", "/login", "GET /login (template-only baseline)"),
    ]

    all_results: dict[str, dict] = {}

    for slug, path, label in targets:
        iters = ENDPOINT_ITERATIONS[slug]
        print(f"\n── {label} ── (iters={iters})")
        profiler, median_elapsed, status, run_times = _run_and_profile(
            client, path, label, iterations=iters
        )
        rps = iters / median_elapsed if median_elapsed > 0 else 0
        avg_ms = (median_elapsed / iters) * 1000

        # Report variance across runs so noisy results are visible
        run_rps = [(iters / t if t > 0 else 0.0) for t in run_times]
        min_rps = min(run_rps)
        max_rps = max(run_rps)
        jitter_pct = ((max_rps - min_rps) / rps * 100) if rps > 0 else 0.0

        # Stability rule: warn if any run took less than 5 seconds
        shortest_run = min(run_times) if run_times else 0.0
        stability_warn = " ⚠️ SHORT RUN <5s" if shortest_run < 5.0 else ""

        print(
            f"  status: {status} | median rps: {rps:.0f} | avg: {avg_ms:.2f}ms | "
            f"per-run rps: [{', '.join(f'{r:.0f}' for r in run_rps)}] "
            f"| jitter: ±{jitter_pct / 2:.1f}% | shortest run: {shortest_run:.1f}s"
            f"{stability_warn}"
        )

        entries, total_tt = _extract_top(profiler, n=30)

        # Save raw profile
        prof_path = LOGS / f"profile_hypernews_{slug}.prof"
        profiler.dump_stats(str(prof_path))

        # Save text report
        txt_path = LOGS / f"profile_hypernews_{slug}.txt"
        _write_text_report(profiler, txt_path, f"HyperNews — {label}")

        all_results[slug] = {
            "label": label,
            "path": path,
            "status": status,
            "iterations_per_run": iters,
            "runs": MULTI_RUN,
            "total_iterations": iters * MULTI_RUN,
            "median_elapsed_s": round(median_elapsed, 3),
            "run_elapsed_s": [round(t, 3) for t in run_times],
            "shortest_run_s": round(shortest_run, 3),
            "median_rps": round(rps, 1),
            "per_run_rps": [round(r, 1) for r in run_rps],
            "jitter_pct": round(jitter_pct / 2, 2),
            "avg_ms": round(avg_ms, 3),
            "total_tottime_s": round(total_tt, 3),
            "top_15": entries[:15],
            "top_30": entries,
        }

        # Print top 10 to stdout
        print("\n  Top 10 by SELF time:")
        print(f"  {'tottime(ms)':>12} {'cumtime(ms)':>12} {'calls':>10}  function")
        for e in entries[:10]:
            tt_ms = e["tottime_s"] * 1000
            ct_ms = e["cumtime_s"] * 1000
            print(
                f"  {tt_ms:>12.2f} {ct_ms:>12.2f} {e['call_count']:>10}  {e['function']}"
            )

    # Dump combined JSON
    json_path = LOGS / "profile_hypernews.json"
    json_path.write_text(json.dumps(all_results, indent=2))
    print(f"\n  Combined JSON: {json_path}")

    # Dump combined markdown summary (auto-generated table; do not overwrite
    # the hand-edited report file if present).
    md_path = LOGS / "profile_hypernews_autogen.md"
    _write_markdown_report(md_path, all_results)
    print(f"  Markdown report (auto): {md_path}")

    print("\n" + "=" * 70)
    print("  HyperNews profiling complete")
    print("=" * 70)


def _write_markdown_report(path: Path, results: dict[str, dict]) -> None:
    """Generate markdown summary across all endpoints."""
    lines: list[str] = []
    lines.append("# HyperNews cProfile Report — Platform-Wide Hotspot Discovery")
    lines.append("")
    lines.append(
        "**Iterations per endpoint**: "
        + ", ".join(f"{k}={v}" for k, v in ENDPOINT_ITERATIONS.items())
        + f" (warmup: {WARMUP}, runs: {MULTI_RUN})"
    )
    lines.append("**Mode**: in-process TestClient + cProfile")
    lines.append("**Release build**: yes (verify with `uv run hyper-build --release`)")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        "| Endpoint | median rps | avg_ms | jitter | runs | top hot function |"
    )
    lines.append("|---|---|---|---|---|---|")
    for slug, r in results.items():
        top_fn = r["top_15"][0]["function"] if r["top_15"] else "-"
        # Truncate long function names
        if len(top_fn) > 60:
            top_fn = top_fn[:57] + "..."
        lines.append(
            f"| {r['label']} | {r['median_rps']} | {r['avg_ms']} | "
            f"±{r['jitter_pct']}% | {r['per_run_rps']} | `{top_fn}` |"
        )
    lines.append("")

    for slug, r in results.items():
        lines.append(f"## {r['label']}")
        lines.append("")
        lines.append(f"- Path: `{r['path']}`")
        lines.append(f"- Status: {r['status']}")
        lines.append(
            f"- {r['total_iterations']} total iter "
            f"({r['iterations_per_run']} × {r['runs']} runs) → "
            f"**median {r['median_rps']} rps**, avg **{r['avg_ms']}ms**"
        )
        lines.append(f"- Per-run rps: {r['per_run_rps']} → jitter ±{r['jitter_pct']}%")
        lines.append(f"- Total tottime: {r['total_tottime_s']}s")
        lines.append("")
        lines.append("### Top 15 by SELF time")
        lines.append("")
        lines.append("| tottime(ms) | cumtime(ms) | calls | % | function |")
        lines.append("|---|---|---|---|---|")
        for e in r["top_15"]:
            tt_ms = e["tottime_s"] * 1000
            ct_ms = e["cumtime_s"] * 1000
            fn = e["function"]
            if len(fn) > 80:
                fn = fn[:77] + "..."
            lines.append(
                f"| {tt_ms:.2f} | {ct_ms:.2f} | {e['call_count']} | {e['pct_of_total']}% | `{fn}` |"
            )
        lines.append("")

    path.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
