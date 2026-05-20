"""
Load tests for HyperDjango services.

Measures throughput and latency under concurrent load using
Python threads (no external tools required).
"""

# hyper-test: e2e

import statistics
import subprocess
import threading
import time

from e2e_helper import TEST_PORTS, AppRunner, http_get

from hyperdjango.testkit import check, finish, run_main


def _setup_app(app: str, seed: str) -> None:
    """Create + seed an service's schema before load-testing it.

    Mirrors the DB-backed e2e tests (e.g. blog_platform): a load test that
    hits DB endpoints must first build the tables via `hyper setup`, otherwise
    the very first query fails with `relation "..." does not exist`.
    """
    subprocess.run(
        ["uv", "run", "hyper", "setup", "--app", app, "--drop", "--seed", seed],
        capture_output=True,
        timeout=120,
    )


def _run_requests(
    url: str,
    count: int,
    results: list[float],
    errors: list[int],
    rate_limited: list[int],
) -> None:
    """Worker thread: send requests, record latencies."""
    for _ in range(count):
        start = time.perf_counter()
        try:
            r = http_get(url)
            elapsed = time.perf_counter() - start
            if r.status == 200:
                results.append(elapsed * 1000)  # ms
            elif r.status == 429:
                rate_limited.append(1)
            else:
                errors.append(r.status)
        except Exception:
            errors.append(0)


def bench(name: str, url: str, threads: int = 8, requests_per_thread: int = 50) -> bool:
    """Run a concurrent benchmark, print results, and record one check.

    The check asserts only that the endpoint served the load at all — some
    request got a response (200 or a 429 from the app's own rate limiter).
    Latency/throughput stay reported, never asserted: this file measures, it
    does not gate on numbers.
    """
    total_requests = threads * requests_per_thread
    results: list[float] = []
    errors: list[int] = []
    rate_limited: list[int] = []

    workers = []
    start = time.perf_counter()
    for _ in range(threads):
        t = threading.Thread(
            target=_run_requests,
            args=(url, requests_per_thread, results, errors, rate_limited),
        )
        workers.append(t)
        t.start()
    for t in workers:
        t.join()
    wall_time = time.perf_counter() - start

    if results:
        results.sort()
        p50 = results[len(results) // 2]
        p99 = results[int(len(results) * 0.99)]
        avg = statistics.mean(results)
        rps = len(results) / wall_time

        print(f"  {name}:")
        parts = [f"{len(results)}/{total_requests} ok"]
        if rate_limited:
            parts.append(f"{len(rate_limited)} rate-limited")
        if errors:
            parts.append(f"{len(errors)} errors")
        print(f"    {', '.join(parts)}")
        print(
            f"    {rps:.0f} req/s | avg {avg:.1f}ms | p50 {p50:.1f}ms | p99 {p99:.1f}ms"
        )
        print(f"    wall time: {wall_time:.2f}s")
    else:
        if rate_limited:
            print(f"  {name}: ALL RATE-LIMITED ({len(rate_limited)} × 429)")
        else:
            print(f"  {name}: ALL FAILED ({len(errors)} errors)")

    return check(
        name,
        bool(results) or bool(rate_limited),
        f"no response served: {len(errors)}/{total_requests} errored",
    )


def main() -> bool:
    print("=" * 60)
    print("HyperDjango Load Tests")
    print("=" * 60)

    # ── REST API (JSON + DB endpoints) ───────────────────────────
    print("\n--- REST API (JSON + DB) ---")
    _setup_app("services.rest_api.app:app", "services.rest_api.seed:run")
    with AppRunner(
        "services.rest_api.app:app", host="127.0.0.1", port=TEST_PORTS["load_rest"]
    ) as runner:
        base = runner.url()
        bench("GET /health", f"{base}/health")
        bench("GET /openapi.json", f"{base}/openapi.json")
        bench("GET /api/posts (list)", f"{base}/api/posts")

    # ── HyperNews (HTML + DB queries) ────────────────────────────
    # Rate limit: 60 req/min per IP. Keep total under 55 to avoid 429s.
    print("\n--- HyperNews (HTML + DB, rate-limited at 60/min) ---")
    _setup_app("services.hypernews.app:app", "services.hypernews.seed:run")
    with AppRunner(
        "services.hypernews.app:app",
        host="127.0.0.1",
        port=TEST_PORTS["load_hypernews"],
    ) as runner:
        base = runner.url()
        bench("GET / (front page)", f"{base}/", threads=4, requests_per_thread=12)
        bench(
            "GET /login (template)", f"{base}/login", threads=1, requests_per_thread=5
        )

    # ── Hello (minimal, raw throughput) ──────────────────────────
    print("\n--- Hello (minimal JSON) ---")
    with AppRunner(
        "services.hello.app:app", host="127.0.0.1", port=TEST_PORTS["load_hello"]
    ) as runner:
        base = runner.url()
        bench("GET / (JSON)", f"{base}/", threads=16, requests_per_thread=100)
        bench(
            "GET /greet/world",
            f"{base}/greet/world",
            threads=16,
            requests_per_thread=100,
        )

    print("\n" + "=" * 60)
    print("Load tests complete")
    print("=" * 60)
    return finish()


if __name__ == "__main__":
    run_main(main)
