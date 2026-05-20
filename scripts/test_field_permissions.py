"""
Tests for field-level RBAC permissions in REST serializers.

Validates:
1. PermissionChecker.get_field_access() returns correct access map
2. PermissionChecker.filter_fields() filters hidden/readonly fields
3. ViewSet.apply_field_permissions() integrates with serializer output
4. SessionUser.has_perm() and .in_group() O(1) membership checks

Usage:
    uv run hyper-test field_permissions
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import (
    AnonymousUser,
    FieldPermission,
    SessionUser,
    ensure_rbac_tables,
)
from hyperdjango.database import Database, set_db

PASS = 0
FAIL = 0
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)


# ── SessionUser frozenset tests (no DB needed) ─────────────────────────


def test_session_user_groups_frozenset():
    """SessionUser.groups is a frozenset, not a list."""
    print("\n--- SessionUser.groups frozenset ---")
    user = SessionUser({"id": 1, "groups": ["staff", "editor"]})
    check("groups is frozenset", isinstance(user.groups, frozenset))
    check("groups has staff", "staff" in user.groups)
    check("groups has editor", "editor" in user.groups)
    check("in_group staff", user.in_group("staff"))
    check("not in_group admin", not user.in_group("admin"))


def test_session_user_permissions_frozenset():
    """SessionUser.permissions is a frozenset, not a list."""
    print("\n--- SessionUser.permissions frozenset ---")
    user = SessionUser({"id": 1, "permissions": ["add_book", "view_book"]})
    check("permissions is frozenset", isinstance(user.permissions, frozenset))
    check("has_perm add_book", user.has_perm("add_book"))
    check("has_perm view_book", user.has_perm("view_book"))
    check("not has_perm delete_book", not user.has_perm("delete_book"))


def test_session_user_superuser_grants_all_perms():
    """SessionUser in superuser group has all permissions."""
    print("\n--- SessionUser superuser grants all ---")
    user = SessionUser({"id": 1, "groups": ["superuser"], "permissions": []})
    check("has_perm anything", user.has_perm("literally_anything"))
    check("is_superuser", user.is_superuser)


def test_session_user_is_staff_derived():
    """is_staff and is_superuser derived from groups."""
    print("\n--- SessionUser is_staff/is_superuser derived ---")
    staff = SessionUser({"id": 1, "groups": ["staff"]})
    check("staff is_staff=True", staff.is_staff)
    check("staff is_superuser=False", not staff.is_superuser)

    su = SessionUser({"id": 2, "groups": ["superuser", "staff"]})
    check("su is_staff=True", su.is_staff)
    check("su is_superuser=True", su.is_superuser)

    nobody = SessionUser({"id": 3, "groups": []})
    check("nobody is_staff=False", not nobody.is_staff)
    check("nobody is_superuser=False", not nobody.is_superuser)


def test_anonymous_user_interface():
    """AnonymousUser has groups/permissions/in_group/has_perm."""
    print("\n--- AnonymousUser interface ---")
    anon = AnonymousUser()
    check("anon groups empty", len(anon.groups) == 0)
    check("anon permissions empty", len(anon.permissions) == 0)
    check("anon in_group False", not anon.in_group("staff"))
    check("anon has_perm False", not anon.has_perm("anything"))
    check("anon get returns default", anon.get("groups", "default") == "default")


def test_session_user_empty_groups():
    """SessionUser with no groups key in dict."""
    print("\n--- SessionUser empty groups ---")
    user = SessionUser({"id": 1})
    check("groups is frozenset", isinstance(user.groups, frozenset))
    check("groups is empty", len(user.groups) == 0)
    check("in_group returns False", not user.in_group("staff"))


# ── DB-backed field permissions tests ───────────────────────────────────


async def test_field_permissions_db():
    """Test PermissionChecker.get_field_access and filter_fields with live DB."""
    print("\n--- FieldPermission DB tests ---")

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await ensure_rbac_tables(db=db)

    checker = PermissionChecker(db)

    # Create a test group and user
    viewer_group = await checker.ensure_group("viewer")
    test_user = await checker.create_user("fp_test_user", "test123", is_staff=False)
    await checker.add_user_to_group(test_user.id, viewer_group.id)

    # Create field permissions: salary is hidden, email is readonly for viewer
    fp1 = FieldPermission(
        model_name="employee",
        field_name="salary",
        group_id=viewer_group.id,
        access="hidden",
    )
    await fp1.save(db=db)

    fp2 = FieldPermission(
        model_name="employee",
        field_name="email",
        group_id=viewer_group.id,
        access="readonly",
    )
    await fp2.save(db=db)

    # Test get_field_access
    access_map = await checker.get_field_access(test_user, "employee")
    check("salary is hidden", access_map.get("salary") == "hidden")
    check("email is readonly", access_map.get("email") == "readonly")
    check("name not restricted", "name" not in access_map)

    # Test filter_fields read mode — hidden fields removed
    data = {"name": "Alice", "salary": 100000, "email": "alice@co.com", "dept": "eng"}
    filtered_read = await checker.filter_fields(test_user, "employee", data, "read")
    check("read: name present", "name" in filtered_read)
    check("read: salary hidden", "salary" not in filtered_read)
    check("read: email present (readonly visible on read)", "email" in filtered_read)
    check("read: dept present", "dept" in filtered_read)

    # Test filter_fields write mode — hidden + readonly fields removed
    filtered_write = await checker.filter_fields(test_user, "employee", data, "write")
    check("write: name present", "name" in filtered_write)
    check("write: salary hidden", "salary" not in filtered_write)
    check("write: email readonly stripped", "email" not in filtered_write)
    check("write: dept present", "dept" in filtered_write)

    # Superuser bypasses all field restrictions
    su_user = await checker.create_user(
        "fp_test_su", "test123", is_staff=True, is_superuser=True
    )
    su_access = await checker.get_field_access(su_user, "employee")
    check("superuser has no restrictions", len(su_access) == 0)

    # Test get_all_field_access — returns all models in one call
    all_access = await checker.get_all_field_access(test_user.id)
    check("all_access has employee", "employee" in all_access)
    check(
        "all_access employee salary hidden",
        all_access.get("employee", {}).get("salary") == "hidden",
    )
    check(
        "all_access employee email readonly",
        all_access.get("employee", {}).get("email") == "readonly",
    )

    # Test build_session_data caches field_access
    from hyperdjango.auth.sessions import build_session_data

    session = await build_session_data(
        test_user.id, db, groups=["viewer"], username="fp_test_user"
    )
    check("session has field_access", "field_access" in session)
    session_fa = session.get("field_access", {})
    check("session field_access has employee", "employee" in session_fa)
    check(
        "session salary hidden",
        session_fa.get("employee", {}).get("salary") == "hidden",
    )

    # Test Require.field_access() guard
    from hyperdjango.guard import Require
    from hyperdjango.guard.types import GuardContext

    mock_user = SessionUser(session)
    mock_request_obj = type("R", (), {"user": mock_user})()

    # ws27 item 5a: Require.field_access now FAILS CLOSED. A field with no
    # explicit permission row defaults to "hidden" (deny), not "writable".
    # "name" is not in the employee field map → access is denied.
    req = Require.field_access("name", "employee")
    result = await req.evaluate_fn(mock_request_obj, GuardContext())
    check("field_access: absent field denied (fail-closed)", result is not None)

    # writable access to "salary" — should fail (hidden)
    req2 = Require.field_access("salary", "employee")
    result2 = await req2.evaluate_fn(mock_request_obj, GuardContext())
    check("field_access: hidden salary denied", result2 is not None)

    # readonly access to "email" — should pass (readonly >= readonly)
    req3 = Require.field_access("email", "employee", level="readonly")
    result3 = await req3.evaluate_fn(mock_request_obj, GuardContext())
    check("field_access: readonly email passes for readonly", result3 is None)

    # writable access to "email" — should fail (readonly < writable)
    req4 = Require.field_access("email", "employee", level="writable")
    result4 = await req4.evaluate_fn(mock_request_obj, GuardContext())
    check("field_access: readonly email denied for writable", result4 is not None)

    # Cleanup
    await db.execute(
        "DELETE FROM hyper_field_permissions WHERE model_name = 'employee'"
    )
    await db.execute("DELETE FROM hyper_user_groups WHERE user_id = $1", test_user.id)
    await db.execute("DELETE FROM hyper_user_groups WHERE user_id = $1", su_user.id)
    await db.execute(
        "DELETE FROM hyper_users WHERE username IN ('fp_test_user', 'fp_test_su')"
    )
    await db.disconnect()


if __name__ == "__main__":
    print("=" * 60)
    print("Field Permission Tests")
    print("=" * 60)

    # Pure unit tests (no DB)
    test_session_user_groups_frozenset()
    test_session_user_permissions_frozenset()
    test_session_user_superuser_grants_all_perms()
    test_session_user_is_staff_derived()
    test_anonymous_user_interface()
    test_session_user_empty_groups()

    # DB-backed tests
    asyncio.run(test_field_permissions_db())

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
