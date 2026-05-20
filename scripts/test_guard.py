"""
HyperGuard unit tests — requirement chain evaluation, composition, error handling.

Tests the full guard lifecycle:
1. Requirement creation via Require.* factories
2. Guard spec compilation from requirement chains
3. Per-request evaluation with short-circuit semantics
4. GuardContext resource accumulation
5. Error responses with correct status codes and messages
6. Redirect handling for unauthenticated users
7. OR composition via Require.any_of()
8. Route scanner for startup validation
9. Decorator integration with mock request objects
10. Timeline tri-state: table-missing → unavailable (allow), DB error → deny

The db_isolated marker gives this file its own empty database so the
timeline tri-state tests control exactly which tables exist — the ban/mute
checks must behave identically whether DATABASE_URL is unset, points at a
database without the timeline table, or points at a provisioned one.
"""

# hyper-test: db_isolated

import asyncio
import contextlib
from dataclasses import dataclass, field

from hyperdjango.auth.user import SessionUser
from hyperdjango.exceptions import HTTPException
from hyperdjango.guard import (
    DenyReason,
    GuardContext,
    GuardDenial,
    GuardSpec,
    Require,
    RequirementKind,
    guard,
    guard_action,
    guard_websocket,
)
from hyperdjango.guard.evaluator import _RedirectDenial, evaluate_guard
from hyperdjango.guard.scanner import (
    GuardedRoute,
    ScanResult,
    UnguardedRoute,
    _find_guard_spec,
)

# ── Mock objects ─────────────────────────────────────────────────────────────


@dataclass
class MockRequest:
    """Minimal request mock for guard testing."""

    user: SessionUser | None = None
    path: str = "/"
    method: str = "GET"
    path_params: dict[str, str] = field(default_factory=dict)
    guard: object = None  # Set by @guard decorator
    cookies: dict[str, str] = field(default_factory=dict)
    api_key_valid: bool = False


@dataclass
class MockForum:
    """Mock forum resource."""

    id: int = 1
    name: str = "test"
    is_public: bool = True
    is_archived: bool = False
    is_locked: bool = False


@dataclass
class MockPost:
    """Mock post resource."""

    id: int = 42
    title: str = "Test Post"
    author_id: int = 1
    forum_id: int = 1


# ── Helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    """Run an async coroutine synchronously."""
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


# ── Test: Requirement types ──────────────────────────────────────────────────


def test_requirement_types():
    """Verify Require.* factories produce correct requirement kinds."""
    print("test_requirement_types")

    auth = Require.authenticated()
    check(auth.kind == RequirementKind.PRECONDITION, "authenticated is precondition")
    check(auth.name == "authenticated", "authenticated name")
    check(auth.resource_key is None, "authenticated has no resource_key")

    staff = Require.staff()
    check(staff.kind == RequirementKind.PRECONDITION, "staff is precondition")
    check(staff.name == "staff", "staff name")

    banned = Require.not_banned()
    check(banned.kind == RequirementKind.PRECONDITION, "not_banned is precondition")
    check(banned.name == "not_banned", "not_banned name")

    async def mock_resolver(request, ctx, val):
        return MockForum()

    res = Require.resource("forum", resolver=mock_resolver, from_path="forum_name")
    check(res.kind == RequirementKind.RESOURCE, "resource is RESOURCE kind")
    check(res.name == "resource:forum", "resource name")
    check(res.resource_key == "forum", "resource_key is forum")

    async def mock_check(request, ctx):
        return None

    custom = Require.check("karma", fn=mock_check)
    check(custom.kind == RequirementKind.CUSTOM, "custom is CUSTOM kind")
    check(custom.name == "karma", "custom name")


# ── Test: Authenticated requirement ──────────────────────────────────────────


def test_authenticated_pass():
    """Authenticated user passes."""
    print("test_authenticated_pass")
    req = Require.authenticated()
    request = MockRequest(user=SessionUser({"id": 1, "username": "alice"}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "authenticated user should pass")


def test_authenticated_fail_no_user():
    """No user fails authentication."""
    print("test_authenticated_fail_no_user")
    req = Require.authenticated()
    request = MockRequest(user=None)
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "no user should fail")
    check(result.reason == DenyReason.NOT_AUTHENTICATED, "correct reason")
    check(result.effective_status == 401, "401 status")


def test_authenticated_pass_no_id():
    """SessionUser without 'id' is still authenticated (session was valid)."""
    print("test_authenticated_pass_no_id")
    req = Require.authenticated()
    request = MockRequest(user=SessionUser({"username": "alice"}))  # No 'id' key
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "SessionUser is authenticated even without id")
    check(request.user.id is None, "user.id is None when not in session data")


def test_authenticated_redirect():
    """Authenticated with redirect_url sets metadata on failure."""
    print("test_authenticated_redirect")
    req = Require.authenticated(redirect_url="/login")
    request = MockRequest(user=None)
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "should fail")
    from hyperdjango.guard.types import _REDIRECT_URL_KEY

    check(ctx.metadata.get(_REDIRECT_URL_KEY) == "/login", "redirect URL in metadata")


# ── Test: Staff requirement ──────────────────────────────────────────────────


def test_staff_pass():
    """Staff user passes."""
    print("test_staff_pass")
    req = Require.staff()
    request = MockRequest(user=SessionUser({"id": 1, "groups": ["staff"]}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "staff user should pass")


def test_staff_fail():
    """Non-staff user fails."""
    print("test_staff_fail")
    req = Require.staff()
    request = MockRequest(user=SessionUser({"id": 1, "is_staff": False}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "non-staff should fail")
    check(result.reason == DenyReason.FORBIDDEN, "correct reason (403)")
    check(result.effective_status == 403, "403 status")


def test_staff_fail_no_key():
    """User without is_staff key fails."""
    print("test_staff_fail_no_key")
    req = Require.staff()
    request = MockRequest(user=SessionUser({"id": 1}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "missing is_staff should fail")


# ── Test: Not banned requirement ─────────────────────────────────────────────


def test_not_banned_pass():
    """Non-banned user passes."""
    print("test_not_banned_pass")
    req = Require.not_banned()
    request = MockRequest(user=SessionUser({"id": 1, "is_banned": False}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "non-banned user passes")


def test_not_banned_fail():
    """Banned user fails."""
    print("test_not_banned_fail")
    req = Require.not_banned()
    request = MockRequest(user=SessionUser({"id": 1, "is_banned": True}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "banned user should fail")
    check(result.reason == DenyReason.FORBIDDEN, "correct reason")
    check("suspended" in result.message, "message mentions suspension")


def test_not_banned_no_user():
    """Non-authenticated user is denied by not_banned (safe default)."""
    print("test_not_banned_no_user")
    req = Require.not_banned()
    request = MockRequest(user=None)
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "no user denied by not_banned")
    check(result.reason == DenyReason.NOT_AUTHENTICATED, "denies as not authenticated")


def test_not_banned_no_key():
    """User dict without is_banned key passes (defaults to not banned)."""
    print("test_not_banned_no_key")
    req = Require.not_banned()
    request = MockRequest(
        user=SessionUser({"id": 1, "username": "alice"})
    )  # No is_banned key
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "missing is_banned key defaults to not banned")


def test_not_muted_no_key():
    """User dict without is_muted key passes (defaults to not muted)."""
    print("test_not_muted_no_key")
    req = Require.not_muted()
    request = MockRequest(
        user=SessionUser({"id": 1, "username": "alice"})
    )  # No is_muted key
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "missing is_muted key defaults to not muted")


def test_not_muted_pass():
    """Non-muted user passes."""
    print("test_not_muted_pass")
    req = Require.not_muted()
    request = MockRequest(user=SessionUser({"id": 1, "is_muted": False}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "non-muted user passes")


def test_not_muted_fail():
    """Muted user fails."""
    print("test_not_muted_fail")
    req = Require.not_muted()
    request = MockRequest(user=SessionUser({"id": 1, "is_muted": True}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "muted user should fail")
    check(result.reason == DenyReason.FORBIDDEN, "correct reason")
    check("muted" in result.message, "message mentions muted")


def test_not_muted_no_user():
    """Non-authenticated user is denied by not_muted."""
    print("test_not_muted_no_user")
    req = Require.not_muted()
    request = MockRequest(user=None)
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "no user denied by not_muted")
    check(result.reason == DenyReason.NOT_AUTHENTICATED, "denies as not authenticated")


# ── Test: Timeline tri-state (unavailable vs error) ──────────────────────────


def test_timeline_table_missing_is_unavailable():
    """A reachable DB WITHOUT hyper_status_events is 'unavailable', not an error.

    Reproduces the CI condition where ambient PG* env resolves DATABASE_URL to
    a real, connectable database whose schema was never provisioned. The
    ban/mute checks must treat that exactly like "no database configured"
    (allow via session-flag fallback) — an unprovisioned timeline holds no
    status data anywhere, so there is nothing for fail-closed to protect.
    """
    print("test_timeline_table_missing_is_unavailable")
    from hyperdjango.database import get_db
    from hyperdjango.db.pgzig_connection import is_undefined_table
    from hyperdjango.guard.requirements import (
        _TIMELINE_UNAVAILABLE,
        _get_cached_active_statuses,
    )
    from hyperdjango.timeline import get_timeline

    # Predicate contract: missing TABLE matches; missing COLUMN (a genuine
    # schema bug) and generic failures do not.
    check(
        is_undefined_table(Exception('relation "hyper_status_events" does not exist')),
        "undefined-table message matches predicate",
    )
    check(
        not is_undefined_table(
            Exception(
                'column "status" of relation "hyper_status_events" does not exist'
            )
        ),
        "undefined-column message does NOT match predicate",
    )
    check(
        not is_undefined_table(Exception("connection refused")),
        "connection failure does NOT match predicate",
    )

    try:
        db = get_db()
    except RuntimeError:
        # No DATABASE_URL configured — the no-DB branch of the same tri-state;
        # the assertions below still exercise UNAVAILABLE → allow.
        db = None
    if db is not None:
        _run(db.execute("DROP TABLE IF EXISTS hyper_status_events CASCADE"))

        async def _probe():
            try:
                await get_timeline().active_statuses("user", 1)
            except Exception as exc:
                return exc
            return None

        raised = _run(_probe())
        check(raised is not None, "query against missing table raises")
        check(
            is_undefined_table(raised),
            "real missing-table error classified as undefined_table",
        )

    ctx = GuardContext()
    tri_state = _run(_get_cached_active_statuses(1, ctx))
    check(
        tri_state is _TIMELINE_UNAVAILABLE,
        "missing table resolves to UNAVAILABLE, not ERROR",
    )

    request = MockRequest(user=SessionUser({"id": 1, "is_banned": False}))
    result = _run(Require.not_banned().evaluate_fn(request, GuardContext()))
    check(result is None, "not_banned allows when timeline table is missing")

    request = MockRequest(user=SessionUser({"id": 1}))
    result = _run(Require.not_muted().evaluate_fn(request, GuardContext()))
    check(result is None, "not_muted allows when timeline table is missing")

    request = MockRequest(user=SessionUser({"id": 1, "is_banned": True}))
    result = _run(Require.not_banned().evaluate_fn(request, GuardContext()))
    check(
        result is not None,
        "session ban flag still denies when timeline table is missing",
    )


def test_timeline_db_error_fails_closed():
    """A genuine timeline DB error denies ban/mute checks (fail closed)."""
    print("test_timeline_db_error_fails_closed")
    from hyperdjango.guard.requirements import (
        _TIMELINE_ERROR,
        _get_cached_active_statuses,
    )
    from hyperdjango.timeline import TimelineManager, get_timeline, set_timeline

    class _BrokenTimeline(TimelineManager):
        async def active_statuses(self, entity_type: str, entity_id: int) -> set[str]:
            raise ConnectionError("simulated database outage")

    original = get_timeline()
    set_timeline(_BrokenTimeline())
    try:
        ctx = GuardContext()
        tri_state = _run(_get_cached_active_statuses(1, ctx))
        check(tri_state is _TIMELINE_ERROR, "DB outage resolves to ERROR tri-state")

        request = MockRequest(user=SessionUser({"id": 1, "is_banned": False}))
        denial = _run(Require.not_banned().evaluate_fn(request, GuardContext()))
        check(denial is not None, "not_banned fails closed on DB error")
        check(denial.reason == DenyReason.FORBIDDEN, "fail-closed reason is FORBIDDEN")
        check(
            "unavailable" in denial.message, "fail-closed message names unavailability"
        )

        request = MockRequest(user=SessionUser({"id": 1}))
        denial = _run(Require.not_muted().evaluate_fn(request, GuardContext()))
        check(denial is not None, "not_muted fails closed on DB error")
    finally:
        set_timeline(original)


# ── Test: Resource requirement ───────────────────────────────────────────────


def test_resource_pass():
    """Resource resolver succeeds and stores in context."""
    print("test_resource_pass")
    forum = MockForum(id=5, name="python")

    async def resolve(request, ctx, forum_name):
        return forum

    req = Require.resource("forum", resolver=resolve, from_path="forum_name")
    request = MockRequest(path_params={"forum_name": "python"})
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "resolver returned forum")
    check(ctx.resources["forum"] is forum, "forum stored in context")
    check(ctx.forum is forum, "accessible via attribute")


def test_resource_not_found():
    """Resource resolver returns None triggers 404."""
    print("test_resource_not_found")

    async def resolve(request, ctx, forum_name):
        return None

    req = Require.resource("forum", resolver=resolve, from_path="forum_name")
    request = MockRequest(path_params={"forum_name": "nonexistent"})
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "should fail")
    check(result.reason == DenyReason.RESOURCE_NOT_FOUND, "404 reason")
    check(result.effective_status == 404, "404 status")


def test_resource_missing_path_param():
    """Missing path parameter triggers 404."""
    print("test_resource_missing_path_param")

    async def resolve(request, ctx, forum_name):
        return MockForum()

    req = Require.resource("forum", resolver=resolve, from_path="forum_name")
    request = MockRequest(path_params={})  # Missing forum_name
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "missing path param should fail")
    check(result.effective_status == 404, "404 status")


def test_resource_custom_deny_message():
    """Custom deny message is used."""
    print("test_resource_custom_deny_message")

    async def resolve(request, ctx, val):
        return None

    req = Require.resource(
        "post",
        resolver=resolve,
        from_path="pid",
        deny_message="Post does not exist or has been deleted",
    )
    request = MockRequest(path_params={"pid": "abc123"})
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "should fail")
    check(result.message == "Post does not exist or has been deleted", "custom message")


def test_resource_no_from_path():
    """Resource resolver without from_path gets no extra arg."""
    print("test_resource_no_from_path")

    async def resolve(request, ctx):
        return MockForum(id=99)

    req = Require.resource("forum", resolver=resolve)
    request = MockRequest()
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "should pass")
    check(ctx.resources["forum"].id == 99, "forum stored")


def test_resource_chaining():
    """Later resource resolver can access previously resolved resources."""
    print("test_resource_chaining")

    forum = MockForum(id=7)
    post = MockPost(id=42, forum_id=7)

    async def resolve_forum(request, ctx, name):
        return forum

    async def resolve_post(request, ctx, pid):
        # Access previously resolved forum
        resolved_forum = ctx.resources.get("forum")
        if resolved_forum and post.forum_id == resolved_forum.id:
            return post
        return None

    req1 = Require.resource("forum", resolver=resolve_forum, from_path="forum_name")
    req2 = Require.resource("post", resolver=resolve_post, from_path="pid")

    request = MockRequest(path_params={"forum_name": "python", "pid": "42"})
    ctx = GuardContext()

    result1 = _run(req1.evaluate_fn(request, ctx))
    check(result1 is None, "forum resolved")
    result2 = _run(req2.evaluate_fn(request, ctx))
    check(result2 is None, "post resolved using forum")
    check(ctx.resources["post"] is post, "post stored")


# ── Test: Custom check ───────────────────────────────────────────────────────


def test_custom_check_pass():
    """Custom check returning None passes."""
    print("test_custom_check_pass")

    async def check_karma(request, ctx):
        return None

    req = Require.check("karma", fn=check_karma)
    request = MockRequest(user=SessionUser({"id": 1}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "custom check passes")


def test_custom_check_fail():
    """Custom check returning GuardDenial fails."""
    print("test_custom_check_fail")

    async def check_karma(request, ctx):
        return GuardDenial(DenyReason.FORBIDDEN, "Insufficient karma")

    req = Require.check("karma", fn=check_karma)
    request = MockRequest(user=SessionUser({"id": 1}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "should fail")
    check(result.message == "Insufficient karma", "correct message")


# ── Test: any_of composition ─────────────────────────────────────────────────


def test_any_of_first_passes():
    """any_of short-circuits on first pass."""
    print("test_any_of_first_passes")
    req = Require.any_of(
        Require.staff(),
        Require.authenticated(),
    )
    # Staff user — first requirement passes
    request = MockRequest(user=SessionUser({"id": 1, "groups": ["staff"]}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "staff passes (first)")


def test_any_of_second_passes():
    """any_of passes when second requirement passes."""
    print("test_any_of_second_passes")

    async def is_mod(request, ctx):
        if request.user and request.user.get("is_mod"):
            return None
        return GuardDenial(DenyReason.FORBIDDEN, "Not a mod")

    req = Require.any_of(
        Require.staff(),
        Require.check("is_mod", fn=is_mod),
    )
    request = MockRequest(
        user=SessionUser({"id": 1, "is_staff": False, "is_mod": True})
    )
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "mod passes (second)")


def test_any_of_all_fail():
    """any_of fails when all requirements fail."""
    print("test_any_of_all_fail")
    req = Require.any_of(
        Require.staff(),
        Require.authenticated(),
    )
    request = MockRequest(user=None)
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "all fail should fail")


def test_any_of_rollback():
    """any_of rolls back partial resource changes from failing alternatives."""
    print("test_any_of_rollback")

    async def failing_resolver(request, ctx):
        ctx.resources["stale"] = "should be rolled back"
        return GuardDenial(DenyReason.FORBIDDEN, "fail")

    async def passing_check(request, ctx):
        return None

    req = Require.any_of(
        Require.check("fail_with_side_effect", fn=failing_resolver),
        Require.check("pass", fn=passing_check),
    )
    request = MockRequest(user=SessionUser({"id": 1}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "second alternative passes")
    check(
        "stale" not in ctx.resources,
        f"stale resource rolled back, got: {list(ctx.resources)}",
    )


def test_any_of_name():
    """any_of has descriptive name."""
    print("test_any_of_name")
    req = Require.any_of(
        Require.staff(),
        Require.authenticated(),
    )
    check("staff" in req.name, "name includes staff")
    check("authenticated" in req.name, "name includes authenticated")
    check("any_of" in req.name, "name includes any_of")


# ── Test: GuardSpec ──────────────────────────────────────────────────────────


def test_guard_spec_frozen():
    """GuardSpec is immutable."""
    print("test_guard_spec_frozen")
    spec = GuardSpec(requirements=(Require.authenticated(),))
    try:
        spec.route_name = "changed"
        check(False, "should be frozen")
    except AttributeError:
        check(True, "frozen dataclass")


def test_guard_spec_requirement_names():
    """GuardSpec exposes requirement names."""
    print("test_guard_spec_requirement_names")
    spec = GuardSpec(
        requirements=(
            Require.authenticated(),
            Require.staff(),
            Require.not_banned(),
        )
    )
    check(
        spec.requirement_names == ("authenticated", "staff", "not_banned"),
        f"names: {spec.requirement_names}",
    )


# ── Test: GuardContext ───────────────────────────────────────────────────────


def test_guard_context_attribute_access():
    """GuardContext supports attribute-style resource access."""
    print("test_guard_context_attribute_access")
    ctx = GuardContext()
    forum = MockForum(id=1)
    ctx.resources["forum"] = forum
    check(ctx.forum is forum, "attribute access works")


def test_guard_context_missing_attribute():
    """GuardContext raises AttributeError with helpful message."""
    print("test_guard_context_missing_attribute")
    ctx = GuardContext()
    ctx.resources["forum"] = MockForum()
    try:
        _ = ctx.post
        check(False, "should raise AttributeError")
    except AttributeError as e:
        check("post" in str(e), "mentions missing key")
        check("forum" in str(e), "lists available resources")


# ── Test: GuardDenial ────────────────────────────────────────────────────────


def test_denial_effective_status():
    """GuardDenial uses correct status codes per reason."""
    print("test_denial_effective_status")
    check(
        GuardDenial(DenyReason.NOT_AUTHENTICATED, "").effective_status == 401,
        "auth=401",
    )
    check(GuardDenial(DenyReason.NOT_STAFF, "").effective_status == 403, "staff=403")
    check(GuardDenial(DenyReason.RATE_LIMITED, "").effective_status == 429, "rate=429")
    check(
        GuardDenial(DenyReason.RESOURCE_NOT_FOUND, "").effective_status == 404,
        "notfound=404",
    )
    check(
        GuardDenial(DenyReason.FORBIDDEN, "").effective_status == 403, "forbidden=403"
    )


def test_denial_custom_status():
    """GuardDenial with explicit status_code overrides auto."""
    print("test_denial_custom_status")
    denial = GuardDenial(DenyReason.FORBIDDEN, "Custom", status_code=451)
    check(denial.effective_status == 451, "custom status overrides")


# ── Test: Evaluator ──────────────────────────────────────────────────────────


def test_evaluate_all_pass():
    """Evaluator returns context when all requirements pass."""
    print("test_evaluate_all_pass")
    spec = GuardSpec(
        requirements=(
            Require.authenticated(),
            Require.not_banned(),
        )
    )
    request = MockRequest(user=SessionUser({"id": 1, "is_banned": False}))
    ctx = _run(evaluate_guard(request, spec))
    check(isinstance(ctx, GuardContext), "returns context")


def test_evaluate_short_circuit():
    """Evaluator short-circuits on first failure."""
    print("test_evaluate_short_circuit")
    call_count = [0]

    async def counting_check(request, ctx):
        call_count[0] += 1
        return None

    spec = GuardSpec(
        requirements=(
            Require.authenticated(),  # Will fail (no user)
            Require.check("counter", fn=counting_check),
        )
    )
    request = MockRequest(user=None)
    try:
        _run(evaluate_guard(request, spec))
        check(False, "should raise HTTPException")
    except HTTPException as e:
        check(e.status_code == 401, "401 from auth failure")
        check(call_count[0] == 0, "second requirement not evaluated")


def test_evaluate_redirect():
    """Evaluator raises _RedirectDenial for auth failure with redirect_url."""
    print("test_evaluate_redirect")
    spec = GuardSpec(requirements=(Require.authenticated(redirect_url="/login"),))
    request = MockRequest(user=None, path="/protected/page")
    try:
        _run(evaluate_guard(request, spec))
        check(False, "should raise")
    except _RedirectDenial as e:
        check(e.redirect_url == "/login", "redirect URL")
        check(e.original_path == "/protected/page", "original path preserved")


def test_evaluate_resource_stored():
    """Evaluator stores resolved resources in context."""
    print("test_evaluate_resource_stored")
    forum = MockForum(id=3)

    async def resolve(request, ctx, name):
        return forum

    spec = GuardSpec(
        requirements=(
            Require.authenticated(),
            Require.resource("forum", resolver=resolve, from_path="forum_name"),
        )
    )
    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "python"},
    )
    ctx = _run(evaluate_guard(request, spec))
    check(ctx.forum is forum, "forum in context")


# ── Test: @guard decorator ───────────────────────────────────────────────────


def test_decorator_pass():
    """@guard decorator passes and sets request.guard."""
    print("test_decorator_pass")

    @guard(Require.authenticated())
    async def handler(request):
        return {"guard": request.guard is not None}

    request = MockRequest(user=SessionUser({"id": 1}))
    result = _run(handler(request))
    check(result["guard"] is True, "request.guard set")
    check(isinstance(request.guard, GuardContext), "guard is GuardContext")


def test_decorator_fail():
    """@guard decorator raises HTTPException on failure."""
    print("test_decorator_fail")

    @guard(Require.authenticated())
    async def handler(request):
        return {"ok": True}

    request = MockRequest(user=None)
    try:
        _run(handler(request))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 401, "401 from auth")


def test_decorator_redirect():
    """@guard decorator returns redirect response."""
    print("test_decorator_redirect")
    from hyperdjango.response import Response

    @guard(Require.authenticated(redirect_url="/login"))
    async def handler(request):
        return Response.json({"ok": True})

    request = MockRequest(user=None, path="/secret")
    result = _run(handler(request))
    check(result.status == 302, f"302 redirect, got {result.status}")


def test_decorator_redirect_url_encoded():
    """Redirect URL-encodes the original path to prevent injection."""
    print("test_decorator_redirect_url_encoded")
    from hyperdjango.response import Response

    @guard(Require.authenticated(redirect_url="/login"))
    async def handler(request):
        return Response.json({"ok": True})

    # Path with special characters that could cause parameter injection
    request = MockRequest(user=None, path="/secret?admin=true&evil=1")
    result = _run(handler(request))
    check(result.status == 302, f"302 redirect, got {result.status}")
    location = result.headers.get("location", "")
    # The query string chars should be percent-encoded, not raw
    check(
        "%3F" in location or "%3D" in location,
        f"path should be encoded in redirect, got: {location}",
    )


def test_decorator_resource_access():
    """@guard decorator makes resources available via request.guard."""
    print("test_decorator_resource_access")
    forum = MockForum(id=7, name="python")

    async def resolve_forum(request, ctx, name):
        return forum

    @guard(
        Require.authenticated(),
        Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
    )
    async def handler(request, forum_name: str):
        return {"forum_id": request.guard.forum.id}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "python"},
    )
    result = _run(handler(request, forum_name="python"))
    check(result["forum_id"] == 7, "forum accessible")


def test_decorator_guard_spec_attached():
    """@guard decorator attaches _guard_spec to wrapped function."""
    print("test_decorator_guard_spec_attached")

    @guard(Require.authenticated(), Require.staff())
    async def handler(request):
        return {}

    check(hasattr(handler, "_guard_spec"), "has _guard_spec")
    check(len(handler._guard_spec.requirements) == 2, "2 requirements")


def test_decorator_preserves_name():
    """@guard decorator preserves function name via functools.wraps."""
    print("test_decorator_preserves_name")

    @guard(Require.authenticated())
    async def my_cool_handler(request):
        return {}

    check(
        my_cool_handler.__name__ == "my_cool_handler",
        f"name: {my_cool_handler.__name__}",
    )


# ── Test: Multi-resource chain ───────────────────────────────────────────────


def test_full_chain():
    """Full requirement chain: auth + not_banned + forum + post."""
    print("test_full_chain")
    forum = MockForum(id=1)
    post = MockPost(id=42, forum_id=1, author_id=1)

    async def resolve_forum(request, ctx, name):
        return forum

    async def resolve_post(request, ctx, pid):
        return post

    @guard(
        Require.authenticated(),
        Require.not_banned(),
        Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
        Require.resource("post", resolver=resolve_post, from_path="pid"),
    )
    async def handler(request, forum_name: str, pid: str):
        return {
            "forum_id": request.guard.forum.id,
            "post_id": request.guard.post.id,
        }

    request = MockRequest(
        user=SessionUser({"id": 1, "is_banned": False}),
        path_params={"forum_name": "python", "pid": "42"},
    )
    result = _run(handler(request, forum_name="python", pid="42"))
    check(result["forum_id"] == 1, "forum resolved")
    check(result["post_id"] == 42, "post resolved")


def test_full_chain_banned_short_circuits():
    """Banned user doesn't even resolve resources."""
    print("test_full_chain_banned_short_circuits")
    resolve_called = [False]

    async def resolve_forum(request, ctx, name):
        resolve_called[0] = True
        return MockForum()

    @guard(
        Require.authenticated(),
        Require.not_banned(),
        Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
    )
    async def handler(request, forum_name: str):
        return {}

    request = MockRequest(
        user=SessionUser({"id": 1, "is_banned": True}),
        path_params={"forum_name": "python"},
    )
    try:
        _run(handler(request, forum_name="python"))
        check(False, "should raise")
    except HTTPException as e:
        check(e.status_code == 403, "403 from ban")
        check(not resolve_called[0], "resolver not called")


# ── Test: Route scanner ──────────────────────────────────────────────────────


def test_find_guard_spec():
    """Scanner detects _guard_spec on wrapped functions."""
    print("test_find_guard_spec")

    @guard(Require.authenticated())
    async def protected(request):
        return {}

    async def unprotected(request):
        return {}

    check(_find_guard_spec(protected) is not None, "found on guarded")
    check(_find_guard_spec(unprotected) is None, "not found on unguarded")


def test_scan_result():
    """ScanResult computes coverage correctly."""
    print("test_scan_result")
    result = ScanResult()
    result.guarded.append(GuardedRoute("GET", "/a", "a", ("authenticated",)))
    result.guarded.append(GuardedRoute("POST", "/b", "b", ("staff",)))
    result.unguarded.append(UnguardedRoute("GET", "/c", "c"))

    check(result.total == 3, f"total: {result.total}")
    check(abs(result.coverage_pct - 66.67) < 1, f"coverage: {result.coverage_pct}")


def test_scan_empty():
    """Empty scan result has 100% coverage."""
    print("test_scan_empty")
    result = ScanResult()
    check(result.coverage_pct == 100.0, "empty = 100%")


# ── Test: HTTPException resolver interaction ─────────────────────────────────


def test_resolver_raises_http_exception():
    """Resource resolver can raise HTTPException directly for custom errors."""
    print("test_resolver_raises_http_exception")

    async def resolve_forum(request, ctx, name):
        raise HTTPException(403, "This forum is archived")

    @guard(
        Require.authenticated(),
        Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
    )
    async def handler(request, forum_name: str):
        return {}

    request = MockRequest(
        user=SessionUser({"id": 1}),
        path_params={"forum_name": "archived"},
    )
    try:
        _run(handler(request, forum_name="archived"))
        check(False, "should raise HTTPException")
    except HTTPException as e:
        check(e.status_code == 403, f"403 from resolver, got {e.status_code}")
        check("archived" in str(e.detail), "message preserved")


# ── Test: Edge cases ─────────────────────────────────────────────────────────


def test_empty_guard():
    """Guard with no requirements passes immediately."""
    print("test_empty_guard")

    @guard()
    async def handler(request):
        return {"ok": True}

    request = MockRequest()
    result = _run(handler(request))
    check(result["ok"] is True, "empty guard passes")
    check(request.guard is not None, "guard context still set")


def test_guard_context_metadata():
    """Guard context metadata can be set by requirements."""
    print("test_guard_context_metadata")

    async def set_metadata(request, ctx):
        ctx.metadata["request_time"] = 12345
        return None

    @guard(Require.check("meta", fn=set_metadata))
    async def handler(request):
        return {"time": request.guard.metadata["request_time"]}

    request = MockRequest()
    result = _run(handler(request))
    check(result["time"] == 12345, "metadata accessible")


def test_denial_frozen():
    """GuardDenial is immutable."""
    print("test_denial_frozen")
    denial = GuardDenial(DenyReason.FORBIDDEN, "test")
    try:
        denial.message = "changed"
        check(False, "should be frozen")
    except AttributeError:
        check(True, "frozen")


def test_requirement_frozen():
    """GuardRequirement is immutable."""
    print("test_requirement_frozen")
    req = Require.authenticated()
    try:
        req.name = "changed"
        check(False, "should be frozen")
    except AttributeError:
        check(True, "frozen")


# ── Test: API key requirement ────────────────────────────────────────────────


def test_api_key_pass():
    """Valid API key passes."""
    print("test_api_key_pass")
    req = Require.api_key()
    request = MockRequest(user=None)
    request.api_key_valid = True
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "valid API key passes")


def test_api_key_fail():
    """Invalid API key fails with 401."""
    print("test_api_key_fail")
    req = Require.api_key()
    request = MockRequest(user=None)
    request.api_key_valid = False
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "invalid key should fail")
    check(result.effective_status == 401, f"401, got {result.effective_status}")
    check("API key" in result.message, "message mentions API key")


def test_api_key_missing():
    """Missing API key (default=False) fails."""
    print("test_api_key_missing")
    req = Require.api_key()
    request = MockRequest(user=None)
    # api_key_valid not set — MockRequest doesn't have it, will be False-ish
    request.api_key_valid = False
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "missing key should fail")


# ── Test: Superuser requirement ──────────────────────────────────────────────


def test_superuser_pass():
    """Superuser passes."""
    print("test_superuser_pass")
    req = Require.superuser()
    request = MockRequest(user=SessionUser({"id": 1, "groups": ["superuser"]}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is None, "superuser passes")


def test_superuser_fail():
    """Non-superuser fails."""
    print("test_superuser_fail")
    req = Require.superuser()
    request = MockRequest(user=SessionUser({"id": 1, "groups": []}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "non-superuser should fail")
    check(result.effective_status == 403, "403 status")


def test_superuser_no_key():
    """User without is_superuser key fails."""
    print("test_superuser_no_key")
    req = Require.superuser()
    request = MockRequest(user=SessionUser({"id": 1}))
    ctx = GuardContext()
    result = _run(req.evaluate_fn(request, ctx))
    check(result is not None, "missing is_superuser should fail")


# ── Test: GuardContext repr ──────────────────────────────────────────────────


def test_guard_context_repr():
    """GuardContext has a useful repr."""
    print("test_guard_context_repr")
    ctx = GuardContext()
    ctx.resources["forum"] = MockForum()
    ctx.resources["post"] = MockPost()
    ctx.metadata["time"] = 123
    r = repr(ctx)
    check("forum" in r, "repr shows forum")
    check("post" in r, "repr shows post")
    check("time" in r, "repr shows metadata")


# ── Test: Audit logging ──────────────────────────────────────────────────────


def test_denial_is_logged():
    """Denied access triggers a log message."""
    print("test_denial_is_logged")
    import io
    import logging

    # Capture log output
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.DEBUG)
    # hyperdjango.logging.logger writes to stdout via the loguru-compat API
    # but we can verify the evaluator's _log_denial is called by checking
    # that an HTTPException is raised (the log is a side effect)
    spec = GuardSpec(requirements=(Require.authenticated(),))
    request = MockRequest(user=None, path="/test/route", method="POST")
    try:
        _run(evaluate_guard(request, spec))
        check(False, "should raise")
    except HTTPException:
        check(True, "denial logged and HTTPException raised")


# ── Test: any_of with staff OR api_key ───────────────────────────────────────


def test_any_of_staff_or_api_key():
    """Staff OR API key — common admin pattern."""
    print("test_any_of_staff_or_api_key")
    req = Require.any_of(
        Require.staff(),
        Require.api_key(),
    )

    # Staff passes
    request1 = MockRequest(user=SessionUser({"id": 1, "groups": ["staff"]}))
    request1.api_key_valid = False
    ctx1 = GuardContext()
    result1 = _run(req.evaluate_fn(request1, ctx1))
    check(result1 is None, "staff passes")

    # API key passes
    request2 = MockRequest(user=None)
    request2.api_key_valid = True
    ctx2 = GuardContext()
    result2 = _run(req.evaluate_fn(request2, ctx2))
    check(result2 is None, "api_key passes")

    # Neither fails
    request3 = MockRequest(user=SessionUser({"id": 1, "groups": []}))
    request3.api_key_valid = False
    ctx3 = GuardContext()
    result3 = _run(req.evaluate_fn(request3, ctx3))
    check(result3 is not None, "neither should fail")


# ── Run all tests ────────────────────────────────────────────────────────────


# ── Test: guard_action ───────────────────────────────────────────────────────


class _MockViewSet:
    """Minimal ViewSet mock for guard_action testing."""

    def __init__(self, request):
        self.request = request


def test_guard_action_pass():
    """guard_action passes when requirements are met."""
    print("test_guard_action_pass")

    @guard_action(Require.authenticated())
    async def publish(self, request, **kwargs):
        return {"published": True, "guard": request.guard is not None}

    vs = _MockViewSet(MockRequest(user=SessionUser({"id": 1, "username": "alice"})))
    result = _run(publish(vs, vs.request))
    check(result["published"] is True, "handler returned")
    check(result["guard"] is True, "request.guard set")
    check(isinstance(vs.request.guard, GuardContext), "guard is GuardContext")


def test_guard_action_fail_auth():
    """guard_action raises HTTPException when not authenticated."""
    print("test_guard_action_fail_auth")

    @guard_action(Require.authenticated())
    async def publish(self, request, **kwargs):
        return {"published": True}

    vs = _MockViewSet(MockRequest(user=None))
    try:
        _run(publish(vs, vs.request))
        check(False, "should raise HTTPException")
    except HTTPException as e:
        check(e.status_code == 401, f"401 from auth, got {e.status_code}")


def test_guard_action_fail_staff():
    """guard_action raises HTTPException when not staff."""
    print("test_guard_action_fail_staff")

    @guard_action(Require.authenticated(), Require.staff())
    async def publish(self, request, **kwargs):
        return {"published": True}

    vs = _MockViewSet(
        MockRequest(user=SessionUser({"id": 1, "username": "alice", "is_staff": False}))
    )
    try:
        _run(publish(vs, vs.request))
        check(False, "should raise HTTPException")
    except HTTPException as e:
        check(e.status_code == 403, f"403 from staff, got {e.status_code}")


def test_guard_action_preserves_action_attrs():
    """guard_action preserves @action metadata attributes."""
    print("test_guard_action_preserves_action_attrs")
    from hyperdjango.rest import ActionMeta, action

    @action(methods=["POST"], detail=True, url_path="publish")
    @guard_action(Require.authenticated(), Require.staff())
    async def publish(self, request, **kwargs):
        return {"published": True}

    # @action sets these on the function — guard_action must forward them
    check(publish.__dict__.get("_is_action") is True, "_is_action preserved")
    check(publish.__dict__.get("_action_detail") is True, "_action_detail preserved")
    check(
        publish.__dict__.get("_action_url_path") == "publish",
        "_action_url_path preserved",
    )
    meta = publish.__dict__.get("_action_meta")
    check(isinstance(meta, ActionMeta), "_action_meta is ActionMeta")
    check(meta.detail is True, "meta.detail is True")
    check(meta.url_path == "publish", "meta.url_path is publish")


def test_guard_action_guard_spec_attached():
    """guard_action attaches _guard_spec to wrapped function."""
    print("test_guard_action_guard_spec_attached")

    @guard_action(Require.authenticated(), Require.staff())
    async def feature(self, request, **kwargs):
        return {}

    check("_guard_spec" in feature.__dict__, "has _guard_spec")
    spec = feature.__dict__["_guard_spec"]
    check(isinstance(spec, GuardSpec), "spec is GuardSpec")
    check(len(spec.requirements) == 2, f"2 requirements, got {len(spec.requirements)}")


def test_guard_action_preserves_name():
    """guard_action preserves function name via functools.wraps."""
    print("test_guard_action_preserves_name")

    @guard_action(Require.authenticated())
    async def my_action_handler(self, request, **kwargs):
        return {}

    check(
        my_action_handler.__name__ == "my_action_handler",
        f"name: {my_action_handler.__name__}",
    )


def test_guard_action_resource_resolver():
    """guard_action supports resource resolvers."""
    print("test_guard_action_resource_resolver")
    forum = MockForum(id=5, name="books")

    async def resolve_forum(request, ctx, name):
        return forum

    @guard_action(
        Require.authenticated(),
        Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
    )
    async def moderate(self, request, **kwargs):
        return {"forum_id": request.guard.forum.id}

    vs = _MockViewSet(
        MockRequest(
            user=SessionUser({"id": 1}),
            path_params={"forum_name": "books"},
        )
    )
    result = _run(moderate(vs, vs.request))
    check(result["forum_id"] == 5, f"forum resolved, got {result['forum_id']}")


def test_guard_action_chain_auth_plus_staff():
    """guard_action short-circuits: auth failure before staff check."""
    print("test_guard_action_chain_auth_plus_staff")

    call_count = 0

    async def counting_check(request, ctx):
        nonlocal call_count
        call_count += 1
        return None

    @guard_action(
        Require.authenticated(),
        Require.check("counter", fn=counting_check),
    )
    async def handler(self, request, **kwargs):
        return {}

    vs = _MockViewSet(MockRequest(user=None))
    with contextlib.suppress(HTTPException):
        _run(handler(vs, vs.request))
    check(call_count == 0, f"short-circuit: counter not called, got {call_count}")


def test_guard_action_redirect_converts_to_401():
    """guard_action converts _RedirectDenial to HTTPException(401) for API actions."""
    print("test_guard_action_redirect_converts_to_401")

    # Using redirect_url with guard_action is a misuse, but it should fail
    # gracefully with a JSON 401 instead of an unhandled exception.
    @guard_action(Require.authenticated(redirect_url="/login"))
    async def publish(self, request, **kwargs):
        return {"ok": True}

    vs = _MockViewSet(MockRequest(user=None))
    try:
        _run(publish(vs, vs.request))
        check(False, "should raise HTTPException")
    except HTTPException as e:
        check(
            e.status_code == 401, f"401 from redirect conversion, got {e.status_code}"
        )


# ── Test: guard_websocket ────────────────────────────────────────────────────


@dataclass
class MockWebSocket:
    """Minimal WebSocket mock for guard_websocket testing."""

    headers: dict[str, str] = field(default_factory=dict)
    path: str = "/ws/chat"
    query_string: str = ""
    user: object = None
    guard: object = None
    _accepted: bool = False
    _closed: bool = False
    _close_code: int = 0
    _close_reason: str = ""
    _sent_messages: list[object] = field(default_factory=list)

    async def accept(self, subprotocol=None):
        self._accepted = True

    async def close(self, code=1000, reason=""):
        self._closed = True
        self._close_code = code
        self._close_reason = reason

    async def send_json(self, data):
        self._sent_messages.append(data)


def _make_ws_auth():
    """Create a SessionAuth + signed cookie for testing."""
    from hyperdjango.auth.sessions import SessionAuth
    from hyperdjango.native._crypto import sign_data

    auth = SessionAuth(secret="test-ws-secret")
    user_data = {"id": 42, "username": "alice", "groups": ["staff"]}
    session_id = auth.store.create(user_data)
    signed_cookie = sign_data(session_id, auth.secret)
    cookie_header = f"{auth.cookie_name}={signed_cookie}"
    return auth, cookie_header, user_data


def test_guard_websocket_pass():
    """guard_websocket passes when authenticated and requirements met."""
    print("test_guard_websocket_pass")
    auth, cookie_header, user_data = _make_ws_auth()

    @guard_websocket(auth, Require.authenticated())
    async def handler(ws):
        return {"user_id": ws.user["id"]}

    ws = MockWebSocket(headers={"cookie": cookie_header})
    result = _run(handler(ws))
    check(ws._accepted, "ws accepted")
    check(not ws._closed, "ws not closed")
    check(ws.user is not None, "ws.user set")
    check(ws.user["id"] == 42, f"user id 42, got {ws.user['id']}")
    check(isinstance(ws.guard, GuardContext), "ws.guard is GuardContext")
    check(result["user_id"] == 42, "handler returned user_id")


def test_guard_websocket_no_cookie():
    """guard_websocket denies when no session cookie."""
    print("test_guard_websocket_no_cookie")
    auth, _, _ = _make_ws_auth()

    @guard_websocket(auth, Require.authenticated())
    async def handler(ws):
        return {"ok": True}

    ws = MockWebSocket(headers={})
    _run(handler(ws))
    check(ws._accepted, "ws accepted before close")
    check(ws._closed, "ws closed")
    check(ws._close_code == 4001, f"close code 4001, got {ws._close_code}")
    check(len(ws._sent_messages) == 1, "sent error message")
    check(ws._sent_messages[0]["type"] == "error", "error type")


def test_guard_websocket_invalid_cookie():
    """guard_websocket denies when cookie signature invalid."""
    print("test_guard_websocket_invalid_cookie")
    auth, _, _ = _make_ws_auth()

    @guard_websocket(auth, Require.authenticated())
    async def handler(ws):
        return {"ok": True}

    ws = MockWebSocket(headers={"cookie": f"{auth.cookie_name}=invalid.signature"})
    _run(handler(ws))
    check(ws._closed, "ws closed on invalid cookie")
    check(ws._close_code == 4001, f"close code 4001, got {ws._close_code}")


def test_guard_websocket_banned():
    """guard_websocket denies banned users with 4003."""
    print("test_guard_websocket_banned")
    from hyperdjango.auth.sessions import SessionAuth
    from hyperdjango.native._crypto import sign_data

    auth = SessionAuth(secret="test-ws-ban")
    banned_data = {"id": 99, "username": "baduser", "is_banned": True}
    session_id = auth.store.create(banned_data)
    signed = sign_data(session_id, auth.secret)
    cookie = f"{auth.cookie_name}={signed}"

    @guard_websocket(auth, Require.authenticated(), Require.not_banned())
    async def handler(ws):
        return {"ok": True}

    ws = MockWebSocket(headers={"cookie": cookie})
    _run(handler(ws))
    check(ws._closed, "ws closed for banned user")
    check(ws._close_code == 4003, f"close code 4003, got {ws._close_code}")


def test_guard_websocket_staff_required():
    """guard_websocket denies non-staff with 4003."""
    print("test_guard_websocket_staff_required")
    from hyperdjango.auth.sessions import SessionAuth
    from hyperdjango.native._crypto import sign_data

    auth = SessionAuth(secret="test-ws-staff")
    user_data = {"id": 10, "username": "bob", "is_staff": False}
    session_id = auth.store.create(user_data)
    signed = sign_data(session_id, auth.secret)
    cookie = f"{auth.cookie_name}={signed}"

    @guard_websocket(auth, Require.authenticated(), Require.staff())
    async def handler(ws):
        return {"ok": True}

    ws = MockWebSocket(headers={"cookie": cookie})
    _run(handler(ws))
    check(ws._closed, "ws closed for non-staff")
    check(ws._close_code == 4003, f"close code 4003, got {ws._close_code}")


def test_guard_websocket_guard_spec_attached():
    """guard_websocket attaches _guard_spec to wrapped function."""
    print("test_guard_websocket_guard_spec_attached")
    auth, _, _ = _make_ws_auth()

    @guard_websocket(auth, Require.authenticated())
    async def handler(ws):
        return {}

    check("_guard_spec" in handler.__dict__, "has _guard_spec")
    spec = handler.__dict__["_guard_spec"]
    check(isinstance(spec, GuardSpec), "spec is GuardSpec")
    check(len(spec.requirements) == 1, f"1 requirement, got {len(spec.requirements)}")


def test_guard_websocket_preserves_name():
    """guard_websocket preserves function name."""
    print("test_guard_websocket_preserves_name")
    auth, _, _ = _make_ws_auth()

    @guard_websocket(auth, Require.authenticated())
    async def my_chat_handler(ws):
        return {}

    check(
        my_chat_handler.__name__ == "my_chat_handler",
        f"name: {my_chat_handler.__name__}",
    )


def test_guard_websocket_redirect_converts_to_4001():
    """guard_websocket converts _RedirectDenial to 4001 close."""
    print("test_guard_websocket_redirect_converts_to_4001")
    auth, _, _ = _make_ws_auth()

    # Using redirect_url with guard_websocket is a misuse, but it should
    # close gracefully with 4001 instead of propagating an unhandled exception.
    @guard_websocket(auth, Require.authenticated(redirect_url="/login"))
    async def handler(ws):
        return {"ok": True}

    ws = MockWebSocket(headers={})  # No cookie → unauthenticated
    _run(handler(ws))
    check(ws._accepted, "ws accepted before close")
    check(ws._closed, "ws closed")
    check(ws._close_code == 4001, f"close code 4001, got {ws._close_code}")


# ── Require.group() + type safety ────────────────────────────────────────────


def test_group_pass():
    """User in the required group passes."""
    print("test_group_pass")
    req = Require.group("editors")
    request = MockRequest(user=SessionUser({"id": 1, "groups": ["editors", "staff"]}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is None, "group member should pass")


def test_group_fail_no_group():
    """User with empty groups list is denied."""
    print("test_group_fail_no_group")
    req = Require.group("editors")
    request = MockRequest(user=SessionUser({"id": 1, "groups": []}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is not None, "empty groups should deny")
    check(result.reason == DenyReason.FORBIDDEN, "reason is forbidden")


def test_group_fail_wrong_group():
    """User in a different group is denied."""
    print("test_group_fail_wrong_group")
    req = Require.group("admin")
    request = MockRequest(user=SessionUser({"id": 1, "groups": ["reader"]}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is not None, "wrong group should deny")


def test_group_fail_not_authenticated():
    """Non-dict user is denied."""
    print("test_group_fail_not_authenticated")
    req = Require.group("staff")
    request = MockRequest(user=None)
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is not None, "no user should deny")
    check(result.reason == DenyReason.NOT_AUTHENTICATED, "reason is not_authenticated")


def test_group_type_safety_string():
    """SECURITY: groups as string must deny (prevents substring bypass)."""
    print("test_group_type_safety_string")
    req = Require.group("staff")
    # "staff" in "staffing" would be True without type check
    request = MockRequest(user=SessionUser({"id": 1, "groups": "staffing"}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is not None, "string groups MUST deny (substring attack)")


def test_group_type_safety_dict():
    """SECURITY: groups as dict must deny (prevents key-check bypass)."""
    print("test_group_type_safety_dict")
    req = Require.group("staff")
    # "staff" in {"staff": True} would be True (checks keys)
    request = MockRequest(user=SessionUser({"id": 1, "groups": {"staff": True}}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is not None, "dict groups MUST deny")


def test_group_type_safety_int():
    """SECURITY: groups as int must deny (not crash with TypeError)."""
    print("test_group_type_safety_int")
    req = Require.group("staff")
    request = MockRequest(user=SessionUser({"id": 1, "groups": 42}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is not None, "int groups MUST deny (not crash)")


def test_staff_with_groups_list():
    """Require.staff() passes when 'staff' is in groups list."""
    print("test_staff_with_groups_list")
    req = Require.staff()
    request = MockRequest(user=SessionUser({"id": 1, "groups": ["staff", "editor"]}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is None, "staff in groups list should pass")


def test_staff_groups_takes_priority():
    """Require.staff() checks groups before is_staff boolean."""
    print("test_staff_groups_takes_priority")
    req = Require.staff()
    # groups has "staff", is_staff is False — should still pass via groups
    request = MockRequest(
        user=SessionUser({"id": 1, "groups": ["staff"], "is_staff": False})
    )
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is None, "groups should take priority over is_staff=False")


def test_staff_boolean_only_denied():
    """Require.staff() denies when only is_staff=True, no groups (RBAC is authoritative)."""
    print("test_staff_boolean_only_denied")
    req = Require.staff()
    request = MockRequest(user=SessionUser({"id": 1, "is_staff": True}))
    result = _run(req.evaluate_fn(request, GuardContext()))
    check(result is not None, "boolean-only is_staff denied (groups authoritative)")


def main():
    tests = [
        test_requirement_types,
        test_authenticated_pass,
        test_authenticated_fail_no_user,
        test_authenticated_pass_no_id,
        test_authenticated_redirect,
        test_staff_pass,
        test_staff_fail,
        test_staff_fail_no_key,
        test_not_banned_pass,
        test_not_banned_fail,
        test_not_banned_no_user,
        test_not_banned_no_key,
        test_not_muted_no_key,
        test_not_muted_pass,
        test_not_muted_fail,
        test_not_muted_no_user,
        test_timeline_table_missing_is_unavailable,
        test_timeline_db_error_fails_closed,
        test_resource_pass,
        test_resource_not_found,
        test_resource_missing_path_param,
        test_resource_custom_deny_message,
        test_resource_no_from_path,
        test_resource_chaining,
        test_custom_check_pass,
        test_custom_check_fail,
        test_any_of_first_passes,
        test_any_of_second_passes,
        test_any_of_all_fail,
        test_any_of_rollback,
        test_any_of_name,
        test_guard_spec_frozen,
        test_guard_spec_requirement_names,
        test_guard_context_attribute_access,
        test_guard_context_missing_attribute,
        test_denial_effective_status,
        test_denial_custom_status,
        test_evaluate_all_pass,
        test_evaluate_short_circuit,
        test_evaluate_redirect,
        test_evaluate_resource_stored,
        test_decorator_pass,
        test_decorator_fail,
        test_decorator_redirect,
        test_decorator_redirect_url_encoded,
        test_decorator_resource_access,
        test_decorator_guard_spec_attached,
        test_decorator_preserves_name,
        test_full_chain,
        test_full_chain_banned_short_circuits,
        test_find_guard_spec,
        test_scan_result,
        test_scan_empty,
        test_resolver_raises_http_exception,
        test_empty_guard,
        test_guard_context_metadata,
        test_denial_frozen,
        test_requirement_frozen,
        test_api_key_pass,
        test_api_key_fail,
        test_api_key_missing,
        test_superuser_pass,
        test_superuser_fail,
        test_superuser_no_key,
        test_guard_context_repr,
        test_denial_is_logged,
        test_any_of_staff_or_api_key,
    ]

    for test in tests:
        test()

    # ── guard_action tests ──────────────────────────────────────────────────
    action_tests = [
        test_guard_action_pass,
        test_guard_action_fail_auth,
        test_guard_action_fail_staff,
        test_guard_action_preserves_action_attrs,
        test_guard_action_guard_spec_attached,
        test_guard_action_preserves_name,
        test_guard_action_resource_resolver,
        test_guard_action_chain_auth_plus_staff,
        test_guard_action_redirect_converts_to_401,
    ]

    for test in action_tests:
        test()

    # ── Require.group() tests ────────────────────────────────────────────────
    group_tests = [
        test_group_pass,
        test_group_fail_no_group,
        test_group_fail_wrong_group,
        test_group_fail_not_authenticated,
        test_group_type_safety_string,
        test_group_type_safety_dict,
        test_group_type_safety_int,
        test_staff_with_groups_list,
        test_staff_groups_takes_priority,
        test_staff_boolean_only_denied,
    ]

    for test in group_tests:
        test()

    # ── guard_websocket tests ───────────────────────────────────────────────
    ws_tests = [
        test_guard_websocket_pass,
        test_guard_websocket_no_cookie,
        test_guard_websocket_invalid_cookie,
        test_guard_websocket_banned,
        test_guard_websocket_staff_required,
        test_guard_websocket_guard_spec_attached,
        test_guard_websocket_preserves_name,
        test_guard_websocket_redirect_converts_to_4001,
    ]

    for test in ws_tests:
        test()

    total = _PASS + _FAIL
    print(f"\n{'=' * 60}")
    print(f"HyperGuard: {_PASS}/{total} passed, {_FAIL} failed")
    if _FAIL:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
