"""
Task #188: cProfile audit of task_queue enqueue + channel publish hot paths.

Two subsystems, two scenarios each, all in-process (no HTTP/DB):
  1. TaskQueue.enqueue → execute → result (end-to-end dispatch)
  2. TaskQueue.enqueue fire-and-forget (pure enqueue rate)
  3. Channel.publish with 1 subscriber (minimum fan-out)
  4. Channel.publish with 16 subscribers (realistic chat room fan-out)

For each scenario: 3 runs × enough iters for ≥5s per run, median taken.

Outputs:
  logs/profile_queue_channel.txt  — human-readable top-15 per scenario
  logs/profile_queue_channel.json — structured top-30 per scenario
"""

import asyncio
import cProfile
import json
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import contextlib

from hyperdjango.channels import InMemoryChannelLayer
from hyperdjango.tasks import TaskQueue

LOGS = Path(__file__).resolve().parent.parent / "logs"

# Stability rule — each scenario run ≥5s. Iteration counts are tuned
# for typical op rates: enqueue is cheap (~1M ops/sec) so we need a
# lot; channel publish is heavier so fewer.
# NOTE: enqueue_fire_forget uses its own dedicated max_queue_size so
# it has headroom for all iterations without triggering "queue full".
SCENARIOS: dict[str, int] = {
    "enqueue_fire_forget": 200_000,  # pure enqueue, no execution
    "enqueue_execute": 10_000,  # full end-to-end
    "channel_publish_1": 100_000,  # 1 subscriber
    "channel_publish_16": 20_000,  # 16 subscribers
}
MULTI_RUN = 3


def _profile_block(name: str, iters: int, run_fn) -> dict:
    """Run `run_fn()` under cProfile for MULTI_RUN iterations, take median."""
    profiler = cProfile.Profile()
    run_times: list[float] = []
    for _ in range(MULTI_RUN):
        t0 = time.perf_counter()
        profiler.enable()
        run_fn(iters)
        profiler.disable()
        run_times.append(time.perf_counter() - t0)

    run_times_sorted = sorted(run_times)
    elapsed = run_times_sorted[len(run_times_sorted) // 2]
    rps = iters / elapsed if elapsed > 0 else 0
    per_run_rps = [iters / t for t in run_times]
    jitter_pct = ((max(per_run_rps) - min(per_run_rps)) / rps * 100 / 2) if rps else 0

    # Use pstats for structured output
    stats = pstats.Stats(profiler)
    stats.strip_dirs()

    top_entries: list[dict] = []
    total_tt = sum(v[2] for v in stats.stats.values())
    ranked_stats = sorted(stats.stats.items(), key=lambda kv: kv[1][2], reverse=True)[
        :15
    ]
    for (fname, lineno, func), (cc, nc, tt, ct, _) in ranked_stats:
        top_entries.append(
            {
                "function": f"{fname}:{lineno}:{func}",
                "call_count": cc,
                "tottime_s": round(tt, 4),
                "cumtime_s": round(ct, 4),
                "pct_of_total": round(tt / total_tt * 100, 2) if total_tt else 0,
            }
        )

    return {
        "iterations": iters,
        "multi_run": MULTI_RUN,
        "median_elapsed_s": round(elapsed, 3),
        "median_rps": round(rps, 1),
        "per_run_rps": [round(r, 1) for r in per_run_rps],
        "jitter_pct": round(jitter_pct, 2),
        "total_tottime_s": round(total_tt, 3),
        "top_15_by_tottime": top_entries,
    }


# ── Scenario 1: enqueue fire-and-forget ────────────────────────────────────
def scenario_enqueue_fire_forget(iters: int) -> dict:
    """Enqueue a cheap task N times with a non-started queue — measures
    pure enqueue path, no worker thread, no executor.

    Max queue size is sized to MULTI_RUN × iters + headroom so the
    3 runs can all fit without dropping.
    """
    tq = TaskQueue(max_queue_size=iters * MULTI_RUN + 10_000)

    # Don't start workers — we only measure enqueue itself.
    def noop_task():
        return None

    def run(n: int):
        for _ in range(n):
            with contextlib.suppress(Exception):
                tq.enqueue(noop_task)

    result = _profile_block("enqueue_fire_forget", iters, run)
    tq.stop()
    return result


# ── Scenario 2: end-to-end enqueue → execute → result ──────────────────────
def scenario_enqueue_execute(iters: int) -> dict:
    """Enqueue a task, wait for it to complete, collect result."""
    tq = TaskQueue(max_queue_size=iters + 1000, workers=4)
    tq.start()

    def cheap_task(x: int) -> int:
        return x * 2

    def run(n: int):
        # Fire all enqueues, then drain the last handles. This approximates
        # burst workloads where producers enqueue faster than workers drain.
        # Use TaskHandle.result(timeout=...) — it's the public API; the
        # old `h.wait()` method doesn't exist and the try/except was
        # silently eating AttributeError.
        handles = []
        for i in range(n):
            with contextlib.suppress(Exception):
                handles.append(tq.enqueue(cheap_task, i))
        # Drain — wait for the last 16 handles so the worker pool has
        # actually processed the tail of the enqueue burst.
        for h in handles[-16:]:
            with contextlib.suppress(RuntimeError, TimeoutError):
                h.result(timeout=30)

    result = _profile_block("enqueue_execute", iters, run)
    tq.stop()
    return result


# ── Scenario 3 + 4: channel.publish fan-out ────────────────────────────────
def _channel_publish_bench(iters: int, n_subs: int) -> dict:
    """Profile channel.publish rate with `n_subs` subscribers.

    Previous version called `loop.run_until_complete(publish_once())`
    per iteration — paying the full asyncio event-loop setup cost on
    every call. cProfile showed 45 % of self time in `kqueue.control`
    alone — pure harness overhead that drowned out the publish path.

    This version runs the entire iteration loop inside a single
    async function so the event loop is entered exactly ONCE per
    profile run. The asyncio fixed cost amortizes across all iters,
    and the cProfile top 10 now reflects the actual Channel.publish
    work (and its downstream `_deliver_to`, filter, subscriber
    callback loop).
    """
    layer = InMemoryChannelLayer()
    ch = layer.channel("bench")

    # Register n_subs simple sync callbacks. `hits` is captured by
    # closure so we can sanity-check after the run that every
    # publish was delivered to every subscriber.
    hits = [0] * n_subs
    for i in range(n_subs):
        idx = i

        def cb(msg, idx=idx):
            hits[idx] += 1

        ch.subscribe(f"sub{i}", cb)

    async def publish_batch(n: int) -> None:
        msg = {"n": 1}
        for _ in range(n):
            await ch.publish(msg)

    def run(n: int):
        asyncio.run(publish_batch(n))

    result = _profile_block(f"channel_publish_{n_subs}", iters, run)

    # Sanity check: every subscriber should have received every
    # publish across all MULTI_RUN iterations.
    expected = iters * MULTI_RUN
    for i, h in enumerate(hits):
        if h != expected:
            print(f"  ⚠ subscriber {i} received {h} hits, expected {expected}")

    return result


def scenario_channel_publish_1(iters: int) -> dict:
    return _channel_publish_bench(iters, 1)


def scenario_channel_publish_16(iters: int) -> dict:
    return _channel_publish_bench(iters, 16)


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print("  Task #188: task_queue + channel cProfile audit")
    print("=" * 70)

    results: dict[str, dict] = {}
    lines: list[str] = []

    scenarios_to_run = [
        ("enqueue_fire_forget", scenario_enqueue_fire_forget),
        ("enqueue_execute", scenario_enqueue_execute),
        ("channel_publish_1", scenario_channel_publish_1),
        ("channel_publish_16", scenario_channel_publish_16),
    ]

    for slug, fn in scenarios_to_run:
        iters = SCENARIOS[slug]
        print(f"\n── {slug} (iters={iters}) ──")
        result = fn(iters)
        results[slug] = result
        print(
            f"  rps: {result['median_rps']:,.0f} | "
            f"per-run: {[f'{r:,.0f}' for r in result['per_run_rps']]} | "
            f"jitter: ±{result['jitter_pct']:.1f}% | "
            f"elapsed: {result['median_elapsed_s']}s"
        )
        print("  Top 10 by SELF time:")
        for entry in result["top_15_by_tottime"][:10]:
            print(
                f"    {entry['tottime_s']:>8.3f}s  {entry['call_count']:>8}  "
                f"{entry['pct_of_total']:>5.1f}%  {entry['function']}"
            )

        lines.append(f"## {slug}")
        lines.append(
            f"iters={iters} rps={result['median_rps']:,.0f} jitter=±{result['jitter_pct']:.1f}%"
        )
        lines.append(f"per-run-rps={result['per_run_rps']}")
        lines.append("")
        lines.append("| Rank | tottime | calls | pct | function |")
        lines.append("|------|--------:|------:|----:|----------|")
        for rank, entry in enumerate(result["top_15_by_tottime"], 1):
            lines.append(
                f"| {rank} | {entry['tottime_s']:.3f}s | {entry['call_count']} | "
                f"{entry['pct_of_total']:.1f}% | `{entry['function']}` |"
            )
        lines.append("")

    (LOGS / "profile_queue_channel.json").write_text(json.dumps(results, indent=2))
    (LOGS / "profile_queue_channel.txt").write_text("\n".join(lines))
    print(f"\n  JSON: {LOGS / 'profile_queue_channel.json'}")
    print(f"  TXT:  {LOGS / 'profile_queue_channel.txt'}")

    print("\n" + "=" * 70)
    print("  Audit complete")
    print("=" * 70)


if __name__ == "__main__":
    main()
