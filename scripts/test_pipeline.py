#!/usr/bin/env python3
"""Test connection pipelining and DataLoader.

Tests split into two groups:
1. DataLoader unit tests (no database needed)
2. Pipeline integration tests (requires PostgreSQL)

Run: uv run hyper-test pipeline
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import time


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── DataLoader unit tests ─────────────────────────────────────────────
    print("\n=== DataLoader unit tests ===")

    from hyperdjango.dataloader import DataLoader

    # Test 1: Basic batching
    batch_calls = []

    async def mock_batch(keys):
        batch_calls.append(list(keys))
        return [f"result_{k}" for k in keys]

    async def test_basic_batch():
        loader = DataLoader(batch_fn=mock_batch)
        r1, r2, r3 = await loader.load_many([1, 2, 3])
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(test_basic_batch())
    check(
        "batch load_many",
        r1 == "result_1" and r2 == "result_2" and r3 == "result_3",
        f"got {r1}, {r2}, {r3}",
    )
    check(
        "batch called once", len(batch_calls) == 1, f"called {len(batch_calls)} times"
    )
    check("batch all keys", batch_calls[0] == [1, 2, 3], f"keys: {batch_calls[0]}")

    # Test 2: Cache
    batch_calls.clear()

    async def test_cache():
        loader = DataLoader(batch_fn=mock_batch)
        r1 = await loader.load(1)
        r2 = await loader.load(1)  # should be cached
        return r1, r2

    r1, r2 = asyncio.run(test_cache())
    check("cache hit", r1 == r2 == "result_1", f"r1={r1}, r2={r2}")

    # Test 3: Clear cache
    async def test_clear():
        loader = DataLoader(batch_fn=mock_batch)
        await loader.load(1)
        loader.clear(1)
        return 1 not in loader._cache

    check("cache clear", asyncio.run(test_clear()))

    # Test 4: Prime cache
    async def test_prime():
        loader = DataLoader(batch_fn=mock_batch)
        loader.prime(99, "pre-loaded")
        result = await loader.load(99)
        return result

    check("prime cache", asyncio.run(test_prime()) == "pre-loaded")

    # Test 5: No cache mode
    no_cache_calls = []

    async def mock_no_cache(keys):
        no_cache_calls.append(list(keys))
        return [f"val_{k}" for k in keys]

    async def test_no_cache():
        loader = DataLoader(batch_fn=mock_no_cache, cache_enabled=False)
        r1 = await loader.load(1)
        return r1

    check("no cache mode", asyncio.run(test_no_cache()) == "val_1")

    # Test 6: Error handling
    async def failing_batch(keys):
        raise ValueError("batch failed")

    async def test_error():
        loader = DataLoader(batch_fn=failing_batch)
        try:
            await loader.load(1)
            return False
        except ValueError:
            return True

    check("error propagation", asyncio.run(test_error()))

    # Test 7: Max batch size
    big_batch_calls = []

    async def big_mock_batch(keys):
        big_batch_calls.append(len(keys))
        return [f"r_{k}" for k in keys]

    async def test_max_batch():
        loader = DataLoader(batch_fn=big_mock_batch, max_batch_size=5)
        results = await loader.load_many(list(range(12)))
        return len(results), results[0], results[11]

    n, first, last = asyncio.run(test_max_batch())
    check("max batch size", n == 12 and first == "r_0" and last == "r_11", f"n={n}")

    # ── Pipeline integration tests (requires PostgreSQL) ──────────────────
    print("\n=== Pipeline integration tests ===")

    try:
        from hyperdjango._hyperdjango_native import _db_configure, _db_pipeline

        # Configure database connection (URI format: postgres://user@host:port/dbname)
        host = os.environ.get("PGHOST", "localhost")
        port = os.environ.get("PGPORT", "5432")
        user = os.environ.get("PGUSER", os.environ.get("USER", "postgres"))
        password = os.environ.get("PGPASSWORD", "")
        dbname = os.environ.get("PGDATABASE", "postgres")
        if password:
            conn_str = f"postgres://{user}:{password}@{host}:{port}/{dbname}"
        else:
            conn_str = f"postgres://{user}@{host}:{port}/{dbname}"
        _db_configure(conn_str, 4)

        # Test: pipeline with simple queries
        results = _db_pipeline(
            0,
            [
                "SELECT 1 AS a",
                "SELECT 2 AS b",
                "SELECT 3 AS c",
            ],
        )
        check("pipeline 3 queries", len(results) == 3, f"got {len(results)} results")
        check(
            "pipeline result 1",
            len(results[0]) == 1 and results[0][0][0] == 1,
            f"got {results[0]}",
        )
        check(
            "pipeline result 2",
            len(results[1]) == 1 and results[1][0][0] == 2,
            f"got {results[1]}",
        )
        check(
            "pipeline result 3",
            len(results[2]) == 1 and results[2][0][0] == 3,
            f"got {results[2]}",
        )

        # Test: empty pipeline
        results = _db_pipeline(0, [])
        check("empty pipeline", results == [] or len(results) == 0, f"got {results}")

        # Test: pipeline with multiple rows
        results = _db_pipeline(
            0,
            [
                "SELECT generate_series(1, 3) AS n",
            ],
        )
        check("multi-row result", len(results[0]) == 3, f"got {len(results[0])} rows")

        # Test: pipeline with mixed types
        results = _db_pipeline(
            0,
            [
                "SELECT 42 AS int_val, 'hello' AS str_val, true AS bool_val",
            ],
        )
        row = results[0][0]
        check("int type in pipeline", row[0] == 42, f"got {row[0]} ({type(row[0])})")
        check("str type in pipeline", row[1] == "hello", f"got {row[1]}")
        check("bool type in pipeline", row[2] is True, f"got {row[2]}")

        # Timing comparison: pipeline vs sequential. INFORMATIONAL ONLY — a
        # wall-clock speedup is a benchmark property, not a correctness
        # contract: over a localhost socket the per-query cost is microseconds,
        # so on a loaded few-core CI runner scheduler noise dominates and the
        # comparison inverts spuriously (observed 0.84x on a 2-core arm runner
        # under the parallel suite while the same build measures >2x quiet).
        # The CONTRACT asserted here is result fidelity: the pipelined batch
        # returns exactly what the same queries return sequentially.
        N_QUERIES = 20
        queries = [f"SELECT {i}" for i in range(N_QUERIES)]

        start = time.perf_counter()
        for _ in range(100):
            pipeline_results = _db_pipeline(0, queries)
        pipeline_time = (time.perf_counter() - start) / 100

        from hyperdjango._hyperdjango_native import _db_query

        start = time.perf_counter()
        for _ in range(100):
            sequential_results = [_db_query(0, q, []) for q in queries]
        sequential_time = (time.perf_counter() - start) / 100

        speedup = sequential_time / pipeline_time
        print(f"\n  Pipeline ({N_QUERIES} queries): {pipeline_time * 1000:.2f} ms")
        print(f"  Sequential ({N_QUERIES} queries): {sequential_time * 1000:.2f} ms")
        print(f"  Speedup: {speedup:.2f}x (informational)")

        check(
            "pipeline results identical to sequential",
            pipeline_results == sequential_results,
            f"pipeline={pipeline_results[:3]}... sequential={sequential_results[:3]}...",
        )

    except Exception as e:
        print(f"  SKIP: PostgreSQL not available ({e})")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All pipeline/DataLoader tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
