#!/usr/bin/env python3
"""Test Django ORM edge cases through hyperdjango.db.

Systematically tests:
1. JSONField with nested structures (list of dicts, nested objects, null, empty)
2. Connection pool under concurrent Django requests (thread safety)
3. Prepared statement cache behavior after schema changes (ALTER TABLE)
4. QuerySet edge cases (empty results, aggregates, F expressions, Q objects)
5. Transaction edge cases (nested savepoints, rollback recovery)
6. Type edge cases (Decimal, UUID, timedelta, bytes, large text)
7. Bulk operations (bulk_create, bulk_update, update_or_create)
8. Raw SQL with various parameter styles

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys
import threading
import time

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

import datetime
import decimal
import uuid

from django.db import connection, models, transaction
from django.db.models import Avg, Count, F, Max, Min, Q, Sum

# ── Create test tables ────────────────────────────────────────────────────


def setup_tables():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS edgecase_item CASCADE")
        cursor.execute("DROP TABLE IF EXISTS edgecase_parent CASCADE")
        cursor.execute("""
            CREATE TABLE edgecase_parent (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                metadata JSONB DEFAULT '{}',
                tags JSONB DEFAULT '[]',
                score NUMERIC(10,4) DEFAULT 0,
                uid UUID DEFAULT gen_random_uuid(),
                raw_data BYTEA DEFAULT '',
                duration INTERVAL DEFAULT '0 seconds',
                big_text TEXT DEFAULT '',
                counter INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE TABLE edgecase_item (
                id SERIAL PRIMARY KEY,
                parent_id INTEGER REFERENCES edgecase_parent(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                value INTEGER DEFAULT 0
            )
        """)


# ── Django ORM models ────────────────────────────────────────────────────


class EdgeParent(models.Model):
    name = models.CharField(max_length=200)
    metadata = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)
    score = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    uid = models.UUIDField(default=uuid.uuid4)
    raw_data = models.BinaryField(default=b"")
    duration = models.DurationField(default=datetime.timedelta)
    big_text = models.TextField(default="")
    counter = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "admin_app"
        db_table = "edgecase_parent"


class EdgeItem(models.Model):
    parent = models.ForeignKey(
        EdgeParent, on_delete=models.CASCADE, related_name="items"
    )
    name = models.CharField(max_length=100)
    value = models.IntegerField(default=0)

    class Meta:
        app_label = "admin_app"
        db_table = "edgecase_item"


# ── Test runner ───────────────────────────────────────────────────────────


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

    setup_tables()

    # ── 1. JSONField nested structures ────────────────────────────────────
    print("\n=== JSONField nested structures ===")

    # Dict with nested objects
    obj = EdgeParent.objects.create(
        name="json_nested",
        metadata={"config": {"debug": True, "level": 3}, "tags": ["a", "b"]},
    )
    obj.refresh_from_db()
    check("nested dict saved", obj.metadata["config"]["debug"] is True)
    check("nested dict list", obj.metadata["tags"] == ["a", "b"])

    # List of dicts
    obj2 = EdgeParent.objects.create(
        name="json_list_dicts",
        tags=[{"name": "python", "score": 95}, {"name": "zig", "score": 99}],
    )
    obj2.refresh_from_db()
    check("list of dicts saved", len(obj2.tags) == 2)
    check("list of dicts content", obj2.tags[0]["name"] == "python")
    check("list of dicts score", obj2.tags[1]["score"] == 99)

    # Deeply nested
    deep = {"a": {"b": {"c": {"d": {"e": [1, 2, {"f": True}]}}}}}
    obj3 = EdgeParent.objects.create(name="json_deep", metadata=deep)
    obj3.refresh_from_db()
    check(
        "deeply nested preserved",
        obj3.metadata["a"]["b"]["c"]["d"]["e"][2]["f"] is True,
    )

    # JSON null
    obj4 = EdgeParent.objects.create(name="json_null", metadata=None)
    obj4.refresh_from_db()
    check("JSON null", obj4.metadata is None)

    # Empty dict and list
    obj5 = EdgeParent.objects.create(name="json_empty", metadata={}, tags=[])
    obj5.refresh_from_db()
    check("empty dict", obj5.metadata == {})
    check("empty list", obj5.tags == [])

    # JSON with unicode
    obj6 = EdgeParent.objects.create(
        name="json_unicode",
        metadata={"emoji": "🎉", "japanese": "日本語", "quotes": 'He said "hello"'},
    )
    obj6.refresh_from_db()
    check("unicode emoji", obj6.metadata["emoji"] == "🎉")
    check("unicode japanese", obj6.metadata["japanese"] == "日本語")
    check("unicode quotes", obj6.metadata["quotes"] == 'He said "hello"')

    # JSON with numbers
    obj7 = EdgeParent.objects.create(
        name="json_numbers",
        metadata={"int": 42, "float": 3.14, "neg": -17, "zero": 0, "big": 2**53},
    )
    obj7.refresh_from_db()
    check("json int", obj7.metadata["int"] == 42)
    check("json float", abs(obj7.metadata["float"] - 3.14) < 0.001)
    check("json negative", obj7.metadata["neg"] == -17)
    check("json big int", obj7.metadata["big"] == 2**53)

    # Update JSONField
    obj.metadata = {"updated": True, "list": [1, 2, 3]}
    obj.save()
    obj.refresh_from_db()
    check("json update", obj.metadata["updated"] is True)
    check("json update list", obj.metadata["list"] == [1, 2, 3])

    # ── 2. Type edge cases ────────────────────────────────────────────────
    print("\n=== Type edge cases ===")

    # Decimal
    dec_obj = EdgeParent.objects.create(
        name="decimal_test", score=decimal.Decimal("123.4567")
    )
    dec_obj.refresh_from_db()
    check("decimal preserved", dec_obj.score == decimal.Decimal("123.4567"))

    # UUID
    test_uid = uuid.uuid4()
    uid_obj = EdgeParent.objects.create(name="uuid_test", uid=test_uid)
    uid_obj.refresh_from_db()
    check("uuid preserved", uid_obj.uid == test_uid)

    # Binary data
    bin_data = b"\x00\x01\x02\xff\xfe\xfd" + bytes(range(256))
    bin_obj = EdgeParent.objects.create(name="binary_test", raw_data=bin_data)
    bin_obj.refresh_from_db()
    raw = bin_obj.raw_data
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    check("binary data preserved", raw == bin_data)

    # Duration/timedelta
    td = datetime.timedelta(hours=2, minutes=30, seconds=15)
    dur_obj = EdgeParent.objects.create(name="duration_test", duration=td)
    dur_obj.refresh_from_db()
    check("duration preserved", dur_obj.duration == td)

    # Large text
    large_text = "x" * 100_000
    txt_obj = EdgeParent.objects.create(name="large_text_test", big_text=large_text)
    txt_obj.refresh_from_db()
    check("large text preserved", len(txt_obj.big_text) == 100_000)

    # Boolean edge cases
    bool_obj = EdgeParent.objects.create(name="bool_false", is_active=False)
    bool_obj.refresh_from_db()
    check("bool false", bool_obj.is_active is False)

    # ── 3. QuerySet edge cases ────────────────────────────────────────────
    print("\n=== QuerySet edge cases ===")

    # Create some items for aggregation
    for i in range(10):
        EdgeParent.objects.create(
            name=f"agg_{i}", counter=i * 10, score=decimal.Decimal(str(i * 1.5))
        )

    # Count
    count = EdgeParent.objects.filter(name__startswith="agg_").count()
    check("count", count == 10)

    # Aggregates
    aggs = EdgeParent.objects.filter(name__startswith="agg_").aggregate(
        total=Sum("counter"),
        avg_counter=Avg("counter"),
        max_counter=Max("counter"),
        min_counter=Min("counter"),
        num=Count("id"),
    )
    check("sum", aggs["total"] == 450)
    check("avg", abs(float(aggs["avg_counter"]) - 45.0) < 0.01)
    check("max", aggs["max_counter"] == 90)
    check("min", aggs["min_counter"] == 0)
    check("count agg", aggs["num"] == 10)

    # F expressions
    EdgeParent.objects.filter(name__startswith="agg_").update(counter=F("counter") + 1)
    updated = EdgeParent.objects.get(name="agg_5")
    check("F expression update", updated.counter == 51)

    # Q objects (complex OR/AND)
    q_count = EdgeParent.objects.filter(
        Q(name__startswith="agg_") & (Q(counter__gte=50) | Q(counter__lte=10))
    ).count()
    check("Q objects work", q_count > 0)

    # values() and values_list()
    names = list(
        EdgeParent.objects.filter(name__startswith="agg_")
        .order_by("name")
        .values_list("name", flat=True)[:3]
    )
    check("values_list", names == ["agg_0", "agg_1", "agg_2"])

    vals = list(EdgeParent.objects.filter(name="agg_0").values("name", "counter"))
    check("values dict", vals[0]["name"] == "agg_0")

    # distinct
    EdgeParent.objects.create(name="dup_test", counter=100)
    EdgeParent.objects.create(name="dup_test", counter=200)
    distinct_count = (
        EdgeParent.objects.filter(name="dup_test").values("name").distinct().count()
    )
    check("distinct", distinct_count == 1)

    # exists()
    check("exists true", EdgeParent.objects.filter(name="agg_0").exists())
    check(
        "exists false", not EdgeParent.objects.filter(name="nonexistent_xyz").exists()
    )

    # Empty queryset
    empty = list(EdgeParent.objects.filter(name="nonexistent_xyz"))
    check("empty queryset", len(empty) == 0)

    # exclude
    excluded_count = (
        EdgeParent.objects.filter(name__startswith="agg_")
        .exclude(counter__gt=50)
        .count()
    )
    check("exclude works", excluded_count > 0 and excluded_count < 10)

    # order_by multiple fields
    ordered = list(
        EdgeParent.objects.filter(name__startswith="agg_")
        .order_by("-counter", "name")
        .values_list("counter", flat=True)[:3]
    )
    check("order_by desc", ordered[0] > ordered[1])

    # ── 4. Transactions and savepoints ────────────────────────────────────
    print("\n=== Transactions and savepoints ===")

    # Basic atomic
    try:
        with transaction.atomic():
            EdgeParent.objects.create(name="atomic_test")
            check(
                "inside atomic", EdgeParent.objects.filter(name="atomic_test").exists()
            )
        check(
            "after atomic commit",
            EdgeParent.objects.filter(name="atomic_test").exists(),
        )
    except Exception as e:
        check("atomic block", False, str(e))

    # Savepoint rollback
    try:
        with transaction.atomic():
            EdgeParent.objects.create(name="savepoint_outer")
            try:
                with transaction.atomic():
                    EdgeParent.objects.create(name="savepoint_inner")
                    raise ValueError("deliberate rollback")
            except ValueError:
                pass
            # Outer should still be there, inner rolled back
            check(
                "savepoint outer exists",
                EdgeParent.objects.filter(name="savepoint_outer").exists(),
            )
            check(
                "savepoint inner rolled back",
                not EdgeParent.objects.filter(name="savepoint_inner").exists(),
            )
    except Exception as e:
        check("savepoint test", False, str(e))

    # Nested atomic with savepoints
    try:
        with transaction.atomic():
            p = EdgeParent.objects.create(name="nested_atomic")
            with transaction.atomic():
                p.counter = 999
                p.save()
            p.refresh_from_db()
            check("nested atomic save", p.counter == 999)
    except Exception as e:
        check("nested atomic", False, str(e))

    # ── 5. Bulk operations ────────────────────────────────────────────────
    print("\n=== Bulk operations ===")

    # bulk_create
    objs = [EdgeParent(name=f"bulk_{i}", counter=i) for i in range(20)]
    created = EdgeParent.objects.bulk_create(objs)
    check("bulk_create count", len(created) == 20)
    check(
        "bulk_create in db",
        EdgeParent.objects.filter(name__startswith="bulk_").count() == 20,
    )

    # bulk_update
    to_update = list(EdgeParent.objects.filter(name__startswith="bulk_"))
    for obj in to_update:
        obj.counter = obj.counter + 1000
    EdgeParent.objects.bulk_update(to_update, ["counter"])
    verify = EdgeParent.objects.get(name="bulk_0")
    check("bulk_update", verify.counter == 1000)

    # update_or_create
    obj, created_flag = EdgeParent.objects.update_or_create(
        name="upsert_test",
        defaults={"counter": 42, "is_active": True},
    )
    check("update_or_create created", created_flag)
    obj2, created_flag2 = EdgeParent.objects.update_or_create(
        name="upsert_test",
        defaults={"counter": 99},
    )
    check("update_or_create updated", not created_flag2)
    check("update_or_create value", obj2.counter == 99)

    # get_or_create
    obj3, created3 = EdgeParent.objects.get_or_create(
        name="get_or_create_test",
        defaults={"counter": 7},
    )
    check("get_or_create created", created3)
    obj4, created4 = EdgeParent.objects.get_or_create(
        name="get_or_create_test",
        defaults={"counter": 999},
    )
    check("get_or_create got", not created4)
    check("get_or_create same pk", obj3.pk == obj4.pk)

    # ── 6. FK relations via ORM ───────────────────────────────────────────
    print("\n=== FK relations ===")

    parent = EdgeParent.objects.create(name="fk_parent", counter=0)
    EdgeItem.objects.create(parent=parent, name="child1", value=10)
    EdgeItem.objects.create(parent=parent, name="child2", value=20)
    EdgeItem.objects.create(parent=parent, name="child3", value=30)

    # Forward FK
    child = EdgeItem.objects.select_related("parent").get(name="child1")
    check("FK forward access", child.parent.name == "fk_parent")

    # Reverse relation
    children = list(parent.items.order_by("name").values_list("name", flat=True))
    check("FK reverse relation", children == ["child1", "child2", "child3"])

    # Filter across FK
    items = EdgeItem.objects.filter(parent__name="fk_parent").count()
    check("FK filter", items == 3)

    # Aggregate across FK
    total = EdgeItem.objects.filter(parent__name="fk_parent").aggregate(
        total=Sum("value")
    )
    check("FK aggregate", total["total"] == 60)

    # ── 7. Concurrent connection pool ─────────────────────────────────────
    print("\n=== Concurrent connection pool ===")

    # Close the main thread's connection first to free PG connections
    # before spawning threads that each create their own pool.
    from django.db import connections as _connections

    _connections.close_all()

    errors = []
    results = []

    def worker(thread_id):
        try:
            from django.db import connections

            for i in range(10):
                obj = EdgeParent.objects.create(
                    name=f"thread_{thread_id}_{i}", counter=thread_id * 100 + i
                )
                obj.refresh_from_db()
                if obj.name != f"thread_{thread_id}_{i}":
                    errors.append(f"Thread {thread_id}: name mismatch")
                    return
            results.append(thread_id)
        except Exception as e:
            errors.append(f"Thread {thread_id}: {e}")
        finally:
            # Django requires closing connections in threads
            from django.db import connections

            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.perf_counter() - t0

    check(
        "concurrent 5 threads completed",
        len(results) == 5,
        f"results={len(results)} errors={errors}",
    )
    concurrent_count = EdgeParent.objects.filter(name__startswith="thread_").count()
    check(
        "concurrent all rows created", concurrent_count == 50, f"got {concurrent_count}"
    )
    check("concurrent no errors", len(errors) == 0, str(errors))
    print(f"    → 5 threads × 10 ops each in {elapsed:.3f}s")

    # ── 8. Prepared statement cache after ALTER TABLE ─────────────────────
    print("\n=== Prepared statement cache after ALTER TABLE ===")

    # Query the table first to populate prepared statement cache
    EdgeParent.objects.filter(name="cache_test_warmup").count()
    EdgeParent.objects.create(name="cache_test_1", counter=1)

    # ALTER TABLE — add a column
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE edgecase_parent ADD COLUMN IF NOT EXISTS extra_col VARCHAR(50) DEFAULT 'x'"
        )

    # Query again — should work despite ALTER TABLE
    try:
        obj = EdgeParent.objects.create(name="cache_test_2", counter=2)
        obj.refresh_from_db()
        check("query after ALTER TABLE", obj.name == "cache_test_2")
    except Exception as e:
        check("query after ALTER TABLE", False, str(e))

    # Verify the count still works
    count = EdgeParent.objects.filter(name__startswith="cache_test_").count()
    check("count after ALTER TABLE", count >= 2, f"got {count}")

    # DROP the column we added
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE edgecase_parent DROP COLUMN IF EXISTS extra_col")

    # Query after DROP
    try:
        obj = EdgeParent.objects.create(name="cache_test_3", counter=3)
        check("query after DROP COLUMN", True)
    except Exception as e:
        check("query after DROP COLUMN", False, str(e))

    # ── 9. Raw SQL with parameter styles ──────────────────────────────────
    print("\n=== Raw SQL parameter styles ===")

    # Positional params
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM edgecase_parent WHERE name = %s", ["agg_0"]
        )
        row = cursor.fetchone()
    check("raw positional params", row[0] >= 1)

    # Named params — not standard for PostgreSQL but Django supports it
    # Actually Django uses %s style with list/tuple params for PostgreSQL

    # Multiple params
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM edgecase_parent WHERE counter >= %s AND counter <= %s",
            [0, 100],
        )
        row = cursor.fetchone()
    check("raw multiple params", row[0] > 0)

    # NULL param
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s IS NULL", [None])
        row = cursor.fetchone()
    check("raw NULL param", row[0] is True)

    # Various types in params
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT %s::integer, %s::text, %s::boolean, %s::numeric",
            [42, "hello", True, decimal.Decimal("3.14")],
        )
        row = cursor.fetchone()
    check("raw int param", row[0] == 42)
    check("raw str param", row[1] == "hello")
    check("raw bool param", row[2] is True)
    check("raw decimal param", float(row[3]) == 3.14)

    # ── 10. Connection recovery ───────────────────────────────────────────
    print("\n=== Connection recovery ===")

    # Force an error then recover
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM nonexistent_table_xyz_123")
    except Exception:
        pass  # Expected

    # Should recover and work
    try:
        count = EdgeParent.objects.count()
        check("recovery after error", count > 0)
    except Exception as e:
        check("recovery after error", False, str(e))

    # Force syntax error then recover
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELEKT BORKED SYNTAX")
    except Exception:
        pass

    try:
        count = EdgeParent.objects.count()
        check("recovery after syntax error", count > 0)
    except Exception as e:
        check("recovery after syntax error", False, str(e))

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS edgecase_item CASCADE")
        cursor.execute("DROP TABLE IF EXISTS edgecase_parent CASCADE")
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All ORM edge case tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
