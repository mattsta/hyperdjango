#!/usr/bin/env python3
"""
Platform performance benchmark suite.

Benchmarks critical hot paths:
1. QuerySet evaluation (simple, FK filter, count, exists)
2. Model instantiation from DB rows
3. Middleware chain overhead
4. Static file middleware (cache hit)
5. Template rendering (native vs Jinja2)
6. Cache operations (LocMemCache get/set)
7. Response construction
8. Request parsing

Usage:
    uv run python scripts/bench_platform.py
"""

import asyncio
import inspect
import os
import time

from hyperdjango.cache import LocMemCache
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    MiddlewareStack,
    TimingMiddleware,
)

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


class BenchItem(Model):
    class Meta:
        table = "bench_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    value: int = Field(default=0)
    category: str = Field(max_length=50, default="general")


class BenchAuthor(Model):
    class Meta:
        table = "bench_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


class BenchBook(Model):
    class Meta:
        table = "bench_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    author_id: int = Field(foreign_key=BenchAuthor)


def bench(name, iterations=1000):
    """Decorator that benchmarks a function."""

    def decorator(func):
        async def wrapper():
            # Warmup
            for _ in range(min(10, iterations)):
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()

            # Benchmark
            start = time.perf_counter()
            for _ in range(iterations):
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
            elapsed = time.perf_counter() - start

            per_op = elapsed / iterations * 1_000_000  # microseconds
            ops_sec = iterations / elapsed
            print(f"  {name:<45} {per_op:>8.1f} µs/op  ({ops_sec:>10,.0f} ops/sec)")

        wrapper.__name__ = name
        wrapper._is_bench = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# DB Benchmarks
# ---------------------------------------------------------------------------


@bench("QuerySet.all() — 50 rows", iterations=500)
async def bench_qs_all():
    await BenchItem.objects.all()


@bench("QuerySet.filter(value__gt=25).all()", iterations=500)
async def bench_qs_filter():
    await BenchItem.objects.filter(value__gt=25).all()


@bench("QuerySet.count()", iterations=1000)
async def bench_qs_count():
    await BenchItem.objects.count()


@bench("QuerySet.exists()", iterations=1000)
async def bench_qs_exists():
    await BenchItem.objects.exists()


@bench("QuerySet.first()", iterations=1000)
async def bench_qs_first():
    await BenchItem.objects.first()


@bench("QuerySet.get(id=1)", iterations=1000)
async def bench_qs_get():
    await BenchItem.objects.get(id=1)


@bench("FK-spanning filter(author__name='Alice')", iterations=500)
async def bench_fk_filter():
    await BenchBook.objects.filter(author__name="Alice").all()


@bench("FK-spanning count(author__name='Alice')", iterations=500)
async def bench_fk_count():
    await BenchBook.objects.filter(author__name="Alice").count()


@bench("Model.save() — update existing", iterations=200)
async def bench_save_update():
    item = await BenchItem.objects.first()
    item.value += 1
    await item.save()


# ---------------------------------------------------------------------------
# Cache Benchmarks
# ---------------------------------------------------------------------------

_cache = LocMemCache(max_size=10000)
for i in range(1000):
    _cache.set(f"key_{i}", f"value_{i}", ttl=3600)


@bench("LocMemCache.get() — hit", iterations=50000)
def bench_cache_get():
    _cache.get("key_500")


@bench("LocMemCache.set()", iterations=50000)
def bench_cache_set():
    _cache.set("bench_key", "bench_value", ttl=60)


@bench("LocMemCache.get_or_set() — hit", iterations=50000)
def bench_cache_get_or_set():
    _cache.get_or_set("key_500", lambda: "computed", ttl=3600)


# ---------------------------------------------------------------------------
# HTTP Benchmarks
# ---------------------------------------------------------------------------


@bench("Request() construction", iterations=50000)
def bench_request():
    Request(
        method="GET",
        path="/api/users",
        headers={"host": "localhost"},
        query_string="page=1&q=test",
    )


@bench("Response.json() construction", iterations=50000)
def bench_response_json():
    Response.json({"id": 1, "name": "Test", "value": 42, "active": True})


@bench("Response.html() construction", iterations=50000)
def bench_response_html():
    Response.html("<h1>Hello World</h1><p>This is a test page.</p>")


@bench("request.query() — parse + get", iterations=50000)
def bench_request_query():
    req = Request(query_string="page=3&q=hello&tags=a&tags=b")
    req.query("page")
    req.query("q")


@bench("request.GET — flat dict property", iterations=50000)
def bench_request_get():
    req = Request(query_string="page=3&q=hello&tags=a&tags=b")
    _ = req.GET


# ---------------------------------------------------------------------------
# Middleware Benchmarks
# ---------------------------------------------------------------------------

_mw_stack = MiddlewareStack()
_mw_stack.add(TimingMiddleware())
_mw_stack.add(CORSMiddleware(origins=["*"]))


@bench("Middleware chain (2 middleware) — wrap+dispatch", iterations=5000)
async def bench_middleware():
    async def handler(req):
        return Response.json({"ok": True})

    wrapped = _mw_stack.wrap(handler)
    req = Request(
        method="GET", path="/api/test", headers={"origin": "http://localhost"}
    )
    await wrapped(req)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Setup tables
    await db.execute("DROP TABLE IF EXISTS bench_books CASCADE")
    await db.execute("DROP TABLE IF EXISTS bench_authors CASCADE")
    await db.execute("DROP TABLE IF EXISTS bench_items CASCADE")
    await db.execute("""
        CREATE TABLE bench_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            value INTEGER DEFAULT 0,
            category VARCHAR(50) DEFAULT 'general'
        )
    """)
    await db.execute("""
        CREATE TABLE bench_authors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE bench_books (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            author_id INTEGER REFERENCES bench_authors(id) ON DELETE CASCADE
        )
    """)

    # Seed data
    for i in range(50):
        await db.execute(
            "INSERT INTO bench_items (name, value, category) VALUES ($1, $2, $3)",
            f"Item {i}",
            i,
            ["electronics", "books", "clothing"][i % 3],
        )
    await db.execute(
        "INSERT INTO bench_authors (id, name) VALUES (1, 'Alice'), (2, 'Bob')"
    )
    for i in range(20):
        await db.execute(
            "INSERT INTO bench_books (title, author_id) VALUES ($1, $2)",
            f"Book {i}",
            1 if i < 12 else 2,
        )

    # Run benchmarks
    benchmarks = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_bench", False)
    ]

    print("\nHyperDjango Platform Benchmarks")
    print("=" * 75)
    print("\n--- Database & ORM ---")
    for b in benchmarks[:9]:
        await b()

    print("\n--- Cache ---")
    for b in benchmarks[9:12]:
        await b()

    print("\n--- HTTP ---")
    for b in benchmarks[12:17]:
        await b()

    print("\n--- Middleware ---")
    for b in benchmarks[17:]:
        await b()

    print(f"\n{'=' * 75}")

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS bench_books CASCADE")
    await db.execute("DROP TABLE IF EXISTS bench_authors CASCADE")
    await db.execute("DROP TABLE IF EXISTS bench_items CASCADE")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
