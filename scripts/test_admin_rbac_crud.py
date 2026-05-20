"""
Tests for RBAC admin CRUD: object permissions, permission rules, field permissions.

Validates that all 3 new RBAC entity types are registered in admin with
correct list_display, fieldsets, and CRUD operations via the admin interface.
"""

# hyper-test: db_isolated

import asyncio
import inspect
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
    symbol = "✓" if condition else "✗"
    print(f"  {symbol} {label}")


def make_admin():
    """Create a HyperAdmin with all auth models registered."""
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp

    app = HyperApp(title="RBAC Test")
    admin = HyperAdmin(app, prefix="/admin", require_auth=False)
    admin.register_auth_models()
    return admin


# ═══════════════════════════════════════════════════════════════════════════
# Registration Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("registration: all RBAC models registered in admin")
async def test_registration():
    admin = make_admin()
    check("object-permissions registered", "object-permissions" in admin._models)
    check("permission-rules registered", "permission-rules" in admin._models)
    check("field-permissions registered", "field-permissions" in admin._models)


@test("registration: object-permissions config correct")
async def test_objperm_config():
    admin = make_admin()
    cfg = admin._models["object-permissions"]
    check("list_display has object_model", "object_model" in cfg.list_display)
    check("list_display has object_id", "object_id" in cfg.list_display)
    check("list_display has user_id", "user_id" in cfg.list_display)
    check("list_display has group_id", "group_id" in cfg.list_display)
    check("has fieldsets", len(cfg.fieldsets) == 3)
    check("fieldset 0 title=Target", cfg.fieldsets[0].title == "Target")
    check("fieldset 1 title=Permission", cfg.fieldsets[1].title == "Permission")
    check("fieldset 2 title=Grant To", cfg.fieldsets[2].title == "Grant To")


@test("registration: permission-rules config correct")
async def test_rule_config():
    admin = make_admin()
    cfg = admin._models["permission-rules"]
    check("list_display has rule_type", "rule_type" in cfg.list_display)
    check("list_display has is_deny", "is_deny" in cfg.list_display)
    check("list_display has priority", "priority" in cfg.list_display)
    check("has 4 fieldsets (Rule, Config, Scope, Behavior)", len(cfg.fieldsets) == 4)
    check("fieldset 0 title=Rule", cfg.fieldsets[0].title == "Rule")
    check(
        "fieldset 1 title=Rule Configuration",
        cfg.fieldsets[1].title == "Rule Configuration",
    )
    check(
        "structured fields: _rc_owner_field",
        "_rc_owner_field" in cfg.fieldsets[1].fields,
    )
    check("structured fields: _rc_start", "_rc_start" in cfg.fieldsets[1].fields)
    check("structured fields: _rc_ranges", "_rc_ranges" in cfg.fieldsets[1].fields)
    check("rule_config excluded from form", "rule_config" in (cfg.exclude_fields or []))


@test("registration: field-permissions config correct")
async def test_fieldperm_config():
    admin = make_admin()
    cfg = admin._models["field-permissions"]
    check("list_display has model_name", "model_name" in cfg.list_display)
    check("list_display has field_name", "field_name" in cfg.list_display)
    check("list_display has access", "access" in cfg.list_display)
    check("has fieldsets", len(cfg.fieldsets) == 3)
    check("access in Access fieldset", "access" in cfg.fieldsets[1].fields)


@test("registration: group shows hierarchy fields")
async def test_group_hierarchy_fields():
    admin = make_admin()
    cfg = admin._models["groups"]
    check("group list_display has parent_id", "parent_id" in cfg.list_display)
    check("group list_display has priority", "priority" in cfg.list_display)
    check(
        "group has Hierarchy fieldset",
        any(fs.title == "Hierarchy" for fs in cfg.fieldsets),
    )
    hierarchy_fs = next(fs for fs in cfg.fieldsets if fs.title == "Hierarchy")
    check("parent_id in Hierarchy", "parent_id" in hierarchy_fs.fields)
    check("priority in Hierarchy", "priority" in hierarchy_fs.fields)


# ═══════════════════════════════════════════════════════════════════════════
# CRUD Tests (with real DB)
# ═══════════════════════════════════════════════════════════════════════════


@test("crud: object permission create and list")
async def test_objperm_crud():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Create test data
    alice = await checker.create_user("alice", "pass123", is_staff=True)
    await checker.create_default_permissions("post", "Post")

    # Grant object permission
    await checker.grant_object_perm("change_post", "post", "42", user_id=alice.id)

    # Verify via direct query
    rows = await db.query(
        "SELECT * FROM hyper_object_permissions WHERE user_id = $1 AND object_id = $2",
        alice.id,
        "42",
    )
    check("object perm row created in DB", len(rows) >= 1)

    # Revoke
    await checker.revoke_object_perm("change_post", "post", "42", user_id=alice.id)
    rows_after = await db.query(
        "SELECT * FROM hyper_object_permissions WHERE user_id = $1 AND object_id = $2",
        alice.id,
        "42",
    )
    check("object perm row deleted after revoke", len(rows_after) == 0)

    await drop_rbac_tables(db)
    await db.disconnect()


@test("crud: permission rule create and query")
async def test_rule_crud():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    bob = await checker.create_user("bob", "pass123", is_staff=True)
    await checker.create_default_permissions("post", "Post")
    editors = await checker.create_group("editors")

    # Add rule
    await checker.add_rule(
        "change_post",
        "post",
        "is_owner",
        {"owner_field": "author_id"},
        group_id=editors.id,
    )

    rows = await db.query("SELECT * FROM hyper_permission_rules")
    check("rule row created", len(rows) >= 1)
    row = rows[0]
    rule_type = row["rule_type"] if isinstance(row, dict) else row[2]
    check("rule_type is is_owner", rule_type == "is_owner")

    await drop_rbac_tables(db)
    await db.disconnect()


@test("crud: field permission create and query")
async def test_fieldperm_crud():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    viewers = await checker.create_group("viewers")

    # Set field access
    await checker.set_field_access(
        "employee", "salary", access="hidden", group_id=viewers.id
    )
    await checker.set_field_access(
        "employee", "ssn", access="readonly", group_id=viewers.id
    )

    rows = await db.query(
        "SELECT * FROM hyper_field_permissions WHERE group_id = $1", viewers.id
    )
    check("2 field permission rows created", len(rows) == 2)

    await drop_rbac_tables(db)
    await db.disconnect()


@test("admin hierarchy: _load_user_permissions uses CTE for inherited perms")
async def test_admin_hierarchy_perms():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Create hierarchy: viewer → editor → admin
    viewer = await checker.create_group("viewer")
    editor = await checker.create_group("editor", parent_id=viewer.id)
    admin_group = await checker.create_group("admin", parent_id=editor.id)

    await checker.create_default_permissions("post", "Post")
    await checker.grant_group_perm(viewer.id, "view_post", "post")
    await checker.grant_group_perm(editor.id, "change_post", "post")
    await checker.grant_group_perm(admin_group.id, "delete_post", "post")

    hieradmin = await checker.create_user("hieradmin", "pass123", is_staff=True)
    await checker.add_user_to_group(hieradmin.id, admin_group.id)

    # Simulate admin's _load_user_permissions
    admin = make_admin()
    admin._db = db
    user_dict = {"id": hieradmin.id, "is_superuser": False, "is_staff": True}
    await admin._load_user_permissions(user_dict)

    perms = user_dict.get("_permissions", set())
    check("admin CTE: has view_post (from viewer)", "view_post" in perms)
    check("admin CTE: has change_post (from editor)", "change_post" in perms)
    check("admin CTE: has delete_post (from admin)", "delete_post" in perms)
    check("admin CTE: does NOT have add_post", "add_post" not in perms)

    await drop_rbac_tables(db)
    await db.disconnect()


# ═══════════════════════════════════════════════════════════════════════════
# Inline, Choices, and Explain Tests
# ═══════════════════════════════════════════════════════════════════════════


@test("inlines: User has group membership inline")
async def test_user_inline():
    admin = make_admin()
    cfg = admin._models["users"]
    check("users has inlines", len(cfg.inlines) >= 1)
    inline = cfg.inlines[0]
    check("inline model is UserGroup", inline.model_class.__name__ == "UserGroup")
    check("inline shows group_id field", "group_id" in inline.fields)


@test("inlines: Group has permission inline")
async def test_group_inline():
    admin = make_admin()
    cfg = admin._models["groups"]
    check("groups has inlines", len(cfg.inlines) >= 1)
    inline = cfg.inlines[0]
    check(
        "inline model is GroupPermission",
        inline.model_class.__name__ == "GroupPermission",
    )
    check("inline shows permission_id field", "permission_id" in inline.fields)


@test("choices: rule_type is a select widget")
async def test_rule_type_select():
    admin = make_admin()
    cfg = admin._models["permission-rules"]
    rule_type_field = next(f for f in cfg.fields if f.name == "rule_type")
    check("rule_type widget is select", rule_type_field.widget == "select")
    check("rule_type has choices", rule_type_field.choices is not None)
    check(
        "choices include is_owner",
        any(c[0] == "is_owner" for c in rule_type_field.choices),
    )
    check(
        "choices include time_window",
        any(c[0] == "time_window" for c in rule_type_field.choices),
    )
    check(
        "choices include ip_range",
        any(c[0] == "ip_range" for c in rule_type_field.choices),
    )


@test("choices: access is a select widget")
async def test_access_select():
    admin = make_admin()
    cfg = admin._models["field-permissions"]
    access_field = next(f for f in cfg.fields if f.name == "access")
    check("access widget is select", access_field.widget == "select")
    check("access has choices", access_field.choices is not None)
    check("choices include hidden", any(c[0] == "hidden" for c in access_field.choices))
    check(
        "choices include readonly",
        any(c[0] == "readonly" for c in access_field.choices),
    )
    check(
        "choices include writable",
        any(c[0] == "writable" for c in access_field.choices),
    )


@test("explain: effective permissions returns full picture")
async def test_explain_effective():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Setup hierarchy
    viewer = await checker.create_group("viewer")
    editor = await checker.create_group("editor", parent_id=viewer.id)
    await checker.create_default_permissions("post", "Post")
    await checker.grant_group_perm(viewer.id, "view_post", "post")
    await checker.grant_group_perm(editor.id, "change_post", "post")

    explainme = await checker.create_user("explainme", "pass123", is_staff=True)
    await checker.add_user_to_group(explainme.id, editor.id)
    await checker.grant_user_perm(explainme.id, "add_post", "post")
    await checker.grant_object_perm("delete_post", "post", "42", user_id=explainme.id)
    await checker.set_field_access(
        "post", "secret", access="hidden", group_id=viewer.id
    )

    result = await checker.explain_effective_permissions(explainme.id)
    check("explain: user info present", result["user"] is not None)
    check("explain: groups present", len(result["groups"]) >= 1)
    check(
        "explain: direct perm (add_post)",
        any(p["codename"] == "add_post" for p in result["direct_permissions"]),
    )
    check(
        "explain: inherited perm (view_post)",
        any(p["codename"] == "view_post" for p in result["inherited_permissions"]),
    )
    check(
        "explain: inherited perm (change_post)",
        any(p["codename"] == "change_post" for p in result["inherited_permissions"]),
    )
    check(
        "explain: object perm (delete post 42)",
        any(p["object_id"] == "42" for p in result["object_permissions"]),
    )
    check(
        "explain: field access (secret hidden)",
        any(f["field_name"] == "secret" for f in result["field_access"]),
    )

    # Source attribution
    view_perm = next(
        p for p in result["inherited_permissions"] if p["codename"] == "view_post"
    )
    check("explain: source attribution present", "source" in view_perm)
    check("explain: via chain present", "via" in view_perm)

    await drop_rbac_tables(db)
    await db.disconnect()


@test("explain: permission decision chain shows steps")
async def test_explain_decision():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    decisionuser = await checker.create_user("decisionuser", "pass123", is_staff=True)
    await checker.create_default_permissions("post", "Post")
    await checker.grant_user_perm(decisionuser.id, "change_post", "post")

    class U:
        id = decisionuser.id
        is_active = True
        is_superuser = False

    user = U()
    result = await checker.explain_permission_decision(user, "change_post", "post")

    check("decision: allowed", result["allowed"] is True)
    check("decision: has steps", len(result["steps"]) >= 3)
    check(
        "decision: is_active step",
        any(s["check"] == "is_active" for s in result["steps"]),
    )
    check(
        "decision: is_superuser step",
        any(s["check"] == "is_superuser" for s in result["steps"]),
    )
    check(
        "decision: model_perm step",
        any(s["check"] == "model_perm" for s in result["steps"]),
    )

    # Test denied case
    result2 = await checker.explain_permission_decision(user, "delete_post", "post")
    check("decision: denied for missing perm", result2["allowed"] is False)

    await drop_rbac_tables(db)
    await db.disconnect()


@test("explain: decision with deny rule shows rule evaluation")
async def test_explain_deny_rule():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    denydecision = await checker.create_user("denydecision", "pass123", is_staff=True)
    await checker.create_default_permissions("post", "Post")
    await checker.grant_user_perm(denydecision.id, "delete_post", "post")

    # Add deny rule for published posts
    await checker.add_rule(
        "delete_post",
        "post",
        "field_match",
        {"field": "status", "values": ["published"]},
        user_id=denydecision.id,
        is_deny=True,
    )

    class U:
        id = denydecision.id
        is_active = True
        is_superuser = False

    user = U()
    published = {"id": 1, "status": "published"}
    result = await checker.explain_permission_decision(
        user, "delete_post", "post", obj=published
    )

    check("decision: denied by rule", result["allowed"] is False)
    deny_steps = [s for s in result["steps"] if "deny_rule" in s["check"]]
    check("decision: deny rule step present", len(deny_steps) >= 1)
    check("decision: deny rule matched", deny_steps[0]["result"] is True)

    await drop_rbac_tables(db)
    await db.disconnect()


# ═══════════════════════════════════════════════════════════════════════════
# Live UI Endpoint Tests (routes actually registered + renderable)
# ═══════════════════════════════════════════════════════════════════════════


@test("management: User has direct permission inline (UserPermission)")
async def test_user_perm_inline():
    admin = make_admin()
    cfg = admin._models["users"]
    check("users has 2 inlines", len(cfg.inlines) == 2)
    group_inline = cfg.inlines[0]
    perm_inline = cfg.inlines[1]
    check("first inline is UserGroup", group_inline.model_class.__name__ == "UserGroup")
    check(
        "second inline is UserPermission",
        perm_inline.model_class.__name__ == "UserPermission",
    )
    check("perm inline shows permission_id", "permission_id" in perm_inline.fields)


@test("management: form template has extra_links slot")
async def test_extra_links_template():
    from hyperdjango.admin.templates import TEMPLATE_FORM

    check("template has extra_links", "extra_links" in TEMPLATE_FORM)
    check("template renders link url", "link.url" in TEMPLATE_FORM)
    check("template renders link label", "link.label" in TEMPLATE_FORM)


@test("management: full user permission workflow via DB")
async def test_full_user_perm_workflow():
    """Simulate the full admin workflow: create user, assign group, grant direct perm, verify."""
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Step 1: Create user (like admin add form)
    workflow_user = await checker.create_user("workflow_user", "pass123", is_staff=True)
    check("workflow: user created", workflow_user is not None)

    # Step 2: Create groups with hierarchy (like admin group form)
    viewer = await checker.create_group("viewer")
    editor = await checker.create_group("editor", parent_id=viewer.id)
    check("workflow: groups created", editor is not None)

    # Step 3: Create permissions (auto-created on model register)
    await checker.create_default_permissions("article", "Article")

    # Step 4: Grant perms to groups (like group form inline)
    await checker.grant_group_perm(viewer.id, "view_article", "article")
    await checker.grant_group_perm(editor.id, "change_article", "article")

    # Step 5: Assign user to editor group (like user form inline)
    await checker.add_user_to_group(workflow_user.id, editor.id)

    # Step 6: Grant direct permission (like user form UserPermission inline)
    await checker.grant_user_perm(workflow_user.id, "delete_article", "article")

    # Step 7: Set object permission (like object-permissions form)
    await checker.grant_object_perm(
        "add_article", "article", "special-draft", user_id=workflow_user.id
    )

    # Step 8: Add conditional rule (like permission-rules form)
    await checker.add_rule(
        "change_article",
        "article",
        "is_owner",
        {"owner_field": "author_id"},
        group_id=editor.id,
    )

    # Step 9: Set field access (like field-permissions form)
    await checker.set_field_access(
        "article", "internal_notes", access="hidden", group_id=viewer.id
    )
    await checker.set_field_access(
        "article", "internal_notes", access="readonly", group_id=editor.id
    )

    # Step 10: Verify effective permissions
    result = await checker.explain_effective_permissions(workflow_user.id)
    check("workflow: user info", result["user"]["username"] == "workflow_user")
    check(
        "workflow: in editor group",
        any(g["name"] == "editor" for g in result["groups"]),
    )

    # Direct perms
    direct_codes = {p["codename"] for p in result["direct_permissions"]}
    check("workflow: has direct delete_article", "delete_article" in direct_codes)

    # Inherited perms
    inherited_codes = {p["codename"] for p in result["inherited_permissions"]}
    check(
        "workflow: inherits view_article (from viewer)",
        "view_article" in inherited_codes,
    )
    check(
        "workflow: inherits change_article (from editor)",
        "change_article" in inherited_codes,
    )

    # Object perms
    check(
        "workflow: has object perm on special-draft",
        any(p["object_id"] == "special-draft" for p in result["object_permissions"]),
    )

    # Rules
    check(
        "workflow: has is_owner rule",
        any(r["rule_type"] == "is_owner" for r in result["rules"]),
    )

    # Field access
    check(
        "workflow: internal_notes field restricted",
        any(f["field_name"] == "internal_notes" for f in result["field_access"]),
    )

    # Step 11: Test decision chain
    class U:
        id = workflow_user.id
        is_active = True
        is_superuser = False

    user = U()
    own_article = {"id": 1, "author_id": workflow_user.id}
    decision = await checker.explain_permission_decision(
        user, "change_article", "article", obj=own_article
    )
    check("workflow: change own article allowed", decision["allowed"] is True)

    other_article = {"id": 2, "author_id": 999}
    decision2 = await checker.explain_permission_decision(
        user, "change_article", "article", obj=other_article
    )
    check("workflow: change other's article denied", decision2["allowed"] is False)

    await drop_rbac_tables(db)
    await db.disconnect()


@test("ui routes: group tree route registered")
async def test_group_tree_route():
    admin = make_admin()
    result = admin.app.router.resolve("GET", "/admin/groups/tree/")
    check("group tree route exists", result is not None)


@test("ui templates: group tree template has hierarchy sections")
async def test_group_tree_template():
    from hyperdjango.admin.templates import TEMPLATE_GROUP_TREE

    check(
        "tree template has Group Hierarchy title",
        "Group Hierarchy" in TEMPLATE_GROUP_TREE,
    )
    check("tree template has depth indentation", "depth" in TEMPLATE_GROUP_TREE)
    check("tree template has perm_count", "perm_count" in TEMPLATE_GROUP_TREE)
    check("tree template has member_count", "member_count" in TEMPLATE_GROUP_TREE)
    check(
        "tree template has root/inherits labels",
        "root" in TEMPLATE_GROUP_TREE and "inherits" in TEMPLATE_GROUP_TREE,
    )
    check("tree template has edit links", "Edit" in TEMPLATE_GROUP_TREE)


@test("ui routes: dashboard has role hierarchy link")
async def test_dashboard_tree_link():
    from hyperdjango.admin.templates import TEMPLATE_DASHBOARD

    check("dashboard has groups/tree link", "groups/tree" in TEMPLATE_DASHBOARD)


@test("audit: RBAC mutations generate audit log entries")
async def test_rbac_audit_log():
    from hyperdjango.auth.permissions import PermissionChecker, RBACauditLog
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Create group (should log)
    audited_group = await checker.create_group("audited_group")
    await checker.create_default_permissions("doc", "Document")

    # Grant perm (should log)
    await checker.grant_group_perm(audited_group.id, "view_doc", "doc")

    # Create user and add to group (should log)
    audituser = await checker.create_user("audituser", "pass123", is_staff=True)
    await checker.add_user_to_group(audituser.id, audited_group.id)

    # Grant direct perm (should log)
    await checker.grant_user_perm(audituser.id, "add_doc", "doc")

    # Add rule (should log)
    await checker.add_rule(
        "view_doc",
        "doc",
        "is_owner",
        {"owner_field": "user_id"},
        group_id=audited_group.id,
    )

    # Set field access (should log)
    await checker.set_field_access(
        "doc", "secret", access="hidden", group_id=audited_group.id
    )

    # Read audit log
    audit = RBACauditLog(db)
    entries = await audit.get_recent(limit=20)
    actions = [e["action"] for e in entries]

    check("audit: create_group logged", "create_group" in actions)
    check("audit: grant_perm (group) logged", actions.count("grant_perm") >= 1)
    check("audit: add_to_group logged", "add_to_group" in actions)
    check("audit: add_rule logged", "add_rule" in actions)
    check("audit: set_field_access logged", "set_field_access" in actions)
    check("audit: entries have timestamps", all(e.get("timestamp") for e in entries))

    await drop_rbac_tables(db)
    await db.disconnect()


@test("audit: RBAC audit route registered")
async def test_rbac_audit_route():
    admin = make_admin()
    result = admin.app.router.resolve("GET", "/admin/rbac-audit/")
    check("rbac-audit route exists", result is not None)


@test("audit: dashboard has RBAC audit link")
async def test_dashboard_audit_link():
    from hyperdjango.admin.templates import TEMPLATE_DASHBOARD

    check("dashboard has rbac-audit link", "rbac-audit" in TEMPLATE_DASHBOARD)


@test("ui routes: effective-permissions route registered")
async def test_effective_perms_route():
    admin = make_admin()
    app = admin.app
    # Check the route exists
    result = app.router.resolve("GET", "/admin/users/1/effective-permissions/")
    check("effective-permissions route exists", result is not None)


@test("ui routes: permission-check GET route registered")
async def test_perm_check_route_get():
    admin = make_admin()
    result = admin.app.router.resolve("GET", "/admin/permission-check/")
    check("permission-check GET route exists", result is not None)


@test("ui routes: permission-check POST route registered")
async def test_perm_check_route_post():
    admin = make_admin()
    result = admin.app.router.resolve("POST", "/admin/permission-check/")
    check("permission-check POST route exists", result is not None)


@test("ui routes: dashboard has permission checker link")
async def test_dashboard_perm_link():
    from hyperdjango.admin.templates import TEMPLATE_DASHBOARD

    check(
        "dashboard template mentions permission-check",
        "permission-check" in TEMPLATE_DASHBOARD,
    )


@test("ui templates: effective perms template has all sections")
async def test_effective_perms_template():
    from hyperdjango.admin.templates import TEMPLATE_EFFECTIVE_PERMS

    check(
        "template has Direct Permissions section",
        "Direct Permissions" in TEMPLATE_EFFECTIVE_PERMS,
    )
    check(
        "template has Inherited Permissions section",
        "Inherited Permissions" in TEMPLATE_EFFECTIVE_PERMS,
    )
    check(
        "template has Object-Level section", "Object-Level" in TEMPLATE_EFFECTIVE_PERMS
    )
    check(
        "template has Conditional Rules section",
        "Conditional Rules" in TEMPLATE_EFFECTIVE_PERMS,
    )
    check(
        "template has Field-Level Access section",
        "Field-Level Access" in TEMPLATE_EFFECTIVE_PERMS,
    )
    check(
        "template has DENY/ALLOW badges",
        "DENY" in TEMPLATE_EFFECTIVE_PERMS and "ALLOW" in TEMPLATE_EFFECTIVE_PERMS,
    )


@test("ui templates: perm check template has decision chain")
async def test_perm_check_template():
    from hyperdjango.admin.templates import TEMPLATE_PERM_CHECK

    check("template has form inputs", "user_id" in TEMPLATE_PERM_CHECK)
    check("template has perm input", 'name="perm"' in TEMPLATE_PERM_CHECK)
    check("template has model_name input", 'name="model_name"' in TEMPLATE_PERM_CHECK)
    check("template has Decision Chain", "Decision Chain" in TEMPLATE_PERM_CHECK)
    check(
        "template has ALLOWED/DENIED",
        "ALLOWED" in TEMPLATE_PERM_CHECK and "DENIED" in TEMPLATE_PERM_CHECK,
    )
    check(
        "template has PASS/FAIL",
        "PASS" in TEMPLATE_PERM_CHECK and "FAIL" in TEMPLATE_PERM_CHECK,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\nAdmin RBAC CRUD Tests ({len(test_funcs)} tests)")
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
