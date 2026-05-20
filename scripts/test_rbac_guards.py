"""
Tests for RBAC guard requirements and build_session_data().

Validates:
1. Require.role() — alias for Require.group()
2. Require.permission() — codename check from session
3. Require.permission() — superuser group bypass
4. build_session_data() — constructs session dict with RBAC groups
5. Integration: guards evaluate correctly with RBAC session data

Usage:
    uv run hyper-test rbac_guards
"""

# hyper-test: unit

import asyncio
import sys
from dataclasses import dataclass

from hyperdjango.auth.user import SessionUser
from hyperdjango.guard import Require
from hyperdjango.guard.requirements import (
    DenyReason,
    GuardContext,
    GuardDenial,
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        if detail:
            print(f"    {detail}")


# Use the REAL SessionUser (not a hand-rolled mock) so these guard tests
# exercise the actual contract: id is None when absent (not a fabricated 1),
# is_superuser/is_staff derive strictly from RBAC groups (an "is_superuser"
# session key is ignored), and username is "" when absent. A drifting mock is
# exactly what let the pk_field / _admin_user regressions slip through.
@dataclass
class MockRequest:
    user: SessionUser | None = None


def test_require_role():
    """Test Require.role() is an alias for Require.group()."""
    print("\n--- Require.role() ---")

    req = Require.role("admin")
    check("role creates guard", req is not None)
    check("name includes group:", "group:admin" in req.name)


def test_require_role_passes():
    """Test Require.role() passes when user has the group."""
    print("\n--- Require.role() passes ---")

    user = SessionUser({"groups": ["admin", "editor"]})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(Require.role("admin").evaluate_fn(request, ctx))
    check("admin role passes", result is None)

    result = asyncio.run(Require.role("editor").evaluate_fn(request, ctx))
    check("editor role passes", result is None)


def test_require_role_denies():
    """Test Require.role() denies when user lacks the group."""
    print("\n--- Require.role() denies ---")

    user = SessionUser({"groups": ["viewer"]})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(Require.role("admin").evaluate_fn(request, ctx))
    check("admin denied", isinstance(result, GuardDenial))
    check("denied reason", result.reason == DenyReason.FORBIDDEN)


def test_require_permission_passes():
    """Test Require.permission() passes with codename in session."""
    print("\n--- Require.permission() passes ---")

    user = SessionUser({"permissions": ["change_post", "view_dashboard"]})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(Require.permission("change_post").evaluate_fn(request, ctx))
    check("permission passes", result is None)


def test_require_permission_denies():
    """Test Require.permission() denies when codename not in session."""
    print("\n--- Require.permission() denies ---")

    user = SessionUser({"permissions": ["view_dashboard"]})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(Require.permission("delete_user").evaluate_fn(request, ctx))
    check("permission denied", isinstance(result, GuardDenial))
    check("denied reason", result.reason == DenyReason.FORBIDDEN)
    check("message has codename", "delete_user" in result.message)


def test_require_permission_superuser_bypass():
    """Test Require.permission() passes for superuser group (all perms)."""
    print("\n--- Require.permission() superuser bypass ---")

    user = SessionUser({"groups": ["superuser"], "permissions": []})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(Require.permission("anything").evaluate_fn(request, ctx))
    check("superuser bypasses", result is None)


def test_require_permission_no_perms():
    """Test Require.permission() denies when no permissions key in session."""
    print("\n--- Require.permission() no perms key ---")

    user = SessionUser({"groups": ["editor"]})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(Require.permission("change_post").evaluate_fn(request, ctx))
    check("denied without perms key", isinstance(result, GuardDenial))


def test_require_permission_unauthenticated():
    """Test Require.permission() denies unauthenticated users."""
    print("\n--- Require.permission() unauthenticated ---")

    request = MockRequest(user=None)
    ctx = GuardContext()

    result = asyncio.run(Require.permission("anything").evaluate_fn(request, ctx))
    check("unauthenticated denied", isinstance(result, GuardDenial))
    check("reason NOT_AUTHENTICATED", result.reason == DenyReason.NOT_AUTHENTICATED)


def test_require_role_unauthenticated():
    """Test Require.role() denies unauthenticated users."""
    print("\n--- Require.role() unauthenticated ---")

    request = MockRequest(user=None)
    ctx = GuardContext()

    result = asyncio.run(Require.role("admin").evaluate_fn(request, ctx))
    check("unauthenticated denied", isinstance(result, GuardDenial))
    check("reason NOT_AUTHENTICATED", result.reason == DenyReason.NOT_AUTHENTICATED)


def test_require_permission_custom_message():
    """Test Require.permission() with custom deny message."""
    print("\n--- Require.permission() custom message ---")

    user = SessionUser({"permissions": []})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(
        Require.permission("admin_panel", deny_message="Admin access only").evaluate_fn(
            request, ctx
        )
    )
    check("custom message", result.message == "Admin access only")


def test_build_session_data_groups():
    """Test build_session_data() populates groups and derived flags."""
    print("\n--- build_session_data() ---")

    # Can't test DB lookup without a real DB, but test the pre-computed groups path
    from hyperdjango.auth.sessions import build_session_data

    async def _test():
        session = await build_session_data(
            42, None, groups=["staff", "editor"], username="alice"
        )
        check("id set", session["id"] == 42)
        check("username set", session["username"] == "alice")
        check("groups set", session["groups"] == ["staff", "editor"])
        check("is_staff derived from groups", session["is_staff"] is True)
        check("is_superuser false", session["is_superuser"] is False)

        session2 = await build_session_data(
            1, None, groups=["superuser", "staff"], username="root"
        )
        check("superuser is_superuser true", session2["is_superuser"] is True)
        check("superuser is_staff true", session2["is_staff"] is True)

        session3 = await build_session_data(2, None, groups=["viewer"], username="bob")
        check("viewer is_staff false", session3["is_staff"] is False)
        check("viewer is_superuser false", session3["is_superuser"] is False)

    asyncio.run(_test())


def test_role_hierarchy_pattern():
    """Test hierarchical role pattern (admin gets admin+team_lead+agent)."""
    print("\n--- Role hierarchy pattern ---")

    # Simulate HyperTicket pattern: admin→[admin,team_lead,agent]
    admin_groups = ["admin", "team_lead", "agent"]
    user = SessionUser({"groups": admin_groups})
    request = MockRequest(user=user)
    ctx = GuardContext()

    result = asyncio.run(Require.role("admin").evaluate_fn(request, ctx))
    check("admin passes admin", result is None)

    result = asyncio.run(Require.role("team_lead").evaluate_fn(request, ctx))
    check("admin passes team_lead", result is None)

    result = asyncio.run(Require.role("agent").evaluate_fn(request, ctx))
    check("admin passes agent", result is None)

    # team_lead should NOT have admin
    tl_groups = ["team_lead", "agent"]
    user2 = SessionUser({"groups": tl_groups})
    request2 = MockRequest(user=user2)

    result = asyncio.run(Require.role("admin").evaluate_fn(request2, GuardContext()))
    check("team_lead denied admin", isinstance(result, GuardDenial))

    result = asyncio.run(Require.role("agent").evaluate_fn(request2, GuardContext()))
    check("team_lead passes agent", result is None)


def test_require_superuser_via_groups():
    """Test Require.superuser() passes when 'superuser' is in RBAC groups."""
    print("\n--- Require.superuser() via groups ---")

    user = SessionUser({"groups": ["superuser", "staff"]})
    request = MockRequest(user=user)
    result = asyncio.run(Require.superuser().evaluate_fn(request, GuardContext()))
    check("superuser group passes", result is None)


def test_require_superuser_boolean_only_denied():
    """Test Require.superuser() DENIES when only is_superuser=True, no group."""
    print("\n--- Require.superuser() boolean-only denied ---")

    user = SessionUser({"is_superuser": True, "groups": []})
    request = MockRequest(user=user)
    result = asyncio.run(Require.superuser().evaluate_fn(request, GuardContext()))
    check("boolean-only denied (groups authoritative)", isinstance(result, GuardDenial))


def test_require_superuser_denies():
    """Test Require.superuser() denies regular users."""
    print("\n--- Require.superuser() denies ---")

    user = SessionUser({"groups": ["staff"]})
    request = MockRequest(user=user)
    result = asyncio.run(Require.superuser().evaluate_fn(request, GuardContext()))
    check("staff-only denied", isinstance(result, GuardDenial))
    check("reason is FORBIDDEN", result.reason == DenyReason.FORBIDDEN)


def test_require_superuser_unauthenticated():
    """Test Require.superuser() denies unauthenticated users."""
    print("\n--- Require.superuser() unauthenticated ---")

    request = MockRequest(user=None)
    result = asyncio.run(Require.superuser().evaluate_fn(request, GuardContext()))
    check("no user denied", isinstance(result, GuardDenial))
    check("reason is NOT_AUTHENTICATED", result.reason == DenyReason.NOT_AUTHENTICATED)


if __name__ == "__main__":
    print("=" * 60)
    print("RBAC Guard Tests")
    print("=" * 60)

    test_require_role()
    test_require_role_passes()
    test_require_role_denies()
    test_require_permission_passes()
    test_require_permission_denies()
    test_require_permission_superuser_bypass()
    test_require_permission_no_perms()
    test_require_permission_unauthenticated()
    test_require_role_unauthenticated()
    test_require_permission_custom_message()
    test_build_session_data_groups()
    test_role_hierarchy_pattern()
    test_require_superuser_via_groups()
    test_require_superuser_boolean_only_denied()
    test_require_superuser_denies()
    test_require_superuser_unauthenticated()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
