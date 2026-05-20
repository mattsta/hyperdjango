#!/usr/bin/env python3
"""Capacity self-scaling equations + Python/Zig lockstep.

``hyperdjango.capacity`` mirrors the native server's worker-sizing equation
(``zig/src/server.zig``) so the database pool sizes to the same worker count
the server actually runs. This proves the equation (floor, ceiling, budget
parsing, override precedence) and asserts the shared constants still match
the values embedded in the Zig source — if either side drifts, a big machine
runs more server workers than the pool has connections and DB handlers fail.

Usage:
    uv run hyper-test capacity_scaling
    uv run python scripts/test_capacity_scaling.py
"""

# hyper-test: unit

import re
from pathlib import Path

from hyperdjango import capacity
from hyperdjango.testkit import check, finish, run_main

_SERVER_ZIG = Path(__file__).resolve().parent.parent / "zig" / "src" / "server.zig"


def test_auto_workers() -> None:
    # At or below the historic default, never size down (small machines unchanged).
    check("budget 1 → floor", capacity.auto_workers(1) == capacity.WORKER_AUTO_MIN)
    check("budget 12 → floor", capacity.auto_workers(12) == capacity.WORKER_AUTO_MIN)
    check("budget 24 → floor", capacity.auto_workers(24) == capacity.WORKER_AUTO_MIN)
    # Above the floor, workers track the budget…
    check("budget 64 → 64", capacity.auto_workers(64) == 64)
    check("budget 128 → 128", capacity.auto_workers(128) == 128)
    check("budget 512 → 512", capacity.auto_workers(512) == 512)
    # …up to the ceiling.
    check(
        "budget 1024 → ceiling",
        capacity.auto_workers(1024) == capacity.WORKER_AUTO_CEILING,
    )


def test_cpu_budget_parsing(monkeyish) -> None:
    with monkeyish({"HYPER_CPU_BUDGET": None}):
        cores = capacity.detect_cores()
        check("unset budget → all cores", capacity.cpu_budget() == cores)
    with monkeyish({"HYPER_CPU_BUDGET": "0.5"}):
        check(
            "fraction halves cores", capacity.cpu_budget() == max((cores + 1) // 2, 1)
        )
    with monkeyish({"HYPER_CPU_BUDGET": "50%"}):
        check("percent halves cores", capacity.cpu_budget() == max((cores + 1) // 2, 1))
    with monkeyish({"HYPER_CPU_BUDGET": "4"}):
        check("absolute budget caps at request", capacity.cpu_budget() == min(4, cores))
    with monkeyish({"HYPER_CPU_BUDGET": "garbage"}):
        check("garbage → all cores", capacity.cpu_budget() == cores)
    with monkeyish({"HYPER_CPU_BUDGET": "0"}):
        check("zero → all cores", capacity.cpu_budget() == cores)


def test_resolve_worker_count_override_wins(monkeyish) -> None:
    with monkeyish({"HYPER_THREAD_POOL_SIZE": "200"}):
        check("explicit override honored", capacity.resolve_worker_count() == 200)
    with monkeyish({"HYPER_THREAD_POOL_SIZE": "0"}):
        check(
            "zero override → auto",
            capacity.resolve_worker_count() == capacity.auto_workers(),
        )
    with monkeyish({"HYPER_THREAD_POOL_SIZE": None}):
        check(
            "unset → auto",
            capacity.resolve_worker_count() == capacity.auto_workers(),
        )
    with monkeyish({"HYPER_THREAD_POOL_SIZE": "999999"}):
        check(
            "fat-finger clamped to hard max",
            capacity.resolve_worker_count() == capacity.WORKER_HARD_MAX,
        )


def _zig_const(name: str) -> int:
    src = _SERVER_ZIG.read_text()
    m = re.search(rf"const {name}[^=]*=\s*(\d+)", src)
    if not m:
        raise AssertionError(f"{name} not found in server.zig")
    return int(m.group(1))


def test_zig_lockstep() -> None:
    # The DB pool would under-serve the server if these drift apart.
    check(
        "WORKER_AUTO_MIN matches Zig DEFAULT_POOL_SIZE",
        _zig_const("DEFAULT_POOL_SIZE") == capacity.WORKER_AUTO_MIN,
        f"py={capacity.WORKER_AUTO_MIN} zig={_zig_const('DEFAULT_POOL_SIZE')}",
    )
    check(
        "WORKER_AUTO_CEILING matches Zig",
        _zig_const("WORKER_AUTO_CEILING") == capacity.WORKER_AUTO_CEILING,
        f"py={capacity.WORKER_AUTO_CEILING} zig={_zig_const('WORKER_AUTO_CEILING')}",
    )
    check(
        "WORKER_HARD_MAX matches Zig",
        _zig_const("WORKER_HARD_MAX") == capacity.WORKER_HARD_MAX,
        f"py={capacity.WORKER_HARD_MAX} zig={_zig_const('WORKER_HARD_MAX')}",
    )


def main() -> bool:
    import contextlib
    import os

    @contextlib.contextmanager
    def monkeyish(overrides):
        # env-boundary: a test harness toggling the native server's own env
        # contract to exercise the capacity mirror; restored on exit.
        saved = {k: os.environ.get(k) for k in overrides}
        try:
            for k, v in overrides.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    print("=" * 64)
    print("Capacity self-scaling + Python/Zig lockstep")
    print("=" * 64)
    test_auto_workers()
    test_cpu_budget_parsing(monkeyish)
    test_resolve_worker_count_override_wins(monkeyish)
    test_zig_lockstep()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
