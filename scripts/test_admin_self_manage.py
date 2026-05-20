#!/usr/bin/env python3
"""Test self-managing admin — User/Group/Permission CRUD within HyperAdmin.

Tests:
1. register_auth_models() registers User, Group, Permission
2. User config — fieldsets, list_display, password field injection
3. Group config — list_display, search
4. Permission config — list_display, list_filter
5. Password hook — hashing on create, skip on edit, virtual field handling
6. Routes for auth model CRUD
7. Virtual _new_password field introspection
8. User create flow — password hashed via hook
9. User edit flow — password unchanged when blank
10. Self-contained: admin manages its own users

Runs against live PostgreSQL.
"""

# hyper-test: db_django

import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.admin_settings"

import django

django.setup()

import asyncio

from django.db import connection

from hyperdjango.admin import HyperAdmin
from hyperdjango.app import HyperApp
from hyperdjango.auth.passwords import verify_password
from hyperdjango.auth.user import drop_rbac_tables_sync, ensure_rbac_tables_sync


def setup():
    with connection.cursor() as cursor:
        drop_rbac_tables_sync(cursor)
        ensure_rbac_tables_sync(cursor)


def cleanup():
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

    setup()

    # ── 1. register_auth_models ───────────────────────────────────────────
    print("\n=== register_auth_models ===")

    app = HyperApp(title="Self-Manage Test")
    admin = HyperAdmin(app, prefix="/sm", require_auth=False)
    admin.register_auth_models()

    check("users registered", "users" in admin._models)
    check("groups registered", "groups" in admin._models)
    check("permissions registered", "permissions" in admin._models)

    # ── 2. User config ────────────────────────────────────────────────────
    print("\n=== User config ===")

    user_cfg = admin._models["users"]
    check(
        "user list_display",
        user_cfg.list_display
        == ["username", "email", "is_staff", "is_superuser", "is_active"],
    )
    check("user search_fields", "username" in user_cfg.search_fields)
    check("user list_filter", "is_staff" in user_cfg.list_filter)
    check(
        "user has fieldsets",
        user_cfg.fieldsets is not None and len(user_cfg.fieldsets) == 3,
    )
    check("user fieldset 0 title", user_cfg.fieldsets[0].title == "Account")
    check("user fieldset 1 title", user_cfg.fieldsets[1].title == "Personal")
    check("user fieldset 2 title", user_cfg.fieldsets[2].title == "Permissions")
    check("user excludes password_hash", "password_hash" in user_cfg.exclude_fields)
    check("user excludes last_login", "last_login" in user_cfg.exclude_fields)
    check(
        "user has save_hooks (password + escalation guard + revocation)",
        len(user_cfg.save_hooks) == 3,
    )
    check("user ordering", user_cfg.ordering == "-id")

    # ── 3. Virtual password field ─────────────────────────────────────────
    print("\n=== Virtual password field ===")

    pw_field = next((f for f in user_cfg.fields if f.name == "_new_password"), None)
    check("_new_password field exists", pw_field is not None)
    if pw_field:
        check("pw widget is password", pw_field.widget == "password")
        check("pw not required", not pw_field.required)
        check(
            "pw has placeholder",
            "blank" in pw_field.attrs.get("placeholder", "").lower(),
        )
        check("pw not auto", not pw_field.is_auto)

    # ── 4. Group config ───────────────────────────────────────────────────
    print("\n=== Group config ===")

    group_cfg = admin._models["groups"]
    check(
        "group list_display",
        group_cfg.list_display == ["name", "parent_id", "priority"],
    )
    check("group search", "name" in group_cfg.search_fields)

    # ── 5. Permission config ──────────────────────────────────────────────
    print("\n=== Permission config ===")

    perm_cfg = admin._models["permissions"]
    check(
        "perm list_display", perm_cfg.list_display == ["codename", "name", "model_name"]
    )
    check("perm list_filter", "model_name" in perm_cfg.list_filter)
    check("perm ordering", perm_cfg.ordering == "model_name")

    # ── 6. Routes ─────────────────────────────────────────────────────────
    print("\n=== Routes ===")

    routes = [(r.method, r.pattern) for r in app.router.routes()]
    check("users list", ("GET", "/sm/users/") in routes)
    check("users add", ("GET", "/sm/users/add/") in routes)
    check("users edit", ("GET", "/sm/users/{id}/") in routes)
    check("groups list", ("GET", "/sm/groups/") in routes)
    check("permissions list", ("GET", "/sm/permissions/") in routes)

    # ── 7. Password hook ─────────────────────────────────────────────────
    print("\n=== Password hook ===")

    hook = user_cfg.save_hooks[0]

    # Create with password
    values_create = asyncio.run(
        hook(
            {"username": "testuser", "email": "t@t.com", "_new_password": "secret123"},
            False,  # is_edit=False
        )
    )
    check("password hashed", "password_hash" in values_create)
    check("hash is argon2", values_create["password_hash"].startswith("$argon2id$"))
    check("_new_password removed", "_new_password" not in values_create)
    check(
        "password verifies",
        verify_password("secret123", values_create["password_hash"]),
    )

    # Edit without changing password
    values_edit = asyncio.run(
        hook(
            {"username": "testuser", "email": "t@t.com", "_new_password": ""},
            True,  # is_edit=True
        )
    )
    check("no password change", "password_hash" not in values_edit)
    check("_new_password removed (edit)", "_new_password" not in values_edit)

    # Edit with new password
    values_edit2 = asyncio.run(
        hook(
            {"username": "testuser", "_new_password": "newpass456"},
            True,
        )
    )
    check(
        "password updated on edit",
        values_edit2["password_hash"].startswith("$argon2id$"),
    )
    check(
        "new password verifies",
        verify_password("newpass456", values_edit2["password_hash"]),
    )

    # Create without password
    values_no_pw = asyncio.run(
        hook(
            {"username": "nopw", "_new_password": ""},
            False,
        )
    )
    check("no password → empty hash", values_no_pw.get("password_hash") == "")

    # ── 8. Form field groups include password ─────────────────────────────
    print("\n=== Form field groups ===")

    groups = admin._build_form_field_groups(user_cfg, values={"username": "alice"})
    # Password should be in Account group
    account_group = groups[0]
    check("account group title", account_group["title"] == "Account")
    field_names = [f["name"] for f in account_group["fields"]]
    check("username in account", "username" in field_names)
    check("_new_password in account", "_new_password" in field_names)

    # Password field renders as password type
    pw_ff = next(
        (f for f in account_group["fields"] if f["name"] == "_new_password"), None
    )
    check("pw field in form", pw_ff is not None)
    if pw_ff:
        check("pw widget type", pw_ff["widget"] == "password")

    # ── 9. Template rendering with auth models ────────────────────────────
    print("\n=== Template rendering ===")

    ctx = admin._base_context()
    model_names = [m["name"] for m in ctx["registered_models"]]
    check("User in nav", "User" in model_names)
    check("Group in nav", "Group" in model_names)
    check("Permission in nav", "Permission" in model_names)

    # ── 10. Return value ──────────────────────────────────────────────────
    print("\n=== Return value ===")

    app2 = HyperApp(title="Chain")
    admin2 = HyperAdmin(app2, prefix="/c", require_auth=False)
    result = admin2.register_auth_models()
    check("returns self for chaining", result is admin2)

    # ── Cleanup ───────────────────────────────────────────────────────────
    print("\n=== Cleanup ===")
    cleanup()
    print("  Tables dropped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All self-managing admin tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
