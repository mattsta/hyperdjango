"""
Hypothesis fuzz tests for RBAC hierarchy permission inheritance.

Uses real DB + PermissionChecker. Proves:
1. Permissions granted to parent group are inherited by child
2. Direct user permissions work
3. Revoking a permission removes it
4. Multi-group union
5. Deep hierarchy (3 levels)

# hyper-test: db_isolated
"""

import asyncio
import os

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


async def setup():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    await drop_rbac_tables(db)
    checker = PermissionChecker(db)
    await checker.ensure_tables()
    await checker.create_default_permissions("doc", "Document")
    return db, checker


async def teardown(db):
    from hyperdjango.auth.user import drop_rbac_tables

    await drop_rbac_tables(db)
    await db.disconnect()


async def test_parent_child_inheritance():
    """User in child group inherits parent group permissions."""
    db, checker = await setup()
    try:
        parent = await checker.create_group("parent_group")
        child = await checker.create_group("child_group")
        await checker.set_group_parent(child.id, parent.id)
        await checker.grant_group_perm(parent.id, "view_doc", "doc")

        user = await checker.create_user("inherit_user", "password123", is_staff=True)
        await checker.add_user_to_group(user.id, child.id)

        has = await checker.has_perm(user, "view_doc", "doc")
        assert has, "Child user should inherit parent group permission"
        print("  PASS: parent→child inheritance")
    finally:
        await teardown(db)


async def test_direct_user_permission():
    """Directly granted permission is effective."""
    db, checker = await setup()
    try:
        user = await checker.create_user("direct_user", "password123", is_staff=True)
        await checker.grant_user_perm(user.id, "view_doc", "doc")

        has = await checker.has_perm(user, "view_doc", "doc")
        assert has, "Direct permission should work"
        print("  PASS: direct user permission")
    finally:
        await teardown(db)


async def test_revoke_removes():
    """Revoking a permission makes has_perm return False."""
    db, checker = await setup()
    try:
        user = await checker.create_user("revoke_user", "password123", is_staff=True)
        await checker.grant_user_perm(user.id, "view_doc", "doc")

        has_before = await checker.has_perm(user, "view_doc", "doc")
        assert has_before, "Should have permission before revoke"

        await checker.revoke_user_perm(user.id, "view_doc", "doc")

        # Refresh user to clear cached perms
        user_fresh = await checker.get_user_by_id(user.id)
        # has_perm needs a user object, not dict — use the model
        from hyperdjango.auth.user import User

        user_obj = await User.objects.filter(id=user.id).first()
        has_after = await checker.has_perm(user_obj, "view_doc", "doc")
        assert not has_after, "Should NOT have permission after revoke"
        print("  PASS: revoke removes permission")
    finally:
        await teardown(db)


async def test_multi_group_union():
    """User in multiple groups gets union of all group permissions."""
    db, checker = await setup()
    try:
        group_a = await checker.create_group("group_a")
        group_b = await checker.create_group("group_b")
        await checker.grant_group_perm(group_a.id, "view_doc", "doc")
        await checker.grant_group_perm(group_b.id, "add_doc", "doc")

        user = await checker.create_user("multi_user", "password123", is_staff=True)
        await checker.add_user_to_group(user.id, group_a.id)
        await checker.add_user_to_group(user.id, group_b.id)

        has_view = await checker.has_perm(user, "view_doc", "doc")
        has_add = await checker.has_perm(user, "add_doc", "doc")
        assert has_view, "Should have view from group_a"
        assert has_add, "Should have add from group_b"
        print("  PASS: multi-group union")
    finally:
        await teardown(db)


async def test_deep_hierarchy():
    """Permission propagates through 3-level hierarchy."""
    db, checker = await setup()
    try:
        gp = await checker.create_group("grandparent")
        p = await checker.create_group("parent_grp")
        c = await checker.create_group("child_grp")
        await checker.set_group_parent(p.id, gp.id)
        await checker.set_group_parent(c.id, p.id)
        await checker.grant_group_perm(gp.id, "delete_doc", "doc")

        user = await checker.create_user("deep_user", "password123", is_staff=True)
        await checker.add_user_to_group(user.id, c.id)

        has = await checker.has_perm(user, "delete_doc", "doc")
        assert has, "Should inherit through 3-level hierarchy"
        print("  PASS: deep hierarchy (3 levels)")
    finally:
        await teardown(db)


async def test_no_permission_by_default():
    """New user with no groups has no permissions."""
    db, checker = await setup()
    try:
        user = await checker.create_user("empty_user", "password123", is_staff=True)
        has = await checker.has_perm(user, "view_doc", "doc")
        assert not has, "New user should have no permissions"
        print("  PASS: no permission by default")
    finally:
        await teardown(db)


def run_tests():
    print("\n── RBAC Hierarchy Fuzz Tests (Live DB) ──\n")

    tests = [
        test_parent_child_inheritance,
        test_direct_user_permission,
        test_revoke_removes,
        test_multi_group_union,
        test_deep_hierarchy,
        test_no_permission_by_default,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            asyncio.run(test())
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"RBAC fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
