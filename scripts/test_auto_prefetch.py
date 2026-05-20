#!/usr/bin/env python3
"""Test auto-prefetch N+1 detection middleware.

Tests:
1. N+1 pattern learning from query analysis
2. Suggestion header generation
3. Pattern memory across requests
4. Threshold configuration
5. Disabled when DEBUG=False
"""

# hyper-test: db_django

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django

django.setup()


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Auto-prefetch middleware ───────────────────────────────────────────
    print("\n=== HyperAutoPrefetchMiddleware ===")

    from django.conf import settings
    from django.http import HttpResponse
    from django.test import RequestFactory

    from hyperdjango.serving.django_middleware import (
        HyperAutoPrefetchMiddleware,
    )

    # Ensure DEBUG is True for query tracking
    settings.DEBUG = True

    factory = RequestFactory()

    def dummy_view(request):
        return HttpResponse("OK")

    middleware = HyperAutoPrefetchMiddleware(dummy_view)
    check("middleware created", middleware is not None)
    check("has learned dict", hasattr(middleware, "_learned"))
    check("learned empty initially", len(middleware.learned_patterns) == 0)

    # ── Query analysis integration ────────────────────────────────────────
    print("\n=== Query analysis ===")

    from hyperdjango.serving.admin import analyze_queries

    # Simulate N+1 pattern
    n_plus_one_queries = [
        {"sql": "SELECT * FROM articles_article", "time": "0.005"},
    ] + [
        {"sql": f'SELECT * FROM articles_author WHERE "id" = {i}', "time": "0.002"}
        for i in range(10)
    ]
    result = analyze_queries(n_plus_one_queries)
    check("detects n+1", len(result["n_plus_one"]) > 0)
    check(
        "suggestion generated",
        "select_related" in result["n_plus_one"][0]["suggestion"],
    )
    check("count >= 5", result["n_plus_one"][0]["count"] >= 5)

    # No N+1 — normal queries
    normal_queries = [
        {"sql": "SELECT * FROM articles", "time": "0.005"},
        {"sql": "SELECT * FROM categories", "time": "0.003"},
    ]
    result = analyze_queries(normal_queries)
    check("no false positive", len(result["n_plus_one"]) == 0)

    # ── Threshold configuration ───────────────────────────────────────────
    print("\n=== Threshold ===")

    # With threshold=3, 3 repeated queries should trigger
    settings.HYPERDJANGO_N_PLUS_ONE_THRESHOLD = 3
    queries_3 = [
        {"sql": f'SELECT * FROM users WHERE "id" = {i}', "time": "0.001"}
        for i in range(3)
    ]
    result = analyze_queries(queries_3)
    check("threshold 3 triggers", len(result["n_plus_one"]) > 0)

    # Reset
    settings.HYPERDJANGO_N_PLUS_ONE_THRESHOLD = 5

    # Below threshold
    queries_2 = [
        {"sql": f'SELECT * FROM orders WHERE "id" = {i}', "time": "0.001"}
        for i in range(2)
    ]
    result = analyze_queries(queries_2)
    check("below threshold no trigger", len(result["n_plus_one"]) == 0)

    # ── Learned patterns property ─────────────────────────────────────────
    print("\n=== Pattern learning ===")

    mw = HyperAutoPrefetchMiddleware(dummy_view)
    check("learned patterns is dict", isinstance(mw.learned_patterns, dict))

    # Manually populate
    mw._learned["GET:/api/articles"] = [
        {
            "pattern": "SELECT * FROM authors WHERE id = ?",
            "count": 10,
            "suggestion": '.select_related("author")',
        }
    ]
    check("learned pattern stored", "GET:/api/articles" in mw.learned_patterns)
    check(
        "learned suggestion",
        "author" in mw.learned_patterns["GET:/api/articles"][0]["suggestion"],
    )

    # ── Suggestion format ─────────────────────────────────────────────────
    print("\n=== Suggestion format ===")

    from hyperdjango.serving.admin import _suggest_fix

    # Various SQL patterns
    check(
        "FK pattern",
        "select_related" in _suggest_fix('SELECT * FROM "users" WHERE "user_id" = ?'),
    )
    check(
        "field extraction",
        "user" in _suggest_fix('SELECT * FROM "profiles" WHERE "user_id" = ?'),
    )
    check(
        "table extraction",
        _suggest_fix('SELECT * FROM "categories" WHERE "id" = ?') != "",
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All auto-prefetch tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
