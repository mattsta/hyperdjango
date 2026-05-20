"""
Python facade over the native metric registry (v0.15.0).

Each metric class is a thin wrapper around a u32 handle returned
by the native `_metric_*_register` FFI. On the hot path (`inc`,
`set`, `observe`) each method is a single FFI call — no Python
state, no locks, no dict lookups.

Zero-cost-when-disabled: every hot-path method begins with a
module-level `_enabled` bool check. When telemetry is disabled
(the platform default), the method returns after one LOAD_GLOBAL +
POP_JUMP bytecode pair — <50ns per call, branch-predicted to the
fast path.

Registration is rare (once per class/metric at module load time)
so it can take the slow path without measurable cost.

Every metric class inherits `@dataclass(slots=True)` so the
per-instance footprint is one u32 handle reference — no dict, no
hidden state. Metric instances are typically module-level
singletons; registering the same name twice is NOT deduped at the
Zig layer and produces two independent time series (this matches
Prometheus's permissive semantics for dynamic registration).
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from hyperdjango._hyperdjango_native import (
    _metric_counter_inc,
    _metric_counter_read,
    _metric_counter_register,
    _metric_counter_vec_inc,
    _metric_counter_vec_register,
    _metric_gauge_add,
    _metric_gauge_read,
    _metric_gauge_register,
    _metric_gauge_set,
    _metric_histogram_observe,
    _metric_histogram_register,
    _metric_histogram_vec_observe,
    _metric_histogram_vec_register,
    _metric_registry_write_prometheus,
)

# ── Global enable/disable flag ───────────────────────────────────────────────
#
# `_enabled` is a module-level bool. Every hot-path method reads it
# as a LOAD_GLOBAL and returns early if false. This matches the
# database.py `_perf_middleware is None` pattern — one branch,
# branch-predicted, <50ns per call when disabled.

_enabled: bool = False


def enable() -> None:
    """Enable telemetry globally. Metrics begin recording immediately."""
    global _enabled
    _enabled = True


def disable() -> None:
    """Disable telemetry globally. All metric methods become no-ops.

    Existing metric handles remain valid — they just stop collecting
    until `enable()` is called again. This is intentional: apps can
    toggle telemetry at runtime without re-registering metrics.
    """
    global _enabled
    _enabled = False


def is_enabled() -> bool:
    return _enabled


# ── Default histogram buckets ────────────────────────────────────────────────
#
# Sensible default for HTTP request duration in seconds. Matches the
# Prometheus client_python default (roughly — ours is trimmed to 12
# buckets for a good density/scrape-size tradeoff).

DEFAULT_DURATION_BUCKETS: tuple[float, ...] = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


# ── Counter ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Counter:
    """A monotonically-increasing counter. Thread-safe via atomic RMW.

    Usage:
        requests_total = Counter("myapp_requests_total", "Total requests.")
        requests_total.inc()
        requests_total.inc(5)
    """

    name: str
    help: str = ""
    _handle: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._handle = _metric_counter_register(self.name, self.help)

    def inc(self, amount: int = 1) -> None:
        if not _enabled:
            return
        _metric_counter_inc(self._handle, amount)

    def value(self) -> int:
        """Read current value (test helper, not a scrape path)."""
        return _metric_counter_read(self._handle)


# ── CounterVec ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class CounterVec:
    """A labeled counter family. Each label combination gets its own
    atomic counter. Label cardinality should stay bounded (<1000
    combinations) to keep memory + scrape time reasonable.

    Usage:
        http_requests = CounterVec(
            "myapp_http_requests_total",
            "HTTP requests by method and status.",
            ["method", "status"],
        )
        http_requests.inc({"method": "GET", "status": "200"})
    """

    name: str
    help: str = ""
    label_names: tuple[str, ...] = ()
    _handle: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Normalize label_names to a tuple (users may pass a list)
        self.label_names = tuple(self.label_names)
        self._handle = _metric_counter_vec_register(
            self.name,
            self.help,
            list(self.label_names),
        )

    def inc(self, labels: dict[str, str], amount: int = 1) -> None:
        if not _enabled:
            return
        # Build ordered list of label values from the dict in the
        # order the labels were declared at registration. Missing
        # keys become empty strings — this gives predictable
        # behavior when a caller forgets a label.
        values = [labels.get(n, "") for n in self.label_names]
        _metric_counter_vec_inc(self._handle, values, amount)

    def inc_tuple(self, values: tuple[str, ...], amount: int = 1) -> None:
        """Fast-path variant that skips the dict lookup. Caller is
        responsible for ordering `values` the same way `label_names`
        is ordered."""
        if not _enabled:
            return
        _metric_counter_vec_inc(self._handle, list(values), amount)


# ── Gauge ────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Gauge:
    """An instantaneous value that can go up and down. Thread-safe.

    Usage:
        in_flight = Gauge("myapp_in_flight_requests", "In-flight request count.")
        in_flight.inc()
        in_flight.dec()
        in_flight.set(42)
    """

    name: str
    help: str = ""
    _handle: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._handle = _metric_gauge_register(self.name, self.help)

    def set(self, value: int) -> None:
        if not _enabled:
            return
        _metric_gauge_set(self._handle, value)

    def inc(self, delta: int = 1) -> None:
        if not _enabled:
            return
        _metric_gauge_add(self._handle, delta)

    def dec(self, delta: int = 1) -> None:
        if not _enabled:
            return
        _metric_gauge_add(self._handle, -delta)

    def value(self) -> int:
        return _metric_gauge_read(self._handle)


# ── Histogram ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Histogram:
    """A histogram with configurable bucket upper bounds. Stores per-
    bucket counts, cumulative sum, and total count. Thread-safe.

    Usage:
        request_duration = Histogram(
            "myapp_request_duration_seconds",
            "Request duration distribution.",
        )
        request_duration.observe(0.037)
    """

    name: str
    help: str = ""
    buckets: tuple[float, ...] = DEFAULT_DURATION_BUCKETS
    _handle: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.buckets = tuple(self.buckets)
        self._handle = _metric_histogram_register(
            self.name,
            self.help,
            self.buckets,
        )

    def observe(self, value: float) -> None:
        if not _enabled:
            return
        _metric_histogram_observe(self._handle, value)


# ── HistogramVec ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class HistogramVec:
    """A labeled histogram family.

    Usage:
        request_duration_by_path = HistogramVec(
            "myapp_request_duration_seconds",
            "Request duration by path.",
            label_names=["method", "path"],
        )
        request_duration_by_path.observe({"method": "GET", "path": "/api/books"}, 0.037)
    """

    name: str
    help: str = ""
    label_names: tuple[str, ...] = ()
    buckets: tuple[float, ...] = DEFAULT_DURATION_BUCKETS
    _handle: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.label_names = tuple(self.label_names)
        self.buckets = tuple(self.buckets)
        self._handle = _metric_histogram_vec_register(
            self.name,
            self.help,
            list(self.label_names),
            self.buckets,
        )

    def observe(self, labels: dict[str, str], value: float) -> None:
        if not _enabled:
            return
        values = [labels.get(n, "") for n in self.label_names]
        _metric_histogram_vec_observe(self._handle, values, value)

    def observe_tuple(self, values: tuple[str, ...], value: float) -> None:
        if not _enabled:
            return
        _metric_histogram_vec_observe(self._handle, list(values), value)


# ── Sampler registry ────────────────────────────────────────────────────────
#
# Some platform subsystems own state that the *metric registry* needs to
# observe periodically — pg.zig pool waiter counts, async task queue depth,
# WebSocket connection counts, cache eviction tallies. These can't be
# bumped from a single state-update site (the state is owned by Zig or by
# a thread we don't control), so we expose a tiny callback registry that
# the drain worker walks once per tick before exporting metrics.
#
# Samplers are pure Python callables: `() -> None`. Each sampler is
# expected to read its source-of-truth state and update its own Gauge/
# Histogram instances. Errors are caught and isolated so one broken
# sampler can't starve the others.
#
# Registration is push-only — there's no remove() because samplers are
# always module-scope singletons established at import time. If a test
# wants to validate a sampler in isolation it can call it directly via
# `_run_samplers()`.

_samplers: list[Callable[[], None]] = []


def register_sampler(fn: Callable[[], None]) -> None:
    """Register a periodic sampler callback.

    The drain worker will call `fn()` once per drain tick (default
    every 1.0 s, configurable via TELEMETRY_DRAIN_INTERVAL). The
    callback should read its source state and update its associated
    Gauge/Histogram instances. Exceptions raised inside `fn` are
    caught by the drain worker and reported via the same channel as
    sink errors — they never crash the drain thread.

    Idempotent registration: a function can be registered multiple
    times, but it will only be invoked once per tick (de-duped on
    identity). Module-level singletons are the expected pattern.
    """
    if fn not in _samplers:
        _samplers.append(fn)


def _run_samplers() -> list[BaseException]:
    """Invoke every registered sampler and return any exceptions raised.

    Used by the drain worker; exposed so tests can drive samplers
    without spinning up a real `_DrainWorker`. Returns the list of
    exceptions in the order they were raised so the caller can
    decide how to log them.
    """
    errors: list[BaseException] = []
    for fn in _samplers:
        try:
            fn()
        # blind-except: a user sampler callback failing must not crash the daemon drain thread; every exception is collected and returned so the drain worker reports it
        except BaseException as exc:
            errors.append(exc)
    return errors


# ── Scrape helper ────────────────────────────────────────────────────────────
#
# Called once per Prometheus scrape (every 15-60 seconds typically).
# Not on the request hot path. Returns the full exposition text as
# bytes.


def collect_prometheus_text() -> bytes:
    """Export all registered metrics as Prometheus exposition text.

    This reads from the native registry regardless of the `_enabled`
    flag — scrape requests come from monitoring infrastructure that
    wants to see ALL registered metrics even if collection was
    paused. Zero values are still meaningful to Prometheus.
    """
    return _metric_registry_write_prometheus()
