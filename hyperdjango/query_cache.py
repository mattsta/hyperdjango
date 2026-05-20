"""
Transparent query cache with write-through invalidation.

Provides automatic caching of QuerySet results with intelligent invalidation
when data changes. Uses the pluggable cache backends from cache.py.

Architecture:
- QueryCacheManager wraps any cache backend (LocMemCache/DatabaseCache)
- Cache keys are namespaced by table name (and a global generation) for
  targeted invalidation
- Invalidation is version-based (O(1), no key scanning):
  1. Table-level: any write to a table bumps its version, missing all cached
     queries for that table
  2. Row-level: a specific PK change is tracked separately for stats but, under
     version-based caching, still maps to a table-version bump (individual
     version-keyed entries can't be selectively removed)
  3. Global: invalidate_all() bumps a generation counter AND clears the backend
- FK dependency tracking: writing to a table also invalidates tables that JOIN to it
- Signal-driven: post_save/post_delete signals trigger automatic invalidation

Usage:
    from hyperdjango.query_cache import get_query_cache, configure_query_cache

    # Configure (typically in app setup)
    configure_query_cache(backend=LocMemCache(max_size=5000), default_ttl=60)

    # QuerySet integration (automatic via .cache())
    users = await User.objects.cache(ttl=120).filter(active=True).all()

    # Per-model default TTL
    class Product(Model):
        class Meta:
            table = "products"
            cache_ttl = 300  # Cache all queries for 5 minutes

    # Manual invalidation
    cache = get_query_cache()
    cache.invalidate_table("users")
    cache.invalidate_row("users", 42)

    # Stats
    stats = cache.stats
    print(f"Hit rate: {stats.hit_rate:.1%}")
"""

import hashlib
import logging
import threading
from dataclasses import dataclass

from hyperdjango.cache import LocMemCache
from hyperdjango.types import JSONValue

_logger = logging.getLogger("hyperdjango.query_cache")

# ---------------------------------------------------------------------------
# Cache stats
# ---------------------------------------------------------------------------


@dataclass
class CacheStats:
    """Query cache statistics."""

    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    table_invalidations: int = 0
    row_invalidations: int = 0
    sets: int = 0

    @property
    def total_requests(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        total = self.total_requests
        if total == 0:
            return 0.0
        return self.hits / total

    def reset(self):
        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        self.table_invalidations = 0
        self.row_invalidations = 0
        self.sets = 0


# ---------------------------------------------------------------------------
# Table dependency tracker — FK relationships for cascading invalidation
# ---------------------------------------------------------------------------


class DependencyTracker:
    """Tracks FK dependencies between tables for cascading cache invalidation.

    When table A has a FK to table B, a write to B should also invalidate
    cached queries for A (since JOINs may be stale).

    Thread-safe.
    """

    def __init__(self):
        # table -> set of tables that depend on it (have FK to it)
        self._dependents: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def register_dependency(self, source_table: str, target_table: str):
        """Register that source_table has a FK pointing to target_table.

        When target_table changes, source_table's cache should also be invalidated.
        """
        with self._lock:
            if target_table not in self._dependents:
                self._dependents[target_table] = set()
            self._dependents[target_table].add(source_table)

    def get_dependents(self, table: str) -> set[str]:
        """Get all tables that have FK dependencies on the given table."""
        with self._lock:
            return set(self._dependents.get(table, set()))

    def get_all_affected_tables(self, table: str) -> set[str]:
        """Get the table itself + all tables with FK dependencies on it."""
        affected = {table}
        affected.update(self.get_dependents(table))
        return affected

    def clear(self):
        with self._lock:
            self._dependents.clear()


# ---------------------------------------------------------------------------
# QueryCacheManager
# ---------------------------------------------------------------------------


class QueryCacheManager:
    """Transparent query cache with write-through invalidation.

    Wraps a cache backend (LocMemCache or DatabaseCache) and provides:
    - Table-namespaced cache keys for targeted invalidation
    - Table-level invalidation (all queries for a table)
    - Row-level invalidation (queries involving a specific PK)
    - FK dependency cascading (invalidate JOINed tables)
    - Cache statistics tracking
    - Per-table version counters for fast bulk invalidation

    Uses version-based invalidation: each table has a monotonically increasing
    version number. Cache keys include the version, so bumping the version
    instantly invalidates all cached queries for that table without scanning.
    """

    def __init__(self, backend: LocMemCache | None = None, default_ttl: int = 60):
        self._backend = backend or LocMemCache(max_size=10000)
        self.default_ttl = default_ttl
        self.stats = CacheStats()
        self.dependencies = DependencyTracker()
        self._enabled = True

        # Table version counters for fast invalidation
        # Key: table_name -> version (int)
        self._table_versions: dict[str, int] = {}
        # Global generation counter — embedded in every cache key. Bumping it
        # invalidates ALL keys at once, including tables still at version 0
        # (which a per-table version bump would miss). Guarded by the same lock
        # as the table versions.
        self._generation: int = 0
        self._version_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    # --- Version management ---

    def _get_table_version(self, table: str) -> int:
        """Get current version for a table. Starts at 0."""
        with self._version_lock:
            return self._table_versions.get(table, 0)

    def _bump_table_version(self, table: str) -> int:
        """Increment table version. Returns new version."""
        with self._version_lock:
            v = self._table_versions.get(table, 0) + 1
            self._table_versions[table] = v
            return v

    # --- Cache key generation ---

    def make_key(self, table: str, sql: str, params: tuple) -> str:
        """Generate a cache key for a query.

        Format: "qc:g{generation}:{table}:v{version}:{hash}"

        Both the global generation and the per-table version are embedded in
        the key, so bumping either makes all affected old keys miss without
        scanning.
        """
        with self._version_lock:
            generation = self._generation
            version = self._table_versions.get(table, 0)
        # Hash the SQL + params for a compact key
        raw = f"{sql}:{params}"
        h = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:16]
        return f"qc:g{generation}:{table}:v{version}:{h}"

    def make_multi_table_key(self, tables: list[str], sql: str, params: tuple) -> str:
        """Generate a cache key for a query spanning multiple tables (JOINs).

        Includes the global generation plus versions of ALL involved tables,
        so any table's change (or a global invalidate_all) invalidates the
        cached query.
        """
        with self._version_lock:
            generation = self._generation
            versions = [
                f"{t}:v{self._table_versions.get(t, 0)}" for t in sorted(tables)
            ]
        version_str = "|".join(versions)
        raw = f"{sql}:{params}"
        h = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:16]
        return f"qc:g{generation}:{version_str}:{h}"

    # --- Get/Set ---

    def get(self, key: str) -> JSONValue:
        """Get a cached query result. Returns None on miss."""
        if not self._enabled:
            return None
        result = self._backend.get(key)
        if result is not None:
            self.stats.hits += 1
        else:
            self.stats.misses += 1
        return result

    def set(self, key: str, value: JSONValue, ttl: int | None = None):
        """Cache a query result."""
        if not self._enabled:
            return
        ttl = ttl or self.default_ttl
        self._backend.set(key, value, ttl)
        self.stats.sets += 1

    # --- Invalidation ---

    def invalidate_table(self, table: str):
        """Invalidate all cached queries for a table.

        Also cascades to tables with FK dependencies on this table.
        Uses version bumping for O(1) invalidation.
        """
        if not self._enabled:
            return

        affected = self.dependencies.get_all_affected_tables(table)
        for t in affected:
            self._bump_table_version(t)
            self.stats.table_invalidations += 1

        self.stats.invalidations += 1

    def invalidate_row(self, table: str, pk: int | str):
        """Invalidate cache entries related to a specific row.

        This is a more targeted invalidation — bumps the table version
        (since we can't selectively invalidate version-keyed entries)
        but tracks stats separately for monitoring.
        """
        if not self._enabled:
            return

        # Version-based caching means row invalidation = table invalidation
        # But we track it separately for stats
        self._bump_table_version(table)
        self.stats.row_invalidations += 1
        self.stats.invalidations += 1

        # Also invalidate dependent tables
        for dep_table in self.dependencies.get_dependents(table):
            self._bump_table_version(dep_table)

    def invalidate_all(self):
        """Invalidate the entire query cache.

        This:

        * advances the global generation counter, so every key computed
          before this call (regardless of per-table version) can never hit —
          including tables still at version 0 (read and cached but never
          written), which a per-table version bump would miss;
        * clears the backend, evicting the dead entries instead of leaving
          them to pollute the shared LRU until eviction.
        """
        with self._version_lock:
            self._generation += 1
            self._table_versions.clear()
        self._backend.clear()
        self.stats.invalidations += 1

    # --- Registration ---

    def register_model(self, model_class):
        """Register a model's FK dependencies for cascading invalidation.

        Called automatically when models are defined.
        """
        meta = model_class._meta
        table = meta.table
        if not table:
            return

        for field_name, field_meta in meta.fields.items():
            if field_meta.foreign_key:
                # This model has a FK to field_meta.foreign_key
                # So changes to the FK target should invalidate queries on this model
                self.dependencies.register_dependency(table, field_meta.foreign_key)

    # --- Warm cache ---

    def warm(self, key: str, value: JSONValue, ttl: int | None = None):
        """Pre-populate a cache entry (cache warming)."""
        self.set(key, value, ttl)

    # --- Diagnostics ---

    def get_table_versions(self) -> dict[str, int]:
        """Get current version counters for all tracked tables."""
        with self._version_lock:
            return dict(self._table_versions)

    def clear(self):
        """Clear all cached data and reset versions."""
        self._backend.clear()
        with self._version_lock:
            self._table_versions.clear()
        self.stats.reset()

    def __repr__(self):
        return (
            f"QueryCacheManager(backend={type(self._backend).__name__}, "
            f"default_ttl={self.default_ttl}, "
            f"hit_rate={self.stats.hit_rate:.1%}, "
            f"tables={len(self._table_versions)})"
        )


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_query_cache_manager: QueryCacheManager | None = None
_query_cache_lock = threading.Lock()


def get_query_cache() -> QueryCacheManager:
    """Get the global query cache manager. Creates a default if none configured."""
    global _query_cache_manager
    # Double-checked locking. Without it, a racing first-call on free-threaded
    # Python briefly runs two managers; a table version-bump on one is invisible
    # to the other, so cached queries go stale. The lock publishes exactly one.
    mgr = _query_cache_manager
    if mgr is not None:
        return mgr
    with _query_cache_lock:
        if _query_cache_manager is None:
            _query_cache_manager = QueryCacheManager()
        return _query_cache_manager


def set_query_cache(manager: QueryCacheManager):
    """Set the global query cache manager."""
    global _query_cache_manager
    _query_cache_manager = manager


def configure_query_cache(
    backend: LocMemCache | None = None,
    default_ttl: int = 60,
    enabled: bool = True,
) -> QueryCacheManager:
    """Configure the global query cache.

    Args:
        backend: Cache backend (LocMemCache or DatabaseCache). Defaults to LocMemCache.
        default_ttl: Default TTL in seconds for cached queries.
        enabled: Whether caching is enabled.

    Returns:
        The configured QueryCacheManager.
    """
    manager = QueryCacheManager(backend=backend, default_ttl=default_ttl)
    manager.enabled = enabled
    set_query_cache(manager)
    return manager


# ---------------------------------------------------------------------------
# Signal handlers for auto-invalidation
# ---------------------------------------------------------------------------


def _install_signal_handlers():
    """Connect cache invalidation to model lifecycle signals.

    Called once during module initialization. Handlers invalidate the
    query cache whenever a model instance is saved or deleted.
    """
    from hyperdjango.signals import post_delete, post_save

    def _safe_invalidate(cache, table, pk):
        """Invalidate a table/row, but never leave a stale positive entry.

        The whole point of these receivers is data correctness: after a write,
        the old cached rows for ``table`` must stop being served. If the
        targeted invalidation raises (transient backend error, lock failure,
        ...), silently returning would leave the pre-write cache entries live
        and the query cache SILENTLY STALE. Instead we:

          1. fail LOUD — log the failure at error level with a traceback, so
             it is observable rather than swallowed; and
          2. fail SAFE — fall back to a global ``invalidate_all`` (bumps the
             generation counter and clears the backend), so subsequent reads
             MISS and re-fetch fresh rather than serve stale data.

        A missed-but-fresh cache is always correct; a stale-but-served cache is
        a correctness bug, so degrading to "miss everything" is the safe choice.
        """
        try:
            if pk is not None:
                cache.invalidate_row(table, pk)
            else:
                cache.invalidate_table(table)
        # blind-except: any backend/version failure here would otherwise leave a definitively-stale positive cache entry; we log loudly and fall back to a global invalidation so reads miss rather than serve stale data (correctness over cache-hit-rate).
        except Exception:
            _logger.error(
                "query cache invalidation failed for table %r pk %r; "
                "falling back to invalidate_all to avoid serving stale data",
                table,
                pk,
                exc_info=True,
            )
            try:
                cache.invalidate_all()
            # blind-except: if even the global fallback fails there is nothing
            # safe left to do but surface it as loudly as possible; re-raise so
            # send_robust captures it and the calling save/delete logs it too.
            except Exception:
                _logger.critical(
                    "query cache invalidate_all fallback ALSO failed for "
                    "table %r; cache may be serving stale data",
                    table,
                    exc_info=True,
                )
                raise

    def _active_transaction_db():
        """Return the default Database iff a transaction is CURRENTLY active on
        this thread/task, else ``None``.

        A cross-subsystem race motivates this: post_save/post_delete fire from
        inside ``obj.save()`` which may run within ``async with db.transaction():``
        — i.e. BEFORE COMMIT. Invalidating the version key inline there lets a
        concurrent reader (between the bump and the COMMIT) re-populate the NEW
        version key with the OLD committed rows, serving stale data for the full
        TTL. So when a transaction is live we defer invalidation to on_commit.

        Detection routes through the single public ``Database.in_transaction()``
        authority so there is one definition of "inside a transaction" across the
        framework. We only READ the Database (never mutate it), which the
        fix-wave rules permit.
        """
        try:
            from hyperdjango.database import get_db

            db = get_db()
        # get_db() raises when no DATABASE_URL is configured (and there is then no
        # framework transaction the write could be inside), so "no active
        # transaction" → inline invalidation is the correct, safe fallback.
        # blind-except: no DB configured ⇒ no active tx; caller invalidates inline, nothing swallowed.
        except Exception:
            return None
        return db if db.in_transaction() else None

    def _invalidate_or_defer(cache, table, pk):
        """Invalidate now (autocommit) or defer to COMMIT (active transaction).

        ``db.on_commit`` runs the callback ONLY after the outermost COMMIT and
        DISCARDS it on rollback (database.py) — exactly the semantics we want:
        on rollback the rows never changed, so skipping invalidation is correct
        and avoids needlessly missing the cache. The callback list has the same
        thread/task affinity as the transaction, so this is correct under
        free-threading. ``table``/``pk`` are captured by value at signal time.
        """
        db = _active_transaction_db()
        if db is not None:
            db.on_commit(lambda: _safe_invalidate(cache, table, pk))
        else:
            _safe_invalidate(cache, table, pk)

    @post_save.connect(dispatch_uid="query_cache_post_save")
    def _on_post_save(sender, **kwargs):
        instance = kwargs.get("instance")
        if instance is None:
            return
        cache = get_query_cache()
        _invalidate_or_defer(cache, instance._meta.table, instance.pk)

    @post_delete.connect(dispatch_uid="query_cache_post_delete")
    def _on_post_delete(sender, **kwargs):
        instance = kwargs.get("instance")
        if instance is None:
            return
        cache = get_query_cache()
        _invalidate_or_defer(cache, instance._meta.table, instance.pk)


# Install handlers on module import
_install_signal_handlers()
