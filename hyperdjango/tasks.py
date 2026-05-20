"""
In-process background task queue for deferred work.

In-process task queue using Python's free-threading (3.14t).
No external dependencies

WARNING: This task system is NOT persistent. All pending tasks, results,
schedules, and dead letters exist only in memory. Process restart = everything
lost. Do NOT use for tasks that must reliably execute (billing, critical
notifications, scheduled reports). For persistent recurring job workflows,
use pyjobby (PostgreSQL-backed durable task queue).

Features:
    - Priority queue (LOW, NORMAL, HIGH, CRITICAL)
    - Task result storage with TaskHandle for status/result/cancel
    - Retry with exponential backoff + jitter
    - Cron-like task scheduling (5-field cron expressions)
    - Dead letter queue for permanently failed tasks
    - Task lifecycle hooks (on_success, on_failure, on_retry)
    - Task groups (parallel execution, wait for all)
    - Enhanced stats (avg execution time, tasks/sec, etc.)

Usage:
    from hyperdjango import HyperApp

    app = HyperApp()

    @app.task
    async def send_email(to, subject, body):
        await email_service.send(to, subject, body)

    @app.task(max_retries=3, retry_delay=1.0, retry_backoff=2.0,
              retry_on=(ConnectionError, TimeoutError))
    async def fetch_data(url):
        ...

    # Call directly (synchronous):
    send_email("user@example.com", "Hello", "Body")

    # Enqueue for background execution:
    handle = send_email.delay("user@example.com", "Hello", "Body")
    handle.status()   # TaskStatus.PENDING / RUNNING / SUCCESS / FAILED
    handle.result()   # blocks until done, returns result
    handle.cancel()   # cancel if still pending
"""

import asyncio
import concurrent.futures
import contextlib
import contextvars
import datetime
import enum
import inspect
import queue
import random
import threading
import time
import traceback as tb_module
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from scheduler import Scheduler as _SchedulerEngine
from scheduler.trigger import (
    Friday,
    Monday,
    Saturday,
    Sunday,
    Thursday,
    Tuesday,
    Wednesday,
)

from hyperdjango.conf import get_setting
from hyperdjango.logging import logger
from hyperdjango.telemetry import metrics as _tel_metrics

# ── Native telemetry metrics (P5.2) ────────────────────────────────────────
# Registered at module load time. Zero cost when telemetry disabled.

_tasks_enqueued = _tel_metrics.Counter(
    "hyperdjango_tasks_enqueued_total",
    "Total tasks enqueued across all functions",
)
_tasks_processed = _tel_metrics.Counter(
    "hyperdjango_tasks_processed_total",
    "Total tasks processed successfully",
)
_tasks_failed = _tel_metrics.Counter(
    "hyperdjango_tasks_failed_total",
    "Total task failures (final, after retries exhausted)",
)
_tasks_retried = _tel_metrics.Counter(
    "hyperdjango_tasks_retried_total",
    "Total task retries across all functions",
)

__all__ = [
    "TaskPriority",
    "TaskStatus",
    "CircuitState",
    "TaskMessage",
    "TaskResult",
    "TaskHandle",
    "TaskQueue",
    "TaskQueueStats",
    "TaskDecorator",
    "TaskGroup",
    "DeadLetter",
    "DeadLetterQueue",
    "CircuitBreakerState",
    "ScheduleEntry",
    "TaskScheduler",
    "TaskUserLimitError",
    "TaskCircuitOpenError",
    "task",
    # Re-exported from scheduler library for convenience
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskPriority(enum.IntEnum):
    """Task priority levels. Higher value = higher priority."""

    LOW = 0
    NORMAL = 10
    HIGH = 20
    CRITICAL = 30


class TaskStatus(enum.StrEnum):
    """Task execution status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class CircuitState(enum.StrEnum):
    """Circuit breaker state machine."""

    CLOSED = "closed"  # Normal — tasks execute
    OPEN = "open"  # Tripped — tasks rejected
    HALF_OPEN = "half_open"  # Probing — one task allowed through


class TaskUserLimitError(RuntimeError):
    """Raised when a user exceeds their per-user pending task limit."""


class TaskCircuitOpenError(RuntimeError):
    """Raised when the circuit breaker for a task type is open."""


# Statuses considered terminal for cleanup eviction
_DONE_STATUSES: frozenset[str] = frozenset(
    {
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)

# Sentinel priority: must be higher than CRITICAL so sentinels dequeue
# before any real task (PriorityQueue pops smallest; __lt__ treats higher
# numeric value as "less than", so sentinels with 31 dequeue before CRITICAL 30).
_SENTINEL_PRIORITY: int = TaskPriority.CRITICAL + 1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TaskMessage:
    """A queued task invocation."""

    func: Callable
    args: tuple
    kwargs: dict[str, object]
    task_id: str
    priority: int = TaskPriority.NORMAL  # int to allow sentinel values > CRITICAL
    enqueued_at: float = field(default_factory=time.monotonic)
    max_retries: int = 0
    retry_delay: float = 1.0
    retry_backoff: float = 2.0
    retry_on: tuple[type[BaseException], ...] | None = None
    attempt: int = 0
    on_success: Callable | None = None
    on_failure: Callable | None = None
    on_retry: Callable | None = None
    timeout: float = 0  # seconds, 0 = no timeout
    user_id: str = ""  # optional user tracking for per-user limits

    def __lt__(self, other: TaskMessage) -> bool:
        """PriorityQueue needs comparison. Higher priority = dequeue first."""
        return self.priority > other.priority


@dataclass(slots=True)
class TaskResult:
    """Stored result of a task execution."""

    task_id: str
    status: TaskStatus
    result: object | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0


@dataclass(slots=True)
class TaskQueueStats:
    """Comprehensive task queue statistics."""

    pending: int
    running: int
    processed: int
    failed: int
    retried: int
    workers: int
    queue_running: bool
    dead_letters: int
    scheduled: int
    avg_execution_time_ms: float
    tasks_per_second: float


@dataclass(slots=True)
class DeadLetter:
    """A task that permanently failed after all retries exhausted."""

    task_id: str
    func_name: str
    args: tuple
    kwargs: dict[str, object]
    error: str
    traceback: str
    failed_at: float
    attempts: int


@dataclass(slots=True)
class CircuitBreakerState:
    """Per-function circuit breaker tracking."""

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_at: float = 0.0
    opened_at: float = 0.0
    half_open_attempts: int = 0


@dataclass(slots=True)
class ScheduleEntry:
    """A scheduled recurring task."""

    task: TaskDecorator
    args: tuple
    kwargs: dict[str, object]
    interval_seconds: float | None = None
    cron: str | None = None
    enabled: bool = True
    schedule_id: str = field(default_factory=lambda: uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# Cron-to-scheduler translation
# ---------------------------------------------------------------------------

# Map cron day-of-week (0=Sunday) to scheduler Weekday classes
_CRON_DOW_MAP: tuple[type, ...] = (
    Sunday,
    Monday,
    Tuesday,
    Wednesday,
    Thursday,
    Friday,
    Saturday,
)

# Three-letter English day names → 0=Sunday index (crontab convention).
_CRON_DOW_NAMES: dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def _parse_cron_dow(dow: str) -> int | None:
    """Parse a single cron day-of-week token to a 0=Sunday index, or None.

    Accepts 0-7 (BOTH 0 and 7 mean Sunday, per the crontab convention) and the
    three-letter English names (sun..sat, case-insensitive). Returns None for
    anything that isn't a single-day token (ranges/lists like "1-5" fall
    through to the caller's other branches).
    """
    if dow.isdigit():
        n = int(dow)
        if 0 <= n <= 7:
            return n % 7  # 7 → 0 (Sunday), preventing an IndexError
        return None
    return _CRON_DOW_NAMES.get(dow.lower())


def _cron_to_scheduler_timing(expr: str) -> tuple[str, object]:
    """Translate a 5-field cron expression to scheduler library timing.

    Returns (method_name, timing_arg) where method_name is one of:
    'cyclic', 'minutely', 'hourly', 'daily', 'weekly'.

    Handles common cron patterns:
    - "*/N * * * *" → cyclic(timedelta(minutes=N))
    - "S * * * *"   → minutely(time(second=0)) at minute S (approximated as cyclic)
    - "M H * * *"   → daily(time(hour=H, minute=M))
    - "M H * * D"   → weekly(Weekday(time(hour=H, minute=M)))
    - "* * * * *"   → cyclic(timedelta(minutes=1))

    For complex cron patterns that don't map cleanly to a single scheduler
    method, falls back to the nearest cyclic interval.
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"Cron expression must have 5 fields, got {len(fields)}: {expr!r}"
        )

    minute, hour, dom, month, dow = fields

    # Every N minutes: */N * * * *
    if "/" in minute and hour == "*" and dom == "*" and month == "*" and dow == "*":
        step = int(minute.split("/", 1)[1])
        if step <= 0:
            raise ValueError(f"Step value must be positive, got {step}")
        return ("cyclic", datetime.timedelta(minutes=step))

    # Every minute: * * * * *
    if minute == "*" and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return ("cyclic", datetime.timedelta(minutes=1))

    # Specific minute every hour: M * * * *
    if minute.isdigit() and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return ("hourly", datetime.time(minute=int(minute)))

    # Specific time daily: M H * * *
    if (
        minute.isdigit()
        and hour.isdigit()
        and dom == "*"
        and month == "*"
        and dow == "*"
    ):
        return ("daily", datetime.time(hour=int(hour), minute=int(minute)))

    # Specific time on specific weekday: M H * * D
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*":
        dow_index = _parse_cron_dow(dow)
        if dow_index is not None:
            t = datetime.time(hour=int(hour), minute=int(minute))
            weekday_cls = _CRON_DOW_MAP[dow_index]
            return ("weekly", weekday_cls(t))

    # Fallback: parse the minute field for a rough interval
    # This handles things like "0 */2 * * *" (every 2 hours)
    if "/" in hour and dom == "*" and month == "*" and dow == "*":
        step = int(hour.split("/", 1)[1])
        if step <= 0:
            raise ValueError(f"Step value must be positive, got {step}")
        return ("cyclic", datetime.timedelta(hours=step))

    # Last resort: daily at midnight as safe default
    logger.warning(
        "Complex cron expression {expr} approximated as daily at midnight",
        expr=repr(expr),
    )
    return ("daily", datetime.time(hour=0, minute=0))


# ---------------------------------------------------------------------------
# TaskHandle -- returned by .delay()
# ---------------------------------------------------------------------------


_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class TaskHandle:
    """Handle to a background task. Use to check status, get result, or cancel.

    Performance note: the `threading.Event` used by `result()`/`wait()` is
    created lazily (on first waiter), not eagerly in __init__. Under the
    `fire_and_forget` workload — the common case for background tasks
    that never call `.result()` — this saves ~1.5 μs per enqueue by
    skipping the Event + internal Condition + Lock object allocations.
    Measured at ~10 % throughput improvement on enqueue-dominated hot
    paths in task #188's cProfile audit.
    """

    def __init__(
        self,
        task_id: str,
        results: dict[str, TaskResult],
        lock: threading.Lock,
        queue: object
        | None = None,  # TaskQueue — forward ref, defined after this class
        user_id: str = "",
    ):
        self.task_id: str = task_id
        self._results: dict[str, TaskResult] = results
        self._lock: threading.Lock = lock
        # _done_event is created lazily by _ensure_event() the first time
        # a caller actually blocks on result()/wait(). Worker-side "mark
        # done" happens via mark_done() which signals only if an event
        # has already been created — otherwise the worker has nothing to
        # do beyond updating the results dict under _lock, and the next
        # status() / result() call will pick up the terminal status
        # directly from the dict without needing an event at all.
        self._done_event: threading.Event | None = None
        self._queue: TaskQueue | None = queue
        self._user_id: str = user_id
        # Terminal-result snapshot, cached by mark_done(). The shared results
        # dict is capacity-bounded and evicts old terminal entries (see
        # TaskQueue._maybe_cleanup), but a caller may hold this handle long
        # after that eviction. Caching the caller's own result here keeps it
        # readable for the handle's lifetime — eviction bounds the QUEUE's
        # memory, not the caller's view — and is freed when the handle is GC'd.
        self._final: TaskResult | None = None

    def _current_result(self) -> TaskResult | None:
        """This task's TaskResult: the live shared-dict entry, or the cached
        terminal snapshot if that entry was already evicted. MUST hold _lock."""
        tr = self._results.get(self.task_id)
        if tr is not None:
            return tr
        return self._final

    def _ensure_event(self) -> threading.Event:
        """Create the completion Event on demand.

        MUST be called while holding self._lock so the worker's
        `mark_done()` (which also runs under _lock) can never race with
        event creation.
        """
        if self._done_event is None:
            ev = threading.Event()
            # If the task already finished before any waiter showed up,
            # the event must be pre-signaled.
            tr = self._current_result()
            if tr is not None and tr.status in _TERMINAL_STATUSES:
                ev.set()
            self._done_event = ev
        return self._done_event

    def mark_done(self) -> None:
        """Signal task completion. MUST be called under self._lock AFTER
        writing the terminal TaskResult into self._results.

        If no waiter has called result()/wait() yet, this is a no-op —
        the next result() call will see the terminal status directly in
        the results dict.
        """
        # Snapshot the terminal result so this handle stays readable even after
        # the shared results dict evicts the entry (see _current_result). Runs
        # under _lock, immediately after the terminal TaskResult was written.
        self._final = self._results.get(self.task_id)
        if self._done_event is not None:
            self._done_event.set()

    def status(self) -> TaskStatus:
        """Return current task status."""
        with self._lock:
            tr = self._current_result()
        if tr is None:
            return TaskStatus.PENDING
        return tr.status

    def result(self, timeout: float | None = None) -> object:
        """Block until the task is done and return its result.

        Raises RuntimeError if the task failed.
        Raises TimeoutError if timeout expires before completion.
        """
        # Fast path: if the task already finished, skip event creation
        # entirely and read the result directly.
        with self._lock:
            tr = self._current_result()
            if tr is not None and tr.status in _TERMINAL_STATUSES:
                if tr.status == TaskStatus.FAILED:
                    raise RuntimeError(f"Task {self.task_id} failed: {tr.error}")
                if tr.status == TaskStatus.CANCELLED:
                    raise RuntimeError(f"Task {self.task_id} was cancelled")
                return tr.result
            evt = self._ensure_event()

        if not evt.wait(timeout=timeout):
            raise TimeoutError(
                f"Task {self.task_id} did not complete within {timeout}s"
            )
        with self._lock:
            # _current_result (not a bare subscript): the shared entry can be
            # evicted between the event firing and this read, but mark_done
            # cached the terminal result on this handle, so it stays readable.
            tr = self._current_result()
        if tr.status == TaskStatus.FAILED:
            raise RuntimeError(f"Task {self.task_id} failed: {tr.error}")
        if tr.status == TaskStatus.CANCELLED:
            raise RuntimeError(f"Task {self.task_id} was cancelled")
        return tr.result

    def cancel(self) -> bool:
        """Cancel the task if it is still pending or retrying. Returns True if cancelled."""
        with self._lock:
            tr = self._current_result()
            if tr is None or tr.status in (TaskStatus.PENDING, TaskStatus.RETRYING):
                # A RETRYING task has a pending retry Timer that will re-enqueue
                # it. The CANCELLED marker we write below is the worker's only
                # stop signal — but it is terminal and can be LRU-evicted before
                # the retry fires. Record the id in an eviction-immune set so the
                # worker still skips it. (PENDING tasks are already in the queue
                # and their non-terminal marker is never evicted, so they need no
                # extra bookkeeping.)
                if (
                    tr is not None
                    and tr.status == TaskStatus.RETRYING
                    and self._queue is not None
                ):
                    self._queue._cancelled_retries.add(self.task_id)
                self._results[self.task_id] = TaskResult(
                    task_id=self.task_id,
                    status=TaskStatus.CANCELLED,
                    finished_at=time.monotonic(),
                )
                self.mark_done()
                # Decrement per-user pending count on cancel
                if self._queue and self._user_id:
                    self._queue._decrement_user_pending(self._user_id)
                return True
            return False

    def is_done(self) -> bool:
        """Return True if the task has finished (success, failed, or cancelled)."""
        with self._lock:
            tr = self._current_result()
            return tr is not None and tr.status in _TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Dead Letter Queue
# ---------------------------------------------------------------------------


class DeadLetterQueue:
    """Thread-safe dead letter storage with configurable max size."""

    def __init__(self, max_size: int = 10000):
        self._letters: list[DeadLetter] = []
        self._lock: threading.Lock = threading.Lock()
        self._max_size: int = max_size
        self._by_id: dict[str, DeadLetter] = {}
        # Stores original func reference for retry
        self._func_registry: dict[str, Callable] = {}

    def push(self, letter: DeadLetter, func: Callable | None = None) -> None:
        """Add a dead letter. Drops oldest if max size exceeded."""
        with self._lock:
            if len(self._letters) >= self._max_size:
                evicted = self._letters.pop(0)
                self._by_id.pop(evicted.task_id, None)
                self._func_registry.pop(evicted.task_id, None)
            self._letters.append(letter)
            self._by_id[letter.task_id] = letter
            if func is not None:
                self._func_registry[letter.task_id] = func

    def pop(self) -> DeadLetter | None:
        """Remove and return the oldest dead letter."""
        with self._lock:
            if not self._letters:
                return None
            letter = self._letters.pop(0)
            self._by_id.pop(letter.task_id, None)
            self._func_registry.pop(letter.task_id, None)
            return letter

    def peek(self, n: int = 10) -> list[DeadLetter]:
        """Return up to n most recent dead letters without removing them."""
        with self._lock:
            return list(self._letters[-n:])

    def retry(self, task_id: str) -> TaskHandle | None:
        """Re-enqueue a dead letter task. Returns TaskHandle or None if not found."""
        with self._lock:
            letter = self._by_id.get(task_id)
            if letter is None:
                return None
            func = self._func_registry.get(task_id)
            if func is None:
                return None
            # Remove from DLQ
            self._letters = [dl for dl in self._letters if dl.task_id != task_id]
            self._by_id.pop(task_id, None)
            self._func_registry.pop(task_id, None)

        return _task_queue.enqueue(func, *letter.args, **letter.kwargs)

    def clear(self) -> None:
        """Remove all dead letters."""
        with self._lock:
            self._letters.clear()
            self._by_id.clear()
            self._func_registry.clear()

    @property
    def size(self) -> int:
        """Number of dead letters currently stored."""
        with self._lock:
            return len(self._letters)


# ---------------------------------------------------------------------------
# Task Queue
# ---------------------------------------------------------------------------


class TaskQueue:
    """In-process priority-based background task queue.

    Uses a thread pool to execute tasks concurrently.
    Thread-safe for Python 3.14t free-threading.
    """

    def __init__(
        self,
        workers: int | None = None,
        max_queue_size: int | None = None,
    ):
        _workers = workers if workers is not None else int(get_setting("TASK_WORKERS"))
        _max_q = (
            max_queue_size
            if max_queue_size is not None
            else int(get_setting("TASK_MAX_QUEUE_SIZE"))
        )
        self._queue: queue.PriorityQueue[TaskMessage | None] = queue.PriorityQueue(
            maxsize=_max_q
        )
        self._workers: list[threading.Thread] = []
        self._running: bool = False
        self._num_workers: int = _workers
        self._lock: threading.Lock = threading.Lock()

        # Shared, bounded executor for TIMED sync tasks. Running each timed task
        # in its own single-thread executor + shutdown(wait=False) orphaned one
        # thread per over-running task with NO bound (Python can't kill a
        # thread). A single pool caps how many orphaned/over-running timed tasks
        # can accumulate — beyond that, new timed tasks queue for a slot instead
        # of spawning unbounded threads. Lazily created on first timed task
        # (apps with no timed tasks never allocate it); shut down in stop().
        self._timed_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._timed_executor_lock: threading.Lock = threading.Lock()

        # Results store
        self._results: dict[str, TaskResult] = {}
        self._results_lock: threading.Lock = threading.Lock()
        self._handles: dict[str, TaskHandle] = {}
        # Task ids cancelled WHILE RETRYING. The CANCELLED result is terminal and
        # therefore evictable by _maybe_cleanup; if it is evicted during the
        # retry delay, the worker's cancel-check (which reads _results directly)
        # would miss it and run the cancelled task anyway — also double-releasing
        # the per-user slot. This set is NOT subject to LRU eviction, so the
        # pending retry is reliably skipped. Entries are removed when the retry
        # is observed (or its terminal result is written). Protected by
        # _results_lock. Bounded by the count of in-flight cancelled retries.
        self._cancelled_retries: set[str] = set()

        # Stats counters -- protected by _lock
        self._tasks_processed: int = 0
        self._tasks_failed: int = 0
        self._tasks_retried: int = 0
        self._tasks_running: int = 0
        self._total_execution_time_ms: float = 0.0
        self._stats_start_time: float = time.monotonic()

        # Cleanup tracking -- evict completed results after this many accumulate
        # Protected by _results_lock (same as _results and _handles)
        self._max_completed_results: int = int(
            get_setting("TASK_MAX_COMPLETED_RESULTS")
        )
        self._cleanup_counter: int = 0
        self._cleanup_interval: int = int(get_setting("TASK_CLEANUP_INTERVAL"))

        # Dead letter queue
        self.dead_letters: DeadLetterQueue = DeadLetterQueue(
            max_size=int(get_setting("TASK_DLQ_MAX_SIZE"))
        )

        # Per-user pending task limits
        self._user_pending: dict[str, int] = {}  # user_id → pending count
        self._user_pending_lock: threading.Lock = threading.Lock()
        self._max_pending_per_user: int = int(
            get_setting("TASK_MAX_PENDING_PER_USER", 0)
        )  # 0 = unlimited

        # Circuit breaker per function name
        self._circuit_breakers: dict[str, CircuitBreakerState] = {}
        self._circuit_lock: threading.Lock = threading.Lock()
        self._circuit_failure_threshold: int = int(
            get_setting("TASK_CIRCUIT_FAILURE_THRESHOLD", 5)
        )
        self._circuit_recovery_timeout: float = float(
            get_setting("TASK_CIRCUIT_RECOVERY_TIMEOUT", 30.0)
        )
        self._circuit_window: float = float(
            get_setting("TASK_CIRCUIT_WINDOW", 300.0)
        )  # 5 min rolling window

    def _get_timed_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Lazily create the shared, bounded executor for timed sync tasks."""
        ex = self._timed_executor
        if ex is not None:
            return ex
        with self._timed_executor_lock:
            if self._timed_executor is None:
                self._timed_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, self._num_workers),
                    thread_name_prefix="task-timed",
                )
            return self._timed_executor

    def start(self) -> None:
        """Start the worker threads.

        The running-check and worker spawn are done under self._lock so two
        concurrent first-callers (e.g. racing .delay() calls) can't both pass
        the check and each spawn a full set of worker threads. Spawning while
        holding the lock is safe: a fresh worker blocks on an empty queue and
        won't contend for self._lock until it dequeues a task, by which time
        start() has released it.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stats_start_time = time.monotonic()
            for i in range(self._num_workers):
                t = threading.Thread(
                    target=self._worker_loop, name=f"task-worker-{i}", daemon=True
                )
                t.start()
                self._workers.append(t)
        logger.info("Task queue started with {n} workers", n=self._num_workers)

    def stop(self, drain: bool = True) -> None:
        """Stop the worker threads.

        drain=True (default): wait for the already-queued tasks to be consumed
        before signalling workers to exit — a graceful drain bounded by
        TASK_SHUTDOWN_TIMEOUT. In-flight tasks always run to completion.

        drain=False: stop immediately. Workers finish their current in-flight
        task, then exit; any tasks still sitting in the queue are abandoned
        (this queue is in-memory and non-persistent by design).

        Note: retries scheduled via a delay Timer may re-enqueue after the
        drain window closes; the in-memory queue makes no delivery guarantee.
        """
        shutdown_timeout = int(get_setting("TASK_SHUTDOWN_TIMEOUT"))

        # Graceful drain: keep workers running (self._running stays True) and
        # wait, bounded, for the queue to empty and in-flight tasks to finish.
        # Done BEFORE the state flip so the `while self._running` worker guard
        # keeps pulling queued work instead of exiting on the next iteration.
        if drain and self._running:
            deadline = time.monotonic() + shutdown_timeout
            while time.monotonic() < deadline:
                with self._lock:
                    drained = self._queue.empty() and self._tasks_running == 0
                if drained:
                    break
                time.sleep(0.01)

        # Guard the check + state flip + sentinel enqueue under self._lock so it
        # pairs with start()'s guard (only one caller performs the shutdown).
        # The join() MUST happen outside the lock: worker loops still take
        # self._lock to finish their in-flight task, so holding it across join()
        # would deadlock.
        with self._lock:
            if not self._running:
                return
            self._running = False
            for _ in self._workers:
                # PriorityQueue needs comparable items; None won't work directly.
                # Use a sentinel TaskMessage with priority > CRITICAL so it dequeues
                # before any real task (our __lt__ treats higher numeric = dequeue first).
                sentinel = TaskMessage(
                    func=lambda: None,
                    args=(),
                    kwargs={},
                    task_id="__sentinel__",
                    priority=_SENTINEL_PRIORITY,
                    max_retries=0,
                )
                self._queue.put(sentinel)
            workers = self._workers
            self._workers = []
        for t in workers:
            t.join(timeout=shutdown_timeout)
        # Tear down the shared timed-task executor. wait=False: don't block
        # shutdown on an over-running task that ignored its timeout.
        if self._timed_executor is not None:
            self._timed_executor.shutdown(wait=False)
            self._timed_executor = None
        logger.info(
            "Task queue stopped. Processed: {processed}, Failed: {failed}",
            processed=self._tasks_processed,
            failed=self._tasks_failed,
        )

    def enqueue(
        self,
        func: Callable,
        *args: object,
        priority: TaskPriority = TaskPriority.NORMAL,
        max_retries: int = 0,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
        retry_on: tuple[type[BaseException], ...] | None = None,
        on_success: Callable | None = None,
        on_failure: Callable | None = None,
        on_retry: Callable | None = None,
        timeout: float = 0,
        user_id: str = "",
        **kwargs: object,
    ) -> TaskHandle:
        """Add a task to the priority queue. Returns a TaskHandle.

        Raises TaskUserLimitError if user_id has too many pending tasks.
        Raises TaskCircuitOpenError if the circuit breaker for this function is open.
        """
        func_name = func.__name__

        # Circuit breaker check
        if self._circuit_failure_threshold > 0:
            rejection = self._check_circuit(func_name)
            if rejection:
                task_id = uuid.uuid4().hex
                with self._results_lock:
                    self._results[task_id] = TaskResult(
                        task_id=task_id,
                        status=TaskStatus.FAILED,
                        error=rejection,
                        finished_at=time.monotonic(),
                    )
                handle = TaskHandle(task_id, self._results, self._results_lock)
                with self._results_lock:
                    self._handles[task_id] = handle
                    handle.mark_done()
                raise TaskCircuitOpenError(rejection)

        # Per-user limit check
        if user_id and self._max_pending_per_user > 0:
            with self._user_pending_lock:
                current = self._user_pending.get(user_id, 0)
                if current >= self._max_pending_per_user:
                    task_id = uuid.uuid4().hex
                    with self._results_lock:
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status=TaskStatus.FAILED,
                            error=f"User {user_id} has {current} pending tasks (limit: {self._max_pending_per_user})",
                            finished_at=time.monotonic(),
                        )
                    handle = TaskHandle(task_id, self._results, self._results_lock)
                    with self._results_lock:
                        self._handles[task_id] = handle
                        handle.mark_done()
                    raise TaskUserLimitError(
                        f"User {user_id} exceeded per-user task limit ({self._max_pending_per_user})"
                    )
                self._user_pending[user_id] = current + 1

        task_id = uuid.uuid4().hex
        msg = TaskMessage(
            func=func,
            args=args,
            kwargs=kwargs,
            task_id=task_id,
            priority=priority,
            max_retries=max_retries,
            retry_delay=retry_delay,
            retry_backoff=retry_backoff,
            retry_on=retry_on,
            on_success=on_success,
            on_failure=on_failure,
            on_retry=on_retry,
            timeout=timeout,
            user_id=user_id,
        )

        # TaskHandle construction is pure-Python attribute init — safe
        # to build outside the lock, then register both the PENDING
        # result and the handle in a single `_results_lock` acquire.
        # Saves one lock round-trip per enqueue call on the happy
        # path, with the same atomicity guarantees (caller cannot see
        # a handle without a matching PENDING result entry).
        handle = TaskHandle(
            task_id,
            self._results,
            self._results_lock,
            queue=self,
            user_id=user_id,
        )
        with self._results_lock:
            self._results[task_id] = TaskResult(
                task_id=task_id,
                status=TaskStatus.PENDING,
            )
            self._handles[task_id] = handle

        try:
            self._queue.put_nowait(msg)
            _tasks_enqueued.inc(1)
        except queue.Full:
            logger.warning("Task queue full, dropping task: {name}", name=func_name)
            with self._results_lock:
                self._results[task_id] = TaskResult(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error="Queue full",
                    finished_at=time.monotonic(),
                )
                handle.mark_done()
            # Decrement user pending on queue-full rejection
            if user_id:
                self._decrement_user_pending(user_id)

        return handle

    def _re_enqueue(self, msg: TaskMessage) -> None:
        """Re-enqueue a task for retry (internal use)."""
        try:
            self._queue.put_nowait(msg)
        except queue.Full:
            # The retry cannot be re-queued. This is a TERMINAL outcome: mirror
            # enqueue()'s queue-full path so result() doesn't block forever
            # (RETRYING is not terminal) and the per-user slot is released.
            # Guard against a task that was CANCELLED during the retry delay —
            # cancel() already wrote the terminal result and decremented, so we
            # must not overwrite it or double-decrement.
            finished = time.monotonic()
            with self._results_lock:
                tr = self._results.get(msg.task_id)
                # Treat an eviction-immune cancelled-retry id as terminal too:
                # cancel() already wrote CANCELLED + decremented, but that marker
                # may have been LRU-evicted (tr is None). Without this we would
                # overwrite it with FAILED and double-decrement the user slot.
                cancelled_evicted = msg.task_id in self._cancelled_retries
                already_terminal = (
                    tr is not None and tr.status in _TERMINAL_STATUSES
                ) or cancelled_evicted
                if cancelled_evicted:
                    self._cancelled_retries.discard(msg.task_id)
                if not already_terminal:
                    self._results[msg.task_id] = TaskResult(
                        task_id=msg.task_id,
                        status=TaskStatus.FAILED,
                        error="Queue full during retry",
                        finished_at=finished,
                        attempts=msg.attempt + 1,
                    )
                    handle = self._handles.get(msg.task_id)
                    if handle is not None:
                        handle.mark_done()
            if already_terminal:
                return
            logger.warning(
                "Task queue full during retry, sending to DLQ: {name}",
                name=msg.func.__name__,
            )
            self._send_to_dlq(msg, "Queue full during retry")
            self._decrement_user_pending(msg.user_id)

    def _send_to_dlq(self, msg: TaskMessage, error: str, tb_str: str = "") -> None:
        """Send a failed task to the dead letter queue."""
        letter = DeadLetter(
            task_id=msg.task_id,
            func_name=msg.func.__name__,
            args=msg.args,
            kwargs=msg.kwargs,
            error=error,
            traceback=tb_str,
            failed_at=time.monotonic(),
            attempts=msg.attempt + 1,
        )
        self.dead_letters.push(letter, func=msg.func)

    def _decrement_user_pending(self, user_id: str) -> None:
        """Decrement the pending task count for a user (on task completion or rejection)."""
        if not user_id:
            return
        with self._user_pending_lock:
            count = self._user_pending.get(user_id, 0)
            if count <= 1:
                self._user_pending.pop(user_id, None)
            else:
                self._user_pending[user_id] = count - 1

    def _check_circuit(self, func_name: str) -> str:
        """Check circuit breaker for a function. Returns rejection reason or empty string.

        Fast path: in the common case (no tracked breaker for this
        function) the dict lookup is thread-safe under free-threading
        and we can bail out without acquiring _circuit_lock. A tracked
        breaker is only added on the first recorded failure — tasks
        with a perfect success history never pay the lock cost.
        """
        if self._circuit_breakers.get(func_name) is None:
            return ""
        with self._circuit_lock:
            cb = self._circuit_breakers.get(func_name)
            if cb is None:
                return ""
            if cb.state == CircuitState.CLOSED:
                return ""
            if cb.state == CircuitState.OPEN:
                # Check if recovery timeout has elapsed → transition to half-open
                elapsed = time.monotonic() - cb.opened_at
                if elapsed >= self._circuit_recovery_timeout:
                    cb.state = CircuitState.HALF_OPEN
                    # This same call consumes the single allowed probe, so mark
                    # it taken (=1). Leaving it 0 let the NEXT caller also pass
                    # the `half_open_attempts > 0` gate below, admitting TWO
                    # probes to a still-unhealthy dependency instead of one.
                    cb.half_open_attempts = 1
                    logger.info(
                        "Circuit breaker half-open for {name} (after {s:.0f}s cooldown)",
                        name=func_name,
                        s=elapsed,
                    )
                    return ""  # Allow the probe through
                return f"Circuit breaker OPEN for {func_name}: {cb.failure_count} failures in window"
            # HALF_OPEN — allow one probe through
            if cb.half_open_attempts > 0:
                return f"Circuit breaker HALF_OPEN for {func_name}: probe in progress"
            cb.half_open_attempts += 1
            return ""

    def _record_circuit_success(self, func_name: str) -> None:
        """Record a successful execution for circuit breaker tracking.

        Fast path: skip the lock entirely when no breaker exists for
        this func_name. Concurrent readers in free-threaded Python see
        an atomic dict.get result, so racing with a failure that's
        just now adding a breaker is safe — the worst case is one
        success going unrecorded, which cannot mask a real outage.
        """
        if self._circuit_breakers.get(func_name) is None:
            return
        with self._circuit_lock:
            cb = self._circuit_breakers.get(func_name)
            if cb is None:
                return
            if cb.state == CircuitState.HALF_OPEN:
                # Probe succeeded — close the circuit
                cb.state = CircuitState.CLOSED
                cb.failure_count = 0
                cb.success_count = 0
                cb.half_open_attempts = 0
                logger.info(
                    "Circuit breaker CLOSED for {name} (probe succeeded)",
                    name=func_name,
                )
            elif cb.state == CircuitState.CLOSED:
                cb.success_count += 1

    def _record_circuit_failure(self, func_name: str) -> None:
        """Record a failure for circuit breaker tracking."""
        now = time.monotonic()
        with self._circuit_lock:
            cb = self._circuit_breakers.get(func_name)
            if cb is None:
                cb = CircuitBreakerState()
                self._circuit_breakers[func_name] = cb

            if cb.state == CircuitState.HALF_OPEN:
                # Probe failed — reopen the circuit
                cb.state = CircuitState.OPEN
                cb.opened_at = now
                cb.half_open_attempts = 0
                logger.warning(
                    "Circuit breaker re-OPENED for {name} (probe failed)",
                    name=func_name,
                )
                return

            # Reset failure count if outside rolling window
            if (
                cb.last_failure_at > 0
                and (now - cb.last_failure_at) > self._circuit_window
            ):
                cb.failure_count = 0

            cb.failure_count += 1
            cb.last_failure_at = now

            if cb.failure_count >= self._circuit_failure_threshold:
                cb.state = CircuitState.OPEN
                cb.opened_at = now
                logger.warning(
                    "Circuit breaker OPENED for {name}: {n} failures in {w:.0f}s window",
                    name=func_name,
                    n=cb.failure_count,
                    w=self._circuit_window,
                )

    def get_user_pending(self, user_id: str) -> int:
        """Return the number of pending tasks for a user."""
        with self._user_pending_lock:
            return self._user_pending.get(user_id, 0)

    def get_circuit_breaker(self, func_name: str) -> CircuitBreakerState | None:
        """Return the circuit breaker state for a function, or None if not tracked."""
        with self._circuit_lock:
            return self._circuit_breakers.get(func_name)

    def get_all_circuit_breakers(self) -> dict[str, CircuitBreakerState]:
        """Return a snapshot of all circuit breaker states."""
        with self._circuit_lock:
            return dict(self._circuit_breakers)

    def _worker_loop(self) -> None:
        """Worker thread loop.

        NOTE: Retry timers use daemon threads. If the process exits during a
        retry delay, that task is silently lost. This is acceptable for an
        in-process queue; persistent delivery requires an external broker.
        """
        loop = asyncio.new_event_loop()
        try:
            self._worker_loop_inner(loop)
        finally:
            loop.close()

    def _worker_loop_inner(self, loop: asyncio.AbstractEventLoop) -> None:
        """Inner worker loop, separated so the outer can guarantee loop.close()."""
        while self._running:
            try:
                msg = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            if msg is None or msg.task_id == "__sentinel__":
                break

            # Fused cancellation check + SET RUNNING transition. The
            # previous split acquired `_results_lock` twice per task
            # (once for the cancel peek, once for the state write) —
            # merging them saves one lock round-trip on the hot path
            # and preserves identical semantics because nothing
            # happens between the two original blocks.
            started = time.monotonic()
            cancelled = False
            with self._results_lock:
                tr = self._results.get(msg.task_id)
                # Cancelled if the marker is present OR its id is in the
                # eviction-immune cancelled-retries set (the CANCELLED result may
                # have been LRU-evicted during the retry delay). Discarding on
                # sight keeps the set bounded.
                if (tr is not None and tr.status == TaskStatus.CANCELLED) or (
                    msg.task_id in self._cancelled_retries
                ):
                    cancelled = True
                    self._cancelled_retries.discard(msg.task_id)
                else:
                    self._results[msg.task_id] = TaskResult(
                        task_id=msg.task_id,
                        status=TaskStatus.RUNNING,
                        started_at=started,
                        attempts=msg.attempt + 1,
                    )

            if cancelled:
                # Do NOT decrement the per-user pending count here.
                # TaskHandle.cancel() is the ONLY writer of the CANCELLED
                # status and it already released the slot for this same
                # enqueue when it wrote that result. Decrementing again would
                # double-count, letting the user under-report pending and
                # bypass TASK_MAX_PENDING_PER_USER. Exactly-one-decrement
                # ownership: enqueue-increment is released by exactly one of
                # {SUCCESS, FAILED, queue-full, cancel()}.
                continue

            with self._lock:
                self._tasks_running += 1

            try:
                # Run each task in its OWN copy of the (clean) worker context so a
                # ContextVar it sets — tenant, locale, request-id, DB routing —
                # cannot leak to the next task on this reused worker thread. Copied
                # here on the worker thread (where the context stays pristine,
                # because every task's mutations are confined to its own copy), so
                # each task starts clean. Without this, a task that does
                # set_tenant(A) without reset would make the NEXT task run as
                # tenant A — a cross-tenant data leak in a shared task pool.
                if inspect.iscoroutinefunction(msg.func):
                    coro = msg.func(*msg.args, **msg.kwargs)
                    if msg.timeout > 0:
                        coro = asyncio.wait_for(coro, timeout=msg.timeout)
                    # Drive the loop inside a fresh context copy — the awaited
                    # Task inherits it, so ContextVar writes stay scoped to this
                    # task and never touch the reused worker thread's context.
                    result_value = contextvars.copy_context().run(
                        loop.run_until_complete, coro
                    )
                else:
                    task_ctx = contextvars.copy_context()
                    if msg.timeout > 0:
                        # Run the sync function on the SHARED timed-task executor
                        # with a timeout. On timeout the future is abandoned
                        # (Python cannot cancel a running thread) but stays on the
                        # shared pool, so over-running tasks are bounded by the
                        # pool size instead of spawning one thread each. Run it
                        # inside task_ctx so the executor thread's context is not
                        # polluted across timed tasks either.
                        future = self._get_timed_executor().submit(
                            task_ctx.run, msg.func, *msg.args, **msg.kwargs
                        )
                        result_value = future.result(timeout=msg.timeout)
                    else:
                        result_value = task_ctx.run(msg.func, *msg.args, **msg.kwargs)

                finished = time.monotonic()
                elapsed_ms = (finished - started) * 1000.0

                # Fused SET SUCCESS + mark_done. The two original
                # `_results_lock` blocks had only lock-free work
                # between them (stats counter, _decrement_user_pending,
                # _record_circuit_success) — those can move BELOW the
                # merged block without inverting lock ordering. Saves
                # one lock round-trip per completed task on the hot
                # path. mark_done runs under `_results_lock` already
                # so the lazy event-creation discipline is preserved.
                with self._results_lock:
                    self._results[msg.task_id] = TaskResult(
                        task_id=msg.task_id,
                        status=TaskStatus.SUCCESS,
                        result=result_value,
                        started_at=started,
                        finished_at=finished,
                        attempts=msg.attempt + 1,
                    )
                    handle = self._handles.get(msg.task_id)
                    if handle is not None:
                        handle.mark_done()

                with self._lock:
                    self._tasks_processed += 1
                    self._tasks_running -= 1
                    self._total_execution_time_ms += elapsed_ms
                _tasks_processed.inc(1)

                # Decrement per-user pending count
                self._decrement_user_pending(msg.user_id)

                # Record circuit breaker success
                self._record_circuit_success(msg.func.__name__)

                self._maybe_cleanup()

                # on_success hook
                if msg.on_success is not None:
                    try:
                        msg.on_success(result_value)
                    # blind-except: user-supplied on_success callback is best-effort; its failure is logged but must not fail the already-succeeded task.
                    except Exception:
                        logger.error(
                            "on_success hook failed for {name}:\n{tb}",
                            name=msg.func.__name__,
                            tb=tb_module.format_exc(),
                        )

            # We deliberately widened Exception → BaseException so a task that
            # raises SystemExit / KeyboardInterrupt / CancelledError can't kill a
            # worker thread (attriting the pool + leaking _tasks_running). We do
            # NOT re-raise them: worker threads are daemons and never the
            # interpreter's signal target, so process shutdown flows through
            # stop()/sentinels, not a task-raised exception.
            # blind-except: worker-loop resilience — every task failure (incl. BaseException raised by the task) is captured into a TaskResult (RETRYING/FAILED), metrics, and the DLQ below, so one poison task can neither kill a worker nor be silently dropped.
            except BaseException as exc:
                finished = time.monotonic()
                elapsed_ms = (finished - started) * 1000.0
                # TimeoutError has an empty str() — provide a useful message
                if isinstance(
                    exc,
                    (
                        TimeoutError,
                        asyncio.TimeoutError,
                        concurrent.futures.TimeoutError,
                    ),
                ):
                    exc = TimeoutError(f"Task timed out after {msg.timeout}s")
                tb_str = tb_module.format_exc()

                with self._lock:
                    self._tasks_running -= 1
                    self._total_execution_time_ms += elapsed_ms

                should_retry = msg.attempt < msg.max_retries and (
                    msg.retry_on is None or isinstance(exc, msg.retry_on)
                )

                if should_retry:
                    # Exponential backoff with jitter
                    delay = msg.retry_delay * (msg.retry_backoff**msg.attempt)
                    jitter = random.uniform(0, 0.25 * delay)
                    total_delay = delay + jitter

                    with self._results_lock:
                        self._results[msg.task_id] = TaskResult(
                            task_id=msg.task_id,
                            status=TaskStatus.RETRYING,
                            error=str(exc),
                            started_at=started,
                            finished_at=finished,
                            attempts=msg.attempt + 1,
                        )
                    with self._lock:
                        self._tasks_retried += 1
                    _tasks_retried.inc(1)

                    # on_retry hook
                    if msg.on_retry is not None:
                        try:
                            msg.on_retry(exc, msg.attempt + 1)
                        # blind-except: user-supplied on_retry callback is best-effort; its failure is logged but must not disrupt the retry schedule.
                        except Exception:
                            logger.error(
                                "on_retry hook failed for {name}:\n{tb}",
                                name=msg.func.__name__,
                                tb=tb_module.format_exc(),
                            )

                    logger.warning(
                        "Task {name} attempt {attempt}/{max} failed, retrying in {delay:.1f}s: {err}",
                        name=msg.func.__name__,
                        attempt=msg.attempt + 1,
                        max=msg.max_retries,
                        delay=total_delay,
                        err=str(exc),
                    )

                    # Schedule retry after delay in a separate thread
                    retry_msg = TaskMessage(
                        func=msg.func,
                        args=msg.args,
                        kwargs=msg.kwargs,
                        task_id=msg.task_id,
                        priority=msg.priority,
                        max_retries=msg.max_retries,
                        retry_delay=msg.retry_delay,
                        retry_backoff=msg.retry_backoff,
                        retry_on=msg.retry_on,
                        attempt=msg.attempt + 1,
                        on_success=msg.on_success,
                        on_failure=msg.on_failure,
                        on_retry=msg.on_retry,
                        timeout=msg.timeout,
                        user_id=msg.user_id,
                    )
                    timer = threading.Timer(
                        total_delay, self._re_enqueue, args=(retry_msg,)
                    )
                    timer.daemon = True
                    timer.start()

                else:
                    # Permanent failure — same lock-merge discipline
                    # as the success path. SET FAILED + mark_done run
                    # in a single `_results_lock` block; stats, user
                    # pending decrement, circuit breaker tracking,
                    # and cleanup all run without the results lock.
                    with self._results_lock:
                        self._results[msg.task_id] = TaskResult(
                            task_id=msg.task_id,
                            status=TaskStatus.FAILED,
                            error=str(exc),
                            started_at=started,
                            finished_at=finished,
                            attempts=msg.attempt + 1,
                        )
                        handle = self._handles.get(msg.task_id)
                        if handle is not None:
                            handle.mark_done()

                    with self._lock:
                        self._tasks_failed += 1
                    _tasks_failed.inc(1)

                    # Decrement per-user pending count
                    self._decrement_user_pending(msg.user_id)

                    # Record circuit breaker failure
                    self._record_circuit_failure(msg.func.__name__)

                    self._maybe_cleanup()

                    # Send to DLQ
                    self._send_to_dlq(msg, str(exc), tb_str)

                    # on_failure hook
                    if msg.on_failure is not None:
                        try:
                            msg.on_failure(exc)
                        # blind-except: user-supplied on_failure callback is best-effort; its own error is logged but must not mask the original task failure.
                        except Exception:
                            logger.error(
                                "on_failure hook failed for {name}:\n{tb}",
                                name=msg.func.__name__,
                                tb=tb_module.format_exc(),
                            )

                    logger.error(
                        "Task {name} permanently failed after {attempts} attempt(s):\n{tb}",
                        name=msg.func.__name__,
                        attempts=msg.attempt + 1,
                        tb=tb_str,
                    )

    def _maybe_cleanup(self) -> None:
        """Evict completed/failed/cancelled results to prevent unbounded growth.

        Called periodically from worker threads after task completion.

        Lock ordering: always _results_lock before _lock to match _worker_loop
        and prevent deadlocks.
        """
        with self._results_lock:
            # Use _results_lock to protect the counter too, avoiding a second
            # lock acquisition that would invert ordering with _worker_loop.
            self._cleanup_counter += 1
            if self._cleanup_counter < self._cleanup_interval:
                return
            self._cleanup_counter = 0

            if len(self._results) <= self._max_completed_results:
                return
            # Find completed task IDs sorted by finished_at, evict oldest
            completed: list[tuple[float, str]] = []
            for task_id, tr in self._results.items():
                if tr.status in _DONE_STATUSES and tr.finished_at is not None:
                    completed.append((tr.finished_at, task_id))
            if len(completed) <= self._max_completed_results // 2:
                return
            completed.sort()
            # Evict the oldest half
            evict_count = len(completed) - self._max_completed_results // 2
            for i in range(evict_count):
                task_id = completed[i][1]
                self._results.pop(task_id, None)
                self._handles.pop(task_id, None)

    @property
    def pending(self) -> int:
        """Number of tasks waiting in the queue."""
        return self._queue.qsize()

    @property
    def stats(self) -> TaskQueueStats:
        """Return comprehensive queue statistics."""
        elapsed = time.monotonic() - self._stats_start_time
        with self._lock:
            processed = self._tasks_processed
            failed = self._tasks_failed
            retried = self._tasks_retried
            running = self._tasks_running
            total_time = self._total_execution_time_ms

        avg_ms = total_time / processed if processed > 0 else 0.0
        tps = processed / elapsed if elapsed > 0 else 0.0

        return TaskQueueStats(
            pending=self.pending,
            running=running,
            processed=processed,
            failed=failed,
            retried=retried,
            workers=len(self._workers),
            queue_running=self._running,
            dead_letters=self.dead_letters.size,
            scheduled=0,  # filled by scheduler
            avg_execution_time_ms=avg_ms,
            tasks_per_second=tps,
        )


# ---------------------------------------------------------------------------
# Task Scheduler
# ---------------------------------------------------------------------------


class TaskScheduler:
    """Runs scheduled tasks using the `scheduler` library's timing engine.

    Replaces the old poll-loop architecture with proper timer-based scheduling
    via scheduler.Scheduler. Supports interval, daily, hourly, weekly, minutely,
    and cron-expression scheduling.

    The scheduler library handles precise next-execution calculations, timezone
    support, skip_missing, and priority-based execution ordering.
    """

    def __init__(self, task_queue: TaskQueue | None = None):
        self._engine: _SchedulerEngine = _SchedulerEngine(n_threads=0)
        self._entries: dict[str, ScheduleEntry] = {}
        self._jobs: dict[str, object] = {}  # schedule_id -> scheduler.Job
        self._lock: threading.Lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running: bool = False
        self._queue: TaskQueue = task_queue or _task_queue
        # Set to wake the loop early when the schedule changes (add/remove/stop),
        # so it recomputes the next due time instead of oversleeping.
        self._wake: threading.Event = threading.Event()

    # Upper bound on how long the loop sleeps between due-time checks. Caps drift
    # from clock changes / long-horizon jobs and keeps stop() responsive.
    _MAX_SLEEP_SECONDS = 5.0

    def add(
        self,
        task_decorator: TaskDecorator,
        *,
        interval: float | None = None,
        cron: str | None = None,
        daily: datetime.time | None = None,
        hourly: datetime.time | None = None,
        weekly: object | None = None,  # Monday(time) etc from scheduler.trigger
        minutely: datetime.time | None = None,
        args: tuple = (),
        kwargs: dict[str, object] | None = None,
        skip_if_running: bool = False,
    ) -> str:
        """Add a scheduled task. Returns the schedule_id.

        Specify exactly one timing method:
            interval: Seconds between executions (cyclic).
            cron: 5-field cron expression (translated to scheduler timing).
            daily: datetime.time — run at this time every day.
            hourly: datetime.time — run at this minute:second every hour.
            weekly: Weekday trigger (e.g., Monday(time(hour=3))) — run weekly.
            minutely: datetime.time — run at this second every minute.

        skip_if_running: when True, a tick is skipped if the previous run of
            this job is still in flight (pending/running/retrying), so a slow
            job cannot pile up backlogged instances. Default False preserves
            fire-every-tick behavior.
        """
        timing_count = sum(
            x is not None for x in (interval, cron, daily, hourly, weekly, minutely)
        )
        if timing_count == 0:
            raise ValueError(
                "Must specify a timing method (interval, cron, daily, hourly, weekly, or minutely)"
            )
        if timing_count > 1:
            raise ValueError("Specify exactly one timing method")

        entry = ScheduleEntry(
            task=task_decorator,
            args=args,
            kwargs=kwargs if kwargs is not None else {},
            interval_seconds=interval,
            cron=cron,
        )

        # Build the callable that fires the task via our TaskQueue
        task_args = args
        task_kwargs = kwargs if kwargs is not None else {}

        # Mutable cell holding the most recent handle for the overlap guard.
        # A list avoids `nonlocal`/dataclass churn while staying per-job.
        _last_handle: list[TaskHandle | None] = [None]
        _ACTIVE_STATUSES = (
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.RETRYING,
        )

        def _fire() -> None:
            if not self._queue._running:
                self._queue.start()
            if skip_if_running:
                prev = _last_handle[0]
                if prev is not None:
                    with prev._lock:
                        tr = prev._results.get(prev.task_id)
                    # A MISSING result means finished-and-evicted (only genuinely
                    # in-flight states trigger a skip); this avoids permanently
                    # skipping once cleanup evicts the completed result — where
                    # status() would otherwise report PENDING for a missing id.
                    if tr is not None and tr.status in _ACTIVE_STATUSES:
                        logger.debug(
                            "Skipping scheduled task {name}: previous run still active",
                            name=task_decorator.__name__,
                        )
                        return
            _last_handle[0] = task_decorator.delay(*task_args, **task_kwargs)

        # Schedule via the scheduler library. All self._engine.* access — job
        # creation here, delete_job in remove(), and the jobs/exec_jobs reads in
        # the loop — is serialized under self._lock: the engine is not internally
        # synchronized, so concurrent add()/remove()/iterate would corrupt it.
        with self._lock:
            if interval is not None:
                job = self._engine.cyclic(
                    datetime.timedelta(seconds=interval),
                    _fire,
                    alias=f"task:{entry.schedule_id}",
                )
            elif cron is not None:
                method_name, timing_arg = _cron_to_scheduler_timing(cron)
                # Explicit dispatch — no getattr. The cron translator returns one
                # of these five method names; anything else is a bug in our code.
                _dispatch: dict[str, Callable[..., object]] = {
                    "cyclic": self._engine.cyclic,
                    "minutely": self._engine.minutely,
                    "hourly": self._engine.hourly,
                    "daily": self._engine.daily,
                    "weekly": self._engine.weekly,
                }
                method = _dispatch[method_name]
                job = method(timing_arg, _fire, alias=f"task:{entry.schedule_id}")
            elif daily is not None:
                job = self._engine.daily(
                    daily, _fire, alias=f"task:{entry.schedule_id}"
                )
            elif hourly is not None:
                job = self._engine.hourly(
                    hourly, _fire, alias=f"task:{entry.schedule_id}"
                )
            elif weekly is not None:
                job = self._engine.weekly(
                    weekly, _fire, alias=f"task:{entry.schedule_id}"
                )
            elif minutely is not None:
                job = self._engine.minutely(
                    minutely, _fire, alias=f"task:{entry.schedule_id}"
                )

            self._entries[entry.schedule_id] = entry
            self._jobs[entry.schedule_id] = job

        # A new job may be due sooner than the loop's current sleep — wake it.
        self._wake.set()
        return entry.schedule_id

    def remove(self, schedule_id: str) -> bool:
        """Remove a scheduled task. Returns True if found and removed."""
        with self._lock:
            entry = self._entries.pop(schedule_id, None)
            job = self._jobs.pop(schedule_id, None)
            # delete_job under the same lock — the engine is not synchronized.
            if job is not None:
                with contextlib.suppress(Exception):
                    self._engine.delete_job(job)
        if entry is None:
            return False
        self._wake.set()
        return True

    def start(self) -> None:
        """Start the scheduler background thread.

        The running-check + state flip + spawn run under self._lock (like
        TaskQueue.start) so two concurrent first-callers can't each pass the
        check and spawn a second scheduler thread — which would fire every job
        twice.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._scheduler_loop, name="task-scheduler", daemon=True
            )
            self._thread.start()
        logger.info("Task scheduler started")

    def is_running(self) -> bool:
        """True when the scheduler loop thread is alive — use in a readiness
        check so scheduled maintenance silently stopping is visible."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Stop the scheduler."""
        if not self._running:
            return
        self._running = False
        self._wake.set()  # break the loop out of its sleep promptly
        if self._thread is not None:
            self._thread.join(timeout=int(get_setting("TASK_SHUTDOWN_TIMEOUT")))
            self._thread = None
        logger.info("Task scheduler stopped")

    @property
    def count(self) -> int:
        """Number of scheduled entries."""
        with self._lock:
            return len(self._entries)

    def _seconds_until_next(self) -> float:
        """Seconds until the soonest due job, clamped to [0, _MAX_SLEEP_SECONDS].

        Returns _MAX_SLEEP_SECONDS when no jobs are scheduled.
        """
        now = datetime.datetime.now()
        soonest: float | None = None
        # Snapshot the engine's job list under the lock (engine is unsynchronized
        # vs. concurrent add()/remove()), then compute due times off the copy.
        with self._lock:
            jobs = list(self._engine.jobs)
        for job in jobs:
            # dynamic-attr: job is a third-party scheduler.Job; its "datetime" (next-due) attribute is external API, not guaranteed present across job types/versions
            dt = getattr(job, "datetime", None)
            if dt is None:
                continue
            delta = (dt - now).total_seconds()
            if soonest is None or delta < soonest:
                soonest = delta
        if soonest is None:
            return self._MAX_SLEEP_SECONDS
        return max(0.0, min(soonest, self._MAX_SLEEP_SECONDS))

    def _scheduler_loop(self) -> None:
        """Background thread that drives the scheduler engine.

        Sleeps until the next job is actually due (bounded by _MAX_SLEEP_SECONDS)
        instead of waking on a flat 100ms tick. The scheduler library computes
        exact due times; we ask it for the soonest and wait precisely that long,
        waking early via self._wake when the schedule changes or on stop().
        """
        while self._running:
            try:
                # Serialize exec_jobs against add()/remove() engine mutations.
                # _fire only enqueues onto the TaskQueue (a different lock), so
                # firing due jobs while holding self._lock can't re-enter here.
                with self._lock:
                    self._engine.exec_jobs()
            # blind-except: scheduler-loop resilience — a failing job execution is logged and the loop continues; one bad job must not kill the background scheduler thread.
            except Exception:
                logger.error(
                    "Scheduler exec_jobs error:\n{tb}",
                    tb=tb_module.format_exc(),
                )
            # Wait until the next due time (or an add/remove/stop wakeup).
            self._wake.wait(timeout=self._seconds_until_next())
            self._wake.clear()


# ---------------------------------------------------------------------------
# Task Group
# ---------------------------------------------------------------------------


class TaskGroup:
    """Run multiple tasks in parallel and wait for all to complete."""

    def __init__(self):
        self._handles: list[TaskHandle] = []

    def add(
        self, task_decorator: TaskDecorator, *args: object, **kwargs: object
    ) -> TaskHandle:
        """Add a task to the group. Returns its TaskHandle."""
        handle = task_decorator.delay(*args, **kwargs)
        self._handles.append(handle)
        return handle

    def run(self, timeout: float | None = None) -> list[TaskResult]:
        """Wait for all tasks to complete and return their results.

        Raises TimeoutError if any task does not finish within `timeout`.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        results: list[TaskResult] = []

        for handle in self._handles:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TaskGroup timed out waiting for tasks")

            # Route through handle.result() so it uses the lazy-event
            # machinery — catches TimeoutError and rewraps with the
            # task-specific message the original code produced. Raised
            # task errors propagate to TaskGroup callers via tr.status.
            try:
                handle.result(timeout=remaining)
            except TimeoutError:
                raise TimeoutError(
                    f"TaskGroup timed out waiting for task {handle.task_id}"
                )
            except RuntimeError:
                # Task failed or was cancelled — still want to collect
                # the TaskResult below so the caller sees it.
                pass
            with handle._lock:
                # _current_result (not a bare dict read): result() above may have
                # resolved from the handle's cached snapshot after the shared
                # entry was evicted — read the same durable view here so a
                # completed task is never mis-reported as PENDING.
                tr = handle._current_result()
            if tr is not None:
                results.append(tr)
            else:
                results.append(
                    TaskResult(
                        task_id=handle.task_id,
                        status=TaskStatus.PENDING,
                    )
                )

        return results


# ---------------------------------------------------------------------------
# Global instances
# ---------------------------------------------------------------------------

_task_queue = TaskQueue()


# ---------------------------------------------------------------------------
# Task Decorator
# ---------------------------------------------------------------------------


class TaskDecorator:
    """Wraps a function to make it enqueueable as a background task.

    The wrapped function can be called normally (sync) or via .delay() (async background).
    """

    def __init__(
        self,
        func: Callable,
        task_queue: TaskQueue | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        max_retries: int = 0,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
        retry_on: tuple[type[BaseException], ...] | None = None,
        on_success: Callable | None = None,
        on_failure: Callable | None = None,
        on_retry: Callable | None = None,
        timeout: float = 0,
    ):
        self._func: Callable = func
        self._queue: TaskQueue = task_queue or _task_queue
        self._priority: TaskPriority = priority
        self._max_retries: int = max_retries
        self._retry_delay: float = retry_delay
        self._retry_backoff: float = retry_backoff
        self._retry_on: tuple[type[BaseException], ...] | None = retry_on
        self._on_success: Callable | None = on_success
        self._on_failure: Callable | None = on_failure
        self._on_retry: Callable | None = on_retry
        self._timeout: float = timeout
        self.__name__: str = func.__name__
        self.__doc__: str | None = func.__doc__

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Call the function directly (synchronous)."""
        if inspect.iscoroutinefunction(self._func):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._func(*args, **kwargs))
            finally:
                loop.close()
        return self._func(*args, **kwargs)

    def delay(self, *args: object, user_id: str = "", **kwargs: object) -> TaskHandle:
        """Enqueue the function for background execution. Returns a TaskHandle.

        Pass user_id= to track per-user pending limits.
        """
        if not self._queue._running:
            self._queue.start()
        return self._queue.enqueue(
            self._func,
            *args,
            priority=self._priority,
            max_retries=self._max_retries,
            retry_delay=self._retry_delay,
            retry_backoff=self._retry_backoff,
            retry_on=self._retry_on,
            on_success=self._on_success,
            on_failure=self._on_failure,
            on_retry=self._on_retry,
            timeout=self._timeout,
            user_id=user_id,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# @task decorator
# ---------------------------------------------------------------------------


def task(
    func: Callable | None = None,
    *,
    queue: TaskQueue | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
    max_retries: int = 0,
    retry_delay: float = 1.0,
    retry_backoff: float = 2.0,
    retry_on: tuple[type[BaseException], ...] | None = None,
    on_success: Callable | None = None,
    on_failure: Callable | None = None,
    on_retry: Callable | None = None,
    timeout: float = 0,
) -> TaskDecorator | Callable:
    """Decorator to make a function a background task.

    Usage:
        @task
        async def send_email(to, subject):
            ...

        @task(max_retries=3, retry_delay=1.0, retry_backoff=2.0,
              retry_on=(ConnectionError, TimeoutError))
        async def fetch_data(url):
            ...

        # Call directly:
        send_email("user@example.com", "Hello")

        # Or enqueue for background:
        handle = send_email.delay("user@example.com", "Hello")
        handle.status()  # TaskStatus.PENDING / RUNNING / SUCCESS / ...
    """
    if func is not None:
        return TaskDecorator(func, queue)

    def decorator(fn: Callable) -> TaskDecorator:
        return TaskDecorator(
            fn,
            task_queue=queue,
            priority=priority,
            max_retries=max_retries,
            retry_delay=retry_delay,
            retry_backoff=retry_backoff,
            retry_on=retry_on,
            on_success=on_success,
            on_failure=on_failure,
            on_retry=on_retry,
            timeout=timeout,
        )

    return decorator
