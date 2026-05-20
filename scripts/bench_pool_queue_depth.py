"""
Task #193: sample pg.zig pool contention during a wrk run.

Uses the new instrumentation added in zig/src/pg/pool.zig (waiters,
max_waiters, wait_count, wait_total_ns, wait_max_ns, acquire_count,
timeout_count) exposed via bookstore_api's /debug/pool/json endpoint.

Algorithm:
  1. Seed bookstore_api DB (drop + seed).
  2. Launch the native server via AppRunner with an optional
     HYPER_POOL_SIZE override forwarded to the subprocess.
  3. Start wrk against a target endpoint in the background.
  4. While wrk is running, poll /debug/pool/json every ~10 ms and
     snapshot the `waiters` counter — builds a histogram of queue
     depth across the wrk run window.
  5. After wrk exits, compute deltas in the cumulative counters
     (wait_count, wait_total_ns, wait_max_ns, acquire_count,
     timeout_count) and emit a human-readable report + JSON.

Outputs:
  logs/bench_pool_queue_depth.json — structured histogram + counters
  logs/bench_pool_queue_depth.txt  — human-readable summary

Run with the default pool size:
  uv run python scripts/bench_pool_queue_depth.py

Override pool size:
  HYPER_POOL_SIZE=8 uv run python scripts/bench_pool_queue_depth.py
"""

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import contextlib

from e2e_helper import AppRunner  # noqa: E402

LOGS = Path(__file__).resolve().parent.parent / "logs"

WRK_DURATION_S = 10  # wrk duration in seconds (≥5s stability rule)
WRK_THREADS = 4
WRK_CONNECTIONS = 32  # push above pool size so we can measure contention
SAMPLE_INTERVAL_MS = 10  # 100 samples/second → 1000 samples over 10s
POOL_SIZE = int(os.environ.get("HYPER_POOL_SIZE", "0"))

# Target endpoint — choose a DB-hitting one with ≥2 queries per request so
# contention is observable even with modest wrk concurrency.
TARGET_PATH = "/api/v1/books/"


def fetch_pool_stats(base: str) -> dict | None:
    url = f"{base}/debug/pool/json"
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            return json.loads(r.read())
    except urllib.error.URLError, TimeoutError, json.JSONDecodeError:
        return None


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("  Task #193 — Pool Queue Depth Sampler")
    print(
        f"  wrk: -t{WRK_THREADS} -c{WRK_CONNECTIONS} -d{WRK_DURATION_S}s | "
        f"sample every {SAMPLE_INTERVAL_MS}ms"
    )
    if POOL_SIZE > 0:
        print(f"  HYPER_POOL_SIZE override: {POOL_SIZE}")
    print("=" * 70)

    print("\nSetting up database...")
    setup = subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.bookstore_api.app:app",
            "--drop",
            "--seed",
            "services.bookstore_api.seed:run",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if setup.returncode != 0:
        print(f"  setup failed: {setup.stderr[-500:]}")
        sys.exit(1)

    os.environ["HYPER_LOAD_TEST"] = "1"
    os.environ["RATE_LIMIT"] = "0"

    runner_env: dict[str, str] = {}
    if POOL_SIZE > 0:
        runner_env["HYPER_POOL_SIZE"] = str(POOL_SIZE)

    with AppRunner(
        "services.bookstore_api.app:app",
        host="127.0.0.1",
        port=18803,
        env=runner_env,
    ) as runner:
        base = runner.url()
        target = f"{base}{TARGET_PATH}"

        # Snapshot pool state before the run so we can diff cumulative
        # counters at the end.
        before = fetch_pool_stats(base)
        if before is None:
            print("  failed to fetch /debug/pool/json before wrk")
            sys.exit(1)
        pool_total = before.get("total", 0)
        print(
            f"\n  Pool before: total={pool_total} "
            f"available={before.get('available')} "
            f"in_use={before.get('in_use')} "
            f"waiters={before.get('waiters')} "
            f"acquire_count={before.get('acquire_count')}"
        )

        # Warmup a few requests so prepared statements are primed.
        subprocess.run(
            ["wrk", "-t2", "-c4", "-d2s", target],
            capture_output=True,
            timeout=15,
        )

        # Reset baseline after warmup so samples only cover the real run.
        before = fetch_pool_stats(base) or before

        print(f"\n  Starting wrk against {target}...")
        wrk_proc = subprocess.Popen(
            [
                "wrk",
                f"-t{WRK_THREADS}",
                f"-c{WRK_CONNECTIONS}",
                f"-d{WRK_DURATION_S}s",
                "--latency",
                target,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Sample while wrk is running. Use wall-clock deadline instead of
        # counting samples so variance in fetch latency can't undersample.
        deadline = time.monotonic() + WRK_DURATION_S + 0.2
        sample_interval = SAMPLE_INTERVAL_MS / 1000.0
        samples: list[dict] = []
        fetch_errors = 0
        next_tick = time.monotonic()
        while time.monotonic() < deadline:
            next_tick += sample_interval
            snap = fetch_pool_stats(base)
            if snap is None:
                fetch_errors += 1
            else:
                snap["t"] = time.monotonic()
                samples.append(snap)
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)

        stdout, stderr = wrk_proc.communicate(timeout=5)
        wrk_output = stdout.decode(errors="replace")

        after = fetch_pool_stats(base)

    # Analyze
    print(f"\n  Collected {len(samples)} samples ({fetch_errors} fetch errors)")

    # Extract the waiters time series + build a histogram.
    waiters_series = [s["waiters"] for s in samples]
    max_wait_hist: dict[int, int] = {}
    for w in waiters_series:
        max_wait_hist[w] = max_wait_hist.get(w, 0) + 1

    max_waiters_seen_sample = max(waiters_series) if waiters_series else 0
    max_waiters_zig = (after or {}).get("max_waiters", before.get("max_waiters", 0))
    mean_waiters = statistics.mean(waiters_series) if waiters_series else 0.0
    p50_waiters = statistics.median(waiters_series) if waiters_series else 0
    sorted_waiters = sorted(waiters_series)
    p95_waiters = (
        sorted_waiters[int(len(sorted_waiters) * 0.95)] if sorted_waiters else 0
    )
    p99_waiters = (
        sorted_waiters[int(len(sorted_waiters) * 0.99)] if sorted_waiters else 0
    )

    # Cumulative deltas over the wrk window
    def delta(key: str) -> int:
        if not after:
            return 0
        return int(after.get(key, 0)) - int(before.get(key, 0))

    d_acquire = delta("acquire_count")
    d_wait_count = delta("wait_count")
    d_wait_total_ns = delta("wait_total_ns")
    d_timeout = delta("timeout_count")
    # Note: wait_max_ns is a peak (monotonic within run), so report
    # the after-value as the peak seen in this window (assumes it dwarfs
    # any prior max from warmup).
    peak_wait_ns = (after or {}).get("wait_max_ns", 0)

    # `d_wait_count` counts waits that STARTED during the window.
    # `d_wait_total_ns` only increments on wait COMPLETION (signal or
    # timeout). Under pathological contention the waiters can be stuck
    # in timedWait when we snapshot, so completed-wait stats are zero
    # even though wait_count is high. Report both so the distinction
    # is visible.
    d_completed_waits = d_wait_count
    if d_wait_total_ns > 0:
        avg_wait_us = d_wait_total_ns / max(d_completed_waits, 1) / 1000.0
    else:
        avg_wait_us = 0.0
    # Denominator for pct_acquires_waited: successful acquires + waits
    # that started. If acquire_count is 0 but wait_count is high, the
    # entire measurable acquire traffic went through the slow path.
    total_acquire_attempts = d_acquire + d_wait_count
    pct_acquires_waited = (
        (d_wait_count / total_acquire_attempts * 100) if total_acquire_attempts else 0.0
    )
    thread_owned_peak = 0
    for s in samples:
        t = int(s.get("thread_owned", 0))
        if t > thread_owned_peak:
            thread_owned_peak = t

    # Extract rps from wrk output
    rps = 0.0
    for line in wrk_output.splitlines():
        if "Requests/sec:" in line:
            with contextlib.suppress(ValueError):
                rps = float(line.split(":")[1].strip())

    # Build human-readable report
    lines = [
        "=" * 70,
        "  Pool Queue Depth Report (task #193)",
        "=" * 70,
        "",
        f"  Pool total: {pool_total}",
        f"  wrk: -t{WRK_THREADS} -c{WRK_CONNECTIONS} -d{WRK_DURATION_S}s "
        f"→ {rps:,.0f} rps on {TARGET_PATH}",
        f"  Sampled {len(samples)} snapshots @ {SAMPLE_INTERVAL_MS}ms interval",
        "",
        "  ── Waiters time series ──────────────────────────────────────────",
        f"    mean: {mean_waiters:.2f} | p50: {p50_waiters} | "
        f"p95: {p95_waiters} | p99: {p99_waiters}",
        f"    max observed (sampled): {max_waiters_seen_sample}",
        f"    max observed (pg.zig native counter): {max_waiters_zig}",
        "",
        "  ── Waiter histogram ─────────────────────────────────────────────",
    ]
    for depth in sorted(max_wait_hist):
        count = max_wait_hist[depth]
        pct = count / len(waiters_series) * 100 if waiters_series else 0
        bar = "█" * int(pct / 2)
        lines.append(f"    waiters={depth:>3}: {count:>5} samples ({pct:>5.1f}%) {bar}")

    lines.extend(
        [
            "",
            "  ── Acquire path counters (delta over wrk window) ────────────────",
            f"    acquire_count (slow path completions): {d_acquire:,}",
            f"    wait_count    (started waiting):       {d_wait_count:,}",
            f"    wait_total_ns (completed waits only):  {d_wait_total_ns:,}"
            f"  (avg {avg_wait_us:.2f}µs)",
            f"    wait_max_ns:                            {peak_wait_ns:,}"
            f"  (= {peak_wait_ns / 1000:.1f}µs longest)",
            f"    timeouts:                               {d_timeout:,}",
            f"    thread_owned peak: {thread_owned_peak} slots pinned",
            "",
        ]
    )

    # Recommendation — factors in the thread-owned fast path that
    # pg.zig runs in front of pool.acquire. See db.zig acquireConnByHandle.
    #
    # The key insight: pg.zig pins one connection per worker thread via
    # tryThreadOwned. Once claimed, the slot is NEVER released. That
    # means pool_size must be ≥ worker_thread_count (+ headroom for any
    # background tasks or debug endpoints that hit the same pool). If
    # pool_size is below that, the "extra" threads block in
    # pool.acquire with no possible wakeup — effectively deadlocking.
    recommendation_lines: list[str] = [
        "  ── Recommendation ───────────────────────────────────────────────"
    ]
    if pool_total == 0:
        recommendation_lines.append(
            "    ⚠ pool_total=0 — couldn't read pool size from /debug/pool/json"
        )
    elif pool_total < thread_owned_peak:
        recommendation_lines.append(
            f"    PATHOLOGICAL: pool_total={pool_total} < peak thread_owned "
            f"slots ({thread_owned_peak}). Non-slot-holding threads blocked "
            f"in pool.acquire with no wakeup path (slot-holders never release)."
        )
        recommendation_lines.append(
            f"    Raise HYPER_POOL_SIZE ≥ {thread_owned_peak + 2} to clear contention."
        )
    elif max_waiters_zig > 0:
        recommendation_lines.append(
            f"    TIGHT: pool_total={pool_total} == thread_owned_peak plus "
            f"{max_waiters_zig} waiter(s). Pool is just at capacity — any "
            f"debug endpoint, background task, or async concurrency "
            f"beyond the worker thread count will contend."
        )
        recommendation_lines.append(
            f"    Recommend HYPER_POOL_SIZE ≥ {thread_owned_peak + max_waiters_zig + 2} "
            f"for comfortable headroom."
        )
    elif d_acquire <= 5 and d_wait_count == 0:
        recommendation_lines.append(
            f"    ✓ Pool is NOT contended at {WRK_CONNECTIONS} wrk connections. "
            f"All {thread_owned_peak} active worker threads got a pinned slot; "
            f"{d_acquire} slow-path acquires across the window (mostly setup)."
        )
        recommendation_lines.append(
            f"    No reason to bump pool size above current {pool_total}."
        )
    else:
        recommendation_lines.append(
            f"    Some slow-path activity: {d_acquire} slow-path acquires, "
            f"{d_wait_count} waits. Investigate if this is background tasks "
            f"(heartbeat, auto-tuner) or unexpected worker thread churn."
        )

    lines.extend(recommendation_lines)
    lines.append("")

    report = "\n".join(lines)
    print("\n" + report)

    (LOGS / "bench_pool_queue_depth.txt").write_text(report)

    (LOGS / "bench_pool_queue_depth.json").write_text(
        json.dumps(
            {
                "config": {
                    "wrk_threads": WRK_THREADS,
                    "wrk_connections": WRK_CONNECTIONS,
                    "wrk_duration_s": WRK_DURATION_S,
                    "sample_interval_ms": SAMPLE_INTERVAL_MS,
                    "pool_size_override": POOL_SIZE if POOL_SIZE > 0 else None,
                    "target_path": TARGET_PATH,
                },
                "pool_total": pool_total,
                "rps": round(rps, 1),
                "samples_collected": len(samples),
                "fetch_errors": fetch_errors,
                "thread_owned_peak": thread_owned_peak,
                "waiters_series": {
                    "mean": round(mean_waiters, 3),
                    "p50": p50_waiters,
                    "p95": p95_waiters,
                    "p99": p99_waiters,
                    "max_sampled": max_waiters_seen_sample,
                    "max_native": max_waiters_zig,
                    "histogram": {str(k): v for k, v in sorted(max_wait_hist.items())},
                },
                "counter_deltas": {
                    "acquire_count": d_acquire,
                    "wait_count": d_wait_count,
                    "wait_total_ns": d_wait_total_ns,
                    "wait_max_ns": peak_wait_ns,
                    "timeout_count": d_timeout,
                    "pct_acquires_waited": round(pct_acquires_waited, 3),
                    "avg_wait_us": round(avg_wait_us, 3),
                },
            },
            indent=2,
        )
    )

    print(f"  JSON: {LOGS / 'bench_pool_queue_depth.json'}")
    print(f"  TXT:  {LOGS / 'bench_pool_queue_depth.txt'}")
    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
