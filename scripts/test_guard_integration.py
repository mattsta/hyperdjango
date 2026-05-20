"""
HyperGuard integration tests — proves @guard works with real resolver patterns.

Tests the bridge between HyperGuard's Require.resource() and HyperNews-style
resolve_forum/resolve_post patterns without needing a running database.

Validates:
1. Intent-based resource resolvers via @guard
2. Forum access control (read, write, moderate, admin)
3. Post access control (ownership, forum state)
4. Composed requirement chains matching real HyperNews routes
5. Error cases: archived, locked, private, banned
6. Redirect-on-auth patterns

The db_isolated marker gives this file its own empty database: the
not_banned/not_muted requirements consult the timeline through whatever
DATABASE_URL resolves to, so an isolated schema-less database keeps their
outcome deterministic (timeline unavailable → session flags decide) instead
of depending on whatever status rows the ambient shared database holds.
"""

# hyper-test: db_isolated

import asyncio
from dataclasses import dataclass, field
from enum import Enum

from hyperdjango.auth.user import SessionUser
from hyperdjango.exceptions import HTTPException
from hyperdjango.guard import (
    DenyReason,
    GuardContext,
    GuardDenial,
    Require,
    guard,
)

# ── Mock models (mimic HyperNews) ───────────────────────────────────────────


class ForumIntent(Enum):
    READ = "read"
    WRITE_POST = "write_post"
    WRITE_COMMENT = "write_comment"
    MODERATE = "moderate"
    ADMIN = "admin"


@dataclass
class Forum:
    id: int = 1
    name: str = "test"
    title: str = "Test Forum"
    is_public: bool = True
    is_archived: bool = False
    is_locked: bool = False
    is_hidden: bool = False


@dataclass
class ForumMember:
    user_id: int = 1
    forum_id: int = 1
    role: str = "member"


@dataclass
class ForumAccess:
    forum: Forum
    is_member: bool
    is_mod: bool
    membership: ForumMember | None


@dataclass
class Post:
    id: int = 42
    title: str = "Test Post"
    author_id: int = 1
    forum_id: int = 1
    status: str = "published"
    is_deleted: bool = False


@dataclass
class PostAccess:
    post: Post
    forum: Forum | None


@dataclass
class MockRequest:
    user: SessionUser | None = None
    path: str = "/"
    method: str = "GET"
    path_params: dict[str, str] = field(default_factory=dict)
    guard: object = None
    cookies: dict[str, str] = field(default_factory=dict)


# ── Test database (in-memory) ────────────────────────────────────────────────

_FORUMS: dict[str, Forum] = {
    "python": Forum(id=1, name="python", title="Python"),
    "archived": Forum(id=2, name="archived", title="Archived", is_archived=True),
    "locked": Forum(id=3, name="locked", title="Locked", is_locked=True),
    "private": Forum(id=4, name="private", title="Private", is_public=False),
    "hidden": Forum(
        id=5, name="hidden", title="Hidden", is_hidden=True, is_public=False
    ),
}

_MEMBERS: dict[tuple[int, int], ForumMember] = {
    (1, 1): ForumMember(user_id=1, forum_id=1, role="member"),
    (1, 4): ForumMember(user_id=1, forum_id=4, role="moderator"),
    (1, 5): ForumMember(user_id=1, forum_id=5, role="admin"),
    (2, 1): ForumMember(user_id=2, forum_id=1, role="admin"),
}

_POSTS: dict[int, Post] = {
    42: Post(id=42, title="Test", author_id=1, forum_id=1),
    43: Post(id=43, title="Draft", author_id=1, forum_id=1, status="draft"),
    44: Post(id=44, title="Archived Post", author_id=2, forum_id=2),
}

_WRITE_INTENTS = frozenset({ForumIntent.WRITE_POST, ForumIntent.WRITE_COMMENT})
_MOD_ROLES = frozenset({"moderator", "admin"})


# ── Resource resolvers (bridge @guard to resolve_forum pattern) ──────────────


def _make_forum_resolver(intent: ForumIntent):
    """Create a forum resolver for a specific intent.

    This is the bridge between @guard's Require.resource() and
    HyperNews's resolve_forum(request, name, intent) pattern.
    """

    async def resolver(
        request: MockRequest, ctx: GuardContext, forum_name: str
    ) -> ForumAccess | None:
        forum = _FORUMS.get(forum_name)
        if not forum:
            return None

        # Resolve membership
        uid = request.user.id if request.user is not None else 0
        member = _MEMBERS.get((uid, forum.id))
        is_member = member is not None
        is_mod = member is not None and member.role in _MOD_ROLES

        # Hidden forum — only members see it
        if forum.is_hidden and not is_member:
            return None  # 404, not 403

        # Private forum — require membership
        if not forum.is_public and not is_member:
            raise HTTPException(403, "This forum is private")

        # Write intents — reject archived and locked
        if intent in _WRITE_INTENTS:
            if forum.is_archived:
                raise HTTPException(403, "This forum is archived")
            if forum.is_locked:
                raise HTTPException(403, "This forum is not accepting new posts")

        # Moderate — require mod or admin role
        if intent == ForumIntent.MODERATE:
            if not is_mod:
                is_staff = request.user.is_staff if request.user is not None else False
                if not is_staff:
                    raise HTTPException(403, "Moderator access required")

        # Admin — require admin role or site staff
        if intent == ForumIntent.ADMIN:
            is_admin = member is not None and member.role == "admin"
            if not is_admin:
                is_staff = request.user.is_staff if request.user is not None else False
                if not is_staff:
                    raise HTTPException(
                        403, "Only forum admins can perform this action"
                    )

        return ForumAccess(forum, is_member, is_mod, member)

    return resolver


# Pre-build resolvers for each intent
_resolve_forum_read = _make_forum_resolver(ForumIntent.READ)
_resolve_forum_write_post = _make_forum_resolver(ForumIntent.WRITE_POST)
_resolve_forum_write_comment = _make_forum_resolver(ForumIntent.WRITE_COMMENT)
_resolve_forum_moderate = _make_forum_resolver(ForumIntent.MODERATE)
_resolve_forum_admin = _make_forum_resolver(ForumIntent.ADMIN)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


_PASS = 0
_FAIL = 0


def check(condition: bool, msg: str) -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {msg}")


# ── Test: Forum read access ─────────────────────────────────────────────────


def test_forum_read_public():
    """Authenticated user reads public forum."""
    print("test_forum_read_public")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
    )
    async def forum_home(request, forum_name: str):
        access = request.guard.forum_access
        return {"forum": access.forum.name, "member": access.is_member}

    request = MockRequest(
        user=SessionUser({"id": 1, "username": "alice"}),
        path_params={"forum_name": "python"},
    )
    result = _run(forum_home(request, forum_name="python"))
    check(result["forum"] == "python", "forum resolved")
    check(result["member"] is True, "is member")


def test_forum_read_anonymous():
    """Anonymous user gets redirected."""
    print("test_forum_read_anonymous")
    from hyperdjango.response import Response

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
    )
    async def forum_home(request, forum_name: str):
        return Response.json({"ok": True})

    request = MockRequest(
        user=None,
        path="/f/python/",
        path_params={"forum_name": "python"},
    )
    result = _run(forum_home(request, forum_name="python"))
    check(result.status == 302, "redirected")
    check("/login" in result.headers.get("location", ""), "to login")


def test_forum_read_not_found():
    """Reading nonexistent forum returns 404."""
    print("test_forum_read_not_found")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
    )
    async def forum_home(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "nonexistent"},
    )
    try:
        _run(forum_home(request, forum_name="nonexistent"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 404, f"404, got {e.status_code}")


def test_forum_read_private_non_member():
    """Non-member can't read private forum."""
    print("test_forum_read_private_non_member")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
    )
    async def forum_home(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 99}),  # Not a member of private forum
        path_params={"forum_name": "private"},
    )
    try:
        _run(forum_home(request, forum_name="private"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 403, f"403, got {e.status_code}")
        check("private" in str(e.detail).lower(), "message mentions private")


def test_forum_read_private_member():
    """Member can read private forum."""
    print("test_forum_read_private_member")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
    )
    async def forum_home(request, forum_name: str):
        return {"forum": request.guard.forum_access.forum.name}

    request = MockRequest(
        user=SessionUser({"id": 1}),  # Member of private forum (id=4)
        path_params={"forum_name": "private"},
    )
    result = _run(forum_home(request, forum_name="private"))
    check(result["forum"] == "private", "member can read")


def test_forum_read_hidden_non_member():
    """Hidden forum returns 404 for non-members (not 403)."""
    print("test_forum_read_hidden_non_member")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
    )
    async def forum_home(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 99}),
        path_params={"forum_name": "hidden"},
    )
    try:
        _run(forum_home(request, forum_name="hidden"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 404, f"hidden=404, got {e.status_code}")


# ── Test: Forum write access ────────────────────────────────────────────────


def test_forum_write_post():
    """Authenticated member can write to public forum."""
    print("test_forum_write_post")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.not_banned(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_write_post, from_path="forum_name"
        ),
    )
    async def forum_submit(request, forum_name: str):
        return {"forum": request.guard.forum_access.forum.name}

    request = MockRequest(
        user=SessionUser({"id": 1, "is_banned": False}),
        path_params={"forum_name": "python"},
    )
    result = _run(forum_submit(request, forum_name="python"))
    check(result["forum"] == "python", "can write")


def test_forum_write_archived():
    """Can't write to archived forum."""
    print("test_forum_write_archived")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_write_post, from_path="forum_name"
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
        check("archived" in str(e.detail).lower(), "message mentions archived")


def test_forum_write_locked():
    """Can't write to locked forum."""
    print("test_forum_write_locked")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource(
            "forum_access", resolver=_resolve_forum_write_post, from_path="forum_name"
        ),
    )
    async def forum_submit(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "locked"},
    )
    try:
        _run(forum_submit(request, forum_name="locked"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 403, f"403, got {e.status_code}")
        check(
            "locked" in str(e.detail).lower()
            or "not accepting" in str(e.detail).lower(),
            "message mentions locked",
        )


def test_forum_write_banned_short_circuits():
    """Banned user gets 403 before resolver runs."""
    print("test_forum_write_banned_short_circuits")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.not_banned(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_write_post, from_path="forum_name"
        ),
    )
    async def forum_submit(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 1, "is_banned": True}),
        path_params={"forum_name": "python"},
    )
    try:
        _run(forum_submit(request, forum_name="python"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 403, f"403, got {e.status_code}")
        check("suspended" in str(e.detail).lower(), "message mentions suspension")


# ── Test: Forum moderate access ──────────────────────────────────────────────


def test_forum_moderate_mod():
    """Moderator can moderate."""
    print("test_forum_moderate_mod")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_moderate, from_path="forum_name"
        ),
    )
    async def pin_post(request, forum_name: str):
        return {"is_mod": request.guard.forum_access.is_mod}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "private"},  # user 1 is moderator of private
    )
    result = _run(pin_post(request, forum_name="private"))
    check(result["is_mod"] is True, "is mod")


def test_forum_moderate_non_mod():
    """Non-mod, non-staff can't moderate."""
    print("test_forum_moderate_non_mod")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_moderate, from_path="forum_name"
        ),
    )
    async def pin_post(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 1, "is_staff": False}),  # member of python, but not mod
        path_params={"forum_name": "python"},
    )
    try:
        _run(pin_post(request, forum_name="python"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 403, f"403, got {e.status_code}")


def test_forum_moderate_staff_bypass():
    """Staff can moderate any forum."""
    print("test_forum_moderate_staff_bypass")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_moderate, from_path="forum_name"
        ),
    )
    async def pin_post(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 99, "groups": ["staff"]}),
        path_params={"forum_name": "python"},
    )
    result = _run(pin_post(request, forum_name="python"))
    check(result["ok"] is True, "staff can moderate")


# ── Test: Forum admin access ────────────────────────────────────────────────


def test_forum_admin_admin():
    """Forum admin can administrate."""
    print("test_forum_admin_admin")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_admin, from_path="forum_name"
        ),
    )
    async def forum_edit(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 2}),  # admin of python
        path_params={"forum_name": "python"},
    )
    result = _run(forum_edit(request, forum_name="python"))
    check(result["ok"] is True, "admin can edit")


def test_forum_admin_non_admin():
    """Regular member can't administrate."""
    print("test_forum_admin_non_admin")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_admin, from_path="forum_name"
        ),
    )
    async def forum_edit(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 1, "is_staff": False}),  # member, not admin
        path_params={"forum_name": "python"},
    )
    try:
        _run(forum_edit(request, forum_name="python"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 403, f"403, got {e.status_code}")


def test_forum_admin_staff_bypass():
    """Site staff can administrate any forum."""
    print("test_forum_admin_staff_bypass")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_admin, from_path="forum_name"
        ),
    )
    async def forum_edit(request, forum_name: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 99, "groups": ["staff"]}),
        path_params={"forum_name": "python"},
    )
    result = _run(forum_edit(request, forum_name="python"))
    check(result["ok"] is True, "staff can admin")


# ── Test: Composed chains matching real HyperNews routes ─────────────────────


def test_real_pattern_forum_submit():
    """Matches HyperNews POST /f/{name}/submit pattern: auth + not_banned + write_post."""
    print("test_real_pattern_forum_submit")

    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.not_banned(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_write_post, from_path="forum_name"
        ),
    )
    async def forum_submit(request, forum_name: str):
        access = request.guard.forum_access
        return {
            "forum_id": access.forum.id,
            "is_member": access.is_member,
        }

    request = MockRequest(
        user=SessionUser({"id": 1, "is_banned": False}),
        path_params={"forum_name": "python"},
    )
    result = _run(forum_submit(request, forum_name="python"))
    check(result["forum_id"] == 1, "forum resolved")
    check(result["is_member"] is True, "member status")


def test_real_pattern_forum_edit():
    """Matches HyperNews POST /f/{name}/edit pattern: auth + admin."""
    print("test_real_pattern_forum_edit")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_admin, from_path="forum_name"
        ),
    )
    async def forum_edit(request, forum_name: str):
        return {"forum_name": request.guard.forum_access.forum.name}

    request = MockRequest(
        user=SessionUser({"id": 2}),
        path_params={"forum_name": "python"},
    )
    result = _run(forum_edit(request, forum_name="python"))
    check(result["forum_name"] == "python", "admin edit works")


def test_real_pattern_pin_post():
    """Matches HyperNews POST /f/{name}/pin pattern: auth + moderate."""
    print("test_real_pattern_pin_post")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_moderate, from_path="forum_name"
        ),
    )
    async def pin_post(request, forum_name: str):
        return {"is_mod": request.guard.forum_access.is_mod}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "private"},  # user 1 is moderator
    )
    result = _run(pin_post(request, forum_name="private"))
    check(result["is_mod"] is True, "mod can pin")


# ── Test: OR composition for staff-or-mod ────────────────────────────────────


def test_any_of_staff_or_mod():
    """staff OR mod can moderate — using any_of composition."""
    print("test_any_of_staff_or_mod")

    async def check_is_forum_mod(request, ctx):
        access = ctx.resources.get("forum_access")
        if access and access.is_mod:
            return None
        return GuardDenial(DenyReason.FORBIDDEN, "Moderator access required")

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
        Require.any_of(
            Require.staff(),
            Require.check("is_forum_mod", fn=check_is_forum_mod),
        ),
    )
    async def moderate_action(request, forum_name: str):
        return {"ok": True}

    # Test: mod passes (not staff)
    request = MockRequest(
        user=SessionUser({"id": 1, "is_staff": False}),
        path_params={"forum_name": "private"},  # user 1 is mod
    )
    result = _run(moderate_action(request, forum_name="private"))
    check(result["ok"] is True, "mod passes via any_of")

    # Test: staff passes (not mod)
    request2 = MockRequest(
        user=SessionUser({"id": 99, "groups": ["staff"]}),
        path_params={"forum_name": "python"},
    )
    result2 = _run(moderate_action(request2, forum_name="python"))
    check(result2["ok"] is True, "staff passes via any_of")

    # Test: neither fails
    request3 = MockRequest(
        user=SessionUser({"id": 1, "is_staff": False}),
        path_params={"forum_name": "python"},  # user 1 is member, not mod
    )
    try:
        _run(moderate_action(request3, forum_name="python"))
        check(False, "should fail")
    except HTTPException as e:
        check(e.status_code == 403, f"403, got {e.status_code}")


# ── Test: Guard context chaining ─────────────────────────────────────────────


def test_context_chaining_forum_then_post():
    """Post resolver can access previously-resolved forum."""
    print("test_context_chaining_forum_then_post")

    async def resolve_post(request, ctx, pid):
        forum_access = ctx.resources.get("forum_access")
        post = _POSTS.get(int(pid))
        if not post:
            return None
        if forum_access and post.forum_id != forum_access.forum.id:
            return None  # Post not in this forum
        return PostAccess(post, forum_access.forum if forum_access else None)

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
        Require.resource("post_access", resolver=resolve_post, from_path="pid"),
    )
    async def post_detail(request, forum_name: str, pid: str):
        return {
            "forum": request.guard.forum_access.forum.name,
            "post_title": request.guard.post_access.post.title,
        }

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "python", "pid": "42"},
    )
    result = _run(post_detail(request, forum_name="python", pid="42"))
    check(result["forum"] == "python", "forum resolved")
    check(result["post_title"] == "Test", "post resolved")


def test_context_chaining_post_wrong_forum():
    """Post in wrong forum returns 404."""
    print("test_context_chaining_post_wrong_forum")

    async def resolve_post(request, ctx, pid):
        forum_access = ctx.resources.get("forum_access")
        post = _POSTS.get(int(pid))
        if not post:
            return None
        if forum_access and post.forum_id != forum_access.forum.id:
            return None
        return PostAccess(post, forum_access.forum if forum_access else None)

    @guard(
        Require.authenticated(),
        Require.resource(
            "forum_access", resolver=_resolve_forum_read, from_path="forum_name"
        ),
        Require.resource("post_access", resolver=resolve_post, from_path="pid"),
    )
    async def post_detail(request, forum_name: str, pid: str):
        return {"ok": True}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={
            "forum_name": "python",
            "pid": "44",
        },  # Post 44 is in archived forum (id=2)
    )
    try:
        _run(post_detail(request, forum_name="python", pid="44"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 404, f"404, got {e.status_code}")


# ── Run all ──────────────────────────────────────────────────────────────────


def main():
    tests = [
        test_forum_read_public,
        test_forum_read_anonymous,
        test_forum_read_not_found,
        test_forum_read_private_non_member,
        test_forum_read_private_member,
        test_forum_read_hidden_non_member,
        test_forum_write_post,
        test_forum_write_archived,
        test_forum_write_locked,
        test_forum_write_banned_short_circuits,
        test_forum_moderate_mod,
        test_forum_moderate_non_mod,
        test_forum_moderate_staff_bypass,
        test_forum_admin_admin,
        test_forum_admin_non_admin,
        test_forum_admin_staff_bypass,
        test_real_pattern_forum_submit,
        test_real_pattern_forum_edit,
        test_real_pattern_pin_post,
        test_any_of_staff_or_mod,
        test_context_chaining_forum_then_post,
        test_context_chaining_post_wrong_forum,
    ]

    for test in tests:
        test()

    total = _PASS + _FAIL
    print(f"\n{'=' * 60}")
    print(f"HyperGuard Integration: {_PASS}/{total} passed, {_FAIL} failed")
    if _FAIL:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
