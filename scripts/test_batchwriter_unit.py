"""
Unit tests for hyperdjango.batchwriter.BatchWriter.

# hyper-test: unit

Proves:
  - rows flush inline when the batch-size trigger trips
  - rows flush when the interval-since-last-flush trigger trips
  - flush_pending() drains everything buffered (read-your-writes)
  - a transient persist failure re-buffers the whole batch (never per-row
    hammering) so a later flush persists it with no rows lost
  - a persistent persist failure never raises out of record() and keeps the
    rows buffered for retry (never-drop within the bound)
  - the buffer is bounded: when the cap is hit the oldest rows are dropped and
    the newest survive
  - Database.in_transaction() is the single public transaction-state authority
  - THE INVARIANT: while a transaction is open on the connection, a due flush
    is deferred — the rows stay buffered, so a rollback of that doomed
    transaction cannot destroy the batch. They persist once the transaction
    is gone (background flush).
  - install() wires a periodic background flush + a shutdown drain onto the
    framework task scheduler / app shutdown hook, and both actually drain.
"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.batchwriter import BatchWriter  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Recorder:
    """Captures every persisted batch (as a list of the row values)."""

    def __init__(self):
        self.batches: list[list] = []

    async def persist(self, rows: list) -> None:
        self.batches.append(list(rows))

    @property
    def flat(self) -> list:
        return [r for b in self.batches for r in b]


def test_size_trigger():
    print("\n== flush on batch-size trigger ==")
    rec = _Recorder()
    # No transaction ever open here — guard returns False.
    w = BatchWriter(
        rec.persist, flush_batch=3, flush_interval=1e9, in_transaction=lambda: False
    )

    async def go():
        await w.record("a")
        await w.record("b")
        check("no flush before batch fills", rec.batches == [], str(rec.batches))
        check("rows buffered", w.pending_count() == 2)
        await w.record("c")

    _run(go())
    check(
        "flush fired when batch filled",
        rec.batches == [["a", "b", "c"]],
        str(rec.batches),
    )
    check("buffer emptied after flush", w.pending_count() == 0)


def test_interval_trigger():
    print("\n== flush on interval trigger ==")
    rec = _Recorder()
    w = BatchWriter(
        rec.persist,
        flush_batch=10_000,
        flush_interval=0.05,
        in_transaction=lambda: False,
    )

    async def go():
        await w.record("a")
        check("no flush before interval elapses", rec.batches == [])
        # The interval trigger is a genuine elapsed-time requirement, but "has
        # the interval elapsed?" is still a CONDITION — and it is the writer's
        # own clock that decides, so read that instead of sleeping a hand-picked
        # 0.06 and hoping it cleared 0.05. Exact on any machine, and it stays
        # correct if the interval in this test ever changes.
        while time.monotonic() - w._last_flush < w._flush_interval:
            time.sleep(0.005)
        await w.record("b")

    _run(go())
    check(
        "interval trigger flushed buffered rows",
        rec.flat == ["a", "b"],
        str(rec.batches),
    )


def test_flush_pending():
    print("\n== flush_pending drains everything ==")
    rec = _Recorder()
    w = BatchWriter(
        rec.persist,
        flush_batch=10_000,
        flush_interval=1e9,
        in_transaction=lambda: False,
    )

    async def go():
        await w.record("a")
        await w.record("b")
        check("still buffered before flush_pending", rec.batches == [])
        await w.flush_pending()

    _run(go())
    check("flush_pending drained the buffer", rec.flat == ["a", "b"], str(rec.batches))
    check("buffer empty after flush_pending", w.pending_count() == 0)


def test_flush_pending_waits_for_inflight_flush():
    print("\n== flush_pending waits out a flush in flight on another task ==")
    # Read-your-writes regression: a concurrent flusher (inline record()
    # trigger or the periodic task) can swap the batch out and sit mid-INSERT
    # while a reader calls flush_pending. Draining only the (now empty) buffer
    # and returning would let the reader query BEFORE its own rows commit —
    # observed live as an audit row missing the instant after a denied request.
    release = asyncio.Event()
    # Set the instant the flush is genuinely inside persist. Every step below
    # sequences on an observed transition rather than on a sleep long enough to
    # "probably" have got there — on a loaded runner "probably" is where the
    # 1-in-N CI failures come from.
    persist_entered = asyncio.Event()
    persisted: list = []

    async def slow_persist(rows: list) -> None:
        persist_entered.set()
        await release.wait()  # holds the flush in flight until the test says so
        persisted.extend(rows)

    w = BatchWriter(
        slow_persist, flush_batch=1, flush_interval=1e9, in_transaction=lambda: False
    )

    async def go():
        # flush_batch=1 → record() itself starts the flush, which parks inside
        # slow_persist. Run it as a task so flush_pending races it.
        inline_flush = asyncio.ensure_future(w.record("audit-row"))
        await persist_entered.wait()  # the record task IS mid-persist now
        check(
            "buffer already swapped out by the in-flight flush", w.pending_count() == 0
        )

        drain_started = asyncio.Event()

        async def reader_drain():
            drain_started.set()
            await w.flush_pending()
            return list(persisted)

        drain = asyncio.ensure_future(reader_drain())
        await drain_started.wait()  # the reader is now inside flush_pending
        # timing-window: a bounded NEGATIVE — flush_pending must NOT return
        # while the flush it is waiting on is still parked in slow_persist.
        # Nothing becomes true when a task declines to finish, so an observation
        # window is the only available construct. It is overshoot-safe: the
        # persist stays parked until `release.set()` below, so a runner that
        # sleeps far longer than asked only strengthens the claim.
        await asyncio.sleep(0.05)
        check(
            "flush_pending is WAITING, not returned-empty",
            not drain.done(),
            f"done={drain.done()}",
        )
        release.set()
        visible_after_drain = await drain
        await inline_flush
        check(
            "rows committed before flush_pending returned",
            visible_after_drain == ["audit-row"],
            f"visible={visible_after_drain}",
        )

    _run(go())


def test_transient_failure_rebuffers_then_persists():
    print("\n== transient DB blip: batch re-buffered, later flush persists all ==")
    persisted: list = []
    state = {"down": True}

    async def persist(rows: list) -> None:
        # First flush hits a transient outage and fails; once recovered, persist
        # succeeds. Never-drop requires the failed batch to survive to retry.
        if state["down"]:
            raise RuntimeError("simulated transient DB outage")
        persisted.extend(rows)

    w = BatchWriter(
        persist, flush_batch=3, flush_interval=1e9, in_transaction=lambda: False
    )

    async def go():
        await w.record("a")
        await w.record("b")
        await w.record("c")  # trips flush → persist fails → whole batch re-buffered
        check("no rows persisted while DB down", persisted == [], str(persisted))
        check("failed batch re-buffered, not dropped", w.pending_count() == 3)
        # DB recovers; the periodic / read-your-writes flush drains the survivors.
        state["down"] = False
        await w.flush_pending()

    _run(go())
    check(
        "no rows lost across the transient failure",
        persisted == ["a", "b", "c"],
        str(persisted),
    )
    check("buffer empty once persisted", w.pending_count() == 0)


def test_persistent_failure_never_raises_and_keeps_rows():
    print("\n== persistent failure: record never raises; rows kept, not lost ==")

    async def persist(rows: list) -> None:
        raise RuntimeError("persist always fails")

    w = BatchWriter(
        persist, flush_batch=2, flush_interval=1e9, in_transaction=lambda: False
    )
    raised = False

    async def go():
        await w.record("a")
        await w.record("b")  # batch flush fails → whole batch re-buffered, no raise

    try:
        _run(go())
    except Exception:  # noqa: BLE001 - the whole point is that none escapes
        raised = True
    check("record() never raises even when every persist fails", not raised)
    check(
        "rows kept buffered for retry (never-drop within bound)",
        w.pending_count() == 2,
    )


def test_buffer_bound_drops_oldest():
    print("\n== bounded buffer: cap holds, oldest dropped, newest survive ==")
    persisted: list = []
    state = {"down": True}

    async def persist(rows: list) -> None:
        if state["down"]:
            raise RuntimeError("DB down — everything stays buffered")
        persisted.extend(rows)

    # Size/interval triggers never trip (huge batch, huge interval), so rows only
    # accumulate via record() appends and the cap is the sole eviction pressure.
    w = BatchWriter(
        persist,
        flush_batch=10_000,
        flush_interval=1e9,
        in_transaction=lambda: False,
        max_pending=5,
    )

    async def go():
        for i in range(20):
            await w.record(i)
        check("buffer never exceeds the cap", w.pending_count() == 5)
        state["down"] = False
        await w.flush_pending()

    _run(go())
    check(
        "only the cap's worth of newest rows survive (oldest dropped)",
        persisted == [15, 16, 17, 18, 19],
        str(persisted),
    )


def test_database_in_transaction():
    print("\n== Database.in_transaction() is the single public tx authority ==")
    from hyperdjango.database import Database

    # Construct without connecting — in_transaction() reads only per-thread /
    # per-task transaction state, never the pool.
    db = Database("postgres://localhost/hyperdjango_test")
    check("no transaction by default", db.in_transaction() is False)
    db._tx_depth.depth = 1
    check("thread-local depth > 0 ⇒ in transaction", db.in_transaction() is True)
    db._tx_depth.depth = 0
    check("back to no transaction when depth clears", db.in_transaction() is False)


def test_in_transaction_defers_and_survives_rollback():
    print(
        "\n== INVARIANT: open transaction defers the flush; rollback can't destroy rows =="
    )
    rec = _Recorder()
    # Model a request handler running inside `async with db.transaction():`.
    tx_open = {"v": True}
    w = BatchWriter(
        rec.persist,
        flush_batch=3,
        flush_interval=1e9,
        in_transaction=lambda: tx_open["v"],
    )

    async def go():
        # Record a full batch's worth WHILE a transaction is open.
        await w.record("a")
        await w.record("b")
        await w.record("c")  # would trip the size flush — but a tx is open
        # The doomed transaction now rolls back. Because the INSERT was NEVER
        # issued on that connection, there is nothing for the rollback to
        # destroy: the rows are still safely buffered in the writer.
        check(
            "no INSERT issued while transaction open",
            rec.batches == [],
            str(rec.batches),
        )
        check("all rows survive in the buffer", w.pending_count() == 3)
        # Transaction gone (committed/rolled back). Background flush now runs on
        # a clean connection and drains the survivors.
        tx_open["v"] = False
        await w.flush_pending()

    _run(go())
    check(
        "rows persisted after the transaction cleared",
        rec.flat == ["a", "b", "c"],
        str(rec.batches),
    )


class _FakeApp:
    def __init__(self):
        self.startup = []
        self.shutdown = []

    def on_startup(self, f):
        self.startup.append(f)
        return f

    def on_shutdown(self, f):
        self.shutdown.append(f)
        return f


def test_install_shutdown_drain():
    print("\n== install() registers a shutdown drain ==")
    from hyperdjango.tasks import TaskScheduler

    rec = _Recorder()
    w = BatchWriter(
        rec.persist,
        flush_batch=10_000,
        flush_interval=1e9,
        in_transaction=lambda: False,
    )
    app = _FakeApp()
    sched = TaskScheduler()
    # Pass an app-owned scheduler: install must NOT register a startup hook then,
    # but MUST register exactly one shutdown drain and schedule the flush job.
    w.install(app, scheduler=sched)
    check("app-owned scheduler → no startup hook registered", app.startup == [])
    check("one shutdown hook registered", len(app.shutdown) == 1)
    check("periodic flush job scheduled", sched.count == 1)

    async def go():
        await w.record("x")
        await w.record("y")
        # Fire the registered shutdown hook.
        await app.shutdown[0]()

    _run(go())
    check("shutdown hook drained the buffer", rec.flat == ["x", "y"], str(rec.batches))


def test_install_periodic_flush_end_to_end():
    print("\n== install() periodic flush drains on the real scheduler ==")
    from hyperdjango.tasks import TaskScheduler

    rec = _Recorder()
    # Short interval so the scheduler fires quickly; guard False (no DB).
    w = BatchWriter(
        rec.persist,
        flush_batch=10_000,
        flush_interval=0.1,
        in_transaction=lambda: False,
    )
    app = _FakeApp()
    sched = TaskScheduler()
    w.install(app, scheduler=sched)

    async def seed():
        await w.record("p")
        await w.record("q")

    _run(seed())
    sched.start()
    try:
        # Condition-wait for the periodic flush to drain the buffer.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and w.pending_count() != 0:
            time.sleep(0.02)
        check("periodic background flush drained the buffer", w.pending_count() == 0)
        check(
            "periodic flush persisted the rows",
            sorted(rec.flat) == ["p", "q"],
            str(rec.batches),
        )
    finally:
        sched.stop()


def test_install_task_name_visible_to_queue():
    print(
        "\n== install() gives the flush task a writer-specific, queue-visible name =="
    )
    from hyperdjango.tasks import TaskScheduler

    rec = _Recorder()
    w = BatchWriter(
        rec.persist,
        flush_batch=10_000,
        flush_interval=1e9,
        in_transaction=lambda: False,
        name="audit",
    )
    app = _FakeApp()
    sched = TaskScheduler()
    w.install(app, scheduler=sched)

    entries = list(sched._entries.values())
    check("exactly one scheduled entry", len(entries) == 1, str(entries))
    task_decorator = entries[0].task
    # The queue keys its per-function circuit breaker and logs on the INNER
    # function's __name__ (delay() enqueues _func), so THAT must be the
    # writer-specific name — not the generic "_flush_batch_writer".
    check(
        "queue-visible (inner) task name is writer-specific",
        task_decorator._func.__name__ == "flush_audit",
        task_decorator._func.__name__,
    )
    # The decorator's own name (scheduler-side logging) matches too.
    check(
        "decorator name matches the writer",
        task_decorator.__name__ == "flush_audit",
        task_decorator.__name__,
    )


def main() -> bool:
    print("hyperdjango.batchwriter unit tests")
    test_size_trigger()
    test_interval_trigger()
    test_flush_pending()
    test_flush_pending_waits_for_inflight_flush()
    test_transient_failure_rebuffers_then_persists()
    test_persistent_failure_never_raises_and_keeps_rows()
    test_buffer_bound_drops_oldest()
    test_database_in_transaction()
    test_in_transaction_defers_and_survives_rollback()
    test_install_shutdown_drain()
    test_install_periodic_flush_end_to_end()
    test_install_task_name_visible_to_queue()
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
