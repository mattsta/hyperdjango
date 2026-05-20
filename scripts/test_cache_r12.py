# hyper-test: unit
"""
Regression tests for the cache fix-wave (round 12).

Covers four independent findings:

  #3  CacheMiddleware cross-token leak — a `Cache-Control: public` +
      `Vary: Authorization` response must be keyed per-token (the Authorization
      request value is folded, hashed, into the cache key) so it can never be
      cross-served to a different bearer under a path-only key.

  #4  CacheMiddleware cross-user leak — when a session cookie is present but
      request.user is unresolved (None), the auth state is indeterminate; the
      middleware must refuse to serve or store an anonymous cache entry
      regardless of middleware ordering.

  #5  get_cache() honours CACHE_BACKEND — a backend registered via
      register_adapter() is actually instantiated and returned instead of the
      docstring being a lie (it previously always built a LocMemCache).

  #15 ConsistentHashRing.add_node() applies weight_fn — a dynamically added
      shard receives its weight-scaled vnode count, not a hardcoded weight of 1.

Usage:
    uv run hyper-test cache_r12
"""

import asyncio
import inspect
import sys
import traceback
from unittest.mock import patch

import hyperdjango.cache as cache_module
from hyperdjango.cache import LocMemCache, get_cache
from hyperdjango.cache_adapters import (
    CacheMiddleware,
    ConsistentHashRing,
    register_adapter,
)
from hyperdjango.conf import DEFAULTS

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
    def __init__(self, headers=None, path="/page", user=None):
        self.method = "GET"
        self.path = path
        self.query_string = ""
        self.headers = dict(headers or {})
        self.user = user


class FakeResponse:
    def __init__(self, status_code=200, body=b"<h1>ok</h1>", headers=None):
        self.status_code = status_code
        self.body = body
        self.headers = dict(headers or {"content-type": "text/html"})


# ---------------------------------------------------------------------------
# #3 — cross-token leak
# ---------------------------------------------------------------------------


@test("#3 public + Vary:Authorization does NOT cross-serve across tokens")
async def t_no_cross_token_serve():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls = 0

    async def call_next(req):
        nonlocal calls
        calls += 1
        token = req.headers.get("Authorization", "none")
        # Personalized body keyed to the caller's token.
        return FakeResponse(
            body=f"secret-for-{token}".encode(),
            headers={
                "content-type": "text/html",
                "Cache-Control": "public",
                "Vary": "Authorization",
            },
        )

    r1 = await mw(FakeRequest(headers={"Authorization": "Bearer AAA"}), call_next)
    r2 = await mw(FakeRequest(headers={"Authorization": "Bearer BBB"}), call_next)

    b1 = r1.body.decode() if isinstance(r1.body, bytes) else r1.body
    b2 = r2.body.decode() if isinstance(r2.body, bytes) else r2.body

    assert calls == 2, f"different tokens must each hit the handler, got {calls}"
    assert b2 == "secret-for-Bearer BBB", f"token BBB was cross-served: {b2!r}"
    assert r2.headers.get("X-Cache") != "HIT", (
        "token BBB served a cached token-AAA body"
    )


@test("#3 same token still caches per-token (legitimate hit preserved)")
async def t_same_token_hits():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls = 0

    async def call_next(req):
        nonlocal calls
        calls += 1
        return FakeResponse(
            headers={
                "content-type": "text/html",
                "Cache-Control": "public",
                "Vary": "Authorization",
            }
        )

    await mw(FakeRequest(headers={"Authorization": "Bearer SAME"}), call_next)
    r2 = await mw(FakeRequest(headers={"Authorization": "Bearer SAME"}), call_next)

    assert calls == 1, f"identical token should be a cache hit, got {calls} calls"
    assert r2.headers.get("X-Cache") == "HIT"


# ---------------------------------------------------------------------------
# #4 — indeterminate auth state
# ---------------------------------------------------------------------------


@test("#4 session cookie + unresolved user is not served the anonymous body")
async def t_indeterminate_auth_not_served():
    session_name = DEFAULTS["SESSION_COOKIE_NAME"]
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls = 0

    async def call_next(req):
        nonlocal calls
        calls += 1
        return FakeResponse(body=b"ANON-PUBLIC-BODY")

    # 1) Genuinely anonymous request (no cookie, user None) populates the cache.
    r_anon = await mw(FakeRequest(headers={}), call_next)
    assert calls == 1
    assert r_anon.headers.get("X-Cache") == "MISS"

    # 2) A request carrying a session cookie but with user unresolved (None):
    #    auth state is indeterminate — must NOT be served the cached anon body.
    r_cookie = await mw(
        FakeRequest(headers={"Cookie": f"{session_name}=abc123; other=x"}),
        call_next,
    )
    assert calls == 2, (
        "indeterminate-auth request must bypass the cache (re-run handler)"
    )
    assert r_cookie.headers.get("X-Cache") != "HIT", (
        "anon entry leaked to cookie-bearing client"
    )


@test("#4 anonymous request without a session cookie still caches normally")
async def t_plain_anon_still_cached():
    mw = CacheMiddleware(LocMemCache(), ttl=60)
    calls = 0

    async def call_next(req):
        nonlocal calls
        calls += 1
        return FakeResponse()

    await mw(FakeRequest(headers={"Cookie": "csrftoken=xyz"}), call_next)
    r2 = await mw(FakeRequest(headers={"Cookie": "csrftoken=xyz"}), call_next)
    assert calls == 1, "non-session cookie must not disable caching"
    assert r2.headers.get("X-Cache") == "HIT"


# ---------------------------------------------------------------------------
# #5 — CACHE_BACKEND selects the adapter
# ---------------------------------------------------------------------------


class _MockBackend:
    """Minimal registered adapter, constructible with no arguments."""

    _is_async = False

    def get(self, key, default=None):
        return default

    def set(self, key, value, ttl=None):
        pass

    def delete(self, key):
        return False

    def clear(self):
        pass

    def has(self, key):
        return False


@test("#5 get_cache() instantiates the adapter named by CACHE_BACKEND")
def t_backend_selects_adapter():
    register_adapter("mock_r12", _MockBackend)
    saved = cache_module._default_cache
    try:
        cache_module._default_cache = None  # clear the memoized default
        with patch.dict(DEFAULTS, {"CACHE_BACKEND": "mock_r12"}):
            c = get_cache()
        assert isinstance(c, _MockBackend), f"expected _MockBackend, got {type(c)}"
    finally:
        cache_module._default_cache = saved


@test("#5 default/'memory' backend still yields LocMemCache")
def t_backend_default_locmem():
    saved = cache_module._default_cache
    try:
        cache_module._default_cache = None
        with patch.dict(DEFAULTS, {"CACHE_BACKEND": "memory"}):
            c = get_cache()
        assert isinstance(c, LocMemCache), f"expected LocMemCache, got {type(c)}"
    finally:
        cache_module._default_cache = saved


@test("#5 unregistered backend (e.g. unconfigured 'database') falls back to LocMem")
def t_backend_unregistered_fallback():
    saved = cache_module._default_cache
    try:
        cache_module._default_cache = None
        with patch.dict(DEFAULTS, {"CACHE_BACKEND": "database"}):
            c = get_cache()
        assert isinstance(c, LocMemCache), (
            f"expected LocMemCache fallback, got {type(c)}"
        )
    finally:
        cache_module._default_cache = saved


# ---------------------------------------------------------------------------
# #15 — add_node applies weight_fn
# ---------------------------------------------------------------------------


def _route_counts(ring, n=3000):
    counts = {}
    for i in range(n):
        name = ring.get_node_name(f"key:{i}")
        counts[name] = counts.get(name, 0) + 1
    return counts


@test("#15 dynamically add_node()ed shard gets weight_fn-scaled distribution")
def t_add_node_applies_weight_fn():
    weights = {"heavy": 8, "light": 1}
    ring = ConsistentHashRing(weight_fn=lambda name: weights[name])

    # Added dynamically WITHOUT an explicit weight — must consult weight_fn.
    ring.add_node("heavy", LocMemCache())
    ring.add_node("light", LocMemCache())

    counts = _route_counts(ring)
    heavy = counts.get("heavy", 0)
    light = counts.get("light", 0)
    assert heavy > light * 2, (
        f"weight_fn ignored by add_node: heavy={heavy} light={light} "
        f"(expected heavy >> light)"
    )


@test("#15 without weight_fn, add_node()ed shards stay balanced (control)")
def t_add_node_no_weight_fn_balanced():
    ring = ConsistentHashRing()
    ring.add_node("a", LocMemCache())
    ring.add_node("b", LocMemCache())
    counts = _route_counts(ring)
    a, b = counts.get("a", 0), counts.get("b", 0)
    # Roughly balanced — neither dominates the way an 8:1 weight would.
    assert a > 0 and b > 0
    ratio = max(a, b) / min(a, b)
    assert ratio < 2.5, f"unexpectedly skewed without weight_fn: a={a} b={b}"


@test("#15 explicit weight arg still overrides weight_fn")
def t_add_node_explicit_weight_wins():
    ring = ConsistentHashRing(weight_fn=lambda name: 99)
    ring.add_node("x", LocMemCache(), weight=1)
    assert ring._weights["x"] == 1, f"explicit weight ignored: {ring._weights['x']}"


async def main():
    all_tests = [
        obj
        for _name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print("\n═══ Cache Round-12 Fix-Wave Tests ═══")
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
