#!/usr/bin/env python3
"""
Benchmark: pg.zig native vs psycopg — raw driver comparison.

Runs identical SQL operations through both drivers and compares throughput.
Direct cursor/query operations — no Django ORM overhead.

Prerequisites:
    PostgreSQL running on localhost:5432
    createdb hyperdjango_test

Run: uv run python scripts/bench_django_db.py [iterations]
"""

import os
import subprocess
import sys
import time

user = os.environ.get("USER", "postgres")
subprocess.run(["createdb", "hyperdjango_test"], capture_output=True)

ITERATIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 1000


def bench_psycopg(iterations):
    """Benchmark psycopg3 directly."""
    import psycopg

    conn = psycopg.connect(
        host="localhost",
        port=5432,
        dbname="hyperdjango_test",
        user=user,
        autocommit=True,
    )

    results = {}

    with conn.cursor() as c:
        c.execute("DROP TABLE IF EXISTS bench_psycopg")
        c.execute("""CREATE TABLE bench_psycopg (
            id SERIAL PRIMARY KEY, name VARCHAR(100), value INTEGER
        )""")

    start = time.perf_counter()
    for i in range(iterations):
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO bench_psycopg (name, value) VALUES (%s, %s)",
                (f"item_{i}", i),
            )
    results["insert"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for i in range(1, iterations + 1):
        with conn.cursor() as c:
            c.execute("SELECT id, name, value FROM bench_psycopg WHERE id = %s", (i,))
            c.fetchone()
    results["select_pk"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for _ in range(iterations):
        with conn.cursor() as c:
            c.execute(
                "SELECT id, name, value FROM bench_psycopg WHERE value > %s AND value < %s",
                (10, 100),
            )
            c.fetchall()
    results["select_range"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for i in range(1, iterations + 1):
        with conn.cursor() as c:
            c.execute(
                "UPDATE bench_psycopg SET value = %s WHERE id = %s", (i + 1000, i)
            )
    results["update"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for _ in range(iterations):
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM bench_psycopg")
            c.fetchone()
    results["count"] = iterations / (time.perf_counter() - start)

    with conn.cursor() as c:
        c.execute("DROP TABLE IF EXISTS bench_psycopg")
    conn.close()
    return results


def bench_pgzig(iterations):
    """Benchmark pg.zig _db_query/_db_execute directly."""
    from hyperdjango._hyperdjango_native import (
        _db_close_pool,
        _db_configure,
        _db_execute,
        _db_query,
    )

    h = _db_configure(f"postgresql://{user}:@localhost:5432/hyperdjango_test", 4)

    results = {}

    _db_execute(h, "DROP TABLE IF EXISTS bench_pgzig", [])
    _db_execute(
        h,
        """CREATE TABLE bench_pgzig (
        id SERIAL PRIMARY KEY, name VARCHAR(100), value INTEGER
    )""",
        [],
    )

    start = time.perf_counter()
    for i in range(iterations):
        _db_execute(
            h,
            "INSERT INTO bench_pgzig (name, value) VALUES ($1, $2)",
            [f"item_{i}", str(i)],
        )
    results["insert"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for i in range(1, iterations + 1):
        _db_query(h, "SELECT id, name, value FROM bench_pgzig WHERE id = $1", [str(i)])
    results["select_pk"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for _ in range(iterations):
        _db_query(
            h,
            "SELECT id, name, value FROM bench_pgzig WHERE value > $1 AND value < $2",
            ["10", "100"],
        )
    results["select_range"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for i in range(1, iterations + 1):
        _db_execute(
            h,
            "UPDATE bench_pgzig SET value = $1 WHERE id = $2",
            [str(i + 1000), str(i)],
        )
    results["update"] = iterations / (time.perf_counter() - start)

    start = time.perf_counter()
    for _ in range(iterations):
        _db_query(h, "SELECT COUNT(*) FROM bench_pgzig", [])
    results["count"] = iterations / (time.perf_counter() - start)

    _db_execute(h, "DROP TABLE IF EXISTS bench_pgzig", [])
    _db_close_pool(h)
    return results


def main():
    print(f"Database Driver Benchmark — {ITERATIONS} iterations per operation")
    print("=" * 70)

    print("\npg.zig (native Zig):")
    try:
        pgzig = bench_pgzig(ITERATIONS)
        for op, ops in pgzig.items():
            print(f"  {op:<15} {ops:>10.0f} ops/sec")
    except Exception as e:
        print(f"  ERROR: {e}")
        pgzig = {}

    print("\npsycopg3 (Python):")
    try:
        psycopg_r = bench_psycopg(ITERATIONS)
        for op, ops in psycopg_r.items():
            print(f"  {op:<15} {ops:>10.0f} ops/sec")
    except Exception as e:
        print(f"  ERROR: {e}")
        psycopg_r = {}

    if pgzig and psycopg_r:
        print(f"\n{'=' * 70}")
        print(f"{'Operation':<15} {'pg.zig':>12} {'psycopg':>12} {'Ratio':>10}")
        print("-" * 52)
        for op in pgzig:
            if op in psycopg_r:
                z = pgzig[op]
                p = psycopg_r[op]
                ratio = z / p if p > 0 else 0
                print(f"{op:<15} {z:>11.0f} {p:>11.0f} {ratio:>8.2f}x")


if __name__ == "__main__":
    main()
