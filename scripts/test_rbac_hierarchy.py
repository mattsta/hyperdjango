"""
Tests for hierarchical RBAC system.

Phase 1: Role hierarchy + inheritance
Phase 2: Object-level permissions
Phase 3: Conditional rules (is_owner, time_window, ip_range, field_match, custom)
Phase 4: Field-level permissions (hidden/readonly/writable)
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
from dataclasses import dataclass

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
results = []
test_funcs = []


def test(name):
    def decorator(func):
        test_funcs.append((name, func))
        return func

    return decorator


def check(label, condition):
    results.append((label, condition))
    symbol = "✓" if condition else "✗"
    print(f"  {symbol} {label}")


async def setup():
    """Create DB, tables, seed data. Returns (db, checker, user_ids, group_ids)."""
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Fresh slate
    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Create permissions
    await checker.create_default_permissions("post", "Post")
    await checker.create_default_permissions("employee", "Employee")

    return db, checker


async def teardown(db):
    from hyperdjango.auth.user import drop_rbac_tables

    await drop_rbac_tables(db)
    await db.disconnect()


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1: Role Hierarchy
# ═══════════════════════════════════════════════════════════════════════════


@test("hierarchy: child inherits parent permissions")
async def test_hierarchy_inherit():
    db, checker = await setup()
    try:
        # Create viewer → editor → admin chain
        viewer = await checker.create_group("viewer")
        editor = await checker.create_group("editor", parent_id=viewer.id)
        admin = await checker.create_group("admin", parent_id=editor.id, priority=10)

        # Grant perms at each level
        await checker.grant_group_perm(viewer.id, "view_post", "post")
        await checker.grant_group_perm(editor.id, "change_post", "post")
        await checker.grant_group_perm(admin.id, "delete_post", "post")

        # Create user in admin group
        alice = await checker.create_user("alice", "pass123", is_staff=True)

        class U:
            id = alice.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(alice.id, admin.id)

        # Admin should have all 3: own (delete) + editor (change) + viewer (view)
        check(
            "admin has delete_post (own)",
            await checker.has_perm(user, "delete_post", "post"),
        )
        check(
            "admin has change_post (inherited from editor)",
            await checker.has_perm(user, "change_post", "post"),
        )
        check(
            "admin has view_post (inherited from viewer)",
            await checker.has_perm(user, "view_post", "post"),
        )
        check(
            "admin does NOT have add_post",
            not await checker.has_perm(user, "add_post", "post"),
        )

        # Create user in editor group only
        bob = await checker.create_user("bob", "pass123", is_staff=True)

        class U2:
            id = bob.id
            is_active = True
            is_superuser = False

        user2 = U2()
        await checker.add_user_to_group(bob.id, editor.id)

        check(
            "editor has change_post (own)",
            await checker.has_perm(user2, "change_post", "post"),
        )
        check(
            "editor has view_post (inherited)",
            await checker.has_perm(user2, "view_post", "post"),
        )
        check(
            "editor does NOT have delete_post",
            not await checker.has_perm(user2, "delete_post", "post"),
        )
    finally:
        await teardown(db)


@test("hierarchy: cycle detection")
async def test_cycle_detection():
    db, checker = await setup()
    try:
        group_a = await checker.create_group("group_a")
        group_b = await checker.create_group("group_b", parent_id=group_a.id)
        group_c = await checker.create_group("group_c", parent_id=group_b.id)

        # Try to make A a child of C (creates cycle: A→B→C→A)
        try:
            await checker.set_group_parent(group_a.id, group_c.id)
            check("cycle detection: should have raised ValueError", False)
        except ValueError:
            check("cycle detection: ValueError raised", True)
    finally:
        await teardown(db)


@test("hierarchy: get_role_ancestors returns full chain")
async def test_ancestors():
    db, checker = await setup()
    try:
        root = await checker.create_group("root")
        mid = await checker.create_group("mid", parent_id=root.id)
        leaf = await checker.create_group("leaf", parent_id=mid.id)

        ancestors = await checker.get_role_ancestors(leaf.id)
        check("ancestors include self", leaf.id in ancestors)
        check("ancestors include mid", mid.id in ancestors)
        check("ancestors include root", root.id in ancestors)
        check("ancestors length is 3", len(ancestors) == 3)
    finally:
        await teardown(db)


@test("hierarchy: get_group_children returns direct children")
async def test_children():
    db, checker = await setup()
    try:
        parent = await checker.create_group("parent")
        child1 = await checker.create_group("child1", parent_id=parent.id)
        child2 = await checker.create_group("child2", parent_id=parent.id)
        await checker.create_group("grandchild", parent_id=child1.id)

        children = await checker.get_group_children(parent.id)
        child_ids = [c.id for c in children]
        check("parent has 2 direct children", len(children) == 2)
        check("child1 in children", child1.id in child_ids)
        check("child2 in children", child2.id in child_ids)
    finally:
        await teardown(db)


@test("hierarchy: deep 5-level chain works")
async def test_deep_hierarchy():
    db, checker = await setup()
    try:
        ids = []
        parent = None
        for i in range(5):
            group = await checker.create_group(f"level_{i}", parent_id=parent)
            ids.append(group.id)
            parent = group.id

        # Grant perm at root (level_0)
        await checker.grant_group_perm(ids[0], "view_post", "post")

        # User in deepest group (level_4) should inherit
        deep_user = await checker.create_user("deep_user", "pass123", is_staff=True)

        class U:
            id = deep_user.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(deep_user.id, ids[4])

        check(
            "5-level deep user inherits root perm",
            await checker.has_perm(user, "view_post", "post"),
        )
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: Object-Level Permissions
# ═══════════════════════════════════════════════════════════════════════════


@test("object perms: user can access specific object only")
async def test_object_perm_user():
    db, checker = await setup()
    try:
        objuser = await checker.create_user("objuser", "pass123", is_staff=True)

        class U:
            id = objuser.id
            is_active = True
            is_superuser = False

        user = U()

        # Grant change_post on object "42" only
        await checker.grant_object_perm("change_post", "post", "42", user_id=objuser.id)

        check(
            "has object perm on post 42",
            await checker.has_object_perm(user, "change_post", "post", "42"),
        )
        check(
            "does NOT have perm on post 99",
            not await checker.has_object_perm(user, "change_post", "post", "99"),
        )
    finally:
        await teardown(db)


@test("object perms: model-level perm grants access to all objects")
async def test_object_perm_model_level():
    db, checker = await setup()
    try:
        modeluser = await checker.create_user("modeluser", "pass123", is_staff=True)

        class U:
            id = modeluser.id
            is_active = True
            is_superuser = False

        user = U()

        # Grant model-level perm
        await checker.grant_user_perm(modeluser.id, "change_post", "post")

        # Should access ANY object
        check(
            "model perm grants access to object 1",
            await checker.has_object_perm(user, "change_post", "post", "1"),
        )
        check(
            "model perm grants access to object 999",
            await checker.has_object_perm(user, "change_post", "post", "999"),
        )
    finally:
        await teardown(db)


@test("object perms: group object perm with hierarchy")
async def test_object_perm_group_hierarchy():
    db, checker = await setup()
    try:
        viewer = await checker.create_group("viewer")
        editor = await checker.create_group("editor", parent_id=viewer.id)

        # Grant object perm to viewer group
        await checker.grant_object_perm("view_post", "post", "10", group_id=viewer.id)

        groupuser = await checker.create_user("groupuser", "pass123", is_staff=True)

        class U:
            id = groupuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(groupuser.id, editor.id)

        # Editor inherits viewer's object perm via hierarchy
        check(
            "editor inherits viewer's object perm",
            await checker.has_object_perm(user, "view_post", "post", "10"),
        )
    finally:
        await teardown(db)


@test("object perms: get_objects_with_perm returns correct list")
async def test_get_objects():
    db, checker = await setup()
    try:
        listuser = await checker.create_user("listuser", "pass123", is_staff=True)

        class U:
            id = listuser.id
            is_active = True
            is_superuser = False

        user = U()

        await checker.grant_object_perm("change_post", "post", "1", user_id=listuser.id)
        await checker.grant_object_perm("change_post", "post", "5", user_id=listuser.id)
        await checker.grant_object_perm(
            "change_post", "post", "10", user_id=listuser.id
        )

        objects = await checker.get_objects_with_perm(user, "change_post", "post")
        check("3 objects returned", len(objects) == 3)
        check("object 1 in list", "1" in objects)
        check("object 5 in list", "5" in objects)
        check("object 10 in list", "10" in objects)
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Conditional Rules
# ═══════════════════════════════════════════════════════════════════════════


@test("rules: is_owner allows own objects, denies others")
async def test_rule_is_owner():
    db, checker = await setup()
    try:
        editor = await checker.create_group("editor")
        await checker.grant_group_perm(editor.id, "change_post", "post")

        owner_user = await checker.create_user("owner_user", "pass123", is_staff=True)

        class U:
            id = owner_user.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(owner_user.id, editor.id)

        # Add is_owner rule
        await checker.add_rule(
            "change_post",
            "post",
            "is_owner",
            {"owner_field": "author_id"},
            group_id=editor.id,
        )

        # Own post
        own_post = {"id": 1, "author_id": owner_user.id, "title": "My Post"}
        check(
            "can edit own post",
            await checker.has_perm_with_rules(
                user, "change_post", "post", obj=own_post
            ),
        )

        # Someone else's post
        other_post = {"id": 2, "author_id": 999, "title": "Other Post"}
        check(
            "cannot edit other's post",
            not await checker.has_perm_with_rules(
                user, "change_post", "post", obj=other_post
            ),
        )
    finally:
        await teardown(db)


@test("rules: time_window allows within hours")
async def test_rule_time_window():
    db, checker = await setup()
    try:
        timeuser = await checker.create_user("timeuser", "pass123", is_staff=True)

        class U:
            id = timeuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(timeuser.id, "view_post", "post")

        # Add rule that allows ALL hours (00:00-23:59) — always passes
        await checker.add_rule(
            "view_post",
            "post",
            "time_window",
            {"start": "00:00", "end": "23:59"},
            user_id=timeuser.id,
        )

        check(
            "time_window 00:00-23:59 allows",
            await checker.has_perm_with_rules(user, "view_post", "post"),
        )
    finally:
        await teardown(db)


@test("rules: ip_range allows matching IPs")
async def test_rule_ip_range():
    db, checker = await setup()
    try:
        ipuser = await checker.create_user("ipuser", "pass123", is_staff=True)

        class U:
            id = ipuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(ipuser.id, "view_post", "post")

        await checker.add_rule(
            "view_post",
            "post",
            "ip_range",
            {"ranges": ["10.0.0.0/8"]},
            user_id=ipuser.id,
        )

        @dataclass
        class Req:
            client_ip: str

        check(
            "IP 10.1.2.3 in range",
            await checker.has_perm_with_rules(
                user, "view_post", "post", request=Req("10.1.2.3")
            ),
        )
        check(
            "IP 192.168.1.1 not in range",
            not await checker.has_perm_with_rules(
                user, "view_post", "post", request=Req("192.168.1.1")
            ),
        )
    finally:
        await teardown(db)


@test("rules: field_match checks object field values")
async def test_rule_field_match():
    db, checker = await setup()
    try:
        fielduser = await checker.create_user("fielduser", "pass123", is_staff=True)

        class U:
            id = fielduser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(fielduser.id, "change_post", "post")

        # Only allow editing draft posts
        await checker.add_rule(
            "change_post",
            "post",
            "field_match",
            {"field": "status", "values": ["draft"]},
            user_id=fielduser.id,
        )

        draft = {"id": 1, "status": "draft"}
        published = {"id": 2, "status": "published"}

        check(
            "can edit draft",
            await checker.has_perm_with_rules(user, "change_post", "post", obj=draft),
        )
        check(
            "cannot edit published",
            not await checker.has_perm_with_rules(
                user, "change_post", "post", obj=published
            ),
        )
    finally:
        await teardown(db)


@test("rules: custom async evaluator")
async def test_rule_custom():
    db, checker = await setup()
    try:
        from hyperdjango.auth.permissions import register_rule_type

        async def always_allow(user, obj, request, config):
            return True

        register_rule_type("always_allow", always_allow)

        customuser = await checker.create_user("customuser", "pass123", is_staff=True)

        class U:
            id = customuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(customuser.id, "view_post", "post")

        await checker.add_rule(
            "view_post", "post", "always_allow", {}, user_id=customuser.id
        )

        check(
            "custom always_allow rule passes",
            await checker.has_perm_with_rules(user, "view_post", "post"),
        )
    finally:
        await teardown(db)


@test("rules: deny overrides allow")
async def test_rule_deny_override():
    db, checker = await setup()
    try:
        denyuser = await checker.create_user("denyuser", "pass123", is_staff=True)

        class U:
            id = denyuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(denyuser.id, "delete_post", "post")

        # Allow rule
        await checker.add_rule(
            "delete_post",
            "post",
            "time_window",
            {"start": "00:00", "end": "23:59"},
            user_id=denyuser.id,
        )
        # Deny rule — field_match denies published posts
        await checker.add_rule(
            "delete_post",
            "post",
            "field_match",
            {"field": "status", "values": ["published"]},
            user_id=denyuser.id,
            is_deny=True,
        )

        published = {"id": 1, "status": "published"}
        draft = {"id": 2, "status": "draft"}

        check(
            "deny rule blocks published",
            not await checker.has_perm_with_rules(
                user, "delete_post", "post", obj=published
            ),
        )
        check(
            "allow rule passes for draft",
            await checker.has_perm_with_rules(user, "delete_post", "post", obj=draft),
        )
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4: Field-Level Permissions
# ═══════════════════════════════════════════════════════════════════════════


@test("field access: hidden field removed in read mode")
async def test_field_hidden():
    db, checker = await setup()
    try:
        viewer = await checker.create_group("viewer")
        fieldviewuser = await checker.create_user(
            "fieldviewuser", "pass123", is_staff=True
        )

        class U:
            id = fieldviewuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(fieldviewuser.id, viewer.id)

        # Hide salary for viewers
        await checker.set_field_access(
            "employee", "salary", access="hidden", group_id=viewer.id
        )

        data = {"name": "Alice", "salary": 100000, "department": "Eng"}
        filtered = await checker.filter_fields(user, "employee", data, mode="read")
        check("salary hidden in read mode", "salary" not in filtered)
        check("name visible", "name" in filtered)
        check("department visible", "department" in filtered)
    finally:
        await teardown(db)


@test("field access: readonly field in write mode")
async def test_field_readonly():
    db, checker = await setup()
    try:
        editor = await checker.create_group("editor")
        editorfielduser = await checker.create_user(
            "editorfielduser", "pass123", is_staff=True
        )

        class U:
            id = editorfielduser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(editorfielduser.id, editor.id)

        # Readonly salary for editors
        await checker.set_field_access(
            "employee", "salary", access="readonly", group_id=editor.id
        )

        data = {"name": "Bob", "salary": 90000}

        # Read mode: salary visible (readonly ≠ hidden)
        read_result = await checker.filter_fields(user, "employee", data, mode="read")
        check("readonly salary visible in read", "salary" in read_result)

        # Write mode: salary stripped (readonly can't write)
        write_result = await checker.filter_fields(user, "employee", data, mode="write")
        check("readonly salary stripped in write", "salary" not in write_result)
        check("name writable", "name" in write_result)
    finally:
        await teardown(db)


@test("field access: most permissive wins across groups")
async def test_field_most_permissive():
    db, checker = await setup()
    try:
        viewer = await checker.create_group("viewer")
        editor = await checker.create_group("editor")
        multigrpuser = await checker.create_user(
            "multigrpuser", "pass123", is_staff=True
        )

        class U:
            id = multigrpuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(multigrpuser.id, viewer.id)
        await checker.add_user_to_group(multigrpuser.id, editor.id)

        # Viewer: hidden; Editor: readonly → most permissive = readonly
        await checker.set_field_access(
            "employee", "salary", access="hidden", group_id=viewer.id
        )
        await checker.set_field_access(
            "employee", "salary", access="readonly", group_id=editor.id
        )

        access = await checker.get_field_access(user, "employee")
        check(
            "most permissive: readonly wins over hidden",
            access.get("salary") == "readonly",
        )
    finally:
        await teardown(db)


@test("field access: superuser gets no restrictions")
async def test_field_superuser():
    db, checker = await setup()
    try:
        superfield = await checker.create_user(
            "superfield", "pass123", is_superuser=True
        )

        class U:
            id = superfield.id
            is_active = True
            is_superuser = True

        user = U()
        access = await checker.get_field_access(user, "employee")
        check("superuser: empty access map (no restrictions)", access == {})
    finally:
        await teardown(db)


@test("field access: hierarchy inherits field restrictions")
async def test_field_hierarchy():
    db, checker = await setup()
    try:
        viewer = await checker.create_group("viewer")
        editor = await checker.create_group("editor", parent_id=viewer.id)
        hierfield = await checker.create_user("hierfield", "pass123", is_staff=True)

        class U:
            id = hierfield.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(hierfield.id, editor.id)

        # Set restriction on viewer (parent) — editor inherits it
        await checker.set_field_access(
            "employee", "ssn", access="hidden", group_id=viewer.id
        )
        # Editor overrides with readonly
        await checker.set_field_access(
            "employee", "ssn", access="readonly", group_id=editor.id
        )

        access = await checker.get_field_access(user, "employee")
        # Editor has both entries — most permissive (readonly) wins
        check(
            "hierarchy field: readonly wins over inherited hidden",
            access.get("ssn") == "readonly",
        )
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Hardening: Revoke paths, sync callables, edge cases
# ═══════════════════════════════════════════════════════════════════════════


@test("revoke: revoke_user_perm removes access")
async def test_revoke_user_perm():
    db, checker = await setup()
    try:
        revokeuser = await checker.create_user("revokeuser", "pass123", is_staff=True)

        class U:
            id = revokeuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(revokeuser.id, "add_post", "post")
        check(
            "has perm before revoke", await checker.has_perm(user, "add_post", "post")
        )
        checker.clear_cache(user)
        await checker.revoke_user_perm(revokeuser.id, "add_post", "post")
        checker.clear_cache(user)
        check(
            "perm gone after revoke",
            not await checker.has_perm(user, "add_post", "post"),
        )
    finally:
        await teardown(db)


@test("revoke: revoke_object_perm removes object access")
async def test_revoke_object_perm():
    db, checker = await setup()
    try:
        revokeobj = await checker.create_user("revokeobj", "pass123", is_staff=True)

        class U:
            id = revokeobj.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_object_perm(
            "change_post", "post", "42", user_id=revokeobj.id
        )
        check(
            "has object perm before revoke",
            await checker.has_object_perm(user, "change_post", "post", "42"),
        )
        await checker.revoke_object_perm(
            "change_post", "post", "42", user_id=revokeobj.id
        )
        check(
            "object perm gone after revoke",
            not await checker.has_object_perm(user, "change_post", "post", "42"),
        )
    finally:
        await teardown(db)


@test("revoke: remove_user_from_group revokes inherited perms")
async def test_remove_from_group():
    db, checker = await setup()
    try:
        editors = await checker.create_group("editors")
        await checker.grant_group_perm(editors.id, "change_post", "post")
        removeme = await checker.create_user("removeme", "pass123", is_staff=True)

        class U:
            id = removeme.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(removeme.id, editors.id)
        check("has group perm", await checker.has_perm(user, "change_post", "post"))
        checker.clear_cache(user)
        await checker.remove_user_from_group(removeme.id, editors.id)
        checker.clear_cache(user)
        check(
            "group perm gone after removal",
            not await checker.has_perm(user, "change_post", "post"),
        )
    finally:
        await teardown(db)


@test("sync callable: sync rule evaluator works without crash")
async def test_sync_rule():
    db, checker = await setup()
    try:
        from hyperdjango.auth.permissions import register_rule_type

        def sync_check(user, obj, request, config):
            return obj is not None and obj.get("approved") is True

        register_rule_type("sync_approved", sync_check)

        syncuser = await checker.create_user("syncuser", "pass123", is_staff=True)

        class U:
            id = syncuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(syncuser.id, "view_post", "post")
        await checker.add_rule(
            "view_post", "post", "sync_approved", {}, user_id=syncuser.id
        )

        approved = {"id": 1, "approved": True}
        denied = {"id": 2, "approved": False}
        check(
            "sync rule allows approved",
            await checker.has_perm_with_rules(user, "view_post", "post", obj=approved),
        )
        check(
            "sync rule denies unapproved",
            not await checker.has_perm_with_rules(
                user, "view_post", "post", obj=denied
            ),
        )
    finally:
        await teardown(db)


@test("global rule: rule with no user_id/group_id applies to all users")
async def test_global_rule():
    db, checker = await setup()
    try:
        glob1 = await checker.create_user("glob1", "pass123", is_staff=True)
        glob2 = await checker.create_user("glob2", "pass123", is_staff=True)

        class U:
            def __init__(self, uid):
                self.id = uid
                self.is_active = True
                self.is_superuser = False

        user1 = U(glob1.id)
        user2 = U(glob2.id)
        await checker.grant_user_perm(glob1.id, "view_post", "post")
        await checker.grant_user_perm(glob2.id, "view_post", "post")

        # Global rule — applies to everyone
        await checker.add_rule(
            "view_post",
            "post",
            "field_match",
            {"field": "status", "values": ["public"]},
        )

        public_post = {"id": 1, "status": "public"}
        private_post = {"id": 2, "status": "private"}
        check(
            "user1 can view public",
            await checker.has_perm_with_rules(
                user1, "view_post", "post", obj=public_post
            ),
        )
        check(
            "user2 can view public",
            await checker.has_perm_with_rules(
                user2, "view_post", "post", obj=public_post
            ),
        )
        check(
            "user1 cannot view private",
            not await checker.has_perm_with_rules(
                user1, "view_post", "post", obj=private_post
            ),
        )
        check(
            "user2 cannot view private",
            not await checker.has_perm_with_rules(
                user2, "view_post", "post", obj=private_post
            ),
        )
    finally:
        await teardown(db)


@test("inactive user: all permission checks return False")
async def test_inactive_user():
    db, checker = await setup()
    try:
        inactive = await checker.create_user("inactive", "pass123", is_staff=True)
        await checker.grant_user_perm(inactive.id, "view_post", "post")

        class U:
            id = inactive.id
            is_active = False
            is_superuser = False

        user = U()
        check(
            "inactive: has_perm returns False",
            not await checker.has_perm(user, "view_post", "post"),
        )
        check(
            "inactive: has_object_perm returns False",
            not await checker.has_object_perm(user, "view_post", "post", "1"),
        )
        check(
            "inactive: has_perm_with_rules returns False",
            not await checker.has_perm_with_rules(user, "view_post", "post"),
        )
    finally:
        await teardown(db)


@test("anonymous user: all permission checks return False")
async def test_anonymous_user():
    db, checker = await setup()
    try:
        from hyperdjango.auth.user import AnonymousUser

        anon = AnonymousUser()
        check(
            "anon: has_perm returns False",
            not await checker.has_perm(anon, "view_post", "post"),
        )
        check(
            "anon: has_object_perm returns False",
            not await checker.has_object_perm(anon, "view_post", "post", "1"),
        )
    finally:
        await teardown(db)


@test("has_perms: checks ALL permissions (AND logic)")
async def test_has_perms():
    db, checker = await setup()
    try:
        multiperms = await checker.create_user("multiperms", "pass123", is_staff=True)

        class U:
            id = multiperms.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(multiperms.id, "view_post", "post")
        await checker.grant_user_perm(multiperms.id, "add_post", "post")

        check(
            "has_perms: both granted → True",
            await checker.has_perms(user, ["view_post", "add_post"], "post"),
        )
        check(
            "has_perms: one missing → False",
            not await checker.has_perms(user, ["view_post", "delete_post"], "post"),
        )
    finally:
        await teardown(db)


@test("has_model_perms: returns correct dict")
async def test_has_model_perms():
    db, checker = await setup()
    try:
        modelperms = await checker.create_user("modelperms", "pass123", is_staff=True)

        class U:
            id = modelperms.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(modelperms.id, "add_post", "post")
        await checker.grant_user_perm(modelperms.id, "view_post", "post")

        perms = await checker.has_model_perms(user, "post")
        check("model_perms: add=True", perms["add"] is True)
        check("model_perms: view=True", perms["view"] is True)
        check("model_perms: change=False", perms["change"] is False)
        check("model_perms: delete=False", perms["delete"] is False)
    finally:
        await teardown(db)


@test("multiple hierarchies: user in two unrelated chains")
async def test_multi_hierarchy():
    db, checker = await setup()
    try:
        # Chain 1: viewer → editor
        viewer = await checker.create_group("viewer")
        editor = await checker.create_group("editor", parent_id=viewer.id)
        await checker.grant_group_perm(viewer.id, "view_post", "post")
        await checker.grant_group_perm(editor.id, "change_post", "post")

        # Chain 2: hr_viewer → hr_manager (completely separate)
        hr_viewer = await checker.create_group("hr_viewer")
        hr_manager = await checker.create_group("hr_manager", parent_id=hr_viewer.id)
        await checker.grant_group_perm(hr_viewer.id, "view_employee", "employee")
        await checker.grant_group_perm(hr_manager.id, "change_employee", "employee")

        dualchain = await checker.create_user("dualchain", "pass123", is_staff=True)

        class U:
            id = dualchain.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(dualchain.id, editor.id)
        await checker.add_user_to_group(dualchain.id, hr_manager.id)

        check(
            "has view_post (from chain 1)",
            await checker.has_perm(user, "view_post", "post"),
        )
        check(
            "has change_post (from chain 1)",
            await checker.has_perm(user, "change_post", "post"),
        )
        check(
            "has view_employee (from chain 2)",
            await checker.has_perm(user, "view_employee", "employee"),
        )
        check(
            "has change_employee (from chain 2)",
            await checker.has_perm(user, "change_employee", "employee"),
        )
        check(
            "does NOT have delete_post",
            not await checker.has_perm(user, "delete_post", "post"),
        )
    finally:
        await teardown(db)


@test("self-cycle: set_group_parent(A, A) fails")
async def test_self_cycle():
    db, checker = await setup()
    try:
        selfref = await checker.create_group("selfref")
        try:
            await checker.set_group_parent(selfref.id, selfref.id)
            check("self-cycle: should have raised", False)
        except ValueError:
            check("self-cycle: ValueError raised", True)
    finally:
        await teardown(db)


@test("empty group: user in group with no perms has no perms")
async def test_empty_group():
    db, checker = await setup()
    try:
        empty_group = await checker.create_group("empty_group")
        emptygrp = await checker.create_user("emptygrp", "pass123", is_staff=True)

        class U:
            id = emptygrp.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.add_user_to_group(emptygrp.id, empty_group.id)
        check(
            "empty group: no perms",
            not await checker.has_perm(user, "view_post", "post"),
        )
    finally:
        await teardown(db)


@test("rules: no obj/request → evaluators handle gracefully")
async def test_rules_no_context():
    db, checker = await setup()
    try:
        nocontext = await checker.create_user("nocontext", "pass123", is_staff=True)

        class U:
            id = nocontext.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(nocontext.id, "change_post", "post")

        # is_owner rule with obj=None → should return False (not crash)
        await checker.add_rule(
            "change_post",
            "post",
            "is_owner",
            {"owner_field": "user_id"},
            user_id=nocontext.id,
        )
        check(
            "is_owner with obj=None → False",
            not await checker.has_perm_with_rules(
                user, "change_post", "post", obj=None
            ),
        )

        # ip_range rule with request=None → should return False
        await checker.add_rule(
            "change_post",
            "post",
            "ip_range",
            {"ranges": ["10.0.0.0/8"]},
            user_id=nocontext.id,
            is_deny=True,
        )
        # With no request, ip_range deny rule returns False (doesn't match), so doesn't block
        # But is_owner allow rule also returns False (no obj), so no allow matches → denied
        check(
            "ip_range with request=None doesn't crash",
            not await checker.has_perm_with_rules(user, "change_post", "post"),
        )
    finally:
        await teardown(db)


@test("cache: clear_cache resets and next check re-queries")
async def test_cache_behavior():
    db, checker = await setup()
    try:
        cacheuser = await checker.create_user("cacheuser", "pass123", is_staff=True)

        class U:
            id = cacheuser.id
            is_active = True
            is_superuser = False

        user = U()
        await checker.grant_user_perm(cacheuser.id, "view_post", "post")

        # First check populates cache
        check(
            "has perm (populates cache)",
            await checker.has_perm(user, "view_post", "post"),
        )
        # Cache key is tenant-aware: _perm_cache_{tenant_id} (None when no tenant context)
        check("cache exists", hasattr(user, "_perm_cache_None"))

        # Clear and re-check
        checker.clear_cache(user)
        check("cache cleared", not hasattr(user, "_perm_cache_None"))

        # Re-check repopulates
        check(
            "has perm (re-queried)", await checker.has_perm(user, "view_post", "post")
        )
        check("cache repopulated", hasattr(user, "_perm_cache_None"))
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\nHierarchical RBAC Tests ({len(test_funcs)} tests)")
    print("=" * 60)
    for name, func in test_funcs:
        try:
            if inspect.iscoroutinefunction(func):
                await func()
            else:
                func()
        except Exception as e:
            results.append((name, False))
            print(f"  ✗ {name}: {e}")
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")

    if failed:
        print("\nFailures:")
        for label, ok in results:
            if not ok:
                print(f"  - {label}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
