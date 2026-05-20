"""
Cache framework with pluggable backends.

Backends:
- LocMemCache: in-process dict with LRU eviction (single-server, fast)
- DatabaseCache: PostgreSQL UNLOGGED table (multi-server, persistent across restarts)

PostgreSQL UNLOGGED tables skip WAL writes and provide
fast ephemeral storage with multi-server coordination via shared DB.

Usage:
    from hyperdjango.cache import LocMemCache, DatabaseCache, cached

    # In-memory (development)
    cache = LocMemCache(max_size=1000)

    # PostgreSQL UNLOGGED (production, multi-server)
    cache = DatabaseCache(db)
    await cache.ensure_table()

    # Key-value API
    await cache.set("user:42", {"name": "Alice"}, ttl=300)
    user = await cache.get("user:42")
    await cache.delete("user:42")

    # Decorator
    @cached(ttl=60)
    async def get_expensive_data(user_id):
        ...
"""

import contextlib
import functools
import hashlib
import inspect
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from sortedcontainers import SortedList

from hyperdjango.conf import get_setting
from hyperdjango.conf import (
    register_settings_changed_hook as _register_settings_changed_hook,
)
from hyperdjango.keybuilder import injective_join
from hyperdjango.native import fast_json_dumps
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.types import JSONValue

# ── Native telemetry metrics (P5.2) ────────────────────────────────────────

_cache_ops = _tel_metrics.CounterVec(
    "hyperdjango_cache_operations_total",
    "Total cache operations by backend and result",
    label_names=("backend", "result"),
)

# Sentinel for distinguishing None cache values from cache misses
_CACHE_MISS = object()

# ─── LocMemCache ───────────────────────────────────────────────────────────────


class LocMemCache:
    """In-process LRU cache. Fast, single-server only.

    Thread-safe via internal lock. Required because OrderedDict.move_to_end()
    and SortedList operations are NOT atomic under Python 3.14t free-threading.
    Proven by test_free_threading_stress.py: SortedList.discard() crashes
    with IndexError under 24-thread concurrent access without lock.

    Uses SortedList for O(log n) expiry cleanup instead of full-dict scan.

    ``clock`` is the time source every TTL decision is read from (default
    ``time.time``). It exists so a caller that needs to prove TTL behaviour —
    "this entry is a hit before its TTL and a miss after it", including through
    the tiers of a ``TwoTierCache`` or a ``QueryCacheManager`` — can ADVANCE
    time instead of waiting out a real one. Waiting is what makes such a check
    depend on how fast (and how loaded) the machine is: a runner that oversleeps
    expires an entry the test still expects to be live. Production passes
    nothing and gets the wall clock.
    """

    def __init__(
        self,
        max_size: int | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self._lock = threading.Lock()
        self._clock = clock
        self._cache: OrderedDict[str, tuple[JSONValue, float]] = OrderedDict()
        # Sorted by expiry time — bisect to find all expired in O(log n)
        self._expiry_index: SortedList = SortedList(key=lambda x: x[0])
        if max_size is not None:
            self.max_size = max_size
        else:
            max_bytes = get_setting("CACHE_MAX_BYTES")
            # Estimate ~1 KB per entry as a rough heuristic
            self.max_size = max(max_bytes // 1024, 1)
        self._is_async = False

    def get(self, key: str, default: JSONValue = None) -> JSONValue:
        """Get a value by key. Returns default if missing or expired."""
        full_key = make_cache_key(key)
        # Classify the outcome inside the lock; emit the metric
        # after releasing so the Prometheus FFI never runs under
        # the cache lock. Eliminates a subtle contention source
        # on cache-heavy workloads.
        outcome = "miss"
        result = default
        with self._lock:
            entry = self._cache.get(full_key)
            if entry is not None:
                value, expires_at = entry
                if expires_at > 0 and self._clock() > expires_at:
                    self._remove(full_key, expires_at)
                    outcome = "expired"
                else:
                    self._cache.move_to_end(full_key)
                    outcome = "hit"
                    result = value
        _cache_ops.inc_tuple(("locmem", outcome))
        return result

    def set(self, key: str, value: JSONValue, ttl: int | None = None):
        """Set a value with optional TTL in seconds."""
        full_key = make_cache_key(key)
        expires_at = self._clock() + ttl if ttl is not None else 0
        with self._lock:
            old = self._cache.pop(full_key, None)
            if old is not None and old[1] > 0:
                self._expiry_index.discard((old[1], full_key))
            self._cache[full_key] = (value, expires_at)
            if expires_at > 0:
                self._expiry_index.add((expires_at, full_key))
            while len(self._cache) > self.max_size:
                evicted_key, (_, evicted_exp) = self._cache.popitem(last=False)
                if evicted_exp > 0:
                    self._expiry_index.discard((evicted_exp, evicted_key))

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        full_key = make_cache_key(key)
        with self._lock:
            entry = self._cache.pop(full_key, None)
            if entry is None:
                return False
            if entry[1] > 0:
                self._expiry_index.discard((entry[1], full_key))
            return True

    def clear(self):
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            self._expiry_index.clear()

    def has(self, key: str) -> bool:
        """Check if a key exists and is not expired."""
        full_key = make_cache_key(key)
        with self._lock:
            entry = self._cache.get(full_key)
            if entry is None:
                return False
            _, expires_at = entry
            if expires_at > 0 and self._clock() > expires_at:
                self._remove(full_key, expires_at)
                return False
            return True

    def count(self) -> int:
        """Count non-expired entries."""
        with self._lock:
            self._cleanup()
            return len(self._cache)

    def _remove(self, key: str, expires_at: float):
        """Remove a key from both cache and expiry index. Caller must hold _lock."""
        del self._cache[key]
        if expires_at > 0:
            self._expiry_index.discard((expires_at, key))

    def _cleanup(self):
        """Remove expired entries. Caller must hold _lock."""
        now = self._clock()
        while self._expiry_index and self._expiry_index[0][0] <= now:
            expires_at, key = self._expiry_index.pop(0)
            entry = self._cache.get(key)
            if entry is not None and entry[1] == expires_at:
                del self._cache[key]

    def get_or_set(self, key: str, default_func, ttl: int | None = None) -> JSONValue:
        """Get value or compute and set it if missing.

        Correctly handles None values — uses sentinel to distinguish miss from stored None.
        """
        value = self.get(key, default=_CACHE_MISS)
        if value is not _CACHE_MISS:
            return value
        value = default_func()
        self.set(key, value, ttl)
        return value


# ─── DatabaseCache ─────────────────────────────────────────────────────────────

# UNLOGGED = no WAL writes, fast ephemeral storage
# Two value columns: JSONB for structured data, BIGINT for atomic counters
CREATE_CACHE_TABLE_SQL = """
CREATE UNLOGGED TABLE IF NOT EXISTS hyper_cache (
    key TEXT PRIMARY KEY,
    value JSONB,
    counter BIGINT,
    expires_at TIMESTAMPTZ NOT NULL
)
"""

CREATE_CACHE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_cache_expires ON hyper_cache (expires_at)",
)


class DatabaseCache:
    """PostgreSQL UNLOGGED table cache.

    Multi-server coordination via shared PostgreSQL.
    UNLOGGED tables skip WAL writes for fast ephemeral data.

    Two-column value design:
    - value (JSONB): structured data — pg.zig returns native Python objects on read
    - counter (BIGINT): atomic integer counters — clean SQL arithmetic, no casting
    """

    def __init__(self, db, default_ttl: int | None = None):
        self.db = db
        self.default_ttl = (
            default_ttl if default_ttl is not None else get_setting("CACHE_TTL")
        )
        self._is_async = True

    async def ensure_table(self):
        """Create the cache UNLOGGED table if it doesn't exist."""
        try:
            await self.db.execute(CREATE_CACHE_TABLE_SQL)
        # blind-except: UNLOGGED is unsupported on some PG deployments (replicas, certain managed services); fall back to a plain TABLE. A real connection/permission error resurfaces on the fallback execute.
        except Exception:
            await self.db.execute(
                CREATE_CACHE_TABLE_SQL.replace("UNLOGGED TABLE", "TABLE")
            )
        for sql in CREATE_CACHE_INDEX_SQL:
            with contextlib.suppress(Exception):
                await self.db.execute(sql)

    async def get(self, key: str, default: JSONValue = None) -> JSONValue:
        """Get a value by key. Returns default if missing or expired."""
        full_key = make_cache_key(key)
        row = await self.db.query_one(
            "SELECT value, counter FROM hyper_cache WHERE key = $1 AND expires_at > NOW()",
            full_key,
        )
        if row is None:
            _cache_ops.inc_tuple(("database", "miss"))
            return default
        _cache_ops.inc_tuple(("database", "hit"))
        val = row["value"] if isinstance(row, dict) else row[0]
        ctr = row["counter"] if isinstance(row, dict) else row[1]
        # counter column takes priority when set (from incr)
        if ctr is not None:
            return ctr
        return val

    async def set(self, key: str, value: JSONValue, ttl: int | None = None):
        """Set a value with optional TTL in seconds."""
        full_key = make_cache_key(key)
        ttl = ttl if ttl is not None else self.default_ttl
        json_val = fast_json_dumps(value)
        if isinstance(json_val, bytes):
            json_val = json_val.decode("utf-8")
        await self.db.execute(
            "INSERT INTO hyper_cache (key, value, counter, expires_at) "
            "VALUES ($1, $2, NULL, NOW() + $3 * INTERVAL '1 second') "
            "ON CONFLICT (key) DO UPDATE SET value = $2, counter = NULL, "
            "expires_at = NOW() + $3 * INTERVAL '1 second'",
            full_key,
            json_val,
            int(ttl),
        )

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        full_key = make_cache_key(key)
        # execute() returns the affected-row count: non-zero iff the key existed.
        return (
            await self.db.execute("DELETE FROM hyper_cache WHERE key = $1", full_key)
            != 0
        )

    async def clear(self):
        """Clear all cached entries."""
        await self.db.execute("DELETE FROM hyper_cache")

    async def has(self, key: str) -> bool:
        """Check if a key exists and is not expired."""
        full_key = make_cache_key(key)
        row = await self.db.query_val(
            "SELECT 1 FROM hyper_cache WHERE key = $1 AND expires_at > NOW()",
            full_key,
        )
        return row is not None

    async def count(self) -> int:
        """Count non-expired entries."""
        return await self.db.query_val(
            "SELECT COUNT(*) FROM hyper_cache WHERE expires_at > NOW()"
        )

    async def cleanup(self):
        """Delete all expired entries. Call periodically."""
        await self.db.execute("DELETE FROM hyper_cache WHERE expires_at < NOW()")

    async def get_or_set(
        self, key: str, default_func, ttl: int | None = None
    ) -> JSONValue:
        """Get value or compute and set it if missing.

        Race-safe: on cache miss, uses INSERT...ON CONFLICT...RETURNING to
        atomically store the computed value. If another connection inserts
        between the SELECT and INSERT, the existing unexpired value wins
        (no unnecessary overwrite).
        """
        full_key = make_cache_key(key)
        ttl = ttl if ttl is not None else self.default_ttl

        # Phase 1: check cache
        row = await self.db.query_one(
            "SELECT value, counter FROM hyper_cache "
            "WHERE key = $1 AND expires_at > NOW()",
            full_key,
        )
        if row is not None:
            ctr = row["counter"] if isinstance(row, dict) else row[1]
            if ctr is not None:
                return ctr
            return row["value"] if isinstance(row, dict) else row[0]

        # Phase 2: compute and store atomically
        value = default_func()
        json_val = fast_json_dumps(value)
        if isinstance(json_val, bytes):
            json_val = json_val.decode("utf-8")

        # INSERT...ON CONFLICT with expiry-aware update + RETURNING.
        # If another connection inserted a valid value between Phase 1 and 2,
        # we preserve their value (CASE WHEN expires_at > NOW()).
        row = await self.db.query_one(
            "INSERT INTO hyper_cache (key, value, counter, expires_at) "
            "VALUES ($1, $2, NULL, NOW() + $3 * INTERVAL '1 second') "
            "ON CONFLICT (key) DO UPDATE SET "
            "  value = CASE WHEN hyper_cache.expires_at <= NOW() "
            "    THEN EXCLUDED.value ELSE hyper_cache.value END, "
            "  counter = CASE WHEN hyper_cache.expires_at <= NOW() "
            "    THEN NULL ELSE hyper_cache.counter END, "
            "  expires_at = CASE WHEN hyper_cache.expires_at <= NOW() "
            "    THEN EXCLUDED.expires_at ELSE hyper_cache.expires_at END "
            "RETURNING value, counter",
            full_key,
            json_val,
            int(ttl),
        )
        if row:
            ctr = row["counter"] if isinstance(row, dict) else row[1]
            if ctr is not None:
                return ctr
            # The RETURNING value is JSONB — if another thread won the race,
            # we get their value. Parse it back since we can't distinguish.
            stored = row["value"] if isinstance(row, dict) else row[0]
            return stored
        return value

    async def get_many(self, keys: list[str]) -> dict[str, JSONValue]:
        """Get multiple values by keys."""
        if not keys:
            return {}
        full_keys = [make_cache_key(k) for k in keys]
        # Reverse map: full_key -> original key
        reverse_map = dict(zip(full_keys, keys))
        placeholders = ", ".join(f"${i + 1}" for i in range(len(full_keys)))
        rows = await self.db.query(
            f"SELECT key, value, counter FROM hyper_cache "
            f"WHERE key IN ({placeholders}) AND expires_at > NOW()",
            *full_keys,
        )
        result = {}
        for row in rows:
            row_key = row["key"] if isinstance(row, dict) else row[0]
            original_key = reverse_map.get(row_key, row_key)
            ctr = row["counter"] if isinstance(row, dict) else row[2]
            if ctr is not None:
                result[original_key] = ctr
            else:
                result[original_key] = row["value"] if isinstance(row, dict) else row[1]
        return result

    async def set_many(self, mapping: dict[str, JSONValue], ttl: int | None = None):
        """Set multiple key-value pairs."""
        for key, value in mapping.items():
            await self.set(key, value, ttl)

    async def delete_many(self, keys: list[str]):
        """Delete multiple keys."""
        if not keys:
            return
        full_keys = [make_cache_key(k) for k in keys]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(full_keys)))
        await self.db.execute(
            f"DELETE FROM hyper_cache WHERE key IN ({placeholders})",
            *full_keys,
        )

    async def incr(self, key: str, delta: int = 1) -> int:
        """Atomic increment. Creates key with delta if missing.

        Uses dedicated BIGINT counter column — clean atomic SQL, no type casting.
        """
        full_key = make_cache_key(key)
        ttl_seconds = int(self.default_ttl)
        # Expiry-aware upsert (mirrors set()/get_or_set()): if the existing row
        # is already expired, RESET the counter to `delta` and stamp a fresh
        # expires_at instead of incrementing a stale value forever. Without this,
        # an expired counter keeps climbing from its old value while get()/has()
        # (which filter on expires_at > NOW()) report it as gone.
        row = await self.db.query_one(
            "INSERT INTO hyper_cache (key, value, counter, expires_at) "
            "VALUES ($1, NULL, $2, NOW() + $3 * INTERVAL '1 second') "
            "ON CONFLICT (key) DO UPDATE SET "
            "  counter = CASE WHEN hyper_cache.expires_at <= NOW() "
            "    THEN EXCLUDED.counter "
            "    ELSE COALESCE(hyper_cache.counter, 0) + EXCLUDED.counter END, "
            "  value = NULL, "
            "  expires_at = CASE WHEN hyper_cache.expires_at <= NOW() "
            "    THEN EXCLUDED.expires_at ELSE hyper_cache.expires_at END "
            "RETURNING counter",
            full_key,
            delta,
            ttl_seconds,
        )
        if row:
            val = row["counter"] if isinstance(row, dict) else row[0]
            return int(val)
        return delta


# ─── Cache Decorator ───────────────────────────────────────────────────────────

# Global cache instance (set by app configuration)
_default_cache: LocMemCache | DatabaseCache | None = None
_default_cache_lock = threading.Lock()


def get_cache() -> LocMemCache | DatabaseCache:
    """Get the global cache instance.

    When no cache has been explicitly set, uses CACHE_BACKEND setting
    to determine the default ("memory" returns LocMemCache).
    """
    global _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            # Build it ONCE and memoize under the lock — a fresh instance per
            # call would make the default cache a silent no-op (every get()
            # misses, every set() writes to a discarded object).
            _default_cache = _build_default_cache()
        return _default_cache


def _build_default_cache() -> LocMemCache | DatabaseCache:
    """Construct the default cache from the CACHE_BACKEND setting.

    "memory" (and any backend with no registered adapter — e.g. an unconfigured
    "database", which needs an explicit set_cache(DatabaseCache(db)) call) falls
    back to the in-process LocMemCache. A backend name registered via
    register_adapter() is instantiated through the adapter registry, so a
    configured backend is actually used instead of being silently ignored.
    """
    backend = get_setting("CACHE_BACKEND")
    if backend and backend != "memory":
        # Lazy import avoids a cache ↔ cache_adapters import cycle at module load.
        from hyperdjango.cache_adapters import get_adapter

        adapter_cls = get_adapter(backend)
        if adapter_cls is not None:
            # Registered default adapters are expected to be constructible with
            # no arguments; a misconfigured one raising here is a real error and
            # is deliberately not swallowed.
            return adapter_cls()
    return LocMemCache()


def set_cache(cache: LocMemCache | DatabaseCache):
    """Set the global cache instance."""
    global _default_cache
    with _default_cache_lock:
        _default_cache = cache


def cached(
    ttl: int | None = None,
    key_prefix: str = "",
    cache: LocMemCache | DatabaseCache | None = None,
):
    """Decorator to cache function results.

    Works with both sync and async functions.
    Cache key is derived from function name + arguments.

    Usage:
        @cached(ttl=60)
        async def get_user(user_id):
            return await db.query_one("SELECT * FROM users WHERE id = $1", user_id)

        @cached(ttl=300, key_prefix="stats")
        def compute_stats(date_range):
            ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            effective_ttl = ttl if ttl is not None else get_setting("CACHE_TTL")
            c = cache or get_cache()
            cache_key = _make_key(key_prefix or func.__name__, args, kwargs)

            if c._is_async:
                result = await c.get(cache_key, default=_CACHE_MISS)
            else:
                result = c.get(cache_key, default=_CACHE_MISS)

            if result is not _CACHE_MISS:
                return result

            result = await func(*args, **kwargs)

            if c._is_async:
                await c.set(cache_key, result, effective_ttl)
            else:
                c.set(cache_key, result, effective_ttl)

            return result

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            effective_ttl = ttl if ttl is not None else get_setting("CACHE_TTL")
            c = cache or get_cache()
            cache_key = _make_key(key_prefix or func.__name__, args, kwargs)
            result = c.get(cache_key, default=_CACHE_MISS)
            if result is not _CACHE_MISS:
                return result
            result = func(*args, **kwargs)
            c.set(cache_key, result, effective_ttl)
            return result

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# Memoized "prefix:vN:" namespace. make_cache_key runs on every cache op, so we
# avoid rebuilding the joined string each call. The memo is keyed on the actual
# (prefix, version) values — not a None flag — so it re-resolves the instant
# either setting changes by ANY mechanism (get_setting, a direct DEFAULTS patch,
# an env override), independent of the settings-changed hook. get_setting is
# already cached (~40ns), so the two reads per call are negligible; the win is
# skipping the f-string build on the common no-change path.
# The memo is a SINGLE (key, value) tuple swapped atomically, so a concurrent
# reader can never match the OLD key against the NEW value (a torn pair would
# yield the wrong cache-key prefix → spurious miss). Readers bind the tuple once
# and read both halves from that same immutable snapshot.
_namespace_memo: tuple[tuple[str, object], str] | None = None


def _cache_namespace() -> str:
    """Return the 'prefix:vN:' namespace, rebuilt only when the settings change."""
    global _namespace_memo
    prefix = get_setting("CACHE_KEY_PREFIX")
    version = get_setting("CACHE_VERSION")
    key = (prefix, version)
    memo = _namespace_memo  # single atomic reference read — no torn pair
    if memo is None or memo[0] != key:
        value = f"{prefix}:v{version}:" if prefix else f"v{version}:"
        _namespace_memo = (key, value)  # atomic reference swap
        return value
    return memo[1]


def _invalidate_namespace_cache() -> None:
    """Drop the memo so it re-resolves on next use (belt-and-braces; the value
    comparison already self-invalidates)."""
    global _namespace_memo
    _namespace_memo = None


_register_settings_changed_hook(_invalidate_namespace_cache)


def make_cache_key(key: str) -> str:
    """Apply global CACHE_KEY_PREFIX and CACHE_VERSION to a cache key.

    All cache backends should use this to namespace and version keys.
    """
    return _cache_namespace() + key


def _make_key(
    prefix: str, args: tuple[JSONValue, ...], kwargs: dict[str, JSONValue]
) -> str:
    """Generate a cache key from function name + arguments.

    The encoding is INJECTIVE: distinct (args, kwargs) always map to distinct
    keys. The previous ``":".join(str(arg))`` scheme collided across argument
    boundaries and types — ``f("a:b")`` vs ``f("a", "b")`` and ``f(1)`` vs
    ``f("1")`` produced the SAME key, so one call could return another call's
    cached value (a correctness bug and, when the arguments carry tenant/user
    identity, a cross-argument data leak). Two defenses:
      * ``repr`` (not ``str``) distinguishes ``1`` from ``"1"`` and quotes
        strings, and the arg/kwarg counts fix the section boundary;
      * length-prefixing every component (``len:value``) makes any embedded
        ``:``/``=``/``|`` harmless, so no content can forge a boundary.
    """
    # repr() (not str()) distinguishes 1 from "1" and quotes strings; the
    # arg/kwarg counts fix the section boundary; injective_join length-prefixes
    # each component so an embedded separator can never forge a boundary.
    parts = [prefix, f"a{len(args)}", f"k{len(kwargs)}"]
    parts.extend(repr(arg) for arg in args)
    parts.extend(f"{k}={v!r}" for k, v in sorted(kwargs.items()))
    raw = injective_join(parts)
    if len(raw) > 200:
        # Hash long keys (the length-prefixed raw form is still injective, so
        # the hash inherits the collision resistance up to md5's).
        h = hashlib.md5(raw.encode()).hexdigest()
        raw = f"{prefix}:{h}"
    return make_cache_key(raw)
