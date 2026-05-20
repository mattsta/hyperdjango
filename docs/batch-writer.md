# Batch Writer & TTL Cache

Two small in-process primitives for high-throughput services: a batched,
never-drop row writer (`hyperdjango.batchwriter.BatchWriter`) and a
monotonic-clock TTL snapshot cache (`hyperdjango.ttlcache.TTLCache`). Both are
free-threading-safe (Python 3.14t).

## Batch writer

An append-only insert stream — audit trails, access logs, event ledgers —
would otherwise pay one `INSERT` per row. `BatchWriter` buffers rows in memory
and persists them in size- or interval-triggered batches: the common write
pays a list append, and a periodic background flush (or a burst that fills the
batch) pays one multi-row `INSERT`. Rows are never silently dropped — when a
flush fails the **whole batch is re-buffered** (not retried row by row) for a
later flush, with a short inline-retry backoff so a still-down database is not
hammered on every append. The periodic background flush is the retry path.

The persister is caller-supplied and model-agnostic: any
`async def persist(rows) -> None`, typically a model's `bulk_create`.

```python
from hyperdjango.batchwriter import BatchWriter
from .models import AccessLog

async def _persist(rows: list[AccessLog]) -> None:
    await AccessLog.objects.bulk_create(rows)

audit = BatchWriter(_persist, flush_batch=500, flush_interval=0.25, name="audit")

# In a request handler — build the row, hand it over:
await audit.record(AccessLog(identity=who, action="read", outcome="allow", ...))

# Before a read that must see its own writes:
await audit.flush_pending()
```

### Bounded buffer (`max_pending`)

Re-buffering a failed flush keeps rows safe across a transient blip, but a
_persistent_ outage — every flush fails and re-buffers while new rows keep
arriving — could otherwise grow the in-memory buffer without limit. `max_pending`
caps the rows held in memory (default: `flush_batch × 40`). Once the buffer is at
the cap, the **oldest** rows are dropped to admit newer ones, each drop emitting a
loud `ERROR` log. Never-drop therefore holds _within the bound_: rows are lost
only under a sustained outage that overflows the cap, and the loss is the oldest
rows and always logged.

```python
# Hold at most 100k rows in memory before shedding the oldest under a
# prolonged outage.
audit = BatchWriter(_persist, flush_batch=500, max_pending=100_000, name="audit")
```

### The in-transaction invariant

A flush must **never** run on a database connection that is inside an open
transaction. `record()` is called from request handlers, and a handler may be
inside `async with db.transaction():` — if the size trigger flushed inline
there, a later `ROLLBACK` in that same block would silently destroy the entire
buffered batch, including the denial/access rows that must survive _precisely
when_ a request fails.

So `BatchWriter` **defers** any inline flush whenever a transaction is active
on the default database: the rows stay buffered and are drained by the periodic
background flush, which runs on a task-worker thread with its own autocommit
connection where no request transaction is ever open. Detection reads the same
per-thread / per-task transaction state the database layer itself consults, so
it is exact — not a heuristic. `flush_pending()` honors the same guard; a read
endpoint is not normally inside a transaction, so read-your-writes is
unaffected in practice.

### Self-management

`install(app)` wires the writer's own periodic flush onto the framework task
scheduler and its own drain onto the app's shutdown hook, so you never babysit
it:

```python
audit.install(app)  # dedicated scheduler thread, or…
audit.install(app, scheduler=my_sched)  # reuse the app's existing scheduler
```

With `install`, the periodic flush fires every `flush_interval` seconds and the
shutdown hook drains the buffer on the way down. `flush_pending()` remains
available for read-your-writes call sites.

Free-threading: the pending list is guarded by a lock and swapped out
atomically; the persist round-trip runs _outside_ the lock so concurrent
recorders never serialize on database latency, and a racing recorder either
lands in the batch being swapped out or in the fresh list — never a torn half
of one.

## TTL cache

`TTLCache` caches values that are expensive to build but tolerate a few seconds
of staleness — an identity's authorization grants, a resolved config bundle,
any per-key snapshot fetched behind a hot path. `get(key, build)` returns a
cached snapshot or awaits the builder on a miss; the entry expires `ttl`
seconds later, bounding how long a change (e.g. a revoked grant) takes to
propagate.

```python
from hyperdjango.ttlcache import TTLCache

grants: TTLCache[int, CallerGrants] = TTLCache(ttl=15.0)


async def caller_grants(identity) -> CallerGrants:
    return await grants.get(identity.id, lambda: _load_grants(identity))


grants.invalidate(identity.id)  # drop one key on an explicit revocation
grants.invalidate()  # drop everything
```

The builder may be sync or async. The clock is `time.monotonic()`, so expiry is
immune to wall-clock jumps.

**Hit/miss accounting** is optional — pass a `CounterVec` and the label-value
tuples to increment on hit and miss:

```python
from hyperdjango.telemetry.metrics import CounterVec

lookups = CounterVec("grant_cache_total", "Grant-cache lookups.", ("result",))
grants = TTLCache(ttl=15.0, counter=lookups, hit_values=("hit",), miss_values=("miss",))
```

Free-threading: a plain dict with whole-entry replacement. A racing reader sees
either the old snapshot or the new one, never a partial entry. Two concurrent
misses for the same key may both build (there is no single-flight); each stores
a complete, self-consistent snapshot and the last writer wins, which is
harmless for snapshot data.
