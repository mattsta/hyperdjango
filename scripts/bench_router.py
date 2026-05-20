#!/usr/bin/env python3
"""
Benchmark: Native radix trie router vs Python regex router.

Compares route resolution speed for static and dynamic routes.

Run: uv run python scripts/bench_router.py
"""

import time

from hyperdjango.router import Router

N = 50000


def bench_router():
    router = Router()

    # Register routes — mix of static and dynamic
    def handler():
        pass

    # Static routes
    for i in range(20):
        router.add("GET", f"/api/v1/resource{i}", handler)

    # Dynamic routes with params
    router.add("GET", "/users/{id:int}", handler)
    router.add("GET", "/users/{id:int}/posts/{post_id:int}", handler)
    router.add("GET", "/products/{slug:str}", handler)
    router.add("GET", "/files/{path:path}", handler)
    router.add("POST", "/users/{id:int}/comments", handler)
    router.add("PUT", "/users/{id:int}", handler)
    router.add("DELETE", "/users/{id:int}", handler)
    router.add("GET", "/orgs/{org}/teams/{team}/members/{member}", handler)

    using_native = router._native_handle is not None

    # Optimize router: compress paths, sort children (like app.run() does)
    router.finalize()

    print(f"Router: {'Native radix trie (Zig)' if using_native else 'Python regex'}")
    print(f"Iterations: {N}")
    print("=" * 60)

    # Benchmark static route resolution
    start = time.perf_counter_ns()
    for _ in range(N):
        router.resolve("GET", "/api/v1/resource5")
    ns = (time.perf_counter_ns() - start) / N
    print(
        f"  Static route:                {ns:>8.0f} ns/resolve  ({N / (ns / 1e9):>10.0f} ops/sec)"
    )

    # Benchmark dynamic route with 1 param
    start = time.perf_counter_ns()
    for _ in range(N):
        route, params = router.resolve("GET", "/users/42")
    ns = (time.perf_counter_ns() - start) / N
    assert route is not None and params.get("id") == 42
    print(
        f"  Dynamic 1 param (/users/42): {ns:>8.0f} ns/resolve  ({N / (ns / 1e9):>10.0f} ops/sec)"
    )

    # Benchmark dynamic route with 2 params
    start = time.perf_counter_ns()
    for _ in range(N):
        route, params = router.resolve("GET", "/users/42/posts/7")
    ns = (time.perf_counter_ns() - start) / N
    assert route is not None and params.get("id") == 42 and params.get("post_id") == 7
    print(
        f"  Dynamic 2 params:            {ns:>8.0f} ns/resolve  ({N / (ns / 1e9):>10.0f} ops/sec)"
    )

    # Benchmark dynamic route with 3 params
    start = time.perf_counter_ns()
    for _ in range(N):
        route, params = router.resolve("GET", "/orgs/acme/teams/eng/members/alice")
    ns = (time.perf_counter_ns() - start) / N
    assert route is not None
    print(
        f"  Dynamic 3 params:            {ns:>8.0f} ns/resolve  ({N / (ns / 1e9):>10.0f} ops/sec)"
    )

    # Benchmark miss (no matching route)
    start = time.perf_counter_ns()
    for _ in range(N):
        router.resolve("GET", "/nonexistent/path/here")
    ns = (time.perf_counter_ns() - start) / N
    print(
        f"  Miss (no match):             {ns:>8.0f} ns/resolve  ({N / (ns / 1e9):>10.0f} ops/sec)"
    )


if __name__ == "__main__":
    bench_router()
