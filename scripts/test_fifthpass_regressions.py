"""
Regression tests for fifth-pass fixes.

Tests:
1. Admin CSRF verification on POST handlers
2. CacheMiddleware user-aware cache keys
3. CacheMiddleware response.status (not status_code)

Usage:
    uv run hyper-test fifthpass_regressions
"""

# hyper-test: unit

import asyncio
import inspect
import sys
import traceback

from hyperdjango.auth.user import SessionUser
from hyperdjango.cache import LocMemCache
from hyperdjango.cache_adapters import CacheMiddleware
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
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Admin CSRF verification
# ---------------------------------------------------------------------------


@test("admin: CSRF verify skips when require_auth=False")
def test_csrf_skip_no_auth():
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp

    app = HyperApp(title="test")
    # Default require_auth=True — CSRF is enforced
    admin_auth = HyperAdmin(app, prefix="/admin")
    req = Request(
        method="POST", path="/admin/test/", headers={"cookie": "hyper_admin_session=x"}
    )
    assert admin_auth._verify_csrf_token(req) is False  # No token = fail

    # require_auth=False — CSRF is skipped
    admin_no_auth = HyperAdmin(app, prefix="/admin2", require_auth=False)
    assert admin_no_auth._verify_csrf_token(req) is True  # Skipped


@test("admin: CSRF verify accepts valid token")
def test_csrf_verify_valid():
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp

    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    req = Request(
        method="POST",
        path="/admin/item/add/",
        headers={"cookie": "hyper_admin_session=test-session"},
    )

    # Generate token for this request
    token = admin._generate_csrf_token(req)

    # Simulate form submission with token
    req._form = {"_csrf_token": [token]}
    assert admin._verify_csrf_token(req) is True


@test("admin: CSRF verify rejects wrong token")
def test_csrf_verify_invalid():
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp

    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    req = Request(
        method="POST",
        path="/admin/item/add/",
        headers={"cookie": "hyper_admin_session=test-session"},
    )
    req._form = {"_csrf_token": ["completely-wrong-token"]}
    assert admin._verify_csrf_token(req) is False


@test("admin: CSRF verify rejects missing token")
def test_csrf_verify_missing():
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp

    app = HyperApp(title="test")
    admin = HyperAdmin(app, prefix="/admin")

    req = Request(
        method="POST",
        path="/admin/item/add/",
        headers={"cookie": "hyper_admin_session=test-session"},
    )
    req._form = {}
    assert admin._verify_csrf_token(req) is False


# ---------------------------------------------------------------------------
# CacheMiddleware user-aware keys
# ---------------------------------------------------------------------------


@test("CacheMiddleware: anonymous users share cache key")
async def test_cache_anon_shared():
    cache = LocMemCache(max_size=100)
    mw = CacheMiddleware(cache, ttl=60)

    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        return Response.html(f"<h1>Response {call_count}</h1>")

    # Two anonymous requests to same path
    req1 = Request(method="GET", path="/page")
    req2 = Request(method="GET", path="/page")

    resp1 = await mw(req1, handler)
    resp2 = await mw(req2, handler)

    assert call_count == 1  # Only computed once (cache hit)
    assert resp2.headers.get("X-Cache") == "HIT"


@test("CacheMiddleware: authenticated users skipped by default")
async def test_cache_skip_auth():
    cache = LocMemCache(max_size=100)
    mw = CacheMiddleware(cache, ttl=60, cache_authenticated=False)

    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        return Response.html("<h1>Private</h1>")

    req = Request(method="GET", path="/dashboard")
    req.user = SessionUser({"id": 1, "is_authenticated": True})

    resp = await mw(req, handler)
    assert call_count == 1
    assert "X-Cache" not in resp.headers  # Not cached


@test("CacheMiddleware: user-aware keys when cache_authenticated=True")
async def test_cache_user_aware():
    cache = LocMemCache(max_size=100)
    mw = CacheMiddleware(cache, ttl=60, cache_authenticated=True)

    async def handler(request):
        user = getattr(request, "user", None)
        uid = user.get("id") if user else "anon"
        return Response.html(f"<h1>User {uid}</h1>")

    # User 1 request
    req1 = Request(method="GET", path="/dashboard")
    req1.user = SessionUser({"id": 1, "is_authenticated": True})
    resp1 = await mw(req1, handler)
    assert b"User 1" in resp1.body

    # User 2 request — should NOT get User 1's cached page
    req2 = Request(method="GET", path="/dashboard")
    req2.user = SessionUser({"id": 2, "is_authenticated": True})
    resp2 = await mw(req2, handler)
    assert b"User 2" in resp2.body  # Different user gets different response


@test("CacheMiddleware: status attribute works (not status_code)")
async def test_cache_status_attr():
    cache = LocMemCache(max_size=100)
    mw = CacheMiddleware(cache, ttl=60)

    async def handler(request):
        return Response.html("<h1>OK</h1>", status=200)

    req = Request(method="GET", path="/ok")
    resp = await mw(req, handler)
    assert resp.status == 200
    assert resp.headers.get("X-Cache") == "MISS"

    # Second request should hit cache
    req2 = Request(method="GET", path="/ok")
    resp2 = await mw(req2, handler)
    assert resp2.headers.get("X-Cache") == "HIT"


@test("CacheMiddleware: excludes paths correctly")
async def test_cache_exclude():
    cache = LocMemCache(max_size=100)
    mw = CacheMiddleware(cache, ttl=60, exclude=["/admin", "/api/auth"])

    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        return Response.html("<h1>OK</h1>")

    req = Request(method="GET", path="/admin/dashboard")
    await mw(req, handler)
    await mw(req, handler)
    assert call_count == 2  # Not cached — excluded path


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nFifth-Pass Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
