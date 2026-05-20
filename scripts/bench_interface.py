#!/usr/bin/env python3
"""
Micro-benchmark: Python↔Zig interface overhead.

Measures each component of the query pipeline to find optimization targets.

Run: uv run python scripts/bench_interface.py
"""

import os
import subprocess
import time

user = os.environ.get("USER", "postgres")
subprocess.run(["createdb", "hyperdjango_test"], capture_output=True)

N = 5000

from hyperdjango._hyperdjango_native import (
    _db_configure,
    _db_execute,
    _db_query,
    hello,
    json_dumps_native,
    validate_email,
    validate_int_range,
)

h = _db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 4)

# Setup test data
_db_execute(h, "DROP TABLE IF EXISTS bench_iface", [])
_db_execute(
    h,
    "CREATE TABLE bench_iface (id SERIAL PRIMARY KEY, name VARCHAR(100), value INTEGER)",
    [],
)
for i in range(100):
    _db_execute(
        h,
        "INSERT INTO bench_iface (name, value) VALUES ($1, $2)",
        [f"item_{i}", str(i)],
    )

print(f"Python↔Zig Interface Micro-Benchmark ({N} iterations)")
print("=" * 60)

# 1. hello() — pure function call overhead, no I/O
start = time.perf_counter_ns()
for _ in range(N):
    hello()
ns = (time.perf_counter_ns() - start) / N
print(
    f"  hello() call overhead:       {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 2. validate_email — Zig SIMD validation
start = time.perf_counter_ns()
for _ in range(N):
    validate_email("alice@example.com")
ns = (time.perf_counter_ns() - start) / N
print(
    f"  validate_email (valid):      {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 3. validate_int_range — Zig integer check
start = time.perf_counter_ns()
for _ in range(N):
    validate_int_range(42, 0, 100)
ns = (time.perf_counter_ns() - start) / N
print(
    f"  validate_int_range:          {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 4. json_dumps_native — Zig JSON serialization
data = {"name": "Alice", "age": 30, "email": "alice@example.com"}
start = time.perf_counter_ns()
for _ in range(N):
    json_dumps_native(data)
ns = (time.perf_counter_ns() - start) / N
print(
    f"  json_dumps_native (dict):    {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 5. _db_query — SELECT 1 (minimal query)
start = time.perf_counter_ns()
for _ in range(N):
    _db_query(h, "SELECT 1", [])
ns = (time.perf_counter_ns() - start) / N
print(
    f"  _db_query('SELECT 1'):       {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 6. _db_query — SELECT by PK with param
start = time.perf_counter_ns()
for _ in range(N):
    _db_query(h, "SELECT id, name, value FROM bench_iface WHERE id = $1", ["1"])
ns = (time.perf_counter_ns() - start) / N
print(
    f"  _db_query(SELECT by PK):     {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 7. _db_query — SELECT multiple rows
start = time.perf_counter_ns()
for _ in range(N):
    _db_query(h, "SELECT id, name, value FROM bench_iface WHERE value > $1", ["50"])
ns = (time.perf_counter_ns() - start) / N
print(
    f"  _db_query(SELECT >50, ~50 rows): {ns:>5.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 8. _db_execute — INSERT
start = time.perf_counter_ns()
for i in range(N):
    _db_execute(
        h, "INSERT INTO bench_iface (name, value) VALUES ($1, $2)", [f"x_{i}", str(i)]
    )
ns = (time.perf_counter_ns() - start) / N
print(
    f"  _db_execute(INSERT):         {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
)

# 9. _db_execute — UPDATE
start = time.perf_counter_ns()
for i in range(1, min(N, 100) + 1):
    _db_execute(
        h, "UPDATE bench_iface SET value = $1 WHERE id = $2", [str(i + 999), str(i)]
    )
update_n = min(N, 100)
ns = (time.perf_counter_ns() - start) / update_n
print(
    f"  _db_execute(UPDATE):         {ns:>8.0f} ns/call  ({update_n / (ns / 1e9):>10.0f} ops/sec)"
)

# 10. Compare: psycopg for same SELECT by PK
print("\n  --- psycopg3 comparison ---")
try:
    import psycopg

    pconn = psycopg.connect(
        host="localhost",
        port=5432,
        dbname="hyperdjango_test",
        user=user,
        autocommit=True,
    )
    start = time.perf_counter_ns()
    for _ in range(N):
        with pconn.cursor() as cur:
            cur.execute("SELECT id, name, value FROM bench_iface WHERE id = %s", (1,))
            cur.fetchone()
    ns = (time.perf_counter_ns() - start) / N
    print(
        f"  psycopg SELECT by PK:        {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
    )

    start = time.perf_counter_ns()
    for _ in range(N):
        with pconn.cursor() as cur:
            cur.execute(
                "SELECT id, name, value FROM bench_iface WHERE value > %s", (50,)
            )
            cur.fetchall()
    ns = (time.perf_counter_ns() - start) / N
    print(
        f"  psycopg SELECT >50:          {ns:>8.0f} ns/call  ({N / (ns / 1e9):>10.0f} ops/sec)"
    )
    pconn.close()
except ImportError:
    print("  psycopg not installed")

# Cleanup
_db_execute(h, "DROP TABLE IF EXISTS bench_iface", [])
