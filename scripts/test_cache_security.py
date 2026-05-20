"""
Tests for CacheMiddleware response-caching safety.

Verifies the page cache never stores a response that is private, marked
no-cache/no-store (case-insensitively), unique-per-request (Vary: *), or
specific to a request that carried an Authorization header (unless the
response is explicitly Cache-Control: public). Authenticated responses also
gain a Vary: Authorization header so shared caches downstream vary correctly.

Usage:
    uv run hyper-test cache_security
"""

# hyper-test: unit

import asyncio
import inspect
import sys
import traceback

from hyperdjango.cache import LocMemCache
from hyperdjango.cache_adapters import CacheMiddleware

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


class FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None, path: str = "/page"):
        self.method = "GET"
        self.path = path
        self.query_string = ""
        self.headers = dict(headers or {})
        self.user = None


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: bytes = b"<h1>ok</h1>",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.body = body
        self.headers = dict(headers or {"content-type": "text/html"})


async def _run_twice(mw, req_headers, resp_headers):
    """Drive two identical requests through mw; return (handler_calls, r1, r2)."""
    calls = 0

    async def call_next(req):
        nonlocal calls
        calls += 1
        return FakeResponse(headers=resp_headers)

    r1 = await mw(FakeRequest(headers=req_headers), call_next)
    r2 = await mw(FakeRequest(headers=req_headers), call_next)
    return calls, r1, r2


@test("plain GET is cached (baseline)")
async def t_baseline():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls, _r1, r2 = await _run_twice(mw, {}, {"content-type": "text/html"})
    assert calls == 1, f"expected 1 handler call, got {calls}"
    assert r2.headers.get("X-Cache") == "HIT"


@test("Cache-Control: private response is not cached")
async def t_private():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls, _r1, r2 = await _run_twice(
        mw, {}, {"content-type": "text/html", "Cache-Control": "private"}
    )
    assert calls == 2, "private response must not be served from cache"
    assert r2.headers.get("X-Cache") != "HIT"


@test("Cache-Control directives are case-insensitive (No-Cache)")
async def t_no_cache_caseins():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls, _r1, _r2 = await _run_twice(
        mw, {}, {"content-type": "text/html", "Cache-Control": "No-Cache"}
    )
    assert calls == 2, "No-Cache (capitalized) must bypass the cache"


@test("Cache-Control: no-store response is not cached")
async def t_no_store():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls, _r1, _r2 = await _run_twice(
        mw, {}, {"content-type": "text/html", "cache-control": "max-age=0, no-store"}
    )
    assert calls == 2, "no-store must bypass the cache"


@test("Authorization request: response not cached + gains Vary: Authorization")
async def t_authorization_not_cached():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls, r1, r2 = await _run_twice(
        mw, {"Authorization": "Bearer tok"}, {"content-type": "text/html"}
    )
    assert calls == 2, "authenticated response must not be cached"
    assert r2.headers.get("X-Cache") != "HIT"
    assert "authorization" in r1.headers.get("Vary", "").lower()


@test("Authorization request with Cache-Control: public IS cached")
async def t_authorization_public_cached():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls, _r1, r2 = await _run_twice(
        mw,
        {"Authorization": "Bearer tok"},
        {"content-type": "text/html", "Cache-Control": "public"},
    )
    assert calls == 1, "public response should be cached even with Authorization"
    assert r2.headers.get("X-Cache") == "HIT"


@test("Vary: * (whitespace-padded) is not cached")
async def t_vary_star_whitespace():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls, _r1, r2 = await _run_twice(
        mw, {}, {"content-type": "text/html", "Vary": " * "}
    )
    assert calls == 2, "Vary: * (with surrounding whitespace) must bypass the cache"
    assert r2.headers.get("X-Cache") != "HIT"


@test("existing Vary: Authorization is not duplicated")
async def t_vary_not_duplicated():
    mw = CacheMiddleware(LocMemCache(), ttl=60)

    async def call_next(req):
        return FakeResponse(
            headers={"content-type": "text/html", "Vary": "Authorization"}
        )

    r1 = await mw(FakeRequest(headers={"Authorization": "Bearer t"}), call_next)
    assert r1.headers.get("Vary", "").lower().count("authorization") == 1


class _KeyReq:
    """Minimal request for exercising CacheMiddleware._make_key directly."""

    def __init__(self, path="/page", qs="", headers=None, user_id=None):
        self.method = "GET"
        self.path = path
        self.query_string = qs

        class _H:
            def __init__(self, d):
                self._d = {k.lower(): v for k, v in (d or {}).items()}

            def get(self, k, default=None):
                return self._d.get(k.lower(), default)

        self.headers = _H(headers)
        self.user = None if user_id is None else type("U", (), {"id": user_id})()


@test("page-cache key: crafted request cannot forge the per-user suffix")
def t_key_no_cross_user_forgery():
    # cache_authenticated appends `user=<id>` to the key. The untrusted path /
    # query string can contain '|' (the join separator), so a plain "|".join let
    # an UNAUTHENTICATED request forge that suffix and collide with an
    # authenticated victim's key — cross-serving or poisoning their cached page.
    mw = CacheMiddleware(cache=LocMemCache(), cache_authenticated=True)
    victim = mw._make_key(_KeyReq("/page", "x=1", user_id=5))
    forge_qs = mw._make_key(_KeyReq("/page", "x=1|user=5", user_id=None))
    forge_path = mw._make_key(_KeyReq("/page|x=1|user=5", "", user_id=None))
    assert victim != forge_qs, "query-string forged the user= suffix (collision)"
    assert victim != forge_path, "path forged the user= suffix (collision)"

    # Vary-header value is also untrusted; it must not forge the suffix either.
    mw2 = CacheMiddleware(
        cache=LocMemCache(), cache_authenticated=True, vary_headers=["X-Region"]
    )
    v2 = mw2._make_key(_KeyReq("/p", headers={"X-Region": "eu"}, user_id=7))
    a2 = mw2._make_key(_KeyReq("/p|X-Region=eu|user=7", user_id=None))
    assert v2 != a2, "vary-header/path forged the user= suffix (collision)"

    # Legitimate behavior intact: deterministic, and distinct users stay distinct.
    assert mw._make_key(_KeyReq("/a", "b=1", user_id=1)) == mw._make_key(
        _KeyReq("/a", "b=1", user_id=1)
    )
    assert mw._make_key(_KeyReq("/a", user_id=1)) != mw._make_key(
        _KeyReq("/a", user_id=2)
    )


async def main():
    all_tests = [
        obj
        for _name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print("\n═══ Cache Security Tests ═══")
    for t in all_tests:
        await t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)
    return RESULTS["failed"] == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
