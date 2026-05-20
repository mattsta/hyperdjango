#!/usr/bin/env python3
"""Test HyperApp standalone auth/RBAC system.

Tests:
1. Password hashing — argon2id hash + verify + needs_rehash
2. User model — set_password, check_password, properties
3. AnonymousUser — mirrors User API, always unauthenticated
4. Permission checker — create tables, create user, authenticate
5. Permissions — grant/revoke, user + group permissions, superuser bypass
6. Groups — create, add user, group permissions cascade
7. Model permissions — auto-create default CRUD permissions
8. Permission caching — cached on user object, clearable
9. Decorators — require_auth, require_staff, require_permission
10. SessionAuth — session → user loading + optional RBAC checker

Runs against live PostgreSQL via hyperdjango.db (Django connection for table setup).
"""

# hyper-test: db_django

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

from django.db import connection

from hyperdjango.app import HTTPException
from hyperdjango.auth.decorators import require_auth, require_staff
from hyperdjango.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import (
    AnonymousUser,
    User,
    drop_rbac_tables_sync,
    ensure_rbac_tables_sync,
)


def setup_tables():
    with connection.cursor() as cursor:
        drop_rbac_tables_sync(cursor)
        ensure_rbac_tables_sync(cursor)


def cleanup_tables():
    with connection.cursor() as cursor:
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

    setup_tables()

    # ── 1. Password hashing (argon2id) ────────────────────────────────────
    print("\n=== Password hashing ===")

    h = hash_password("my_secure_password")
    check("hash starts with $argon2id", h.startswith("$argon2id$"))
    check("hash is long", len(h) > 50)

    check("verify correct password", verify_password("my_secure_password", h))
    check("verify wrong password", not verify_password("wrong_password", h))
    check("verify empty password", not verify_password("", h))

    check("needs_rehash current params", not needs_rehash(h))

    # Different passwords produce different hashes
    h2 = hash_password("my_secure_password")
    check("different salts", h != h2)
    check("both verify", verify_password("my_secure_password", h2))

    # Invalid hash
    check("verify invalid hash", not verify_password("test", "invalid_hash"))
    check("verify empty hash", not verify_password("test", ""))

    # ── 2. User model ─────────────────────────────────────────────────────
    print("\n=== User model ===")

    user = User(username="alice", email="alice@example.com")
    user.set_password("secret123")
    check("password set", user.password_hash.startswith("$argon2id$"))
    check("check correct", user.check_password("secret123"))
    check("check wrong", not user.check_password("wrong"))
    check("check empty", not user.check_password(""))
    check("is_authenticated", user.is_authenticated)
    check("not is_anonymous", not user.is_anonymous)
    check("str", str(user) == "alice")

    user.first_name = "Alice"
    user.last_name = "Smith"
    check("full_name", user.full_name == "Alice Smith")

    user_no_pw = User(username="nopw", password_hash="")
    check("no password check", not user_no_pw.check_password("anything"))
    check("no password rehash", not user_no_pw.password_needs_rehash())

    # ── 3. AnonymousUser ──────────────────────────────────────────────────
    print("\n=== AnonymousUser ===")

    anon = AnonymousUser()
    check("anon not authenticated", not anon.is_authenticated)
    check("anon is_anonymous", anon.is_anonymous)
    check("anon no perm", not anon.has_perm("anything"))
    check("anon no perms", not anon.has_perms(["a", "b"]))
    check("anon str", str(anon) == "AnonymousUser")
    check("anon id None", anon.id is None)
    check("anon is_staff False", not anon.is_staff)
    check("anon is_superuser False", not anon.is_superuser)
    check("anon full_name empty", anon.full_name == "")

    # ── 4. Permission checker — user CRUD ─────────────────────────────────
    print("\n=== Permission checker — user CRUD ===")

    import asyncio

    from asgiref.sync import sync_to_async

    class SyncDB:
        """Wrapper around Django connection for testing async auth code."""

        def _execute_sync(self, sql, params):
            with connection.cursor() as cursor:
                converted_sql = sql
                for i in range(len(params), 0, -1):
                    converted_sql = converted_sql.replace(f"${i}", "%s")
                cursor.execute(converted_sql, list(params))

        def _query_sync(self, sql, params):
            with connection.cursor() as cursor:
                converted_sql = sql
                for i in range(len(params), 0, -1):
                    converted_sql = converted_sql.replace(f"${i}", "%s")
                cursor.execute(converted_sql, list(params))
                columns = (
                    [col[0] for col in cursor.description] if cursor.description else []
                )
                return [dict(zip(columns, row)) for row in cursor.fetchall()]

        async def execute(self, sql, *params):
            await sync_to_async(self._execute_sync)(sql, params)

        async def query(self, sql, *params):
            return await sync_to_async(self._query_sync)(sql, params)

        async def query_one(self, sql, *params):
            rows = await self.query(sql, *params)
            return rows[0] if rows else None

        async def query_val(self, sql, *params):
            row = await self.query_one(sql, *params)
            if row is None:
                return None
            if isinstance(row, dict):
                return next(iter(row.values()))
            return row[0]

    db = SyncDB()
    checker = PermissionChecker(db)

    # Create user
    user = asyncio.run(
        checker.create_user(
            username="testuser",
            password="test_password_123",
            email="test@example.com",
            is_staff=True,
            first_name="Test",
            last_name="User",
        )
    )
    user_id = user.id
    check("create user returns model", user is not None and user.id > 0)

    # Authenticate
    user_dict = asyncio.run(checker.authenticate("testuser", "test_password_123"))
    check("authenticate success", user_dict is not None)
    check("authenticate username", user_dict["username"] == "testuser")
    check("authenticate email", user_dict["email"] == "test@example.com")
    check("authenticate is_staff", user_dict["is_staff"] is True)

    # Authenticate wrong password
    bad = asyncio.run(checker.authenticate("testuser", "wrong"))
    check("authenticate wrong password", bad is None)

    # Authenticate nonexistent user
    bad2 = asyncio.run(checker.authenticate("nonexistent", "test"))
    check("authenticate nonexistent", bad2 is None)

    # Get user by ID
    fetched = asyncio.run(checker.get_user_by_id(user_id))
    check("get_user_by_id", fetched is not None)
    check("fetched username", fetched["username"] == "testuser")

    # ── 5. Permissions ────────────────────────────────────────────────────
    print("\n=== Permissions ===")

    # Create default permissions for a model
    asyncio.run(checker.create_default_permissions("product", "Product"))

    # Verify permissions exist
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM hyper_permissions WHERE model_name = 'product'"
        )
        perm_count = cursor.fetchone()[0]
    check("4 default permissions created", perm_count == 4)

    # Build a user-like object for permission checking
    class UserObj:
        def __init__(self, uid, active=True, superuser=False):
            self.id = uid
            self.is_active = active
            self.is_superuser = superuser

    normal_user = UserObj(user_id)

    # No permissions yet
    has_add = asyncio.run(checker.has_perm(normal_user, "add_product", "product"))
    check("no perm initially", not has_add)

    # Grant permission
    asyncio.run(checker.grant_user_perm(user_id, "add_product", "product"))
    checker.clear_cache(normal_user)
    has_add = asyncio.run(checker.has_perm(normal_user, "add_product", "product"))
    check("perm granted", has_add)

    # Other perms still denied
    has_delete = asyncio.run(checker.has_perm(normal_user, "delete_product", "product"))
    check("other perm still denied", not has_delete)

    # Grant more
    asyncio.run(checker.grant_user_perm(user_id, "change_product", "product"))
    asyncio.run(checker.grant_user_perm(user_id, "view_product", "product"))
    checker.clear_cache(normal_user)

    model_perms = asyncio.run(checker.has_model_perms(normal_user, "product"))
    check("model perms add", model_perms["add"])
    check("model perms change", model_perms["change"])
    check("model perms view", model_perms["view"])
    check("model perms delete denied", not model_perms["delete"])

    # Revoke
    asyncio.run(checker.revoke_user_perm(user_id, "add_product", "product"))
    checker.clear_cache(normal_user)
    has_add = asyncio.run(checker.has_perm(normal_user, "add_product", "product"))
    check("perm revoked", not has_add)

    # ── 6. Superuser bypass ───────────────────────────────────────────────
    print("\n=== Superuser bypass ===")

    # Create superuser
    su_id = asyncio.run(
        checker.create_user("admin", "admin123", is_superuser=True, is_staff=True)
    )
    su = UserObj(su_id, superuser=True)

    has_any = asyncio.run(checker.has_perm(su, "delete_anything", "anything"))
    check("superuser bypasses all", has_any)

    # Inactive superuser denied
    inactive_su = UserObj(su_id, active=False, superuser=True)
    has_any = asyncio.run(checker.has_perm(inactive_su, "add_product", "product"))
    check("inactive superuser denied", not has_any)

    # ── 7. Groups ─────────────────────────────────────────────────────────
    print("\n=== Groups ===")

    group = asyncio.run(checker.create_group("editors"))
    group_id = group.id
    check("group created", group_id > 0)

    # Grant permission to group
    asyncio.run(checker.grant_group_perm(group_id, "delete_product", "product"))

    # Add user to group
    asyncio.run(checker.add_user_to_group(user_id, group_id))
    checker.clear_cache(normal_user)

    # User now has delete via group
    has_delete = asyncio.run(checker.has_perm(normal_user, "delete_product", "product"))
    check("group perm cascades", has_delete)

    # Remove from group
    asyncio.run(checker.remove_user_from_group(user_id, group_id))
    checker.clear_cache(normal_user)
    has_delete = asyncio.run(checker.has_perm(normal_user, "delete_product", "product"))
    check("group removal revokes", not has_delete)

    # ── 8. Permission caching ─────────────────────────────────────────────
    print("\n=== Permission caching ===")

    checker.clear_cache(normal_user)
    check("cache cleared", not hasattr(normal_user, "_perm_cache_None"))

    # First check populates cache (tenant_id=None → _perm_cache_None)
    asyncio.run(checker.has_perm(normal_user, "change_product", "product"))
    check("cache populated", hasattr(normal_user, "_perm_cache_None"))
    check("cache is set", isinstance(normal_user._perm_cache_None, set))

    # has_perms
    has_both = asyncio.run(
        checker.has_perms(normal_user, ["change_product", "view_product"], "product")
    )
    check("has_perms both", has_both)

    has_missing = asyncio.run(
        checker.has_perms(normal_user, ["change_product", "delete_product"], "product")
    )
    check("has_perms missing one", not has_missing)

    # ── 9. Decorators ─────────────────────────────────────────────────────
    print("\n=== Decorators ===")

    # require_auth
    @require_auth()
    async def protected_view(request):
        return "ok"

    class MockReq:
        def __init__(self, user=None):
            self.user = user

    # Authenticated
    req = MockReq(user=UserObj(1))
    req.user.is_authenticated = True
    try:
        result = asyncio.run(protected_view(req))
        check("require_auth passes", result == "ok")
    except HTTPException:
        check("require_auth passes", False)

    # Unauthenticated — require_auth redirects to LOGIN_URL (302) by default
    req_anon = MockReq(user=AnonymousUser())
    result = asyncio.run(protected_view(req_anon))
    check("require_auth blocks anon", result.status == 302)

    # require_staff
    @require_staff
    async def staff_view(request):
        return "staff ok"

    staff_req = MockReq()
    staff_req.user = UserObj(1)
    staff_req.user.is_authenticated = True
    staff_req.user.is_staff = True
    try:
        result = asyncio.run(staff_view(staff_req))
        check("require_staff passes staff", result == "staff ok")
    except HTTPException:
        check("require_staff passes staff", False)

    non_staff = MockReq()
    non_staff.user = UserObj(1)
    non_staff.user.is_authenticated = True
    non_staff.user.is_staff = False
    try:
        asyncio.run(staff_view(non_staff))
        check("require_staff blocks non-staff", False)
    except HTTPException as e:
        check("require_staff blocks non-staff", e.status_code == 403)

    # ── 10. Multiple models permissions ───────────────────────────────────
    print("\n=== Multiple models ===")

    asyncio.run(checker.create_default_permissions("article", "Article"))
    asyncio.run(checker.create_default_permissions("comment", "Comment"))

    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM hyper_permissions")
        total_perms = cursor.fetchone()[0]
    check("12 total permissions (3 models x 4)", total_perms == 12)

    # Idempotent creation
    asyncio.run(checker.create_default_permissions("product", "Product"))
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM hyper_permissions")
        still_12 = cursor.fetchone()[0]
    check("idempotent perm creation", still_12 == 12)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup_tables()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All auth system tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
