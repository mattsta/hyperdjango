"""
Tests for RBAC policy import/export and dashboard stats.

- export_policy() roundtrip
- import_policy() with merge and clear modes
- import_policy() with invalid data
- Dashboard stats queries
- Admin route registration
"""

# hyper-test: db_isolated

import asyncio
import json
import os
import sys

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
    symbol = "\u2713" if condition else "\u2717"
    print(f"  {symbol} {label}")


async def setup():
    """Create DB, tables, seed data. Returns (db, checker)."""
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

    return db, checker


async def teardown(db):
    from hyperdjango.auth.user import drop_rbac_tables

    await drop_rbac_tables(db)
    await db.disconnect()


async def seed_policy(checker):
    """Create a full RBAC policy for testing export/import."""
    # Groups: viewer -> editor -> admin
    viewer = await checker.create_group("viewer")
    editor = await checker.create_group("editor", parent_id=viewer.id)
    admin = await checker.create_group("admin", parent_id=editor.id, priority=10)

    # Permissions
    await checker.create_default_permissions("post", "Post")
    await checker.create_default_permissions("employee", "Employee")

    # Users
    alice = await checker.create_user("alice", "pass123", is_staff=True)
    bob = await checker.create_user("bob", "pass456", is_staff=True, is_superuser=True)

    # Assign groups
    await checker.add_user_to_group(alice.id, editor.id)
    await checker.add_user_to_group(bob.id, admin.id)

    # Group permissions
    await checker.grant_group_perm(viewer.id, "view_post", "post")
    await checker.grant_group_perm(editor.id, "change_post", "post")
    await checker.grant_group_perm(admin.id, "delete_post", "post")

    # Direct user permission
    await checker.grant_user_perm(alice.id, "add_employee", "employee")

    # Object permission
    await checker.grant_object_perm("change_post", "post", "42", user_id=alice.id)

    # Rule
    await checker.add_rule(
        "change_post",
        "post",
        "is_owner",
        {"owner_field": "user_id"},
        group_id=editor.id,
    )

    # Field permission
    await checker.set_field_access(
        "employee", "salary", group_id=viewer.id, access="hidden"
    )
    await checker.set_field_access(
        "employee", "salary", group_id=editor.id, access="readonly"
    )

    return {
        "viewer_id": viewer.id,
        "editor_id": editor.id,
        "admin_id": admin.id,
        "user_id": alice.id,
        "admin_user_id": bob.id,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Export Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("export: returns valid structure")
async def test_export_structure():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)
        policy = await checker.export_policy()

        check("has version", policy["version"] == 1)
        check("has exported_at", "exported_at" in policy)
        check("has groups", isinstance(policy["groups"], list))
        check("has permissions", isinstance(policy["permissions"], list))
        check("has user_groups", isinstance(policy["user_groups"], list))
        check("has group_permissions", isinstance(policy["group_permissions"], list))
        check("has user_permissions", isinstance(policy["user_permissions"], list))
        check("has object_permissions", isinstance(policy["object_permissions"], list))
        check("has permission_rules", isinstance(policy["permission_rules"], list))
        check("has field_permissions", isinstance(policy["field_permissions"], list))
    finally:
        await teardown(db)


@test("export: captures all data")
async def test_export_data_completeness():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)
        policy = await checker.export_policy()

        check("3 groups", len(policy["groups"]) == 3)
        check("8 permissions (4 post + 4 employee)", len(policy["permissions"]) == 8)
        check("2 user_groups", len(policy["user_groups"]) == 2)
        check("3 group_permissions", len(policy["group_permissions"]) == 3)
        check("1 user_permission", len(policy["user_permissions"]) == 1)
        check("1 object_permission", len(policy["object_permissions"]) == 1)
        check("1 rule", len(policy["permission_rules"]) == 1)
        check("2 field_permissions", len(policy["field_permissions"]) == 2)

        # Verify group hierarchy in export
        groups_by_name = {g["name"]: g for g in policy["groups"]}
        check("viewer is root", groups_by_name["viewer"]["parent_id"] is None)
        check(
            "editor parent is viewer",
            groups_by_name["editor"]["parent_id"] == groups_by_name["viewer"]["id"],
        )
        check(
            "admin parent is editor",
            groups_by_name["admin"]["parent_id"] == groups_by_name["editor"]["id"],
        )
        check("admin priority=10", groups_by_name["admin"]["priority"] == 10)

        # Verify rule config is dict not string
        rule = policy["permission_rules"][0]
        check("rule_config is dict", isinstance(rule["rule_config"], dict))
        check(
            "rule has owner_field", rule["rule_config"].get("owner_field") == "user_id"
        )
    finally:
        await teardown(db)


@test("export: JSON serializable")
async def test_export_json_serializable():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)
        policy = await checker.export_policy()

        # Must be fully JSON-serializable
        json_str = json.dumps(policy, default=str)
        check("serializes to JSON", len(json_str) > 100)

        # Round-trip
        parsed = json.loads(json_str)
        check("round-trip version", parsed["version"] == 1)
        check("round-trip groups count", len(parsed["groups"]) == 3)
    finally:
        await teardown(db)


@test("export: empty policy")
async def test_export_empty():
    db, checker = await setup()
    try:
        policy = await checker.export_policy()

        check("empty groups", len(policy["groups"]) == 0)
        check("empty permissions", len(policy["permissions"]) == 0)
        check("version still 1", policy["version"] == 1)
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Import Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("import: roundtrip export->import")
async def test_import_roundtrip():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)
        policy = await checker.export_policy()

        # Clear RBAC tables but keep users (since user_groups/user_perms reference users)
        for table in [
            "hyper_field_permissions",
            "hyper_permission_rules",
            "hyper_object_permissions",
            "hyper_user_permissions",
            "hyper_group_permissions",
            "hyper_user_groups",
            "hyper_permissions",
            "hyper_groups",
        ]:
            await db.execute(f"DELETE FROM {table}")

        result = await checker.import_policy(policy, clear_existing=False)
        check("no errors", len(result["errors"]) == 0)
        check("imported 3 groups", result["imported"]["groups"] == 3)
        check("imported 8 permissions", result["imported"]["permissions"] == 8)
        check("imported 2 user_groups", result["imported"]["user_groups"] == 2)
        check(
            "imported 3 group_permissions", result["imported"]["group_permissions"] == 3
        )
        check("imported 1 user_permission", result["imported"]["user_permissions"] == 1)
        check(
            "imported 1 object_permission",
            result["imported"]["object_permissions"] == 1,
        )
        check("imported 1 rule", result["imported"]["permission_rules"] == 1)
        check(
            "imported 2 field_permissions", result["imported"]["field_permissions"] == 2
        )

        # Verify data is actually there
        re_export = await checker.export_policy()
        check("re-export matches groups", len(re_export["groups"]) == 3)
        check("re-export matches perms", len(re_export["permissions"]) == 8)
        check("re-export matches rules", len(re_export["permission_rules"]) == 1)
    finally:
        await teardown(db)


@test("import: merge mode (ON CONFLICT DO NOTHING)")
async def test_import_merge():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)
        policy = await checker.export_policy()

        # Import again without clearing — should merge (duplicates ignored)
        result = await checker.import_policy(policy, clear_existing=False)
        check("no errors on merge", len(result["errors"]) == 0)

        # Counts should remain the same
        re_export = await checker.export_policy()
        check("still 3 groups after merge", len(re_export["groups"]) == 3)
        check("still 8 perms after merge", len(re_export["permissions"]) == 8)
    finally:
        await teardown(db)


@test("import: clear_existing mode")
async def test_import_clear():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)

        # Create a smaller policy
        small_policy = {
            "version": 1,
            "groups": [
                {"id": 100, "name": "test_group", "parent_id": None, "priority": 0}
            ],
            "permissions": [
                {
                    "id": 100,
                    "codename": "test_perm",
                    "name": "Test",
                    "model_name": "test",
                }
            ],
            "user_groups": [],
            "group_permissions": [],
            "user_permissions": [],
            "object_permissions": [],
            "permission_rules": [],
            "field_permissions": [],
        }

        result = await checker.import_policy(small_policy, clear_existing=True)
        check("no errors on clear+import", len(result["errors"]) == 0)

        re_export = await checker.export_policy()
        check("only 1 group after clear", len(re_export["groups"]) == 1)
        check("only 1 perm after clear", len(re_export["permissions"]) == 1)
        check(
            "group name is test_group", re_export["groups"][0]["name"] == "test_group"
        )
    finally:
        await teardown(db)


@test("import: invalid version rejected")
async def test_import_invalid_version():
    db, checker = await setup()
    try:
        result = await checker.import_policy({"version": 99})
        check("has error", len(result["errors"]) > 0)
        check("error mentions version", "version" in result["errors"][0].lower())
    finally:
        await teardown(db)


@test("import: partial failure doesn't stop other sections")
async def test_import_partial_failure():
    db, checker = await setup()
    try:
        # Policy with one good group and one that references non-existent user
        policy = {
            "version": 1,
            "groups": [
                {"id": 200, "name": "good_group", "parent_id": None, "priority": 0}
            ],
            "permissions": [
                {
                    "id": 200,
                    "codename": "good_perm",
                    "name": "Good",
                    "model_name": "good",
                }
            ],
            "user_groups": [{"user_id": 99999, "group_id": 200}],  # user doesn't exist
            "group_permissions": [{"group_id": 200, "permission_id": 200}],
            "user_permissions": [],
            "object_permissions": [],
            "permission_rules": [],
            "field_permissions": [],
        }

        result = await checker.import_policy(policy)
        check("group imported", result["imported"]["groups"] == 1)
        check("permission imported", result["imported"]["permissions"] == 1)
        check("group_permission imported", result["imported"]["group_permissions"] == 1)
        # user_groups should have an error due to FK violation
        check("has errors", len(result["errors"]) > 0)
    finally:
        await teardown(db)


@test("import: empty policy is valid")
async def test_import_empty():
    db, checker = await setup()
    try:
        result = await checker.import_policy(
            {
                "version": 1,
                "groups": [],
                "permissions": [],
                "user_groups": [],
                "group_permissions": [],
                "user_permissions": [],
                "object_permissions": [],
                "permission_rules": [],
                "field_permissions": [],
            }
        )
        check("no errors", len(result["errors"]) == 0)
        check("all counts zero", all(v == 0 for v in result["imported"].values()))
    finally:
        await teardown(db)


@test("import: rule_config dict preserved")
async def test_import_rule_config():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)
        policy = await checker.export_policy()

        # Clear RBAC tables (keep users for FK integrity)
        for table in [
            "hyper_field_permissions",
            "hyper_permission_rules",
            "hyper_object_permissions",
            "hyper_user_permissions",
            "hyper_group_permissions",
            "hyper_user_groups",
            "hyper_permissions",
            "hyper_groups",
        ]:
            await db.execute(f"DELETE FROM {table}")

        await checker.import_policy(policy)

        re_export = await checker.export_policy()
        rule = re_export["permission_rules"][0]
        check(
            "rule_config is dict after roundtrip", isinstance(rule["rule_config"], dict)
        )
        check(
            "owner_field preserved", rule["rule_config"].get("owner_field") == "user_id"
        )
    finally:
        await teardown(db)


@test("import: hierarchy ordering (parents before children)")
async def test_import_hierarchy_order():
    db, checker = await setup()
    try:
        # Create policy with reversed order (child listed before parent)
        policy = {
            "version": 1,
            "groups": [
                {"id": 302, "name": "child", "parent_id": 301, "priority": 0},
                {"id": 301, "name": "parent", "parent_id": None, "priority": 0},
            ],
            "permissions": [],
            "user_groups": [],
            "group_permissions": [],
            "user_permissions": [],
            "object_permissions": [],
            "permission_rules": [],
            "field_permissions": [],
        }

        result = await checker.import_policy(policy)
        check("both groups imported", result["imported"]["groups"] == 2)
        check("no errors", len(result["errors"]) == 0)

        # Verify hierarchy
        ancestors = await checker.get_role_ancestors(302)
        check("child has parent in ancestors", 301 in ancestors)
    finally:
        await teardown(db)


@test("import: audit log recorded")
async def test_import_audit():
    db, checker = await setup()
    try:
        policy = {
            "version": 1,
            "groups": [
                {"id": 400, "name": "audit_test", "parent_id": None, "priority": 0}
            ],
            "permissions": [],
            "user_groups": [],
            "group_permissions": [],
            "user_permissions": [],
            "object_permissions": [],
            "permission_rules": [],
            "field_permissions": [],
        }
        await checker.import_policy(policy)

        from hyperdjango.auth.permissions import RBACauditLog

        audit = RBACauditLog(db)
        entries = await audit.get_recent(limit=5)
        import_entries = [e for e in entries if e["action"] == "import_policy"]
        check("audit log has import entry", len(import_entries) >= 1)
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard Stats Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("dashboard: basic stats queries")
async def test_dashboard_stats():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)

        # Total counts
        row = await db.query_one("SELECT COUNT(*) FROM hyper_groups")
        count = int(row[0] if not isinstance(row, dict) else row["count"])
        check("total groups = 3", count == 3)

        row = await db.query_one("SELECT COUNT(*) FROM hyper_permissions")
        count = int(row[0] if not isinstance(row, dict) else row["count"])
        check("total permissions = 8", count == 8)

        row = await db.query_one(
            "SELECT COUNT(*) FROM hyper_users WHERE is_active = true"
        )
        count = int(row[0] if not isinstance(row, dict) else row["count"])
        check("total active users = 2", count == 2)
    finally:
        await teardown(db)


@test("dashboard: users per group")
async def test_dashboard_users_per_group():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)

        rows = await db.query(
            "SELECT g.name, COUNT(ug.user_id) AS cnt "
            "FROM hyper_groups g "
            "LEFT JOIN hyper_user_groups ug ON g.id = ug.group_id "
            "GROUP BY g.id, g.name ORDER BY cnt DESC"
        )
        data = []
        for r in rows:
            if isinstance(r, dict):
                d = {"name": r["name"], "count": int(r["cnt"]) if r["cnt"] else 0}
            else:
                d = {"name": r[0], "count": int(r[1]) if r[1] else 0}
            data.append(d)

        check("3 groups in result", len(data) == 3)
        # editor has alice, admin has bob, viewer has 0
        editor_row = next(d for d in data if d["name"] == "editor")
        admin_row = next(d for d in data if d["name"] == "admin")
        viewer_row = next(d for d in data if d["name"] == "viewer")
        check("editor has 1 user", editor_row["count"] == 1)
        check("admin has 1 user", admin_row["count"] == 1)
        check("viewer has 0 users", viewer_row["count"] == 0)
    finally:
        await teardown(db)


@test("dashboard: permission coverage")
async def test_dashboard_coverage():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)

        rows = await db.query(
            "SELECT p.model_name, "
            "COUNT(DISTINCT p.id) AS perm_count, "
            "COUNT(DISTINCT gp.group_id) AS group_count, "
            "COUNT(DISTINCT up.user_id) AS user_count "
            "FROM hyper_permissions p "
            "LEFT JOIN hyper_group_permissions gp ON p.id = gp.permission_id "
            "LEFT JOIN hyper_user_permissions up ON p.id = up.permission_id "
            "GROUP BY p.model_name ORDER BY p.model_name"
        )
        coverage = []
        for r in rows:
            d = (
                dict(zip(["model_name", "perm_count", "group_count", "user_count"], r))
                if not isinstance(r, dict)
                else r
            )
            for k in ("perm_count", "group_count", "user_count"):
                d[k] = int(d[k]) if d[k] else 0
            coverage.append(d)

        check("2 models (post, employee)", len(coverage) == 2)
        post = next(d for d in coverage if d["model_name"] == "post")
        emp = next(d for d in coverage if d["model_name"] == "employee")
        check("post has 4 permissions", post["perm_count"] == 4)
        check("post has 3 assigned groups", post["group_count"] == 3)
        check("employee has 1 assigned user", emp["user_count"] == 1)
    finally:
        await teardown(db)


@test("dashboard: orphaned permissions")
async def test_dashboard_orphaned():
    db, checker = await setup()
    try:
        ids = await seed_policy(checker)

        rows = await db.query(
            "SELECT p.codename, p.model_name FROM hyper_permissions p "
            "WHERE p.id NOT IN (SELECT permission_id FROM hyper_group_permissions) "
            "AND p.id NOT IN (SELECT permission_id FROM hyper_user_permissions) "
            "ORDER BY p.model_name, p.codename"
        )
        orphaned = []
        for r in rows:
            d = (
                dict(zip(["codename", "model_name"], r))
                if not isinstance(r, dict)
                else r
            )
            orphaned.append(d)

        # post: view_post (viewer), change_post (editor), delete_post (admin) assigned
        # post: add_post not assigned
        # employee: add_employee assigned to user, rest unassigned
        check("has orphaned perms", len(orphaned) > 0)
        codenames = [o["codename"] for o in orphaned]
        check("add_post is orphaned", "add_post" in codenames)
        check("view_employee is orphaned", "view_employee" in codenames)
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Admin Route Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("admin: RBAC export/import methods exist")
async def test_admin_methods():
    from hyperdjango.admin import HyperAdmin

    check("has _make_rbac_policy_view", hasattr(HyperAdmin, "_make_rbac_policy_view"))
    check(
        "has _make_rbac_export_handler",
        hasattr(HyperAdmin, "_make_rbac_export_handler"),
    )
    check(
        "has _make_rbac_import_handler",
        hasattr(HyperAdmin, "_make_rbac_import_handler"),
    )
    check(
        "has _make_rbac_dashboard_view",
        hasattr(HyperAdmin, "_make_rbac_dashboard_view"),
    )


@test("admin: dashboard template has export/import buttons")
async def test_dashboard_buttons():
    from hyperdjango.admin.templates import TEMPLATE_DASHBOARD

    check("has export/import link", "rbac-policy" in TEMPLATE_DASHBOARD)
    check("has dashboard link", "rbac-dashboard" in TEMPLATE_DASHBOARD)


@test("admin: export template has download link")
async def test_export_template():
    from hyperdjango.admin.templates import TEMPLATE_RBAC_EXPORT

    check("has download link", "rbac-export/download" in TEMPLATE_RBAC_EXPORT)
    check("has import form", "rbac-import" in TEMPLATE_RBAC_EXPORT)
    check("has clear_existing checkbox", "clear_existing" in TEMPLATE_RBAC_EXPORT)


@test("admin: dashboard template has stats")
async def test_dashboard_template():
    from hyperdjango.admin.templates import TEMPLATE_RBAC_DASHBOARD

    check("has users_per_group", "users_per_group" in TEMPLATE_RBAC_DASHBOARD)
    check("has permission_coverage", "permission_coverage" in TEMPLATE_RBAC_DASHBOARD)
    check("has orphaned_permissions", "orphaned_permissions" in TEMPLATE_RBAC_DASHBOARD)
    check("has recent_changes", "recent_changes" in TEMPLATE_RBAC_DASHBOARD)
    check("has total_groups", "total_groups" in TEMPLATE_RBAC_DASHBOARD)
    check("has total_rules", "total_rules" in TEMPLATE_RBAC_DASHBOARD)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\n{'=' * 60}")
    print("RBAC Policy Export/Import + Dashboard Tests")
    print(f"{'=' * 60}\n")

    for name, func in test_funcs:
        print(f"\n[TEST] {name}")
        try:
            await func()
        except Exception as e:
            check(f"EXCEPTION: {e}", False)
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed:")
        for label, ok in results:
            if not ok:
                print(f"  \u2717 {label}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
