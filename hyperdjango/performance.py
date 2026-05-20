"""
Performance monitoring middleware — query tracking, slow query detection, N+1 alerts.

Tracks all database queries per request, detects N+1 patterns, and provides
a dashboard endpoint for monitoring.

Usage:
    from hyperdjango.performance import PerformanceMiddleware

    perf = PerformanceMiddleware(slow_query_threshold_ms=100)
    app.use(perf)

    # Dashboard at /debug/performance
    # Per-request stats via X-Query-Count, X-Query-Time headers
"""

import contextvars
import re
import threading
import time
from dataclasses import dataclass, field

from hyperdjango.response import Response

# Per-request query state held in ContextVars, NOT threading.local. A single
# event loop serves many concurrent requests on ONE thread: while request A is
# suspended on `await call_next`, request B runs on the same thread. With
# threading.local they would share state, so B opening its window would reset
# A's queries and cross-attribute them. ContextVars are copied per asyncio Task
# and survive across await points, so each request sees only its own state.
#
# _perf_queries holds the list of (raw_sql, duration_ms) tuples for the current
# request (raw SQL, not normalized — most queries don't repeat, so normalization
# happens once per UNIQUE raw SQL in the response handler, off the hot path).
# _perf_in_request gates writes: record_query is a no-op when it's False so
# background queries (pool heartbeat, etc.) don't leak into a tracked request.
_perf_queries: contextvars.ContextVar[list[tuple[str, float]] | None] = (
    contextvars.ContextVar("_perf_queries", default=None)
)
_perf_in_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_perf_in_request", default=False
)


@dataclass
class QueryRecord:
    """Single query execution record.

    Only allocated for slow queries inside the response handler — never on
    the record_query hot path. A dataclass so callers can read named fields
    off ``PerformanceMiddleware._history[].slow_queries``.
    """

    sql: str
    duration_ms: float
    timestamp: float
    stacktrace: str = ""


@dataclass
class RequestStats:
    """Query stats for a single request."""

    path: str
    method: str
    query_count: int
    total_query_ms: float
    slow_queries: list[QueryRecord]
    n_plus_one: list[str]  # SQL patterns detected as N+1
    timestamp: float


@dataclass
class PerformanceMiddleware:
    """Track query performance per request.

    Features:
    - Per-request query count and total time (X-Query-Count, X-Query-Time headers)
    - Slow query detection (configurable threshold)
    - N+1 query pattern detection (same SQL repeated > threshold times)
    - Ring buffer of recent request stats (for dashboard)
    - Thread-safe
    """

    slow_query_threshold_ms: float = 100.0
    n_plus_one_threshold: int = 5
    max_history: int = 1000
    dashboard_path: str = "/debug/performance"
    enabled: bool = True

    # Internal state (not init params)
    _history: list[RequestStats] = field(default_factory=list, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    # Loose-mode buckets: queries recorded outside a tracked request window
    # (tests that synthesize queries with an explicit rid, handlers that
    # predate the middleware integration). Normal requests never touch this
    # dict — record_query goes through the _perf_queries ContextVar on the fast path.
    _loose_buckets: dict[int, list[tuple[str, float]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _total_requests: int = field(default=0, init=False, repr=False)
    _total_queries: int = field(default=0, init=False, repr=False)
    _slow_count: int = field(default=0, init=False, repr=False)
    _n_plus_one_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        # Wire settings if fields are at their defaults
        from hyperdjango.conf import get_setting

        setting_history = get_setting("PERFORMANCE_HISTORY_SIZE")
        if self.max_history == 1000 and setting_history != 1000:
            self.max_history = int(setting_history)
        setting_n1 = get_setting("PERFORMANCE_N_PLUS_ONE_THRESHOLD")
        if self.n_plus_one_threshold == 5 and setting_n1 != 5:
            self.n_plus_one_threshold = int(setting_n1)

    def record_query(self, sql: str, duration_ms: float, request_id: int | None = None):
        """Record a query execution. Called by the database layer.

        Hot path: appends a (sql, duration_ms) tuple to the per-thread queue
        with no locks, no dataclass allocation, no SQL normalization. All
        aggregation (N+1 detection, normalization, slow query records) is
        deferred to __call__ where it runs once per request, not once per
        query.

        Loose path: when `request_id` is explicitly provided OR no request
        window is open (in_request is False), queries are bucketed in the
        shared `_loose_buckets` dict under a single lock acquisition. This
        path only exists to keep existing test/handler contracts working.
        """
        if not self.enabled:
            return
        # Fast path: inside a tracked request, append to this request's own
        # ContextVar list. The per-request append needs no lock (the list is
        # private to the current task), but the SHARED counters do: `+= 1` is
        # not atomic under free-threaded Python, and the loose path below bumps
        # the same counters under self._lock — mixing locked and unlocked writes
        # loses increments. So both paths update counters under self._lock.
        if request_id is None and _perf_in_request.get():
            queue = _perf_queries.get()
            if queue is not None:
                queue.append((sql, duration_ms))
            is_slow = duration_ms > self.slow_query_threshold_ms
            with self._lock:
                self._total_queries += 1
                if is_slow:
                    self._slow_count += 1
            return
        # Loose path: synthesize a bucket so __call__ can drain it later.
        rid = request_id if request_id is not None else id(threading.current_thread())
        with self._lock:
            bucket = self._loose_buckets.get(rid)
            if bucket is None:
                bucket = []
                self._loose_buckets[rid] = bucket
            bucket.append((sql, duration_ms))
            self._total_queries += 1
            if duration_ms > self.slow_query_threshold_ms:
                self._slow_count += 1

    def get_stats(
        self,
    ) -> dict[str, int | float | list[dict[str, str | float]] | list[str]]:
        """Get aggregate performance statistics."""
        with self._lock:
            avg_queries = (
                (self._total_queries / self._total_requests)
                if self._total_requests > 0
                else 0
            )
            recent = self._history[-100:] if self._history else []
            slow_queries = []
            n_plus_one_patterns = []
            for stats in recent:
                slow_queries.extend(stats.slow_queries)
                n_plus_one_patterns.extend(stats.n_plus_one)
            return {
                "total_requests": self._total_requests,
                "total_queries": self._total_queries,
                "avg_queries_per_request": round(avg_queries, 1),
                "slow_query_count": self._slow_count,
                "n_plus_one_count": self._n_plus_one_count,
                "recent_slow_queries": [
                    {"sql": q.sql[:200], "duration_ms": round(q.duration_ms, 2)}
                    for q in sorted(slow_queries, key=lambda x: -x.duration_ms)[:20]
                ],
                "recent_n_plus_one": list(set(n_plus_one_patterns))[:20],
            }

    async def __call__(self, request, call_next):
        """Middleware: track queries for this request, add headers."""
        if not self.enabled:
            return await call_next(request)

        # Dashboard endpoint
        if request.path == self.dashboard_path and request.method == "GET":
            return self._dashboard_response()

        # JSON stats endpoint
        if request.path == f"{self.dashboard_path}/json" and request.method == "GET":
            return Response.json(self.get_stats())

        # Open the per-request query collection window. record_query appends
        # (sql, duration_ms) tuples to this task's own `queries` list (held in
        # the _perf_queries ContextVar) until `in_request` flips back to False
        # in the finally. Because the ContextVar is task-local, a concurrent
        # request on the same event-loop thread has its own independent window.
        queries: list[tuple[str, float]] = []
        _perf_queries.set(queries)
        _perf_in_request.set(True)
        rid = id(threading.current_thread())
        try:
            response = await call_next(request)
        finally:
            _perf_in_request.set(False)
            _perf_queries.set(None)

        # Drain any queries recorded via the loose path for this thread.
        # The common case is no loose buckets at all, so the dict truthiness
        # check short-circuits.
        if self._loose_buckets:
            with self._lock:
                loose = self._loose_buckets.pop(rid, None)
            if loose:
                queries = list(queries) + loose

        query_count = len(queries)
        if query_count == 0:
            total_query_ms = 0.0
            slow_queries: list[QueryRecord] = []
            n_plus_one: list[str] = []
        else:
            total_query_ms = 0.0
            raw_counts: dict[str, int] = {}
            slow_threshold = self.slow_query_threshold_ms
            slow_raw: list[tuple[str, float]] = []
            for sql, duration_ms in queries:
                total_query_ms += duration_ms
                raw_counts[sql] = raw_counts.get(sql, 0) + 1
                if duration_ms > slow_threshold:
                    slow_raw.append((sql, duration_ms))

            # Normalize SQL once per unique raw query, then aggregate counts
            # under the normalized key. This collapses queries like
            # "WHERE id = 1" and "WHERE id = 2" into a single N+1 pattern.
            if len(raw_counts) == 1:
                only_sql, only_count = next(iter(raw_counts.items()))
                if only_count >= self.n_plus_one_threshold:
                    n_plus_one = [_normalize_sql(only_sql)]
                else:
                    n_plus_one = []
            else:
                norm_counts: dict[str, int] = {}
                for raw_sql, count in raw_counts.items():
                    norm = _normalize_sql(raw_sql)
                    norm_counts[norm] = norm_counts.get(norm, 0) + count
                n_plus_one = [
                    norm
                    for norm, count in norm_counts.items()
                    if count >= self.n_plus_one_threshold
                ]

            # Build QueryRecord dataclass instances only for slow queries —
            # these are rare outside of an actual incident.
            if slow_raw:
                ts = time.time()
                slow_queries = [
                    QueryRecord(sql=_normalize_sql(s), duration_ms=d, timestamp=ts)
                    for s, d in slow_raw
                ]
            else:
                slow_queries = []

        # Single lock acquire per request for shared state updates.
        stats = RequestStats(
            path=request.path,
            method=request.method,
            query_count=query_count,
            total_query_ms=total_query_ms,
            slow_queries=slow_queries,
            n_plus_one=n_plus_one,
            timestamp=time.time(),
        )
        with self._lock:
            self._total_requests += 1
            if n_plus_one:
                self._n_plus_one_count += len(n_plus_one)
            self._history.append(stats)
            if len(self._history) > self.max_history:
                self._history = self._history[-self.max_history :]

        response.headers["X-Query-Count"] = str(query_count)
        response.headers["X-Query-Time"] = f"{total_query_ms:.1f}ms"
        if n_plus_one:
            response.headers["X-N-Plus-One"] = str(len(n_plus_one))

        return response

    def _dashboard_response(self):
        """Render HTML performance dashboard."""
        stats = self.get_stats()
        html = f"""<!DOCTYPE html>
<html><head><title>Performance Dashboard</title>
<style>
body {{ font-family: system-ui; max-width: 900px; margin: 2em auto; padding: 0 1em; }}
h1 {{ color: #333; }} table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #f5f5f5; }} .warn {{ color: #e67e22; }} .error {{ color: #e74c3c; }}
.stat {{ font-size: 2em; font-weight: bold; color: #2c3e50; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1em; margin: 1em 0; }}
.card {{ background: #f8f9fa; border-radius: 8px; padding: 1em; text-align: center; }}
</style></head><body>
<h1>Performance Dashboard</h1>
<div class="grid">
  <div class="card"><div class="stat">{stats["total_requests"]}</div>Total Requests</div>
  <div class="card"><div class="stat">{stats["total_queries"]}</div>Total Queries</div>
  <div class="card"><div class="stat">{stats["avg_queries_per_request"]}</div>Avg Queries/Req</div>
  <div class="card"><div class="stat {"error" if stats["slow_query_count"] > 0 else ""}">{stats["slow_query_count"]}</div>Slow Queries</div>
  <div class="card"><div class="stat {"error" if stats["n_plus_one_count"] > 0 else ""}">{stats["n_plus_one_count"]}</div>N+1 Patterns</div>
</div>"""

        if stats["recent_slow_queries"]:
            html += "<h2>Recent Slow Queries</h2><table><tr><th>SQL</th><th>Duration</th></tr>"
            for q in stats["recent_slow_queries"]:
                html += f"<tr><td><code>{q['sql']}</code></td><td class='warn'>{q['duration_ms']}ms</td></tr>"
            html += "</table>"

        if stats["recent_n_plus_one"]:
            html += "<h2>N+1 Query Patterns</h2><table><tr><th>SQL Pattern</th></tr>"
            for sql in stats["recent_n_plus_one"]:
                html += f"<tr><td class='error'><code>{sql[:200]}</code></td></tr>"
            html += "</table>"

        html += f"""
<p style="color:#999; margin-top:2em">Auto-refresh: <a href="{self.dashboard_path}">reload</a> |
JSON: <a href="{self.dashboard_path}/json">API</a></p>
</body></html>"""
        return Response.html(html)


# Precompiled patterns: avoids re's internal cache lookup on every call and
# lets the C-level Pattern.sub fast path engage directly.
_SQL_STRING_LITERAL_RE = re.compile(r"'[^']*'")
_SQL_NUMBER_LITERAL_RE = re.compile(r"\b\d+\b")


def _normalize_sql(sql: str) -> str:
    """Normalize SQL for pattern matching — replace literal values with ?."""
    return _SQL_NUMBER_LITERAL_RE.sub(
        "?", _SQL_STRING_LITERAL_RE.sub("'?'", sql)
    ).strip()


# Global singleton for easy access from database layer
_perf_middleware: PerformanceMiddleware | None = None


def get_perf_middleware() -> PerformanceMiddleware | None:
    """Get the global performance middleware instance."""
    return _perf_middleware


def set_perf_middleware(mw: PerformanceMiddleware):
    """Set the global performance middleware instance."""
    global _perf_middleware
    _perf_middleware = mw
