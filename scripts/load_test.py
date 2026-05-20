#!/usr/bin/env python3
"""Load testing for hyperdjango native server.

Runs sustained load against the Zig HTTP server using concurrent connections.
Measures throughput, latency percentiles, and error rates.

Usage:
    # Start server first:
    uv run python -c "
    from hyperdjango import HyperApp
    app = HyperApp('loadtest')
    @app.route('GET', '/ping')
    def ping(request): return {'status': 'ok'}
    app.listen(port=9876)
    "

    # Then run load test:
    uv run python scripts/load_test.py --url http://localhost:9876/ping --duration 60 --concurrency 16
"""

import argparse
import http.client
import statistics
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class LoadResult:
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    latencies_us: list = field(default_factory=list)
    status_codes: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    start_time: float = 0
    end_time: float = 0

    @property
    def duration(self):
        return self.end_time - self.start_time

    @property
    def rps(self):
        return self.total_requests / self.duration if self.duration > 0 else 0

    def merge(self, other):
        self.total_requests += other.total_requests
        self.successful += other.successful
        self.failed += other.failed
        self.latencies_us.extend(other.latencies_us)
        self.status_codes.update(other.status_codes)
        self.errors.update(other.errors)


def worker(url, duration_s, result):
    """Single worker thread — sends requests in a tight loop."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"

    deadline = time.monotonic() + duration_s

    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            t0 = time.perf_counter_ns()
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            t1 = time.perf_counter_ns()

            latency_us = (t1 - t0) / 1000
            result.total_requests += 1
            result.latencies_us.append(latency_us)
            result.status_codes[resp.status] += 1

            if 200 <= resp.status < 400:
                result.successful += 1
            else:
                result.failed += 1

            conn.close()
        except Exception as e:
            result.total_requests += 1
            result.failed += 1
            result.errors[type(e).__name__] += 1


def run_load_test(url, duration_s, concurrency):
    """Run load test with N concurrent workers."""
    print(f"\nLoad Test: {url}")
    print(f"  Duration: {duration_s}s, Concurrency: {concurrency}")
    print("  Running...\n")

    results = [LoadResult() for _ in range(concurrency)]
    threads = []

    start = time.monotonic()
    for i in range(concurrency):
        t = threading.Thread(target=worker, args=(url, duration_s, results[i]))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()
    end = time.monotonic()

    # Merge results
    merged = LoadResult()
    merged.start_time = start
    merged.end_time = end
    for r in results:
        merged.merge(r)

    # Print results
    print(f"  {'─' * 50}")
    print(f"  Total requests:  {merged.total_requests:,}")
    print(f"  Successful:      {merged.successful:,}")
    print(f"  Failed:          {merged.failed:,}")
    print(f"  Duration:        {merged.duration:.2f}s")
    print(f"  Throughput:      {merged.rps:,.0f} req/s")

    if merged.latencies_us:
        sorted_lat = sorted(merged.latencies_us)
        print("\n  Latency:")
        print(f"    Min:    {sorted_lat[0]:,.0f} μs")
        print(f"    Mean:   {statistics.mean(sorted_lat):,.0f} μs")
        print(f"    Median: {sorted_lat[len(sorted_lat) // 2]:,.0f} μs")
        print(f"    p95:    {sorted_lat[int(len(sorted_lat) * 0.95)]:,.0f} μs")
        print(f"    p99:    {sorted_lat[int(len(sorted_lat) * 0.99)]:,.0f} μs")
        print(f"    Max:    {sorted_lat[-1]:,.0f} μs")

    if merged.status_codes:
        print("\n  Status codes:")
        for code, count in sorted(merged.status_codes.items()):
            print(f"    {code}: {count:,}")

    if merged.errors:
        print("\n  Errors:")
        for err, count in merged.errors.most_common(10):
            print(f"    {err}: {count:,}")

    # Pass/fail criteria
    error_rate = (
        merged.failed / merged.total_requests if merged.total_requests > 0 else 1
    )
    print(f"\n  Error rate: {error_rate * 100:.2f}%")
    if error_rate > 0.01:
        print("  FAIL: Error rate > 1%")
        return False
    print("  PASS: Error rate < 1%")
    return True


def main():
    parser = argparse.ArgumentParser(description="Load test hyperdjango")
    parser.add_argument(
        "--url", default="http://localhost:9876/ping", help="URL to test"
    )
    parser.add_argument("--duration", type=int, default=10, help="Duration in seconds")
    parser.add_argument(
        "--concurrency", type=int, default=8, help="Concurrent connections"
    )
    args = parser.parse_args()

    success = run_load_test(args.url, args.duration, args.concurrency)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
