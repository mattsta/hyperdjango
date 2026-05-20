"""Tests for QuerySet.with_cte() — task #197.

CTE prefix support for recursive queries (RBAC role trees,
tenant hierarchies, metering account rollup) that can't be cleanly
expressed via pure ORM chainables.

Tests cover:
1. Simple (non-recursive) CTE prefix
2. WITH RECURSIVE prefix when any clause is recursive
3. CTE referencing via where_raw on the outer query
4. Parameter numbering across CTE body + outer WHERE
5. Multiple CTEs (comma-separated WITH list)
6. Real recursive CTE — walk a parent/child tree

Usage:
    uv run hyper-test with_cte
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


class CteGroup(Model):
    class Meta:
        table = "cte_groups"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    parent_id: int = Field(default=0)  # 0 = no parent


class CtePermission(Model):
    class Meta:
        table = "cte_permissions"

    id: int = Field(primary_key=True, auto=True)
    codename: str = Field(max_length=100)


class CteGroupPerm(Model):
    class Meta:
        table = "cte_group_perms"

    id: int = Field(primary_key=True, auto=True)
    group_id: int = Field(foreign_key=CteGroup)
    permission_id: int = Field(foreign_key=CtePermission)


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    for sql in [
        "DROP TABLE IF EXISTS cte_group_perms CASCADE",
        "DROP TABLE IF EXISTS cte_permissions CASCADE",
        "DROP TABLE IF EXISTS cte_groups CASCADE",
        """CREATE TABLE cte_groups (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            parent_id INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE cte_permissions (
            id SERIAL PRIMARY KEY,
            codename VARCHAR(100) NOT NULL
        )""",
        """CREATE TABLE cte_group_perms (
            id SERIAL PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES cte_groups(id),
            permission_id INTEGER NOT NULL REFERENCES cte_permissions(id)
        )""",
    ]:
        await db.execute(sql)
    return db


async def teardown_db(db):
    for tbl in ("cte_group_perms", "cte_permissions", "cte_groups"):
        await db.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


async def main() -> int:
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

    db = await setup_db()
    try:
        # Build a group tree:
        #   admin (1)
        #   ├── staff (2, parent=1)
        #   │   └── moderator (3, parent=2)
        #   └── hr (4, parent=1)
        #   guest (5, no parent)
        admin = CteGroup(name="admin", parent_id=0)
        await admin.save()
        staff = CteGroup(name="staff", parent_id=admin.id)
        await staff.save()
        mod = CteGroup(name="moderator", parent_id=staff.id)
        await mod.save()
        hr = CteGroup(name="hr", parent_id=admin.id)
        await hr.save()
        guest = CteGroup(name="guest", parent_id=0)
        await guest.save()

        # Permissions
        read_all = CtePermission(codename="read_all")
        await read_all.save()
        moderate = CtePermission(codename="moderate")
        await moderate.save()
        kick = CtePermission(codename="kick_user")
        await kick.save()

        # Admin has read_all, staff has moderate, moderator has kick
        await CteGroupPerm(group_id=admin.id, permission_id=read_all.id).save()
        await CteGroupPerm(group_id=staff.id, permission_id=moderate.id).save()
        await CteGroupPerm(group_id=mod.id, permission_id=kick.id).save()

        # ── Test 1: Simple non-recursive CTE ──────────────────────────
        print("\n=== Non-recursive CTE prefix ===")

        # Use a CTE to pre-compute "all groups named admin" then filter
        # by CTE membership. Contrived but exercises the compile path.
        qs = CteGroup.objects.with_cte(
            "admin_groups",
            "SELECT id FROM cte_groups WHERE name = {idx}",
            "admin",
        ).where_raw("id IN (SELECT id FROM admin_groups)")
        result = await qs.all()
        check("non-recursive CTE matches 1 row", len(result) == 1)
        check("non-recursive CTE returns admin", result[0].name == "admin")

        # ── Test 2: Recursive CTE — descendants of admin ──────────────
        print("\n=== WITH RECURSIVE — descendant tree walk ===")

        # "All groups in the admin subtree" — admin + its descendants
        descendants_qs = (
            CteGroup.objects.with_cte(
                "admin_tree",
                "SELECT id, name, parent_id FROM cte_groups WHERE id = {idx} "
                "UNION ALL "
                "SELECT g.id, g.name, g.parent_id FROM cte_groups g "
                "JOIN admin_tree t ON g.parent_id = t.id",
                admin.id,
                recursive=True,
            )
            .where_raw("id IN (SELECT id FROM admin_tree)")
            .order_by("id")
        )
        desc = await descendants_qs.all()
        names = [g.name for g in desc]
        check("recursive CTE walked 4 levels", len(desc) == 4)
        check(
            "recursive CTE found admin+staff+mod+hr",
            set(names) == {"admin", "staff", "moderator", "hr"},
            f"got {names}",
        )
        check("recursive CTE excluded guest", "guest" not in names)

        # ── Test 3: Permission query via recursive role_tree ──────────
        # This mirrors the real hyperdjango/auth/permissions.py pattern.
        print("\n=== Permission query via role_tree CTE ===")

        perm_qs = (
            CtePermission.objects.with_cte(
                "role_tree",
                "SELECT id FROM cte_groups WHERE id = {idx} "
                "UNION ALL "
                "SELECT g.id FROM cte_groups g "
                "JOIN role_tree rt ON g.parent_id = rt.id",
                admin.id,
                recursive=True,
            )
            .where_raw(
                "id IN ("
                "SELECT permission_id FROM cte_group_perms "
                "WHERE group_id IN (SELECT id FROM role_tree)"
                ")"
            )
            .order_by("id")
        )
        perms = await perm_qs.all()
        codenames = {p.codename for p in perms}
        check("permissions via role_tree count == 3", len(perms) == 3)
        check(
            "admin role inherits all descendant perms",
            codenames == {"read_all", "moderate", "kick_user"},
            f"got {codenames}",
        )

        # ── Test 4: Param numbering across CTE + outer WHERE ──────────
        print("\n=== Param numbering: CTE + outer filter ===")

        # CTE uses 1 param (admin.id), outer WHERE uses 1 param (1 row limit)
        limited_qs = (
            CteGroup.objects.with_cte(
                "admin_tree",
                "SELECT id FROM cte_groups WHERE id = {idx} "
                "UNION ALL "
                "SELECT g.id FROM cte_groups g "
                "JOIN admin_tree t ON g.parent_id = t.id",
                admin.id,
                recursive=True,
            )
            .where_raw("id IN (SELECT id FROM admin_tree) AND name != {idx}", "admin")
            .order_by("name")
        )
        res = await limited_qs.all()
        check("param count across CTE+WHERE == 3", len(res) == 3)
        res_names = {r.name for r in res}
        check(
            "result is staff/moderator/hr",
            res_names == {"staff", "moderator", "hr"},
            f"got {res_names}",
        )

        # ── Test 5: Multiple CTEs ─────────────────────────────────────
        print("\n=== Multiple WITH clauses ===")

        multi_qs = (
            CteGroup.objects.with_cte(
                "top_level",
                "SELECT id FROM cte_groups WHERE parent_id = {idx}",
                0,
            )
            .with_cte(
                "has_children",
                "SELECT DISTINCT parent_id AS id FROM cte_groups "
                "WHERE parent_id != {idx}",
                0,
            )
            .where_raw(
                "id IN (SELECT id FROM top_level) "
                "AND id IN (SELECT id FROM has_children)"
            )
            .order_by("id")
        )
        multi = await multi_qs.all()
        check("multi-CTE count == 1", len(multi) == 1)
        check(
            "multi-CTE is admin (top level with children)",
            multi and multi[0].name == "admin",
        )

    finally:
        await teardown_db(db)
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All with_cte tests passed!")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
