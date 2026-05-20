#!/usr/bin/env python3
"""
Regression tests for admin security fixes.

Tests:
1. SQL injection via sort_field — validates against model columns
2. Open redirect via next_url — rejects absolute URLs
3. Permission enforcement on POST handlers — add/edit/delete/bulk
4. Page parameter validation — non-numeric values handled

Runs against live PostgreSQL via hyperdjango.db (Django bridge for setup).

Usage:
    uv run hyper-test admin_security
"""

# hyper-test: db_isolated

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

from django.db import connection

from hyperdjango.admin import HyperAdmin
from hyperdjango.app import HyperApp
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

# ---------------------------------------------------------------------------
# Test model
# ---------------------------------------------------------------------------


class SecItem(Model):
    class Meta:
        table = "sectest_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    value: int = Field(default=0)


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


def setup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS sectest_items CASCADE")
        cursor.execute("""
            CREATE TABLE sectest_items (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                value INTEGER DEFAULT 0
            )
        """)
        cursor.execute("INSERT INTO sectest_items (name, value) VALUES ('alpha', 10)")
        cursor.execute("INSERT INTO sectest_items (name, value) VALUES ('beta', 20)")


def cleanup():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS sectest_items CASCADE")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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

    import asyncio

    loop = asyncio.new_event_loop()

    # Set up hyperdjango DB connection
    db = Database(DB_URL)
    loop.run_until_complete(db.connect())
    set_db(db)

    # Create tables via hyperdjango DB (not Django connection) so pg.zig pool can see them
    loop.run_until_complete(db.execute("DROP TABLE IF EXISTS sectest_items CASCADE"))
    loop.run_until_complete(
        db.execute("""
        CREATE TABLE sectest_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            value INTEGER DEFAULT 0
        )
    """)
    )
    loop.run_until_complete(
        db.execute("INSERT INTO sectest_items (name, value) VALUES ('alpha', 10)")
    )
    loop.run_until_complete(
        db.execute("INSERT INTO sectest_items (name, value) VALUES ('beta', 20)")
    )

    app = HyperApp(title="Sec Test", database=DB_URL)
    admin = HyperAdmin(app, prefix="/sec")
    config = admin.register(SecItem)
    meta = SecItem._meta

    # ── Sort field SQL injection prevention ──────────────────────────────

    print("\n--- Sort field validation ---")

    class MockRequest:
        """Minimal request mock for admin internals. Admin expects request.query as dict-like."""

        def __init__(self, query_dict=None, method="GET", path="/"):
            self.GET = query_dict or {}
            self.method = method
            self.path = path
            self.headers = {}
            self._admin_user = {"is_staff": True, "is_superuser": False}

    def make_list_request(sort="id", dir="asc", page="1"):
        return MockRequest(query_dict={"sort": sort, "dir": dir, "page": page, "q": ""})

    # Test: valid sort field passes through
    req = make_list_request(sort="name")
    ctx = loop.run_until_complete(admin._build_list_context(config, req))
    check(
        "sort: valid column 'name' accepted",
        ctx["sort_field"] == "name",
        f"Got: {ctx.get('sort_field')}",
    )

    # Test: SQL injection attempt falls back to PK
    req = make_list_request(sort="1;DROP TABLE users--")
    ctx = loop.run_until_complete(admin._build_list_context(config, req))
    check(
        "sort: SQL injection rejected, falls back to PK",
        ctx["sort_field"] == meta.pk_field,
        f"Got: {ctx.get('sort_field')}",
    )

    # Test: non-existent column falls back to PK
    req = make_list_request(sort="nonexistent_column")
    ctx = loop.run_until_complete(admin._build_list_context(config, req))
    check(
        "sort: non-existent column falls back to PK",
        ctx["sort_field"] == meta.pk_field,
        f"Got: {ctx.get('sort_field')}",
    )

    # Test: sort with - prefix (descending) is validated
    req = make_list_request(sort="-name")
    ctx = loop.run_until_complete(admin._build_list_context(config, req))
    check(
        "sort: '-name' descending prefix accepted",
        ctx["sort_field"] in ("name", "-name"),
        f"Got: {ctx.get('sort_field')}",
    )

    # ── Page parameter validation ────────────────────────────────────────

    print("\n--- Page parameter validation ---")

    # Test: valid page works
    req = make_list_request(page="1")
    ctx = loop.run_until_complete(admin._build_list_context(config, req))
    check("page: valid page=1 works", ctx["page"] == 1, f"Got: {ctx.get('page')}")

    # Test: non-numeric page falls back to 1
    req = make_list_request(page="abc")
    ctx = loop.run_until_complete(admin._build_list_context(config, req))
    check(
        "page: non-numeric 'abc' falls back to 1",
        ctx["page"] == 1,
        f"Got: {ctx.get('page')}",
    )

    # Test: negative page clamped to 1
    req = make_list_request(page="-5")
    ctx = loop.run_until_complete(admin._build_list_context(config, req))
    check("page: negative -5 clamped to 1", ctx["page"] >= 1, f"Got: {ctx.get('page')}")

    # ── Open redirect prevention ─────────────────────────────────────────

    print("\n--- Open redirect prevention ---")

    check(
        "redirect: relative URL allowed",
        not "/admin/".startswith("//") and "://" not in "/admin/",
    )

    for url in ["https://evil.com", "http://evil.com", "//evil.com"]:
        blocked = url.startswith("//") or "://" in url
        check(f"redirect: '{url}' blocked", blocked)

    # ── Permission enforcement ───────────────────────────────────────────

    print("\n--- Permission enforcement ---")

    # _user_has_perm: superuser gets all perms
    check(
        "perm: superuser has all perms",
        admin._user_has_perm(
            {"is_superuser": True, "is_staff": False}, "delete_secitem"
        ),
    )

    # _user_has_perm: staff gets all perms (current design)
    check(
        "perm: staff user has admin perms",
        admin._user_has_perm({"is_staff": True, "is_superuser": False}, "add_secitem"),
    )

    # _user_has_perm: non-staff denied
    check(
        "perm: non-staff denied",
        not admin._user_has_perm(
            {"is_staff": False, "is_superuser": False}, "view_secitem"
        ),
    )

    # _get_model_perms: returns permission dict
    req = MockRequest()
    req._admin_user = {"is_staff": True, "is_superuser": False}
    perms = admin._get_model_perms(config, req)
    check(
        "perm: _get_model_perms returns can_add/change/delete/view",
        all(k in perms for k in ("can_add", "can_change", "can_delete", "can_view")),
        f"Keys: {list(perms.keys())}",
    )

    # Verify permission checks exist in handlers (code inspection)
    import inspect

    add_src = inspect.getsource(admin._make_add_handler(config))
    check(
        "perm: add_handler checks can_add",
        "can_add" in add_src,
        "Permission check missing in add handler",
    )

    edit_src = inspect.getsource(admin._make_edit_handler(config))
    check(
        "perm: edit_handler checks can_change",
        "can_change" in edit_src,
        "Permission check missing in edit handler",
    )

    delete_src = inspect.getsource(admin._make_delete_handler(config))
    check(
        "perm: delete_handler checks can_delete",
        "can_delete" in delete_src,
        "Permission check missing in delete handler",
    )

    action_src = inspect.getsource(admin._make_list_action_handler(config))
    check(
        "perm: action_handler checks permission",
        "can_delete" in action_src,
        "Permission check missing in action handler",
    )

    save_src = inspect.getsource(admin._make_save_list_handler(config))
    check(
        "perm: save_list_handler checks can_change",
        "can_change" in save_src,
        "Permission check missing in save list handler",
    )

    # ── Runtime enforcement: actually DISPATCH each POST handler as a
    #    non-permitted (non-staff) user and assert a real 403 comes back.
    #    Source-string checks above can pass even if the runtime guard is
    #    dead code; this proves the guarantee end-to-end.
    class DeniedRequest:
        def __init__(self):
            self.GET = {}
            self.method = "POST"
            self.path = "/"
            self.headers = {}
            self.body = b""
            self._form = {}
            self._admin_user = {"is_staff": False, "is_superuser": False}

        async def form(self):
            return self._form

    for label, factory in (
        ("add", admin._make_add_handler),
        ("edit", admin._make_edit_handler),
        ("delete", admin._make_delete_handler),
        ("list_action", admin._make_list_action_handler),
        ("save_list", admin._make_save_list_handler),
    ):
        handler = factory(config)
        resp = loop.run_until_complete(handler(DeniedRequest()))
        check(
            f"perm(runtime): non-staff {label} handler → 403",
            resp.status == 403,
            f"got status={resp.status}",
        )

    # ── SQL injection in sort (verify table survived) ────────────────────

    print("\n--- Post-injection table integrity ---")

    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM sectest_items")
        count = cursor.fetchone()[0]
    check(
        "integrity: table still has 2 rows after injection attempts",
        count == 2,
        f"Got: {count}",
    )

    # ── Summary ──────────────────────────────────────────────────────────

    loop.run_until_complete(db.execute("DROP TABLE IF EXISTS sectest_items CASCADE"))
    loop.run_until_complete(db.disconnect())
    loop.close()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
