"""
Production benchmark suite using wrk.

Measures real throughput with keep-alive connections, proper thread pools,
and accurate latency percentiles from wrk's C implementation.

Usage:
    uv run python scripts/bench_wrk.py
"""

import re
import subprocess

from e2e_helper import AppRunner


def run_wrk(
    url: str,
    threads: int = 4,
    connections: int = 100,
    duration: str = "10s",
    label: str = "",
) -> dict[str, object]:
    """Run wrk and parse results."""
    cmd = ["wrk", f"-t{threads}", f"-c{connections}", f"-d{duration}", "--latency", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr

    # Parse wrk output
    parsed: dict[str, object] = {"label": label, "raw": output}

    # Requests/sec
    m = re.search(r"Requests/sec:\s+([\d.]+)", output)
    if m:
        parsed["rps"] = float(m.group(1))

    # Latency
    m = re.search(
        r"Latency\s+([\d.]+)(us|ms|s)\s+([\d.]+)(us|ms|s)\s+([\d.]+)(us|ms|s)", output
    )
    if m:

        def to_ms(val: str, unit: str) -> float:
            v = float(val)
            if unit == "us":
                return v / 1000
            if unit == "s":
                return v * 1000
            return v

        parsed["lat_avg_ms"] = to_ms(m.group(1), m.group(2))
        parsed["lat_stdev_ms"] = to_ms(m.group(3), m.group(4))
        parsed["lat_max_ms"] = to_ms(m.group(5), m.group(6))

    # Latency percentiles
    for pct_label, pct_key in [
        ("50%", "p50"),
        ("75%", "p75"),
        ("90%", "p90"),
        ("99%", "p99"),
    ]:
        m = re.search(rf"\s+{re.escape(pct_label)}\s+([\d.]+)(us|ms|s)", output)
        if m:
            parsed[f"{pct_key}_ms"] = to_ms(m.group(1), m.group(2))

    # Transfer
    m = re.search(r"Transfer/sec:\s+([\d.]+)(\w+)", output)
    if m:
        parsed["transfer"] = f"{m.group(1)}{m.group(2)}"

    # Total requests
    m = re.search(r"(\d+) requests in", output)
    if m:
        parsed["total_requests"] = int(m.group(1))

    # Errors
    m = re.search(r"Non-2xx or 3xx responses:\s+(\d+)", output)
    if m:
        parsed["errors"] = int(m.group(1))
    else:
        parsed["errors"] = 0

    # Socket errors
    m = re.search(
        r"Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)", output
    )
    if m:
        parsed["sock_errors"] = {
            "connect": int(m.group(1)),
            "read": int(m.group(2)),
            "write": int(m.group(3)),
            "timeout": int(m.group(4)),
        }

    return parsed


def print_result(r: dict[str, object]) -> None:
    label = r.get("label", "?")
    rps = r.get("rps", 0)
    total = r.get("total_requests", 0)
    errors = r.get("errors", 0)
    p50 = r.get("p50_ms", 0)
    p99 = r.get("p99_ms", 0)
    avg = r.get("lat_avg_ms", 0)
    transfer = r.get("transfer", "?")

    sock = r.get("sock_errors", {})
    error_parts: list[str] = []
    if errors:
        error_parts.append(f"{errors:,} non-2xx")
    if sock:
        se = [f"{k}={v}" for k, v in sock.items() if v > 0]
        if se:
            error_parts.append(f"socket: {', '.join(se)}")
    error_str = f" ({'; '.join(error_parts)})" if error_parts else ""
    print(f"  {label}:")
    print(f"    {rps:,.0f} req/s | {total:,} total{error_str}")
    print(f"    avg {avg:.2f}ms | p50 {p50:.2f}ms | p99 {p99:.2f}ms")
    print(f"    transfer: {transfer}/s")
    if errors:
        # Print raw wrk output for debugging
        raw = r.get("raw", "")
        for line in raw.strip().split("\n"):
            if "Non-2xx" in line or "Socket" in line or "error" in line.lower():
                print(f"    wrk: {line.strip()}")


def main() -> None:
    duration = "5s"
    threads = 4
    conns = 50

    print("=" * 60)
    print(f"HyperDjango Benchmark Suite (wrk, {threads}t/{conns}c, {duration})")
    print("Native extension: ReleaseFast")
    print("=" * 60)

    # ── Hello (raw Zig server throughput) ────────────────────────
    print("\n--- Hello (minimal JSON, no DB, no middleware) ---")
    with AppRunner("services.hello.app:app", host="127.0.0.1", port=18700) as runner:
        print_result(
            run_wrk(
                runner.url("/"),
                threads=threads,
                connections=conns,
                duration=duration,
                label="GET / (JSON)",
            )
        )
        print_result(
            run_wrk(
                runner.url("/greet/world"),
                threads=threads,
                connections=conns,
                duration=duration,
                label="GET /greet/world (path param)",
            )
        )

    # ── Benchmark app (TechEmpower-style) ────────────────────────
    print("\n--- Benchmark App (TechEmpower-style) ---")
    with AppRunner(
        "services.benchmark_app.app:app", host="127.0.0.1", port=18710
    ) as runner:
        print_result(
            run_wrk(
                runner.url("/json"),
                threads=threads,
                connections=conns,
                duration=duration,
                label="GET /json",
            )
        )
        print_result(
            run_wrk(
                runner.url("/plaintext"),
                threads=threads,
                connections=conns,
                duration=duration,
                label="GET /plaintext",
            )
        )

    # ── REST API (middleware + DB) ───────────────────────────────
    print("\n--- REST API (middleware stack + DB queries) ---")
    with AppRunner("services.rest_api.app:app", host="127.0.0.1", port=18720) as runner:
        print_result(
            run_wrk(
                runner.url("/health"),
                threads=threads,
                connections=conns,
                duration=duration,
                label="GET /health (JSON, no DB)",
            )
        )
        print_result(
            run_wrk(
                runner.url("/api/posts"),
                threads=threads,
                connections=conns,
                duration=duration,
                label="GET /api/posts (DB query)",
            )
        )

    # ── HyperNews (full stack: middleware + DB + templates) ──────
    # Note: rate-limited at 60/min per IP. wrk uses one IP so it hits the limit fast.
    # Use very short duration and few connections to stay under limit.
    print("\n--- HyperNews (middleware + DB + Jinja2 template) ---")
    print("    NOTE: Rate-limited at 60 req/min per IP")
    with AppRunner(
        "services.hypernews.app:app", host="127.0.0.1", port=18730
    ) as runner:
        print_result(
            run_wrk(
                runner.url("/health"),
                threads=1,
                connections=1,
                duration="1s",
                label="GET /health (1 conn, rate-limited)",
            )
        )
        print_result(
            run_wrk(
                runner.url("/login"),
                threads=1,
                connections=1,
                duration="1s",
                label="GET /login (template, 1 conn)",
            )
        )

    print("\n" + "=" * 60)
    print("Benchmark complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
