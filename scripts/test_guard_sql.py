"""
HyperGuard SQL generation + QuerySet integration + Require.policy() bridge tests.

Tests the full Phase 4 pipeline:
1. Policy AST → SQL WHERE fragments
2. QuerySet.guard_filter() applies SQL from policies
3. Require.policy() bridges @guard to PolicyRegistry
"""

# hyper-test: unit

import asyncio

from hyperdjango.auth.user import SessionUser
from hyperdjango.guard import DenyReason, GuardContext, Require, guard
from hyperdjango.guard.registry import PolicyRegistry
from hyperdjango.guard.sql import generate_where

_PASS = 0
_FAIL = 0


def check(condition: bool, msg: str) -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {msg}")


def _run(coro):
    return asyncio.run(coro)


# ── Test: SQL generation from policies ───────────────────────────────────────


def test_sql_simple_bool_condition():
    """resource.is_public = true → is_public = $1 with param True."""
    print("test_sql_simple_bool_condition")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)
    resource = registry.get_resource("Forum")
    fragment = generate_where(resource, "read")
    check(fragment.sql != "FALSE", f"should generate SQL, got: {fragment.sql}")
    check(
        '"is_public"' in fragment.sql,
        f"should reference is_public, got: {fragment.sql}",
    )
    check(len(fragment.params) > 0, "should have params")
    check(fragment.params[0] is True, f"param should be True, got: {fragment.params}")


def test_sql_multiple_and_conditions():
    """Multiple AND conditions → col1 = $1 AND col2 = $2."""
    print("test_sql_multiple_and_conditions")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
        }
    }
    """)
    resource = registry.get_resource("Forum")
    fragment = generate_where(resource, "write_post")
    check("is_archived" in fragment.sql, f"should have is_archived: {fragment.sql}")
    check("is_locked" in fragment.sql, f"should have is_locked: {fragment.sql}")
    check("AND" in fragment.sql, f"should have AND: {fragment.sql}")


def test_sql_ownership_cross_field():
    """resource.author_id = user.id → author_id = $1 with user's id as param."""
    print("test_sql_ownership_cross_field")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Post {
        allow edit where {
            resource.author_id = user.id
        }
    }
    """)
    resource = registry.get_resource("Post")
    fragment = generate_where(resource, "edit", user_fields={"id": 42})
    check("author_id" in fragment.sql, f"should reference author_id: {fragment.sql}")
    check(42 in fragment.params, f"should have user id 42 in params: {fragment.params}")


def test_sql_deny_before_allow():
    """Deny rules become NOT(...) in SQL."""
    print("test_sql_deny_before_allow")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Post {
        deny read where {
            resource.is_deleted = true
        }
        allow read where {
            resource.is_public = true
        }
    }
    """)
    resource = registry.get_resource("Post")
    fragment = generate_where(resource, "read")
    check("NOT" in fragment.sql, f"should have NOT for deny: {fragment.sql}")
    check(
        "is_public" in fragment.sql, f"should have is_public for allow: {fragment.sql}"
    )
    check(
        "is_deleted" in fragment.sql, f"should have is_deleted for deny: {fragment.sql}"
    )


def test_sql_multiple_allow_or():
    """Multiple allow rules → OR-joined."""
    print("test_sql_multiple_allow_or")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Post {
        allow edit where {
            resource.author_id = user.id
        }
        allow edit where {
            user.is_staff = true
        }
    }
    """)
    resource = registry.get_resource("Post")
    fragment = generate_where(resource, "edit", user_fields={"id": 1, "is_staff": True})
    check("OR" in fragment.sql, f"should have OR: {fragment.sql}")


def test_sql_no_rules():
    """No rules for action → FALSE."""
    print("test_sql_no_rules")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)
    resource = registry.get_resource("Forum")
    fragment = generate_where(resource, "delete")
    check(fragment.sql == "FALSE", f"no rules = FALSE, got: {fragment.sql}")


def test_sql_needs_python_deny_blocks():
    """Deny rule with relation → FALSE (can't generate safe SQL)."""
    print("test_sql_needs_python_deny_blocks")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        deny read where {
            user is banned of resource
        }
        allow read where {
            resource.is_public = true
        }
    }
    """)
    resource = registry.get_resource("Forum")
    fragment = generate_where(resource, "read")
    check(fragment.sql == "FALSE", f"needs_python deny = FALSE: {fragment.sql}")


def test_sql_string_condition():
    """String conditions generate correct SQL."""
    print("test_sql_string_condition")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Post {
        allow read where {
            resource.status = "published"
        }
    }
    """)
    resource = registry.get_resource("Post")
    fragment = generate_where(resource, "read")
    check("status" in fragment.sql, f"should reference status: {fragment.sql}")
    check(
        "published" in fragment.params,
        f"should have 'published' in params: {fragment.params}",
    )


def test_sql_with_table_prefix():
    """Table prefix applied to column names."""
    print("test_sql_with_table_prefix")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)
    resource = registry.get_resource("Forum")
    fragment = generate_where(resource, "read", table_name="hn_forums")
    check(
        '"hn_forums"."is_public"' in fragment.sql,
        f"should have quoted table prefix: {fragment.sql}",
    )


# ── Test: Require.policy() bridge ────────────────────────────────────────────


def test_require_policy_allows():
    """Require.policy() passes when policy allows."""
    print("test_require_policy_allows")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)

    from dataclasses import dataclass, field

    @dataclass
    class MockRequest:
        user: SessionUser | None = None
        path: str = "/"
        method: str = "GET"
        path_params: dict[str, str] = field(default_factory=dict)
        guard: object = None
        api_key_valid: bool = False

    req = Require.policy(
        "Forum.read",
        registry=registry,
        resource_dict_fn=lambda r, ctx: {"is_public": True},
    )
    request = MockRequest(user=SessionUser({"id": 1}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "policy allows → passes")


def test_require_policy_denies():
    """Require.policy() denies when policy denies."""
    print("test_require_policy_denies")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)

    from dataclasses import dataclass, field

    @dataclass
    class MockRequest:
        user: SessionUser | None = None
        path: str = "/"
        method: str = "GET"
        path_params: dict[str, str] = field(default_factory=dict)
        guard: object = None
        api_key_valid: bool = False

    req = Require.policy(
        "Forum.read",
        registry=registry,
        resource_dict_fn=lambda r, ctx: {"is_public": False},
    )
    request = MockRequest(user=SessionUser({"id": 1}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "policy denies → denied")
    check(result.reason == DenyReason.FORBIDDEN, f"reason: {result.reason}")
    check("Forum.read" in result.message, f"message: {result.message}")


def test_require_policy_invalid_format():
    """Require.policy() rejects invalid resource.action format."""
    print("test_require_policy_invalid_format")
    try:
        Require.policy("invalid", registry=PolicyRegistry())
        check(False, "should raise ValueError")
    except ValueError as e:
        check("Resource.action" in str(e), f"error: {e}")


def test_require_policy_in_guard_chain():
    """Require.policy() works in a @guard() chain with resource resolvers."""
    print("test_require_policy_in_guard_chain")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
        }
    }
    """)

    from dataclasses import dataclass, field

    @dataclass
    class MockRequest:
        user: SessionUser | None = None
        path: str = "/"
        method: str = "POST"
        path_params: dict[str, str] = field(default_factory=dict)
        guard: object = None
        api_key_valid: bool = False

    @dataclass
    class MockForum:
        name: str = "python"
        is_archived: bool = False
        is_locked: bool = False

    forum = MockForum()

    async def resolve_forum(request, ctx, name):
        return forum

    @guard(
        Require.authenticated(),
        Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
        Require.policy(
            "Forum.write_post",
            registry=registry,
            resource_dict_fn=lambda r, ctx: {
                "is_archived": ctx.forum.is_archived,
                "is_locked": ctx.forum.is_locked,
            },
        ),
    )
    async def forum_submit(request, forum_name: str):
        return {"ok": True, "forum": request.guard.forum.name}

    # Should pass: authenticated + forum resolved + policy allows
    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "python"},
    )
    result = _run(forum_submit(request, forum_name="python"))
    check(result["ok"] is True, "full chain passes")
    check(result["forum"] == "python", "forum accessible")


def test_require_policy_chain_denies_archived():
    """Policy denies archived forum in a full chain."""
    print("test_require_policy_chain_denies_archived")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
        }
    }
    """)

    from dataclasses import dataclass, field

    from hyperdjango.exceptions import HTTPException

    @dataclass
    class MockRequest:
        user: SessionUser | None = None
        path: str = "/"
        method: str = "POST"
        path_params: dict[str, str] = field(default_factory=dict)
        guard: object = None
        api_key_valid: bool = False

    @dataclass
    class MockForum:
        name: str = "archived"
        is_archived: bool = True
        is_locked: bool = False

    async def resolve_forum(request, ctx, name):
        return MockForum()

    @guard(
        Require.authenticated(),
        Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
        Require.policy(
            "Forum.write_post",
            registry=registry,
            resource_dict_fn=lambda r, ctx: {
                "is_archived": ctx.forum.is_archived,
                "is_locked": ctx.forum.is_locked,
            },
        ),
    )
    async def forum_submit(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "archived"},
    )
    try:
        _run(forum_submit(request, forum_name="archived"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 403, f"403, got {e.status_code}")
        check("Forum.write_post" in str(e.detail), f"message: {e.detail}")


def test_require_policy_no_resource_dict_fn():
    """Require.policy() works with user-only checks (no resource_dict_fn)."""
    print("test_require_policy_no_resource_dict_fn")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Admin {
        allow access where {
            user.is_staff = true
        }
    }
    """)

    from dataclasses import dataclass, field

    @dataclass
    class MockRequest:
        user: SessionUser | None = None
        path: str = "/"
        method: str = "GET"
        path_params: dict[str, str] = field(default_factory=dict)
        guard: object = None
        api_key_valid: bool = False

    # Staff → allow (no resource_dict_fn needed)
    req = Require.policy("Admin.access", registry=registry)
    request = MockRequest(user=SessionUser({"id": 1, "is_staff": True}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "staff user passes user-only policy")

    # Non-staff → deny
    request2 = MockRequest(user=SessionUser({"id": 2, "is_staff": False}))
    ctx2 = GuardContext()
    result2 = _run(req.evaluate_fn(request2, ctx2))
    check(result2 is not None, "non-staff denied by user-only policy")


def test_sql_param_count_and_ordering():
    """Verify {idx} placeholder count matches param count."""
    print("test_sql_param_count_and_ordering")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
        }
    }
    """)
    resource = registry.get_resource("Forum")
    fragment = generate_where(resource, "write_post")
    idx_count = fragment.sql.count("{idx}")
    check(
        idx_count == len(fragment.params),
        f"{idx_count} placeholders but {len(fragment.params)} params: {fragment.sql}",
    )
    check(idx_count == 2, f"expected 2 placeholders, got {idx_count}")


def test_sql_column_quoting():
    """Column names are quoted to handle reserved words."""
    print("test_sql_column_quoting")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Item {
        allow read where {
            resource.is_public = true
        }
    }
    """)
    resource = registry.get_resource("Item")
    fragment = generate_where(resource, "read")
    check('"is_public"' in fragment.sql, f"column should be quoted: {fragment.sql}")


# ── Run all ──────────────────────────────────────────────────────────────────


def main():
    tests = [
        # SQL generation
        test_sql_simple_bool_condition,
        test_sql_multiple_and_conditions,
        test_sql_ownership_cross_field,
        test_sql_deny_before_allow,
        test_sql_multiple_allow_or,
        test_sql_no_rules,
        test_sql_needs_python_deny_blocks,
        test_sql_string_condition,
        test_sql_with_table_prefix,
        # Require.policy() bridge
        test_require_policy_allows,
        test_require_policy_denies,
        test_require_policy_invalid_format,
        test_require_policy_in_guard_chain,
        test_require_policy_chain_denies_archived,
        test_require_policy_no_resource_dict_fn,
        test_sql_param_count_and_ordering,
        test_sql_column_quoting,
    ]

    for test in tests:
        test()

    total = _PASS + _FAIL
    print(f"\n{'=' * 60}")
    print(f"HyperGuard SQL + Policy Bridge: {_PASS}/{total} passed, {_FAIL} failed")
    if _FAIL:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
