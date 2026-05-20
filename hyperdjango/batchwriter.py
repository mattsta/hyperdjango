"""
Batched row writer for high-volume, never-drop inserts.

An append-only stream (audit trails, access logs, event ledgers) that would
pay one INSERT per row instead buffers rows in memory and persists them in
size- or interval-triggered batches. The common write pays a list append; a
periodic background flush (or a burst that fills the batch) pays one multi-row
INSERT. Rows are never silently dropped — on a failed flush the WHOLE batch is
re-buffered (not retried row by row) for a later flush, with a short inline-retry
backoff so a still-down database isn't hammered on every append. The buffer is
bounded by ``max_pending``; only once that cap is exceeded are the OLDEST rows
dropped, with a loud ERROR log — never-drop holds within the bound.

The persister is caller-supplied and model-agnostic: any
``async def persist(rows) -> None`` (typically ``Model.objects.bulk_create``).
``record(row)`` takes an already-built model instance, so the writer knows
nothing about the row's shape.

In-transaction safety (the invariant that motivates the whole design)
---------------------------------------------------------------------
A flush must **never** run on a database connection that is inside an open
transaction. ``record()`` is called from request handlers, and a handler may be
inside ``async with db.transaction():`` — if the size trigger flushed inline
there, a later ``ROLLBACK`` in that same block would silently destroy the entire
buffered batch, including the denial/access rows that must survive precisely
when a request fails. So an inline flush is **deferred** whenever a transaction
is active on the default database: the rows stay buffered and are drained by the
periodic background flush, which runs on a task-worker thread with its own
autocommit connection where no request transaction is ever open. Detection reads
the same per-thread / per-task transaction state the database layer itself
consults (see ``hyperdjango.query_cache`` for the sibling read-only use).

Self-management
---------------
``install(app)`` wires the writer's own periodic flush onto the framework task
scheduler and its own drain onto the app's shutdown hook, so consumers never
babysit it. ``flush_pending()`` remains public for read-your-writes callers that
must see their own buffered rows before a query.
"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable

from hyperdjango.logging import logger

# Sane defaults: a quarter-second bounds staleness for readers while keeping the
# batch large enough that a busy stream amortizes the INSERT cost well.
DEFAULT_FLUSH_INTERVAL = 0.25
DEFAULT_FLUSH_BATCH = 500
# Buffer cap as a multiple of the batch size when the caller doesn't set one.
# Generous enough to ride out a transient DB outage without dropping audit rows,
# bounded enough that a persistent outage can't grow the buffer without limit.
DEFAULT_MAX_PENDING_FACTOR = 40
# flush_pending's wait on flushes in flight on OTHER threads/loops polls the
# counter at this cadence — cheap enough to stay responsive, coarse enough to
# not spin (the wait ends when the concurrent INSERT round-trip completes).
INFLIGHT_POLL_SECONDS = 0.005
# One drain, then at most one more round for rows a FAILED concurrent flush
# re-buffered during the wait. A still-failing database won't be cured by more
# rounds — the periodic flush is the retry path.
READ_YOUR_WRITES_ROUNDS = 2


def _default_db_in_transaction() -> bool:
    """True iff a framework transaction is CURRENTLY open on this thread/task
    for the default database.

    Delegates to the single public ``Database.in_transaction()`` authority so
    there is one definition of "inside a transaction" across the framework. When
    no database is configured, ``get_db()`` raises and there is then no
    transaction a flush could be inside, so "not in a transaction" is the correct
    answer.
    """
    try:
        from hyperdjango.database import get_db

        db = get_db()
    # blind-except: no DB configured ⇒ no active tx; the flush is safe to run.
    except Exception:
        return False
    return db.in_transaction()


class BatchWriter[T]:
    """In-memory batch + trigger-based flush for a stream of model rows.

    Free-threading (3.14t): the pending list is guarded by a lock and swapped
    out atomically; the persist round-trip runs OUTSIDE the lock so concurrent
    recorders never serialize on database latency. A racing recorder either
    lands in the batch being swapped out or in the fresh list — never a torn
    half of one.

    ``persist`` is called with the swapped-out list and must insert every row.
    On failure the whole batch is re-buffered (bounded by ``max_pending``) for a
    later flush rather than hammered row-by-row against the still-down database,
    so a transient DB blip never loses rows. The bound protects against a
    persistent outage: when it is hit, the oldest rows are dropped with a loud
    log — never-drop holds within the bound.
    """

    def __init__(
        self,
        persist: Callable[[list[T]], Awaitable[None]],
        *,
        flush_batch: int = DEFAULT_FLUSH_BATCH,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        name: str = "batch",
        in_transaction: Callable[[], bool] | None = None,
        max_pending: int | None = None,
    ):
        self._persist = persist
        self._flush_batch = flush_batch
        self._flush_interval = flush_interval
        self._name = name
        # Swappable for tests; defaults to the real default-database detector.
        self._in_transaction = in_transaction or _default_db_in_transaction
        self._pending: list[T] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self._scheduler = None  # set by install()
        # Bounded buffer: cap the rows held in memory so a persistent DB outage
        # (every flush fails and re-buffers) can't grow _pending without limit.
        self._max_pending = (
            max_pending
            if max_pending is not None
            else flush_batch * DEFAULT_MAX_PENDING_FACTOR
        )
        # After a failed flush the rows are re-buffered; suppress INLINE
        # record()-triggered re-flushes until this monotonic deadline so a
        # still-down DB isn't hammered on every subsequent append. The periodic
        # background flush (flush_pending) is the throttled retry path and
        # ignores this suppression.
        self._suppress_inline_until = 0.0
        # Batches swapped out and currently inside a persist round-trip
        # (guarded by _lock; incremented in the SAME locked section as the
        # swap). flush_pending must wait for these: a reader's read-your-writes
        # drain that only emptied the buffer would return while a concurrent
        # flush — possibly carrying the reader's own rows — is still mid-INSERT.
        self._inflight = 0

    def _due(self, now: float) -> bool:
        return (
            len(self._pending) >= self._flush_batch
            or now - self._last_flush >= self._flush_interval
        )

    async def record(self, row: T) -> None:
        """Buffer one row; flush inline when the batch or interval trigger trips
        AND no request transaction is open on the default database.

        When a transaction IS open the flush is deferred (the row stays
        buffered): flushing on that connection would let a later rollback
        destroy the whole batch. The periodic background flush installed by
        ``install()`` drains the buffer on a clean connection.
        """
        now = time.monotonic()
        dropped = 0
        with self._lock:
            self._pending.append(row)
            # Enforce the buffer cap on the append path too: if the periodic
            # flush can't drain (DB down) while records keep arriving, drop the
            # oldest rows rather than grow without bound.
            dropped = self._cap_locked()
            due = self._due(now)
        if dropped:
            logger.error(
                "{name} buffer at cap {cap}; dropped {n} oldest buffered rows",
                name=self._name,
                cap=self._max_pending,
                n=dropped,
            )
        if not due:
            return
        # Back off inline re-flush attempts after a recent failure so a still-down
        # DB isn't hammered on every append; the periodic flush retries instead.
        if now < self._suppress_inline_until:
            return
        # A flush is due — pay the transaction-detection cost only now (rare:
        # once per batch or interval), not on every append.
        if self._in_transaction():
            return
        batch: list[T] | None = None
        with self._lock:
            # Re-check under the lock: a concurrent record() may already have
            # swapped the batch out, in which case there is nothing due.
            if self._due(time.monotonic()):
                batch = self._pending
                self._pending = []
                self._last_flush = time.monotonic()
                self._inflight += 1
        if batch:
            try:
                await self._flush(batch)
            finally:
                with self._lock:
                    self._inflight -= 1

    def _cap_locked(self) -> int:
        """Trim ``_pending`` to ``_max_pending`` by dropping the oldest rows.

        Caller holds ``self._lock``. Returns how many rows were dropped so the
        caller can log OUTSIDE the lock.
        """
        excess = len(self._pending) - self._max_pending
        if excess > 0:
            del self._pending[:excess]
            return excess
        return 0

    def _rebuffer(self, rows: list[T]) -> None:
        """Return a failed batch to the front of the buffer for a later flush.

        Prepended so the oldest audit rows are retried first and drain in order.
        Enforces the buffer cap (dropping the oldest, with a loud log) so a
        persistent outage can't grow the buffer without bound.
        """
        with self._lock:
            self._pending[:0] = rows
            dropped = self._cap_locked()
        if dropped:
            logger.error(
                "{name} buffer at cap {cap}; dropped {n} oldest rows on re-buffer",
                name=self._name,
                cap=self._max_pending,
                n=dropped,
            )

    async def flush_pending(self) -> None:
        """Drain everything buffered AND wait out flushes already in flight.
        Called by the periodic flush, at shutdown, and before a read that must
        see its own writes.

        Read-your-writes needs both halves: draining only this buffer would
        return early when a concurrent flusher (an inline record() trigger or
        the periodic task on another thread) swapped the batch out moments ago
        and is still mid-INSERT — the reader would then query the table before
        rows it already caused were committed. So after draining, wait until no
        flush is in flight anywhere; if a FAILED concurrent flush re-buffered
        its rows during that wait, drain once more (bounded — a still-failing
        database is the periodic flush's problem, not the reader's).

        Honors the same in-transaction guard as ``record()``: if called from
        inside an open transaction the rows stay buffered rather than risking a
        rollback destroying them (a read endpoint is not normally inside a
        transaction, so read-your-writes is unaffected in practice). The
        periodic flush and shutdown drain run outside any request transaction,
        so the guard passes and the buffer empties.
        """
        if self._in_transaction():
            return
        for _ in range(READ_YOUR_WRITES_ROUNDS):
            batch: list[T] | None = None
            with self._lock:
                if self._pending:
                    batch = self._pending
                    self._pending = []
                    self._last_flush = time.monotonic()
                    self._inflight += 1
            if batch:
                try:
                    await self._flush(batch)
                finally:
                    with self._lock:
                        self._inflight -= 1
            while True:
                with self._lock:
                    inflight = self._inflight
                if inflight == 0:
                    break
                await asyncio.sleep(INFLIGHT_POLL_SECONDS)
            with self._lock:
                rebuffered = bool(self._pending)
            if not rebuffered:
                return

    async def _flush(self, rows: list[T]) -> None:
        try:
            await self._persist(rows)
        # blind-except: a flush must never fail the request that tripped it. On a
        # transient DB blip the whole batch is re-buffered for a later flush
        # rather than hammered row-by-row against the still-down DB, so rows are
        # not lost. A persistent outage is bounded by _max_pending (see
        # _rebuffer). Inline re-flushes are suppressed briefly so appends don't
        # re-drive the doomed INSERT; the periodic flush is the retry path.
        except Exception as exc:
            self._rebuffer(rows)
            now = time.monotonic()
            # Guard these trigger-state writes with the lock, like every other
            # trigger-state mutation: an unguarded write here races a concurrent
            # record() reading _suppress_inline_until and swapping _last_flush.
            # (_rebuffer already took and released the lock, so acquire fresh.)
            with self._lock:
                self._suppress_inline_until = now + self._flush_interval
                self._last_flush = now
            logger.error(
                "{name} batch flush failed ({err}); re-buffered {n} rows for retry",
                name=self._name,
                err=exc,
                n=len(rows),
            )

    def pending_count(self) -> int:
        """Number of rows currently buffered (test/observability helper)."""
        with self._lock:
            return len(self._pending)

    def install(self, app, *, scheduler=None) -> None:
        """Register the periodic flush and shutdown drain on the app's lifecycle.

        The periodic flush fires every ``flush_interval`` seconds on the
        framework task scheduler. It runs on a task-worker thread with its own
        loop and its own autocommit database connection — no request transaction
        is ever open there, so the flush lands safely and drains any rows that
        ``record()`` deferred because a transaction was active.

        Pass an existing ``scheduler`` (a ``hyperdjango.tasks.TaskScheduler``) to
        reuse the app's scheduler thread; omit it and the writer creates and
        owns a dedicated scheduler, starting it on app startup. Either way the
        shutdown hook stops the owned scheduler (if any) and drains the buffer.
        """
        from hyperdjango.tasks import TaskScheduler
        from hyperdjango.tasks import task as _task

        writer = self

        async def _flush_batch_writer() -> None:
            await writer.flush_pending()

        # Distinct name per writer so the scheduler's logging AND the task
        # queue's per-function circuit breaker / logs don't conflate two writers.
        # TaskDecorator.delay enqueues the INNER function and the queue keys its
        # circuit breaker and logs on ITS __name__ — so the plain function must be
        # renamed BEFORE decorating; renaming the wrapper alone leaves every
        # writer showing as "_flush_batch_writer" queue-side.
        _flush_batch_writer.__name__ = f"flush_{self._name}"
        _flush_batch_writer = _task(_flush_batch_writer)

        owns_scheduler = scheduler is None
        sched = scheduler if scheduler is not None else TaskScheduler()
        sched.add(
            _flush_batch_writer, interval=self._flush_interval, skip_if_running=True
        )
        self._scheduler = sched

        if owns_scheduler:

            @app.on_startup
            async def _start_batch_writer() -> None:
                sched.start()

        @app.on_shutdown
        async def _drain_batch_writer() -> None:
            if owns_scheduler:
                sched.stop()
            await writer.flush_pending()
