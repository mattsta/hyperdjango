#!/usr/bin/env python3
"""Test audit log system for HyperApp.

Tests:
1. Table creation — ensure_table creates table + indexes
2. Log add — records create operations
3. Log change — records update with JSON diff
4. Log delete — records delete operations
5. Diff computation — field-level old/new comparison
6. Object history — get_object_history returns ordered entries
7. Recent activity — get_recent returns latest across all models
8. User activity — get_user_activity filters by user
9. Model activity — get_model_activity filters by model
10. Count — total + filtered counts
11. JSON changes — complex nested diffs stored and retrieved

Runs against live PostgreSQL via hyperdjango.db.
"""

# hyper-test: db_django

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

import asyncio

from asgiref.sync import sync_to_async
from django.db import connection

from hyperdjango.auth.audit import AuditLog


class SyncDB:
    def _query_sync(self, sql, params):
        with connection.cursor() as cursor:
            converted = sql
            for i in range(len(params), 0, -1):
                converted = converted.replace(f"${i}", "%s")
            cursor.execute(converted, list(params))
            return cursor.fetchall()

    def _exec_sync(self, sql, params):
        with connection.cursor() as cursor:
            converted = sql
            for i in range(len(params), 0, -1):
                converted = converted.replace(f"${i}", "%s")
            cursor.execute(converted, list(params))

    async def query(self, sql, *params):
        return await sync_to_async(self._query_sync)(sql, params)

    async def query_one(self, sql, *params):
        rows = await self.query(sql, *params)
        return rows[0] if rows else None

    async def query_val(self, sql, *params):
        row = await self.query_one(sql, *params)
        return row[0] if row else None

    async def execute(self, sql, *params):
        await sync_to_async(self._exec_sync)(sql, params)


def setup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS hyper_audit_log CASCADE")


def cleanup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS hyper_audit_log CASCADE")


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

    setup()

    db = SyncDB()
    audit = AuditLog(db)

    # ── 1. Table creation ─────────────────────────────────────────────────
    print("\n=== Table creation ===")

    asyncio.run(audit.ensure_table())
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM hyper_audit_log")
        count = cursor.fetchone()[0]
    check("table created", count == 0)

    # Idempotent
    asyncio.run(audit.ensure_table())
    check("idempotent create", True)

    # ── 2. Log add ────────────────────────────────────────────────────────
    print("\n=== Log add ===")

    asyncio.run(
        audit.log_add(
            user_id=1,
            model="product",
            object_id="42",
            object_repr="Widget Pro",
            username="alice",
        )
    )

    entries = asyncio.run(audit.get_recent(limit=10))
    check("add entry created", len(entries) == 1)
    check("add action", entries[0]["action"] == "add")
    check("add model", entries[0]["model_name"] == "product")
    check("add object_id", entries[0]["object_id"] == "42")
    check("add object_repr", entries[0]["object_repr"] == "Widget Pro")
    check("add username", entries[0]["username"] == "alice")
    check("add user_id", entries[0]["user_id"] == 1)
    check("add timestamp", entries[0]["timestamp"] is not None)

    # ── 3. Log change with diff ───────────────────────────────────────────
    print("\n=== Log change ===")

    changes = {
        "price": {"old": 9.99, "new": 14.99},
        "name": {"old": "Widget", "new": "Widget Pro"},
    }
    asyncio.run(
        audit.log_change(
            user_id=1,
            model="product",
            object_id="42",
            object_repr="Widget Pro",
            changes=changes,
            username="alice",
        )
    )

    entries = asyncio.run(audit.get_recent(limit=10))
    check("change entry created", len(entries) == 2)
    change_entry = entries[0]  # most recent
    check("change action", change_entry["action"] == "change")
    check("change has diff", "price" in change_entry["changes"])
    check("change diff has old", "9.99" in change_entry["changes"])
    check("change diff has new", "14.99" in change_entry["changes"])

    # ── 4. Log delete ─────────────────────────────────────────────────────
    print("\n=== Log delete ===")

    asyncio.run(
        audit.log_delete(
            user_id=2,
            model="product",
            object_id="42",
            object_repr="Widget Pro",
            username="bob",
        )
    )

    entries = asyncio.run(audit.get_recent(limit=10))
    check("delete entry", len(entries) == 3)
    check("delete action", entries[0]["action"] == "delete")
    check("delete user", entries[0]["username"] == "bob")

    # ── 5. Diff computation ───────────────────────────────────────────────
    print("\n=== Diff computation ===")

    old = {"name": "Widget", "price": 9.99, "active": True}
    new = {"name": "Widget", "price": 14.99, "active": False, "category": "premium"}

    diff = AuditLog.compute_diff(old, new)
    check("diff detects price change", "price" in diff)
    check("diff old price", diff["price"]["old"] == 9.99)
    check("diff new price", diff["price"]["new"] == 14.99)
    check("diff detects active change", "active" in diff)
    check("diff detects new field", "category" in diff)
    check("diff category old is None", diff["category"]["old"] is None)
    check("diff unchanged excluded", "name" not in diff)

    # Empty diff
    same = AuditLog.compute_diff({"a": 1}, {"a": 1})
    check("no changes = empty diff", len(same) == 0)

    # ── 6. Object history ─────────────────────────────────────────────────
    print("\n=== Object history ===")

    history = asyncio.run(audit.get_object_history("product", "42"))
    check("3 history entries", len(history) == 3)
    check("history ordered desc", history[0]["action"] == "delete")
    check("history[1] is change", history[1]["action"] == "change")
    check("history[2] is add", history[2]["action"] == "add")

    # Non-existent object
    empty = asyncio.run(audit.get_object_history("product", "999"))
    check("no history for unknown", len(empty) == 0)

    # ── 7. User activity ──────────────────────────────────────────────────
    print("\n=== User activity ===")

    user1 = asyncio.run(audit.get_user_activity(user_id=1))
    check("user 1 has 2 entries", len(user1) == 2)

    user2 = asyncio.run(audit.get_user_activity(user_id=2))
    check("user 2 has 1 entry", len(user2) == 1)
    check("user 2 entry is delete", user2[0]["action"] == "delete")

    user999 = asyncio.run(audit.get_user_activity(user_id=999))
    check("unknown user empty", len(user999) == 0)

    # ── 8. Model activity ─────────────────────────────────────────────────
    print("\n=== Model activity ===")

    # Add entries for another model
    asyncio.run(
        audit.log_add(
            user_id=1, model="category", object_id="1", object_repr="Electronics"
        )
    )
    asyncio.run(
        audit.log_add(user_id=1, model="category", object_id="2", object_repr="Books")
    )

    product_activity = asyncio.run(audit.get_model_activity("product"))
    check("product activity = 3", len(product_activity) == 3)

    category_activity = asyncio.run(audit.get_model_activity("category"))
    check("category activity = 2", len(category_activity) == 2)

    # ── 9. Count ──────────────────────────────────────────────────────────
    print("\n=== Count ===")

    total = asyncio.run(audit.count())
    check("total count = 5", total == 5)

    product_count = asyncio.run(audit.count(model="product"))
    check("product count = 3", product_count == 3)

    category_count = asyncio.run(audit.count(model="category"))
    check("category count = 2", category_count == 2)

    unknown_count = asyncio.run(audit.count(model="nonexistent"))
    check("unknown model = 0", unknown_count == 0)

    # ── 10. Complex nested diff ───────────────────────────────────────────
    print("\n=== Complex nested diff ===")

    complex_changes = {
        "metadata": {"old": {"tags": ["a"]}, "new": {"tags": ["a", "b"], "score": 95}},
        "items": {"old": [1, 2], "new": [1, 2, 3]},
    }
    asyncio.run(
        audit.log_change(
            user_id=1, model="product", object_id="100", changes=complex_changes
        )
    )

    history = asyncio.run(audit.get_object_history("product", "100"))
    check("complex diff stored", len(history) == 1)
    check("complex diff has metadata", "metadata" in history[0]["changes"])
    check("complex diff has items", "items" in history[0]["changes"])

    # ── 11. Recent with limit ─────────────────────────────────────────────
    print("\n=== Recent with limit ===")

    recent_2 = asyncio.run(audit.get_recent(limit=2))
    check("limit=2 returns 2", len(recent_2) == 2)

    recent_all = asyncio.run(audit.get_recent(limit=100))
    check("all entries returned", len(recent_all) == 6)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup()
    print("  Table dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All audit log tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
