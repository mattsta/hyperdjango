"""
Tests for rule-based multi-dimensional rate limiting.

- Rule matching: path patterns, methods, tiers
- Priority resolution
- Cost-based limits (expensive endpoints consume more quota)
- Tier integration (free/pro/enterprise with per-endpoint overrides)
- Fallback to tier defaults when no rule matches
- Rule caching with TTL
- Combined scenarios (POST reports costs 5 for free, 1 for enterprise)
- Backend increment support
- Response headers (tier, rule, cost)
- Admin registration
"""

# hyper-test: db_isolated

import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass

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
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import drop_rbac_tables
    from hyperdjango.database import Database, set_db
    from hyperdjango.ratelimit import (
        ALTER_GROUPS_TIER_SQL,
        CREATE_RATELIMIT_RULES_TABLE_SQL,
    )

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await drop_rbac_tables(db)
    with contextlib.suppress(Exception):
        await db.execute("DROP TABLE IF EXISTS hyper_rate_limit_rules CASCADE")

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    with contextlib.suppress(Exception):
        await db.execute(ALTER_GROUPS_TIER_SQL)
    with contextlib.suppress(Exception):
        await db.execute(CREATE_RATELIMIT_RULES_TABLE_SQL)

    return db, checker


async def teardown(db):
    from hyperdjango.auth.user import drop_rbac_tables

    with contextlib.suppress(Exception):
        await db.execute("DROP TABLE IF EXISTS hyper_rate_limit_rules CASCADE")
    await drop_rbac_tables(db)
    await db.disconnect()


@dataclass
class FakeRequest:
    user: object = None
    client_ip: str = "127.0.0.1"
    path: str = "/"
    method: str = "GET"


@dataclass
class FakeUser:
    id: int = 1
    username: str = "test"
    is_active: bool = True
    is_superuser: bool = False


TIERS = {
    "free": {"max_requests": 100, "window": 60},
    "pro": {"max_requests": 1000, "window": 60},
    "enterprise": {"max_requests": 10000, "window": 60},
}


async def next_handler(r):
    from hyperdjango.response import Response

    return Response.json({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════
# match_rule() — pure function tests
# ═══════════════════════════════════════════════════════════════════════════


@test("match: wildcard matches everything")
async def test_match_wildcard():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {"path_pattern": "*", "method": "*", "tier": "*", "is_active": True, "id": 1}
    ]
    result = match_rule(rules, "/api/users", "GET", "free")
    check("wildcard matches", result is not None)
    check("correct rule id", result["id"] == 1)


@test("match: exact path")
async def test_match_exact_path():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {
            "path_pattern": "/api/reports",
            "method": "*",
            "tier": "*",
            "is_active": True,
            "id": 1,
        },
        {"path_pattern": "*", "method": "*", "tier": "*", "is_active": True, "id": 2},
    ]
    result = match_rule(rules, "/api/reports", "GET", "free")
    check("exact path matches", result["id"] == 1)

    result2 = match_rule(rules, "/api/users", "GET", "free")
    check("other path falls to wildcard", result2["id"] == 2)


@test("match: prefix pattern with wildcard")
async def test_match_prefix():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {
            "path_pattern": "/api/reports*",
            "method": "*",
            "tier": "*",
            "is_active": True,
            "id": 1,
        },
    ]
    check(
        "matches /api/reports",
        match_rule(rules, "/api/reports", "GET", "free") is not None,
    )
    check(
        "matches /api/reports/123",
        match_rule(rules, "/api/reports/123", "GET", "free") is not None,
    )
    check("no match /api/users", match_rule(rules, "/api/users", "GET", "free") is None)


@test("match: glob pattern with wildcard segment")
async def test_match_glob():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {
            "path_pattern": "/api/*/export",
            "method": "*",
            "tier": "*",
            "is_active": True,
            "id": 1,
        },
    ]
    check(
        "matches /api/users/export",
        match_rule(rules, "/api/users/export", "GET", "free") is not None,
    )
    check(
        "matches /api/reports/export",
        match_rule(rules, "/api/reports/export", "GET", "free") is not None,
    )
    check(
        "no match /api/users/list",
        match_rule(rules, "/api/users/list", "GET", "free") is None,
    )


@test("match: method filtering")
async def test_match_method():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {
            "path_pattern": "*",
            "method": "POST",
            "tier": "*",
            "is_active": True,
            "id": 1,
            "max_requests": 50,
        },
        {
            "path_pattern": "*",
            "method": "GET",
            "tier": "*",
            "is_active": True,
            "id": 2,
            "max_requests": 500,
        },
    ]
    post_match = match_rule(rules, "/api/users", "POST", "free")
    get_match = match_rule(rules, "/api/users", "GET", "free")
    check("POST matches rule 1", post_match["id"] == 1)
    check("GET matches rule 2", get_match["id"] == 2)
    check(
        "DELETE matches neither",
        match_rule(rules, "/api/users", "DELETE", "free") is None,
    )


@test("match: tier filtering")
async def test_match_tier():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {
            "path_pattern": "*",
            "method": "*",
            "tier": "free",
            "is_active": True,
            "id": 1,
            "max_requests": 100,
        },
        {
            "path_pattern": "*",
            "method": "*",
            "tier": "pro",
            "is_active": True,
            "id": 2,
            "max_requests": 1000,
        },
    ]
    free_match = match_rule(rules, "/api/users", "GET", "free")
    pro_match = match_rule(rules, "/api/users", "GET", "pro")
    enterprise_match = match_rule(rules, "/api/users", "GET", "enterprise")
    check("free matches rule 1", free_match["id"] == 1)
    check("pro matches rule 2", pro_match["id"] == 2)
    check("enterprise matches neither", enterprise_match is None)


@test("match: inactive rules skipped")
async def test_match_inactive():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {"path_pattern": "*", "method": "*", "tier": "*", "is_active": False, "id": 1},
        {"path_pattern": "*", "method": "*", "tier": "*", "is_active": True, "id": 2},
    ]
    result = match_rule(rules, "/api/users", "GET", "free")
    check("skips inactive, uses active", result["id"] == 2)


@test("match: priority order (first match wins)")
async def test_match_priority():
    from hyperdjango.ratelimit import match_rule

    # Rules sorted by priority DESC (highest first)
    rules = [
        {
            "path_pattern": "/api/reports*",
            "method": "GET",
            "tier": "free",
            "is_active": True,
            "id": 1,
            "priority": 100,
        },
        {
            "path_pattern": "/api/*",
            "method": "*",
            "tier": "free",
            "is_active": True,
            "id": 2,
            "priority": 50,
        },
        {
            "path_pattern": "*",
            "method": "*",
            "tier": "*",
            "is_active": True,
            "id": 3,
            "priority": 0,
        },
    ]
    # Specific rule wins for /api/reports GET by free user
    result = match_rule(rules, "/api/reports/123", "GET", "free")
    check("specific rule wins", result["id"] == 1)

    # Non-matching first rule falls to second
    result2 = match_rule(rules, "/api/users", "POST", "free")
    check("falls to api/* rule", result2["id"] == 2)

    # Pro tier doesn't match first two, falls to wildcard
    result3 = match_rule(rules, "/api/reports/123", "GET", "pro")
    check("pro falls to wildcard", result3["id"] == 3)


@test("match: method case insensitive")
async def test_match_case():
    from hyperdjango.ratelimit import match_rule

    rules = [
        {
            "path_pattern": "*",
            "method": "POST",
            "tier": "*",
            "is_active": True,
            "id": 1,
        },
    ]
    check("lowercase post matches", match_rule(rules, "/", "post", "free") is not None)
    check("uppercase POST matches", match_rule(rules, "/", "POST", "free") is not None)


@test("match: empty rules returns None")
async def test_match_empty():
    from hyperdjango.ratelimit import match_rule

    result = match_rule([], "/api/users", "GET", "free")
    check("empty rules returns None", result is None)


# ═══════════════════════════════════════════════════════════════════════════
# Cost-based limits (InMemory backend)
# ═══════════════════════════════════════════════════════════════════════════


@test("cost: increment=1 (normal)")
async def test_cost_1():
    from hyperdjango.ratelimit import InMemoryRateLimitBackend

    backend = InMemoryRateLimitBackend()
    allowed, remaining, _ = backend.check_and_increment("key1", 5, 60, 1)
    check("cost=1 allowed", allowed)
    check("cost=1 remaining=4", remaining == 4)


@test("cost: increment=5 (expensive)")
async def test_cost_5():
    from hyperdjango.ratelimit import InMemoryRateLimitBackend

    backend = InMemoryRateLimitBackend()
    # Max 20 requests, cost 5 each → 4 expensive requests allowed
    allowed1, remaining1, _ = backend.check_and_increment("key2", 20, 60, 5)
    check("first cost=5 allowed", allowed1)
    check("remaining=15", remaining1 == 15)

    allowed2, remaining2, _ = backend.check_and_increment("key2", 20, 60, 5)
    check("second cost=5 allowed", allowed2)
    check("remaining=10", remaining2 == 10)

    allowed3, remaining3, _ = backend.check_and_increment("key2", 20, 60, 5)
    check("third cost=5 allowed", allowed3)
    check("remaining=5", remaining3 == 5)

    allowed4, remaining4, _ = backend.check_and_increment("key2", 20, 60, 5)
    check("fourth cost=5 allowed", allowed4)
    check("remaining=0", remaining4 == 0)

    allowed5, remaining5, _ = backend.check_and_increment("key2", 20, 60, 5)
    check("fifth cost=5 DENIED", not allowed5)


@test("cost: mixed costs on same key")
async def test_cost_mixed():
    from hyperdjango.ratelimit import InMemoryRateLimitBackend

    backend = InMemoryRateLimitBackend()
    # Max 10, first use 3, then try 8 (would be 11 total)
    allowed1, _, _ = backend.check_and_increment("key3", 10, 60, 3)
    check("cost=3 allowed", allowed1)

    allowed2, _, _ = backend.check_and_increment("key3", 10, 60, 3)
    check("second cost=3 allowed (total=6)", allowed2)

    allowed3, _, _ = backend.check_and_increment("key3", 10, 60, 3)
    check("third cost=3 allowed (total=9)", allowed3)

    # 9+3=12 > 10
    allowed4, _, _ = backend.check_and_increment("key3", 10, 60, 3)
    check("fourth cost=3 DENIED (would be 12)", not allowed4)


# ═══════════════════════════════════════════════════════════════════════════
# RuleBasedRateLimitMiddleware — integration tests
# ═══════════════════════════════════════════════════════════════════════════


@test("middleware: fallback to tier when no rules in DB")
async def test_mw_fallback():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=None, path="/api/anything", method="GET")

        resp = await mw(req, next_handler)
        check("200 OK", resp.status == 200)
        check("tier header = free", resp.headers.get("x-ratelimit-tier") == "free")
        check(
            "limit = 100 (free default)", resp.headers.get("x-ratelimit-limit") == "100"
        )
    finally:
        await teardown(db)


@test("middleware: rule with per-path limit")
async def test_mw_rule_path():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            "reports-limit",
            "/api/reports*",
            "*",
            "*",
            10,
            60,
            1,
            100,
            True,
        )

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        await mw.ensure_tables()

        # Request to /api/reports → rule limit (10)
        req = FakeRequest(user=None, path="/api/reports/123", method="GET")
        resp = await mw(req, next_handler)
        check("reports limit = 10", resp.headers.get("x-ratelimit-limit") == "10")
        check(
            "rule header set", resp.headers.get("x-ratelimit-rule") == "reports-limit"
        )
    finally:
        await teardown(db)


@test("middleware: rule with cost > 1")
async def test_mw_cost():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            "expensive-reports",
            "/api/reports*",
            "GET",
            "*",
            10,
            60,
            5,
            100,
            True,
        )

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)

        req = FakeRequest(user=None, path="/api/reports/big", method="GET")
        resp = await mw(req, next_handler)
        check("200 OK", resp.status == 200)
        check(
            "remaining = 5 (10-5=5)", resp.headers.get("x-ratelimit-remaining") == "5"
        )
        check("cost header = 5", resp.headers.get("x-ratelimit-cost") == "5")

        # Second request: 5+5=10, which is limit
        resp2 = await mw(req, next_handler)
        check("second request OK (total=10)", resp2.status == 200)
        check("remaining = 0", resp2.headers.get("x-ratelimit-remaining") == "0")

        # Third request: 10+5=15 > 10 → denied
        resp3 = await mw(req, next_handler)
        check("third request DENIED", resp3.status == 429)
    finally:
        await teardown(db)


@test("middleware: different rules for GET vs POST")
async def test_mw_method_rules():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        # Insert both rules in one statement to avoid prepared statement collision
        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) VALUES "
            "('api-read', '/api/*', 'GET', '*', 1000, 60, 1, 50, TRUE), "
            "('api-write', '/api/*', 'POST', '*', 50, 60, 1, 50, TRUE)"
        )

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)

        get_req = FakeRequest(user=None, path="/api/users", method="GET")
        post_req = FakeRequest(user=None, path="/api/users", method="POST")

        get_resp = await mw(get_req, next_handler)
        post_resp = await mw(post_req, next_handler)

        check("GET limit = 1000", get_resp.headers.get("x-ratelimit-limit") == "1000")
        check("POST limit = 50", post_resp.headers.get("x-ratelimit-limit") == "50")
        check(
            "GET rule = api-read",
            get_resp.headers.get("x-ratelimit-rule") == "api-read",
        )
        check(
            "POST rule = api-write",
            post_resp.headers.get("x-ratelimit-rule") == "api-write",
        )
    finally:
        await teardown(db)


@test("middleware: tier-specific rules (free vs pro)")
async def test_mw_tier_rules():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) VALUES "
            "('reports-free', '/api/reports*', '*', 'free', 20, 60, 1, 100, TRUE), "
            "('reports-pro', '/api/reports*', '*', 'pro', 500, 60, 1, 100, TRUE)"
        )

        # Create pro user
        alice = await checker.create_user("alice", "pass123")
        pro_group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE id = $1",
            pro_group.id,
        )
        await checker.add_user_to_group(alice.id, pro_group.id)

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)

        # Anonymous (free tier)
        free_req = FakeRequest(user=None, path="/api/reports/123", method="GET")
        free_resp = await mw(free_req, next_handler)
        check(
            "free tier gets limit=20",
            free_resp.headers.get("x-ratelimit-limit") == "20",
        )

        # Pro user
        pro_req = FakeRequest(
            user=FakeUser(id=alice.id), path="/api/reports/123", method="GET"
        )
        pro_resp = await mw(pro_req, next_handler)
        check(
            "pro tier gets limit=500",
            pro_resp.headers.get("x-ratelimit-limit") == "500",
        )
    finally:
        await teardown(db)


@test("middleware: separate counters per rule")
async def test_mw_separate_counters():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) VALUES "
            "('rule-a', '/api/a*', '*', '*', 3, 60, 1, 100, TRUE), "
            "('rule-b', '/api/b*', '*', '*', 3, 60, 1, 100, TRUE)"
        )

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)

        # Use up rule-a's quota
        req_a = FakeRequest(user=None, path="/api/a/endpoint", method="GET")
        for _ in range(3):
            await mw(req_a, next_handler)
        resp_a = await mw(req_a, next_handler)
        check("rule-a exhausted", resp_a.status == 429)

        # rule-b should still work
        req_b = FakeRequest(user=None, path="/api/b/endpoint", method="GET")
        resp_b = await mw(req_b, next_handler)
        check("rule-b still available", resp_b.status == 200)
    finally:
        await teardown(db)


@test("middleware: 429 includes rule and cost info")
async def test_mw_429_info():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            "tiny-rule",
            "*",
            "*",
            "*",
            1,
            60,
            1,
            0,
            True,
        )

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req = FakeRequest(user=None, path="/any", method="GET")

        await mw(req, next_handler)  # first OK
        resp = await mw(req, next_handler)  # second denied

        check("429 status", resp.status == 429)
        check("has rule header", resp.headers.get("x-ratelimit-rule") == "tiny-rule")
        check("has tier header", resp.headers.get("x-ratelimit-tier") == "free")
    finally:
        await teardown(db)


@test("middleware: rules cache refresh")
async def test_mw_cache_refresh():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        mw = RuleBasedRateLimitMiddleware(
            tiers=TIERS, default_tier="free", db=db, rules_cache_ttl=0
        )

        # No rules initially
        req = FakeRequest(user=None, path="/api/users", method="GET")
        resp1 = await mw(req, next_handler)
        check(
            "no rule, uses tier default",
            resp1.headers.get("x-ratelimit-limit") == "100",
        )

        # Add a rule
        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            "new-rule",
            "/api/*",
            "*",
            "*",
            42,
            60,
            1,
            100,
            True,
        )

        # With TTL=0, next call reloads
        mw.clear_rules_cache()
        resp2 = await mw(
            FakeRequest(user=None, path="/api/users", method="GET"), next_handler
        )
        check(
            "after cache refresh, new rule applies",
            resp2.headers.get("x-ratelimit-limit") == "42",
        )
    finally:
        await teardown(db)


@test("middleware: ensure_tables is idempotent")
async def test_mw_ensure_tables():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        # Table already exists from setup — ensure_tables should be idempotent
        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, db=db)
        await mw.ensure_tables()
        await mw.ensure_tables()

        # Should be able to query it
        rows = await db.query("SELECT * FROM hyper_rate_limit_rules")
        check("table queryable after ensure_tables", isinstance(rows, list))
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# Combined scenario: expensive endpoint with tier-specific cost
# ═══════════════════════════════════════════════════════════════════════════


@test("scenario: POST reports costs 5 for free, 2 for pro")
async def test_scenario_tiered_cost():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) VALUES "
            "('reports-free', '/api/reports*', 'POST', 'free', 20, 60, 5, 100, TRUE), "
            "('reports-pro', '/api/reports*', 'POST', 'pro', 100, 60, 2, 100, TRUE)"
        )

        # Create pro user
        alice = await checker.create_user("alice", "pass123")
        pro_group = await checker.create_group("pro_users", priority=5)
        await db.execute(
            "UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE id = $1",
            pro_group.id,
        )
        await checker.add_user_to_group(alice.id, pro_group.id)

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)

        # Free user: 4 requests then denied
        free_req = FakeRequest(user=None, path="/api/reports/generate", method="POST")
        free_ok = 0
        for _ in range(6):
            resp = await mw(free_req, next_handler)
            if resp.status == 200:
                free_ok += 1
        check("free gets 4 requests (20/5)", free_ok == 4)

        # Pro user: many more allowed
        pro_req = FakeRequest(
            user=FakeUser(id=alice.id), path="/api/reports/generate", method="POST"
        )
        pro_ok = 0
        for _ in range(10):
            resp = await mw(pro_req, next_handler)
            if resp.status == 200:
                pro_ok += 1
        check("pro gets all 10 requests (100/2=50 budget)", pro_ok == 10)
    finally:
        await teardown(db)


@test("scenario: GET is cheap, POST is expensive on same path")
async def test_scenario_method_cost():
    db, checker = await setup()
    try:
        from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

        await db.execute(
            "INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active) VALUES "
            "('api-get', '/api/*', 'GET', '*', 100, 60, 1, 50, TRUE), "
            "('api-post', '/api/*', 'POST', '*', 100, 60, 10, 50, TRUE)"
        )

        mw = RuleBasedRateLimitMiddleware(tiers=TIERS, default_tier="free", db=db)
        req_get = FakeRequest(user=None, path="/api/data", method="GET")
        req_post = FakeRequest(user=None, path="/api/data", method="POST")

        resp_get = await mw(req_get, next_handler)
        resp_post = await mw(req_post, next_handler)

        check(
            "GET remaining=99 (cost=1)",
            resp_get.headers.get("x-ratelimit-remaining") == "99",
        )
        check(
            "POST remaining=90 (cost=10)",
            resp_post.headers.get("x-ratelimit-remaining") == "90",
        )
        check(
            "GET has no cost header", resp_get.headers.get("x-ratelimit-cost") is None
        )
        check(
            "POST has cost=10 header", resp_post.headers.get("x-ratelimit-cost") == "10"
        )
    finally:
        await teardown(db)


# ═══════════════════════════════════════════════════════════════════════════
# RateLimitRule model
# ═══════════════════════════════════════════════════════════════════════════


@test("model: RateLimitRule exists and has correct fields")
async def test_rule_model():
    from hyperdjango.ratelimit import RateLimitRule

    annotations = RateLimitRule.__annotations__
    check("has name", "name" in annotations)
    check("has path_pattern", "path_pattern" in annotations)
    check("has method", "method" in annotations)
    check("has tier", "tier" in annotations)
    check("has max_requests", "max_requests" in annotations)
    check("has window_seconds", "window_seconds" in annotations)
    check("has cost", "cost" in annotations)
    check("has priority", "priority" in annotations)
    check("has is_active", "is_active" in annotations)


@test("model: correct table name")
async def test_rule_table():
    from hyperdjango.ratelimit import RateLimitRule

    check(
        "table = hyper_rate_limit_rules",
        RateLimitRule._meta.table == "hyper_rate_limit_rules",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Admin registration
# ═══════════════════════════════════════════════════════════════════════════


@test("admin: register_ratelimit_models exists")
async def test_admin_method():
    from hyperdjango.admin import HyperAdmin

    check(
        "has register_ratelimit_models",
        hasattr(HyperAdmin, "register_ratelimit_models"),
    )


@test("admin: dashboard has rate limit rules link")
async def test_admin_link():
    from hyperdjango.admin.templates import TEMPLATE_DASHBOARD

    check("has rate-limit-rules link", "rate-limit-rules" in TEMPLATE_DASHBOARD)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    print(f"\n{'=' * 60}")
    print("Rule-Based Rate Limiting Tests")
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
