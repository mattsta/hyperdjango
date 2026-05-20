"""
Performance benchmark suite with EXPLAIN ANALYZE regression detection.

Runs key queries against a seeded database, captures execution plans,
and compares against a baseline to detect performance regressions.

Usage:
    hyper benchmark                    # Run all benchmarks
    hyper benchmark --save-baseline    # Save current results as baseline
    hyper benchmark --json             # JSON output for CI
    hyper benchmark --database URL     # Custom database URL
    hyper benchmark --threshold 2.0    # Regression threshold multiplier (default 2x)
"""

import asyncio
import json as _stdlib_json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path

from hyperdjango.conf import resolve_database_url
from hyperdjango.database import Database
from hyperdjango.logging import logger
from hyperdjango.native import fast_json_loads

BASELINE_FILE = Path(".hyper.benchmark.json")
BENCHMARK_DB = "hyper_benchmark"

# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class QueryBenchmark:
    """Result of benchmarking a single query."""

    name: str
    sql: str
    execution_time_ms: float
    planning_time_ms: float
    has_seq_scan: bool
    seq_scan_tables: list[str]
    index_scans: list[str]
    node_type: str
    total_cost: float
    actual_rows: int


@dataclass
class BenchmarkResult:
    """Full benchmark suite result."""

    timestamp: str
    database: str
    seed_rows: int
    queries: list[QueryBenchmark]
    total_time_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "database": self.database,
            "seed_rows": self.seed_rows,
            "total_time_ms": self.total_time_ms,
            "queries": [asdict(q) for q in self.queries],
        }


@dataclass
class RegressionReport:
    """Comparison between current and baseline results."""

    regressions: list[dict[str, object]] = field(default_factory=list)
    improvements: list[dict[str, object]] = field(default_factory=list)
    new_seq_scans: list[dict[str, object]] = field(default_factory=list)
    passed: bool = True


# ── Benchmark queries ────────────────────────────────────────────────────────

# These represent the critical query patterns in a production HyperDjango app.
# Each tuple: (name, sql, params)
BENCHMARK_QUERIES: list[tuple[str, str, list[object]]] = [
    (
        "front_page_hot",
        "SELECT id, title, score, hot_score, created_at FROM bench_posts "
        "ORDER BY hot_score DESC LIMIT 30",
        [],
    ),
    (
        "front_page_new",
        "SELECT id, title, score, created_at FROM bench_posts "
        "ORDER BY created_at DESC LIMIT 30",
        [],
    ),
    (
        "front_page_top",
        "SELECT id, title, score, created_at FROM bench_posts "
        "ORDER BY score DESC LIMIT 30",
        [],
    ),
    (
        "front_page_controversial",
        "SELECT id, title, score, controversy, upvotes, downvotes FROM bench_posts "
        "WHERE (upvotes + downvotes) >= 5 ORDER BY controversy DESC LIMIT 30",
        [],
    ),
    (
        "front_page_rising",
        "SELECT id, title, score, velocity, created_at FROM bench_posts "
        "WHERE created_at > NOW() - INTERVAL '24 hours' ORDER BY velocity DESC LIMIT 30",
        [],
    ),
    (
        "post_by_pk",
        "SELECT * FROM bench_posts WHERE id = $1",
        [1],
    ),
    (
        "post_comments",
        "SELECT c.id, c.text, c.score, c.created_at FROM bench_comments c "
        "WHERE c.post_id = $1 ORDER BY c.score DESC LIMIT 50",
        [1],
    ),
    (
        "user_posts",
        "SELECT id, title, score FROM bench_posts WHERE author_id = $1 "
        "ORDER BY created_at DESC LIMIT 20",
        [1],
    ),
    (
        "vote_check",
        "SELECT id, value FROM bench_votes WHERE user_id = $1 AND post_id = $2",
        [1, 1],
    ),
    (
        "keyset_pagination",
        "SELECT id, title, score, hot_score FROM bench_posts "
        "WHERE hot_score < $1 OR (hot_score = $1 AND id < $2) "
        "ORDER BY hot_score DESC, id DESC LIMIT 30",
        [5.0, 10000],
    ),
    (
        "count_posts",
        "SELECT COUNT(*) FROM bench_posts",
        [],
    ),
    (
        "search_title",
        "SELECT id, title, score FROM bench_posts "
        "WHERE title ILIKE $1 ORDER BY score DESC LIMIT 20",
        ["%benchmark%"],
    ),
    (
        "aggregate_scores",
        "SELECT author_id, COUNT(*) as post_count, SUM(score) as total_score, "
        "AVG(score) as avg_score FROM bench_posts GROUP BY author_id "
        "ORDER BY total_score DESC LIMIT 10",
        [],
    ),
    (
        "join_post_author",
        "SELECT p.id, p.title, p.score, u.username FROM bench_posts p "
        "JOIN bench_users u ON u.id = p.author_id "
        "ORDER BY p.hot_score DESC LIMIT 30",
        [],
    ),
]


# ── Seed data ────────────────────────────────────────────────────────────────


async def _seed_benchmark_db(db: Database, num_posts: int = 50000) -> int:
    """Seed benchmark tables with realistic data. Returns row count."""

    # Drop and recreate
    await db.execute("DROP TABLE IF EXISTS bench_votes CASCADE")
    await db.execute("DROP TABLE IF EXISTS bench_comments CASCADE")
    await db.execute("DROP TABLE IF EXISTS bench_posts CASCADE")
    await db.execute("DROP TABLE IF EXISTS bench_users CASCADE")

    await db.execute("""
        CREATE TABLE bench_users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) NOT NULL,
            karma INTEGER DEFAULT 0
        )
    """)

    await db.execute("""
        CREATE TABLE bench_posts (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT DEFAULT '',
            text TEXT DEFAULT '',
            author_id INTEGER REFERENCES bench_users(id) ON DELETE CASCADE,
            score INTEGER DEFAULT 0,
            weighted_score DOUBLE PRECISION DEFAULT 0,
            upvotes INTEGER DEFAULT 0,
            downvotes INTEGER DEFAULT 0,
            hot_score DOUBLE PRECISION DEFAULT 0,
            controversy DOUBLE PRECISION DEFAULT 0,
            velocity DOUBLE PRECISION DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            is_ask BOOLEAN DEFAULT false,
            is_show BOOLEAN DEFAULT false,
            is_deleted BOOLEAN DEFAULT false,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    await db.execute("""
        CREATE TABLE bench_comments (
            id SERIAL PRIMARY KEY,
            post_id INTEGER REFERENCES bench_posts(id) ON DELETE CASCADE,
            author_id INTEGER REFERENCES bench_users(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            score INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    await db.execute("""
        CREATE TABLE bench_votes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            post_id INTEGER,
            comment_id INTEGER,
            value SMALLINT NOT NULL DEFAULT 1
        )
    """)

    # Seed users
    await db.execute("""
        INSERT INTO bench_users (username, karma)
        SELECT 'user_' || i, (random() * 500)::int
        FROM generate_series(1, 1000) AS i
    """)

    # Seed posts with realistic distributions
    await db.execute(f"""
        INSERT INTO bench_posts (title, author_id, score, upvotes, downvotes,
            hot_score, controversy, velocity, comment_count, is_ask, created_at)
        SELECT
            'Benchmark Post ' || i || ' — ' || md5(i::text),
            1 + (random() * 999)::int,
            (random() * 200 - 20)::int,
            (random() * 150)::int,
            (random() * 50)::int,
            (random() * 100)::float,
            random() * 5,
            random() * 20,
            (random() * 50)::int,
            random() < 0.1,
            NOW() - ((random() * 30)::int || ' days')::interval
        FROM generate_series(1, {num_posts}) AS i
    """)

    # Update weighted_score
    await db.execute("""
        UPDATE bench_posts SET weighted_score = score * (1 + random() * 0.5)
    """)

    # Seed comments (5 per post average)
    num_comments = num_posts * 5
    await db.execute(f"""
        INSERT INTO bench_comments (post_id, author_id, text, score, created_at)
        SELECT
            1 + (random() * {num_posts - 1})::int,
            1 + (random() * 999)::int,
            'Comment ' || i || ' on benchmark post',
            (random() * 50 - 5)::int,
            NOW() - ((random() * 30)::int || ' days')::interval
        FROM generate_series(1, {num_comments}) AS i
    """)

    # Seed votes (4 per post average)
    num_votes = num_posts * 4
    await db.execute(f"""
        INSERT INTO bench_votes (user_id, post_id, value)
        SELECT
            1 + (random() * 999)::int,
            1 + (random() * {num_posts - 1})::int,
            CASE WHEN random() > 0.3 THEN 1 ELSE -1 END
        FROM generate_series(1, {num_votes}) AS i
    """)

    # Create indexes (matches production schema)
    await db.execute(
        "CREATE INDEX idx_bench_posts_hot ON bench_posts (hot_score DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX idx_bench_posts_new ON bench_posts (created_at DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX idx_bench_posts_top ON bench_posts (score DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX idx_bench_posts_controversy ON bench_posts (controversy DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX idx_bench_posts_velocity ON bench_posts (velocity DESC) WHERE NOT is_deleted"
    )
    await db.execute(
        "CREATE INDEX idx_bench_posts_author ON bench_posts (author_id, created_at DESC)"
    )
    await db.execute(
        "CREATE INDEX idx_bench_comments_post ON bench_comments (post_id, score DESC)"
    )
    await db.execute(
        "CREATE INDEX idx_bench_votes_user_post ON bench_votes (user_id, post_id)"
    )

    # ANALYZE for fresh statistics
    await db.execute("ANALYZE bench_users")
    await db.execute("ANALYZE bench_posts")
    await db.execute("ANALYZE bench_comments")
    await db.execute("ANALYZE bench_votes")

    return num_posts


# ── Benchmark runner ─────────────────────────────────────────────────────────


async def _run_benchmarks(db: Database) -> list[QueryBenchmark]:
    """Run all benchmark queries with EXPLAIN ANALYZE."""
    results: list[QueryBenchmark] = []

    for name, sql, params in BENCHMARK_QUERIES:
        result = await db.explain(sql, *params, analyze=True, buffers=True)

        index_names = [n.index_name for n in result.index_scans if n.index_name]

        results.append(
            QueryBenchmark(
                name=name,
                sql=sql,
                execution_time_ms=result.execution_time,
                planning_time_ms=result.planning_time,
                has_seq_scan=result.has_seq_scan,
                seq_scan_tables=result.seq_scan_tables,
                index_scans=index_names,
                node_type=result.plan.node_type if result.plan else "unknown",
                total_cost=result.plan.total_cost if result.plan else 0.0,
                actual_rows=result.plan.actual_rows if result.plan else 0,
            )
        )

    return results


# ── Regression detection ─────────────────────────────────────────────────────


def compare_results(
    current: BenchmarkResult,
    baseline: BenchmarkResult,
    threshold: float = 2.0,
) -> RegressionReport:
    """Compare current results against baseline. Returns regression report."""
    report = RegressionReport()

    baseline_map = {q["name"]: q for q in [asdict(q) for q in baseline.queries]}

    for query in current.queries:
        qd = asdict(query)
        base = baseline_map.get(query.name)
        if not base:
            continue

        # Timing regression: current > baseline * threshold
        if (
            base["execution_time_ms"] > 0
            and query.execution_time_ms > base["execution_time_ms"] * threshold
        ):
            report.regressions.append(
                {
                    "name": query.name,
                    "metric": "execution_time_ms",
                    "baseline": base["execution_time_ms"],
                    "current": query.execution_time_ms,
                    "ratio": query.execution_time_ms / base["execution_time_ms"],
                }
            )
            report.passed = False

        # Timing improvement
        elif (
            base["execution_time_ms"] > 0
            and query.execution_time_ms < base["execution_time_ms"] * 0.5
        ):
            report.improvements.append(
                {
                    "name": query.name,
                    "metric": "execution_time_ms",
                    "baseline": base["execution_time_ms"],
                    "current": query.execution_time_ms,
                    "ratio": query.execution_time_ms / base["execution_time_ms"],
                }
            )

        # New seq scan that wasn't there before
        if query.has_seq_scan and not base["has_seq_scan"]:
            report.new_seq_scans.append(
                {
                    "name": query.name,
                    "tables": query.seq_scan_tables,
                }
            )
            report.passed = False

    return report


# ── Output formatting ────────────────────────────────────────────────────────


def _format_table(results: list[QueryBenchmark]) -> str:
    """Format results as a human-readable table."""
    lines: list[str] = []
    lines.append(
        f"{'Query':<30} {'Exec (ms)':>10} {'Plan (ms)':>10} {'Rows':>8} {'Scan':>10} {'Index':>6}"
    )
    lines.append("-" * 80)

    for q in results:
        scan = "SEQ!" if q.has_seq_scan else "idx"
        idx_count = len(q.index_scans)
        lines.append(
            f"{q.name:<30} {q.execution_time_ms:>10.3f} {q.planning_time_ms:>10.3f} "
            f"{q.actual_rows:>8} {scan:>10} {idx_count:>6}"
        )

    return "\n".join(lines)


def _format_regression_report(report: RegressionReport) -> str:
    """Format regression report for terminal output."""
    lines: list[str] = []

    if report.regressions:
        lines.append("\nREGRESSIONS DETECTED:")
        for r in report.regressions:
            lines.append(
                f"  {r['name']}: {r['baseline']:.3f}ms → {r['current']:.3f}ms "
                f"({r['ratio']:.1f}x slower)"
            )

    if report.new_seq_scans:
        lines.append("\nNEW SEQUENTIAL SCANS:")
        for s in report.new_seq_scans:
            lines.append(f"  {s['name']}: seq scan on {', '.join(s['tables'])}")

    if report.improvements:
        lines.append("\nIMPROVEMENTS:")
        for i in report.improvements:
            lines.append(
                f"  {i['name']}: {i['baseline']:.3f}ms → {i['current']:.3f}ms "
                f"({i['ratio']:.2f}x)"
            )

    if report.passed:
        lines.append("\nAll benchmarks within threshold.")

    return "\n".join(lines)


# ── Main entry point ─────────────────────────────────────────────────────────


async def _async_benchmark(args) -> int:
    """Run benchmark suite. Returns exit code (0 = pass, 1 = regression)."""

    # args.database wins; otherwise the single connection-URL authority
    # (DATABASE_URL / HYPER_DATABASE_URL / PG*), then a local benchmark default
    # whose auth is filled by the connection layer.
    db_url = (
        args.database
        or resolve_database_url()
        or f"postgres://localhost/{BENCHMARK_DB}"
    )
    save_baseline = args.save_baseline
    json_output = args.json
    threshold = args.threshold
    num_posts = args.posts

    logger.info("HyperDjango Benchmark Suite")
    logger.opt(raw=True).info(f"{'=' * 60}\n")
    logger.info("Database: {db_url}", db_url=db_url)
    logger.info("Posts: {num_posts}", num_posts=f"{num_posts:,}")
    logger.info("Threshold: {threshold}x", threshold=threshold)

    # Connect
    db = Database(db_url, min_size=2, max_size=4)
    await db.connect()

    try:
        # Seed
        logger.info("Seeding benchmark data...")
        t0 = time.perf_counter()
        rows = await _seed_benchmark_db(db, num_posts)
        seed_time = (time.perf_counter() - t0) * 1000
        logger.info(
            "Seeded {rows} posts + 250K comments + 200K votes in {seed_time}ms",
            rows=f"{rows:,}",
            seed_time=f"{seed_time:.0f}",
        )

        # Run benchmarks
        logger.info("Running EXPLAIN ANALYZE benchmarks...")
        t0 = time.perf_counter()
        queries = await _run_benchmarks(db)
        total_time = (time.perf_counter() - t0) * 1000
        logger.info(
            "Completed {count} queries in {total_time}ms",
            count=len(queries),
            total_time=f"{total_time:.0f}",
        )

        # Build result
        from datetime import datetime

        result = BenchmarkResult(
            timestamp=datetime.now(UTC).isoformat(),
            database=db_url.split("@")[-1] if "@" in db_url else db_url,
            seed_rows=rows,
            queries=queries,
            total_time_ms=total_time,
        )

        # Display results
        for line in _format_table(queries).split("\n"):
            logger.opt(raw=True).info(f"{line}\n")

        # Seq scan warnings
        seq_scans = [q for q in queries if q.has_seq_scan]
        if seq_scans:
            logger.warning("Sequential scans: {count}", count=len(seq_scans))
            for q in seq_scans:
                logger.warning(
                    "  {name}: {tables}",
                    name=q.name,
                    tables=", ".join(q.seq_scan_tables),
                )

        # Save baseline
        if save_baseline:
            BASELINE_FILE.write_text(_stdlib_json.dumps(result.to_dict(), indent=2))
            logger.success("Baseline saved to {path}", path=BASELINE_FILE)
            return 0

        # JSON output
        if json_output:
            logger.opt(raw=True).info(
                f"{_stdlib_json.dumps(result.to_dict(), indent=2)}\n"
            )

        # Compare against baseline
        if BASELINE_FILE.exists():
            baseline_data = fast_json_loads(BASELINE_FILE.read_text())
            baseline = BenchmarkResult(
                timestamp=baseline_data["timestamp"],
                database=baseline_data["database"],
                seed_rows=baseline_data["seed_rows"],
                total_time_ms=baseline_data["total_time_ms"],
                queries=[QueryBenchmark(**q) for q in baseline_data["queries"]],
            )

            report = compare_results(result, baseline, threshold)
            for line in _format_regression_report(report).split("\n"):
                logger.opt(raw=True).info(f"{line}\n")

            if not report.passed:
                logger.error(
                    "BENCHMARK FAILED — {regressions} regressions, {seq_scans} new seq scans",
                    regressions=len(report.regressions),
                    seq_scans=len(report.new_seq_scans),
                )
                return 1
            logger.success("BENCHMARK PASSED")
            return 0

        logger.info("No baseline found. Run with --save-baseline to create one.")
        return 0

    finally:
        # Cleanup benchmark tables
        await db.execute("DROP TABLE IF EXISTS bench_votes CASCADE")
        await db.execute("DROP TABLE IF EXISTS bench_comments CASCADE")
        await db.execute("DROP TABLE IF EXISTS bench_posts CASCADE")
        await db.execute("DROP TABLE IF EXISTS bench_users CASCADE")
        await db.disconnect()


def run_benchmark(args) -> None:
    """CLI entry point for hyper benchmark."""
    exit_code = asyncio.run(_async_benchmark(args))
    sys.exit(exit_code)
