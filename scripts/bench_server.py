#!/usr/bin/env python3
"""
Benchmark the native Zig HTTP server vs uvicorn ASGI server.

Starts each server, fires concurrent requests, measures throughput.

Run: uv run python scripts/bench_server.py
"""

import concurrent.futures
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

NATIVE_PORT = 19881
UVICORN_PORT = 19882
REQUESTS = 1000
CONCURRENCY = 10


def make_test_app(port, mode):
    """Create a test app file for the given mode."""
    path = Path(__file__).parent / f"_bench_{mode}.py"
    with path.open("w") as f:
        if mode == "native":
            f.write(f"""
import sys, os
from hyperdjango import HyperApp

app = HyperApp(title="Bench Native")

@app.get("/json")
def json_ep(request):
    return {{"message": "Hello, World!", "server": "zig"}}

@app.get("/plaintext")
def text_ep(request):
    from hyperdjango import Response
    return Response.text("Hello, World!")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port={port})
""")
        else:  # uvicorn
            f.write(f"""
import sys, os
from hyperdjango import HyperApp

app = HyperApp(title="Bench ASGI")

@app.get("/json")
async def json_ep(request):
    return {{"message": "Hello, World!", "server": "uvicorn"}}

@app.get("/plaintext")
async def text_ep(request):
    from hyperdjango import Response
    return Response.text("Hello, World!")

if __name__ == "__main__":
    app._run_asgi("127.0.0.1", {port})
""")
    return str(path)


def start_server(app_path):
    """Start a server subprocess."""
    return subprocess.Popen(
        [sys.executable, app_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_for_server(port, timeout=5):
    """Wait for a server to be ready."""
    for _ in range(int(timeout * 10)):
        time.sleep(0.1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1)
            return True
        except urllib.error.URLError, ConnectionRefusedError, OSError:
            pass
    return False


def fire_request(url):
    """Fire a single request and return success/failure + latency."""
    start = time.perf_counter_ns()
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        body = resp.read()
        elapsed_ns = time.perf_counter_ns() - start
        return True, elapsed_ns
    except Exception:
        elapsed_ns = time.perf_counter_ns() - start
        return False, elapsed_ns


def benchmark_server(name, port, endpoint="/json"):
    """Benchmark a server with concurrent requests."""
    url = f"http://127.0.0.1:{port}{endpoint}"

    # Warmup
    for _ in range(10):
        fire_request(url)

    # Benchmark
    start = time.perf_counter()
    results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(fire_request, url) for _ in range(REQUESTS)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    elapsed = time.perf_counter() - start

    successes = sum(1 for ok, _ in results if ok)
    latencies = [ns for ok, ns in results if ok]

    if latencies:
        avg_latency_us = sum(latencies) / len(latencies) / 1000
        p50 = sorted(latencies)[len(latencies) // 2] / 1000
        p99 = sorted(latencies)[int(len(latencies) * 0.99)] / 1000
    else:
        avg_latency_us = p50 = p99 = 0

    rps = successes / elapsed

    return {
        "name": name,
        "requests": REQUESTS,
        "successes": successes,
        "rps": rps,
        "avg_us": avg_latency_us,
        "p50_us": p50,
        "p99_us": p99,
        "elapsed": elapsed,
    }


def print_results(results):
    print(
        f"\n{'Server':<20} {'Req/s':>10} {'Avg':>10} {'P50':>10} {'P99':>10} {'OK':>6}"
    )
    print("-" * 70)
    for r in results:
        print(
            f"{r['name']:<20} {r['rps']:>10.0f} {r['avg_us']:>9.0f}us {r['p50_us']:>9.0f}us "
            f"{r['p99_us']:>9.0f}us {r['successes']:>5}/{r['requests']}"
        )

    if len(results) == 2 and results[1]["rps"] > 0:
        speedup = results[0]["rps"] / results[1]["rps"]
        print(
            f"\n  Native is {speedup:.1f}x {'faster' if speedup > 1 else 'slower'} than ASGI"
        )


def main():
    print("HyperDjango Server Benchmark")
    print(f"  Requests: {REQUESTS}")
    print(f"  Concurrency: {CONCURRENCY}")

    results = []
    servers = []

    # Native Zig server
    print("\nStarting native Zig server...", end=" ", flush=True)
    app_path = make_test_app(NATIVE_PORT, "native")
    proc = start_server(app_path)
    if wait_for_server(NATIVE_PORT):
        print("ready")
        r = benchmark_server("Native Zig", NATIVE_PORT)
        results.append(r)
    else:
        print("FAILED (skipping)")
        stderr = proc.stderr.read().decode()[:200] if proc.stderr else ""
        print(f"  Error: {stderr}")
    servers.append((proc, app_path))

    # Uvicorn ASGI server
    try:
        import uvicorn  # noqa: F401

        print("Starting uvicorn ASGI server...", end=" ", flush=True)
        app_path2 = make_test_app(UVICORN_PORT, "uvicorn")
        proc2 = start_server(app_path2)
        if wait_for_server(UVICORN_PORT):
            print("ready")
            r = benchmark_server("ASGI (uvicorn)", UVICORN_PORT)
            results.append(r)
        else:
            print("FAILED (skipping)")
        servers.append((proc2, app_path2))
    except ImportError:
        print("uvicorn not installed — skipping ASGI benchmark")

    print_results(results)

    # Cleanup
    for proc, path in servers:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        Path(path).unlink()


if __name__ == "__main__":
    main()
