#!/usr/bin/env python3
"""Test pluggable rate limiter with multi-tenant support.

Tests:
1. InMemoryBackend — sliding window, check_and_increment, reset
2. DatabaseBackend — UNLOGGED table, atomic upsert, multi-server coordination
3. Key strategies — ip_key, user_key, org_key, composite_key
4. RateLimitMiddleware — with in-memory backend
5. RateLimitMiddleware — with database backend
6. Multi-tenant rate limiting (per-user, per-org)
7. Hierarchical rate limits (stack middlewares)
8. Rate limit headers (X-RateLimit-*)
9. UNLOGGED table verification

Run: uv run hyper-test ratelimit
Requires: PostgreSQL running, DATABASE_URL or default hyperdjango_test
"""

# hyper-test: db_isolated

import asyncio
import os
import sys

from hyperdjango.auth.user import SessionUser
from hyperdjango.database import Database, set_db
from hyperdjango.ratelimit import (
    DatabaseRateLimitBackend,
    InMemoryRateLimitBackend,
    RateLimitMiddleware,
    composite_key,
    ip_key,
    org_key,
    user_key,
)
from hyperdjango.request import Request
from hyperdjango.response import Response

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://localhost/hyperdjango_test",
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


def make_request(ip="1.2.3.4", user=None, method="GET", path="/"):
    """Create a test request with optional user context.

    client_ip now derives from the socket peer (ASGI scope) by default —
    X-Forwarded-For is only trusted behind a configured proxy — so set the peer
    address rather than a spoofable header.
    """
    scope = {"client": (ip, 12345)} if ip else None
    req = Request(
        method=method, path=path, headers={}, query_string="", body=b"", scope=scope
    )
    req.user = user
    return req


async def test_in_memory_backend():
    """Test InMemoryRateLimitBackend."""
    print("\n=== InMemoryBackend ===")

    backend = InMemoryRateLimitBackend()

    # First request — allowed
    allowed, remaining, reset = backend.check_and_increment("test:1", 3, 60)
    check("first request allowed", allowed)
    check("remaining is 2", remaining == 2, f"got {remaining}")

    # Second request
    allowed, remaining, _ = backend.check_and_increment("test:1", 3, 60)
    check("second request allowed", allowed)
    check("remaining is 1", remaining == 1)

    # Third request
    allowed, remaining, _ = backend.check_and_increment("test:1", 3, 60)
    check("third request allowed", allowed)
    check("remaining is 0", remaining == 0)

    # Fourth request — rate limited
    allowed, remaining, reset = backend.check_and_increment("test:1", 3, 60)
    check("fourth request blocked", not allowed)
    check("remaining is 0 when blocked", remaining == 0)
    check("reset is positive", reset > 0)

    # Different key — independent
    allowed, remaining, _ = backend.check_and_increment("test:2", 3, 60)
    check("different key allowed", allowed)
    check("different key full remaining", remaining == 2)

    # Reset
    backend.reset("test:1")
    allowed, _, _ = backend.check_and_increment("test:1", 3, 60)
    check("reset allows again", allowed)


async def test_in_memory_window_expiry():
    """Test that in-memory rate limits expire after window."""
    print("\n=== InMemory Window Expiry ===")

    backend = InMemoryRateLimitBackend()

    # Fill up with 1-second window
    for _ in range(3):
        backend.check_and_increment("expire:1", 3, 1)

    allowed, _, _ = backend.check_and_increment("expire:1", 3, 1)
    check("blocked when full", not allowed)

    # timing-window: window ageing — the claim IS "a full idle window has
    # elapsed", so real elapsed time is the thing under test and there is no
    # earlier state to wait for. Polling with check_and_increment would not do:
    # the first call the bucket happens to admit can land mid-refill, before the
    # full quota is back, and would report a smaller `remaining` — the poll
    # would be measuring the machine just as much as the sleep did. Overshoot is
    # safe by construction: further idleness keeps rolling the window with zero
    # admissions and keeps the bucket full, so a runner that sleeps 10s instead
    # of 1.2 gets exactly the same answer.
    await asyncio.sleep(1.2)

    allowed, remaining, _ = backend.check_and_increment("expire:1", 3, 1)
    check("allowed after window expires", allowed)
    check("remaining reset", remaining == 2)


async def test_in_memory_window_cap():
    """No more than N units may be admitted within any single fixed window.

    Guards the regression where a plain token bucket admitted ~2x the limit
    within one window (initial full bucket + a window's worth of continuous
    refill). Two checks:

    1. A tight (instant) burst of 3N at several phase offsets — each burst lands
       in one fixed window and must admit exactly N (no full-bucket burst).
    2. Refill must not defeat the cap: after admitting N, waiting inside the
       SAME window (while tokens visibly refill) admits nothing more. Aligned to
       a window boundary so the sub-test provably stays within one window.

    (A burst straddling a boundary can total up to 2N across the two adjacent
    windows — the standard fixed-window property — so the guarantee is <= N per
    fixed window, matching the windowed-token-bucket design.)
    """
    print("\n=== InMemory Window Cap (<= N per fixed window) ===")

    import time as _time

    N = 10

    # 1. Instant tight loop at multiple phase offsets — each lands in one window.
    W = 2  # seconds
    for offset in (0.0, 0.3, 0.7, 1.1, 1.6):
        await asyncio.sleep(offset)
        backend = InMemoryRateLimitBackend()
        key = f"cap:{offset}"
        admitted = sum(
            1 for _ in range(N * 3) if backend.check_and_increment(key, N, W)[0]
        )
        check(
            f"instant burst admits exactly N at offset {offset} (got {admitted})",
            admitted == N,
            f"admitted {admitted}, expected {N}",
        )

    # 2. Refill-within-window: use a long window and align to just past a
    #    boundary so the whole sub-test (< 1s) provably stays in one window.
    #
    #    The observation must land inside ONE fixed window or it proves nothing:
    #    a NEW window legitimately admits N again. Alignment makes that likely,
    #    not certain — a loaded runner can oversleep the 0.5s wait clean past the
    #    boundary, and the old code would then have reported a correct limiter as
    #    broken. So the window index is read on both sides of the wait and the
    #    attempt is retried when it crossed, instead of asserting on a premise
    #    that no longer holds. `more` stays -1 if no attempt ever lands cleanly,
    #    which fails loudly rather than silently skipping the check.
    WL = 4  # seconds — generous headroom after alignment
    first = -1
    more = -1
    for _attempt in range(6):
        while (_time.monotonic() % WL) > 0.05:
            await asyncio.sleep(0.005)
        backend = InMemoryRateLimitBackend()
        first = sum(
            1 for _ in range(N) if backend.check_and_increment("cap:refill", N, WL)[0]
        )
        window_before = _time.monotonic() // WL
        # timing-window: a bounded NEGATIVE inside one fixed window — tokens
        # visibly refill across this wait and must still admit nothing more.
        # Nothing becomes true when an admission fails to happen, so a window is
        # the only construct; the boundary re-check below makes overshoot safe.
        await asyncio.sleep(0.5)
        if _time.monotonic() // WL != window_before:
            continue  # overslept into a new window — premise gone, retry
        more = sum(
            1 for _ in range(N) if backend.check_and_increment("cap:refill", N, WL)[0]
        )
        break
    check(f"first N admitted (got {first})", first == N, f"admitted {first}")
    check(
        f"refill does not exceed the per-window cap (extra admitted={more})",
        more == 0,
        f"admitted {more} extra within the same window",
    )


async def test_key_strategies():
    """Test key strategy functions."""
    print("\n=== Key Strategies ===")

    # IP key
    req = make_request(ip="10.0.0.1")
    check("ip_key", ip_key(req) == "ip:10.0.0.1")

    # User key (session user)
    req = make_request(user=SessionUser({"id": 42, "username": "alice"}))
    check("user_key with dict", user_key(req) == "user:42")

    # User key (no user falls back to IP)
    req = make_request(ip="10.0.0.2")
    check("user_key no user → IP", user_key(req) == "ip:10.0.0.2")

    # Org key
    req = make_request(user=SessionUser({"id": 1, "org_id": 5}))
    check("org_key", org_key(req) == "org:5")

    # Org key (no org falls back to user)
    req = make_request(user=SessionUser({"id": 1}))
    check("org_key no org → user", org_key(req) == "user:1")

    # Org key with tenant_id
    req = make_request(user=SessionUser({"id": 1, "tenant_id": "acme"}))
    check("org_key with tenant_id", org_key(req) == "org:acme")

    # Composite key
    ck = composite_key(org_key, user_key)
    req = make_request(user=SessionUser({"id": 42, "org_id": 5}))
    key = ck(req)
    check("composite_key combines", "org:5" in key and "user:42" in key)


async def test_middleware_inmemory():
    """Test RateLimitMiddleware with in-memory backend."""
    print("\n=== Middleware + InMemory ===")

    middleware = RateLimitMiddleware(max_requests=3, window=60)

    async def handler(req):
        return Response.json({"ok": True})

    # Make 3 allowed requests
    for i in range(3):
        req = make_request(ip="5.5.5.5")
        resp = await middleware(req, handler)
        check(f"request {i + 1} allowed", resp.status == 200)

    # 4th blocked
    req = make_request(ip="5.5.5.5")
    resp = await middleware(req, handler)
    check("4th request blocked (429)", resp.status == 429)

    # Check headers
    check("has X-RateLimit-Limit", resp.headers.get("x-ratelimit-limit") == "3")
    check("has X-RateLimit-Remaining", resp.headers.get("x-ratelimit-remaining") == "0")
    check("has X-RateLimit-Reset", "x-ratelimit-reset" in resp.headers)
    check("has Retry-After", "retry-after" in resp.headers)

    # Different IP is allowed
    req = make_request(ip="6.6.6.6")
    resp = await middleware(req, handler)
    check("different IP allowed", resp.status == 200)


async def test_middleware_user_key():
    """Test middleware with user-based key function."""
    print("\n=== Middleware + User Key ===")

    middleware = RateLimitMiddleware(
        max_requests=2,
        window=60,
        key_func=user_key,
    )

    async def handler(req):
        return Response.json({"ok": True})

    # User 1 makes 2 requests
    for _ in range(2):
        req = make_request(user=SessionUser({"id": 100}))
        resp = await middleware(req, handler)

    # User 1 blocked
    req = make_request(user=SessionUser({"id": 100}))
    resp = await middleware(req, handler)
    check("user 100 blocked", resp.status == 429)

    # User 2 still allowed
    req = make_request(user=SessionUser({"id": 200}))
    resp = await middleware(req, handler)
    check("user 200 allowed", resp.status == 200)


async def test_db_backend(db):
    """Test DatabaseRateLimitBackend."""
    print("\n=== DatabaseBackend ===")

    backend = DatabaseRateLimitBackend(db)
    await backend.ensure_table()
    await db.execute("DELETE FROM hyper_rate_limits")

    # First request
    allowed, remaining, reset = await backend.check_and_increment("db:1", 3, 60)
    check("first request allowed", allowed)
    check("remaining is 2", remaining == 2, f"got {remaining}")

    # Fill up
    await backend.check_and_increment("db:1", 3, 60)
    await backend.check_and_increment("db:1", 3, 60)

    # Blocked
    allowed, remaining, _ = await backend.check_and_increment("db:1", 3, 60)
    check("blocked after 3", not allowed)

    # Different key
    allowed, _, _ = await backend.check_and_increment("db:2", 3, 60)
    check("different key allowed", allowed)

    # Reset
    await backend.reset("db:1")
    allowed, _, _ = await backend.check_and_increment("db:1", 3, 60)
    check("reset allows again", allowed)

    # Usage stats
    usage = await backend.get_usage("db:1", 60)
    check("usage count is 1", usage["count"] == 1, f"got {usage}")


async def test_middleware_db_backend(db):
    """Test RateLimitMiddleware with database backend."""
    print("\n=== Middleware + DatabaseBackend ===")

    backend = DatabaseRateLimitBackend(db)
    await db.execute("DELETE FROM hyper_rate_limits")

    middleware = RateLimitMiddleware(
        max_requests=2,
        window=60,
        key_func=lambda r: f"mw:{r.client_ip}",
        backend=backend,
    )

    async def handler(req):
        return Response.json({"ok": True})

    # 2 allowed
    for _ in range(2):
        req = make_request(ip="7.7.7.7")
        resp = await middleware(req, handler)
    check("2 requests allowed", resp.status == 200)

    # 3rd blocked
    req = make_request(ip="7.7.7.7")
    resp = await middleware(req, handler)
    check("3rd request blocked", resp.status == 429)

    # Headers present
    check("limit header", resp.headers.get("x-ratelimit-limit") == "2")


async def test_db_backend_cleanup(db):
    """Test database backend cleanup of old entries."""
    print("\n=== DatabaseBackend Cleanup ===")

    backend = DatabaseRateLimitBackend(db)
    await db.execute("DELETE FROM hyper_rate_limits")

    # Insert some entries
    await backend.check_and_increment("cleanup:1", 100, 60)
    await backend.check_and_increment("cleanup:2", 100, 60)

    count = await db.query_val("SELECT COUNT(*) FROM hyper_rate_limits")
    check("entries exist", count >= 2, f"got {count}")

    # Cleanup shouldn't remove recent entries
    await backend.cleanup()
    count_after = await db.query_val("SELECT COUNT(*) FROM hyper_rate_limits")
    check("recent entries kept", count_after >= 2)


async def test_db_unlogged(db):
    """Verify rate limits table is UNLOGGED."""
    print("\n=== UNLOGGED Table ===")

    row = await db.query_one(
        "SELECT relpersistence FROM pg_class WHERE relname = 'hyper_rate_limits'"
    )
    if row:
        check(
            "rate limits table is UNLOGGED",
            row.get("relpersistence") == "u",
            f"got {row.get('relpersistence')}",
        )
    else:
        check("hyper_rate_limits exists", False)


async def test_multi_tenant_scenario(db):
    """Test multi-tenant rate limiting scenario."""
    print("\n=== Multi-Tenant Scenario ===")

    backend = DatabaseRateLimitBackend(db)
    await db.execute("DELETE FROM hyper_rate_limits")

    # Org-level rate limit: 5 requests per org per minute
    middleware = RateLimitMiddleware(
        max_requests=5,
        window=60,
        key_func=org_key,
        backend=backend,
    )

    async def handler(req):
        return Response.json({"ok": True})

    # User 1 in org 10 makes 3 requests
    for _ in range(3):
        req = make_request(user=SessionUser({"id": 1, "org_id": 10}))
        await middleware(req, handler)

    # User 2 in SAME org 10 makes 2 more — total 5
    for _ in range(2):
        req = make_request(user=SessionUser({"id": 2, "org_id": 10}))
        await middleware(req, handler)

    # User 3 in SAME org 10 — blocked (org limit reached)
    req = make_request(user=SessionUser({"id": 3, "org_id": 10}))
    resp = await middleware(req, handler)
    check("org limit blocks all users in org", resp.status == 429)

    # User in DIFFERENT org 20 — allowed
    req = make_request(user=SessionUser({"id": 4, "org_id": 20}))
    resp = await middleware(req, handler)
    check("different org allowed", resp.status == 200)


async def main():
    global passed, failed

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    try:
        await db.execute("DROP TABLE IF EXISTS hyper_rate_limits CASCADE")

        await test_in_memory_backend()
        await test_in_memory_window_expiry()
        await test_in_memory_window_cap()
        await test_key_strategies()
        await test_middleware_inmemory()
        await test_middleware_user_key()
        await test_db_backend(db)
        await test_middleware_db_backend(db)
        await test_db_backend_cleanup(db)
        await test_db_unlogged(db)
        await test_multi_tenant_scenario(db)
    finally:
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All rate limiter tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
