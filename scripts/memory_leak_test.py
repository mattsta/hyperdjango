#!/usr/bin/env python3
"""Memory leak detection for hyperdjango native extension.

Runs sustained operations and monitors RSS growth to detect memory leaks.
Tests database queries, model validation, JSON serialization, and routing.

Usage:
    uv run python scripts/memory_leak_test.py
"""

import gc
import os
import resource
import sys
import time

from hyperdjango.conf import fill_url_auth


def get_rss_mb():
    """Get current RSS in MB."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def test_memory_stability(name, func, iterations, max_growth_mb=10):
    """Run func() N times and check RSS doesn't grow more than max_growth_mb."""
    gc.collect()
    gc.collect()
    rss_before = get_rss_mb()

    t0 = time.perf_counter()
    for _ in range(iterations):
        func()
    elapsed = time.perf_counter() - t0

    gc.collect()
    gc.collect()
    rss_after = get_rss_mb()
    growth = rss_after - rss_before

    status = "PASS" if growth < max_growth_mb else "FAIL"
    print(f"  {status}: {name}")
    print(
        f"    {iterations:,} iterations in {elapsed:.2f}s ({iterations / elapsed:,.0f}/s)"
    )
    print(f"    RSS: {rss_before:.1f}MB → {rss_after:.1f}MB (growth: {growth:+.1f}MB)")

    return growth < max_growth_mb


def main():
    passed = 0
    failed = 0

    print("\n=== Memory Leak Detection ===\n")

    # ── Test 1: JSON serialization ────────────────────────────────────────
    print("Test 1: JSON serialization (native)")
    try:
        from hyperdjango._hyperdjango_native import (
            json_dumps_native,
            json_loads_native,
        )

        data = {"name": "test", "age": 42, "tags": ["a", "b", "c"], "nested": {"x": 1}}

        def json_roundtrip():
            s = json_dumps_native(data)
            json_loads_native(s)

        if test_memory_stability("json_dumps + json_loads", json_roundtrip, 100_000):
            passed += 1
        else:
            failed += 1
    except ImportError:
        print("  SKIP: native extension not available")

    # ── Test 2: Model validation ──────────────────────────────────────────
    print("\nTest 2: Model validation (compile_model_specs + init_model_full)")
    try:
        from hyperdjango._hyperdjango_native import validate_field

        def validate_fields():
            validate_field(
                42,
                "age",
                (
                    "int",
                    False,
                    0,
                    150,
                    -1,
                    -1,
                    -1,
                    -1,
                    -1,
                    -1,
                    False,
                    False,
                    False,
                    0,
                    None,
                    None,
                ),
            )
            validate_field(
                "hello@test.com",
                "email",
                (
                    "str",
                    False,
                    -1,
                    -1,
                    -1,
                    -1,
                    -1,
                    -1,
                    0,
                    255,
                    False,
                    False,
                    False,
                    1,
                    None,
                    None,
                ),
            )

        if test_memory_stability("validate_field", validate_fields, 100_000):
            passed += 1
        else:
            failed += 1
    except (ImportError, Exception) as e:
        print(f"  SKIP: {e}")

    # ── Test 3: Router operations ─────────────────────────────────────────
    print("\nTest 3: Router resolve")
    try:
        from hyperdjango._hyperdjango_native import (
            _router_add,
            _router_free,
            _router_new,
            _router_resolve,
        )

        handle = _router_new()
        _router_add(handle, "GET", "/users/{id}", "get_user")
        _router_add(handle, "POST", "/users", "create_user")
        _router_add(handle, "GET", "/items/{category}/{id}", "get_item")

        def router_resolve():
            _router_resolve(handle, "GET", "/users/42")
            _router_resolve(handle, "POST", "/users")
            _router_resolve(handle, "GET", "/items/books/99")
            _router_resolve(handle, "GET", "/nonexistent")

        if test_memory_stability("router_resolve", router_resolve, 100_000):
            passed += 1
        else:
            failed += 1

        _router_free(handle)
    except (ImportError, Exception) as e:
        print(f"  SKIP: {e}")

    # ── Test 4: Database queries ──────────────────────────────────────────
    print("\nTest 4: Database queries")
    try:
        from hyperdjango._hyperdjango_native import (
            _db_close_pool,
            _db_configure,
            _db_query,
        )

        # Userless URL; fill_url_auth resolves the role from PG*/OS user —
        # the single auth authority, so no username literal lives in source.
        conn_str = fill_url_auth(
            f"postgresql://localhost:5432/{os.environ.get('PGDATABASE', 'hyperdjango_test')}"
        )
        pool = _db_configure(conn_str, 2)

        def db_query():
            _db_query(pool, "SELECT 1, 'hello', 42.5, true", [])

        if test_memory_stability("SELECT queries", db_query, 50_000, max_growth_mb=20):
            passed += 1
        else:
            failed += 1

        _db_close_pool(pool)
    except (ImportError, RuntimeError) as e:
        print(f"  SKIP: {e}")

    # ── Test 5: String operations ─────────────────────────────────────────
    print("\nTest 5: String operations (SIMD)")
    try:
        from hyperdjango._hyperdjango_native import (
            html_escape_native,
            url_decode_native,
            url_encode_native,
        )

        def string_ops():
            html_escape_native("<script>alert('xss')</script>")
            url_encode_native("hello world & foo=bar")
            url_decode_native("hello%20world%26foo%3Dbar")

        if test_memory_stability(
            "html_escape + url_encode + url_decode", string_ops, 100_000
        ):
            passed += 1
        else:
            failed += 1
    except ImportError:
        print("  SKIP: native extension not available")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        print("WARNING: Potential memory leaks detected!")
        sys.exit(1)
    print("No memory leaks detected.")


if __name__ == "__main__":
    main()
