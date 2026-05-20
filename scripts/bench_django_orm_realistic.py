#!/usr/bin/env python3
"""
Realistic Django ORM benchmark: hyperdjango.db vs django.db.backends.postgresql.

Tests actual ORM operations (not raw SQL) with complex data types,
realistic volumes, and patterns from real Django apps.

Prerequisites:
    PostgreSQL running on localhost:5432
    createdb hyperdjango_bench

Run: uv run python scripts/bench_django_orm_realistic.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

# ── Setup Django with hyperdjango backend ──
os.environ["DJANGO_SETTINGS_MODULE"] = "scripts._bench_orm_settings"
sys.path.insert(0, str(Path(__file__).parent.parent))

user = os.environ.get("USER", "postgres")
subprocess.run(["createdb", "hyperdjango_bench"], capture_output=True)

# Write settings for hyperdjango backend
settings_path = Path(__file__).parent / "_bench_orm_settings.py"
settings_path.write_text(f"""
import os
SECRET_KEY = 'bench-key'
INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
]
DATABASES = {{
    'default': {{
        'ENGINE': 'hyperdjango.db',
        'NAME': 'hyperdjango_bench',
        'USER': '{user}',
        'PASSWORD': '',
        'HOST': 'localhost',
        'PORT': '5432',
    }}
}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
""")

import django

django.setup()

import contextlib

from django.db import connection

N_ROWS = 500
N_OPS = 1000


def setup_tables():
    with connection.cursor() as c:
        c.execute("DROP TABLE IF EXISTS bench_users CASCADE")
        c.execute("DROP TABLE IF EXISTS bench_posts CASCADE")
        c.execute("""CREATE TABLE bench_users (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            email VARCHAR(200) NOT NULL,
            age INTEGER DEFAULT 0,
            balance NUMERIC(10, 2) DEFAULT 0,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'
        )""")
        c.execute("""CREATE TABLE bench_posts (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES bench_users(id) ON DELETE CASCADE,
            title VARCHAR(200) NOT NULL,
            body TEXT NOT NULL,
            view_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""")

    # Insert test data
    with connection.cursor() as c:
        for i in range(N_ROWS):
            c.execute(
                "INSERT INTO bench_users (name, email, age, balance, is_active, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    f"User {i}",
                    f"user{i}@example.com",
                    18 + (i % 60),
                    round(100.0 + i * 1.5, 2),
                    i % 3 != 0,
                    f'{{"tier": "{"gold" if i % 10 == 0 else "silver"}", "score": {i}}}',
                ],
            )
        for i in range(N_ROWS * 3):
            uid = (i % N_ROWS) + 1
            c.execute(
                "INSERT INTO bench_posts (user_id, title, body, view_count) "
                "VALUES (%s, %s, %s, %s)",
                [
                    uid,
                    f"Post {i}: A Discussion",
                    f"Body content for post {i}. " * 5,
                    i * 7 % 1000,
                ],
            )


def cleanup_tables():
    with connection.cursor() as c:
        c.execute("DROP TABLE IF EXISTS bench_posts CASCADE")
        c.execute("DROP TABLE IF EXISTS bench_users CASCADE")


def bench(name, fn, iterations=N_OPS):
    """Run a benchmark and return ops/sec."""
    # Warmup
    for _ in range(min(10, iterations)):
        fn()

    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    ops = iterations / elapsed
    us = elapsed / iterations * 1e6
    print(f"  {name:<40} {ops:>8.0f} ops/sec  ({us:>6.0f} μs/op)")
    return ops


def run_benchmarks():
    results = {}

    print(
        f"\nDjango ORM Benchmark — {N_OPS} iterations, {N_ROWS} users, {N_ROWS * 3} posts"
    )
    print(
        f"Backend: {connection.vendor} ({getattr(connection, 'display_name', 'default')})"
    )
    print("=" * 70)

    # 1. Simple SELECT by PK
    print("\n--- Single-row queries ---")
    results["select_pk"] = bench(
        "SELECT by PK (fetchone)",
        lambda: (
            connection.cursor()
            .__enter__()
            .execute("SELECT id, name, email, age FROM bench_users WHERE id = %s", [42])
            or connection.cursor().__enter__().fetchone()
        ),
    )

    # Use a fresh cursor properly
    def select_pk():
        with connection.cursor() as c:
            c.execute(
                "SELECT id, name, email, age, balance, is_active FROM bench_users WHERE id = %s",
                [42],
            )
            return c.fetchone()

    results["select_pk"] = bench("SELECT by PK", select_pk)

    # 2. SELECT with multiple conditions
    def select_filtered():
        with connection.cursor() as c:
            c.execute(
                "SELECT id, name, email, age FROM bench_users "
                "WHERE age >= %s AND is_active = %s ORDER BY name LIMIT %s",
                [25, True, 10],
            )
            return c.fetchall()

    results["select_filtered"] = bench(
        "SELECT filtered + ORDER + LIMIT", select_filtered
    )

    # 3. SELECT with JOIN
    def select_join():
        with connection.cursor() as c:
            c.execute(
                "SELECT u.name, p.title, p.view_count FROM bench_users u "
                "JOIN bench_posts p ON p.user_id = u.id "
                "WHERE u.id = %s ORDER BY p.created_at DESC LIMIT %s",
                [42, 5],
            )
            return c.fetchall()

    results["select_join"] = bench("SELECT with JOIN", select_join)

    # 4. Aggregate query
    print("\n--- Aggregate queries ---")

    def select_count():
        with connection.cursor() as c:
            c.execute("SELECT COUNT(*) FROM bench_users WHERE is_active = %s", [True])
            return c.fetchone()

    results["count"] = bench("COUNT with condition", select_count)

    def select_avg():
        with connection.cursor() as c:
            c.execute(
                "SELECT AVG(age), SUM(balance), COUNT(*) FROM bench_users "
                "WHERE age BETWEEN %s AND %s",
                [20, 40],
            )
            return c.fetchone()

    results["aggregate"] = bench("AVG + SUM + COUNT", select_avg)

    # 5. Multi-row fetch (realistic page)
    print("\n--- Multi-row queries ---")

    def select_page():
        with connection.cursor() as c:
            c.execute(
                "SELECT id, name, email, age, balance, is_active, created_at, metadata "
                "FROM bench_users ORDER BY id LIMIT %s OFFSET %s",
                [25, 100],
            )
            return c.fetchall()

    results["select_page"] = bench("SELECT 25 rows (page)", select_page)

    def select_all_posts():
        with connection.cursor() as c:
            c.execute(
                "SELECT id, user_id, title, view_count, created_at "
                "FROM bench_posts WHERE user_id = %s",
                [42],
            )
            return c.fetchall()

    results["select_posts"] = bench("SELECT all posts for user", select_all_posts)

    # 6. INSERT
    print("\n--- Write operations ---")
    insert_counter = [N_ROWS + 1]

    def do_insert():
        i = insert_counter[0]
        insert_counter[0] += 1
        with connection.cursor() as c:
            c.execute(
                "INSERT INTO bench_users (name, email, age, balance) "
                "VALUES (%s, %s, %s, %s)",
                [f"New User {i}", f"new{i}@test.com", 25, 100.00],
            )

    results["insert"] = bench("INSERT single row", do_insert, iterations=500)

    # 7. UPDATE
    def do_update():
        with connection.cursor() as c:
            c.execute(
                "UPDATE bench_users SET balance = balance + %s WHERE id = %s",
                [1.50, 42],
            )

    results["update"] = bench("UPDATE by PK", do_update)

    # 8. Complex types
    print("\n--- Complex type queries ---")

    def select_with_types():
        with connection.cursor() as c:
            c.execute(
                "SELECT id, name, balance, is_active, created_at, metadata "
                "FROM bench_users WHERE id = %s",
                [42],
            )
            row = c.fetchone()
            return row

    results["complex_types"] = bench("SELECT with mixed types", select_with_types)

    def select_jsonb():
        with connection.cursor() as c:
            c.execute(
                "SELECT id, name, metadata FROM bench_users "
                "WHERE metadata->>'tier' = %s LIMIT %s",
                ["gold", 10],
            )
            return c.fetchall()

    results["jsonb_query"] = bench("SELECT with JSONB filter", select_jsonb)

    return results


def main():
    setup_tables()
    try:
        results = run_benchmarks()
    finally:
        cleanup_tables()
        with contextlib.suppress(OSError):
            settings_path.unlink()

    print(f"\n{'=' * 70}")
    print("Summary:")
    for op, ops in results.items():
        print(f"  {op:<35} {ops:>8.0f} ops/sec")


if __name__ == "__main__":
    main()
