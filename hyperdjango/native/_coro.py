"""Native-path coroutine runners: complete handler coroutines with full
asyncio Task semantics at fast-path cost.

Two callers resolve handler coroutines outside a running event loop:

- ``app.py``'s native dispatch (``_run_dispatch``) — every request on the Zig
  server's Python hot path.
- The Zig server's direct-coroutine fallback (``run_handler_coro`` below) —
  handlers registered without the app wrapper. This used to call
  ``asyncio.run(coro)`` per request: a brand-new event loop per call (epoll
  fd + socketpair creation and teardown, ~111 us measured, 6+ fd syscalls),
  whose per-request fd alloc/free serializes all workers on the kernel's
  process-wide fd-table lock — an accidental scaling limiter.

Both now share ``run_coro_on_loop``: an **eagerly-started asyncio.Task** on a
persistent per-worker-thread loop.

Why an eager Task and not a bare ``coro.send(None)`` fast path: the previous
hand-rolled step machinery never registered a ``current_task()``, so any
handler touching ``asyncio.wait_for`` / ``asyncio.timeout`` / ``TaskGroup``
on the native path died with ``RuntimeError("Timeout should be used inside a
task")`` -> 500. ``Task(..., eager_start=True)`` is the stdlib's designed
mechanism for exactly this shape: the first step runs synchronously with
correct current-task bookkeeping and context isolation (the context copy
happens in C), completing never-suspending handlers with zero event-loop
iterations — measured at ~0.5 us, at parity with the bare-send path it
replaces. A handler that genuinely suspends (DataLoader batching,
``asyncio.sleep``, real futures) continues on the worker's own long-lived
loop via ``run_until_complete(task)``.

Eager start only fires while the loop reports ``is_running()``, which is
derived from its ``_thread_id`` slot — set only inside ``run_forever``. The
runner installs this thread's id (and the running-loop slot) around Task
construction and clears both in ``finally``, so ``get_running_loop()``,
``current_task()``, and future construction inside the handler all bind to
the real loop while nothing else observes a "running" loop that isn't.

Thread model: each native worker thread gets its own loop; loops never run
concurrently with each other's callbacks and are never shared across
threads. Free-threading safe by construction (all state is
``threading.local``).
"""

from __future__ import annotations

import contextlib
import threading
from asyncio import AbstractEventLoop, Task, events, new_event_loop, tasks
from collections.abc import Coroutine

# ── Per-worker-thread loop registry ─────────────────────────────────────────
# ONE registry for every native-path caller (app.py's wrapper dispatch, the
# Zig direct-coroutine fallback, streaming drivers), so a worker thread has
# exactly ONE loop no matter which path created it first. This matters beyond
# tidiness: database.mark_loop_multiplexing keys DB-offload policy by loop
# identity, and shutdown closes loops through this list — a second untracked
# loop on the same thread would dodge both.

_all_thread_loops: list[AbstractEventLoop] = []
_all_thread_loops_lock = threading.Lock()


class _WorkerLoopState(threading.local):
    """Per-thread slot for this worker's persistent event loop."""

    def __init__(self) -> None:
        self.loop: AbstractEventLoop | None = None


_state = _WorkerLoopState()


def get_thread_event_loop() -> AbstractEventLoop:
    """Get or create the event loop for the current native worker thread.

    Every loop leaves here carrying ``_hyper_poke_depth`` — the count of
    ACTIVE run_coro_on_loop eager-step pokes on it. The slot lives ON THE
    LOOP (a thread-owned object) and NOT in a ``threading.local`` because on
    free-threaded CPython a ``threading.local`` attribute WRITE serializes
    process-wide: measured 0.18M ops/s across 64 threads (~5.5 us per write
    under contention) vs 158M ops/s for a plain attribute on a thread-owned
    object — an 880x cliff that capped the whole reactor at ~66k rps when
    the depth counter lived in a shared ``threading.local`` and was written
    twice per request. threading.local READS stay cheap (the ``_state.loop``
    lookup below is one), so the registry itself is unaffected.
    """
    loop = _state.loop
    if loop is None or loop.is_closed():
        loop = new_event_loop()
        loop._hyper_poke_depth = 0  # reentrancy slot — see docstring
        # Thread-per-request: this loop drives one request at a time on its
        # worker thread, so DB round-trips run inline on it (the default) —
        # optimal, and it keeps the query on the thread that owns the pinned
        # pool connection. Only multiplexing loops (shared WS pool / reactor)
        # are flagged for offload; see database.mark_loop_multiplexing.
        _state.loop = loop
        with _all_thread_loops_lock:
            _all_thread_loops.append(loop)
    return loop


def close_all_thread_loops() -> None:
    """Close all thread-local event loops on shutdown. Prevents fd=-1 errors.

    Only closes loops that are not currently running (safe for cross-thread).
    """
    with _all_thread_loops_lock:
        for loop in _all_thread_loops:
            if not loop.is_closed() and not loop.is_running():
                with contextlib.suppress(Exception):
                    loop.close()
        _all_thread_loops.clear()


def run_coro_on_loop(
    loop: AbstractEventLoop, coro: Coroutine[object, object, object]
) -> object:
    """Run ``coro`` to completion on ``loop`` (not currently running) and
    return its value. Fast path: an eagerly-started Task completes a
    never-suspending coroutine synchronously (~0.5 us). Slow path: the loop
    finishes the suspended task via ``run_until_complete``.

    REENTRANT: a handler resolved here may synchronously invoke another
    native-path wrapper on the same thread (double-wrapped handlers,
    in-process test drivers), nesting a second ``run_coro_on_loop`` inside
    the outer eager step. The pokes below therefore SAVE and RESTORE the
    previous running-loop/thread-id state rather than clearing it — an inner
    call that reset the state to None mid-step made the outer eager task's
    teardown see a foreign state and raise ``RuntimeError("... is not the
    running loop")``.

    One nesting shape cannot complete synchronously by construction: a call
    made from a callback while this loop is GENUINELY spinning (a handler
    that suspended, resumed inside the blocking drive below, then invoked a
    sync native wrapper) whose inner coroutine itself suspends — finishing
    it would require re-entering an already-running loop. That case fails
    fast with a clear error (the pre-eager implementation failed there too,
    as "This event loop is already running"); never-suspending inner
    coroutines still complete eagerly even in that shape.
    """
    # ALL per-call state lives on `loop` (thread-owned: plain attribute
    # access, no cross-thread traffic). A previous revision kept the poke
    # depth in a shared threading.local — on free-threaded CPython its
    # per-request WRITES serialized process-wide and collapsed the reactor
    # 9x (66k vs 579k rps at W=64); see get_thread_event_loop's docstring
    # for the measured numbers. `loop` must come from get_thread_event_loop
    # (every in-repo caller does), which installs the depth slot.
    try:
        depth = loop._hyper_poke_depth
    except AttributeError:
        # Foreign loop (test-constructed / user-supplied rather than from
        # get_thread_event_loop): install the reentrancy slot on first sight.
        # Steady state stays a plain attribute read — no per-call getattr.
        loop._hyper_poke_depth = depth = 0
    prev_tid = loop._thread_id  # noqa: SLF001
    if prev_tid is not None and depth == 0:
        # The loop is genuinely spinning (we are inside its own callback,
        # not inside one of our poked eager steps — poke depth distinguishes
        # the two). Eager-complete without touching any loop state.
        nested: Task[object] = Task(coro, loop=loop, eager_start=True)
        if nested.done():
            return nested.result()
        nested.cancel()  # don't leave it running in the background
        raise RuntimeError(
            "coroutine suspended inside a running event loop: a sync "
            "native wrapper invoked from a resumed async handler cannot "
            "block on the same loop — await the inner handler instead"
        )
    # Nested-in-eager-step (depth > 0) means the previous running-loop slot
    # holds this loop; at top level it is empty. Deriving it avoids a C call.
    prev_running = loop if depth > 0 else None
    events._set_running_loop(loop)  # noqa: SLF001 — the only way get_running_loop() can resolve during the eager first step, which runs before the loop itself spins
    # BaseEventLoop.is_running() is `self._thread_id is not None`; Task's
    # eager_start only executes when the loop reports running. There is no
    # public API to run one eager step outside run_forever, so install this
    # thread's id for the duration of the step and restore the previous
    # value (None at top level, this thread's id when nested) in finally.
    loop._thread_id = threading.get_ident()  # noqa: SLF001
    loop._hyper_poke_depth = depth + 1
    try:
        task: Task[object] = Task(coro, loop=loop, eager_start=True)
    finally:
        loop._hyper_poke_depth = depth
        loop._thread_id = prev_tid  # noqa: SLF001
        events._set_running_loop(prev_running)  # noqa: SLF001
    if task.done():
        # Never suspended: the eager first step ran the whole handler.
        # result() re-raises a handler exception, matching Task semantics.
        return task.result()
    # Genuinely suspended — block-drive the worker's persistent loop. Clear
    # any reentrant pokes for the duration: the loop is not ACTUALLY spinning
    # (an outer eager step is just a synchronous frame above us), and
    # run_until_complete's _check_running() must see the truth or a nested
    # suspend would die with "This event loop is already running". The
    # current-task registration must clear too — when this call is nested in
    # an outer eager step, that outer task is still registered as current and
    # the loop would refuse to resume the inner one ("Cannot enter into task
    # ... while another task is being executed"). _swap_current_task demands
    # that its loop BE the running loop at call time, so the swap happens
    # inside a running-loop window on both sides of the drive.
    events._set_running_loop(loop)  # noqa: SLF001
    prev_task = tasks._swap_current_task(loop, None)  # noqa: SLF001 — the same stdlib-private hook eager tasks themselves use for exactly this bookkeeping
    events._set_running_loop(None)  # noqa: SLF001
    loop._thread_id = None  # noqa: SLF001
    try:
        return loop.run_until_complete(task)
    finally:
        events._set_running_loop(loop)  # noqa: SLF001
        tasks._swap_current_task(loop, prev_task)  # noqa: SLF001
        loop._thread_id = prev_tid  # noqa: SLF001
        events._set_running_loop(prev_running)  # noqa: SLF001


def run_handler_coro(coro: Coroutine[object, object, object]) -> object:
    """Zig-server fallback entry: resolve a bare handler coroutine on this
    worker thread's persistent loop. See module docstring."""
    return run_coro_on_loop(get_thread_event_loop(), coro)
