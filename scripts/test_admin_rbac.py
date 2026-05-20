"""
Regression tests for admin RBAC (real permission checking).

Tests:
1. _user_has_perm: superuser bypasses all checks
2. _user_has_perm: explicit _permissions set controls access
3. _user_has_perm: staff without _permissions gets all (Django fallback)
4. _user_has_perm: non-staff without _permissions denied
5. _get_model_perms: returns correct flags based on user permissions
6. _load_user_permissions: loads from DB and caches

Usage:
    uv run hyper-test admin_rbac
"""

# hyper-test: db_isolated

import asyncio
import contextlib
import inspect
import os
import sys
import traceback

from hyperdjango.admin import HyperAdmin
from hyperdjango.app import HyperApp
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.request import Request

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


class RBACItem(Model):
    class Meta:
        table = "rbac_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)


# ---------------------------------------------------------------------------
# _user_has_perm
# ---------------------------------------------------------------------------


@test("RBAC: superuser bypasses all permission checks")
def test_superuser_bypass():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    user = {"is_superuser": True, "is_staff": False}
    assert admin._user_has_perm(user, "add_anything") is True
    assert admin._user_has_perm(user, "delete_everything") is True


@test("RBAC: explicit _permissions set — allowed perm")
def test_explicit_perms_allowed():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    user = {
        "is_staff": True,
        "is_superuser": False,
        "_permissions": {"add_rbacitem", "view_rbacitem"},
    }
    assert admin._user_has_perm(user, "add_rbacitem") is True
    assert admin._user_has_perm(user, "view_rbacitem") is True


@test("RBAC: explicit _permissions set — denied perm")
def test_explicit_perms_denied():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    user = {"is_staff": True, "is_superuser": False, "_permissions": {"view_rbacitem"}}
    assert admin._user_has_perm(user, "delete_rbacitem") is False
    assert admin._user_has_perm(user, "add_rbacitem") is False


@test("RBAC: empty _permissions set — all denied")
def test_empty_perms():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    user = {"is_staff": True, "is_superuser": False, "_permissions": set()}
    assert admin._user_has_perm(user, "add_rbacitem") is False


@test("RBAC: staff without _permissions — fallback grants all (Django default)")
def test_staff_fallback():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    user = {"is_staff": True, "is_superuser": False}
    # No _permissions key → fallback to is_staff grants all
    assert admin._user_has_perm(user, "add_rbacitem") is True
    assert admin._user_has_perm(user, "delete_rbacitem") is True


@test("RBAC: non-staff without _permissions — denied")
def test_non_staff_denied():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    user = {"is_staff": False, "is_superuser": False}
    assert admin._user_has_perm(user, "view_rbacitem") is False


# ---------------------------------------------------------------------------
# _get_model_perms with _permissions
# ---------------------------------------------------------------------------


@test("RBAC: _get_model_perms respects explicit permissions")
def test_model_perms_explicit():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")
    config = admin.register(RBACItem)

    req = Request(method="GET", path="/admin/rbacitem/")
    req._admin_user = {
        "is_staff": True,
        "is_superuser": False,
        "_permissions": {"add_rbacitem", "view_rbacitem"},
    }

    perms = admin._get_model_perms(config, req)
    assert perms["can_add"] is True
    assert perms["can_view"] is True
    assert perms["can_change"] is False
    assert perms["can_delete"] is False


@test("RBAC: _get_model_perms caches on request")
def test_model_perms_cached():
    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")
    config = admin.register(RBACItem)

    req = Request(method="GET", path="/admin/rbacitem/")
    req._admin_user = {
        "is_staff": True,
        "is_superuser": False,
        "_permissions": {"view_rbacitem"},
    }

    perms1 = admin._get_model_perms(config, req)
    perms2 = admin._get_model_perms(config, req)
    assert perms1 is perms2  # Same dict object (cached)


# ---------------------------------------------------------------------------
# _load_user_permissions
# ---------------------------------------------------------------------------


@test("RBAC: _load_user_permissions loads from DB")
async def test_load_perms_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.auth.passwords import hash_password
    from hyperdjango.auth.user import ensure_rbac_tables

    await ensure_rbac_tables(db)

    # Create user
    pw = hash_password("testpass")
    with contextlib.suppress(Exception):
        await db.execute(
            "INSERT INTO hyper_users (id, username, email, password_hash, is_active, is_staff) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            9999,
            "rbac_test_user",
            "rbac@test.com",
            pw,
            True,
            True,
        )

    # Create permissions
    try:
        await db.execute(
            "INSERT INTO hyper_permissions (id, codename, name, model_name) VALUES ($1, $2, $3, $4)",
            9991,
            "add_rbacitem",
            "Can add rbacitem",
            "rbacitem",
        )
        await db.execute(
            "INSERT INTO hyper_permissions (id, codename, name, model_name) VALUES ($1, $2, $3, $4)",
            9992,
            "view_rbacitem",
            "Can view rbacitem",
            "rbacitem",
        )
    except Exception:
        pass

    # Grant permissions to user
    try:
        await db.execute(
            "INSERT INTO hyper_user_permissions (user_id, permission_id) VALUES ($1, $2)",
            9999,
            9991,
        )
        await db.execute(
            "INSERT INTO hyper_user_permissions (user_id, permission_id) VALUES ($1, $2)",
            9999,
            9992,
        )
    except Exception:
        pass

    app = HyperApp(title="test", database=DB_URL)
    admin = HyperAdmin(app, prefix="/admin")

    user = {"id": 9999, "is_staff": True, "is_superuser": False}
    await admin._load_user_permissions(user)

    assert "_permissions" in user
    assert "add_rbacitem" in user["_permissions"]
    assert "view_rbacitem" in user["_permissions"]
    assert "delete_rbacitem" not in user["_permissions"]

    # Cleanup
    await db.execute("DELETE FROM hyper_user_permissions WHERE user_id = 9999")
    await db.execute("DELETE FROM hyper_permissions WHERE id IN (9991, 9992)")
    await db.execute("DELETE FROM hyper_users WHERE id = 9999")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nAdmin RBAC Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
