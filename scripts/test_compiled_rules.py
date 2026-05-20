"""Tests for compiled rule index optimization in rate limiter.

Tests CompiledRuleIndex, _CompiledRule, prefix matching optimization,
method+tier indexing, and parity with the original match_rule function.
"""

# hyper-test: unit

import sys
import time

from hyperdjango.ratelimit import CompiledRuleIndex, match_rule


def make_rule(
    name: str,
    path: str = "*",
    method: str = "*",
    tier: str = "*",
    max_requests: int = 100,
    window: int = 60,
    cost: int = 1,
    priority: int = 0,
    is_active: bool = True,
) -> dict[str, int | str | bool]:
    return {
        "id": hash(name) % 10000,
        "name": name,
        "path_pattern": path,
        "method": method,
        "tier": tier,
        "max_requests": max_requests,
        "window_seconds": window,
        "cost": cost,
        "priority": priority,
        "is_active": is_active,
    }


# ── Basic matching tests ─────────────────────────────────────────────────


def test_empty_index():
    """Empty rule set matches nothing."""
    idx = CompiledRuleIndex.build([])
    assert idx.match("/api/users", "GET", "free") is None
    assert idx.rule_count == 0
    print("  PASS: Empty index returns None")


def test_wildcard_rule():
    """Wildcard rule matches everything."""
    rules = [make_rule("catch-all")]
    idx = CompiledRuleIndex.build(rules)
    assert idx.match("/anything", "GET", "free") is not None
    assert idx.match("/api/users", "POST", "enterprise") is not None
    print("  PASS: Wildcard rule matches all")


def test_method_filter():
    """Method-specific rules only match that method."""
    rules = [
        make_rule("post-only", method="POST", priority=10),
        make_rule("catch-all", priority=0),
    ]
    idx = CompiledRuleIndex.build(rules)
    result = idx.match("/api", "POST", "free")
    assert result["name"] == "post-only"
    result = idx.match("/api", "GET", "free")
    assert result["name"] == "catch-all"
    print("  PASS: Method filtering")


def test_tier_filter():
    """Tier-specific rules only match that tier."""
    rules = [
        make_rule(
            "enterprise-rule", tier="enterprise", max_requests=10000, priority=10
        ),
        make_rule("default", max_requests=100, priority=0),
    ]
    idx = CompiledRuleIndex.build(rules)
    result = idx.match("/api", "GET", "enterprise")
    assert result["name"] == "enterprise-rule"
    result = idx.match("/api", "GET", "free")
    assert result["name"] == "default"
    print("  PASS: Tier filtering")


def test_path_prefix():
    """Prefix pattern (/api/*) uses fast startswith."""
    rules = [
        make_rule("api-rule", path="/api/*", max_requests=50, priority=10),
        make_rule("catch-all", priority=0),
    ]
    idx = CompiledRuleIndex.build(rules)
    result = idx.match("/api/users", "GET", "free")
    assert result["name"] == "api-rule"
    result = idx.match("/api/posts/123", "GET", "free")
    assert result["name"] == "api-rule"
    result = idx.match("/admin/dashboard", "GET", "free")
    assert result["name"] == "catch-all"
    print("  PASS: Prefix path matching")


def test_fnmatch_glob():
    """Complex glob patterns still use fnmatch."""
    rules = [
        make_rule("reports", path="/api/report[s]/*", priority=10),
        make_rule("catch-all", priority=0),
    ]
    idx = CompiledRuleIndex.build(rules)
    result = idx.match("/api/reports/monthly", "GET", "free")
    assert result["name"] == "reports"
    result = idx.match("/api/other", "GET", "free")
    assert result["name"] == "catch-all"
    print("  PASS: Glob pattern matching (fnmatch)")


def test_priority_ordering():
    """Higher priority rules match first."""
    rules = [
        make_rule("low", path="/api/*", priority=1),
        make_rule("high", path="/api/*", priority=100),
    ]
    idx = CompiledRuleIndex.build(rules)
    result = idx.match("/api/users", "GET", "free")
    assert result["name"] == "high"
    print("  PASS: Priority ordering")


def test_inactive_rules_skipped():
    """Inactive rules are excluded during compilation."""
    rules = [
        make_rule("inactive", path="/api/*", priority=100, is_active=False),
        make_rule("active", path="/api/*", priority=1, is_active=True),
    ]
    idx = CompiledRuleIndex.build(rules)
    result = idx.match("/api/users", "GET", "free")
    assert result["name"] == "active"
    print("  PASS: Inactive rules skipped")


def test_method_tier_combination():
    """Specific method+tier combination matched correctly."""
    rules = [
        make_rule(
            "post-enterprise",
            method="POST",
            tier="enterprise",
            max_requests=10000,
            priority=100,
        ),
        make_rule(
            "post-free", method="POST", tier="free", max_requests=50, priority=90
        ),
        make_rule("get-any", method="GET", max_requests=500, priority=80),
        make_rule("fallback", priority=0),
    ]
    idx = CompiledRuleIndex.build(rules)
    assert idx.match("/api", "POST", "enterprise")["name"] == "post-enterprise"
    assert idx.match("/api", "POST", "free")["name"] == "post-free"
    assert idx.match("/api", "GET", "free")["name"] == "get-any"
    assert idx.match("/api", "DELETE", "free")["name"] == "fallback"
    print("  PASS: Method+tier combination matching")


def test_no_match_without_fallback():
    """No match when rules don't cover the request."""
    rules = [
        make_rule("post-only", method="POST", path="/api/*"),
    ]
    idx = CompiledRuleIndex.build(rules)
    assert idx.match("/admin", "GET", "free") is None
    print("  PASS: No match without fallback")


def test_cost_preserved():
    """Cost field is accessible on matched rule."""
    rules = [
        make_rule("expensive", path="/api/reports*", cost=5, priority=10),
    ]
    idx = CompiledRuleIndex.build(rules)
    result = idx.match("/api/reports/monthly", "GET", "free")
    assert result["cost"] == 5
    print("  PASS: Cost preserved in matched rule")


# ── Parity with match_rule ───────────────────────────────────────────────


def test_parity_simple():
    """CompiledRuleIndex gives same result as match_rule for simple cases."""
    rules = [
        make_rule("r1", path="/api/*", method="GET", tier="free", priority=10),
        make_rule("r2", path="/admin/*", method="*", tier="*", priority=5),
        make_rule("r3", priority=0),
    ]
    idx = CompiledRuleIndex.build(rules)
    test_cases = [
        ("/api/users", "GET", "free"),
        ("/api/users", "POST", "free"),
        ("/admin/panel", "GET", "enterprise"),
        ("/other", "GET", "free"),
    ]
    for path, method, tier in test_cases:
        expected = match_rule(rules, path, method, tier)
        actual = idx.match(path, method, tier)
        exp_name = expected["name"] if expected else None
        act_name = actual["name"] if actual else None
        assert exp_name == act_name, (
            f"Mismatch for ({path}, {method}, {tier}): {exp_name} vs {act_name}"
        )
    print("  PASS: Parity with match_rule (simple)")


def test_parity_complex():
    """Parity test with many rules and varied requests."""
    rules = [
        make_rule(
            "reports-free-get",
            path="/api/reports*",
            method="GET",
            tier="free",
            max_requests=20,
            cost=5,
            priority=100,
        ),
        make_rule(
            "reports-pro-get",
            path="/api/reports*",
            method="GET",
            tier="pro",
            max_requests=200,
            cost=3,
            priority=100,
        ),
        make_rule(
            "write-api-free",
            path="/api/*",
            method="POST",
            tier="free",
            max_requests=50,
            priority=50,
        ),
        make_rule(
            "write-api-pro",
            path="/api/*",
            method="POST",
            tier="pro",
            max_requests=500,
            priority=50,
        ),
        make_rule(
            "admin-any",
            path="/admin/*",
            method="*",
            tier="*",
            max_requests=1000,
            priority=30,
        ),
        make_rule("default-free", tier="free", max_requests=100, priority=10),
        make_rule("default-pro", tier="pro", max_requests=1000, priority=10),
        make_rule("catch-all", max_requests=200, priority=0),
    ]
    idx = CompiledRuleIndex.build(rules)
    test_cases = [
        ("/api/reports/monthly", "GET", "free", "reports-free-get"),
        ("/api/reports/daily", "GET", "pro", "reports-pro-get"),
        ("/api/users", "POST", "free", "write-api-free"),
        ("/api/users", "POST", "pro", "write-api-pro"),
        ("/api/users", "GET", "free", "default-free"),
        ("/admin/dashboard", "GET", "free", "admin-any"),
        ("/admin/settings", "POST", "enterprise", "admin-any"),
        ("/other", "GET", "free", "default-free"),
        ("/other", "GET", "enterprise", "catch-all"),
    ]
    for path, method, tier, expected_name in test_cases:
        result = idx.match(path, method, tier)
        assert result is not None, f"No match for ({path}, {method}, {tier})"
        assert result["name"] == expected_name, (
            f"Expected {expected_name} for ({path}, {method}, {tier}), got {result['name']}"
        )
    print("  PASS: Parity with match_rule (complex)")


# ── Benchmark ─────────────────────────────────────────────────────────────


def test_benchmark():
    """Benchmark compiled index vs linear match_rule."""
    rules = [
        make_rule(
            f"rule-{i}",
            path=f"/api/v{i}/*",
            method="GET" if i % 2 == 0 else "POST",
            tier="free" if i % 3 == 0 else "pro",
            priority=100 - i,
        )
        for i in range(50)
    ] + [make_rule("catch-all", priority=0)]

    idx = CompiledRuleIndex.build(rules)
    iterations = 10_000
    paths = [
        "/api/v0/users",
        "/api/v25/data",
        "/api/v49/items",
        "/other/path",
        "/admin/panel",
    ]

    # Benchmark linear match
    start = time.perf_counter_ns()
    for _ in range(iterations):
        for path in paths:
            match_rule(rules, path, "GET", "free")
    linear_ns = (time.perf_counter_ns() - start) / (iterations * len(paths))

    # Benchmark compiled index
    start = time.perf_counter_ns()
    for _ in range(iterations):
        for path in paths:
            idx.match(path, "GET", "free")
    compiled_ns = (time.perf_counter_ns() - start) / (iterations * len(paths))

    speedup = linear_ns / compiled_ns if compiled_ns > 0 else 0
    print(
        f"  PASS: Benchmark — linear: {linear_ns:.0f}ns, compiled: {compiled_ns:.0f}ns, speedup: {speedup:.2f}x"
    )


def main():
    tests = [
        test_empty_index,
        test_wildcard_rule,
        test_method_filter,
        test_tier_filter,
        test_path_prefix,
        test_fnmatch_glob,
        test_priority_ordering,
        test_inactive_rules_skipped,
        test_method_tier_combination,
        test_no_match_without_fallback,
        test_cost_preserved,
        test_parity_simple,
        test_parity_complex,
        test_benchmark,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Compiled Rule Index Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback

            failed += 1
            errors.append((test.__name__, str(e)))
            traceback.print_exc()
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
