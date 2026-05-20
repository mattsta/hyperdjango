#!/usr/bin/env python3
"""Test object history view + dashboard activity panel + auto audit logging.

Tests:
1. History route registered per model
2. History template content
3. _audit_log helper — logs add/change/delete
4. Auto-audit wiring — save/delete handlers call _audit_log
5. Dashboard recent activity — loads from AuditLog
6. Dashboard template includes activity panel
7. History view renders entries
8. Audit with user context from request

Runs against live PostgreSQL.
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

from hyperdjango.admin import TEMPLATE_DASHBOARD, TEMPLATE_HISTORY, HyperAdmin
from hyperdjango.app import HyperApp
from hyperdjango.auth.audit import CREATE_AUDIT_TABLE_SQL, AuditLog
from hyperdjango.models import Field, Model


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


class HistProduct(Model):
    class Meta:
        table = "hist_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    price: float = Field(ge=0.0, default=0.0)


def setup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS hyper_audit_log CASCADE")
        cursor.execute(CREATE_AUDIT_TABLE_SQL)
        cursor.execute("DROP TABLE IF EXISTS hist_products CASCADE")
        cursor.execute("""CREATE TABLE hist_products (
            id SERIAL PRIMARY KEY, name VARCHAR(200) NOT NULL, price DOUBLE PRECISION DEFAULT 0.0)""")


def cleanup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS hist_products CASCADE")
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
    app = HyperApp(title="History Test")
    admin = HyperAdmin(app, prefix="/hist", require_auth=False)
    admin.app._db = db
    config = admin.register(HistProduct, list_display=["name", "price"])

    # ── 1. History route registered ───────────────────────────────────────
    print("\n=== History route ===")

    routes = [(r.method, r.pattern) for r in app.router.routes()]
    check("history route", ("GET", "/hist/histproduct/{id}/history/") in routes)

    # ── 2. History template ───────────────────────────────────────────────
    print("\n=== History template ===")

    check("template has entries loop", "entries" in TEMPLATE_HISTORY)
    check("template has action display", "Created" in TEMPLATE_HISTORY)
    check("template has Changed", "Changed" in TEMPLATE_HISTORY)
    check("template has Deleted", "Deleted" in TEMPLATE_HISTORY)
    check("template has back link", "Back to edit" in TEMPLATE_HISTORY)
    check("template has timestamp", "timestamp" in TEMPLATE_HISTORY)
    check("template has username", "username" in TEMPLATE_HISTORY)

    # ── 3. _audit_log helper ──────────────────────────────────────────────
    print("\n=== _audit_log helper ===")

    asyncio.run(admin._audit_log("add", config, "1", "Widget"))
    asyncio.run(
        admin._audit_log(
            "change", config, "1", "Widget", changes={"price": {"old": 9, "new": 15}}
        )
    )
    asyncio.run(admin._audit_log("delete", config, "1", "Widget"))

    audit = AuditLog(db)
    entries = asyncio.run(audit.get_object_history("histproduct", "1"))
    check("3 audit entries", len(entries) == 3)
    check("delete first (newest)", entries[0]["action"] == "delete")
    check("change second", entries[1]["action"] == "change")
    check("add third (oldest)", entries[2]["action"] == "add")

    # ── 4. _audit_log with request context ────────────────────────────────
    print("\n=== Audit with user context ===")

    class MockReq:
        _admin_user = {"user_id": 42, "username": "alice"}

    asyncio.run(admin._audit_log("add", config, "99", "Test Item", request=MockReq()))

    entries99 = asyncio.run(audit.get_object_history("histproduct", "99"))
    check("entry has user_id", entries99[0]["user_id"] == 42)
    check("entry has username", entries99[0]["username"] == "alice")

    # ── 5. _audit_log graceful on no table ────────────────────────────────
    print("\n=== Audit graceful failure ===")

    # Should not raise even with bad DB
    class BrokenDB:
        async def execute(self, *a):
            raise RuntimeError("db broken")

    admin2 = HyperAdmin(HyperApp(title="broken"), prefix="/br", require_auth=False)
    admin2.app._db = BrokenDB()
    asyncio.run(admin2._audit_log("add", config, "1", "test"))
    check("graceful on broken db", True)

    # ── 6. Dashboard template has activity panel ──────────────────────────
    print("\n=== Dashboard activity panel ===")

    check("dashboard has recent_activity", "recent_activity" in TEMPLATE_DASHBOARD)
    check(
        "dashboard has Recent Activity heading", "Recent Activity" in TEMPLATE_DASHBOARD
    )
    check("dashboard has action display", "action" in TEMPLATE_DASHBOARD)
    check("dashboard has object_repr", "object_repr" in TEMPLATE_DASHBOARD)
    check("dashboard has model_name", "model_name" in TEMPLATE_DASHBOARD)

    # Render with activity data
    ctx = admin._base_context()
    ctx.update(
        {
            "title": "Dashboard",
            "models": [
                {"slug": "histproduct", "name": "HistProduct", "field_count": 3}
            ],
            "recent_activity": [
                {
                    "action": "add",
                    "object_repr": "Widget",
                    "model_name": "histproduct",
                    "username": "alice",
                },
                {
                    "action": "change",
                    "object_repr": "Gadget",
                    "model_name": "histproduct",
                    "username": "bob",
                },
            ],
            "admin_user": None,
        }
    )
    html = admin._render(TEMPLATE_DASHBOARD, ctx)
    check("activity renders Widget", "Widget" in html)
    check("activity renders Gadget", "Gadget" in html)
    check("activity renders alice", "alice" in html)
    check("activity shows action", "add" in html)

    # ── 7. History template rendering ─────────────────────────────────────
    print("\n=== History rendering ===")

    hist_ctx = admin._base_context()
    hist_ctx.update(
        {
            "title": "History: Widget",
            "model_name": "HistProduct",
            "slug": "histproduct",
            "pk": 1,
            "object_repr": "Widget",
            "entries": [
                {
                    "timestamp": "2024-01-15 10:30:00",
                    "username": "alice",
                    "action": "change",
                    "changes": '{"price": {"old": 9, "new": 15}}',
                },
                {
                    "timestamp": "2024-01-15 09:00:00",
                    "username": "alice",
                    "action": "add",
                    "changes": "",
                },
            ],
        }
    )
    hist_html = admin._render(TEMPLATE_HISTORY, hist_ctx)
    check("history renders", len(hist_html) > 100)
    check("history has object_repr", "Widget" in hist_html)
    check("history has entries", "alice" in hist_html)
    check("history has Created", "Created" in hist_html)
    check("history has Changed", "Changed" in hist_html)
    check("history has back link", "Back to edit" in hist_html)

    # Empty history
    empty_ctx = dict(hist_ctx, entries=[])
    empty_html = admin._render(TEMPLATE_HISTORY, empty_ctx)
    check("empty history message", "No history" in empty_html)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All admin history tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
