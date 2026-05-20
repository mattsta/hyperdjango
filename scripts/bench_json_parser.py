#!/usr/bin/env python3
"""
Benchmark: json_loads_native (SIMD Zig) vs json.loads (Python stdlib).

Tests with various payload sizes and structures to find where SIMD
parsing provides the most benefit.

Run: uv run python scripts/bench_json_parser.py
"""

import json
import os
import time

from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_execute,
    _db_query,
    json_loads_native,
)

N = 10000


def bench(name, json_str):
    """Benchmark json_loads_native vs json.loads for a given JSON string."""
    # Warm up
    for _ in range(100):
        json_loads_native(json_str)
        json.loads(json_str)

    # Native SIMD
    start = time.perf_counter_ns()
    for _ in range(N):
        json_loads_native(json_str)
    native_ns = (time.perf_counter_ns() - start) / N

    # Python stdlib
    start = time.perf_counter_ns()
    for _ in range(N):
        json.loads(json_str)
    stdlib_ns = (time.perf_counter_ns() - start) / N

    ratio = stdlib_ns / native_ns if native_ns > 0 else 0
    print(f"  {name:<35} {native_ns:>8.0f} ns  {stdlib_ns:>8.0f} ns  {ratio:>5.2f}x")


def bench_jsonb_postgres():
    """Benchmark JSONB column reading: native parse vs string + json.loads."""
    user = os.environ.get("USER", "postgres")
    h = _db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 2)

    _db_execute(h, "DROP TABLE IF EXISTS bench_jsonb", [])
    _db_execute(h, "CREATE TABLE bench_jsonb (id SERIAL PRIMARY KEY, data JSONB)", [])

    # Insert 100 rows of JSONB data
    for i in range(100):
        obj = json.dumps(
            {
                "id": i,
                "name": f"user_{i}",
                "scores": [i * 10, i * 20, i * 30],
                "active": i % 2 == 0,
            }
        )
        _db_execute(h, f"INSERT INTO bench_jsonb (data) VALUES ('{obj}'::jsonb)", [])

    # Benchmark: read all 100 rows (JSONB auto-parsed by pg.zig)
    n_reads = 1000
    start = time.perf_counter_ns()
    for _ in range(n_reads):
        rows = _db_query(h, "SELECT data FROM bench_jsonb", [])
    total_ns = (time.perf_counter_ns() - start) / n_reads

    # Verify data is already parsed
    sample = _db_query(h, "SELECT data FROM bench_jsonb LIMIT 1", [])
    data_type = type(sample[0][0]).__name__

    print(
        f"\n  JSONB 100 rows (auto-parsed to {data_type}): {total_ns:>10.0f} ns/query  ({n_reads * 100 / (total_ns * n_reads / 1e9):>8.0f} rows/sec)"
    )

    _db_execute(h, "DROP TABLE bench_jsonb", [])
    _db_close_pool(h)


if __name__ == "__main__":
    print(f"JSON Parser Benchmark — {N} iterations per test")
    print(f"{'':>38} {'native':>8}   {'stdlib':>8}   {'ratio':>5}")
    print("=" * 70)

    # Small payloads
    bench('tiny object {"a":1}', '{"a": 1}')
    bench("small object (3 keys)", '{"name": "Alice", "age": 30, "active": true}')
    bench("integer", "42")
    bench("float", "3.14159265358979")
    bench("short string", '"hello world"')
    bench("boolean", "true")
    bench("null", "null")

    # Medium payloads
    bench("medium object (10 keys)", json.dumps({f"key_{i}": i for i in range(10)}))
    bench("array of 10 ints", json.dumps(list(range(10))))
    bench("array of 10 strings", json.dumps([f"item_{i}" for i in range(10)]))

    # Larger payloads
    bench(
        "large object (50 keys)",
        json.dumps({f"key_{i}": f"value_{i}" for i in range(50)}),
    )
    bench("array of 100 ints", json.dumps(list(range(100))))
    bench("nested 3 levels", json.dumps({"a": {"b": {"c": [1, 2, 3]}}}))

    # Real-world-ish
    api_response = json.dumps(
        {
            "users": [
                {
                    "id": i,
                    "name": f"User {i}",
                    "email": f"user{i}@example.com",
                    "scores": [85 + i, 90 + i, 95 + i],
                    "active": i % 2 == 0,
                }
                for i in range(10)
            ],
            "total": 10,
            "page": 1,
        }
    )
    bench("API response (10 users)", api_response)

    large_api = json.dumps(
        {
            "data": [
                {"id": i, "value": f"v{i}", "meta": {"x": i, "y": i * 2}}
                for i in range(100)
            ],
        }
    )
    bench("large API (100 items nested)", large_api)

    # Escape-heavy string
    bench("string with escapes", json.dumps({"msg": 'line1\nline2\ttab\r\n"quoted"'}))

    # PostgreSQL JSONB benchmark
    bench_jsonb_postgres()

    print("\nDone.")
