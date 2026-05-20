"""
Round-10 task-queue / dataloader / mail robustness regression tests.

Covers the fixes for:

  #1 tasks.TaskHandle.cancel() must decrement the per-user pending count
     EXACTLY ONCE. The worker's CANCELLED branch used to decrement a second
     time, so a user who cancelled a task while OTHER tasks of theirs were
     still pending would under-report pending and bypass
     TASK_MAX_PENDING_PER_USER. After the fix the worker branch owns no
     decrement — cancel() is the sole owner.

  #2 tasks.TaskQueue._re_enqueue() hitting a full queue during a retry must
     write a TERMINAL FAILED result (+ mark_done + user-count decrement),
     mirroring enqueue()'s queue-full path, instead of leaving the task
     RETRYING forever (result() would block) and leaking the user count. The
     cancel-during-retry guard must NOT overwrite an already-terminal result
     or double-decrement.

  #6 tasks worker guard catches BaseException, so a task that raises
     SystemExit/KeyboardInterrupt/CancelledError is recorded FAILED and the
     worker thread survives to process the next task (no pool attrition).

  #7 tasks.TaskQueue.stop(drain=True) drains the already-queued tasks before
     shutting workers down.

  #3 dataloader._dispatch() must fail (not hang) the leftover keys when
     batch_fn returns a SHORT results list — zip() would otherwise leave the
     tail futures unresolved forever (there is no per-load timeout).

  #4 tasks._cron_to_scheduler_timing() accepts cron dow=7 (Sunday) and the
     three-letter day names instead of raising IndexError.

  #8 mail._send_smtp() uses a VERIFYING ssl context for STARTTLS (was
     context=None → unverified → MITM).

Pure-Python: no native rebuild needed. Run:

    uv run python scripts/test_tasks_robustness_r10.py
"""

# hyper-test: unit

import asyncio
import ssl
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperdjango import mail
from hyperdjango.dataloader import DataLoader
from hyperdjango.mail import EmailMessage, MailConfig
from hyperdjango.tasks import (
    Sunday,
    TaskHandle,
    TaskMessage,
    TaskPriority,
    TaskQueue,
    TaskResult,
    TaskStatus,
    _cron_to_scheduler_timing,
)
from hyperdjango.testkit import check, finish, run_main

# ---------------------------------------------------------------------------
# #1 cancel-while-pending decrements the per-user count EXACTLY once
# ---------------------------------------------------------------------------


def test_cancel_decrements_once() -> None:
    print("\n#1 cancel decrements per-user pending exactly once")
    q = TaskQueue(workers=1, max_queue_size=100)
    q._max_pending_per_user = 100
    q.start()
    try:
        e1 = threading.Event()  # release the blocker
        blk_started = threading.Event()

        def blocker() -> None:
            blk_started.set()
            e1.wait(5)

        q.enqueue(blocker, user_id="blk")
        assert blk_started.wait(5), "blocker never started"
        # The single worker is now busy → A and B queue up behind it.

        e2 = threading.Event()
        b_started = threading.Event()

        def task_b() -> None:
            b_started.set()
            e2.wait(5)

        def task_a() -> None:  # cancelled before it ever runs
            pass

        # HIGH so the cancelled A is dequeued BEFORE B once the worker frees up.
        h_a = q.enqueue(task_a, priority=TaskPriority.HIGH, user_id="u1")
        q.enqueue(task_b, priority=TaskPriority.NORMAL, user_id="u1")

        check("two pending before cancel", q.get_user_pending("u1") == 2)
        check("cancel() returns True while pending", h_a.cancel() is True)
        check(
            "count == 1 immediately after cancel (single decrement)",
            q.get_user_pending("u1") == 1,
        )

        # Free the worker: it processes A (cancelled → must NOT decrement) then
        # starts B (blocks). If the worker double-decremented on A, the count
        # would drop to 0 even though B is still pending.
        e1.set()
        assert b_started.wait(5), "task_b never started"
        check(
            "count stays 1 after cancelled task processed (no double decrement)",
            q.get_user_pending("u1") == 1,
        )

        e2.set()  # let B finish
        deadline = threading.Event()
        for _ in range(500):
            if q.get_user_pending("u1") == 0:
                deadline.set()
                break
            threading.Event().wait(0.01)
        check("count returns to 0 after B completes", deadline.is_set())
    finally:
        q.stop(drain=False)


# ---------------------------------------------------------------------------
# #2 retry-on-queue-full → terminal FAILED (result() must not hang)
# ---------------------------------------------------------------------------


def test_re_enqueue_queue_full_is_terminal() -> None:
    print("\n#2 _re_enqueue on a full queue writes terminal FAILED")
    q = TaskQueue(workers=1, max_queue_size=1)
    q._max_pending_per_user = 100

    # Fill the queue so put_nowait raises queue.Full.
    def _filler() -> None:
        pass

    q._queue.put_nowait(TaskMessage(func=_filler, args=(), kwargs={}, task_id="filler"))

    tid = "retry-task"
    handle = TaskHandle(tid, q._results, q._results_lock, queue=q, user_id="u9")
    with q._results_lock:
        q._results[tid] = TaskResult(task_id=tid, status=TaskStatus.RETRYING)
        q._handles[tid] = handle
    q._user_pending["u9"] = 1  # simulate the original enqueue's increment

    retry_msg = TaskMessage(
        func=_filler, args=(), kwargs={}, task_id=tid, user_id="u9", attempt=1
    )
    q._re_enqueue(retry_msg)

    tr = q._results[tid]
    check("status is FAILED (was RETRYING)", tr.status == TaskStatus.FAILED)
    check(
        "error message mentions the full queue",
        (tr.error or "").startswith("Queue full"),
    )
    check("per-user count released", q.get_user_pending("u9") == 0)

    # result() must NOT hang — the task is terminal.
    raised = False
    try:
        handle.result(timeout=1.0)
    except RuntimeError:
        raised = True
    except TimeoutError:
        raised = False
    check("result() raises RuntimeError immediately (no hang)", raised)

    # Cancel-during-retry guard: an already-terminal (CANCELLED) result must
    # not be overwritten and must not double-decrement.
    tid2 = "cancelled-retry"
    h2 = TaskHandle(tid2, q._results, q._results_lock, queue=q, user_id="u8")
    with q._results_lock:
        q._results[tid2] = TaskResult(task_id=tid2, status=TaskStatus.CANCELLED)
        q._handles[tid2] = h2
    # cancel() already released the slot → count is 0 for u8.
    q._re_enqueue(
        TaskMessage(
            func=_filler, args=(), kwargs={}, task_id=tid2, user_id="u8", attempt=1
        )
    )
    check(
        "already-CANCELLED retry stays CANCELLED (not overwritten)",
        q._results[tid2].status == TaskStatus.CANCELLED,
    )
    check("no double-decrement for cancelled retry", q.get_user_pending("u8") == 0)


# ---------------------------------------------------------------------------
# #6 worker survives a task that raises BaseException (SystemExit)
# ---------------------------------------------------------------------------


def test_worker_survives_baseexception() -> None:
    print("\n#6 worker survives a BaseException-raising task")
    q = TaskQueue(workers=1, max_queue_size=100)
    q.start()
    try:

        def poison() -> None:
            raise SystemExit("boom")

        h_poison = q.enqueue(poison)
        try:
            h_poison.result(timeout=5)
        except RuntimeError:
            pass
        except TimeoutError:
            pass
        check("poison task recorded FAILED", h_poison.status() == TaskStatus.FAILED)

        def ok() -> int:
            return 42

        h_ok = q.enqueue(ok)
        result = None
        try:
            result = h_ok.result(timeout=5)
        except TimeoutError:
            result = None
        check("worker still alive; next task runs to SUCCESS", result == 42)
    finally:
        q.stop(drain=False)


# ---------------------------------------------------------------------------
# #7 stop(drain=True) drains queued tasks
# ---------------------------------------------------------------------------


def test_stop_drains() -> None:
    print("\n#7 stop(drain=True) drains queued tasks")
    q = TaskQueue(workers=2, max_queue_size=1000)
    q.start()
    done: list[int] = []
    done_lock = threading.Lock()

    def worker_task(n: int) -> None:
        with done_lock:
            done.append(n)

    for i in range(50):
        q.enqueue(worker_task, i)
    q.stop(drain=True)
    check(f"all 50 queued tasks processed on drain (got {len(done)})", len(done) == 50)


# ---------------------------------------------------------------------------
# #3 dataloader short batch_fn rejects leftover keys (no hang)
# ---------------------------------------------------------------------------


def test_dataloader_short_batch() -> None:
    print("\n#3 dataloader short batch_fn fails leftover keys (no hang)")

    async def scenario() -> list[object]:
        async def short_batch(keys: list[int]) -> list[str]:
            # Only ONE result for N keys.
            return [f"v{keys[0]}"]

        loader = DataLoader(batch_fn=short_batch, max_batch_size=100)
        return await asyncio.gather(
            loader.load(1),
            loader.load(2),
            loader.load(3),
            return_exceptions=True,
        ), loader

    async def runner() -> None:
        (results, loader) = await asyncio.wait_for(scenario(), timeout=3.0)
        check("first key resolves to its result", results[0] == "v1")
        check(
            "second (leftover) key rejected with descriptive error",
            isinstance(results[1], RuntimeError)
            and "too few results" in str(results[1]),
        )
        check("third (leftover) key rejected", isinstance(results[2], RuntimeError))
        check("one batch error recorded", loader.get_stats().errors == 1)

    try:
        asyncio.run(runner())
    except TimeoutError:
        check("dataloader hung on short batch_fn (leftover futures unresolved)", False)


# ---------------------------------------------------------------------------
# #4 cron dow=7 (and names) schedule instead of raising IndexError
# ---------------------------------------------------------------------------


def test_cron_dow_seven() -> None:
    print("\n#4 cron dow=7 / day-names schedule as weekly Sunday")
    m7, a7 = _cron_to_scheduler_timing("30 9 * * 7")
    m0, a0 = _cron_to_scheduler_timing("30 9 * * 0")
    check("dow=7 maps to weekly (no IndexError)", m7 == "weekly")
    check("dow=0 maps to weekly", m0 == "weekly")
    check(
        "dow=7 and dow=0 both resolve to Sunday",
        type(a7) is type(a0) and isinstance(a7, Sunday),
    )
    m_name, _ = _cron_to_scheduler_timing("30 9 * * sun")
    check("day name 'sun' maps to weekly", m_name == "weekly")
    m_mon, a_mon = _cron_to_scheduler_timing("0 3 * * mon")
    check("day name 'mon' works", m_mon == "weekly" and not isinstance(a_mon, Sunday))
    # Out-of-range dow must not crash — falls through to the safe fallback.
    m_bad, _ = _cron_to_scheduler_timing("30 9 * * 8")
    check("dow=8 falls through, no crash", m_bad in {"weekly", "daily", "cyclic"})


# ---------------------------------------------------------------------------
# #8 STARTTLS uses a verifying ssl context
# ---------------------------------------------------------------------------


def test_starttls_verifying_context() -> None:
    print("\n#8 STARTTLS uses a verifying ssl context")

    captured: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: object = None) -> None:
            pass

        def starttls(self, context: object = None) -> None:
            captured["ctx"] = context

        def login(self, user: str, password: str) -> None:
            pass

        def sendmail(self, from_addr: str, to: object, body: str) -> None:
            captured["sent"] = True

        def quit(self) -> None:
            pass

    orig = mail.smtplib.SMTP
    mail.smtplib.SMTP = FakeSMTP
    try:
        config = MailConfig(
            host="smtp.example.com",
            port=587,
            username="",
            password="",
            use_tls=True,
            use_ssl=False,
            timeout=5,
        )
        msg = EmailMessage(subject="hi", body="body", recipients=["a@example.com"])
        ok = msg._send_smtp(config, "from@example.com")
        check("_send_smtp returned True", ok is True)
        ctx = captured.get("ctx")
        check("starttls received an SSLContext", isinstance(ctx, ssl.SSLContext))
        check(
            "context verifies the peer certificate (CERT_REQUIRED)",
            isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED,
        )
        check(
            "context checks the hostname",
            isinstance(ctx, ssl.SSLContext) and ctx.check_hostname is True,
        )
    finally:
        mail.smtplib.SMTP = orig


# ---------------------------------------------------------------------------
# #9 A TaskHandle held past result-eviction must still read its own result.
#     TaskQueue._maybe_cleanup evicts old terminal entries from the shared
#     results dict to bound memory (TASK_MAX_COMPLETED_RESULTS). A caller can
#     hold a handle long after that eviction. Previously result()/status() read
#     ONLY the shared dict, so an evicted-but-succeeded task's result() blocked
#     forever (default timeout=None) — the completion Event was never set for a
#     result that vanished — and cancel() saw None and wrongly marked the
#     already-succeeded task CANCELLED (double-decrementing the user count). The
#     fix caches the terminal result on the handle in mark_done().
# ---------------------------------------------------------------------------


def test_result_survives_eviction() -> None:
    print("\n#9 held handle still reads its result after cache eviction")
    q = TaskQueue(workers=2, max_queue_size=2000)
    q._max_completed_results = 20  # tiny cap so eviction fires quickly
    q._cleanup_interval = 1  # check on every completion
    q.start()
    try:

        def echo(x: object) -> object:
            return x

        # Enqueue and finish the task we will hold onto.
        keep = q.enqueue(echo, "keepme")
        check("held task completed", keep.result(timeout=5) == "keepme")

        # Flood many more completions to evict the old terminal entry.
        for h in [q.enqueue(echo, i) for i in range(300)]:
            # Reading each: before the fix, evicted ones raised TimeoutError.
            h.result(timeout=5)

        evicted = q._results.get(keep.task_id) is None
        check("old result was evicted from the shared dict", evicted)

        # The held handle must STILL resolve — from its cached snapshot.
        check("evicted handle result() works", keep.result(timeout=3) == "keepme")
        check("evicted handle status SUCCESS", keep.status() == TaskStatus.SUCCESS)
        # And cancel() must NOT treat an evicted-success as pending.
        check("evicted-success handle is not cancellable", keep.cancel() is False)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# #10 A task cancelled WHILE RETRYING must NOT run even if its CANCELLED marker
#     was LRU-evicted before the retry Timer fired. The worker's cancel-check
#     reads the (capacity-bounded) results dict directly; once the CANCELLED
#     entry is evicted it saw None and ran the cancelled task anyway, also
#     double-releasing the per-user slot (→ under-count → TASK_MAX_PENDING_PER_USER
#     bypass). Fix records cancelled-while-retrying ids in an eviction-immune set.
# ---------------------------------------------------------------------------


def test_cancelled_retry_survives_eviction() -> None:
    print("\n#10 cancelled-while-retrying task skipped even after result eviction")
    q = TaskQueue(workers=1, max_queue_size=100)
    q.start()
    try:
        ran = threading.Event()

        def job() -> str:
            ran.set()
            return "done"

        tid = "r10_cancel_retry"
        # Put the task into RETRYING with a registered handle, and give the user
        # other in-flight work so a spurious decrement is observable.
        with q._results_lock:
            q._results[tid] = TaskResult(task_id=tid, status=TaskStatus.RETRYING)
            h = TaskHandle(tid, q._results, q._results_lock, queue=q, user_id="u1")
            q._handles[tid] = h
        with q._user_pending_lock:
            q._user_pending["u1"] = 3

        check("RETRYING task is cancellable", h.cancel() is True)
        check("cancelled-retry id registered", tid in q._cancelled_retries)
        check("cancel decremented pending once (3→2)", q.get_user_pending("u1") == 2)

        # Evict the CANCELLED marker (simulates _maybe_cleanup during the delay).
        with q._results_lock:
            q._results.pop(tid, None)

        # The retry Timer fires: the message lands on the queue.
        q._queue.put_nowait(
            TaskMessage(
                func=job,
                args=(),
                kwargs={},
                task_id=tid,
                priority=TaskPriority.NORMAL,
                user_id="u1",
                attempt=1,
            )
        )
        # Give the worker time to dequeue and skip it.
        for _ in range(50):
            if tid not in q._cancelled_retries:
                break
            time.sleep(0.02)

        check("cancelled task did NOT run after eviction", not ran.is_set())
        check("no double-decrement (pending still 2)", q.get_user_pending("u1") == 2)
        check("cancelled-retry id cleaned up", tid not in q._cancelled_retries)
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# #11 Each task runs in an ISOLATED ContextVar context. A worker thread is
#     reused across tasks (and tenants); if a task sets a ContextVar (tenant,
#     locale, request-id, DB routing) the NEXT task on that thread must NOT
#     observe it — otherwise a task queue is a cross-tenant data-leak vector.
# ---------------------------------------------------------------------------


def test_task_contextvar_isolation() -> None:
    print("\n#11 tasks run in isolated ContextVar contexts (no cross-task bleed)")
    import contextvars

    probe: contextvars.ContextVar = contextvars.ContextVar("r10_probe", default=None)
    q = TaskQueue(workers=1, max_queue_size=100)  # single worker → thread reuse
    q.start()
    try:
        seen: list = []

        def sync_set() -> str:
            probe.set("tenantA")  # set WITHOUT reset
            return "set"

        def sync_check() -> str:
            seen.append(probe.get())
            return "ok"

        async def async_set() -> str:
            probe.set("tenantB")
            return "set"

        async def async_check() -> str:
            seen.append(probe.get())
            return "ok"

        # sync → sync
        q.enqueue(sync_set).result(timeout=5)
        q.enqueue(sync_check).result(timeout=5)
        # async → async
        q.enqueue(async_set).result(timeout=5)
        q.enqueue(async_check).result(timeout=5)
        # timed sync (executor path) → sync
        q.enqueue(sync_set, timeout=5).result(timeout=5)
        q.enqueue(sync_check).result(timeout=5)

        check(
            f"no ContextVar bleed across tasks (saw {seen!r})",
            seen == [None, None, None],
        )
    finally:
        q.stop()


def main() -> bool:
    test_cancel_decrements_once()
    test_re_enqueue_queue_full_is_terminal()
    test_worker_survives_baseexception()
    test_stop_drains()
    test_dataloader_short_batch()
    test_cron_dow_seven()
    test_starttls_verifying_context()
    test_result_survives_eviction()
    test_cancelled_retry_survives_eviction()
    test_task_contextvar_isolation()

    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
