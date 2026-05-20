#!/usr/bin/env python3
"""CI benchmark suite — produces JSON output for github-action-benchmark.

Measures core hyperdjango operations and outputs results in the
customSmallerIsBetter format expected by benchmark-action.

Usage:
    uv run python scripts/bench_ci.py --json > bench_results.json
"""

import json
import os
import sys
import time


def bench(name, func, iterations):
    """Run func() N times, return (name, ns_per_op)."""
    # Warmup
    for _ in range(min(iterations // 10, 1000)):
        func()

    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        func()
    elapsed_ns = time.perf_counter_ns() - t0

    ns_per_op = elapsed_ns / iterations
    return {"name": name, "value": round(ns_per_op, 1), "unit": "ns/op"}


def main():
    results = []
    use_json = "--json" in sys.argv

    # ── JSON serialization ────────────────────────────────────────────────
    try:
        from hyperdjango._hyperdjango_native import (
            json_dumps_native,
            json_loads_native,
        )

        data = {"name": "test", "age": 42, "tags": ["a", "b", "c"]}
        json_str = json_dumps_native(data)

        results.append(bench("json_dumps", lambda: json_dumps_native(data), 100_000))
        results.append(
            bench("json_loads", lambda: json_loads_native(json_str), 100_000)
        )
    except ImportError:
        pass

    # ── String operations ─────────────────────────────────────────────────
    try:
        from hyperdjango._hyperdjango_native import (
            html_escape_native,
            parse_query_string_native,
            url_decode_native,
            url_encode_native,
        )

        html = "<script>alert('xss')</script>"
        url = "hello world & foo=bar"
        encoded = "hello%20world%26foo%3Dbar"
        qs = "foo=bar&baz=123&key=value&a=1&b=2"

        results.append(bench("html_escape", lambda: html_escape_native(html), 100_000))
        results.append(bench("url_encode", lambda: url_encode_native(url), 100_000))
        results.append(bench("url_decode", lambda: url_decode_native(encoded), 100_000))
        results.append(
            bench("parse_query_string", lambda: parse_query_string_native(qs), 100_000)
        )
    except ImportError:
        pass

    # ── Router ────────────────────────────────────────────────────────────
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
        _router_add(handle, "GET", "/health", "health")

        results.append(
            bench(
                "router_resolve_static",
                lambda: _router_resolve(handle, "GET", "/health"),
                100_000,
            )
        )
        results.append(
            bench(
                "router_resolve_dynamic",
                lambda: _router_resolve(handle, "GET", "/users/42"),
                100_000,
            )
        )
        results.append(
            bench(
                "router_resolve_multi_param",
                lambda: _router_resolve(handle, "GET", "/items/books/99"),
                100_000,
            )
        )
        _router_free(handle)
    except ImportError:
        pass

    # ── Database queries ──────────────────────────────────────────────────
    try:
        from hyperdjango._hyperdjango_native import (
            _db_close_pool,
            _db_configure,
            _db_query,
        )

        host = os.environ.get("PGHOST", "localhost")
        port = os.environ.get("PGPORT", "5432")
        # Same resolution as test_runner._DB_USER: role defaults to the login
        # user, never a hardcoded dev username (fails on any other machine).
        user = os.environ.get("PGUSER") or os.environ.get("USER", "postgres")
        pw = os.environ.get("PGPASSWORD", "")
        db = os.environ.get("PGDATABASE", "hyperdjango_test")
        conn_str = f"postgresql://{user}:{pw}@{host}:{port}/{db}"

        pool = _db_configure(conn_str, 2)
        results.append(
            bench("db_select_1", lambda: _db_query(pool, "SELECT 1", []), 10_000)
        )
        results.append(
            bench(
                "db_select_row",
                lambda: _db_query(pool, "SELECT 1, 'hello', 42.5, true, null", []),
                10_000,
            )
        )
        _db_close_pool(pool)
    except ImportError, RuntimeError:
        pass

    # ── Profiler ──────────────────────────────────────────────────────────
    try:
        from hyperdjango._hyperdjango_native import (
            _profiler_nanos,
        )

        results.append(bench("profiler_nanos", _profiler_nanos, 100_000))
    except ImportError:
        pass

    # ── Output ────────────────────────────────────────────────────────────
    if use_json:
        print(json.dumps(results, indent=2))
    else:
        print(f"\n{'Benchmark':<35} {'ns/op':>12} {'ops/sec':>15}")
        print(f"{'─' * 65}")
        for r in results:
            ops_sec = 1_000_000_000 / r["value"] if r["value"] > 0 else 0
            print(f"  {r['name']:<33} {r['value']:>10.1f}  {ops_sec:>13,.0f}")
        print()


if __name__ == "__main__":
    main()
