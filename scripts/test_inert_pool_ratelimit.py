"""Regression tests for silently-inert rate-limit + native-pool settings.

Covers three fix-wave findings (#8, #9, #10):

  #8  ``RATE_LIMIT_REQUESTS`` takes effect: the async ``RateLimitMiddleware``
      honors the setting for ``max_requests``, and the Django adapter reads
      ``HYPERDJANGO_RATE_LIMIT_REQUESTS``.

  #9  ``LOAD_TEST`` ("disable rate limiting for load tests") was never consulted,
      so bench scripts setting ``HYPER_LOAD_TEST=1`` were still throttled.

  #10 The native ``Database`` pool ignored ``CONNECT_TIMEOUT`` / ``QUERY_TIMEOUT``
      (hardcoded 10000 / 0) and its dedup key omitted ``min_size`` so two pools
      with different min_size silently aliased one native pool.

Run:  uv run hyper-test inert_pool_ratelimit
"""

# hyper-test: unit

import asyncio
from unittest.mock import patch

from hyperdjango.conf import (
    DEFAULT_RATE_LIMIT_MAX_REQUESTS,
    DEFAULT_RATE_LIMIT_WINDOW,
    DEFAULTS,
)

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ── #8: async RateLimitMiddleware honors RATE_LIMIT_REQUESTS / RATE_LIMIT_WINDOW ─


def test_async_middleware_honors_setting() -> None:
    print("\n=== #8 async RateLimitMiddleware honors RATE_LIMIT_REQUESTS ===")
    from hyperdjango.ratelimit import RateLimitMiddleware

    # Default (no explicit arg) must inherit the configured setting, not the
    # module constant. Sanity: the setting differs from the constant so a pass
    # can only come from reading the setting.
    with patch.dict(DEFAULTS, {"RATE_LIMIT_REQUESTS": 250, "RATE_LIMIT_WINDOW": 15}):
        mw = RateLimitMiddleware()
        check(
            "max_requests falls back to RATE_LIMIT_REQUESTS setting (250)",
            mw.max_requests == 250,
            f"got {mw.max_requests}",
        )
        check(
            "window falls back to RATE_LIMIT_WINDOW setting (15)",
            mw.window == 15,
            f"got {mw.window}",
        )
        check(
            "setting differs from module constant (proves setting was read)",
            DEFAULT_RATE_LIMIT_MAX_REQUESTS != 250 and DEFAULT_RATE_LIMIT_WINDOW != 15,
        )

    # Explicit constructor arg must win over the setting.
    with patch.dict(DEFAULTS, {"RATE_LIMIT_REQUESTS": 250, "RATE_LIMIT_WINDOW": 15}):
        mw = RateLimitMiddleware(max_requests=7, window=3)
        check(
            "explicit max_requests wins over setting",
            mw.max_requests == 7,
            f"got {mw.max_requests}",
        )
        check("explicit window wins over setting", mw.window == 3, f"got {mw.window}")


# ── #8: Django adapter reads the correct HYPERDJANGO_RATE_LIMIT_REQUESTS key ───


def _configure_django() -> None:
    import django
    from django.conf import settings as dj_settings

    if not dj_settings.configured:
        dj_settings.configure(
            DEBUG=False,
            DATABASES={},
            INSTALLED_APPS=[],
            HYPERDJANGO_RATE_LIMIT_REQUESTS=333,
        )
        django.setup()


def test_django_adapter_reads_correct_key() -> None:
    print("\n=== #8 Django adapter reads HYPERDJANGO_RATE_LIMIT_REQUESTS ===")
    _configure_django()
    from django.conf import settings as dj_settings

    from hyperdjango.serving.django_middleware import HyperRateLimitMiddleware

    dj_settings.HYPERDJANGO_RATE_LIMIT_REQUESTS = 333
    # A stray HYPERDJANGO_RATE_LIMIT attr is present and DIFFERENT — the adapter
    # reads only HYPERDJANGO_RATE_LIMIT_REQUESTS, so a pass proves it ignores it.
    dj_settings.HYPERDJANGO_RATE_LIMIT = 999

    mw = HyperRateLimitMiddleware(lambda request: None)
    check(
        "limit reads HYPERDJANGO_RATE_LIMIT_REQUESTS (333), ignores stray key (999)",
        mw.limit == 333,
        f"got {mw.limit}",
    )

    # With no HYPERDJANGO_RATE_LIMIT_REQUESTS, the adapter falls to its default
    # (100) — the old HYPERDJANGO_RATE_LIMIT key is not consulted.
    del dj_settings.HYPERDJANGO_RATE_LIMIT_REQUESTS
    dj_settings.HYPERDJANGO_RATE_LIMIT = 222
    mw2 = HyperRateLimitMiddleware(lambda request: None)
    check(
        "unset HYPERDJANGO_RATE_LIMIT_REQUESTS defaults to 100 (old key ignored)",
        mw2.limit == 100,
        f"got {mw2.limit}",
    )


# ── #9: LOAD_TEST bypasses throttling ─────────────────────────────────────────


class _FakeRequest:
    client_ip = "203.0.113.7"
    # Django adapter path (only reached when NOT bypassing).
    META = {"REMOTE_ADDR": "203.0.113.7"}
    method = "GET"
    path = "/"


class _FakeResponse:
    """Minimal downstream response — carries a headers dict so the allowed
    path (set_ratelimit_headers) works on the pass-through."""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.status = 200


async def _run(mw, request):
    """Drive an async middleware once; return the response it produced."""
    marker = _FakeResponse()

    async def call_next(_req):
        return marker

    resp = await mw(request, call_next)
    return resp, marker


def test_async_load_test_bypass() -> None:
    print("\n=== #9 LOAD_TEST bypasses async throttling ===")
    from hyperdjango.ratelimit import RateLimitMiddleware

    req = _FakeRequest()

    async def scenario():
        # WITHOUT LOAD_TEST: max_requests=1 → first allowed, second throttled.
        with patch.dict(DEFAULTS, {"LOAD_TEST": False}):
            mw = RateLimitMiddleware(max_requests=1, window=60)
            r1, marker = await _run(mw, req)
            r2, _ = await _run(mw, req)
            check("throttling active: 1st request allowed", r1 is marker)
            # r2 is a Response (429) when throttled, else the pass-through marker.
            throttled = r2 is not marker and r2.status == 429
            check("throttling active: 2nd request 429", throttled, f"got {r2}")

        # WITH LOAD_TEST: same tiny limit, but every request is allowed.
        with patch.dict(DEFAULTS, {"LOAD_TEST": True}):
            mw = RateLimitMiddleware(max_requests=1, window=60)
            check("mw._load_test wired True from setting", mw._load_test is True)
            allowed = True
            for _ in range(5):
                r, marker = await _run(mw, req)
                allowed = allowed and (r is marker)
            check("LOAD_TEST bypass: all 5 requests allowed", allowed)

    asyncio.run(scenario())


def test_django_load_test_bypass() -> None:
    print("\n=== #9 LOAD_TEST bypasses Django adapter throttling ===")
    _configure_django()
    from hyperdjango.serving.django_middleware import HyperRateLimitMiddleware

    marker = object()

    # No HYPERDJANGO_LOAD_TEST in Django settings, so get_setting("LOAD_TEST")
    # falls through to DEFAULTS — patch it True.
    with patch.dict(DEFAULTS, {"LOAD_TEST": True}):
        mw = HyperRateLimitMiddleware(lambda request: marker)
        check("Django mw._load_test wired True", mw._load_test is True)
        # A request with no usable META would blow up in the throttle path; the
        # bypass must short-circuit before any client-IP logic runs.
        out = mw(object())
        check("LOAD_TEST bypass returns get_response() result", out is marker)


# ── #10: native Database pool honors timeouts + min_size in dedup key ──────────


def test_pool_honors_timeouts_and_min_size() -> None:
    print("\n=== #10 pool honors CONNECT/QUERY timeout + min_size dedup ===")
    import hyperdjango.database as db

    url = "postgresql://u:p@127.0.0.1:5432/inert_test_db"
    captured: list[tuple] = []
    closed: list[int] = []
    handle_counter = [1000]

    def fake_configure(conn_url, max_size, connect_ms, query_ms, max_q, max_life):
        captured.append((conn_url, max_size, connect_ms, query_ms, max_q, max_life))
        handle_counter[0] += 1
        return handle_counter[0]

    def fake_close(handle):
        closed.append(handle)

    # Snapshot + clear the shared registry so pool-count deltas are exact.
    with db._pool_registry_lock:
        saved = dict(db._pool_registry)
        db._pool_registry.clear()

    try:
        with (
            patch.object(db, "_db_configure", fake_configure),
            patch.object(db, "_db_close_pool", fake_close),
            patch.dict(DEFAULTS, {"CONNECT_TIMEOUT": 4321, "QUERY_TIMEOUT": 8765}),
        ):
            # (a) timeouts are plumbed from settings, not hardcoded 10000/0.
            h_a = db._acquire_pool(url, 5, 2)
            check("one _db_configure call so far", len(captured) == 1)
            _, msize, ct, qt, _, _ = captured[-1]
            check("max_size passed through (5)", msize == 5, f"got {msize}")
            check(
                "CONNECT_TIMEOUT honored (4321, not hardcoded 10000)",
                ct == 4321,
                f"got {ct}",
            )
            check(
                "QUERY_TIMEOUT honored (8765, not hardcoded 0)",
                qt == 8765,
                f"got {qt}",
            )

            # (b) same url/max_size, DIFFERENT min_size → NOT deduped: a second
            #     native pool is created with its own handle.
            h_b = db._acquire_pool(url, 5, 9)
            check(
                "different min_size creates a second pool (2 configure calls)",
                len(captured) == 2,
                f"got {len(captured)} calls",
            )
            check(
                "different min_size → distinct pool handle",
                h_a != h_b,
                f"h_a={h_a} h_b={h_b}",
            )
            check(
                "registry now holds 2 distinct pools",
                db.pool_registry_stats()["pools"] == 2,
                f"got {db.pool_registry_stats()}",
            )

            # (c) same url/max_size/min_size → deduped (ref-count bump, no new
            #     _db_configure, same handle).
            h_a2 = db._acquire_pool(url, 5, 2)
            check(
                "same (url,max_size,min_size) dedups: no new configure call",
                len(captured) == 2,
                f"got {len(captured)} calls",
            )
            check("dedup returns the same handle", h_a2 == h_a)

            # Cleanup: release everything and confirm pools close.
            db._release_pool(url, 5, 2)  # ref 2 -> 1
            check(
                "shared pool not closed while a ref remains",
                db.pool_registry_stats()["pools"] == 2,
            )
            db._release_pool(url, 5, 2)  # ref 1 -> 0, closes h_a
            db._release_pool(url, 5, 9)  # ref 1 -> 0, closes h_b
            check(
                "all pools released → registry empty",
                db.pool_registry_stats()["pools"] == 0,
                f"got {db.pool_registry_stats()}",
            )
            check(
                "both distinct handles were closed",
                set(closed) == {h_a, h_b},
                f"closed={closed} h_a={h_a} h_b={h_b}",
            )
    finally:
        with db._pool_registry_lock:
            db._pool_registry.clear()
            db._pool_registry.update(saved)


def run() -> bool:
    test_async_middleware_honors_setting()
    test_django_adapter_reads_correct_key()
    test_async_load_test_bypass()
    test_django_load_test_bypass()
    test_pool_honors_timeouts_and_min_size()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
