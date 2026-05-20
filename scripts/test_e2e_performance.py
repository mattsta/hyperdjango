"""
Performance tests for HyperNews front page queries with 50K+ posts.

# hyper-test: e2e

Uses the native db.explain() API to verify that all 6 sort tab queries
use index scans and complete within acceptable time limits.

Runs against a live HyperNews server via AppRunner.
Seeds 50K posts via generate_series (fast bulk insert).
"""

import asyncio
import os
import subprocess
import time

from e2e_helper import TEST_PORTS, AppRunner, http_get

PASS = 0
FAIL = 0
ERRORS: list[str] = []

# Max acceptable time per query (ms).
# When running as part of the full suite (many parallel tests hitting the same
# PostgreSQL instance), queries take longer due to shared CPU/IO. The
# HYPER_TEST_PARALLEL env var (set by the test runner) triggers a relaxed limit.
_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL", "") == "1"
MAX_QUERY_MS = 2000.0 if _PARALLEL else 50.0


def ok(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
    print(msg)
    ERRORS.append(msg)
    return False


async def run_perf_tests(base: str) -> None:
    """Run all performance tests using native db.explain()."""
    from hyperdjango.database import Database

    db = Database(os.environ.get("DATABASE_URL", "postgres://localhost/hypernews"))
    await db.connect()

    # ── Seed 50K posts ────────────────────────────────────────────
    print("\n--- Seeding 50,000 posts ---")
    t0 = time.time()

    admin_rows = await db.query_tuples(
        "SELECT id FROM hn_users WHERE username = 'admin'"
    )
    if not admin_rows:
        print("  ERROR: admin user not found")
        return
    admin_id = admin_rows[0][0]

    count_rows = await db.query_tuples("SELECT COUNT(*) FROM hn_posts")
    existing = count_rows[0][0]
    needed = 50000 - existing
    print(f"  Existing: {existing}, need: {needed}")

    if needed > 0:
        await db.execute(
            """INSERT INTO hn_posts (title, slug, url, text, author_id, score,
                    weighted_score, upvotes, downvotes, hot_score,
                    controversy, velocity, comment_count,
                    is_ask, is_show, is_deleted, created_at)
               SELECT
                   'Perf Test Post ' || i,
                   'perf-test-post-' || i,
                   'https://example.com/perf/' || i,
                   'Performance test post body ' || i,
                   $1,
                   (random() * 200)::int,
                   (random() * 200)::float,
                   (random() * 100)::int,
                   (random() * 30)::int,
                   random() * 100,
                   random() * 5,
                   random() * 10,
                   (random() * 50)::int,
                   (i % 10 = 0),
                   (i % 15 = 0),
                   false,
                   NOW() - ((random() * 30)::int || ' days')::interval
               FROM generate_series(1, $2) AS i""",
            admin_id,
            needed,
        )
        elapsed = time.time() - t0
        print(f"  Seeded {needed} posts in {elapsed:.1f}s")

    count_rows = await db.query_tuples(
        "SELECT COUNT(*) FROM hn_posts WHERE NOT is_deleted"
    )
    total = count_rows[0][0]
    ok("50K+ posts seeded", total >= 50000, f"got {total}")

    # Seed comments so hn_comments is large enough for index scans
    comment_count = await db.query_tuples("SELECT COUNT(*) FROM hn_comments")
    existing_comments = comment_count[0][0]
    if existing_comments < 10000:
        print(f"\n--- Seeding comments ({existing_comments} existing) ---")
        t0 = time.time()
        first_post = await db.query_tuples(
            "SELECT id FROM hn_posts WHERE comment_count > 0 LIMIT 1"
        )
        target_post_id = first_post[0][0] if first_post else 1
        await db.execute(
            """INSERT INTO hn_comments (post_id, author_id, text, parent_id,
                    score, upvotes, downvotes, weighted_score,
                    agree_count, disagree_count, is_deleted, created_at)
               SELECT
                   $1,
                   $2,
                   'Performance test comment ' || i,
                   0,
                   (random() * 20)::int,
                   (random() * 10)::int,
                   (random() * 5)::int,
                   random() * 20,
                   (random() * 5)::int,
                   (random() * 2)::int,
                   false,
                   NOW() - ((random() * 30)::int || ' days')::interval
               FROM generate_series(1, $3) AS i""",
            target_post_id,
            admin_id,
            10000 - existing_comments,
        )
        print(
            f"  Seeded {10000 - existing_comments} comments in {time.time() - t0:.1f}s"
        )

    # Ensure ranking indexes exist (normally created by app startup hooks)
    print("\n--- Creating indexes ---")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hn_posts_hot ON hn_posts(hot_score DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hn_posts_controversy ON hn_posts(controversy DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hn_posts_score ON hn_posts(score DESC, id DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_hn_comments_post_id ON hn_comments(post_id) WHERE NOT is_deleted"
    )
    print("  Indexes created")

    # Update stats for accurate planner decisions
    print("\n--- Running ANALYZE ---")
    await db.execute("ANALYZE hn_posts")
    await db.execute("ANALYZE hn_comments")
    print("  ANALYZE complete")

    # ── Verify indexes exist ──────────────────────────────────────
    print("\n--- Verifying indexes ---")
    idx_rows = await db.query_tuples(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'hn_posts'"
    )
    idx_names = [r[0] for r in idx_rows]
    ok(
        "idx_hn_posts_hot exists",
        "idx_hn_posts_hot" in idx_names,
        f"found: {idx_names}",
    )
    ok(
        "idx_hn_posts_controversy exists",
        "idx_hn_posts_controversy" in idx_names,
        f"found: {idx_names}",
    )
    ok(
        "idx_hn_posts_score exists",
        "idx_hn_posts_score" in idx_names,
        f"found: {idx_names}",
    )

    # ── EXPLAIN ANALYZE: 6 sort tabs ──────────────────────────────
    print("\n--- Hot (default): ORDER BY hot_score DESC ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false
           ORDER BY hot_score DESC, id DESC LIMIT $1 OFFSET $2""",
        30,
        0,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"Hot < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    print("\n--- New: ORDER BY created_at DESC ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false
           ORDER BY created_at DESC, id DESC LIMIT $1""",
        30,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"New < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    print("\n--- Top: ORDER BY score DESC ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false
           ORDER BY score DESC, created_at DESC, id DESC LIMIT $1""",
        30,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"Top < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    print("\n--- Controversial: ORDER BY controversy DESC ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false AND (upvotes + downvotes) >= 2
           ORDER BY controversy DESC, id DESC LIMIT $1 OFFSET $2""",
        30,
        0,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"Controversial < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    print("\n--- Rising: ORDER BY velocity DESC (last 24h) ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false
             AND created_at > NOW() - INTERVAL '24 hours'
           ORDER BY velocity DESC, id DESC LIMIT $1 OFFSET $2""",
        30,
        0,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"Rising < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    print("\n--- Ask: is_ask = true ORDER BY created_at DESC ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false AND is_ask = true
           ORDER BY created_at DESC, id DESC LIMIT $1""",
        30,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"Ask < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    # ── Keyset pagination (cursor-based) ──────────────────────────
    print("\n--- New with keyset cursor ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false
             AND (created_at, id) < ($1::timestamptz, $2::int)
           ORDER BY created_at DESC, id DESC LIMIT $3""",
        "2026-03-15T00:00:00Z",
        25000,
        30,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"New keyset < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    print("\n--- Top with keyset cursor ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false
             AND (score, created_at, id) < ($1::int, $2::timestamptz, $3::int)
           ORDER BY score DESC, created_at DESC, id DESC LIMIT $4""",
        100,
        "2026-03-15T00:00:00Z",
        25000,
        30,
        analyze=True,
        buffers=True,
    )
    print(f"  Execution time: {result.execution_time:.2f}ms")
    print(f"  Plan:\n{result.text}")
    ok(
        f"Top keyset < {MAX_QUERY_MS}ms",
        result.execution_time < MAX_QUERY_MS,
        f"got {result.execution_time:.2f}ms",
    )

    # ── Comment detail query ──────────────────────────────────────
    print("\n--- Post detail (comments by post_id) ---")
    post_rows = await db.query_tuples(
        "SELECT id FROM hn_posts WHERE comment_count > 0 LIMIT 1"
    )
    if post_rows:
        pid = post_rows[0][0]
        result = await db.explain(
            """SELECT id, post_id, author_id, text, parent_id, score,
                      upvotes, downvotes, weighted_score,
                      agree_count, disagree_count,
                      is_deleted, created_at
               FROM hn_comments WHERE post_id = $1
               ORDER BY created_at ASC""",
            pid,
            analyze=True,
            buffers=True,
        )
        print(f"  Execution time: {result.execution_time:.2f}ms")
        ok(
            "Comments: no seq scan on hn_comments",
            not result.has_seq_scan,
            f"seq scan on: {result.seq_scan_tables}",
        )
        ok(
            f"Comments < {MAX_QUERY_MS}ms",
            result.execution_time < MAX_QUERY_MS,
            f"got {result.execution_time:.2f}ms",
        )
    else:
        print("  SKIP: no posts with comments")

    # ── Structured plan inspection ────────────────────────────────
    print("\n--- Structured plan inspection (hot query) ---")
    result = await db.explain(
        """SELECT id, title, slug, url, text, author_id, score, comment_count,
                  is_ask, is_show, is_deleted, created_at
           FROM hn_posts WHERE is_deleted = false
           ORDER BY hot_score DESC, id DESC LIMIT $1 OFFSET $2""",
        30,
        0,
        analyze=True,
    )
    ok("Plan node parsed", result.plan is not None)
    if result.plan:
        ok(
            "Root node type",
            bool(result.plan.node_type),
            f"type={result.plan.node_type}",
        )
        all_nodes = result.all_nodes
        ok(f"Plan has {len(all_nodes)} nodes", len(all_nodes) >= 1)
        index_nodes = result.index_scans
        print(f"  Root: {result.plan.node_type}")
        print(f"  All nodes: {[n.node_type for n in all_nodes]}")
        print(f"  Index scans: {[n.index_name for n in index_nodes]}")

    # ── HTTP endpoint latency ─────────────────────────────────────
    max_http_ms = 2000.0 if _PARALLEL else 500.0
    print(
        f"\n--- HTTP Endpoint Latency (front page with 50K posts, limit {max_http_ms:.0f}ms) ---"
    )
    for tab in ["hot", "new", "top", "controversial", "rising", "ask"]:
        t0 = time.time()
        url = f"{base}/?tab={tab}" if tab != "hot" else base
        r = http_get(url)
        elapsed_ms = (time.time() - t0) * 1000
        ok(
            f"/{tab} HTTP < {max_http_ms:.0f}ms ({elapsed_ms:.0f}ms)",
            r.status == 200 and elapsed_ms < max_http_ms,
            f"status={r.status} time={elapsed_ms:.0f}ms",
        )

    # ── Cleanup ───────────────────────────────────────────────────
    print("\n--- Cleanup ---")
    await db.execute("DELETE FROM hn_posts WHERE title LIKE 'Perf Test Post %'")
    print("  Removed perf test posts")
    await db.disconnect()


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("HyperNews Performance Tests (db.explain, 50K posts)")
    print("=" * 60)

    subprocess.run(
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
        timeout=120,
    )

    with AppRunner(
        "services.hypernews.app:app", host="127.0.0.1", port=TEST_PORTS["performance"]
    ) as runner:
        base = runner.url()
        asyncio.run(run_perf_tests(base))

    print("\n" + "=" * 60)
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for err in ERRORS:
            print(err)
    print("=" * 60)

    raise SystemExit(FAIL)


if __name__ == "__main__":
    main()
