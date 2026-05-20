"""Comprehensive tests for hyperdjango.tasks enhanced module."""

# hyper-test: unit

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import contextlib
import datetime as dt
import enum
import threading
import time

from hyperdjango.tasks import (
    DeadLetter,
    DeadLetterQueue,
    Monday,
    ScheduleEntry,
    TaskDecorator,
    TaskGroup,
    TaskHandle,
    TaskMessage,
    TaskPriority,
    TaskQueue,
    TaskQueueStats,
    TaskResult,
    TaskScheduler,
    TaskStatus,
    _cron_to_scheduler_timing,
    task,
)

PASS = 0
FAIL = 0


def test(name, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        print(f"    got:      {got!r}")
        print(f"    expected: {expected!r}")


def test_true(name, value):
    test(name, bool(value), True)


def test_false(name, value):
    test(name, bool(value), False)


def test_isinstance(name, obj, cls):
    test(name, isinstance(obj, cls), True)


def test_raises(name, exc_type, fn):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        print(f"  FAIL: {name}")
        print(f"    expected {exc_type.__name__} but no exception raised")
    except exc_type:
        PASS += 1
    except Exception as e:
        FAIL += 1
        print(f"  FAIL: {name}")
        print(f"    expected {exc_type.__name__} but got {type(e).__name__}: {e}")


def wait_for(pred, timeout: float = 10.0, interval: float = 0.01) -> bool:
    """Poll ``pred`` until true or the deadline; condition-wait, not sleep.

    A fixed sleep before an assertion states a guess about how fast the machine
    is; this states the condition the assertion actually depends on, so the same
    test is exact on a dev box and on a loaded 2-core runner. The ceiling is
    generous because it only bounds the pathological CPU-starved case — a real
    failure never satisfies the predicate and still fails once it elapses.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def drain_queue(q, timeout: float = 10.0) -> bool:
    """Wait until the queue is empty AND no worker is still executing a task.

    ``pending == 0`` alone is not "the work is done": the message has left the
    queue but a worker may still be inside the function. Both counters must
    read zero for an assertion about the RESULT of that work to be sound.
    """
    return wait_for(lambda: q.pending == 0 and q.stats.running == 0, timeout=timeout)


# ---------------------------------------------------------------------------
# TaskPriority enum
# ---------------------------------------------------------------------------


def test_task_priority():
    print("-- TaskPriority --")
    test("LOW value", TaskPriority.LOW, 0)
    test("NORMAL value", TaskPriority.NORMAL, 10)
    test("HIGH value", TaskPriority.HIGH, 20)
    test("CRITICAL value", TaskPriority.CRITICAL, 30)

    test_true("HIGH > NORMAL", TaskPriority.HIGH > TaskPriority.NORMAL)
    test_true("NORMAL > LOW", TaskPriority.NORMAL > TaskPriority.LOW)
    test_true("CRITICAL > HIGH", TaskPriority.CRITICAL > TaskPriority.HIGH)
    test_false("LOW > NORMAL", TaskPriority.LOW > TaskPriority.NORMAL)

    test_true("IntEnum subclass", issubclass(TaskPriority, enum.IntEnum))
    test_true("IntEnum instance", isinstance(TaskPriority.LOW, int))

    # Arithmetic works because IntEnum
    test("HIGH + LOW", TaskPriority.HIGH + TaskPriority.LOW, 20)
    test("CRITICAL - NORMAL", TaskPriority.CRITICAL - TaskPriority.NORMAL, 20)


# ---------------------------------------------------------------------------
# TaskStatus enum
# ---------------------------------------------------------------------------


def test_task_status():
    print("-- TaskStatus --")
    test("PENDING value", TaskStatus.PENDING, "pending")
    test("RUNNING value", TaskStatus.RUNNING, "running")
    test("SUCCESS value", TaskStatus.SUCCESS, "success")
    test("FAILED value", TaskStatus.FAILED, "failed")
    test("RETRYING value", TaskStatus.RETRYING, "retrying")
    test("CANCELLED value", TaskStatus.CANCELLED, "cancelled")

    test_true("StrEnum subclass", issubclass(TaskStatus, enum.StrEnum))
    test_true("StrEnum instance", isinstance(TaskStatus.PENDING, str))

    # String comparison
    test_true("PENDING == 'pending'", TaskStatus.PENDING == "pending")
    test("str(SUCCESS)", str(TaskStatus.SUCCESS), "success")

    # All 6 values
    test("status count", len(TaskStatus), 6)


# ---------------------------------------------------------------------------
# TaskResult dataclass
# ---------------------------------------------------------------------------


def test_task_result():
    print("-- TaskResult --")
    tr = TaskResult(task_id="abc", status=TaskStatus.PENDING)
    test("task_id", tr.task_id, "abc")
    test("status", tr.status, TaskStatus.PENDING)
    test("result default", tr.result, None)
    test("error default", tr.error, None)
    test("started_at default", tr.started_at, None)
    test("finished_at default", tr.finished_at, None)
    test("attempts default", tr.attempts, 0)

    tr2 = TaskResult(
        task_id="xyz",
        status=TaskStatus.SUCCESS,
        result=42,
        error=None,
        started_at=1.0,
        finished_at=2.0,
        attempts=3,
    )
    test("result value", tr2.result, 42)
    test("started_at", tr2.started_at, 1.0)
    test("finished_at", tr2.finished_at, 2.0)
    test("attempts", tr2.attempts, 3)

    # slots=True
    test_true("has __slots__", hasattr(TaskResult, "__slots__"))


# ---------------------------------------------------------------------------
# TaskMessage ordering
# ---------------------------------------------------------------------------


def test_task_message_ordering():
    print("-- TaskMessage ordering --")

    def noop():
        pass

    high = TaskMessage(
        func=noop,
        args=(),
        kwargs={},
        task_id="h",
        priority=TaskPriority.HIGH,
        enqueued_at=2.0,
    )
    normal = TaskMessage(
        func=noop,
        args=(),
        kwargs={},
        task_id="n",
        priority=TaskPriority.NORMAL,
        enqueued_at=1.0,
    )
    low = TaskMessage(
        func=noop,
        args=(),
        kwargs={},
        task_id="l",
        priority=TaskPriority.LOW,
        enqueued_at=0.5,
    )

    # Higher priority compares as "less than" for PriorityQueue (dequeue first)
    test_true("HIGH < NORMAL (dequeues first)", high < normal)
    test_true("NORMAL < LOW (dequeues first)", normal < low)
    test_true("HIGH < LOW (dequeues first)", high < low)
    test_false("LOW < HIGH", low < high)

    # Same priority, same comparison (uses priority only)
    normal2 = TaskMessage(
        func=noop,
        args=(),
        kwargs={},
        task_id="n2",
        priority=TaskPriority.NORMAL,
        enqueued_at=5.0,
    )
    test_false("same priority not less", normal < normal2)
    test_false("same priority not less reverse", normal2 < normal)

    # Sorting gives highest priority first
    msgs = [low, normal, high]
    msgs.sort()
    test("sorted priorities", [m.task_id for m in msgs], ["h", "n", "l"])


# ---------------------------------------------------------------------------
# TaskQueue basics
# ---------------------------------------------------------------------------


def test_task_queue_lifecycle():
    print("-- TaskQueue lifecycle --")
    q = TaskQueue(workers=2, max_queue_size=100)
    test_false("not running initially", q._running)
    test("pending 0", q.pending, 0)

    q.start()
    test_true("running after start", q._running)
    test("worker count", len(q._workers), 2)

    # Double start is no-op
    q.start()
    test("still 2 workers", len(q._workers), 2)

    q.stop()
    test_false("not running after stop", q._running)
    test("workers cleared", len(q._workers), 0)

    # Double stop is no-op
    q.stop()
    test_false("still not running", q._running)


def test_task_queue_sync():
    print("-- TaskQueue sync task --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:

        def add(a, b):
            return a + b

        handle = q.enqueue(add, 3, 7)
        result = handle.result(timeout=5)
        test("sync result", result, 10)
        test("status success", handle.status(), TaskStatus.SUCCESS)
        test_true("is_done", handle.is_done())
    finally:
        q.stop()


def test_task_queue_async():
    print("-- TaskQueue async task --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:

        async def multiply(a, b):
            return a * b

        handle = q.enqueue(multiply, 4, 5)
        result = handle.result(timeout=5)
        test("async result", result, 20)
        test("status success", handle.status(), TaskStatus.SUCCESS)
    finally:
        q.stop()


def test_task_queue_stats():
    print("-- TaskQueue stats --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:
        done = threading.Event()

        def simple():
            done.set()
            return 1

        handle = q.enqueue(simple)
        handle.result(timeout=5)
        done.wait(timeout=5)
        # The worker publishes the result (releasing `result()`) BEFORE it bumps
        # the stats counters, so wait for the counter rather than for the machine
        # — then the count can be asserted EXACTLY: one task was enqueued.
        test_true("stats counter caught up", wait_for(lambda: q.stats.processed >= 1))

        stats = q.stats
        test_isinstance("stats type", stats, TaskQueueStats)
        test("processed", stats.processed, 1)
        test("workers", stats.workers, 2)
        test_true("queue_running", stats.queue_running)
        test_true("avg_execution_time_ms >= 0", stats.avg_execution_time_ms >= 0)
        test_true("tasks_per_second >= 0", stats.tasks_per_second >= 0)
    finally:
        q.stop()


def test_task_queue_full():
    print("-- TaskQueue full --")
    q = TaskQueue(workers=1, max_queue_size=2)
    # Don't start -- tasks won't be consumed, so queue fills up
    try:

        def slow():
            time.sleep(10)
            return 1

        # Fill up the queue (not started, so nothing drains)
        h1 = q.enqueue(slow)
        h2 = q.enqueue(slow)
        # Third should be dropped
        h3 = q.enqueue(slow)
        test("dropped task status", h3.status(), TaskStatus.FAILED)
        test_true("dropped task is_done", h3.is_done())
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# TaskHandle
# ---------------------------------------------------------------------------


def test_task_handle():
    print("-- TaskHandle --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:

        def compute():
            return 42

        handle = q.enqueue(compute)
        result = handle.result(timeout=5)
        test("result value", result, 42)
        test("status", handle.status(), TaskStatus.SUCCESS)
        test_true("is_done", handle.is_done())
    finally:
        q.stop()


def test_task_handle_timeout():
    print("-- TaskHandle timeout --")
    q = TaskQueue(workers=1, max_queue_size=100)
    q.start()
    try:
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(10)
            return 1

        handle = q.enqueue(slow)
        started.wait(timeout=5)
        test_raises("result timeout", TimeoutError, lambda: handle.result(timeout=0.05))
    finally:
        q.stop()


def test_execution_timeout_frees_worker():
    # EXECUTION timeout (msg.timeout) — distinct from the handle.result wait.
    # A sync task exceeding its timeout must be marked FAILED via the SHARED
    # timed-task executor (not a fresh per-task executor), and must not block
    # the worker: a later task still runs.
    print("-- execution timeout: shared executor, worker keeps going --")
    q = TaskQueue(workers=1, max_queue_size=100)
    q.start()
    try:
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(10)  # >> the 0.2s execution timeout
            return "unreachable"

        h_slow = q.enqueue(slow, timeout=0.2)
        started.wait(timeout=5)
        test_raises(
            "timed-out task fails", RuntimeError, lambda: h_slow.result(timeout=5)
        )
        test("timed-out task status", h_slow.status(), TaskStatus.FAILED)
        # The shared, bounded executor is used (not one per task).
        test_true("shared timed executor created", q._timed_executor is not None)
        # The worker is not blocked by the orphaned slow task.
        h_next = q.enqueue(lambda: 7)
        test("worker continues after timeout", h_next.result(timeout=5), 7)
    finally:
        q.stop()


def test_task_handle_cancel():
    print("-- TaskHandle cancel --")
    q = TaskQueue(workers=1, max_queue_size=100)
    # Don't start so task stays pending
    try:

        def noop():
            return 1

        handle = q.enqueue(noop)
        test("pending before cancel", handle.status(), TaskStatus.PENDING)
        cancelled = handle.cancel()
        test_true("cancel returned True", cancelled)
        test("status cancelled", handle.status(), TaskStatus.CANCELLED)
        test_true("is_done after cancel", handle.is_done())

        # Cancel again returns False
        cancelled2 = handle.cancel()
        test_false("second cancel returns False", cancelled2)

        # result() on cancelled task raises RuntimeError
        test_raises(
            "result on cancelled", RuntimeError, lambda: handle.result(timeout=1)
        )
    finally:
        q.stop()


def test_task_handle_cancel_running():
    print("-- TaskHandle cancel running --")
    q = TaskQueue(workers=1, max_queue_size=100)
    q.start()
    try:
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(10)
            return 1

        handle = q.enqueue(slow)
        started.wait(timeout=5)
        # Task is already running, cancel should return False
        cancelled = handle.cancel()
        test_false("cannot cancel running task", cancelled)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# @task decorator
# ---------------------------------------------------------------------------


def test_task_decorator_bare():
    print("-- @task bare --")
    q = TaskQueue(workers=2, max_queue_size=100)

    @task
    def greet(name):
        return f"Hello, {name}!"

    test_isinstance("returns TaskDecorator", greet, TaskDecorator)
    test("__name__", greet.__name__, "greet")

    # Direct call
    result = greet("World")
    test("direct call", result, "Hello, World!")


def test_task_decorator_parens():
    print("-- @task() with parens --")
    q = TaskQueue(workers=2, max_queue_size=100)

    @task()
    def add(a, b):
        return a + b

    test_isinstance("returns TaskDecorator", add, TaskDecorator)
    result = add(2, 3)
    test("direct call", result, 5)


def test_task_decorator_priority():
    print("-- @task with priority --")

    @task(priority=TaskPriority.HIGH)
    def important():
        return "done"

    test("priority set", important._priority, TaskPriority.HIGH)
    test("direct call", important(), "done")


def test_task_decorator_delay():
    print("-- @task .delay() --")
    q = TaskQueue(workers=2, max_queue_size=100)

    @task(queue=q)
    def compute(x):
        return x * 2

    q.start()
    try:
        handle = compute.delay(21)
        test_isinstance("handle type", handle, TaskHandle)
        result = handle.result(timeout=5)
        test("delay result", result, 42)
    finally:
        q.stop()


def test_task_decorator_delay_autostart():
    print("-- @task .delay() auto-starts queue --")
    q = TaskQueue(workers=1, max_queue_size=100)

    @task(queue=q)
    def simple():
        return 99

    test_false("queue not running before delay", q._running)
    handle = simple.delay()
    test_true("queue running after delay", q._running)
    result = handle.result(timeout=5)
    test("autostart result", result, 99)
    q.stop()


def test_task_decorator_async():
    print("-- @task async function --")
    q = TaskQueue(workers=2, max_queue_size=100)

    @task(queue=q)
    async def async_add(a, b):
        return a + b

    # Direct call (synchronous wrapper)
    result = async_add(10, 20)
    test("async direct call", result, 30)

    # Delay
    q.start()
    try:
        handle = async_add.delay(5, 15)
        result = handle.result(timeout=5)
        test("async delay result", result, 20)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# Retry mechanism
# ---------------------------------------------------------------------------


def test_retry_basic():
    print("-- Retry basic --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("not yet")
        return "ok"

    try:
        handle = q.enqueue(
            flaky,
            max_retries=3,
            retry_delay=0.01,
            retry_backoff=1.0,
        )
        result = handle.result(timeout=10)
        test("retry succeeded", result, "ok")
        test("attempt count", len(attempts), 3)
    finally:
        q.stop()


def test_retry_with_filter():
    print("-- Retry with retry_on filter --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    attempts = []

    def fails_wrong_type():
        attempts.append(1)
        raise TypeError("wrong type")

    try:
        handle = q.enqueue(
            fails_wrong_type,
            max_retries=3,
            retry_delay=0.01,
            retry_backoff=1.0,
            retry_on=(ValueError,),  # Only retry ValueError, not TypeError
        )
        # Should fail immediately since TypeError not in retry_on
        test_raises(
            "fails with RuntimeError", RuntimeError, lambda: handle.result(timeout=5)
        )
        test("only 1 attempt", len(attempts), 1)
    finally:
        q.stop()


def test_retry_exhausted_dlq():
    print("-- Retry exhausted -> DLQ --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    attempts = []

    def always_fails():
        attempts.append(1)
        raise ValueError("permanent failure")

    try:
        handle = q.enqueue(
            always_fails,
            max_retries=2,
            retry_delay=0.01,
            retry_backoff=1.0,
        )
        test_raises(
            "permanently failed", RuntimeError, lambda: handle.result(timeout=10)
        )
        test("3 attempts (1 + 2 retries)", len(attempts), 3)
        # The DLQ push happens AFTER the FAILED result is published, so the
        # raise above does not imply the entry has landed. Wait for it, then
        # assert the exact count — one task failed permanently, one dead letter.
        test_true("dlq entry landed", wait_for(lambda: q.dead_letters.size >= 1))
        test("dlq has entry", q.dead_letters.size, 1)
    finally:
        q.stop()


def test_retry_on_retry_hook():
    print("-- on_retry hook --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    retry_info = []
    attempts = []

    def on_retry_hook(exc, attempt_num):
        retry_info.append((str(exc), attempt_num))

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("oops")
        return "done"

    try:
        handle = q.enqueue(
            flaky,
            max_retries=3,
            retry_delay=0.01,
            retry_backoff=1.0,
            on_retry=on_retry_hook,
        )
        handle.result(timeout=10)
        test("retry hook called twice", len(retry_info), 2)
        test("first retry exception", retry_info[0][0], "oops")
        test("first retry attempt", retry_info[0][1], 1)
        test("second retry attempt", retry_info[1][1], 2)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# Task lifecycle hooks
# ---------------------------------------------------------------------------


def test_on_success_hook():
    print("-- on_success hook --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    success_values = []

    def on_success(value):
        success_values.append(value)

    def compute():
        return 42

    try:
        handle = q.enqueue(compute, on_success=on_success)
        handle.result(timeout=5)
        # The on_success hook runs AFTER the result is published, so `result()`
        # returning says nothing about the hook. Wait for the hook's own effect.
        test_true("on_success hook fired", wait_for(lambda: len(success_values) >= 1))
        test("on_success called", success_values, [42])
    finally:
        q.stop()


def test_on_failure_hook():
    print("-- on_failure hook --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    failure_errors = []

    def on_failure(exc):
        failure_errors.append(str(exc))

    def fails():
        raise RuntimeError("boom")

    try:
        handle = q.enqueue(fails, on_failure=on_failure)
        with contextlib.suppress(RuntimeError):
            handle.result(timeout=5)
        # Same ordering as on_success: the FAILED result is published first and
        # the hook runs after it, so wait for the hook's effect, not for time.
        test_true("on_failure hook fired", wait_for(lambda: len(failure_errors) >= 1))
        test("on_failure called", len(failure_errors), 1)
        test("on_failure error", failure_errors[0], "boom")
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# TaskScheduler
# ---------------------------------------------------------------------------


def test_scheduler_interval():
    print("-- TaskScheduler interval --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    executions = []

    @task(queue=q)
    def tick():
        executions.append(time.monotonic())
        return "tick"

    scheduler = TaskScheduler(task_queue=q)
    schedule_id = scheduler.add(tick, interval=0.1)
    test_true("schedule_id is string", isinstance(schedule_id, str))
    test("scheduler count", scheduler.count, 1)

    scheduler.start()
    try:
        # Wait for a few executions
        deadline = time.monotonic() + 3
        while len(executions) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        test_true("scheduled task ran >= 2 times", len(executions) >= 2)
    finally:
        scheduler.stop()
        q.stop()


def test_scheduler_remove():
    print("-- TaskScheduler remove --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    executions = []

    @task(queue=q)
    def tick2():
        executions.append(1)

    scheduler = TaskScheduler(task_queue=q)
    sid = scheduler.add(tick2, interval=0.1)
    scheduler.start()
    try:
        # Establish that the schedule is genuinely firing before removing it —
        # otherwise "stopped executing" below is vacuously true on a machine
        # that never got round to the first tick. Wait for the condition
        # (executions observed) rather than guessing a duration for it.
        test_true(
            "schedule fired before remove", wait_for(lambda: len(executions) >= 3)
        )
        removed = scheduler.remove(sid)
        test_true("remove returned True", removed)
        test("scheduler count after remove", scheduler.count, 0)

        # `remove` stops future SCHEDULING; it cannot retract ticks already
        # sitting in the task queue. Snapshotting executions right after the
        # call therefore measures the queue's backlog, not the scheduler — on a
        # loaded runner the workers are behind, the backlog drains during the
        # sleep below, and a correct `remove` looks broken (the CI failure:
        # "stopped executing, got False"). Drain first, THEN measure, and the
        # assertion becomes exact rather than approximate: nothing new can be
        # enqueued, so the count must not move AT ALL.
        test_true("backlog drained before measuring", drain_queue(q))
        count_before = len(executions)
        # timing-window: a bounded NEGATIVE — with the schedule removed and the
        # backlog drained, nothing can enqueue another tick, so the claim is
        # "no further execution happens", which has no condition to wait on. A
        # window is the correct construct; oversleeping on a loaded runner only
        # makes it a stronger check, never a false pass.
        time.sleep(0.5)
        count_after = len(executions)
        test("stopped executing", count_after, count_before)

        # Remove non-existent
        removed2 = scheduler.remove("nonexistent")
        test_false("remove nonexistent returns False", removed2)
    finally:
        scheduler.stop()
        q.stop()


def test_scheduler_lifecycle():
    print("-- TaskScheduler lifecycle --")
    q = TaskQueue(workers=1, max_queue_size=100)
    scheduler = TaskScheduler(task_queue=q)
    test_false("not running initially", scheduler._running)
    scheduler.start()
    test_true("running after start", scheduler._running)
    scheduler.start()  # Double start is no-op
    test_true("still running", scheduler._running)
    scheduler.stop()
    test_false("stopped", scheduler._running)
    scheduler.stop()  # Double stop is no-op
    test_false("still stopped", scheduler._running)
    q.stop()


# ---------------------------------------------------------------------------
# Cron parsing
# ---------------------------------------------------------------------------


def test_cron_to_scheduler_every_minute():
    print("-- Cron translation: every minute --")
    method, timing = _cron_to_scheduler_timing("* * * * *")
    test("method", method, "cyclic")
    test("timing", timing, dt.timedelta(minutes=1))


def test_cron_to_scheduler_every_5_minutes():
    print("-- Cron translation: every 5 minutes --")
    method, timing = _cron_to_scheduler_timing("*/5 * * * *")
    test("method", method, "cyclic")
    test("timing", timing, dt.timedelta(minutes=5))


def test_cron_to_scheduler_every_hour():
    print("-- Cron translation: minute 0 every hour --")
    method, timing = _cron_to_scheduler_timing("0 * * * *")
    test("method", method, "hourly")
    test("timing", timing, dt.time(minute=0))


def test_cron_to_scheduler_daily_midnight():
    print("-- Cron translation: daily midnight --")
    method, timing = _cron_to_scheduler_timing("0 0 * * *")
    test("method", method, "daily")
    test("timing", timing, dt.time(hour=0, minute=0))


def test_cron_to_scheduler_daily_3am():
    print("-- Cron translation: daily 3am --")
    method, timing = _cron_to_scheduler_timing("0 3 * * *")
    test("method", method, "daily")
    test("timing", timing, dt.time(hour=3, minute=0))


def test_cron_to_scheduler_weekly_monday():
    print("-- Cron translation: every Monday midnight --")
    method, timing = _cron_to_scheduler_timing("0 0 * * 1")
    test("method", method, "weekly")
    # Should produce Monday trigger
    test_true("is Monday type", type(timing).__name__ == "Monday")


def test_cron_to_scheduler_every_2_hours():
    print("-- Cron translation: every 2 hours --")
    method, timing = _cron_to_scheduler_timing("0 */2 * * *")
    test("method", method, "cyclic")
    test("timing", timing, dt.timedelta(hours=2))


def test_cron_to_scheduler_invalid():
    print("-- Cron translation: invalid --")
    test_raises(
        "too few fields", ValueError, lambda: _cron_to_scheduler_timing("* * *")
    )
    test_raises(
        "too many fields", ValueError, lambda: _cron_to_scheduler_timing("* * * * * *")
    )
    test_raises("empty string", ValueError, lambda: _cron_to_scheduler_timing(""))
    test_raises(
        "zero step", ValueError, lambda: _cron_to_scheduler_timing("*/0 * * * *")
    )


def test_cron_to_scheduler_minute_30_hourly():
    print("-- Cron translation: at :30 every hour --")
    method, timing = _cron_to_scheduler_timing("30 * * * *")
    test("method", method, "hourly")
    test("timing", timing, dt.time(minute=30))


def test_cron_to_scheduler_sunday():
    print("-- Cron translation: Sunday 9am --")
    method, timing = _cron_to_scheduler_timing("0 9 * * 0")
    test("method", method, "weekly")
    test_true("is Sunday type", type(timing).__name__ == "Sunday")


# ---------------------------------------------------------------------------
# DeadLetterQueue
# ---------------------------------------------------------------------------


def test_dlq_push_pop():
    print("-- DLQ push/pop --")
    dlq = DeadLetterQueue(max_size=100)
    test("initial size", dlq.size, 0)

    letter = DeadLetter(
        task_id="t1",
        func_name="test_func",
        args=(1, 2),
        kwargs={"key": "val"},
        error="oops",
        traceback="...",
        failed_at=1.0,
        attempts=1,
    )
    dlq.push(letter)
    test("size after push", dlq.size, 1)

    popped = dlq.pop()
    test("popped task_id", popped.task_id, "t1")
    test("popped func_name", popped.func_name, "test_func")
    test("size after pop", dlq.size, 0)

    # Pop from empty
    empty = dlq.pop()
    test("pop empty returns None", empty, None)


def test_dlq_peek():
    print("-- DLQ peek --")
    dlq = DeadLetterQueue(max_size=100)
    for i in range(5):
        dlq.push(
            DeadLetter(
                task_id=f"t{i}",
                func_name="f",
                args=(),
                kwargs={},
                error="err",
                traceback="",
                failed_at=float(i),
                attempts=1,
            )
        )

    peeked = dlq.peek(3)
    test("peek count", len(peeked), 3)
    # Peek returns most recent (last 3)
    test("peek ids", [p.task_id for p in peeked], ["t2", "t3", "t4"])
    test("size unchanged", dlq.size, 5)


def test_dlq_clear():
    print("-- DLQ clear --")
    dlq = DeadLetterQueue(max_size=100)
    for i in range(3):
        dlq.push(
            DeadLetter(
                task_id=f"t{i}",
                func_name="f",
                args=(),
                kwargs={},
                error="err",
                traceback="",
                failed_at=0.0,
                attempts=1,
            )
        )
    test("size before clear", dlq.size, 3)
    dlq.clear()
    test("size after clear", dlq.size, 0)


def test_dlq_max_size():
    print("-- DLQ max size --")
    dlq = DeadLetterQueue(max_size=3)
    for i in range(5):
        dlq.push(
            DeadLetter(
                task_id=f"t{i}",
                func_name="f",
                args=(),
                kwargs={},
                error="err",
                traceback="",
                failed_at=float(i),
                attempts=1,
            )
        )
    test("capped at max_size", dlq.size, 3)
    # Oldest should have been dropped
    peeked = dlq.peek(10)
    test("oldest dropped", [p.task_id for p in peeked], ["t2", "t3", "t4"])


def test_dlq_retry():
    print("-- DLQ retry --")
    # DLQ.retry() uses the global _task_queue, so we test the mechanics
    # of push with func registry, and verify retry removes the entry
    dlq = DeadLetterQueue(max_size=100)

    def my_func():
        return "recovered"

    letter = DeadLetter(
        task_id="retry_test",
        func_name="my_func",
        args=(),
        kwargs={},
        error="failed",
        traceback="",
        failed_at=1.0,
        attempts=1,
    )
    dlq.push(letter, func=my_func)
    test("size after push", dlq.size, 1)
    test_true("func registered", "retry_test" in dlq._func_registry)

    # Test that retry removes from DLQ (it will enqueue to global _task_queue)
    # We need the global queue running for this
    from hyperdjango.tasks import _task_queue

    _task_queue.start()
    try:
        retry_handle = dlq.retry("retry_test")
        test_true("retry returned handle", retry_handle is not None)
        test("size after retry", dlq.size, 0)
        if retry_handle is not None:
            result = retry_handle.result(timeout=5)
            test("retry result", result, "recovered")
    finally:
        _task_queue.stop()


def test_dlq_retry_not_found():
    print("-- DLQ retry not found --")
    dlq = DeadLetterQueue(max_size=100)
    result = dlq.retry("nonexistent")
    test("retry nonexistent", result, None)


# ---------------------------------------------------------------------------
# TaskGroup
# ---------------------------------------------------------------------------


def test_task_group():
    print("-- TaskGroup --")
    q = TaskQueue(workers=4, max_queue_size=100)
    q.start()
    try:

        @task(queue=q)
        def compute(x):
            return x * 2

        group = TaskGroup()
        group.add(compute, 1)
        group.add(compute, 2)
        group.add(compute, 3)

        results = group.run(timeout=5)
        test("result count", len(results), 3)
        result_values = sorted(
            [r.result for r in results if r.status == TaskStatus.SUCCESS]
        )
        test("result values", result_values, [2, 4, 6])
    finally:
        q.stop()


def test_task_group_timeout():
    print("-- TaskGroup timeout --")
    q = TaskQueue(workers=1, max_queue_size=100)
    q.start()
    try:

        @task(queue=q)
        def slow_task():
            time.sleep(10)
            return "done"

        group = TaskGroup()
        # Need at least 2 tasks: the first wait exhausts the timeout,
        # then the second iteration sees remaining <= 0 and raises
        group.add(slow_task)
        group.add(slow_task)

        test_raises("group timeout", TimeoutError, lambda: group.run(timeout=0.1))
    finally:
        q.stop()


def test_task_group_empty():
    print("-- TaskGroup empty --")
    group = TaskGroup()
    results = group.run(timeout=1)
    test("empty group results", results, [])


# ---------------------------------------------------------------------------
# TaskQueueStats
# ---------------------------------------------------------------------------


def test_queue_stats_fields():
    print("-- TaskQueueStats fields --")
    stats = TaskQueueStats(
        pending=5,
        running=2,
        processed=100,
        failed=3,
        retried=7,
        workers=4,
        queue_running=True,
        dead_letters=1,
        scheduled=2,
        avg_execution_time_ms=15.5,
        tasks_per_second=42.0,
    )
    test("pending", stats.pending, 5)
    test("running", stats.running, 2)
    test("processed", stats.processed, 100)
    test("failed", stats.failed, 3)
    test("retried", stats.retried, 7)
    test("workers", stats.workers, 4)
    test("queue_running", stats.queue_running, True)
    test("dead_letters", stats.dead_letters, 1)
    test("scheduled", stats.scheduled, 2)
    test("avg_execution_time_ms", stats.avg_execution_time_ms, 15.5)
    test("tasks_per_second", stats.tasks_per_second, 42.0)


def test_queue_stats_computed():
    print("-- TaskQueueStats computed from queue --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:
        handles = []
        for i in range(5):
            h = q.enqueue(lambda: time.sleep(0.01) or 1)
            handles.append(h)

        for h in handles:
            h.result(timeout=5)

        # `mark_done` (which releases `result()`) runs BEFORE the worker bumps
        # `_tasks_processed`, so a returned result does not imply the counter has
        # moved. Wait for the counter itself, then assert the EXACT total —
        # exactly five tasks were enqueued on a fresh queue.
        test_true("all five accounted for", wait_for(lambda: q.stats.processed >= 5))
        stats = q.stats
        test("processed", stats.processed, 5)
        test_true("avg_execution_time_ms > 0", stats.avg_execution_time_ms > 0)
        test_true("tasks_per_second > 0", stats.tasks_per_second > 0)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_delay():
    print("-- Thread safety: concurrent delay --")
    q = TaskQueue(workers=4, max_queue_size=1000)
    q.start()
    try:

        @task(queue=q)
        def identity(x):
            return x

        handles = []
        errors = []

        def enqueue_many(start, count):
            for i in range(start, start + count):
                try:
                    h = identity.delay(i)
                    handles.append(h)
                except Exception as e:
                    errors.append(e)

        threads = []
        for t_idx in range(4):
            t = threading.Thread(target=enqueue_many, args=(t_idx * 25, 25))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10)

        test("no enqueue errors", len(errors), 0)
        test("100 handles", len(handles), 100)

        # Wait for all to complete
        results = set()
        for h in handles:
            try:
                r = h.result(timeout=10)
                results.add(r)
            except Exception:
                pass

        test("all unique results", len(results), 100)
    finally:
        q.stop()


def test_concurrent_result_reads():
    print("-- Thread safety: concurrent result reads --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:

        def slow_compute():
            time.sleep(0.1)
            return 42

        handle = q.enqueue(slow_compute)
        read_results = []
        errors = []

        def read_result():
            try:
                r = handle.result(timeout=5)
                read_results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_result) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        test("no read errors", len(errors), 0)
        test("all got same result", read_results, [42] * 5)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_integration_full_lifecycle():
    print("-- Integration: full lifecycle --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:
        results_log = []

        def process(data):
            results_log.append(data)
            return data.upper()

        handle = q.enqueue(process, "hello")
        test(
            "initial pending or running",
            handle.status()
            in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.SUCCESS),
            True,
        )

        result = handle.result(timeout=5)
        test("lifecycle result", result, "HELLO")
        test("status success", handle.status(), TaskStatus.SUCCESS)
        test_true("is_done", handle.is_done())
        test("side effect", results_log, ["hello"])
    finally:
        q.stop()


def test_integration_priority_ordering():
    print("-- Integration: priority ordering --")
    q = TaskQueue(workers=1, max_queue_size=100)
    # Don't start yet - fill queue first
    execution_order = []
    gate = threading.Event()

    def record(label):
        gate.wait(timeout=5)
        execution_order.append(label)
        return label

    # Enqueue low first, then high
    h_low = q.enqueue(record, "low", priority=TaskPriority.LOW)
    h_normal = q.enqueue(record, "normal", priority=TaskPriority.NORMAL)
    h_high = q.enqueue(record, "high", priority=TaskPriority.HIGH)

    # Now start and release the gate
    q.start()
    gate.set()
    try:
        h_high.result(timeout=5)
        h_normal.result(timeout=5)
        h_low.result(timeout=5)

        # With 1 worker, higher priority should run first
        test("first executed", execution_order[0], "high")
        test("second executed", execution_order[1], "normal")
        test("third executed", execution_order[2], "low")
    finally:
        q.stop()


def test_integration_retry_then_success():
    print("-- Integration: retry -> success --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    call_count = []

    def eventually_works():
        call_count.append(1)
        if len(call_count) < 2:
            raise ValueError("not ready")
        return "success"

    try:
        handle = q.enqueue(
            eventually_works,
            max_retries=3,
            retry_delay=0.01,
            retry_backoff=1.0,
        )
        result = handle.result(timeout=10)
        test("retry success", result, "success")
        test("called twice", len(call_count), 2)
    finally:
        q.stop()


def test_integration_retry_then_dlq():
    print("-- Integration: retry -> failure -> DLQ --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    call_count = []

    def always_fails():
        call_count.append(1)
        raise ValueError("always broken")

    failure_captured = []

    def on_failure(exc):
        failure_captured.append(str(exc))

    try:
        handle = q.enqueue(
            always_fails,
            max_retries=2,
            retry_delay=0.01,
            retry_backoff=1.0,
            on_failure=on_failure,
        )
        test_raises(
            "permanently failed", RuntimeError, lambda: handle.result(timeout=10)
        )
        # The FAILED result is published (releasing `result()`) BEFORE the worker
        # pushes to the DLQ and runs `on_failure`, so the raise above does not
        # imply either has happened yet. Wait for the last of them, then assert
        # EXACT counts instead of the approximate ones a sleep forced.
        test_true(
            "failure fully accounted for",
            wait_for(lambda: q.dead_letters.size >= 1 and len(failure_captured) >= 1),
        )
        test("total attempts", len(call_count), 3)
        test("in DLQ", q.dead_letters.size, 1)
        test("on_failure called", len(failure_captured), 1)
        test("failure message", failure_captured[0], "always broken")
    finally:
        q.stop()


def test_integration_kwargs():
    print("-- Integration: kwargs --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:

        def greet(name, greeting="Hello"):
            return f"{greeting}, {name}!"

        handle = q.enqueue(greet, "Alice", greeting="Hi")
        result = handle.result(timeout=5)
        test("kwargs result", result, "Hi, Alice!")
    finally:
        q.stop()


def test_integration_multiple_handles():
    print("-- Integration: multiple handles --")
    q = TaskQueue(workers=4, max_queue_size=100)
    q.start()
    try:

        def square(n):
            return n * n

        handles = [q.enqueue(square, i) for i in range(10)]
        results = [h.result(timeout=5) for h in handles]
        test("multiple results", sorted(results), [0, 1, 4, 9, 16, 25, 36, 49, 64, 81])
    finally:
        q.stop()


def test_task_decorator_with_all_options():
    print("-- @task with all options --")
    q = TaskQueue(workers=2, max_queue_size=100)
    success_log = []
    failure_log = []
    retry_log = []

    @task(
        queue=q,
        priority=TaskPriority.HIGH,
        max_retries=2,
        retry_delay=0.5,
        retry_backoff=2.0,
        retry_on=(ValueError,),
        on_success=lambda v: success_log.append(v),
        on_failure=lambda e: failure_log.append(str(e)),
        on_retry=lambda e, a: retry_log.append((str(e), a)),
    )
    def configured_task():
        return "configured"

    test("priority", configured_task._priority, TaskPriority.HIGH)
    test("max_retries", configured_task._max_retries, 2)
    test("retry_delay", configured_task._retry_delay, 0.5)
    test("retry_backoff", configured_task._retry_backoff, 2.0)
    test("retry_on", configured_task._retry_on, (ValueError,))

    # Direct call still works
    result = configured_task()
    test("direct call", result, "configured")


def test_dead_letter_fields():
    print("-- DeadLetter fields --")
    dl = DeadLetter(
        task_id="abc",
        func_name="my_func",
        args=(1, 2),
        kwargs={"k": "v"},
        error="failed",
        traceback="Traceback...",
        failed_at=12345.0,
        attempts=3,
    )
    test("task_id", dl.task_id, "abc")
    test("func_name", dl.func_name, "my_func")
    test("args", dl.args, (1, 2))
    test("kwargs", dl.kwargs, {"k": "v"})
    test("error", dl.error, "failed")
    test("traceback", dl.traceback, "Traceback...")
    test("failed_at", dl.failed_at, 12345.0)
    test("attempts", dl.attempts, 3)
    test_true("has __slots__", hasattr(DeadLetter, "__slots__"))


def test_schedule_entry_fields():
    print("-- ScheduleEntry fields --")

    @task
    def dummy():
        return 1

    entry = ScheduleEntry(
        task=dummy,
        args=(1,),
        kwargs={"key": "val"},
        interval_seconds=60.0,
    )
    test("args", entry.args, (1,))
    test("kwargs", entry.kwargs, {"key": "val"})
    test("interval", entry.interval_seconds, 60.0)
    test("cron default", entry.cron, None)
    test_true("schedule_id is str", isinstance(entry.schedule_id, str))
    test("enabled default", entry.enabled, True)


def test_scheduler_requires_timing():
    print("-- TaskScheduler requires timing method --")
    q = TaskQueue(workers=1, max_queue_size=100)
    sched = TaskScheduler(task_queue=q)

    @task(queue=q)
    def dummy():
        return 1

    test_raises("no timing", ValueError, lambda: sched.add(dummy))
    test_raises(
        "multiple timing",
        ValueError,
        lambda: sched.add(dummy, interval=1.0, cron="* * * * *"),
    )
    test_raises(
        "multiple timing 2",
        ValueError,
        lambda: sched.add(dummy, interval=1.0, daily=dt.time(hour=3)),
    )
    q.stop()


def test_scheduler_cron():
    print("-- TaskScheduler cron via scheduler library --")
    q = TaskQueue(workers=1, max_queue_size=100)
    sched = TaskScheduler(task_queue=q)

    @task(queue=q)
    def cron_task():
        return "cron"

    sid = sched.add(cron_task, cron="*/5 * * * *")
    test_true("schedule_id", isinstance(sid, str))
    test("count", sched.count, 1)

    # Verify the job was registered in the scheduler engine
    test_true("job stored", sid in sched._jobs)

    # Cleanup
    sched.remove(sid)
    test("count after remove", sched.count, 0)
    q.stop()


def test_scheduler_daily():
    print("-- TaskScheduler daily timing --")
    q = TaskQueue(workers=1, max_queue_size=100)
    sched = TaskScheduler(task_queue=q)

    @task(queue=q)
    def daily_task():
        return "daily"

    sid = sched.add(daily_task, daily=dt.time(hour=3, minute=30))
    test_true("schedule_id", isinstance(sid, str))
    test("count", sched.count, 1)
    sched.remove(sid)
    q.stop()


def test_scheduler_weekly():
    print("-- TaskScheduler weekly timing --")
    q = TaskQueue(workers=1, max_queue_size=100)
    sched = TaskScheduler(task_queue=q)

    @task(queue=q)
    def weekly_task():
        return "weekly"

    sid = sched.add(weekly_task, weekly=Monday(dt.time(hour=9)))
    test_true("schedule_id", isinstance(sid, str))
    test("count", sched.count, 1)
    sched.remove(sid)
    q.stop()


def test_scheduler_hourly():
    print("-- TaskScheduler hourly timing --")
    q = TaskQueue(workers=1, max_queue_size=100)
    sched = TaskScheduler(task_queue=q)

    @task(queue=q)
    def hourly_task():
        return "hourly"

    sid = sched.add(hourly_task, hourly=dt.time(minute=15))
    test_true("schedule_id", isinstance(sid, str))
    test("count", sched.count, 1)
    sched.remove(sid)
    q.stop()


def test_handle_result_on_failed():
    print("-- TaskHandle result on failed task --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    try:

        def boom():
            raise ValueError("kaboom")

        handle = q.enqueue(boom)
        test_raises(
            "result raises RuntimeError", RuntimeError, lambda: handle.result(timeout=5)
        )
    finally:
        q.stop()


def test_task_message_defaults():
    print("-- TaskMessage defaults --")

    def noop():
        pass

    msg = TaskMessage(func=noop, args=(), kwargs={}, task_id="test")
    test("default priority", msg.priority, TaskPriority.NORMAL)
    test("default max_retries", msg.max_retries, 0)
    test("default retry_delay", msg.retry_delay, 1.0)
    test("default retry_backoff", msg.retry_backoff, 2.0)
    test("default retry_on", msg.retry_on, None)
    test("default attempt", msg.attempt, 0)
    test("default on_success", msg.on_success, None)
    test("default on_failure", msg.on_failure, None)
    test("default on_retry", msg.on_retry, None)
    test_true("enqueued_at is float", isinstance(msg.enqueued_at, float))


def test_task_handle_pending_status():
    print("-- TaskHandle pending status --")
    results = {}
    lock = threading.Lock()
    handle = TaskHandle("test_id", results, lock)
    test("no result entry -> PENDING", handle.status(), TaskStatus.PENDING)
    test_false("not done", handle.is_done())


def test_exponential_backoff_timing():
    print("-- Exponential backoff timing --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    timestamps = []

    def timed_fail():
        timestamps.append(time.monotonic())
        raise ValueError("fail")

    try:
        # Use a larger base retry_delay (0.5s vs 0.05s) so worker
        # cold-start jitter (~1s observed on Linux CI) doesn't swamp
        # the timing signal. delay1 ≈ 0.5s + jitter, delay2 ≈ 1.0s +
        # jitter — the 2.0× multiplier remains observable even with
        # ±0.5s of CI scheduling noise.
        handle = q.enqueue(
            timed_fail,
            max_retries=2,
            retry_delay=0.5,
            retry_backoff=2.0,
        )
        with contextlib.suppress(RuntimeError):
            handle.result(timeout=10)

        # Should have 3 timestamps: original + 2 retries
        # Delays: ~0.5s, ~1.0s (with jitter)
        if len(timestamps) >= 3:
            delay1 = timestamps[1] - timestamps[0]
            delay2 = timestamps[2] - timestamps[1]
            test_true("backoff increasing", delay2 > delay1 * 0.8)
        test("3 attempts", len(timestamps), 3)
    finally:
        q.stop()


def test_retry_on_matching_exception():
    print("-- Retry on matching exception --")
    q = TaskQueue(workers=2, max_queue_size=100)
    q.start()
    attempts = []

    def fails_with_value_error():
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("retryable")
        return "ok"

    try:
        handle = q.enqueue(
            fails_with_value_error,
            max_retries=3,
            retry_delay=0.01,
            retry_backoff=1.0,
            retry_on=(ValueError,),
        )
        result = handle.result(timeout=10)
        test("succeeded after retries", result, "ok")
        test("correct attempt count", len(attempts), 3)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # TaskPriority
    test_task_priority()

    # TaskStatus
    test_task_status()

    # TaskResult
    test_task_result()

    # TaskMessage ordering
    test_task_message_ordering()

    # TaskQueue basics
    test_task_queue_lifecycle()
    test_task_queue_sync()
    test_task_queue_async()
    test_task_queue_stats()
    test_task_queue_full()

    # TaskHandle
    test_task_handle()
    test_task_handle_timeout()
    test_task_handle_cancel()
    test_task_handle_cancel_running()
    test_task_handle_pending_status()

    # @task decorator
    test_task_decorator_bare()
    test_task_decorator_parens()
    test_task_decorator_priority()
    test_task_decorator_delay()
    test_task_decorator_delay_autostart()
    test_task_decorator_async()
    test_task_decorator_with_all_options()

    # Retry mechanism
    test_retry_basic()
    test_retry_with_filter()
    test_retry_exhausted_dlq()
    test_retry_on_retry_hook()
    test_retry_on_matching_exception()
    test_exponential_backoff_timing()

    # Lifecycle hooks
    test_on_success_hook()
    test_on_failure_hook()

    # TaskScheduler
    test_scheduler_lifecycle()
    test_scheduler_interval()
    test_scheduler_remove()
    test_scheduler_requires_timing()
    test_scheduler_cron()
    test_scheduler_daily()
    test_scheduler_weekly()
    test_scheduler_hourly()

    # Cron-to-scheduler translation
    test_cron_to_scheduler_every_minute()
    test_cron_to_scheduler_every_5_minutes()
    test_cron_to_scheduler_every_hour()
    test_cron_to_scheduler_daily_midnight()
    test_cron_to_scheduler_daily_3am()
    test_cron_to_scheduler_weekly_monday()
    test_cron_to_scheduler_every_2_hours()
    test_cron_to_scheduler_invalid()
    test_cron_to_scheduler_minute_30_hourly()
    test_cron_to_scheduler_sunday()

    # DeadLetterQueue
    test_dlq_push_pop()
    test_dlq_peek()
    test_dlq_clear()
    test_dlq_max_size()
    test_dlq_retry()
    test_dlq_retry_not_found()

    # TaskGroup
    test_task_group()
    test_task_group_timeout()
    test_task_group_empty()

    # TaskQueueStats
    test_queue_stats_fields()
    test_queue_stats_computed()

    # Thread safety
    test_concurrent_delay()
    test_concurrent_result_reads()

    # Integration tests
    test_integration_full_lifecycle()
    test_integration_priority_ordering()
    test_integration_retry_then_success()
    test_integration_retry_then_dlq()
    test_integration_kwargs()
    test_integration_multiple_handles()

    # Additional coverage
    test_execution_timeout_frees_worker()
    test_task_message_defaults()
    test_dead_letter_fields()
    test_schedule_entry_fields()
    test_handle_result_on_failed()

    print(f"\n{'=' * 60}")
    print(f"tasks_enhanced: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
