"""
DataLoader — batch and deduplicate database lookups.

Prevents N+1 queries by collecting individual load() calls within a batch
window and executing them as a single pipelined query.

Usage:
    from hyperdjango.dataloader import DataLoader

    async def batch_users(keys):
        results = await db.pipeline([
            (f"SELECT * FROM users WHERE id = {k}", []) for k in keys
        ])
        return [r[0] if r else None for r in results]

    loader = DataLoader(batch_fn=batch_users)

    # These are batched into one pipeline:
    user1 = await loader.load(1)
    user2 = await loader.load(2)
    user3 = await loader.load(3)

Observability:
    stats = loader.get_stats()
    # DataLoaderStats(total_loads=N, cache_hits=M, cache_misses=K,
    #                 batch_calls=B, keys_batched=K, max_batch_size=X,
    #                 errors=E)
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from hyperdjango.telemetry import metrics as _tel_metrics

_KT = TypeVar("_KT")
_VT = TypeVar("_VT")

# ── Native telemetry (zero cost when disabled) ──────────────────────────────
#
# Three process-wide series shared across every DataLoader instance:
#   * loads_total{result}    — counts load() calls by hit/miss
#   * batch_dispatches_total — counts every batch_fn invocation
#   * batch_size             — histogram of batch sizes (1, 2, 5, ..., 500)
#
# Per-loader detail still lives in DataLoaderStats. The metrics here are
# the *aggregate* observability surface that gets scraped by Prometheus
# without per-loader instrumentation. Bucket boundaries are tuned for
# the typical N+1 fix shape (small batches dominate, occasional 100+).

_DATALOADER_BATCH_BUCKETS: tuple[float, ...] = (
    1.0,
    2.0,
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    200.0,
    500.0,
    1000.0,
)

_dataloader_loads_total = _tel_metrics.CounterVec(
    "hyperdjango_dataloader_loads_total",
    "DataLoader load() calls by cache result.",
    label_names=("result",),
)
_dataloader_batch_dispatches_total = _tel_metrics.Counter(
    "hyperdjango_dataloader_batch_dispatches_total",
    "Total DataLoader batch_fn invocations.",
)
_dataloader_batch_size = _tel_metrics.Histogram(
    "hyperdjango_dataloader_batch_size",
    "Distribution of DataLoader batch sizes at dispatch.",
    buckets=_DATALOADER_BATCH_BUCKETS,
)


@dataclass(slots=True)
class DataLoaderStats:
    """Observability snapshot for a DataLoader instance.

    Counters:
      total_loads  — load()/load_many() calls received
      cache_hits   — load() calls resolved from internal cache
      cache_misses — load() calls that required a batch dispatch
      batch_calls  — number of batch_fn invocations (chunks count)
      keys_batched — total keys passed across all batch_fn calls
      largest_batch — max keys in a single batch_fn call
      errors       — exceptions raised by batch_fn (counts chunk-level failures)
    """

    total_loads: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    batch_calls: int = 0
    keys_batched: int = 0
    largest_batch: int = 0
    errors: int = 0

    @property
    def hit_rate(self) -> float:
        return self.cache_hits / self.total_loads if self.total_loads else 0.0

    @property
    def avg_batch_size(self) -> float:
        return self.keys_batched / self.batch_calls if self.batch_calls else 0.0


@dataclass
class DataLoader:
    """Batch and deduplicate async lookups.

    Collects load() calls and dispatches them in a single batch via batch_fn.
    Supports caching and max batch size.
    """

    batch_fn: Callable[[list[_KT]], Awaitable[list[_VT]]]
    max_batch_size: int = 100
    cache_enabled: bool = True
    # Batch window (milliseconds). The default 0.0 keeps the original
    # behavior: keys are coalesced only within the CURRENT event-loop tick
    # (``call_soon``), so loads separated by an ``await`` land in different
    # batches. Set > 0 to accumulate across awaits — dispatch is deferred by
    # this many ms (``call_later``), giving interleaved awaits a chance to
    # coalesce into one batch. A batch that fills to ``max_batch_size`` is
    # dispatched immediately regardless of the window.
    batch_window_ms: float = 0.0
    _cache: dict[_KT, _VT] = field(default_factory=dict, init=False, repr=False)
    _pending: dict[_KT, asyncio.Future[_VT]] = field(
        default_factory=dict, init=False, repr=False
    )
    _scheduled: bool = field(default=False, init=False, repr=False)
    _timer_handle: object = field(default=None, init=False, repr=False)
    _stats: DataLoaderStats = field(
        default_factory=DataLoaderStats, init=False, repr=False
    )

    async def load(self, key: _KT) -> _VT:
        """Load a value by key. Batched with other load() calls in the same tick."""
        self._stats.total_loads += 1

        # Check cache first
        if self.cache_enabled and key in self._cache:
            self._stats.cache_hits += 1
            _dataloader_loads_total.inc_tuple(("hit",))
            return self._cache[key]

        # Check if already pending (same key requested twice in same tick)
        if key in self._pending:
            self._stats.cache_hits += 1  # Deduplicated — no extra batch work
            _dataloader_loads_total.inc_tuple(("hit",))
            return await self._pending[key]

        self._stats.cache_misses += 1
        _dataloader_loads_total.inc_tuple(("miss",))

        # Create future for this key
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[key] = future

        # Schedule batch dispatch.
        if not self._scheduled:
            self._scheduled = True
            if self.batch_window_ms > 0:
                # Accumulate across awaits: defer dispatch by the window so
                # loads interleaved with awaits still coalesce into one batch.
                self._timer_handle = loop.call_later(
                    self.batch_window_ms / 1000.0, self._fire_dispatch
                )
            else:
                # Default: coalesce within the current event-loop tick only.
                loop.call_soon(self._fire_dispatch)
        elif (
            self._timer_handle is not None and len(self._pending) >= self.max_batch_size
        ):
            # Window is open but the batch is already full — dispatch now
            # instead of waiting out the remaining window.
            self._timer_handle.cancel()
            self._timer_handle = None
            loop.call_soon(self._fire_dispatch)

        return await future

    def _fire_dispatch(self) -> None:
        """Timer/tick callback → kick off the async batch dispatch."""
        self._timer_handle = None
        asyncio.ensure_future(self._dispatch())

    async def load_many(self, keys: list[_KT]) -> list[_VT]:
        """Load multiple values. All keys batched together."""
        return await asyncio.gather(*(self.load(k) for k in keys))

    def prime(self, key, value):
        """Pre-populate the cache with a known value."""
        if self.cache_enabled:
            self._cache[key] = value

    def clear(self, key=None):
        """Clear cache. If key given, clear only that key."""
        if key is not None:
            self._cache.pop(key, None)
        else:
            self._cache.clear()

    def get_stats(self) -> DataLoaderStats:
        """Return a snapshot of the current stats.

        Returns the live DataLoaderStats instance. Callers should read values
        from the snapshot; do not mutate it. Use reset_stats() to start a
        fresh measurement window.
        """
        return self._stats

    def reset_stats(self) -> None:
        """Reset all stat counters to zero. Useful for per-request measurements."""
        self._stats = DataLoaderStats()

    async def _dispatch(self):
        """Dispatch all pending keys as a single batch."""
        self._scheduled = False

        if not self._pending:
            return

        # Collect pending keys and futures
        batch_keys = list(self._pending.keys())
        batch_futures = {k: self._pending.pop(k) for k in batch_keys}

        # Respect max batch size
        for i in range(0, len(batch_keys), self.max_batch_size):
            chunk_keys = batch_keys[i : i + self.max_batch_size]
            self._stats.batch_calls += 1
            self._stats.keys_batched += len(chunk_keys)
            if len(chunk_keys) > self._stats.largest_batch:
                self._stats.largest_batch = len(chunk_keys)
            # Native telemetry — one Counter bump + one Histogram observe
            # per chunk. The histogram lets dashboards show the dispatched
            # batch-size distribution (a leading indicator of N+1 fixes).
            _dataloader_batch_dispatches_total.inc(1)
            _dataloader_batch_size.observe(float(len(chunk_keys)))

            try:
                results = await self.batch_fn(chunk_keys)

                # Resolve futures. zip() stops at the shorter of the two
                # sequences, so a SHORT results list from batch_fn would leave
                # the tail futures unresolved — and there is no per-load
                # timeout, so those load() awaits would hang forever.
                for key, result in zip(chunk_keys, results):
                    if self.cache_enabled:
                        self._cache[key] = result
                    future = batch_futures[key]
                    if not future.done():
                        future.set_result(result)

                # Fail any key batch_fn did not provide a result for (short /
                # mismatched list). A missing-but-still-pending future is the
                # exact hang condition; reject it with a descriptive error so
                # the waiting load() raises instead of blocking indefinitely.
                leftover = [k for k in chunk_keys if not batch_futures[k].done()]
                if leftover:
                    self._stats.errors += 1
                    err = RuntimeError(
                        f"batch_fn returned too few results: {len(leftover)} of "
                        f"{len(chunk_keys)} keys unresolved "
                        "(batch_fn must return exactly one result per key, in order)"
                    )
                    for key in leftover:
                        batch_futures[key].set_exception(err)

            # blind-except: a batch_fn failure is propagated to every waiting future via set_exception; one batch's error must not kill the loader dispatch loop
            except Exception as exc:
                self._stats.errors += 1
                # Reject all futures in this chunk
                for key in chunk_keys:
                    future = batch_futures[key]
                    if not future.done():
                        future.set_exception(exc)
