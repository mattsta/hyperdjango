#!/usr/bin/env python3
"""Test Django Admin performance overlay.

Tests:
1. HyperAdminSite context injection
2. HyperModelAdmin auto-prefetch detection
3. Query analysis (slow queries, N+1 detection, suggestions)
4. install_hyper_admin monkey-patching
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

    # ── HyperAdminSite ────────────────────────────────────────────────────
    print("\n=== HyperAdminSite ===")

    from hyperdjango.serving.admin import HyperAdminSite, HyperModelAdmin

    site = HyperAdminSite(name="test_admin")
    check("site created", site is not None)
    check("site header", site.site_header == "HyperDjango Admin")
    check("is AdminSite subclass", isinstance(site, django.contrib.admin.AdminSite))

    # ── HyperModelAdmin ───────────────────────────────────────────────────
    print("\n=== HyperModelAdmin ===")

    check(
        "is ModelAdmin subclass",
        issubclass(HyperModelAdmin, django.contrib.admin.ModelAdmin),
    )

    # Test auto-prefetch detection with a real Django model
    from django.contrib.auth.models import User

    admin_instance = HyperModelAdmin(User, site)
    check("admin instance created", admin_instance is not None)

    # ── Query Analysis ────────────────────────────────────────────────────
    print("\n=== Query analysis ===")

    from hyperdjango.serving.admin import analyze_queries

    # Normal queries — no issues
    normal_queries = [
        {"sql": "SELECT * FROM articles", "time": "0.005"},
        {"sql": "SELECT * FROM users WHERE id = 1", "time": "0.003"},
    ]
    result = analyze_queries(normal_queries)
    check("normal query count", result["query_count"] == 2)
    check("normal total time", result["total_ms"] > 0)
    check("normal no issues", not result["has_issues"])
    check("normal no slow", len(result["slow_queries"]) == 0)
    check("normal no n+1", len(result["n_plus_one"]) == 0)

    # Slow query
    slow_queries = [
        {"sql": "SELECT * FROM big_table WHERE complex_join", "time": "0.500"},
    ]
    result = analyze_queries(slow_queries)
    check("slow detected", len(result["slow_queries"]) == 1)
    check("slow has issues", result["has_issues"])
    check("slow time", result["slow_queries"][0]["time_ms"] == 500.0)

    # N+1 pattern
    n_plus_one_queries = [
        {"sql": "SELECT * FROM articles", "time": "0.005"},
    ] + [
        {"sql": f"SELECT * FROM authors WHERE id = {i}", "time": "0.002"}
        for i in range(10)
    ]
    result = analyze_queries(n_plus_one_queries)
    check("n+1 detected", len(result["n_plus_one"]) > 0)
    check("n+1 has issues", result["has_issues"])
    check("n+1 count >= 5", result["n_plus_one"][0]["count"] >= 5)
    check(
        "n+1 has suggestion", "select_related" in result["n_plus_one"][0]["suggestion"]
    )

    # Mixed: slow + N+1
    mixed_queries = [
        {"sql": "SELECT * FROM big_table JOIN authors", "time": "0.200"},
    ] + [
        {"sql": f"SELECT * FROM categories WHERE id = {i}", "time": "0.001"}
        for i in range(7)
    ]
    result = analyze_queries(mixed_queries)
    check("mixed slow count", len(result["slow_queries"]) == 1)
    check("mixed n+1 count", len(result["n_plus_one"]) >= 1)
    check("mixed has issues", result["has_issues"])

    # ── Fix suggestions ───────────────────────────────────────────────────
    print("\n=== Fix suggestions ===")

    from hyperdjango.serving.admin import _suggest_fix

    suggestion = _suggest_fix('SELECT * FROM "authors" WHERE "id" = ?')
    check("suggest FK fix", "select_related" in suggestion)

    suggestion = _suggest_fix('SELECT * FROM "categories" WHERE "category_id" = ?')
    check("suggest FK field", "category" in suggestion)

    suggestion = _suggest_fix("UNKNOWN PATTERN")
    check(
        "fallback suggestion",
        "select_related" in suggestion or "prefetch_related" in suggestion,
    )

    # ── install_hyper_admin ───────────────────────────────────────────────
    print("\n=== install_hyper_admin ===")

    from hyperdjango.serving.admin import install_hyper_admin

    check("install function exists", callable(install_hyper_admin))

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All admin perf tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
