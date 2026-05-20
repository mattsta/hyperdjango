#!/usr/bin/env python3
"""Profile INSERT overhead: parameter conversion vs execution."""

import os
import time

user = os.environ.get("USER", "postgres")
N = 2000

from hyperdjango._hyperdjango_native import _db_configure, _db_execute

_db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 4)
_db_execute("DROP TABLE IF EXISTS bench_ins", [])
_db_execute(
    "CREATE TABLE bench_ins (id SERIAL PRIMARY KEY, name VARCHAR(100), value INTEGER)",
    [],
)

# Benchmark: INSERT with string params (current)
start = time.perf_counter()
for i in range(N):
    _db_execute(
        "INSERT INTO bench_ins (name, value) VALUES ($1, $2)", [f"item_{i}", str(i)]
    )
elapsed = time.perf_counter() - start
print(
    f"pg.zig INSERT (str params):  {N / elapsed:.0f} ops/sec  ({elapsed / N * 1e6:.0f} us/op)"
)

_db_execute("DELETE FROM bench_ins", [])

# Benchmark: INSERT with int params (does Zig handle int→str conversion?)
start = time.perf_counter()
for i in range(N):
    _db_execute("INSERT INTO bench_ins (name, value) VALUES ($1, $2)", [f"item_{i}", i])
elapsed = time.perf_counter() - start
print(
    f"pg.zig INSERT (int params):  {N / elapsed:.0f} ops/sec  ({elapsed / N * 1e6:.0f} us/op)"
)

_db_execute("DELETE FROM bench_ins", [])

# Benchmark: psycopg INSERT for comparison
import psycopg

conn = psycopg.connect(
    host="localhost", port=5432, dbname="hyperdjango_test", user=user, autocommit=True
)

start = time.perf_counter()
for i in range(N):
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO bench_ins (name, value) VALUES (%s, %s)", (f"item_{i}", i)
        )
elapsed = time.perf_counter() - start
print(
    f"psycopg INSERT:              {N / elapsed:.0f} ops/sec  ({elapsed / N * 1e6:.0f} us/op)"
)

conn.close()

# Benchmark: just the Python list creation overhead
start = time.perf_counter()
for i in range(N):
    params = [f"item_{i}", str(i)]
elapsed = time.perf_counter() - start
print(
    f"Python list creation only:   {N / elapsed:.0f} ops/sec  ({elapsed / N * 1e6:.0f} us/op)"
)

_db_execute("DROP TABLE IF EXISTS bench_ins", [])
