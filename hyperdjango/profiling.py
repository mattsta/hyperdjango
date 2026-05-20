"""
Built-in profiler — nanosecond precision request profiling.

Uses Zig std.time.nanoTimestamp() for sub-microsecond accuracy.
Zero overhead when disabled. Per-request breakdown of middleware,
routing, handler, and SQL timing.

Usage:
    from hyperdjango import HyperApp
    app = HyperApp()

    # Profile a single route
    @app.route('GET', '/users')
    @app.profile
    def list_users(request):
        ...

    # Profile all routes
    app.profiling = True

    # Access profile data
    # Response includes X-Profile header with timing breakdown
    # Example: X-Profile: total=1.2ms handler=0.8ms sql=0.3ms(2q) middleware=0.1ms
"""

import contextvars
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from hyperdjango._hyperdjango_native import (
    _profiler_diff_nanos,
    _profiler_nanos,
)


def nanos() -> int:
    """Get current nanosecond timestamp (monotonic)."""
    return _profiler_nanos()


def elapsed_nanos(start: int) -> int:
    """Get elapsed nanoseconds since start."""
    return _profiler_diff_nanos(start)


@dataclass(slots=True)
class SQLQuery:
    """A single profiled SQL query."""

    sql: str
    duration_ns: int
    params: tuple | None = None


@dataclass(slots=True)
class RequestProfile:
    """Complete profile for a single request."""

    method: str = ""
    path: str = ""
    start_ns: int = 0
    total_ns: int = 0
    middleware_ns: int = 0
    routing_ns: int = 0
    handler_ns: int = 0
    sql_total_ns: int = 0
    sql_queries: list[SQLQuery] = field(default_factory=list)

    @property
    def sql_count(self) -> int:
        return len(self.sql_queries)

    def to_header(self) -> str:
        """Format as X-Profile header value."""
        parts = [f"total={_fmt_ns(self.total_ns)}"]
        if self.handler_ns:
            parts.append(f"handler={_fmt_ns(self.handler_ns)}")
        if self.sql_total_ns:
            parts.append(f"sql={_fmt_ns(self.sql_total_ns)}({self.sql_count}q)")
        if self.middleware_ns:
            parts.append(f"middleware={_fmt_ns(self.middleware_ns)}")
        if self.routing_ns:
            parts.append(f"routing={_fmt_ns(self.routing_ns)}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Full profile as dict (for JSON response)."""
        return {
            "method": self.method,
            "path": self.path,
            "total_ns": self.total_ns,
            "total": _fmt_ns(self.total_ns),
            "middleware_ns": self.middleware_ns,
            "routing_ns": self.routing_ns,
            "handler_ns": self.handler_ns,
            "sql_total_ns": self.sql_total_ns,
            "sql_count": self.sql_count,
            "queries": [
                {
                    "sql": q.sql[:200],
                    "duration_ns": q.duration_ns,
                    "duration": _fmt_ns(q.duration_ns),
                }
                for q in self.sql_queries
            ],
        }

    def to_collapsed_stack(self) -> str:
        """Format as collapsed stack for flame graph (speedscope compatible).

        Each line: stack_frame;stack_frame count
        """
        lines = []
        if self.middleware_ns:
            lines.append(f"request;middleware {self.middleware_ns}")
        if self.routing_ns:
            lines.append(f"request;routing {self.routing_ns}")
        if self.handler_ns:
            handler_non_sql = self.handler_ns - self.sql_total_ns
            if handler_non_sql > 0:
                lines.append(f"request;handler;python {handler_non_sql}")
            for i, q in enumerate(self.sql_queries):
                # Truncate SQL for readability
                sql_label = q.sql[:60].replace(";", ",").replace("\n", " ")
                lines.append(f"request;handler;sql;{sql_label} {q.duration_ns}")
        return "\n".join(lines)


# Per-request profile storage.
#
# The ContextVar is the reactor-safe source of truth: request-scoped state
# isolated per asyncio Task, so under a future multiplexing reactor (or when a
# profile is started on the shared WS loop) task A's spans/SQL cannot bleed into
# task B's profile. This matches the ContextVar pattern i18n.py / tenancy.py use
# for their request-scoped state. Under today's thread-per-request HTTP the
# behavior is identical (exactly one Task per thread).
#
# `_thread_local` is retained ONLY as a mirror for the native SQL-recording
# fast path in database.py, which reads `_prof_module._thread_local` directly on
# every query (`_query_tracking_enabled` and the per-query profile append). It is
# kept in lock-step with the ContextVar below. database.py itself should migrate
# to `get_current_profile()` to be fully reactor-safe; until then this mirror
# preserves that hot path unchanged under thread-per-request.
_current_profile: contextvars.ContextVar[RequestProfile | None] = (
    contextvars.ContextVar("hyperdjango_current_profile", default=None)
)


@dataclass(slots=True)
class _ProfileMirror:
    """Thread-owned mirror of the active profile. Written per profiled
    request — on a plain object, NOT the threading.local itself, because
    threading.local attribute WRITES serialize process-wide under
    free-threaded CPython (measured 880x slower than thread-owned-object
    writes). A profiler must not inject the very contention it measures."""

    profile: RequestProfile | None = None


class _ProfileLocal(threading.local):
    def __init__(self) -> None:
        self.state = _ProfileMirror()


_thread_local = _ProfileLocal()


def get_current_profile() -> RequestProfile | None:
    """Get the profile for the current request (if profiling is active)."""
    return _current_profile.get()


def start_profile(method: str = "", path: str = "") -> RequestProfile:
    """Start profiling the current request."""
    profile = RequestProfile(method=method, path=path, start_ns=nanos())
    _current_profile.set(profile)
    _thread_local.state.profile = profile  # mirror for database.py's native reader
    return profile


def end_profile() -> RequestProfile | None:
    """End profiling and return the completed profile."""
    profile = _current_profile.get()
    if profile is not None:
        profile.total_ns = elapsed_nanos(profile.start_ns)
        _current_profile.set(None)
        _thread_local.state.profile = None  # mirror for database.py's native reader
    return profile


def record_sql(sql: str, duration_ns: int, params: tuple | None = None):
    """Record a SQL query in the current profile."""
    profile = get_current_profile()
    if profile is not None:
        profile.sql_queries.append(
            SQLQuery(sql=sql, duration_ns=duration_ns, params=params)
        )
        profile.sql_total_ns += duration_ns


def profile_handler(func: Callable) -> Callable:
    """Decorator to profile a route handler.

    Wraps the handler to measure execution time and add X-Profile header.
    """

    @wraps(func)
    def wrapper(request, *args, **kwargs):
        prof = start_profile(
            method=request.method,
            path=request.path,
        )

        handler_start = nanos()
        response = func(request, *args, **kwargs)
        prof.handler_ns = elapsed_nanos(handler_start)

        completed = end_profile()
        if completed is not None:
            # Add X-Profile header to response
            header_val = completed.to_header()
            if hasattr(response, "headers"):
                response.headers["X-Profile"] = header_val
            elif hasattr(response, "__setitem__"):
                response["X-Profile"] = header_val

        return response

    wrapper._profiled = True
    return wrapper


@dataclass(slots=True)
class ProfileStore:
    """Stores recent request profiles for analysis.

    Thread-safe ring buffer of the last N profiles.
    """

    max_profiles: int = 1000
    _profiles: list[RequestProfile] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, profile: RequestProfile):
        with self._lock:
            self._profiles.append(profile)
            if len(self._profiles) > self.max_profiles:
                self._profiles = self._profiles[-self.max_profiles :]

    def get_all(self) -> list[RequestProfile]:
        with self._lock:
            return list(self._profiles)

    def get_slowest(self, n: int = 10) -> list[RequestProfile]:
        with self._lock:
            return sorted(self._profiles, key=lambda p: p.total_ns, reverse=True)[:n]

    def get_flame_graph(self) -> str:
        """Generate collapsed stack format for all stored profiles."""
        with self._lock:
            return "\n".join(
                p.to_collapsed_stack() for p in self._profiles if p.total_ns > 0
            )

    def clear(self):
        with self._lock:
            self._profiles.clear()


# Global profile store
_store = ProfileStore()


def get_store() -> ProfileStore:
    """Get the global profile store."""
    return _store


def _fmt_ns(ns: int) -> str:
    """Format nanoseconds as human-readable duration."""
    if ns < 1_000:
        return f"{ns}ns"
    elif ns < 1_000_000:
        return f"{ns / 1_000:.1f}μs"
    elif ns < 1_000_000_000:
        return f"{ns / 1_000_000:.1f}ms"
    else:
        return f"{ns / 1_000_000_000:.2f}s"
