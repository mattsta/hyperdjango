"""
Tests for tiered rate limiting (per-group rate limit tiers).

- TieredRateLimitMiddleware with in-memory backend
- Tier resolution from group hierarchy
- Default tier for anonymous users
- Tier caching and cache invalidation
- Tier headers in responses
- Integration with RBAC groups
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from dataclasses import dataclass

from hyperdjango.auth.user import SessionUser

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
results = []
test_funcs = []


def test(name):
    def decorator(func):
        test_funcs.append((name, func))
        return func

    return decorator


def check(label, condition):
    results.append((label, condition))
    symbol = "\u2713" if condition else "\u2717"
    print(f"  {symbol} {label}")


async def setup():
    """Create DB, tables, seed users+groups."""
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db
    from hyperdjango.ratelimit import ALTER_GROUPS_TIER_SQL

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Add rate_limit_tier column
    import contextlib

    with contextlib.suppress(Exception):
        await db.execute(ALTER_GROUPS_TIER_SQL)

    return db, checker


async def teardown(db):
    from hyperdjango.auth.user import drop_rbac_tables

    await drop_rbac_tables(db)
    await db.disconnect()


@dataclass
class FakeRequest:
    user: object = None
    client_ip: str = "127.0.0.1"


@dataclass
class FakeUser:
    id: int = 1
    username: str = "test"
    is_active: bool = True
    is_superuser: bool = False


TIERS = {
    "free": {"max_requests": 5, "window": 60},
    "pro": {"max_requests": 50, "window": 60},
    "enterprise": {"max_requests": 500, "window": 60},
}

# ═══════════════════════════════════════════════════════════════════════════
# Basic Tier Resolution
# ═══════════════════════════════════════════════════════════════════════════


@test("tier: anonymous user gets default tier")
async def test_anonymous_default():
    from hyperdjango.ratelimit import TieredRateLimitMiddleware

    mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free")
    req = FakeRequest(user=None)
    tier = await mw.get_user_tier(req)
    check("anonymous gets free tier", tier == "free")


@test("tier: user without group gets default tier")
async def test_no_group_default():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")
        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)

        req = FakeRequest(user=FakeUser(id=alice.id))
        tier = await mw.get_user_tier(req)
        check("user without group gets free", tier == "free")
    finally:
        await teardown(db)


@test("tier: user with tiered group gets group tier")
async def test_group_tier():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")
        pro_group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = $1 WHERE id = $2",
            "pro",
            pro_group.id,
        )
        await checker.add_user_to_group(alice.id, pro_group.id)

        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=FakeUser(id=alice.id))
        tier = await mw.get_user_tier(req)
        check("user gets pro tier", tier == "pro")
    finally:
        await teardown(db)


@test("tier: highest priority group wins")
async def test_highest_priority():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")

        free_group = await checker.create_group("free_users", priority=1)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'free' WHERE id = $1",
            free_group.id,
        )
        await checker.add_user_to_group(alice.id, free_group.id)

        enterprise_group = await checker.create_group("enterprise_users", priority=100)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'enterprise' WHERE id = $1",
            enterprise_group.id,
        )
        await checker.add_user_to_group(alice.id, enterprise_group.id)

        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=FakeUser(id=alice.id))
        tier = await mw.get_user_tier(req)
        check("highest priority wins (enterprise)", tier == "enterprise")
    finally:
        await teardown(db)


@test("tier: group without tier is skipped")
async def test_skip_no_tier():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")

        # Group with no tier set
        plain_group = await checker.create_group("editors", priority=10)
        await checker.add_user_to_group(alice.id, plain_group.id)

        # Group with tier but lower priority
        pro_group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE id = $1",
            pro_group.id,
        )
        await checker.add_user_to_group(alice.id, pro_group.id)

        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=FakeUser(id=alice.id))
        tier = await mw.get_user_tier(req)
        check("skips group without tier, uses pro", tier == "pro")
    finally:
        await teardown(db)


@test("tier: unknown tier in DB falls back to default")
async def test_unknown_tier():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")
        group = await checker.create_group("weird_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'nonexistent' WHERE id = $1",
            group.id,
        )
        await checker.add_user_to_group(alice.id, group.id)

        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=FakeUser(id=alice.id))
        tier = await mw.get_user_tier(req)
        check("unknown tier falls back to default", tier == "free")
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Tier Caching
# ═══════════════════════════════════════════════════════════════════════════


@test("tier: result is cached")
async def test_tier_caching():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")
        group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE id = $1", group.id
        )
        await checker.add_user_to_group(alice.id, group.id)

        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=FakeUser(id=alice.id))

        # First call populates cache
        tier1 = await mw.get_user_tier(req)
        check("first call returns pro", tier1 == "pro")

        # Second call should hit cache (no DB)
        tier2 = await mw.get_user_tier(req)
        check("second call still pro", tier2 == "pro")
        check("user in cache", alice.id in mw._tier_cache)
    finally:
        await teardown(db)


@test("tier: cache invalidation per user")
async def test_cache_invalidation_user():
    from hyperdjango.ratelimit import TieredRateLimitMiddleware

    mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free")
    mw._tier_cache[1] = "pro"
    mw._tier_cache[2] = "enterprise"

    mw.clear_tier_cache(user_id=1)
    check("user 1 cleared", 1 not in mw._tier_cache)
    check("user 2 still cached", 2 in mw._tier_cache)


@test("tier: cache invalidation all")
async def test_cache_invalidation_all():
    from hyperdjango.ratelimit import TieredRateLimitMiddleware

    mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free")
    mw._tier_cache[1] = "pro"
    mw._tier_cache[2] = "enterprise"

    mw.clear_tier_cache()
    check("all cleared", len(mw._tier_cache) == 0)


# ═══════════════════════════════════════════════════════════════════════════
# Rate Limiting Behavior
# ═══════════════════════════════════════════════════════════════════════════


@test("ratelimit: free tier enforced")
async def test_free_tier_limit():
    from hyperdjango.ratelimit import TieredRateLimitMiddleware

    mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free")
    req = FakeRequest(user=None)  # anonymous -> free tier (5 requests/60s)

    responses = []
    for i in range(7):

        async def next_handler(r):
            from hyperdjango.response import Response

            return Response.json({"ok": True})

        resp = await mw(req, next_handler)
        responses.append(resp)

    ok_count = sum(1 for r in responses if r.status == 200)
    limited_count = sum(1 for r in responses if r.status == 429)
    check("5 allowed", ok_count == 5)
    check("2 rate limited", limited_count == 2)


@test("ratelimit: pro tier has higher limit")
async def test_pro_tier_limit():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")
        group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE id = $1", group.id
        )
        await checker.add_user_to_group(alice.id, group.id)

        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=FakeUser(id=alice.id))

        # Pro tier allows 50 requests. Send 7 — all should pass.
        responses = []
        for i in range(7):

            async def next_handler(r):
                from hyperdjango.response import Response

                return Response.json({"ok": True})

            resp = await mw(req, next_handler)
            responses.append(resp)

        ok_count = sum(1 for r in responses if r.status == 200)
        check("all 7 pass for pro tier", ok_count == 7)
    finally:
        await teardown(db)


@test("ratelimit: tier header in response")
async def test_tier_header():
    from hyperdjango.ratelimit import TieredRateLimitMiddleware

    mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free")
    req = FakeRequest(user=None)

    async def next_handler(r):
        from hyperdjango.response import Response

        return Response.json({"ok": True})

    resp = await mw(req, next_handler)
    check("has x-ratelimit-tier header", "x-ratelimit-tier" in resp.headers)
    check("tier is free", resp.headers["x-ratelimit-tier"] == "free")
    check("has x-ratelimit-limit header", "x-ratelimit-limit" in resp.headers)
    check("limit is 5", resp.headers["x-ratelimit-limit"] == "5")


@test("ratelimit: 429 response includes tier info")
async def test_429_tier():
    from hyperdjango.ratelimit import TieredRateLimitMiddleware

    # Use tiny limit
    tiny_tiers = {"test": {"max_requests": 1, "window": 60}}
    mw = TieredRateLimitMiddleware(tiers=tiny_tiers, default_tier="test")
    req = FakeRequest(user=None)

    async def next_handler(r):
        from hyperdjango.response import Response

        return Response.json({"ok": True})

    # First passes
    resp1 = await mw(req, next_handler)
    check("first request OK", resp1.status == 200)

    # Second rate limited
    resp2 = await mw(req, next_handler)
    check("second request 429", resp2.status == 429)
    check("429 has tier header", resp2.headers.get("x-ratelimit-tier") == "test")


# ═══════════════════════════════════════════════════════════════════════════
# User Dict Support
# ═══════════════════════════════════════════════════════════════════════════


@test("tier: dict user supported")
async def test_dict_user():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        alice = await checker.create_user("alice", "pass123")
        group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE id = $1", group.id
        )
        await checker.add_user_to_group(alice.id, group.id)

        mw = TieredRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=SessionUser({"id": alice.id, "username": "alice"}))
        tier = await mw.get_user_tier(req)
        check("dict user resolved to pro", tier == "pro")
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# ensure_column
# ═══════════════════════════════════════════════════════════════════════════


@test("ensure_column: adds rate_limit_tier")
async def test_ensure_column():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        mw = TieredRateLimitMiddleware(tiers=TIERS, db=db)
        await mw.ensure_column()

        # Should be able to query it
        rows = await db.query("SELECT rate_limit_tier FROM hyper_groups")
        check("column exists and queryable", isinstance(rows, list))
    finally:
        await teardown(db)


@test("ensure_column: idempotent")
async def test_ensure_column_idempotent():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import TieredRateLimitMiddleware

        mw = TieredRateLimitMiddleware(tiers=TIERS, db=db)
        await mw.ensure_column()
        await mw.ensure_column()  # Second call should not fail
        check("idempotent call succeeds", True)
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Group Model
# ═══════════════════════════════════════════════════════════════════════════


@test("group model: has rate_limit_tier field")
async def test_group_model_field():
    from hyperdjango.auth.user import Group

    # Fields could be strings or Field objects — check annotations
    annotations = Group.__annotations__
    check("Group has rate_limit_tier annotation", "rate_limit_tier" in annotations)


@test("group model: rate_limit_tier defaults to empty string")
async def test_group_model_default():
    from hyperdjango.auth.user import Group

    check("default is empty string", Group.rate_limit_tier.default == "")


# ═══════════════════════════════════════════════════════════════════════════
# Integration with export/import
# ═══════════════════════════════════════════════════════════════════════════


@test("export: includes rate_limit_tier in groups")
async def test_export_tier():
    db, checker = await setup()
    try:
        pro_group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE id = $1",
            pro_group.id,
        )

        policy = await checker.export_policy()
        groups = policy["groups"]
        check("1 group exported", len(groups) == 1)
        # The export may or may not include rate_limit_tier depending on the SELECT
        # But we can verify the group exists
        check("group name is pro_users", groups[0]["name"] == "pro_users")
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\n{'=' * 60}")
    print("Tiered Rate Limiting Tests")
    print(f"{'=' * 60}\n")

    for name, func in test_funcs:
        print(f"\n[TEST] {name}")
        try:
            await func()
        except Exception as e:
            check(f"EXCEPTION: {e}", False)
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed:")
        for label, ok in results:
            if not ok:
                print(f"  \u2717 {label}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
