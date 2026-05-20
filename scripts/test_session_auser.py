"""
Tests for request.auser async accessor on login/logout.

Verifies that SessionAuth.login / login_async / logout / logout_async install
both request.user (sync) and request.auser (async, awaitable) so async
frameworks can read the authenticated user without blocking the event loop.

Coverage:
1. _set_auth_user sets both request.user and request.auser
2. request.auser is awaitable and returns the same object as request.user
3. login(): request.user is the SessionUser AND await request.auser() == it
4. login_async(): same invariant
5. logout(): request.user/auser reflect AnonymousUser
6. logout_async(): same invariant
7. request.user (sync) still works unchanged after login/logout
8. auser is consistently awaitable any number of times
9. middleware installs auser=None for anonymous requests
10. login without a request argument is a no-op for auser (backward compat)

Usage:
    uv run hyper-test session_auser
"""

# hyper-test: unit

import asyncio
import inspect
import sys
import traceback

from hyperdjango.auth.sessions import (
    InMemorySessionStore,
    SessionAuth,
    _resolve_auth_user,
    _set_auth_user,
)
from hyperdjango.auth.user import AnonymousUser, SessionUser
from hyperdjango.request import Request
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


SECRET = "test-secret-key-for-session-auser"


def _make_request(cookie_header: str = "") -> Request:
    """Create a real Request, optionally carrying a session cookie."""
    headers = {"cookie": cookie_header} if cookie_header else {}
    return Request(
        method="GET",
        path="/test",
        query_string="",
        headers=headers,
        body=b"",
    )


def _make_auth(store=None, **kwargs) -> SessionAuth:
    return SessionAuth(
        secret=SECRET,
        store=store if store is not None else InMemorySessionStore(),
        secure_cookie=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. _set_auth_user helper
# ---------------------------------------------------------------------------


@test("_set_auth_user: sets request.user to the given object")
def test_helper_sets_user():
    req = _make_request()
    user = SessionUser({"id": 7, "username": "alice"})
    _set_auth_user(req, user)
    assert req.user is user


@test("_set_auth_user: installs request.auser as a callable")
def test_helper_sets_auser_callable():
    req = _make_request()
    user = SessionUser({"id": 7, "username": "alice"})
    _set_auth_user(req, user)
    assert callable(req.auser), "request.auser must be callable"


@test("_set_auth_user: request.auser() returns an awaitable")
async def test_helper_auser_awaitable():
    req = _make_request()
    user = SessionUser({"id": 7, "username": "alice"})
    _set_auth_user(req, user)
    awaitable = req.auser()
    assert inspect.isawaitable(awaitable), "request.auser() must be awaitable"
    resolved = await awaitable
    assert resolved is user


@test("_set_auth_user: await request.auser() == request.user")
async def test_helper_auser_matches_user():
    req = _make_request()
    user = SessionUser({"id": 7, "username": "alice"})
    _set_auth_user(req, user)
    resolved = await req.auser()
    assert resolved is req.user


@test("_resolve_auth_user: coroutine returns the captured user")
async def test_resolve_auth_user():
    sentinel = object()
    result = await _resolve_auth_user(sentinel)
    assert result is sentinel


# ---------------------------------------------------------------------------
# 2. login() — sync store
# ---------------------------------------------------------------------------


@test("login: request.user is the SessionUser for the logged-in data")
def test_login_sets_user():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    user_data = {"id": 42, "username": "bob", "email": "bob@example.com"}
    auth.login(resp, user_data, req)
    assert isinstance(req.user, SessionUser)
    assert req.user.id == 42
    assert req.user.username == "bob"
    assert req.user.is_authenticated is True


@test("login: await request.auser() returns the same user object")
async def test_login_auser_matches():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    auth.login(resp, {"id": 42, "username": "bob"}, req)
    resolved = await req.auser()
    assert resolved is req.user
    assert resolved.id == 42
    assert resolved.username == "bob"


@test("login: sets request.session_id")
def test_login_sets_session_id():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    session_id = auth.login(resp, {"id": 1, "username": "x"}, req)
    assert req.session_id == session_id
    assert req.session_id is not None


@test("login: without request argument does not raise (backward compat)")
def test_login_no_request():
    auth = _make_auth()
    resp = Response.json({})
    session_id = auth.login(resp, {"id": 1, "username": "x"})
    assert session_id is not None


# ---------------------------------------------------------------------------
# 3. login_async() — async path
# ---------------------------------------------------------------------------


@test("login_async: request.user is the SessionUser")
async def test_login_async_sets_user():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    await auth.login_async(resp, {"id": 99, "username": "carol"}, req)
    assert isinstance(req.user, SessionUser)
    assert req.user.id == 99
    assert req.user.username == "carol"


@test("login_async: await request.auser() matches request.user")
async def test_login_async_auser_matches():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    await auth.login_async(resp, {"id": 99, "username": "carol"}, req)
    resolved = await req.auser()
    assert resolved is req.user
    assert resolved.username == "carol"


# ---------------------------------------------------------------------------
# 4. logout() — resets to anonymous
# ---------------------------------------------------------------------------


@test("logout: request.user becomes AnonymousUser")
def test_logout_anonymous_user():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    session_id = auth.login(resp, {"id": 5, "username": "dave"}, req)
    assert req.user.is_authenticated is True

    logout_resp = Response.json({})
    auth.logout(logout_resp, session_id, req)
    assert isinstance(req.user, AnonymousUser)
    assert req.user.is_authenticated is False
    assert req.session_id is None


@test("logout: await request.auser() reflects AnonymousUser")
async def test_logout_auser_anonymous():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    session_id = auth.login(resp, {"id": 5, "username": "dave"}, req)

    logout_resp = Response.json({})
    auth.logout(logout_resp, session_id, req)
    resolved = await req.auser()
    assert resolved is req.user
    assert isinstance(resolved, AnonymousUser)
    assert resolved.is_authenticated is False


@test("logout: without request argument does not raise (backward compat)")
def test_logout_no_request():
    auth = _make_auth()
    resp = Response.json({})
    session_id = auth.login(resp, {"id": 1, "username": "x"})
    logout_resp = Response.json({})
    auth.logout(logout_resp, session_id)  # no request — must not raise


# ---------------------------------------------------------------------------
# 5. logout_async()
# ---------------------------------------------------------------------------


@test("logout_async: request.user becomes AnonymousUser")
async def test_logout_async_anonymous():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    session_id = await auth.login_async(resp, {"id": 6, "username": "erin"}, req)
    assert req.user.is_authenticated is True

    logout_resp = Response.json({})
    await auth.logout_async(logout_resp, session_id, req)
    assert isinstance(req.user, AnonymousUser)
    resolved = await req.auser()
    assert resolved is req.user
    assert resolved.is_authenticated is False
    assert req.session_id is None


# ---------------------------------------------------------------------------
# 6. Sync user still works; auser consistently awaitable
# ---------------------------------------------------------------------------


@test("login then logout: sync request.user path works unchanged")
def test_sync_path_roundtrip():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    session_id = auth.login(resp, {"id": 11, "username": "frank"}, req)
    # Sync access — no await needed
    assert req.user.username == "frank"
    assert req.user.is_authenticated

    logout_resp = Response.json({})
    auth.logout(logout_resp, session_id, req)
    assert req.user.is_authenticated is False


@test("auser is consistently awaitable multiple times")
async def test_auser_repeatable():
    auth = _make_auth()
    req = _make_request()
    resp = Response.json({})
    auth.login(resp, {"id": 12, "username": "grace"}, req)
    first = await req.auser()
    second = await req.auser()
    third = await req.auser()
    assert first is second is third is req.user


# ---------------------------------------------------------------------------
# 7. Middleware installs auser for anonymous requests
# ---------------------------------------------------------------------------


@test("middleware: anonymous request gets AnonymousUser + awaitable auser")
async def test_middleware_anonymous_auser():
    auth = _make_auth()
    req = _make_request()  # no cookie

    captured = {}

    async def call_next(request):
        captured["user"] = request.user
        captured["auser"] = request.auser
        return Response.json({})

    await auth(req, call_next)
    # ws27 item 1: anonymous requests get an AnonymousUser() sentinel (never
    # None), so request.user.is_authenticated is a safe False through both
    # SessionAuth (the one session-auth middleware).
    assert isinstance(captured["user"], AnonymousUser)
    assert captured["user"].is_authenticated is False
    assert callable(captured["auser"])
    resolved = await captured["auser"]()
    assert isinstance(resolved, AnonymousUser)


@test("middleware: valid session sets user + matching awaitable auser")
async def test_middleware_authenticated_auser():
    store = InMemorySessionStore()
    auth = _make_auth(store=store)
    # Log in to create a real cookie + session.
    login_resp = Response.json({})
    login_req = _make_request()
    session_id = auth.login(login_resp, {"id": 21, "username": "heidi"}, login_req)
    signed = auth._sign_session_id(session_id)

    # Fresh request carrying the session cookie through the middleware.
    req = _make_request(f"{auth.cookie_name}={signed}")

    captured = {}

    async def call_next(request):
        captured["user"] = request.user
        captured["auser"] = request.auser
        return Response.json({})

    await auth(req, call_next)
    assert isinstance(captured["user"], SessionUser)
    assert captured["user"].username == "heidi"
    resolved = await captured["auser"]()
    assert resolved is captured["user"]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    print("=" * 70)
    print("request.auser async accessor")
    print("=" * 70)

    tests = [
        obj
        for name, obj in sorted(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    for t in tests:
        await t()

    print("=" * 70)
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")
    print("=" * 70)
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---\n{tb}")
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
