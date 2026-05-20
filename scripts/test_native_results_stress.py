"""
Stress tests for native result building — memory, cache bounds, scalability.

Tests:
1. Cache eviction: interned key cache + JSON key cache bounded at COLUMN_CACHE_MAX
2. Memory stability: 10K unique queries don't cause unbounded growth
3. Large result sets: 10K+ rows work correctly
4. Concurrent-safe: multiple query patterns interleaved
5. Edge cases: empty results, NULL-heavy, wide tables, long strings

Run: uv run hyper-test native_results_stress
"""

# hyper-test: db_isolated

import asyncio
import json
import os
import resource
import sys
import time

passed = 0
failed = 0
errors: list[str] = []


def check(name, condition, msg=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  ✗ {name} {msg}")


def get_rss_mb():
    """Get current resident set size in MB."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


async def run_stress_tests():
    from hyperdjango._hyperdjango_native import (
        _db_query as _native_query,
    )
    from hyperdjango._hyperdjango_native import (
        _db_query_dicts as _native_query_dicts,
    )
    from hyperdjango._hyperdjango_native import (
        _db_query_json as _native_query_json,
    )

    from hyperdjango.database import Database, set_db

    DB_URL = os.environ.get(
        "DATABASE_URL", "postgres://localhost:5432/hyperdjango_test"
    )
    db = Database(DB_URL, max_size=5)

    try:
        await db.connect()
    except Exception as e:
        print(f"  Cannot connect to DB: {e}")
        return

    set_db(db)

    try:
        # Setup
        await db.execute("DROP TABLE IF EXISTS stress_test CASCADE")
        await db.execute("""
            CREATE TABLE stress_test (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                value INTEGER NOT NULL,
                description TEXT,
                active BOOLEAN DEFAULT true,
                score DOUBLE PRECISION DEFAULT 0.0
            )
        """)

        # ── Test 1: Large result set (10K rows) ──
        print("\n── Large Result Sets ──")
        await db.execute_many(
            "INSERT INTO stress_test (name, value, description, active, score) VALUES ($1, $2, $3, $4, $5)",
            [
                (
                    f"item_{i}",
                    i,
                    f"Description for item {i} with some padding text to make it realistic",
                    i % 2 == 0,
                    i * 0.1,
                )
                for i in range(10000)
            ],
        )

        count = await db.query_val("SELECT COUNT(*) FROM stress_test")
        check("10K rows inserted", count == 10000)

        # Query all 10K as dicts
        start = time.perf_counter()
        rows = await db.query(
            "SELECT id, name, value, description, active, score FROM stress_test ORDER BY id"
        )
        dict_time = time.perf_counter() - start
        check("10K dict query returns all rows", len(rows) == 10000)
        check("10K dict first row correct", rows[0]["name"] == "item_0")
        check("10K dict last row correct", rows[9999]["name"] == "item_9999")
        check(
            "10K dict types correct",
            isinstance(rows[0]["id"], int)
            and isinstance(rows[0]["active"], bool)
            and isinstance(rows[0]["score"], float),
        )
        print(f"  ℹ 10K rows as dicts: {dict_time * 1000:.1f}ms")

        # Query all 10K as JSON
        start = time.perf_counter()
        json_bytes = _native_query_json(
            db._pool_handle,
            "SELECT id, name, value, description, active, score FROM stress_test ORDER BY id",
            [],
        )
        json_time = time.perf_counter() - start
        parsed = json.loads(json_bytes)
        check("10K JSON query returns all rows", len(parsed) == 10000)
        check("10K JSON first row", parsed[0]["name"] == "item_0")
        check("10K JSON last row", parsed[9999]["name"] == "item_9999")
        check("10K JSON bool type", parsed[0]["active"] is True)
        check("10K JSON float type", isinstance(parsed[0]["score"], float))
        print(f"  ℹ 10K rows as JSON: {json_time * 1000:.1f}ms")

        # ── Test 2: NULL handling ──
        print("\n── NULL Handling ──")
        await db.execute(
            "INSERT INTO stress_test (name, value, description, active, score) VALUES ($1, $2, $3, $4, $5)",
            "null_test",
            0,
            None,
            None,
            None,
        )
        null_row = await db.query_one(
            "SELECT * FROM stress_test WHERE name = $1", "null_test"
        )
        check("NULL description is None", null_row["description"] is None)
        check("NULL active is None", null_row["active"] is None)
        check("NULL score is None", null_row["score"] is None)

        # NULL in JSON
        null_json = _native_query_json(
            db._pool_handle, "SELECT * FROM stress_test WHERE name = $1", ["null_test"]
        )
        null_parsed = json.loads(null_json)
        check("JSON NULL is null", null_parsed[0]["description"] is None)
        check("JSON NULL active", null_parsed[0]["active"] is None)

        # ── Test 3: Empty results ──
        print("\n── Empty Results ──")
        empty = await db.query("SELECT * FROM stress_test WHERE id = -1")
        check("Empty dict query returns []", empty == [])

        empty_json = _native_query_json(
            db._pool_handle, "SELECT * FROM stress_test WHERE id = -1", []
        )
        check("Empty JSON returns []", empty_json == b"[]")

        empty_one = await db.query_one("SELECT * FROM stress_test WHERE id = -1")
        check("Empty query_one returns None", empty_one is None)

        # ── Test 4: String escaping in JSON ──
        print("\n── JSON String Escaping ──")
        await db.execute(
            "INSERT INTO stress_test (name, value, description) VALUES ($1, $2, $3)",
            "escape_test",
            0,
            'He said "hello"\nand\ttabs\\backslash',
        )
        esc_json = _native_query_json(
            db._pool_handle,
            "SELECT description FROM stress_test WHERE name = $1",
            ["escape_test"],
        )
        esc_parsed = json.loads(esc_json)
        check("JSON escaping valid JSON", len(esc_parsed) == 1)
        check(
            "JSON escaping preserves content", "hello" in esc_parsed[0]["description"]
        )
        check("JSON escaping preserves newline", "\n" in esc_parsed[0]["description"])
        check("JSON escaping preserves tab", "\t" in esc_parsed[0]["description"])
        check("JSON escaping preserves backslash", "\\" in esc_parsed[0]["description"])
        check("JSON escaping preserves quotes", '"' in esc_parsed[0]["description"])

        # ── Test 5: Cache eviction — many unique queries ──
        # ru_maxrss is a MONOTONIC high-water mark: a one-time warmup allocation
        # (connection pools, interpreter arenas, first-touch of the compiled-SQL
        # cache) inflates the first batch's apparent "growth" and never releases,
        # which made a single-batch `growth < 50MB` assertion flaky. Measure the
        # SLOPE across two identical-sized batches instead: a real leak keeps
        # pushing the high-water up every batch, while warmup only bumps batch 1.
        print("\n── Cache Eviction / Memory Stability ──")

        async def _run_unique_batch(base):
            # Unique SQL text per query (distinct literal) forces distinct SQL
            # hashes and exercises cache eviction at COLUMN_CACHE_MAX (4096).
            for i in range(5000):
                await db.query(
                    f"SELECT id, name FROM stress_test WHERE value = {base + i}"
                )

        rss_start = get_rss_mb()
        await _run_unique_batch(0)
        rss_after_b1 = get_rss_mb()
        await _run_unique_batch(1_000_000)
        rss_after_b2 = get_rss_mb()

        batch1_growth = rss_after_b1 - rss_start
        batch2_growth = rss_after_b2 - rss_after_b1
        print(
            f"  ℹ RSS start {rss_start:.1f}MB → batch1 {rss_after_b1:.1f}MB "
            f"(+{batch1_growth:.1f}) → batch2 {rss_after_b2:.1f}MB (+{batch2_growth:.1f})"
        )
        # The second-batch slope isolates a true per-batch leak from one-time
        # warmup: if the cache evicts correctly, batch 2 adds ~nothing.
        check(
            "No sustained memory leak (batch-2 RSS slope < 15MB)",
            batch2_growth < 15,
            f"batch2 grew {batch2_growth:.1f}MB (batch1 {batch1_growth:.1f}MB) — cache not evicting",
        )

        # ── Test 6: Repeated queries use cache ──
        print("\n── Cache Performance ──")
        sql = "SELECT id, name, value FROM stress_test WHERE value < 100"

        # First call (cache miss)
        start = time.perf_counter()
        _native_query_dicts(db._pool_handle, sql, [])
        first_time = time.perf_counter() - start

        # Subsequent calls (cache hit for interned keys)
        start = time.perf_counter()
        for _ in range(1000):
            _native_query_dicts(db._pool_handle, sql, [])
        cached_time = (time.perf_counter() - start) / 1000

        print(
            f"  ℹ First call: {first_time * 1000:.2f}ms, cached avg: {cached_time * 1000:.3f}ms"
        )

        # ── Test 7: Interleaved query patterns ──
        print("\n── Interleaved Queries ──")
        queries = [
            "SELECT id, name FROM stress_test WHERE value < 10",
            "SELECT name, value, active FROM stress_test WHERE active = true LIMIT 5",
            "SELECT id, description FROM stress_test WHERE description IS NOT NULL LIMIT 3",
            "SELECT COUNT(*) FROM stress_test",
        ]
        for _ in range(100):
            for sql in queries:
                _native_query_dicts(db._pool_handle, sql, [])
        check("Interleaved queries stable", True)

        # JSON interleaved
        for _ in range(100):
            for sql in queries:
                result = _native_query_json(db._pool_handle, sql, [])
                json.loads(result)  # validate
        check("Interleaved JSON queries stable", True)

        # ── Test 8: Wide table (many columns) ──
        print("\n── Wide Table ──")
        await db.execute("DROP TABLE IF EXISTS wide_test CASCADE")
        cols = ", ".join(f"col_{i} TEXT" for i in range(50))
        await db.execute(f"CREATE TABLE wide_test (id SERIAL PRIMARY KEY, {cols})")

        col_vals = ", ".join(f"${i + 1}" for i in range(50))
        col_names = ", ".join(f"col_{i}" for i in range(50))
        await db.execute(
            f"INSERT INTO wide_test ({col_names}) VALUES ({col_vals})",
            *[f"val_{i}" for i in range(50)],
        )

        wide_rows = await db.query("SELECT * FROM wide_test")
        check("Wide table query works", len(wide_rows) == 1)
        check("Wide table has 51 columns", len(wide_rows[0]) == 51)  # id + 50 cols
        check("Wide table col_0 correct", wide_rows[0]["col_0"] == "val_0")
        check("Wide table col_49 correct", wide_rows[0]["col_49"] == "val_49")

        wide_json = _native_query_json(db._pool_handle, "SELECT * FROM wide_test", [])
        wide_parsed = json.loads(wide_json)
        check("Wide JSON valid", len(wide_parsed) == 1)
        check("Wide JSON has all cols", len(wide_parsed[0]) == 51)

        await db.execute("DROP TABLE IF EXISTS wide_test CASCADE")

        # ── Benchmark: Release mode, 10K rows ──
        print("\n── Release Mode Benchmarks (10K rows) ──")
        sql = "SELECT id, name, value, description, active, score FROM stress_test ORDER BY id"
        iters = 20

        # Native dicts
        start = time.perf_counter()
        for _ in range(iters):
            _native_query_dicts(db._pool_handle, sql, [])
        dict_total = time.perf_counter() - start
        dict_per = dict_total / iters

        # Old tuple + Python dict
        from hyperdjango._hyperdjango_native import _db_get_last_columns

        start = time.perf_counter()
        for _ in range(iters):
            raw = _native_query(db._pool_handle, sql, [])
            cols = _db_get_last_columns()
            cn = [col[0] for col in cols]
            [dict(zip(cn, row)) for row in raw]
        old_total = time.perf_counter() - start
        old_per = old_total / iters

        # Native JSON
        start = time.perf_counter()
        for _ in range(iters):
            _native_query_json(db._pool_handle, sql, [])
        json_total = time.perf_counter() - start
        json_per = json_total / iters

        # Dicts + json.dumps
        start = time.perf_counter()
        for _ in range(iters):
            rows = _native_query_dicts(db._pool_handle, sql, [])
            json.dumps(rows)
        dumps_total = time.perf_counter() - start
        dumps_per = dumps_total / iters

        dict_speedup = old_per / dict_per
        json_speedup = dumps_per / json_per

        print(
            f"  ℹ Native dicts:        {dict_per * 1000:.1f}ms/query ({dict_speedup:.2f}x vs old)"
        )
        print(f"  ℹ Old tuple+dict:      {old_per * 1000:.1f}ms/query")
        print(
            f"  ℹ Native JSON:         {json_per * 1000:.1f}ms/query ({json_speedup:.2f}x vs dicts+dumps)"
        )
        print(f"  ℹ Dicts + json.dumps:  {dumps_per * 1000:.1f}ms/query")
        from hyperdjango.native import is_release_build

        # Under parallel CI load, both code paths slow down equally
        # but per-call timing variance can flip the relative order
        # below the noise floor — that's a benchmark observation, not
        # a correctness regression. Skip the comparison entirely when
        # we know we're racing with 24+ other test workers, and only
        # enforce it on serial release builds where the comparison is
        # actually meaningful.
        _parallel = os.environ.get("HYPER_TEST_PARALLEL") == "1"
        if is_release_build and not _parallel:
            check("Native dicts faster", dict_speedup > 1.0)
            check("Native JSON faster", json_speedup > 1.0)
        elif _parallel:
            print(
                f"  (skipping perf assertions under parallel CI load — "
                f"speedups: dicts={dict_speedup:.2f}x json={json_speedup:.2f}x)"
            )
        else:
            print("  (skipping perf assertions in debug build)")

        # Cleanup
        await db.execute("DROP TABLE IF EXISTS stress_test CASCADE")

    finally:
        await db.disconnect()


def main():
    print("=" * 60)
    print("Native Result Building — Stress & Scalability Tests")
    print("=" * 60)

    asyncio.run(run_stress_tests())

    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
