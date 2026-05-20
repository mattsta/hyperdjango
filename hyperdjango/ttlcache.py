"""
Monotonic-clock TTL snapshot cache.

A tiny in-process cache for values that are expensive to build but tolerate a
few seconds of staleness: an identity's authorization grants, a resolved
config bundle, any per-key snapshot fetched behind a hot path. ``get(key,
build)`` returns a cached snapshot or awaits the builder on a miss; the entry
expires ``ttl`` seconds later, bounding how long a change (e.g. a revoked
grant) takes to propagate.

Free-threading (3.14t): a plain dict with whole-entry replacement. A racing
reader sees either the old snapshot or the new one, never a partial entry, and
never a torn ``(expiry, value)`` pair — both are published in one dict
assignment. Two concurrent misses for the same key may both build (there is no
single-flight); each stores a complete, self-consistent snapshot and the last
writer wins, which is harmless for snapshot data.

Invalidation-vs-build safety: a build may take a while, and an ``invalidate()``
can fire while it runs (e.g. a grant is revoked mid-rebuild). Storing the
already-stale snapshot afterwards would silently undo that invalidation for a
full TTL. A monotonically-increasing generation counter — bumped by every
``invalidate()`` — is captured before the build and re-checked before the store;
if it moved, the snapshot is returned to the caller but NOT cached, so the next
``get`` rebuilds against current data. The counter bump, the store decision, and
every map mutation are serialized by one lock so the check-then-store is atomic
under free-threading.

Eviction: expired entries left behind by keys that never come back would leak
memory forever. Stores opportunistically sweep expired entries (throttled to at
most once per TTL window so a hot cache doesn't pay an O(n) scan per store), and
an optional ``max_entries`` hard cap evicts expired-then-oldest entries so a
burst of distinct keys can't grow the map without bound.

The clock is ``time.monotonic()`` so expiry is immune to wall-clock jumps; it is
injectable (``clock=``) so expiry can be exercised by advancing time instead of
waiting for it.
"""

import inspect
import threading
import time
from collections.abc import Awaitable, Callable

from hyperdjango.telemetry.metrics import CounterVec


class TTLCache[K, V]:
    """TTL cache of ``V`` snapshots keyed by a hashable ``K``.

    Optionally records hit/miss to a ``CounterVec``: pass ``counter`` plus the
    label-value tuples to increment on hit and miss (ordered to match the
    counter's declared labels — same contract as ``CounterVec.inc_tuple``).

    ``max_entries`` (optional) caps the number of live entries; when a store
    would exceed it, expired entries are swept first and then the oldest
    first-seen entries are evicted until the map is within the cap.

    Constraints (by design, not bugs):
      - An entry's expiry is stamped from the ``now`` captured BEFORE the build
        runs, so the effective cached lifetime is ``ttl`` minus the build's own
        duration. A builder that takes longer than ``ttl`` produces an entry
        that is already expired on store — it is returned to the caller but the
        next ``get`` always rebuilds (nothing is ever served from cache).
      - The invalidation generation counter is GLOBAL, not per-key: any
        ``invalidate()`` (even for a different key, or the whole map) that fires
        while other builds are in flight suppresses caching of ALL of those
        in-flight results. They are returned to their callers but not stored;
        the next ``get`` for each rebuilds. This is deliberately conservative —
        it never serves data that an invalidation may have staled.
      - Cap eviction is FIRST-SEEN (insertion) order, not LRU: a frequently read
        but early-inserted key is evicted before a rarely read newer one. Entry
        reads do not refresh recency.

    ``clock`` is the time source every expiry decision is read from (default
    ``time.monotonic``). It exists so a caller proving TTL behaviour — "the
    entry is reused inside its TTL and rebuilt after it" — can ADVANCE time
    rather than sleep out a real TTL, which is what makes such a check depend
    on how fast and how loaded the machine is. Production passes nothing.
    """

    def __init__(
        self,
        ttl: float,
        *,
        counter: CounterVec | None = None,
        hit_values: tuple[str, ...] = ("hit",),
        miss_values: tuple[str, ...] = ("miss",),
        max_entries: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        # A non-positive TTL is never valid: every stored entry would be born
        # already-expired (or expire in the past), so the cache would never
        # serve a hit and each get would rebuild. Fail loudly at construction
        # rather than silently degrade to a no-op cache.
        if ttl <= 0:
            raise ValueError(f"ttl must be > 0, got {ttl!r}")
        self._ttl = ttl
        self._clock = clock
        self._counter = counter
        self._hit_values = hit_values
        self._miss_values = miss_values
        self._max_entries = max_entries
        self._entries: dict[K, tuple[float, V]] = {}
        # Bumped by every invalidate(); captured before a build and re-checked
        # before the store so an invalidation that races a build wins.
        self._generation = 0
        # Serializes the generation bump, the store decision, and all map
        # mutations so check-then-store is atomic under free-threading.
        self._lock = threading.Lock()
        # Monotonic time of the last opportunistic expired-sweep — throttles the
        # O(n) scan to at most once per TTL window.
        self._last_sweep = clock()

    def invalidate(self, key: K | None = None) -> None:
        """Drop one key, or (``key=None``) clear the whole map so the next
        ``get`` for every key rebuilds.

        Bumps the generation counter so any build in flight — which may have
        already read now-stale data — will NOT store its result, keeping a
        revocation from being silently undone for up to one TTL."""
        with self._lock:
            self._generation += 1
            if key is None:
                self._entries = {}
            else:
                self._entries.pop(key, None)

    def _sweep_expired(self, now: float) -> None:
        """Drop every entry whose TTL has elapsed. Caller holds ``self._lock``."""
        expired = [k for k, (expiry, _) in self._entries.items() if expiry <= now]
        for k in expired:
            del self._entries[k]

    async def get(self, key: K, build: Callable[[], Awaitable[V] | V]) -> V:
        """Return the cached snapshot for ``key`` if it is still within its TTL,
        otherwise call ``build`` (awaiting it if it returns a coroutine), cache
        the result, and return it.

        If an ``invalidate()`` fires between the miss and the store, the freshly
        built value is returned to this caller but not cached — the next ``get``
        rebuilds against current data."""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and entry[0] > now:
            if self._counter is not None:
                self._counter.inc_tuple(self._hit_values)
            return entry[1]
        if self._counter is not None:
            self._counter.inc_tuple(self._miss_values)
        # Capture the generation BEFORE building so the window fully covers the
        # builder's read of the underlying data.
        with self._lock:
            gen = self._generation
        built = build()
        value: V = await built if inspect.isawaitable(built) else built
        with self._lock:
            if self._generation != gen:
                # An invalidate() fired during the build — the snapshot may be
                # stale. Return it to this caller but do not cache it.
                return value
            # Opportunistic expired-entry sweep, throttled to once per TTL window
            # so a hot cache doesn't pay an O(n) scan on every store.
            if now - self._last_sweep >= self._ttl:
                self._sweep_expired(now)
                self._last_sweep = now
            self._entries[key] = (now + self._ttl, value)
            # Hard cap: if a burst of distinct keys outran the periodic sweep,
            # drop expired entries first, then the oldest first-seen entries.
            if self._max_entries is not None and len(self._entries) > self._max_entries:
                self._sweep_expired(now)
                while len(self._entries) > self._max_entries:
                    oldest = next(iter(self._entries))
                    del self._entries[oldest]
        return value
