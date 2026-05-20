"""
Database connection manager.

Uses pg.zig (native Zig PostgreSQL driver) as the primary backend.
Binary protocol, prepared statement caching, connection pooling.

No Django dependency.

Usage:
    db = Database("postgres://localhost/mydb")
    await db.connect()
    rows = await db.query("SELECT * FROM users WHERE age > $1", 18)
    await db.disconnect()

Error taxonomy — THE contract for BOTH dispatch paths:
    Every database operation here — the direct-SQL interface (`query`,
    `query_one`, `query_val`, `execute`, `execute_many`, `pipeline`, `copy_*`,
    `explain`) AND the ORM built on top of it — raises ONE typed exception
    hierarchy for a given PostgreSQL failure, classified at the native FFI
    boundary by `_classify_pg_error` (the SAME classifier the psycopg-compat
    cursor path uses). The direct-SQL and ORM interfaces stay separate APIs at
    different levels; only the exception TYPES are unified. The mapping (from
    `hyperdjango.db.pgzig_connection`):
        * IntegrityError       — unique / duplicate-key, foreign-key, and other
                                 constraint violations
        * DuplicateTable /
          DuplicateDatabase    — "already exists" DDL collisions
        * ProgrammingError     — syntax errors, undefined objects
        * InvalidParameterValue (DataError) — bad parameter/value/role
        * OperationalError     — connection loss, permission denied, pool
                                 exhaustion, "in use by other users"
        * DatabaseError        — the base / catch-all for anything unmatched
    A raw `RuntimeError` from a native op therefore never reaches callers: it is
    always the typed subclass. Framework-precondition failures that are NOT a
    server-side error (e.g. "Database not connected") keep raising `RuntimeError`
    — they are not a PostgreSQL result and have no place in this hierarchy.
"""

import asyncio
import enum
import inspect
import os
import threading
import time as _time
import uuid as _uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.parse import urlparse, urlsplit, urlunsplit

from hyperdjango import performance as _perf_module
from hyperdjango import profiling as _prof_module
from hyperdjango._hyperdjango_native import (
    _db_close_pool,
    _db_configure,
    _db_conn_acquire,
    _db_conn_execute,
    _db_conn_release,
    _db_copy_from,
    _db_copy_to,
    _db_exec_many,
    _db_execute,
    _db_get_last_columns,
    _db_mark_offload_worker,
    _db_pipeline,
    _db_pool_stats,
    _db_query,
    _db_query_dicts,
    _db_query_json,
    _db_register_query,
    _db_register_vector,
)
from hyperdjango._lazy import SafeLazy
from hyperdjango.conf import fill_url_auth
from hyperdjango.conf import get_setting as _get_setting
from hyperdjango.native import fast_json_loads
from hyperdjango.profiling import SQLQuery as _SQLQuery
from hyperdjango.telemetry import metrics as _tel_metrics

# ── Native telemetry metrics (zero cost when telemetry disabled) ───────────
#
# Registered at module load time — one FFI call each, happens once per
# process. When telemetry is disabled these are pure no-ops (one
# LOAD_GLOBAL + branch, ~20-30 ns each). When enabled they bump native
# Counter/Histogram handles via a single FFI call each.

_db_queries_total = _tel_metrics.Counter(
    "hyperdjango_db_queries_total",
    "Total number of database queries executed",
)
_db_query_duration_seconds = _tel_metrics.Histogram(
    "hyperdjango_db_query_duration_seconds",
    "Database query execution duration in seconds",
)

# pg.zig pool gauges — sampled by the drain worker once per tick via the
# `_sample_pool_gauges` callback registered below. Tracks the default pool
# only; apps with multiple pools can register their own samplers using the
# same pattern.
#
# These gauges expose the contention metrics shipped in v0.14.13 (task #193)
# so dashboards can alert on saturation BEFORE it shows up as request
# latency. `pool_waiters` is the live count of threads currently blocked in
# `timedWait`; `pool_in_use` is the count of pinned connections.

_pool_total_connections = _tel_metrics.Gauge(
    "hyperdjango_pool_total_connections",
    "Configured pool size for the default database pool.",
)
_pool_in_use_connections = _tel_metrics.Gauge(
    "hyperdjango_pool_in_use_connections",
    "Currently-pinned connections in the default pool.",
)
_pool_available_connections = _tel_metrics.Gauge(
    "hyperdjango_pool_available_connections",
    "Currently-idle connections in the default pool.",
)
_pool_waiters = _tel_metrics.Gauge(
    "hyperdjango_pool_waiters",
    "Threads currently blocked waiting for a pool connection.",
)
_pool_max_waiters = _tel_metrics.Gauge(
    "hyperdjango_pool_max_waiters",
    "High-water mark for blocked threads since pool start.",
)
_pool_acquire_count_total = _tel_metrics.Gauge(
    "hyperdjango_pool_acquires",
    "Cumulative pool acquire count (mirrored from native counter).",
)
_pool_timeout_count_total = _tel_metrics.Gauge(
    "hyperdjango_pool_timeouts",
    "Cumulative pool acquire timeouts (mirrored from native counter).",
)


def _sample_pool_gauges() -> None:
    """Periodic sampler — pull pg.zig pool stats into Prometheus gauges.

    Registered with the telemetry sampler registry at module load time.
    The drain worker invokes this once per drain tick (default 1.0 s).

    No-op when:
      * the global default `Database` instance has not been created yet
        (early boot, or apps that build their own Database directly)
      * the pool is closed / unconfigured
      * `_db_pool_stats` raises (the underlying FFI is defensive but a
        broken handle should never crash the drain thread)

    Reads `_db` directly (NOT `get_db()`) to avoid lazily creating a
    pool just to sample it — that would cause telemetry to deadlock
    on first access. If `_db` is None we skip silently.
    """
    db = _db
    if db is None or db._pool_handle < 0:
        return
    try:
        stats = _db_pool_stats(db._pool_handle)
    # blind-except: telemetry sampler must never crash the metrics drain thread; a broken or torn-down pool handle (raced against this FFI call) is skipped this cycle.
    except Exception:
        return
    # Defensive — `stats` may be empty if the pool was just torn down
    # between the handle check and the FFI call.
    if not stats:
        return
    _pool_total_connections.set(int(stats.get("total", 0)))
    _pool_in_use_connections.set(int(stats.get("in_use", 0)))
    _pool_available_connections.set(int(stats.get("available", 0)))
    _pool_waiters.set(int(stats.get("waiters", 0)))
    _pool_max_waiters.set(int(stats.get("max_waiters", 0)))
    _pool_acquire_count_total.set(int(stats.get("acquire_count", 0)))
    _pool_timeout_count_total.set(int(stats.get("timeout_count", 0)))


_tel_metrics.register_sampler(_sample_pool_gauges)


def _should_track_query() -> bool:
    """Return True if any active consumer wants per-query timing stats.

    Three independent consumers can request tracking; the condition is
    their disjunction:

      1. `PerformanceMiddleware` (slow-query log, N+1 detection, /debug)
      2. `profiling.RequestProfile` (per-request flame graphs)
      3. `hyperdjango.telemetry` (native Prometheus metrics + spans)

    When ALL three are inactive (the production default), every query
    method takes the fast path: direct FFI call, no timing, no stats.
    Saves ~1 μs per query of pure-Python tracking dispatch.

    Single source of truth — any code that wants to branch on "is
    somebody observing query stats right now?" MUST call this helper
    instead of duplicating the disjunction inline. Adding a new
    consumer is a one-line change here instead of 6+ method updates.
    """
    return (
        _perf_module._perf_middleware is not None
        or _prof_module._thread_local.state.profile is not None
        or _tel_metrics.is_enabled()
    )


# ── Lock-free native query handles ─────────────────────────────────────────
#
# Each distinct SQL string maps to a native registry handle (see the Zig
# "Lock-free query registry" comment). With a handle, `_db_query_dicts`
# skips BOTH process-global mutexes the handle-less path takes per call
# (prep-statement name lookup + interned-column-key lookup) — critical for free-threaded
# scaling, where those mutexes reserialized every parallel query despite the
# thread-owned-connection design.
#
# The native registry is a FIXED 4096-slot append-only table, so we must NOT
# feed it unbounded one-off SQL (paginated queries, per-cardinality prefetch
# IN/ANY lists, ad-hoc user SQL) or it fills and returns -1 forever. Two
# guards keep both this dict AND the native registry bounded:
#
#   * Register only on the SECOND sighting of a SQL string. One-off SQL then
#     only ever touches `_query_seen_once` (a bounded set) and never consumes
#     a registry slot; genuinely-repeated query shapes register on reuse and
#     reap the lock-free fast path.
#   * Both structures are size-capped and cleared wholesale on overflow (a
#     coarse but allocation-free eviction — the worst case is re-registering a
#     hot query, which is cheap). We never cache a -1 result, so a transiently
#     full registry doesn't permanently poison a SQL's entry.
#
# Plain dict/set ops are individually thread-safe under free-threading; the
# races here are all benign (a duplicate registration returns the same
# hash-deduped handle; a lost insert just re-runs the cheap fallback once).
_QUERY_CACHE_MAX = 4096
_query_handle_cache: dict[str, int] = {}  # sql → handle (>= 0 only)
_query_seen_once: set[str] = set()  # sql seen once, awaiting a 2nd sighting


def _query_handle(sql: str) -> int:
    """Return the lock-free registry handle for ``sql``, registering it only on
    its second sighting so one-off SQL never consumes a native registry slot.
    ``-1`` means "no handle" and the native dict path uses its shared locked
    caches (always correct, just not lock-free)."""
    handle = _query_handle_cache.get(sql)
    if handle is not None:
        return handle

    # Not registered yet. First sighting → remember it and use the fallback;
    # only a repeated query is worth a scarce registry slot.
    if sql not in _query_seen_once:
        if len(_query_seen_once) >= _QUERY_CACHE_MAX:
            _query_seen_once.clear()
        _query_seen_once.add(sql)
        return -1

    # Second sighting → register now.
    handle = _db_register_query(sql)
    if handle < 0:
        # Registry full — fall back WITHOUT caching -1 (don't poison the cache
        # nor grow it with dead entries).
        return -1
    if len(_query_handle_cache) >= _QUERY_CACHE_MAX:
        _query_handle_cache.clear()
    _query_handle_cache[sql] = handle
    _query_seen_once.discard(sql)
    return handle


def _stmt_creates_vector_extension(sql: str) -> bool:
    """True if ``sql`` is a ``CREATE EXTENSION`` that installs pgvector.

    Kept cheap for the hot ``execute()`` path: a normal INSERT/UPDATE/DELETE
    is rejected by the leading-token check before any full-string lowercasing
    happens. Only actual ``CREATE EXTENSION`` DDL pays for the ``vector`` scan.
    """
    if not sql.lstrip()[:16].lower().startswith("create extension"):
        return False
    return "vector" in sql.lower()


# ── Off-loop execution of blocking native queries ──────────────────────────
#
# The native `_db_query_*` / `_db_execute` calls are a fully synchronous
# PostgreSQL round-trip: the calling OS thread is busy for the query's whole
# duration, and if the pool is exhausted it parks in the native
# `pool.acquire()` timedWait. These methods are `async def`, so how they run
# depends on the ROLE of the event loop driving them:
#
#   * Thread-per-request loops (the documented HTTP model: one loop per Zig
#     worker thread, one request at a time; also tests, scripts, startup,
#     thread-mode WS). The loop drives a SINGLE flow of awaits, so a blocking
#     round-trip only stalls the very request that is waiting for it. Running
#     INLINE is optimal: no thread hop, and — critically — the query stays on
#     the calling thread, which owns exactly one pinned pool connection. This
#     is what keeps the connection budget = THREAD_POOL_SIZE + headroom
#     (see `_derive_pool_size_from_thread_count`).
#
#   * MULTIPLEXING loops (the high-performance default for real concurrency:
#     the shared WebSocket event-loop pool today, the HTTP reactor tomorrow).
#     One loop drives MANY connections, so a blocking round-trip would stall
#     all of them. Such loops OFFLOAD the round-trip to a small, bounded,
#     process-wide DB executor so the loop stays responsive. Free-threaded
#     Python (3.14t, no GIL) runs the executor thread in true parallel.
#
# The offload is therefore automatic on exactly the loops that need it — it
# is not an opt-in: the default high-performance concurrency models (shared
# WS pool, reactor) flag their loops multiplexing at creation via
# `mark_loop_multiplexing`, and everything else stays inline. The executor is
# sized through the settings system (`DB_OFFLOAD_WORKERS`) and its slots are
# folded into the pool budget so it never over-subscribes PostgreSQL.

_T = TypeVar("_T")

# Flag set on loops that multiplex many connections. DB round-trips on such
# loops offload to the DB executor; every other loop runs inline.
_MULTIPLEXING_LOOP_ATTR = "_hyperdjango_multiplexing"

# Allowlist of SQL isolation levels. The value is interpolated into a BEGIN
# statement (not a bind param — PostgreSQL doesn't parametrize it), so it MUST be
# a fixed allowlisted keyword, never raw caller input.
_ISOLATION_LEVELS = {
    "read_committed": "READ COMMITTED",
    "repeatable_read": "REPEATABLE READ",
    "serializable": "SERIALIZABLE",
    "read_uncommitted": "READ UNCOMMITTED",
}


def _begin_statement(isolation_level: str | None) -> str:
    """Build the outermost BEGIN, optionally with an allowlisted isolation level.

    Raises ValueError for an unknown level (never interpolates raw input).
    """
    if isolation_level is None:
        return "BEGIN"
    level = _ISOLATION_LEVELS.get(isolation_level.lower())
    if level is None:
        raise ValueError(
            f"unknown isolation_level {isolation_level!r}; "
            f"choose one of {sorted(_ISOLATION_LEVELS)}"
        )
    return f"BEGIN ISOLATION LEVEL {level}"


def mark_loop_multiplexing(loop: asyncio.AbstractEventLoop) -> None:
    """Flag an event loop as multiplexing many connections/requests.

    Blocking native DB round-trips on a flagged loop are offloaded to the
    bounded DB executor so one query can't stall the other connections the
    loop is driving. Called by the runtime where it owns such loops — the
    shared WebSocket event-loop pool, and (later) the HTTP reactor. Loops
    that are NOT flagged (thread-per-request HTTP, tests, scripts, startup,
    thread-mode WS) run inline, which is optimal for a single-flow loop and
    preserves the pinned-connection-per-thread budget.
    """
    # Some loop implementations forbid arbitrary attributes; falling back to
    # inline is always correct (just not de-stalled).
    with suppress(AttributeError, TypeError):
        # dynamic-attr: injecting a framework marker (name held in a module constant) onto a foreign asyncio event-loop object that does not declare it
        setattr(loop, _MULTIPLEXING_LOOP_ATTR, True)


def db_offload_worker_count() -> int:
    """Resolve the DB offload executor size (max concurrent offloaded
    round-trips) via the settings system.

    `DB_OFFLOAD_WORKERS` (Django setting / `HYPER_DB_OFFLOAD_WORKERS` env /
    DEFAULTS); 0 = auto = min(cpu_count, 8) — the scale of the shared WS
    loop pool. Folded into the pool budget by
    `_derive_pool_size_from_thread_count` so offload connections fit.
    """
    try:
        configured = int(_get_setting("DB_OFFLOAD_WORKERS", 0) or 0)
    except ValueError, TypeError:
        configured = 0
    if configured > 0:
        return configured
    return min(os.cpu_count() or 4, 8)


def _make_db_offload_executor() -> ThreadPoolExecutor:
    # `initializer` marks each worker thread as a DB-offload worker in the native
    # layer (runs once per thread at startup). An offload worker acquires and
    # releases a pool connection PER OP instead of pinning one thread-owned
    # connection for its lifetime: these threads serve arbitrary unrelated tasks'
    # queries, so a pinned connection would never return to the pool while the
    # loop is idle (inflating `in_use`, starving other consumers) AND would carry
    # session state (SET / search_path / cursors) between unrelated tasks. See
    # `offload_worker` in zig/src/db.zig.
    return ThreadPoolExecutor(
        max_workers=max(1, db_offload_worker_count()),
        thread_name_prefix="hyperdjango-db",
        initializer=_db_mark_offload_worker,
    )


# Bounded, process-wide executor for off-loop blocking DB round-trips. Built
# lazily via SafeLazy (the one audited double-checked-locking primitive): a pure
# thread-per-request (HTTP-only) deployment never offloads, so this executor and
# its pool connections never exist. Under free-threaded CPython a racing first
# call must NOT build 14 executors (14× the DB-connection budget) — SafeLazy
# builds exactly once. One pool per process; never one-per-connection.
_db_offload_executor_lazy: SafeLazy[ThreadPoolExecutor] = SafeLazy(
    _make_db_offload_executor
)


def _db_offload_executor() -> ThreadPoolExecutor:
    return _db_offload_executor_lazy.get()


async def _run_db_blocking[T](op: Callable[[], T]) -> T:
    """Run a blocking native DB op with the correct event-loop semantics for
    the current loop's role: inline on a single-flow loop, offloaded to the
    DB executor on a multiplexing loop. See the module note above.
    """
    loop = asyncio.get_running_loop()
    # dynamic-attr: reading the framework marker (name held in a module constant) that may have been injected onto a foreign asyncio event-loop object
    if getattr(loop, _MULTIPLEXING_LOOP_ATTR, False):
        return await loop.run_in_executor(_db_offload_executor(), op)
    return op()


def _classify_call[T](native_call: Callable[[], _T]) -> Callable[[], _T]:
    """Wrap a RAW native FFI call so a PostgreSQL failure surfaces as the unified
    typed exception hierarchy instead of a bare ``RuntimeError``.

    The native ``_db_*`` FFI raises a plain ``RuntimeError`` carrying the
    PostgreSQL error text for any server-side failure. This wrapper routes that
    text through ``_classify_pg_error`` — the SAME classifier the psycopg-compat
    cursor path applies — so a given Postgres error raises the IDENTICAL typed
    class (IntegrityError, OperationalError, ProgrammingError, DataError, ...)
    whether it reaches the caller via the native direct-SQL / ORM path or the
    cursor path. See the module docstring for the full mapping.

    Applied to the raw FFI call ONLY — before any ``execute_wrapper`` is composed
    around it — so a user wrapper that deliberately raises ``RuntimeError`` (e.g.
    a test guard that blocks queries) is never reclassified. The classifier
    import is deferred to the error path so the success hot path and cold start
    never pull in psycopg.
    """

    def classified() -> _T:
        try:
            return native_call()
        except RuntimeError as exc:
            from hyperdjango.db.pgzig_connection import _classify_pg_error

            raise _classify_pg_error(str(exc)) from exc

    return classified


@dataclass
class ExplainNode:
    """A single node in a PostgreSQL query plan tree."""

    node_type: str
    relation: str
    index_name: str
    startup_cost: float
    total_cost: float
    plan_rows: int
    plan_width: int
    actual_startup_time: float
    actual_total_time: float
    actual_rows: int
    actual_loops: int
    shared_hit_blocks: int
    shared_read_blocks: int
    children: list[ExplainNode] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def is_seq_scan(self) -> bool:
        return self.node_type == "Seq Scan"

    @property
    def is_index_scan(self) -> bool:
        return self.node_type in ("Index Scan", "Index Only Scan", "Bitmap Index Scan")


@dataclass
class ExplainResult:
    """Structured result from EXPLAIN / EXPLAIN ANALYZE.

    Attributes:
        text: The full text plan output.
        plan: Root ExplainNode (parsed from JSON format).
        planning_time: Planning time in ms (ANALYZE only).
        execution_time: Execution time in ms (ANALYZE only).
        analyzed: Whether ANALYZE was used (actual times available).

    Usage:
        result = await db.explain("SELECT * FROM users WHERE id = $1", 1, analyze=True)
        print(result.execution_time)        # 0.042
        print(result.has_seq_scan)           # False
        print(result.seq_scan_tables)        # []
        print(result.index_scans)            # [ExplainNode(...)]
        print(result.text)                   # Full plan text
    """

    text: str
    plan: ExplainNode | None
    planning_time: float
    execution_time: float
    analyzed: bool

    @property
    def has_seq_scan(self) -> bool:
        """True if any node in the plan uses a sequential scan."""
        return bool(self.seq_scan_tables)

    @property
    def seq_scan_tables(self) -> list[str]:
        """List of table names that use sequential scans."""
        if self.plan is None:
            return []
        tables: list[str] = []
        _collect_seq_scans(self.plan, tables)
        return tables

    @property
    def index_scans(self) -> list[ExplainNode]:
        """List of all index scan nodes in the plan."""
        if self.plan is None:
            return []
        nodes: list[ExplainNode] = []
        _collect_index_scans(self.plan, nodes)
        return nodes

    @property
    def all_nodes(self) -> list[ExplainNode]:
        """Flat list of all nodes in the plan tree (pre-order)."""
        if self.plan is None:
            return []
        nodes: list[ExplainNode] = []
        _collect_all_nodes(self.plan, nodes)
        return nodes


def _collect_seq_scans(node: ExplainNode, out: list[str]) -> None:
    if node.is_seq_scan and node.relation:
        out.append(node.relation)
    for child in node.children:
        _collect_seq_scans(child, out)


def _collect_index_scans(node: ExplainNode, out: list[ExplainNode]) -> None:
    if node.is_index_scan:
        out.append(node)
    for child in node.children:
        _collect_index_scans(child, out)


def _collect_all_nodes(node: ExplainNode, out: list[ExplainNode]) -> None:
    out.append(node)
    for child in node.children:
        _collect_all_nodes(child, out)


def _parse_plan_node(raw: dict[str, object]) -> ExplainNode:
    """Parse a single plan node from PostgreSQL JSON EXPLAIN output."""
    children = [_parse_plan_node(c) for c in raw.get("Plans", [])]
    # Collect extra fields not in the main dataclass
    known = {
        "Node Type",
        "Relation Name",
        "Index Name",
        "Startup Cost",
        "Total Cost",
        "Plan Rows",
        "Plan Width",
        "Actual Startup Time",
        "Actual Total Time",
        "Actual Rows",
        "Actual Loops",
        "Shared Hit Blocks",
        "Shared Read Blocks",
        "Plans",
    }
    extra = {k: v for k, v in raw.items() if k not in known}
    return ExplainNode(
        node_type=raw.get("Node Type", ""),
        relation=raw.get("Relation Name", ""),
        index_name=raw.get("Index Name", ""),
        startup_cost=float(raw.get("Startup Cost", 0)),
        total_cost=float(raw.get("Total Cost", 0)),
        plan_rows=int(raw.get("Plan Rows", 0)),
        plan_width=int(raw.get("Plan Width", 0)),
        actual_startup_time=float(raw.get("Actual Startup Time", 0)),
        actual_total_time=float(raw.get("Actual Total Time", 0)),
        actual_rows=int(raw.get("Actual Rows", 0)),
        actual_loops=int(raw.get("Actual Loops", 0)),
        shared_hit_blocks=int(raw.get("Shared Hit Blocks", 0)),
        shared_read_blocks=int(raw.get("Shared Read Blocks", 0)),
        children=children,
        extra=extra,
    )


# Pool deduplication registry — shared across Database instances.
# Key: (normalized_url, max_size, min_size) → (pool_handle, ref_count)
# min_size is part of the key so two Database instances that ask for the same
# URL/max_size but a DIFFERENT min_size get their own registry entry instead of
# silently aliasing one pool (the pre-#10 key was (url, max_size) only).
# Thread-safe via _pool_registry_lock.
_pool_registry: dict[tuple[str, int, int], tuple[int, int]] = {}
_pool_registry_lock = threading.Lock()


# Headroom above the Zig HTTP worker thread count. Covers debug
# endpoints, pool heartbeat, pool auto-tuner, background tasks, and
# any Python-side asyncio task that hits the pool without owning a
# worker-thread slot. 8 is comfortable on all deployments and still
# fits PostgreSQL's default max_connections budget when running
# multiple HyperDjango processes per host.
_POOL_HEADROOM = 8
# Absolute floor for computed defaults — matches the old hardcoded
# default and ensures small THREAD_POOL_SIZE values still get a
# sensible pool. Users who want a smaller pool can still pass
# `max_size=N` explicitly.
_POOL_DEFAULT_FLOOR = 32


def _derive_pool_size_from_thread_count() -> int:
    """Compute the default pool max_size from THREAD_POOL_SIZE.

    Returns `max(thread_pool_size + headroom + offload_workers, floor)`.

    * `thread_pool_size` connections cover the worker threads (each pins one
      via the native fast path).
    * `headroom` covers endpoints, the pool heartbeat, the auto-tuner, and
      background tasks that hit the pool without owning a worker slot.
    * `offload_workers` covers the DB offload executor (see
      `db_offload_worker_count`): when a multiplexing loop (shared WS pool /
      reactor) offloads a round-trip, the executor thread pins a connection.
      Folding it into the ceiling keeps that from over-subscribing PostgreSQL.
      It is a ceiling, not an eager allocation — an HTTP-only app never
      creates the executor, so those slots stay unused (no idle connections).

    Falls back to the hardcoded floor if settings are not yet initialized
    (which can happen during very-early module import before conf.py has
    loaded Django / env overrides).
    """
    try:
        # Lazy import — conf.py imports native_json which transitively
        # imports this module during early startup in some test paths.
        from hyperdjango.capacity import resolve_worker_count
        from hyperdjango.conf import get_setting

        # Size for whichever is LARGER: an explicitly-set THREAD_POOL_SIZE, or
        # the worker count the native server auto-scales to on this machine.
        # Each server worker can pin one pool connection, so on a big box where
        # the server auto-scales to (say) 128 workers, a pool sized only from
        # the static setting default (24) would starve DB handlers with an
        # undersized-pool error. Taking the max never shrinks a user's
        # explicit setting and never under-serves the running server.
        thread_pool_size = max(
            int(get_setting("THREAD_POOL_SIZE", _POOL_DEFAULT_FLOOR - _POOL_HEADROOM)),
            resolve_worker_count(),
        )
    # blind-except: pool-size resolution can run during very-early import, before conf.py/Django/env settings are loaded (lazy import above may fail, or the setting may be unparseable); fall back to the hardcoded floor.
    except Exception:
        return _POOL_DEFAULT_FLOOR
    offload_workers = db_offload_worker_count()
    return max(thread_pool_size + _POOL_HEADROOM + offload_workers, _POOL_DEFAULT_FLOOR)


def _acquire_pool(conn_url: str, max_size: int, min_size: int = 2) -> int:
    """Get or create a pool handle. Increments ref count for shared pools.

    ``min_size`` participates in the dedup key so differently-configured pools
    do not alias. NOTE: the native ``_db_configure`` signature ("si|iiLL") has
    NO min_size / prepared_statements / statement_cache_size parameter — the
    native pool is fixed-size — so ``min_size`` shapes only the Python-side
    dedup key here; it is not (and cannot be, without a native ABI change)
    plumbed into the native pool.
    """
    key = (conn_url, max_size, min_size)
    with _pool_registry_lock:
        if key in _pool_registry:
            handle, ref_count = _pool_registry[key]
            _pool_registry[key] = (handle, ref_count + 1)
            return handle
    # Read rotation settings from the conf system (4-tier resolution)
    max_queries = int(_get_setting("POOL_MAX_QUERIES", 0))
    max_lifetime = int(_get_setting("POOL_MAX_LIFETIME", 0))
    # Honor the configured CONNECT_TIMEOUT / QUERY_TIMEOUT (ms) instead of the
    # old hardcoded 10000 / 0, matching db/pgzig_connection.py._create_pool.
    connect_timeout = int(_get_setting("CONNECT_TIMEOUT", 10000) or 10000)
    query_timeout = int(_get_setting("QUERY_TIMEOUT", 0) or 0)
    # Create new pool outside lock (Zig call may block)
    handle = _db_configure(
        conn_url, max_size, connect_timeout, query_timeout, max_queries, max_lifetime
    )
    with _pool_registry_lock:
        # Check again — another thread may have created the same pool
        if key in _pool_registry:
            existing_handle, ref_count = _pool_registry[key]
            _pool_registry[key] = (existing_handle, ref_count + 1)
            # Close the pool we just created (duplicate)
            with suppress(RuntimeError):
                _db_close_pool(handle)
            return existing_handle
        _pool_registry[key] = (handle, 1)
    return handle


def _release_pool(conn_url: str, max_size: int, min_size: int = 2) -> None:
    """Decrement ref count. Close pool when last reference is released.

    ``min_size`` must match the value passed to ``_acquire_pool`` so the same
    registry entry is decremented (the key includes min_size).
    """
    key = (conn_url, max_size, min_size)
    with _pool_registry_lock:
        if key not in _pool_registry:
            return
        handle, ref_count = _pool_registry[key]
        if ref_count <= 1:
            del _pool_registry[key]
            with suppress(RuntimeError):
                _db_close_pool(handle)
        else:
            _pool_registry[key] = (handle, ref_count - 1)


def pool_registry_stats() -> dict[str, int]:
    """Return pool registry statistics: {pools, total_refs}."""
    with _pool_registry_lock:
        total_refs = sum(rc for _, rc in _pool_registry.values())
        return {"pools": len(_pool_registry), "total_refs": total_refs}


def _ensure_url_user(url: str) -> str:
    """Reject a URL that names no database, then complete its auth/host.

    The single connection-URL authority (``conf.resolve_database_url``) decides
    WHICH database and already fills auth/host/port for URLs it returns. This
    reasserts that for URLs handed straight to ``Database(url=...)``: the
    ``PG*``/OS filling is delegated to ``conf.fill_url_auth`` (the sole DB-env
    boundary), so this layer does not read the environment itself.

    A URL that names NO database is rejected: connecting anyway would silently
    fall through to a role-named default database, so a URL "missing only the
    dbname" is a misconfiguration, not something to paper over.
    """
    parsed = urlparse(url)
    dbname = parsed.path.lstrip("/")
    if not dbname:
        raise RuntimeError(
            f"Database URL {url!r} names no database. Include a database name "
            f"(e.g. postgres://host/mydb) or set DATABASE_URL / "
            f"HYPER_DATABASE_URL / PGDATABASE."
        )
    return fill_url_auth(url)


@dataclass(slots=True)
class _ThreadTxState:
    """One thread's transaction state — a plain, thread-owned object.

    Mutations (depth on every BEGIN/COMMIT/SAVEPOINT, callback-list install)
    land HERE and not on the ``threading.local`` itself: under free-threaded
    CPython a ``threading.local`` attribute WRITE serializes process-wide
    (measured 0.18M ops/s across 64 threads vs 158M ops/s for a plain
    attribute on a thread-owned object — the 880x cliff that once collapsed
    the reactor 9x). Reads of the local stay cheap, so each access does one
    local READ (``.state``) and then plain-object attribute traffic."""

    depth: int = 0
    callbacks: list | None = None


class _TxLocal(threading.local):
    """Per-thread transaction state holder. The ONLY local write is the
    one-time ``state`` install per thread; see _ThreadTxState for why."""

    def __init__(self) -> None:
        self.state = _ThreadTxState()


@dataclass(slots=True)
class _TaskTxState:
    """Task-scoped transaction state used on MULTIPLEXING loops.

    A multiplexing loop drives many coroutines on ONE thread, so the thread-
    owned pinned connection + thread-local ``_TxLocal`` depth are shared by every
    coroutine — two concurrent ``transaction()`` blocks would issue each other's
    SAVEPOINTs on one connection and commit/rollback together. This state instead
    lives in a ``ContextVar`` (per asyncio Task), and each transaction OWNS a
    dedicated pool connection for its whole lifetime so its queries route to that
    connection and nothing else.
    """

    depth: int
    conn_handle: int  # raw pinned slot from _db_conn_acquire (>= 0)
    pinned_handle: int  # encoded -(conn_handle + 2) for query routing
    callbacks: list
    # Serializes every operation that touches this transaction's ONE pinned
    # connection. asyncio.Task copies the ContextVar, so a CHILD task spawned
    # inside ``async with db.transaction():`` inherits this SAME state and routes
    # to the SAME connection; each op is offloaded to a SEPARATE executor thread,
    # so without mutual exclusion two sibling tasks (e.g. ``asyncio.gather``) put
    # two commands in flight on one pg connection → wire-protocol desync under
    # free-threading. Held around every pinned-connection op (in-body queries AND
    # BEGIN/COMMIT/SAVEPOINT/RELEASE) plus the ``depth`` mutation. Non-reentrant:
    # no lock-holding path re-enters another lock-taking path in the same task
    # (control statements call ``_run_db_settled`` directly, never ``_run_op``;
    # on_commit callbacks run AFTER the COMMIT lock is released). Bound to the
    # transaction's loop on first acquire (all ops share that one loop thread).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _Once:
    """A one-shot latch: ``first()`` returns True exactly once, then False.

    Loop-thread cleanup (a Future done-callback, a ``finally`` release) can be
    driven more than once for a single pinned-connection checkout under the
    free-threaded scheduler. A duplicate ``_db_conn_release`` of a slot that
    another task has since reclaimed would return that task's LIVE connection to
    the pool. This latch makes such cleanup exactly-once at its call site — the
    idempotency lives in one named primitive instead of ad-hoc flags. Loop
    callbacks are serialized on the loop thread, so no lock is needed.
    """

    _fired: bool = False

    def first(self) -> bool:
        if self._fired:
            return False
        self._fired = True
        return True


# Per-asyncio-Task transaction registry for multiplexing loops. Maps
# ``id(Database)`` → ``_TaskTxState`` so a single Task can hold independent
# transactions on multiple Database instances. ContextVars give each Task its
# own view even though many Tasks share one loop thread; the value is treated
# copy-on-write (a fresh dict per outermost enter) so a child Task spawned mid-
# transaction never mutates its parent's registry.
_tx_context: ContextVar[dict[int, _TaskTxState] | None] = ContextVar(
    "hyperdjango_db_tx", default=None
)


async def ensure_database_exists(db_url: str) -> bool:
    """Create the target database when it does not exist. Returns True when
    this call created it, False when it already existed (or the URL names no
    database to create).

    ``hyper setup`` is the platform's DDL authority, and the database itself
    is DDL: a fresh machine — or any harness pointed at a new app — must not
    need a secret ``createdb`` step before setup can run. Connects to the
    ``postgres`` maintenance database on the same server with the same
    credentials, checks ``pg_database``, and issues ``CREATE DATABASE``.
    Losing a concurrent-create race counts as 'already existed' — the goal
    state is reached either way.
    """
    parts = urlsplit(fill_url_auth(db_url))
    dbname = parts.path.lstrip("/")
    if not dbname:
        return False
    maint = Database(
        urlunsplit(parts._replace(path="/postgres")), min_size=1, max_size=2
    )
    await maint.connect()
    try:
        exists = await maint.query_val(
            "SELECT 1 FROM pg_database WHERE datname = $1", dbname
        )
        if exists is not None:
            return False
        # Deferred import mirrors _classify_call: pgzig_connection imports from
        # this module, so a top-level import here would be circular.
        from hyperdjango.db.pgzig_connection import DatabaseError

        quoted = dbname.replace('"', '""')
        try:
            await maint.execute(f'CREATE DATABASE "{quoted}"')
        except DatabaseError, RuntimeError:
            # Concurrent creator won the race; verify rather than assume.
            exists = await maint.query_val(
                "SELECT 1 FROM pg_database WHERE datname = $1", dbname
            )
            if exists is None:
                raise
            return False
        return True
    finally:
        await maint.disconnect()


class Database:
    """Async database connection pool backed by pg.zig.

    Uses the native Zig PostgreSQL driver for all operations.
    Binary protocol, prepared statement caching, connection pooling.
    """

    def __init__(self, url, min_size=2, max_size: int | None = None):
        self.url = url
        self.min_size = min_size
        # Pool sizing resolution order:
        #
        # 1. Explicit `max_size=N` constructor arg — honored as-is.
        # 2. `POOL_SIZE` setting (HYPER_POOL_SIZE / HYPERDJANGO_POOL_SIZE) —
        #    documented in README, CLAUDE.md, docs/deployment-guide.md, and
        #    every bench script. 0 = auto.
        # 3. Derived from `THREAD_POOL_SIZE + headroom` — pg.zig pins
        #    one connection per Zig HTTP worker thread via the
        #    thread-owned slot fast path in `acquireConnByHandle`.
        #    `pool_size < thread_count` creates a pathological regime
        #    (excess threads block forever in pool.acquire with no wakeup
        #    path). The headroom (_POOL_HEADROOM) covers debug endpoints,
        #    pool heartbeat, auto-tuner, and any background tasks that hit
        #    the pool without owning a worker-thread slot.
        # 4. Fallback to 32 if `get_setting("THREAD_POOL_SIZE")` is
        #    unavailable (e.g., during very-early import before
        #    settings are initialized).
        if max_size is None:
            configured = int(_get_setting("POOL_SIZE", 0) or 0)
            if configured > 0:
                max_size = configured
            else:
                max_size = _derive_pool_size_from_thread_count()
        self.max_size = max_size
        self._pool = None
        self._pool_handle = None
        self._backend = None
        self._conn_url: str | None = None
        # Transaction depth AND the pending on_commit callback list are both
        # thread-local: a callback registered inside one thread's transaction
        # must only ever be fired or discarded by that same thread's
        # COMMIT/ROLLBACK. A process-shared list races catastrophically under
        # free-threading — thread B's COMMIT would fire thread A's not-yet-
        # committed callbacks, and A's ROLLBACK would discard B's pending ones.
        self._tx_local = _TxLocal()
        self._execute_wrappers: list[Callable] = []  # query instrumentation

    @property
    def _tx_depth(self) -> _ThreadTxState:
        """Per-instance, per-thread transaction state (depth + on_commit callbacks).

        Exposed as a lazily-initialized property so the state is *always* present
        on any Database object — including instances built via
        ``Database.__new__`` that never run ``__init__`` (test doubles, pickling).
        Backed by a per-instance ``_tx_local`` stored directly in ``__dict__`` so
        every Database keeps its own thread-local state (a class-level
        ``threading.local`` would be shared across all instances and cross-wire
        their transaction depths under free-threading).
        """
        local = self.__dict__.get("_tx_local")
        if local is None:
            local = _TxLocal()
            self.__dict__["_tx_local"] = local
        return local.state

    async def connect(self):
        """Create or reuse a connection pool via pg.zig.

        Pool deduplication: if another Database instance already created a pool
        with the same connection string and max_size, the existing pool handle
        is shared (ref-counted). This avoids redundant 50-100ms pool creation
        for applications using multiple Database instances with the same URL.
        """
        if self._pool is not None:
            return

        self._conn_url = _ensure_url_user(self.url)
        self._pool_handle = _acquire_pool(self._conn_url, self.max_size, self.min_size)
        self._backend = "pgzig"
        self._pool = True

        # Ensure all connections use UTC timezone — pg.zig returns TIMESTAMPTZ
        # values converted to the session timezone without tzinfo. Setting UTC
        # guarantees the values match what Python stored via datetime.now(UTC).
        _db_execute(self._pool_handle, "SET timezone = 'UTC'", [])

        # Auto-register pgvector OID for native SIMD vector decoding
        _db_register_vector(self._pool_handle)

    async def disconnect(self):
        """Release connection pool reference. Pool is closed when last reference is released."""
        if self._pool_handle is not None and self._conn_url is not None:
            _release_pool(self._conn_url, self.max_size, self.min_size)
        self._pool = None
        self._pool_handle = None
        self._backend = None
        self._conn_url = None

    def _check_pool(self):
        if self._pool is None:
            raise RuntimeError("Database not connected. Call await db.connect() first.")

    @staticmethod
    def _prep_args(args):
        """Prepare query arguments for the native driver.

        Converts Enum instances to their .value so pg.zig receives plain
        Python scalars (str/int/float) instead of Enum wrappers.

        The native param path accepts any sequence (list OR tuple), so the
        overwhelmingly common cases — no params, or params with no Enums —
        return the original ``args`` untouched instead of allocating a fresh
        list and running a per-arg ``isinstance`` on the hot query path.
        """
        if not args:
            return args
        for a in args:
            if isinstance(a, enum.Enum):
                # At least one Enum — materialize the converted list.
                return [x.value if isinstance(x, enum.Enum) else x for x in args]
        return args

    @staticmethod
    def _record_query_stats(sql: str, start_ns: int) -> None:
        """Report query duration to PerformanceMiddleware + RequestProfile
        and bump the native telemetry metrics.

        ONLY called from the slow path of the query methods after the
        fast-path tracking-disabled check has already returned. Inside
        this body we know at least one of (perf middleware, request
        profile, telemetry) is active.
        """
        duration_ns = _time.monotonic_ns() - start_ns
        duration_ms = duration_ns / 1_000_000.0
        perf = _perf_module._perf_middleware
        if perf is not None:
            perf.record_query(sql, duration_ms)
        # Per-request profile integration (profiling.py)
        # dynamic-attr: ``profile`` is set only on threads inside a profiled request; absent (unset) on a threading.local for any other thread
        profile = _prof_module._thread_local.state.profile
        if profile is not None:
            profile.sql_queries.append(_SQLQuery(sql=sql, duration_ns=duration_ns))
            profile.sql_total_ns += duration_ns
        # Native telemetry — zero cost when telemetry disabled. Counter +
        # Histogram each do their own `_enabled` branch, so this is two
        # no-op returns when the master switch is off.
        _db_queries_total.inc(1)
        _db_query_duration_seconds.observe(duration_ns / 1e9)

    def _task_tx(self) -> _TaskTxState | None:
        """The current asyncio Task's transaction state for THIS database, or
        None. Populated only by `transaction()` on multiplexing loops; on every
        other loop (thread-per-request) this is always None and the thread-local
        `_tx_depth` path is used instead."""
        m = _tx_context.get()
        if m is None:
            return None
        return m.get(id(self))

    def in_transaction(self) -> bool:
        """True iff a framework transaction is CURRENTLY open on this
        thread/task for this database.

        This is the single public authority for "am I inside a transaction?"
        It reads exactly the same per-thread / per-task state ``transaction()``
        itself consults — the task-scoped state on multiplexing loops, else the
        thread-local BEGIN/COMMIT nesting depth on single-flow loops. Subsystems
        that must defer work until after COMMIT (query-cache invalidation, the
        batch writer's flush guard) route through here rather than reaching into
        private transaction state.
        """
        if self._task_tx() is not None:
            return True
        return self._tx_depth.depth > 0

    def _effective_handle(self) -> int:
        """Pool handle to route a query to.

        Inside a task-scoped (multiplexing-loop) transaction, returns the
        NEGATIVE-encoded handle of that transaction's dedicated pinned
        connection so the query runs on it (and sees its uncommitted writes).
        Otherwise the shared pool handle (thread-owned-slot fast path)."""
        state = self._task_tx()
        if state is not None:
            return state.pinned_handle
        return self._pool_handle

    async def _run_op(self, op: Callable[[], _T]) -> _T:
        """Run a blocking native DB op with the right event-loop semantics.

        * Inside a THREAD-LOCAL (single-flow-loop) transaction, run INLINE: that
          transaction pins the loop THREAD's pool connection, so the op must stay
          on this thread — offloading to an executor thread would use a different
          pooled connection (a separate autocommit session) and silently drop the
          write from the transaction.
        * Otherwise offload on multiplexing loops (inline elsewhere) via
          `_run_db_blocking`. This covers both plain queries AND task-scoped
          transactions: a task-scoped op already targets its transaction's
          dedicated connection by explicit handle (`_effective_handle`), so
          offloading it is safe — and keeps the multiplexing loop responsive.
          A single Task's queries are await-serialized, so its connection is
          never touched concurrently.
        """
        state = self._task_tx()
        # _TxLocal declares `depth: int = 0` as a class default, so direct access
        # is always safe (returns 0 on threads that never opened a transaction).
        if state is None and self._tx_depth.depth > 0:
            return op()
        if state is not None:
            # Task-scoped (multiplexing) transaction: this op runs on the pinned
            # connection. Hold the per-transaction lock so a SIBLING task sharing
            # this state (child Task spawned inside the block, e.g. via
            # asyncio.gather) can't run its own op on the SAME pinned connection
            # concurrently — that would put two commands in flight on one pg
            # connection → wire desync under free-threading. Then use the
            # settle-shield so a cancellation mid-query can't resume into the
            # transaction's inline cleanup (ROLLBACK + connection release) while
            # the executor thread is STILL mid-query on that same connection.
            # The lock is non-reentrant but safe: ``op`` is a native call that
            # never re-enters ``_run_op``, and the tx control statements take the
            # lock separately (never nested under this one in the same task).
            async with state.lock:
                return await self._run_db_settled(op)
        return await _run_db_blocking(op)

    @staticmethod
    def _compose_execute_wrapper(wrapper: Callable, inner: Callable) -> Callable:
        """Bind one execute_wrapper around an inner ``execute(sql, params)``
        callable (factory avoids the late-binding closure trap in a loop)."""

        def wrapped(sql, params):
            return wrapper(inner, sql, params)

        return wrapped

    def _apply_execute_wrappers(
        self, sql: str, params, native_call: Callable[[], _T]
    ) -> Callable[[], _T]:
        """Compose the active ``execute_wrapper`` callbacks around the native
        round-trip. Each wrapper is invoked as ``wrapper(execute, sql, params)``
        and must call ``execute(sql, params)`` to proceed; the innermost
        ``execute`` performs the real query. The last-registered wrapper is
        outermost (matches the enter/exit stack in `execute_wrapper`). Returns
        ``native_call`` unchanged when none are registered (zero overhead)."""
        wrappers = self._execute_wrappers
        if not wrappers:
            return native_call

        def base_execute(_sql, _params):
            return native_call()

        call = base_execute
        for wrapper in wrappers:  # first registered → innermost, last → outermost
            call = self._compose_execute_wrapper(wrapper, call)
        return lambda: call(sql, params)

    async def _run_tracked(self, sql, native_call: Callable[[], _T], params=()) -> _T:
        """Execute a blocking native DB round-trip with the standard
        track/no-track stats wrapper, offloaded off multiplexing loops.

        `native_call` is a zero-arg callable performing the FFI round-trip.
        The stats wrapper (when a consumer is watching — see
        `_should_track_query`) is applied *inside* the offloaded op so the
        recorded duration measures the round-trip itself, not executor queue
        time. On a dedicated loop, or inside a transaction, it runs inline.

        Any active `execute_wrapper()` callbacks are composed around the native
        call first (so they see the real ``sql``/``params`` and can time, log,
        or block the query), then the stats timer wraps that.
        """
        # Unify the error taxonomy at the native boundary: a PostgreSQL failure
        # from the raw FFI call surfaces as the typed hierarchy (see module
        # docstring) — identical to the psycopg-compat cursor path. Wrap the RAW
        # call so classification sits INSIDE any execute_wrapper: a wrapper that
        # deliberately raises RuntimeError is left untouched.
        native_call = _classify_call(native_call)
        if self._execute_wrappers:
            native_call = self._apply_execute_wrappers(sql, params, native_call)
        if not _should_track_query():
            # Fast path: no observer is watching — no timestamp, no stats.
            return await self._run_op(native_call)

        def _op() -> _T:
            start = _time.monotonic_ns()
            try:
                return native_call()
            finally:
                self._record_query_stats(sql, start)

        return await self._run_op(_op)

    async def query(self, sql, *args):
        """Execute a query and return all rows as dicts.

        Uses native dict building: dicts are constructed in Zig with pre-interned
        column name keys. Zero per-row string allocation, no Python-side zip/dict.
        """
        self._check_pool()
        prep = self._prep_args(args)
        handle = self._effective_handle()
        qh = _query_handle(sql)
        return await self._run_tracked(
            sql, lambda: _db_query_dicts(handle, sql, prep, qh), prep
        )

    async def query_tuples(self, sql, *args):
        """Execute a query and return all rows as tuples.

        Lower-level than query() — returns raw tuples without column names.
        Used by .values_list(flat=True) and internal code that doesn't need dicts.
        """
        self._check_pool()
        prep = self._prep_args(args)
        handle = self._effective_handle()
        return await self._run_tracked(sql, lambda: _db_query(handle, sql, prep), prep)

    async def query_json(self, sql, *args) -> bytes:
        """Execute a query and return results as a JSON bytes array.

        Builds JSON directly in Zig from PostgreSQL binary wire protocol.
        No Python dict creation, no json.dumps. Returns bytes like:
        b'[{"id":1,"name":"Alice"},{"id":2,"name":"Bob"}]'

        Ideal for REST API responses where the result goes straight to HTTP.
        """
        self._check_pool()
        prep = self._prep_args(args)
        handle = self._effective_handle()
        return await self._run_tracked(
            sql, lambda: _db_query_json(handle, sql, prep), prep
        )

    async def query_one(self, sql, *args):
        """Execute a query and return a single row as dict, or None."""
        self._check_pool()
        prep = self._prep_args(args)
        handle = self._effective_handle()
        qh = _query_handle(sql)
        rows = await self._run_tracked(
            sql, lambda: _db_query_dicts(handle, sql, prep, qh), prep
        )
        return rows[0] if rows else None

    async def query_val(self, sql, *args):
        """Execute a query and return a single scalar value."""
        self._check_pool()
        prep = self._prep_args(args)
        handle = self._effective_handle()
        raw_rows = await self._run_tracked(
            sql, lambda: _db_query(handle, sql, prep), prep
        )
        if raw_rows and len(raw_rows[0]) > 0:
            return raw_rows[0][0]
        return None

    async def execute(self, sql, *args) -> int:
        """Execute a statement (INSERT, UPDATE, DELETE) and return the affected-row count.

        Returns the number of rows affected as a plain ``int`` — e.g. ``1`` for a
        single-row INSERT, ``5`` for an UPDATE that touched five rows, ``0`` when
        nothing matched. The native ``_db_execute`` already hands back that integer
        (parsed from PostgreSQL's command tag), so we return it directly.

            deleted = await db.execute("DELETE FROM sessions WHERE expires_at < NOW()")
            if deleted:
                logger.info("purged %d expired sessions", deleted)
        """
        self._check_pool()
        prep = self._prep_args(args)
        handle = self._effective_handle()
        affected = int(
            await self._run_tracked(sql, lambda: _db_execute(handle, sql, prep), prep)
        )
        # The pgvector type OID only exists in pg_type *after* the extension is
        # installed. ``connect()`` registers it eagerly, but a program that runs
        # ``CREATE EXTENSION vector`` after connecting (the common case: a fresh
        # database, a migration, or a test) would otherwise leave the OID at 0 —
        # and every vector column would decode to None instead of list[float].
        # Re-register the moment such a statement runs so vector values decode
        # correctly regardless of connect-vs-CREATE ordering.
        if _stmt_creates_vector_extension(sql):
            _db_register_vector(handle)
        return affected

    async def explain(
        self,
        sql: str,
        *args: object,
        analyze: bool = False,
        buffers: bool = False,
        verbose: bool = False,
    ) -> ExplainResult:
        """Run EXPLAIN on a query and return structured results.

        Returns an ExplainResult with the parsed plan tree, execution time,
        and convenience properties for checking index usage.

        Args:
            sql: The SQL query to explain.
            *args: Query parameters.
            analyze: If True, actually execute the query (EXPLAIN ANALYZE).
            buffers: If True, include buffer usage stats (requires analyze=True).
            verbose: If True, include verbose output.

        Usage:
            # Basic plan (no execution)
            result = await db.explain("SELECT * FROM users WHERE id = $1", 1)
            print(result.plan.node_type)  # "Index Scan"

            # With execution timing
            result = await db.explain("SELECT * FROM users", analyze=True, buffers=True)
            print(result.execution_time)     # 0.042 (ms)
            print(result.has_seq_scan)       # True
            print(result.seq_scan_tables)    # ["users"]

            # Assert performance in tests
            result = await db.explain(query, analyze=True)
            assert not result.has_seq_scan, f"Seq scan on: {result.seq_scan_tables}"
            assert result.execution_time < 5.0, f"Too slow: {result.execution_time}ms"
        """
        self._check_pool()

        # Build EXPLAIN options
        options = ["FORMAT JSON"]
        if analyze:
            options.append("ANALYZE")
        if buffers:
            options.append("BUFFERS")
        if verbose:
            options.append("VERBOSE")
        options_str = ", ".join(options)

        # Run EXPLAIN with JSON format for structured parsing
        explain_sql = f"EXPLAIN ({options_str}) {sql}"
        handle = self._effective_handle()
        explain_args = list(args)
        json_rows = await self._run_op(
            _classify_call(lambda: _db_query(handle, explain_sql, explain_args))
        )

        # PostgreSQL returns EXPLAIN JSON as a single row with a single column
        # containing the JSON array as a string
        raw_json = json_rows[0][0]
        plan_data = fast_json_loads(raw_json) if isinstance(raw_json, str) else raw_json

        # plan_data is a list with one element (the top-level plan object)
        top = plan_data[0] if plan_data else {}
        plan_raw = top.get("Plan", {})
        plan_node = _parse_plan_node(plan_raw) if plan_raw else None

        planning_time = float(top.get("Planning Time", 0))
        execution_time = float(top.get("Execution Time", 0))

        # Also get the text format for human-readable output
        text_options = []
        if analyze:
            text_options.append("ANALYZE")
        if buffers:
            text_options.append("BUFFERS")
        if verbose:
            text_options.append("VERBOSE")
        if text_options:
            text_sql = f"EXPLAIN ({', '.join(text_options)}) {sql}"
        else:
            text_sql = f"EXPLAIN {sql}"
        text_rows = await self._run_op(
            _classify_call(lambda: _db_query(handle, text_sql, explain_args))
        )
        text = "\n".join(
            list(row.values())[0] if isinstance(row, dict) else row[0]
            for row in text_rows
        )

        return ExplainResult(
            text=text,
            plan=plan_node,
            planning_time=planning_time,
            execution_time=execution_time,
            analyzed=analyze,
        )

    async def execute_many(self, sql, args_list):
        """Execute a statement with many parameter sets.

        Uses native batched wire protocol: Parse+Describe once, then N×(Bind+Execute)
        with 256KB flush batches. 10-100x faster than individual execute() calls
        for bulk INSERT/UPDATE operations.
        """
        self._check_pool()
        # Defensive copy per row: `_prep_args` may return the CALLER's own list
        # unchanged (no-Enum fast path), and `_run_op` defers the native write
        # to an executor thread — a caller mutating its arg_set list mid-flight
        # would otherwise change what gets written. `list(...)` snapshots each
        # set now. (Per-query `*args` are fresh tuples, so only the batch path
        # needs this.)
        rows = [list(self._prep_args(arg_set)) for arg_set in args_list]
        if rows:
            handle = self._effective_handle()
            native_call = self._apply_execute_wrappers(
                sql, rows, _classify_call(lambda: _db_exec_many(handle, sql, rows))
            )
            await self._run_op(native_call)

    async def pipeline(self, queries: list[str]) -> list[list[tuple]]:
        """Execute N queries in a single pipeline (one network round-trip)."""
        self._check_pool()
        handle = self._effective_handle()
        return await self._run_op(_classify_call(lambda: _db_pipeline(handle, queries)))

    async def copy_from(
        self, table: str, columns: list[str], rows: list[list[str]]
    ) -> int:
        """Bulk insert rows using PostgreSQL COPY FROM STDIN protocol.

        42.8x faster than individual INSERTs for large datasets. Each row is
        sent as tab-separated text via the COPY wire protocol — no per-row
        round-trips.

        Acquires a pinned connection for the COPY operation and releases it
        when complete.

        Args:
            table: Target table name.
            columns: Column names (e.g., ["id", "name", "email"]).
            rows: List of rows, each row a list of string values.

        Returns:
            Number of rows copied.

        Usage:
            count = await db.copy_from("users", ["name", "email"], [
                ["Alice", "alice@example.com"],
                ["Bob", "bob@example.com"],
            ])
        """
        self._check_pool()
        col_list = ", ".join(columns)
        copy_sql = f"COPY {table} ({col_list}) FROM STDIN"
        # Native expects list of tab-separated row strings with trailing newline
        text_rows = ["\t".join(str(v) for v in row) + "\n" for row in rows]

        state = self._task_tx()
        if state is not None:
            # Inside a task-scoped (multiplexing) transaction: COPY must run on
            # the transaction's OWN pinned connection — its raw slot handle —
            # so the bulk insert participates in the transaction (rolls back
            # with it, sees its uncommitted rows) instead of landing on a
            # separate autocommit connection. `_run_op` holds the tx lock and
            # settle-shield (the raw slot is `state.conn_handle`, which
            # `_db_copy_from`'s `pinnedGet` resolves directly).
            return await self._run_op(
                _classify_call(
                    lambda: _db_copy_from(state.conn_handle, copy_sql, text_rows)
                )
            )

        # No task-scoped transaction: dedicated, non-pinned connection.
        # acquire + COPY + release all run on ONE thread, so the whole block is
        # offloaded as a unit — bulk COPY must not stall a multiplexing loop for
        # its full duration. (A THREAD-LOCAL single-flow transaction cannot be
        # joined here: `_db_copy_from` requires a raw pinned slot and the
        # thread-owned tx connection is not one — a native-API limitation.)
        pool_handle = self._pool_handle

        def _op() -> int:
            conn_handle = _db_conn_acquire(pool_handle)
            try:
                return _db_copy_from(conn_handle, copy_sql, text_rows)
            finally:
                _db_conn_release(conn_handle)

        return await _run_db_blocking(_classify_call(_op))

    async def copy_to(self, sql: str) -> list[str]:
        """Export rows using PostgreSQL COPY TO STDOUT protocol.

        Acquires a pinned connection for the COPY operation and releases it
        when complete.

        Args:
            sql: COPY SQL statement (e.g., "COPY users TO STDOUT").

        Returns:
            List of tab-separated row strings.

        Usage:
            rows = await db.copy_to("COPY users TO STDOUT")
            for row in rows:
                name, email = row.split("\\t")
        """
        self._check_pool()

        state = self._task_tx()
        if state is not None:
            # Inside a task-scoped (multiplexing) transaction: export from the
            # transaction's OWN pinned connection so it sees the transaction's
            # uncommitted rows (raw slot; `_run_op` holds the tx lock + shield).
            return await self._run_op(
                _classify_call(lambda: _db_copy_to(state.conn_handle, sql))
            )

        # No task-scoped transaction: dedicated, non-pinned connection (see the
        # copy_from note re: the thread-local single-flow tx native limitation).
        pool_handle = self._pool_handle

        def _op() -> list[str]:
            conn_handle = _db_conn_acquire(pool_handle)
            try:
                return _db_copy_to(conn_handle, sql)
            finally:
                _db_conn_release(conn_handle)

        return await _run_db_blocking(_classify_call(_op))

    async def server_cursor(
        self, sql: str, params: list[object] | None = None, page_size: int = 100
    ) -> DatabaseServerCursor:
        """Create a real PostgreSQL server-side cursor for large result streaming.

        Acquires a pinned connection, sends BEGIN + DECLARE CURSOR, and returns
        a cursor object that can FETCH pages. The cursor MUST be closed when done
        (use as async context manager).

        This pins a pool connection for the cursor's lifetime. Use sparingly —
        each active cursor consumes one pool slot.

        Usage:
            async with await db.server_cursor("SELECT * FROM big_table WHERE status = $1", ["active"]) as cursor:
                while True:
                    rows = await cursor.fetch_page()
                    if not rows:
                        break
                    process(rows)
        """
        self._check_pool()

        # Inside an existing transaction (task-scoped multiplexing OR thread-
        # local single-flow), the cursor must live on the transaction's OWN
        # connection so FETCH sees its uncommitted rows and the cursor is
        # discarded when the transaction rolls back — NOT on a separate
        # autocommit connection with its own BEGIN/COMMIT. `_effective_handle`
        # resolves to the pinned handle (task-scoped) or the thread-owned pool
        # handle (thread-local); both are honoured by `_db_query`/`_db_execute`
        # via `acquireConnByHandle`. DECLARE only (no BEGIN — already open);
        # `_run_op` serializes it under the tx lock (task-scoped) or runs it
        # inline (thread-local). The surrounding transaction owns COMMIT/ROLLBACK
        # and the connection release, so the returned cursor is "borrowed".
        if self.in_transaction():
            handle = self._effective_handle()
            cursor_name = f"hyper_sc_{_uuid.uuid4().hex[:12]}"
            declare_sql = f'DECLARE "{cursor_name}" CURSOR FOR {sql}'
            # A failed DECLARE aborts the caller's transaction; let it propagate
            # (their transaction() block rolls back) — do NOT release the
            # connection here, we do not own it.
            await self._run_op(
                _classify_call(lambda: _db_execute(handle, declare_sql, params or []))
            )
            return DatabaseServerCursor(
                cursor_name=cursor_name,
                conn_handle=-1,  # not owned — never released by the cursor
                pool_handle=self._pool_handle,
                page_size=page_size,
                _db_query=_db_query,
                _db_conn_execute=_db_conn_execute,
                _db_conn_release=_db_conn_release,
                _get_last_columns=_db_get_last_columns,
                query_handle=handle,
                owns_connection=False,
                _db_execute=_db_execute,
            )

        # Acquire pinned connection from pool
        conn_handle = _db_conn_acquire(self._pool_handle)

        # BEGIN + DECLARE must be wrapped: until DatabaseServerCursor exists,
        # this pinned connection has no owner to release it. If DECLARE raises
        # (bad SQL, missing table), the slot would leak and repeats drain the
        # pool. Roll back and release here, then re-raise.
        try:
            # Begin transaction (required for DECLARE CURSOR). Classify the raw
            # FFI error so a bad DECLARE (missing table, syntax error) raises the
            # SAME typed exception here on the autocommit path as it does inside a
            # transaction — one taxonomy regardless of the cursor's tx context.
            _classify_call(lambda: _db_conn_execute(conn_handle, "BEGIN", []))()

            # Generate unique cursor name
            cursor_name = f"hyper_sc_{_uuid.uuid4().hex[:12]}"

            # Build DECLARE CURSOR SQL
            declare_sql = f'DECLARE "{cursor_name}" CURSOR FOR {sql}'
            _classify_call(
                lambda: _db_conn_execute(conn_handle, declare_sql, params or [])
            )()
        except BaseException:
            # Release the pinned connection, but never let a cleanup failure
            # mask the original BEGIN/DECLARE error — suppress both cleanup steps.
            with suppress(Exception):
                _db_conn_execute(conn_handle, "ROLLBACK", [])
            with suppress(Exception):
                _db_conn_release(conn_handle)
            raise

        return DatabaseServerCursor(
            cursor_name=cursor_name,
            conn_handle=conn_handle,
            pool_handle=self._pool_handle,
            page_size=page_size,
            _db_query=_db_query,
            _db_conn_execute=_db_conn_execute,
            _db_conn_release=_db_conn_release,
            _get_last_columns=_db_get_last_columns,
        )

    def on_commit(self, callback: object) -> None:
        """Register a callback to run after the current transaction commits.

        Callbacks are called in registration order after COMMIT. If the
        transaction rolls back, callbacks are discarded.

        Can be used as a decorator or called directly:

            db.on_commit(lambda: print("committed!"))

            @db.on_commit
            def after_commit():
                send_notification()

        Args:
            callback: Callable (sync or async) to run after commit.
        """
        self._get_on_commit_callbacks().append(callback)
        return callback  # allow use as decorator

    def _get_on_commit_callbacks(self) -> list[object]:
        """Return the pending on_commit callback list for the current scope.

        Inside a task-scoped (multiplexing-loop) transaction the list lives on
        the ``_TaskTxState`` so it shares that transaction's TASK affinity —
        consumed only by that task's own COMMIT/ROLLBACK. Otherwise it lives on
        the thread-local ``_tx_depth`` (single-flow loops): created lazily on
        first registration, consumed only by that thread's COMMIT/ROLLBACK.
        Never shared across tasks or threads.
        """
        state = self._task_tx()
        if state is not None:
            return state.callbacks
        callbacks = self._tx_depth.callbacks
        if callbacks is None:
            callbacks = []
            self._tx_depth.callbacks = callbacks
        return callbacks

    async def _run_on_commit_callbacks(self) -> None:
        """Execute and clear the current scope's on_commit callbacks."""
        callbacks = self._get_on_commit_callbacks()
        pending = callbacks[:]
        callbacks.clear()
        for cb in pending:
            if inspect.iscoroutinefunction(cb):
                await cb()
            else:
                cb()

    @asynccontextmanager
    async def transaction(
        self, savepoint_name: str | None = None, *, isolation_level: str | None = None
    ):
        """Context manager for a database transaction with savepoint support.

        Supports nesting: outer call uses BEGIN/COMMIT, inner calls use
        SAVEPOINT/RELEASE SAVEPOINT. Inner rollback only affects the savepoint,
        not the outer transaction.

        ``isolation_level`` (outermost transaction only) selects the SQL isolation
        level — ``"read_committed"`` (PostgreSQL default), ``"repeatable_read"``,
        or ``"serializable"`` — for operations that need protection from phantom
        reads / write skew (balances, inventory, bookings). It is refused on a
        nested (savepoint) transaction, which inherits the outer level.

        on_commit() callbacks are executed after the outermost COMMIT.

        Usage:
            async with db.transaction():          # BEGIN
                await db.execute("INSERT ...")
                db.on_commit(lambda: print("done!"))
                async with db.transaction():      # SAVEPOINT sp_2
                    await db.execute("INSERT ...")
                # RELEASE SAVEPOINT sp_2
            # COMMIT → on_commit callbacks run here

            async with db.transaction(isolation_level="serializable"):
                await db.execute("UPDATE accounts SET ...")
        """
        self._check_pool()

        # On a MULTIPLEXING loop (shared WS pool / reactor) many coroutines run
        # on one thread and share the thread's pinned pool connection + the
        # thread-local `_tx_depth` — so two concurrent transactions would issue
        # each other's SAVEPOINTs on one connection. There we run a TASK-scoped
        # transaction that owns its OWN connection (see `_transaction_multiplexed`).
        # On a single-flow loop the thread-local path is correct and connection-
        # efficient, so it is preserved byte-for-byte below.
        loop = asyncio.get_running_loop()
        # dynamic-attr: reading the framework marker (name held in a module constant) that may have been injected onto a foreign asyncio event-loop object
        if getattr(loop, _MULTIPLEXING_LOOP_ATTR, False):
            async with self._transaction_multiplexed(
                savepoint_name, isolation_level=isolation_level
            ) as db:
                yield db
            return

        # Track nesting depth per-thread (_TxLocal defaults .depth to 0).
        depth = self._tx_depth.depth

        if depth == 0:
            # Outermost: real transaction (optionally at a chosen isolation level).
            # Control statements classify too (a COMMIT can fail with a
            # serialization/deadlock error) so the whole transaction speaks the
            # unified taxonomy, not just the queries inside it.
            _classify_call(
                lambda: _db_execute(
                    self._pool_handle, _begin_statement(isolation_level), []
                )
            )()
            self._tx_depth.depth = 1
            try:
                yield self
                _classify_call(lambda: _db_execute(self._pool_handle, "COMMIT", []))()
                # Run on_commit callbacks after successful COMMIT
                await self._run_on_commit_callbacks()
            except BaseException:
                # BaseException (not just Exception) so asyncio.CancelledError
                # — client disconnect / request timeout mid-transaction — still
                # rolls back. Without it the cancelled writes stay on the pinned
                # connection and the NEXT transaction on this loop thread commits
                # them silently (proven: "idle in transaction" + committed
                # cancelled INSERT).
                # Discard on_commit callbacks on rollback (this thread's only).
                self._get_on_commit_callbacks().clear()
                with suppress(Exception):
                    _db_execute(self._pool_handle, "ROLLBACK", [])
                raise
            finally:
                self._tx_depth.depth = 0
        else:
            # Nested: use savepoint. Isolation level is a property of the whole
            # transaction and can't be changed at a savepoint — refuse it here so
            # a caller isn't misled into thinking a nested block runs stricter.
            if isolation_level is not None:
                raise ValueError(
                    "isolation_level can only be set on the outermost transaction, "
                    "not a nested (savepoint) block"
                )
            sp_name = savepoint_name or f"sp_{depth + 1}"
            _classify_call(
                lambda: _db_execute(self._pool_handle, f"SAVEPOINT {sp_name}", [])
            )()
            self._tx_depth.depth = depth + 1
            try:
                yield self
                _classify_call(
                    lambda: _db_execute(
                        self._pool_handle, f"RELEASE SAVEPOINT {sp_name}", []
                    )
                )()
            except BaseException:
                # BaseException so CancelledError rolls the savepoint back too
                # (see the outer arm) rather than leaving it half-applied.
                with suppress(Exception):
                    _db_execute(
                        self._pool_handle, f"ROLLBACK TO SAVEPOINT {sp_name}", []
                    )
                raise
            finally:
                self._tx_depth.depth = depth

    @staticmethod
    async def _run_db_settled[T](fn: Callable[[], T]) -> T:
        """Run a blocking DB op on the executor, guaranteeing it has SETTLED
        before this coroutine returns OR propagates — even when the Task is
        cancelled mid-flight.

        The multiplexed-transaction control statements (BEGIN/COMMIT/SAVEPOINT/
        RELEASE) all run on a single PINNED pg connection whose error-path
        cleanup (ROLLBACK, RELEASE SAVEPOINT, connection release) runs INLINE
        and synchronously. A plain ``await _run_db_blocking(...)`` returns to
        that inline cleanup the instant the Task is cancelled — while the DB
        executor thread is STILL running the abandoned statement on the very
        same connection. Two commands in flight on one pg connection under
        no-GIL desync the wire protocol.

        This wrapper shields the offloaded op and, if a cancellation lands
        while it is still running, keeps waiting for it to settle before
        re-raising the cancellation. By the time control reaches any inline
        cleanup the pinned connection is provably idle, so the handoff is
        single-writer.

        UNBOUNDED-HANG WARNING: because the shield deliberately absorbs
        cancellation and re-waits until the op settles, a round-trip that never
        completes (server wedged, network black hole) hangs the awaiting
        coroutine FOREVER — the cancellation cannot abandon it, by design. The
        only bound is a server-side statement timeout. Set ``QUERY_TIMEOUT``
        (milliseconds) in the DB settings so every offloaded statement is
        guaranteed to error out (and thus settle) within a bounded time; it
        defaults to 0 (no timeout) and is intentionally left at the caller's
        discretion here so legitimate long queries/migrations are not killed —
        deployments that value liveness over long-query tolerance MUST set it.
        """
        fut = asyncio.ensure_future(_run_db_blocking(fn))
        cancelled: BaseException | None = None
        while True:
            try:
                result = await asyncio.shield(fut)
                break
            except asyncio.CancelledError as exc:
                # Our Task was cancelled (shield keeps ``fut`` itself alive).
                cancelled = exc
                if fut.done():
                    break
                # Op still touching the pinned connection — absorb the cancel
                # and loop back to wait for it to finish before we let cleanup
                # run. Never leave the connection with an in-flight command.
                continue
        if cancelled is not None:
            raise cancelled
        return result

    @asynccontextmanager
    async def _transaction_multiplexed(
        self, savepoint_name: str | None = None, *, isolation_level: str | None = None
    ):
        """Task-scoped transaction for multiplexing loops.

        The outermost enter ACQUIRES A DEDICATED pool connection and pins it for
        the whole block; every query in the block routes to it via
        `_effective_handle` (task-scoped state in `_tx_context`), so concurrent
        transactions on the same loop can never touch each other's connection or
        nesting depth. Nested enters use SAVEPOINTs on that same connection.
        The connection acquire and BEGIN/COMMIT/SAVEPOINT/RELEASE are offloaded
        to the DB executor so they don't stall the loop. Error-path cleanup
        (ROLLBACK, connection release) runs INLINE and synchronously — a native
        call, not an ``await`` — so a cancellation propagating through the block
        can't re-raise inside cleanup and mask the original exception.
        """
        state = self._task_tx()
        if state is not None:
            # Nested: SAVEPOINT on this transaction's own pinned connection.
            # Isolation level belongs to the whole transaction — refuse it here
            # (mirrors the thread-local path) rather than silently ignore it.
            if isolation_level is not None:
                raise ValueError(
                    "isolation_level can only be set on the outermost transaction, "
                    "not a nested (savepoint) block"
                )
            pinned = state.pinned_handle
            # Hold the transaction lock across BOTH the depth read (for the
            # default savepoint name) AND the increment: two sibling tasks
            # entering a nested block concurrently would otherwise both read the
            # same ``depth`` and pick the same ``sp_N`` name, then both increment
            # → colliding SAVEPOINTs / corrupted depth. ``sp_name`` is finalized
            # here so the except/finally arms below can reference it.
            sp_name = savepoint_name
            async with state.lock:
                if sp_name is None:
                    sp_name = f"sp_{state.depth + 1}"
                # Settle the SAVEPOINT before releasing the lock (and before
                # propagating any cancellation): an abandoned in-flight SAVEPOINT
                # would race the outer transaction's inline ROLLBACK on this same
                # pinned connection.
                await self._run_db_settled(
                    _classify_call(
                        lambda: _db_execute(pinned, f"SAVEPOINT {sp_name}", [])
                    )
                )
                state.depth += 1
            try:
                yield self
                # Settle RELEASE before the except-path ROLLBACK TO SAVEPOINT
                # below can touch the same connection under cancellation. Under
                # the lock so a sibling op can't be mid-flight on the connection.
                async with state.lock:
                    await self._run_db_settled(
                        _classify_call(
                            lambda: _db_execute(
                                pinned, f"RELEASE SAVEPOINT {sp_name}", []
                            )
                        )
                    )
            except BaseException:
                with suppress(Exception):
                    _db_execute(pinned, f"ROLLBACK TO SAVEPOINT {sp_name}", [])
                raise
            finally:
                # Plain decrement (no lock): it derives nothing and is atomic on
                # the single loop thread. Keeping it await-free means a
                # cancellation propagating through cleanup can't strand ``depth``
                # one too high — mirrors the original synchronous restore.
                state.depth -= 1
            return

        # Outermost: acquire a dedicated connection and BEGIN on it.
        pool_handle = self._pool_handle

        # Acquire + BEGIN as ONE offloaded op that owns its own cleanup: if BEGIN
        # fails it releases the connection internally and only ever returns a
        # handle once the connection is fully pinned AND a transaction is open.
        def _acquire_and_begin() -> int:
            h = _db_conn_acquire(pool_handle)
            try:
                _classify_call(
                    lambda: _db_execute(-(h + 2), _begin_statement(isolation_level), [])
                )()
            except BaseException:
                with suppress(Exception):
                    _db_conn_release(h)
                raise
            return h

        # CANCELLATION SAFETY: the acquire is offloaded to the DB executor and
        # runs to completion even if THIS Task is cancelled while awaiting it —
        # so a naive `await` would pin a connection whose handle we then throw
        # away (leak). Shield the op so cancellation can't abandon it, and if we
        # are cancelled anyway, register a done-callback that rolls back and
        # releases the connection once the shielded op settles. Cleanup runs
        # inline (native, no await) — consistent with the error-path policy in
        # this method's docstring.
        begin_fut = asyncio.ensure_future(_run_db_blocking(_acquire_and_begin))
        try:
            conn_handle = await asyncio.shield(begin_fut)
        except BaseException:
            orphan_once = _Once()

            def _release_orphan(fut: asyncio.Future[int]) -> None:
                # Only a fully-successful acquire+BEGIN leaves a connection we
                # own; a failed op already released it internally. And release
                # exactly once (orphan_once): under the free-threaded scheduler
                # this cleanup can be driven more than once, and a duplicate
                # release of a since-reused pinned slot would return ANOTHER
                # task's live connection to the pool (a "steal").
                if (
                    fut.cancelled()
                    or fut.exception() is not None
                    or not orphan_once.first()
                ):
                    return
                h = fut.result()
                with suppress(Exception):
                    _db_execute(-(h + 2), "ROLLBACK", [])
                with suppress(Exception):
                    _db_conn_release(h)

            begin_fut.add_done_callback(_release_orphan)
            raise
        pinned = -(conn_handle + 2)  # encode for acquireConnByHandle routing

        state = _TaskTxState(
            depth=1,
            conn_handle=conn_handle,
            pinned_handle=pinned,
            callbacks=[],
        )
        # Copy-on-write the registry so a child Task spawned mid-transaction
        # can't mutate this Task's (or its parent's) view.
        current = _tx_context.get()
        registry = dict(current) if current else {}
        registry[id(self)] = state
        token = _tx_context.set(registry)
        # Exactly-once release for the success/finally path too — same no-GIL
        # duplicate-dispatch hazard as the orphan callback above.
        release_once = _Once()
        try:
            yield self
            # Settle the COMMIT before the except-path inline ROLLBACK (and the
            # finally-path connection release) can run: under cancellation an
            # unshielded COMMIT await would resume into that cleanup while the
            # executor thread was still committing on this same pinned
            # connection → two concurrent commands → protocol desync. Under the
            # lock so a sibling task's op can't be in flight on the connection
            # while we COMMIT.
            async with state.lock:
                await self._run_db_settled(
                    _classify_call(lambda: _db_execute(pinned, "COMMIT", []))
                )
            # Run on_commit callbacks after the successful COMMIT (token still
            # set, so `_get_on_commit_callbacks` resolves to this state's list).
            # OUTSIDE the lock: a callback may itself issue db.query on this same
            # transaction, which re-takes the (non-reentrant) lock — holding it
            # here would self-deadlock.
            await self._run_on_commit_callbacks()
        except BaseException:
            # Discard this transaction's on_commit callbacks on rollback.
            self._get_on_commit_callbacks().clear()
            with suppress(Exception):
                _db_execute(pinned, "ROLLBACK", [])
            raise
        finally:
            _tx_context.reset(token)
            if release_once.first():
                with suppress(Exception):
                    _db_conn_release(conn_handle)

    @contextmanager
    def execute_wrapper(self, wrapper: Callable):
        """Context manager for query instrumentation.

        The wrapper function is called for every query executed while
        the context manager is active. It receives (execute, sql, params)
        and must call execute(sql, params) to proceed.

        Usage:
            def log_queries(execute, sql, params):
                start = time.perf_counter()
                result = execute(sql, params)
                elapsed = time.perf_counter() - start
                print(f"{elapsed*1000:.1f}ms: {sql}")
                return result

            with db.execute_wrapper(log_queries):
                await db.query("SELECT * FROM users")

            # Block all queries (useful in tests)
            def block_queries(execute, sql, params):
                raise RuntimeError(f"Unexpected query: {sql}")

            with db.execute_wrapper(block_queries):
                # Any query here raises RuntimeError
                ...
        """
        self._execute_wrappers.append(wrapper)
        try:
            yield
        finally:
            self._execute_wrappers.pop()

    @asynccontextmanager
    async def atomic(
        self, savepoint_name: str | None = None, *, isolation_level: str | None = None
    ):
        """Alias for transaction()."""
        async with self.transaction(
            savepoint_name=savepoint_name, isolation_level=isolation_level
        ):
            yield self

    def pool_stats(self) -> dict[str, int]:
        """Return connection pool statistics."""
        self._check_pool()
        return _db_pool_stats(self._pool_handle)

    @property
    def is_connected(self):
        return self._pool is not None

    @property
    def backend(self):
        return self._backend or "none"

    def __repr__(self):
        return f"Database({self.url!r}, backend={self.backend})"


# Global database instance — lazily created from settings on first use.
# No manual set_db() required for common usage.
_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    """Get the default database connection.

    Lazily creates and connects a Database instance from the DATABASE_URL
    setting on first access. Thread-safe.

    Connection is synchronous — pg.zig pool creation via _db_configure is
    a blocking C call (~10ms) that doesn't need an event loop.
    """
    global _db
    if _db is not None:
        return _db
    with _db_lock:
        # Double-check after lock
        if _db is not None:
            return _db
        url = _get_setting("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "No database configured. Set DATABASE_URL in settings or "
                "pass database= to HyperApp()."
            )
        db = Database(url)
        # Direct synchronous connect — Database.connect() is async in signature
        # but purely synchronous internally (calls C extension _db_configure).
        db._conn_url = _ensure_url_user(db.url)
        db._pool_handle = _acquire_pool(db._conn_url, db.max_size, db.min_size)
        db._backend = "pgzig"
        db._pool = True
        _db_register_vector(db._pool_handle)
        _db = db
        return _db


def set_db(db: Database):
    """Explicitly set the global database instance.

    Used by CLI commands and test infrastructure. For normal app usage,
    get_db() auto-creates from DATABASE_URL setting.
    """
    global _db
    _db = db


@dataclass(slots=True)
class CursorPage:
    """A single page of results from a server-side cursor."""

    rows: list[dict[str, object]]
    page_number: int
    row_count: int
    total_fetched: int
    is_last: bool


class DatabaseServerCursor:
    """Real PostgreSQL server-side cursor with DECLARE CURSOR / FETCH / CLOSE.

    Pins a pool connection for the cursor's lifetime. The cursor operates
    inside a transaction (BEGIN) that is committed on close.

    This is NOT the keyset pagination trick — this is a real database cursor
    that holds position in the result set. The database maintains the cursor
    state, and FETCH retrieves the next N rows without re-executing the query.

    Properties:
    - O(1) memory per page (only current page in memory)
    - O(1) per FETCH (no OFFSET scanning, no re-execution)
    - Requires a pinned pool connection (consumes one pool slot)
    - Must be explicitly closed (connection is not returned to pool until close)
    - Works with read replicas (create from replica Database instance)

    Usage:
        async with await db.server_cursor("SELECT * FROM big_table", page_size=100) as cursor:
            async for page in cursor:
                print(f"Page {page.page_number}: {len(page.rows)} rows")
                process(page.rows)

    For read-replica routing:
        replica_db = connection_manager["replica"]
        async with await replica_db.server_cursor(sql) as cursor:
            async for page in cursor:
                ...
    """

    def __init__(
        self,
        cursor_name: str,
        conn_handle: int,
        pool_handle: int,
        page_size: int,
        _db_query: object,
        _db_conn_execute: object,
        _db_conn_release: object,
        _get_last_columns: object,
        query_handle: int | None = None,
        owns_connection: bool = True,
        _db_execute: object = None,
    ):
        self.cursor_name = cursor_name
        self._conn_handle = conn_handle
        self._pool_handle = pool_handle
        self.page_size = page_size
        self._query = _db_query
        self._execute = _db_conn_execute
        self._release = _db_conn_release
        self._get_columns = _get_last_columns
        self._closed = False
        self._exhausted = False
        self.total_fetched: int = 0
        # When the cursor is created INSIDE a caller's transaction it borrows
        # that transaction's connection: FETCH/CLOSE route through
        # ``_query_handle`` (the transaction's effective handle, resolved by
        # ``acquireConnByHandle``) via ``_db_query``/``_db_execute``, and the
        # cursor never issues BEGIN/COMMIT/ROLLBACK or releases the connection —
        # the surrounding ``transaction()`` owns that lifecycle. When it owns its
        # own connection (no active transaction) ``query_handle`` is None and the
        # raw-slot path (``-(conn_handle + 2)``) + COMMIT + release runs.
        self._query_handle = query_handle
        self._owns_connection = owns_connection
        self._db_execute = _db_execute

    async def fetch_page(self) -> list[dict[str, object]]:
        """Fetch the next page of rows from the server-side cursor.

        Returns a list of dicts (column_name → value), or empty list if exhausted.
        Uses FETCH N FROM cursor — the database maintains position.
        """
        if self._closed or self._exhausted:
            return []

        # Borrowed cursor: FETCH on the transaction's effective handle. Owned
        # cursor: negative-encode its raw pinned slot to reuse the connection.
        pinned_h = (
            self._query_handle
            if self._query_handle is not None
            else -(self._conn_handle + 2)
        )
        fetch_sql = f'FETCH {self.page_size} FROM "{self.cursor_name}"'
        # Classify a FETCH failure into the unified typed hierarchy, matching
        # every other query path.
        raw_rows = _classify_call(lambda: self._query(pinned_h, fetch_sql, []))()

        if not raw_rows:
            self._exhausted = True
            return []

        if len(raw_rows) < self.page_size:
            self._exhausted = True

        self.total_fetched += len(raw_rows)

        # Convert tuples to dicts
        cols = self._get_columns()
        col_names = (
            [c[0] for c in cols] if cols else [str(i) for i in range(len(raw_rows[0]))]
        )
        return [dict(zip(col_names, row)) for row in raw_rows]

    async def close(self) -> None:
        """Close the cursor, commit the transaction, and release the connection back to pool."""
        if self._closed:
            return
        self._closed = True
        if not self._owns_connection:
            # Borrowed the surrounding transaction's connection: CLOSE the cursor
            # only. The transaction owns BEGIN/COMMIT/ROLLBACK and the connection
            # release — committing or releasing here would break it. Best-effort:
            # if the transaction already aborted, CLOSE fails harmlessly.
            with suppress(Exception):
                self._db_execute(self._query_handle, f'CLOSE "{self.cursor_name}"', [])
            return
        try:
            self._execute(self._conn_handle, f'CLOSE "{self.cursor_name}"', [])
            self._execute(self._conn_handle, "COMMIT", [])
        # blind-except: cursor teardown — if CLOSE/COMMIT fails (aborted tx, dead conn) we roll back best-effort; the connection is always released in the finally, so nothing leaks.
        except Exception:
            with suppress(Exception):
                self._execute(self._conn_handle, "ROLLBACK", [])
        finally:
            with suppress(Exception):
                self._release(self._conn_handle)

    @property
    def is_exhausted(self) -> bool:
        return self._exhausted

    async def pages(self):
        """Async generator that yields CursorPage objects.

        Auto-closes the cursor and releases the pinned pool connection
        when iteration completes (exhausted or break).

        Usage:
            async for page in cursor.pages():
                print(f"Page {page.page_number}: {page.row_count} rows, last={page.is_last}")
                for row in page.rows:
                    process(row)
            # Connection is auto-released here
        """
        page_number = 0
        try:
            while True:
                rows = await self.fetch_page()
                if not rows:
                    return
                page_number += 1
                yield CursorPage(
                    rows=rows,
                    page_number=page_number,
                    row_count=len(rows),
                    total_fetched=self.total_fetched,
                    is_last=self._exhausted,
                )
        finally:
            await self.close()

    def __aiter__(self):
        """Async iterate over CursorPage objects.

        Usage:
            async for page in cursor:
                process(page.rows)
        """
        return self.pages()

    async def __aenter__(self) -> DatabaseServerCursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
