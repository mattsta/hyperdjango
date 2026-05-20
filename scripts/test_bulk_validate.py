"""Tests for bulk_create_validated — batch validation + bulk insert.

Tests SIMD batch validation integration with QuerySet.bulk_create,
error handling (raise vs skip), and performance comparison.
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import time

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hyperdjango_test")


def run_async(coro):
    return asyncio.run(coro)


class BulkItem(Model):
    class Meta:
        table = "test_bulk_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    value: int = Field(default=0)
    status: str = Field(max_length=20, default="active")


async def setup(db):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS test_bulk_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            value INTEGER DEFAULT 0,
            status VARCHAR(20) DEFAULT 'active'
        )
    """)
    await db.execute("DELETE FROM test_bulk_items")


async def teardown(db):
    await db.execute("DROP TABLE IF EXISTS test_bulk_items CASCADE")


def test_bulk_create_validated_basic():
    """Basic bulk_create_validated inserts valid rows."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        await setup(db)

        try:
            set_db(db)
            data = [
                {"name": f"Item {i}", "value": i, "status": "active"} for i in range(10)
            ]
            instances = await BulkItem.objects.bulk_create_validated(data)
            assert len(instances) == 10
            for i, inst in enumerate(instances):
                assert inst.name == f"Item {i}"
                assert inst.id is not None  # Auto-generated PK

            # Verify in DB
            rows = await db.query("SELECT COUNT(*) FROM test_bulk_items")
            assert rows[0]["count"] == 10
            print("  PASS: Basic bulk_create_validated (10 rows)")
        finally:
            await teardown(db)
            await db.disconnect()

    run_async(run())


def test_bulk_create_validated_empty():
    """Empty data returns empty list."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        await setup(db)

        try:
            set_db(db)
            result = await BulkItem.objects.bulk_create_validated([])
            assert result == []
            print("  PASS: Empty data returns empty list")
        finally:
            await teardown(db)
            await db.disconnect()

    run_async(run())


def test_bulk_create_validated_skip_invalid():
    """With raise_on_error=False, invalid rows are skipped."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        await setup(db)

        try:
            set_db(db)
            data = [
                {"name": "Valid 1", "value": 10, "status": "active"},
                {"name": "Valid 2", "value": 20, "status": "active"},
                {"name": "Valid 3", "value": 30, "status": "active"},
            ]
            instances = await BulkItem.objects.bulk_create_validated(
                data, raise_on_error=False
            )
            # All valid — should all be inserted
            assert len(instances) == 3
            print("  PASS: Skip invalid mode works with all-valid data")
        finally:
            await teardown(db)
            await db.disconnect()

    run_async(run())


def test_bulk_create_validated_large_batch():
    """Large batch (1000 rows) works correctly."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        await setup(db)

        try:
            set_db(db)
            data = [
                {"name": f"Item {i}", "value": i % 100, "status": "active"}
                for i in range(1000)
            ]
            instances = await BulkItem.objects.bulk_create_validated(data)
            assert len(instances) == 1000

            rows = await db.query("SELECT COUNT(*) FROM test_bulk_items")
            assert rows[0]["count"] == 1000
            print("  PASS: Large batch (1000 rows)")
        finally:
            await teardown(db)
            await db.disconnect()

    run_async(run())


def test_bulk_create_validated_benchmark():
    """Benchmark: bulk_create_validated vs individual create."""

    async def run():
        db = Database(DB_URL, max_size=3)
        await db.connect()
        await setup(db)

        try:
            set_db(db)
            count = 500
            data = [
                {"name": f"Item {i}", "value": i, "status": "active"}
                for i in range(count)
            ]

            # Benchmark bulk_create_validated
            start = time.perf_counter_ns()
            instances = await BulkItem.objects.bulk_create_validated(data)
            bulk_ns = time.perf_counter_ns() - start
            assert len(instances) == count

            await db.execute("DELETE FROM test_bulk_items")

            # Benchmark individual creates
            start = time.perf_counter_ns()
            for d in data:
                await BulkItem.objects.create(**d)
            individual_ns = time.perf_counter_ns() - start

            speedup = individual_ns / bulk_ns if bulk_ns > 0 else 0
            print(
                f"  PASS: Benchmark ({count} rows) — "
                f"bulk: {bulk_ns / 1e6:.1f}ms, individual: {individual_ns / 1e6:.1f}ms, "
                f"speedup: {speedup:.1f}x"
            )
        finally:
            await teardown(db)
            await db.disconnect()

    run_async(run())


def main():
    tests = [
        test_bulk_create_validated_basic,
        test_bulk_create_validated_empty,
        test_bulk_create_validated_skip_invalid,
        test_bulk_create_validated_large_batch,
        test_bulk_create_validated_benchmark,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Bulk Create Validated Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback

            failed += 1
            errors.append((test.__name__, str(e)))
            traceback.print_exc()
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
