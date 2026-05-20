#!/usr/bin/env python3
"""Native-path coroutine runner: eager-Task semantics + reentrancy matrix.

``hyperdjango.native._coro.run_coro_on_loop`` is the hot path under EVERY
async handler on the native server, and it leans on three stdlib-private
asyncio hooks (``events._set_running_loop``, ``BaseEventLoop._thread_id``,
``tasks._swap_current_task``) to run an eager Task step outside a spinning
loop. This file is the canary for that dependence: if a CPython upgrade
changes any of those semantics, it must fail HERE — loudly, with the shape
that broke — not as production 500s. It pins the full nesting matrix that
broke once already (an inner call clobbering the outer eager step's state)
plus the task-context guarantees the runner exists to provide
(``current_task()`` inside handlers, so ``asyncio.wait_for`` /
``asyncio.timeout`` work — the old hand-rolled stepper silently broke them).

Usage:
    uv run hyper-test coro_runner
"""

# hyper-test: unit

import asyncio
import contextvars

from hyperdjango.native._coro import get_thread_event_loop, run_coro_on_loop
from hyperdjango.testkit import check, finish, run_main

_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("coro_runner_var")


async def _sync_value() -> int:
    return 41


async def _suspending_value() -> int:
    await asyncio.sleep(0.002)
    return 40


async def _wait_for_timeout() -> str:
    try:
        await asyncio.wait_for(asyncio.sleep(5), timeout=0.01)
    except TimeoutError:
        return "timeout-caught"
    return "no-timeout"


async def _uses_current_task() -> bool:
    return asyncio.current_task() is not None


async def _gather() -> int:
    async def part(n: int) -> int:
        await asyncio.sleep(0.001)
        return n

    return sum(await asyncio.gather(part(1), part(2), part(3)))


async def _raises() -> None:
    raise ValueError("expected-error")


async def _nested_sync() -> int:
    # A handler synchronously invoking another native wrapper on the same
    # thread/loop (double-wrapped handlers, in-process drivers).
    return run_coro_on_loop(get_thread_event_loop(), _sync_value()) + 1


async def _nested_suspend() -> int:
    return run_coro_on_loop(get_thread_event_loop(), _suspending_value()) + 2


async def _in_drive_nested_sync() -> int:
    # Suspend first so we resume INSIDE the blocking drive (loop genuinely
    # running), then nest a never-suspending call — must complete eagerly.
    await asyncio.sleep(0.001)
    return run_coro_on_loop(get_thread_event_loop(), _sync_value()) + 3


async def _in_drive_nested_suspend() -> int:
    # The one impossible shape: a SUSPENDING coroutine resolved from a
    # callback of the genuinely-running loop. Must fail fast, never deadlock.
    await asyncio.sleep(0.001)
    return run_coro_on_loop(get_thread_event_loop(), _suspending_value())


async def _context_isolated() -> str:
    _VAR.set("inner")
    return _VAR.get()


def main() -> bool:
    loop = get_thread_event_loop()

    check("fast path (never suspends)", run_coro_on_loop(loop, _sync_value()) == 41)
    check("suspend path", run_coro_on_loop(loop, _suspending_value()) == 40)
    check(
        "current_task() registered during eager step",
        run_coro_on_loop(loop, _uses_current_task()) is True,
    )
    check(
        "wait_for/timeout machinery works (the bug the runner fixed)",
        run_coro_on_loop(loop, _wait_for_timeout()) == "timeout-caught",
    )
    check("gather over real futures", run_coro_on_loop(loop, _gather()) == 6)

    try:
        run_coro_on_loop(loop, _raises())
        check("handler exception re-raised", False, "no exception surfaced")
    except ValueError as exc:
        check("handler exception re-raised", str(exc) == "expected-error")

    # Reentrancy matrix — the shapes that clobbered outer eager-step state.
    check(
        "nested: sync inside eager step", run_coro_on_loop(loop, _nested_sync()) == 42
    )
    check(
        "nested: suspend inside eager step",
        run_coro_on_loop(loop, _nested_suspend()) == 42,
    )
    check(
        "nested: sync inside blocking drive",
        run_coro_on_loop(loop, _in_drive_nested_sync()) == 44,
    )
    try:
        run_coro_on_loop(loop, _in_drive_nested_suspend())
        check("nested suspend inside running loop fails fast", False, "no error raised")
    except RuntimeError as exc:
        check(
            "nested suspend inside running loop fails fast",
            "suspended inside a running event loop" in str(exc),
            str(exc)[:80],
        )

    # State restoration: after every shape above, the thread must be clean —
    # a plain call still works and no running-loop residue leaks out.
    check(
        "runner reusable after all shapes", run_coro_on_loop(loop, _sync_value()) == 41
    )
    check(
        "no running-loop residue on the thread",
        asyncio.events._get_running_loop() is None,  # noqa: SLF001 — asserting the exact private state the runner must restore
    )
    check(
        "loop not left marked running",
        not loop.is_running(),
    )

    # Context isolation: each call runs in its own context copy (Task
    # semantics) — the handler's ContextVar writes must not leak out.
    check(
        "context isolated per call",
        run_coro_on_loop(loop, _context_isolated()) == "inner",
    )
    check("contextvar did not leak to caller", _VAR.get("unset") == "unset")

    return finish()


if __name__ == "__main__":
    run_main(main)
