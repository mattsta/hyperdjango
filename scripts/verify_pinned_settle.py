#!/usr/bin/env python3
"""Prove the R9 settle condition is sound when the DB executor is slow.

The R9 leak assertions passed on a fast dev box and failed on a 2-core CI
runner. The cause was the settle condition, not the platform: the old poll
returned as soon as ``in_use <= base``, and its FIRST look happens before the
cancelled transactions' shielded acquires reach the pool — so it read the
baseline, returned immediately, the acquires then landed, and the caller
reported a leak that did not exist.

This reproduces that machine difference deterministically by delaying the
native acquire, and compares the two settle conditions on identical runs:

    uv run python scripts/verify_pinned_settle.py            # default 15ms
    uv run python scripts/verify_pinned_settle.py --delay 0.05

Expected: the gauge-only condition reports a false leak, the acquire/release
balance condition does not. Exits non-zero if the balance condition ever
reports a leak (that WOULD be a real platform bug) or if the gauge condition
stops being fooled (the reproduction no longer reproduces, so the guard it
justifies would be untestable).
"""

import argparse
import asyncio
import contextlib
import os
import sys
import threading
import time

import hyperdjango.database as _dbmod
from hyperdjango.database import Database, mark_loop_multiplexing

ITERATIONS = 40


def _run_on_multiplexing_loop(coro_fn):
    box: dict = {}

    def runner():
        loop = asyncio.new_event_loop()
        mark_loop_multiplexing(loop)
        try:
            box["value"] = loop.run_until_complete(coro_fn())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=runner, name="verify-pinned-settle")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


async def _measure(db_url: str, delay_s: float) -> tuple[int, int, int]:
    """Cancel ITERATIONS transactions at the acquire await with a slowed
    acquire. Returns (base, in_use when the GAUGE condition says settled,
    in_use when the BALANCE condition says settled)."""
    db = Database(db_url)
    await db.connect()

    lock = threading.Lock()
    counts = {"acquired": 0, "released": 0}
    real_acquire = _dbmod._db_conn_acquire
    real_release = _dbmod._db_conn_release

    def slow_acquire(pool_handle):
        # An op counts as outstanding from the moment the executor picks it up,
        # NOT from when it reaches the pool — that gap is the whole point. The
        # stall stands in for a CPU-starved executor thread on a 2-core runner:
        # the work exists, but the pool gauge cannot see it yet.
        with lock:
            counts["acquired"] += 1
        time.sleep(delay_s)
        return real_acquire(pool_handle)

    def counted_release(handle):
        with lock:
            counts["released"] += 1
        return real_release(handle)

    def pending() -> int:
        with lock:
            return counts["acquired"] - counts["released"]

    _dbmod._db_conn_acquire = slow_acquire
    _dbmod._db_conn_release = counted_release
    try:
        for _ in range(5):
            await asyncio.sleep(0.01)
        base = db.pool_stats()["in_use"]

        for _ in range(ITERATIONS):

            async def run_tx():
                async with db.transaction():
                    await db.execute("SELECT 1")

            task = asyncio.ensure_future(run_tx())
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        # Condition A — the old one: gauge only. What matters is not the value
        # it reports but WHETHER IT IS ENTITLED TO REPORT ANYTHING: record how
        # much work was still outstanding at the instant it declared "settled".
        for _ in range(9000):
            if db.pool_stats()["in_use"] <= base:
                break
            await asyncio.sleep(0.01)
        outstanding_when_gauge_settled = pending()

        # Condition B — the fix: every acquire has a matching release.
        for _ in range(9000):
            if pending() == 0 and db.pool_stats()["in_use"] <= base:
                break
            await asyncio.sleep(0.01)
        balance_final = db.pool_stats()["in_use"]
        return base, outstanding_when_gauge_settled, balance_final
    finally:
        _dbmod._db_conn_acquire = real_acquire
        _dbmod._db_conn_release = real_release
        await db.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.015,
        help="seconds to stall each native acquire (default 0.015)",
    )
    args = parser.parse_args()

    # env-boundary: standalone verification tool, run outside the framework.
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("DATABASE_URL is not set — this tool needs a live PostgreSQL.")
        return 2

    base, outstanding_when_gauge_settled, balance_final = _run_on_multiplexing_loop(
        lambda: _measure(db_url, args.delay)
    )
    print(f"  acquire stalled by {args.delay * 1000:.0f}ms, {ITERATIONS} cancellations")
    print(f"  baseline in_use                          : {base}")
    print(f"  acquires still outstanding when the")
    print(
        f"    GAUGE-ONLY condition said 'settled'    : {outstanding_when_gauge_settled}"
    )
    print(f"  in_use once the BALANCE condition settled : {balance_final}")

    ok = True
    if balance_final > base:
        print(
            f"\nFAIL: the balance condition reported a leak "
            f"({base} -> {balance_final}). That is a REAL pinned-connection "
            f"leak in the platform, not a test artifact."
        )
        ok = False
    if outstanding_when_gauge_settled == 0:
        print(
            "\nFAIL: the gauge-only condition was NOT fooled — no work was "
            "outstanding when it claimed the pool had settled, so this run does "
            "not reproduce the CI condition. Raise --delay until it does."
        )
        ok = False
    if ok:
        print(
            f"\nOK: the gauge-only condition declared the pool settled while "
            f"{outstanding_when_gauge_settled} acquire(s) were still in flight — "
            f"whatever the caller reads next is meaningless, which is exactly the "
            f"false 'in_use 1 -> 7' leak CI reported. The acquire/release balance "
            f"waited for real completion and found no leak."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
