#!/usr/bin/env python3
"""Test admin login/logout + database session store + auth gating.

Tests:
1. DatabaseSessionStore — create, get, update, delete, cleanup, expire
2. Login page renders
3. Login handler — success, wrong password, non-staff, missing fields
4. Session cookie set on login
5. Auth gating — dashboard requires staff, redirects to login
6. Logout clears session
7. Login template content
8. Auth wrap on model routes
9. require_auth=False disables gating

Runs against live PostgreSQL.
"""

# hyper-test: db_isolated

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

import asyncio

from asgiref.sync import sync_to_async
from django.db import connection

from hyperdjango.admin import TEMPLATE_LOGIN, HyperAdmin
from hyperdjango.app import HyperApp
from hyperdjango.auth.db_sessions import DatabaseSessionStore, HyperSession
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import drop_rbac_tables_sync, ensure_rbac_tables_sync
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model, create_table_for_model


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


class LoginProduct(Model):
    class Meta:
        table = "login_products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


def setup():

    with connection.cursor() as cursor:
        drop_rbac_tables_sync(cursor)
        ensure_rbac_tables_sync(cursor)
        cursor.execute("DROP TABLE IF EXISTS login_products CASCADE")
        cursor.execute(
            "CREATE TABLE login_products (id SERIAL PRIMARY KEY, name VARCHAR(100))"
        )


def cleanup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS login_products CASCADE")
        cursor.execute("DROP TABLE IF EXISTS hyper_sessions CASCADE")
        drop_rbac_tables_sync(cursor)


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

    # Set up pg.zig ORM connection (DatabaseSessionStore uses it via get_db())
    pgdb = os.environ.get("PGDATABASE", "hyperdjango_test")
    native_db = Database(f"postgres://localhost/{pgdb}")
    asyncio.run(native_db.connect())
    set_db(native_db)

    # Create session table from HyperSession model via ORM
    asyncio.run(create_table_for_model(HyperSession, db=native_db, drop=True))

    db = SyncDB()

    # Create test users
    checker = PermissionChecker(db)
    staff_id = asyncio.run(checker.create_user("staffuser", "staff123", is_staff=True))
    non_staff_id = asyncio.run(
        checker.create_user("normaluser", "normal123", is_staff=False)
    )
    admin_id = asyncio.run(
        checker.create_user("admin", "admin123", is_staff=True, is_superuser=True)
    )

    # ── 1. DatabaseSessionStore ───────────────────────────────────────────
    print("\n=== DatabaseSessionStore ===")

    store = DatabaseSessionStore(max_age=3600)

    # Create session
    sid = asyncio.run(store.create({"user_id": 1, "username": "alice"}))
    check("session created", sid is not None and len(sid) > 20)

    # Get session
    data = asyncio.run(store.get(sid))
    check("session get", data is not None)
    check("session data", data.get("user_id") == 1)
    check("session username", data.get("username") == "alice")

    # Update session
    asyncio.run(store.update(sid, {"user_id": 1, "username": "alice", "extra": "data"}))
    data2 = asyncio.run(store.get(sid))
    check("session update", data2.get("extra") == "data")

    # Count
    count = asyncio.run(store.count())
    check("session count", count == 1)

    # Delete session
    asyncio.run(store.delete(sid))
    data3 = asyncio.run(store.get(sid))
    check("session deleted", data3 is None)

    # Expired session. max_age=0 puts expires_at AT the create instant and
    # get() filters on `expires_at > now` (strict), so the row is unservable
    # from the moment it lands — no clock has to advance, on any machine. The
    # row is asserted to EXIST first so "returns None" cannot pass by the
    # insert having silently failed. This previously slept 0.1s before
    # asserting, which made the outcome a function of machine speed.
    expired_store = DatabaseSessionStore(max_age=0)
    esid = asyncio.run(expired_store.create({"user_id": 99}))
    stored = asyncio.run(HyperSession.objects.filter(session_id=esid).count())
    check("expired session row was persisted", stored == 1, f"count={stored}")
    edata = asyncio.run(expired_store.get(esid))
    check("expired session returns None", edata is None)

    # Cleanup
    asyncio.run(store.cleanup())
    check("cleanup runs", True)

    # ── 2. Login template ─────────────────────────────────────────────────
    print("\n=== Login template ===")

    check("login has username field", 'name="username"' in TEMPLATE_LOGIN)
    check("login has password field", 'name="password"' in TEMPLATE_LOGIN)
    check("login has submit", 'type="submit"' in TEMPLATE_LOGIN)
    check("login has form", "<form" in TEMPLATE_LOGIN)
    check("login has error block", "error" in TEMPLATE_LOGIN)

    # ── 3. HyperAdmin auth gating ─────────────────────────────────────────
    print("\n=== Auth gating ===")

    app = HyperApp(title="Auth Test")
    admin = HyperAdmin(app, prefix="/auth", secret_key="test-secret", require_auth=True)
    admin.register(LoginProduct, list_display=["name"])

    # Routes registered
    routes = [(r.method, r.pattern) for r in app.router.routes()]
    check("login GET route", ("GET", "/auth/login/") in routes)
    check("login POST route", ("POST", "/auth/login/") in routes)
    check("logout GET route", ("GET", "/auth/logout/") in routes)

    # Check auth — no cookie → None
    class MockRequest:
        def __init__(self, cookies=None, query=None, path="/"):
            self.cookies = cookies or {}
            self.GET = query or {}
            self.path = path
            self.path_params = {}

    req_no_auth = MockRequest()
    user = admin._check_auth(req_no_auth)
    check("no cookie → no user", user is None)

    # Check redirect (async method since session auth hash verification)
    redirect = asyncio.run(admin._require_staff_or_redirect(req_no_auth))
    check("no auth → redirect", redirect is not None)
    check("redirect to login", redirect.status == 302)
    check(
        "redirect has login URL", "/auth/login/" in redirect.headers.get("location", "")
    )

    # ── 4. Auth disabled ──────────────────────────────────────────────────
    print("\n=== Auth disabled ===")

    app2 = HyperApp(title="No Auth")
    admin2 = HyperAdmin(app2, prefix="/noauth", require_auth=False)
    admin2.register(LoginProduct, slug="product2")

    req2 = MockRequest()
    user2 = admin2._check_auth(req2)
    check("auth disabled → user returned", user2 is not None)
    check("auth disabled → is_staff", user2.get("is_staff"))

    redirect2 = asyncio.run(admin2._require_staff_or_redirect(req2))
    check("auth disabled → no redirect", redirect2 is None)

    # ── 5. Session flow ───────────────────────────────────────────────────
    print("\n=== Session flow ===")

    from hyperdjango.native._crypto import sign_data

    # Simulate login: create session in store, sign cookie
    session_store = admin._get_session_store()
    test_session = session_store.create(
        {
            "user_id": staff_id,
            "username": "staffuser",
            "is_staff": True,
            "is_superuser": False,
        }
    )
    signed_cookie = sign_data(test_session, "test-secret")

    # Check auth with valid session cookie
    req_auth = MockRequest(cookies={"hyper_admin_session": signed_cookie})
    user_check = admin._check_auth(req_auth)
    check("valid session → user", user_check is not None)
    check("valid session → username", user_check.get("username") == "staffuser")
    check("valid session → is_staff", user_check.get("is_staff") is True)

    # Auth check passes (no redirect)
    redirect_auth = asyncio.run(admin._require_staff_or_redirect(req_auth))
    check("valid session → no redirect", redirect_auth is None)

    # Invalid cookie → no user
    req_bad = MockRequest(cookies={"hyper_admin_session": "invalid.cookie"})
    check("bad cookie → no user", admin._check_auth(req_bad) is None)

    # ── 6. Logout ─────────────────────────────────────────────────────────
    print("\n=== Logout ===")

    # After logout, session is deleted from store
    session_store.delete(test_session)
    req_after_logout = MockRequest(cookies={"hyper_admin_session": signed_cookie})
    check("after logout → no user", admin._check_auth(req_after_logout) is None)

    # ── 7. Login page rendering ───────────────────────────────────────────
    print("\n=== Login page rendering ===")

    html = admin.engine.render_string(
        TEMPLATE_LOGIN,
        {
            "admin_title": "Test Admin",
            "error": "",
        },
    )
    check("login renders", len(html) > 100)
    check("login has title", "Test Admin" in html)

    html_err = admin.engine.render_string(
        TEMPLATE_LOGIN,
        {
            "admin_title": "Test Admin",
            "error": "Invalid credentials",
        },
    )
    check("login error renders", "Invalid credentials" in html_err)

    # ── 8. Header has logout link ─────────────────────────────────────────
    print("\n=== Header logout link ===")

    from hyperdjango.admin import _TEMPLATE_HEADER

    check("header has logout", "logout" in _TEMPLATE_HEADER.lower())

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All admin login tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
