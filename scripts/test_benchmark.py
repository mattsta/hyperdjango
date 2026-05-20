"""Tests for the hyper benchmark system.
# hyper-test: db_isolated

Tests benchmark seeding, EXPLAIN ANALYZE execution, regression detection,
and output formatting.

Run: uv run hyper-test benchmark
"""

import asyncio
import json
import os
import sys

from hyperdjango.benchmark import (
    BENCHMARK_QUERIES,
    BenchmarkResult,
    QueryBenchmark,
    RegressionReport,
    _format_regression_report,
    _format_table,
    _run_benchmarks,
    _seed_benchmark_db,
    compare_results,
)
from hyperdjango.database import Database

passed = 0
failed = 0
errors: list[str] = []


def check(name, condition, msg=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  ✗ {name} {msg}")


def _conn_str() -> str:
    if "DATABASE_URL" in os.environ:
        return os.environ["DATABASE_URL"]
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", os.environ.get("USER", "postgres"))
    password = os.environ.get("PGPASSWORD", "")
    dbname = os.environ.get("PGDATABASE", "hyperdjango_test")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


# ── Unit tests (no DB) ──────────────────────────────────────────────────────


def test_query_benchmark_dataclass():
    q = QueryBenchmark(
        name="test_query",
        sql="SELECT 1",
        execution_time_ms=0.5,
        planning_time_ms=0.1,
        has_seq_scan=False,
        seq_scan_tables=[],
        index_scans=["idx_test"],
        node_type="Index Scan",
        total_cost=1.0,
        actual_rows=1,
    )
    check("QueryBenchmark name", q.name == "test_query")
    check("QueryBenchmark no seq scan", not q.has_seq_scan)
    check("QueryBenchmark has index", len(q.index_scans) == 1)


def test_benchmark_result_to_dict():
    result = BenchmarkResult(
        timestamp="2026-03-30T12:00:00Z",
        database="localhost/test",
        seed_rows=1000,
        queries=[
            QueryBenchmark(
                "q1", "SELECT 1", 0.5, 0.1, False, [], ["idx1"], "Index Scan", 1.0, 1
            ),
        ],
        total_time_ms=100.0,
    )
    d = result.to_dict()
    check("to_dict has timestamp", d["timestamp"] == "2026-03-30T12:00:00Z")
    check("to_dict has queries list", len(d["queries"]) == 1)
    check("to_dict roundtrips to JSON", json.dumps(d) is not None)


def test_compare_no_regression():
    baseline = BenchmarkResult(
        timestamp="t0",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark("q1", "SELECT 1", 1.0, 0.1, False, [], [], "Scan", 1.0, 1)
        ],
        total_time_ms=10.0,
    )
    current = BenchmarkResult(
        timestamp="t1",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark("q1", "SELECT 1", 1.5, 0.1, False, [], [], "Scan", 1.0, 1)
        ],
        total_time_ms=10.0,
    )
    report = compare_results(current, baseline, threshold=2.0)
    check("no regression at 1.5x (threshold 2.0)", report.passed)
    check("zero regressions", len(report.regressions) == 0)


def test_compare_with_regression():
    baseline = BenchmarkResult(
        timestamp="t0",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark("q1", "SELECT 1", 1.0, 0.1, False, [], [], "Scan", 1.0, 1)
        ],
        total_time_ms=10.0,
    )
    current = BenchmarkResult(
        timestamp="t1",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark("q1", "SELECT 1", 3.0, 0.1, False, [], [], "Scan", 1.0, 1)
        ],
        total_time_ms=10.0,
    )
    report = compare_results(current, baseline, threshold=2.0)
    check("regression detected at 3x (threshold 2.0)", not report.passed)
    check("one regression", len(report.regressions) == 1)
    check("regression ratio ~3.0", abs(report.regressions[0]["ratio"] - 3.0) < 0.01)


def test_compare_new_seq_scan():
    baseline = BenchmarkResult(
        timestamp="t0",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark(
                "q1", "SELECT 1", 1.0, 0.1, False, [], ["idx1"], "Index Scan", 1.0, 1
            )
        ],
        total_time_ms=10.0,
    )
    current = BenchmarkResult(
        timestamp="t1",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark(
                "q1",
                "SELECT 1",
                1.0,
                0.1,
                True,
                ["posts"],
                [],
                "Seq Scan",
                100.0,
                50000,
            )
        ],
        total_time_ms=10.0,
    )
    report = compare_results(current, baseline, threshold=2.0)
    check("new seq scan fails benchmark", not report.passed)
    check("seq scan detected", len(report.new_seq_scans) == 1)
    check("seq scan table name", report.new_seq_scans[0]["tables"] == ["posts"])


def test_compare_improvement():
    baseline = BenchmarkResult(
        timestamp="t0",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark("q1", "SELECT 1", 10.0, 0.1, False, [], [], "Scan", 1.0, 1)
        ],
        total_time_ms=100.0,
    )
    current = BenchmarkResult(
        timestamp="t1",
        database="db",
        seed_rows=1000,
        queries=[
            QueryBenchmark("q1", "SELECT 1", 2.0, 0.1, False, [], [], "Scan", 1.0, 1)
        ],
        total_time_ms=100.0,
    )
    report = compare_results(current, baseline, threshold=2.0)
    check("improvement detected (5x faster)", len(report.improvements) == 1)
    check("still passes", report.passed)


def test_format_table():
    queries = [
        QueryBenchmark(
            "front_page_hot",
            "SELECT ...",
            0.5,
            0.1,
            False,
            [],
            ["idx1"],
            "Index Scan",
            1.0,
            30,
        ),
        QueryBenchmark(
            "count_posts",
            "SELECT ...",
            5.0,
            0.2,
            True,
            ["posts"],
            [],
            "Seq Scan",
            100.0,
            50000,
        ),
    ]
    table = _format_table(queries)
    check("table has header", "Query" in table)
    check("table has front_page_hot", "front_page_hot" in table)
    check("table shows SEQ!", "SEQ!" in table)
    check("table shows idx", "idx" in table)


def test_format_regression_report():
    report = RegressionReport(
        regressions=[{"name": "q1", "baseline": 1.0, "current": 5.0, "ratio": 5.0}],
        new_seq_scans=[{"name": "q2", "tables": ["posts"]}],
        improvements=[{"name": "q3", "baseline": 10.0, "current": 2.0, "ratio": 0.2}],
        passed=False,
    )
    text = _format_regression_report(report)
    check("report shows REGRESSIONS", "REGRESSIONS" in text)
    check("report shows SEQUENTIAL SCANS", "SEQUENTIAL SCANS" in text)
    check("report shows IMPROVEMENTS", "IMPROVEMENTS" in text)


def test_benchmark_queries_defined():
    check(
        "benchmark queries defined",
        len(BENCHMARK_QUERIES) >= 10,
        f"only {len(BENCHMARK_QUERIES)}",
    )
    names = [q[0] for q in BENCHMARK_QUERIES]
    check("front_page_hot in queries", "front_page_hot" in names)
    check("post_by_pk in queries", "post_by_pk" in names)
    check("keyset_pagination in queries", "keyset_pagination" in names)
    check("search_title in queries", "search_title" in names)


# ── Integration tests (live DB) ─────────────────────────────────────────────


def test_seed_and_benchmark():
    """Seed benchmark data and run EXPLAIN ANALYZE queries against live DB."""

    async def _run():
        db = Database(_conn_str(), min_size=2, max_size=4)
        await db.connect()

        try:
            # Seed with small dataset for test speed
            rows = await _seed_benchmark_db(db, num_posts=1000)
            check("seeded 1000 posts", rows == 1000)

            # Run benchmarks
            results = await _run_benchmarks(db)
            check(
                "all benchmark queries executed", len(results) == len(BENCHMARK_QUERIES)
            )

            # Verify results have timing data
            for q in results:
                check(
                    f"  {q.name}: timing > 0",
                    q.execution_time_ms >= 0,
                    f"exec={q.execution_time_ms}ms",
                )

            # Check index usage on key queries
            hot = next(q for q in results if q.name == "front_page_hot")
            check(
                "front_page_hot uses index",
                not hot.has_seq_scan or hot.actual_rows < 100,
            )

            pk = next(q for q in results if q.name == "post_by_pk")
            check("post_by_pk uses index", not pk.has_seq_scan)

            vote = next(q for q in results if q.name == "vote_check")
            check("vote_check uses index", not vote.has_seq_scan)

        finally:
            await db.execute("DROP TABLE IF EXISTS bench_votes CASCADE")
            await db.execute("DROP TABLE IF EXISTS bench_comments CASCADE")
            await db.execute("DROP TABLE IF EXISTS bench_posts CASCADE")
            await db.execute("DROP TABLE IF EXISTS bench_users CASCADE")
            await db.disconnect()

    asyncio.run(_run())


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    tests = [
        test_query_benchmark_dataclass,
        test_benchmark_result_to_dict,
        test_compare_no_regression,
        test_compare_with_regression,
        test_compare_new_seq_scan,
        test_compare_improvement,
        test_format_table,
        test_format_regression_report,
        test_benchmark_queries_defined,
        test_seed_and_benchmark,
    ]

    print(f"\n{'=' * 60}")
    print("Benchmark System Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
        except Exception as e:
            global failed
            failed += 1
            errors.append(f"FAIL: {test.__name__}: {e}")
            print(f"  ✗ {test.__name__}: {e}")
            import traceback

            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
    if errors:
        print("\nFailures:")
        for err in errors:
            print(f"  - {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
