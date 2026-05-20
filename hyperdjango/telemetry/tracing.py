"""
Tracer + Span public API over the native span ring (v0.15.0+).

The `Tracer` class is the canonical entry point for starting spans.
It owns the sampling policy and bridges the Python contextvar layer
to the native ring buffer FFI.

Usage (async context manager):

    tracer = Tracer("myapp")
    async with tracer.start_span("compute_recommendations") as span:
        span.set_attr("user_id", user.id)
        result = await heavy_work()

Usage (sync context manager):

    with tracer.start_span("sync_work") as span:
        span.set_attr("batch_size", 100)
        ...

Usage (decorator):

    @tracer.trace("list_books")
    async def list_books(request):
        ...

Usage (manual — not recommended, prefer context managers):

    span = tracer.start_span_raw("manual")
    try:
        span.set_attr("step", "setup")
        ...
    finally:
        span.end()

Zero-cost when disabled: if `hyperdjango.telemetry.is_enabled()` is
False, `start_span()` returns a `NoopSpan` that short-circuits every
method. No FFI calls, no contextvar writes, no allocations beyond
the NoopSpan singleton.

Zero-cost when unsampled: when the sampling policy returns False,
we still create a `SpanContext` with `sampled=False` and propagate
it so child spans inherit the decision (consistent-trace property),
but the `Span` returned is a `NoopSpan` — native FFI is never
called, no slot is claimed.
"""

import inspect
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hyperdjango.logging._logger import Logger

from hyperdjango._hyperdjango_native import (
    _span_add_event,
    _span_end,
    _span_set_attr_float,
    _span_set_attr_int,
    _span_set_attr_str,
    _span_set_status,
    _span_start,
)
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.telemetry.context import SpanContext, current, reset, set
from hyperdjango.telemetry.sampling import (
    ParentBased,
    RatioSample,
    SamplingPolicy,
)

# ── Status codes (mirror span_ring.zig StatusCode) ──────────────────────────

STATUS_UNSET: int = 0
STATUS_OK: int = 1
STATUS_ERROR: int = 2


# ── Noop span (zero-cost fallback) ──────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class NoopSpan:
    """Returned when telemetry is disabled OR the span is unsampled.

    Every method is a short-circuit. Used via singleton `_NOOP_SPAN`
    so we don't allocate a new object per unsampled span.

    `handle` and `context` are **class-level** attributes (not
    properties) so callers can branch on `span.handle != 0` in one
    attribute load — no Python-level `@property` call, no `isinstance`
    check against `Span` needed on the hot path.
    """

    # Constants visible at the class level — both Span and NoopSpan
    # expose `.handle: int`, so `span.handle != 0` is the one canonical
    # way to ask "is this span recorded?" without an isinstance call.
    handle: int = 0
    context: object | None = None

    def set_attr(self, key: str, value: Any) -> None:
        return

    def set_attr_str(self, key: str, value: str) -> None:
        """Fast-path string-only set_attr — skips the type-dispatch ladder.

        Mirrors `Span.set_attr_str`. Callers with statically-known
        string values (HTTP method, route, peer IP, ...) use this
        instead of `set_attr` to save 4 isinstance calls per attr.
        """
        return

    def set_attr_int(self, key: str, value: int) -> None:
        return

    def set_attr_float(self, key: str, value: float) -> None:
        return

    def set_attr_bool(self, key: str, value: bool) -> None:
        return

    def add_event(self, name: str) -> None:
        """No-op add_event — mirrors Span.add_event for polymorphism."""
        return

    def set_status(self, code: int, message: str = "") -> None:
        return

    def end(self) -> None:
        return


_NOOP_SPAN: NoopSpan = NoopSpan()


# ── Custom span context manager ────────────────────────────────────────────
#
# Replaces the previous `@contextmanager`-decorated generator approach.
# A slotted dataclass with explicit `__enter__`/`__exit__` is roughly
# 1 μs/request faster than `@contextmanager` because:
#
#   1. No generator frame allocation per `with`
#   2. No `_GeneratorContextManager` wrapper instance
#   3. No `next(self.gen)` to reach the yield
#   4. No `self.gen.throw(...)` machinery on the exception path
#
# The instance implements BOTH the sync and async context manager
# protocols (`__enter__`/`__exit__` AND `__aenter__`/`__aexit__`) so
# the same dataclass serves `with tracer.start_span()` and
# `async with tracer.start_span_async()` callers — saves a class.


@dataclass(slots=True)
class _SpanCM:
    """Sync + async context manager for a Tracer-issued span.

    Constructed by `Tracer.start_span` / `Tracer.start_span_async`.
    Carries the `(span, contextvar_token)` pair so `__exit__` can
    end the span and reset the contextvar in one place.
    """

    span: Span | NoopSpan
    token: Any  # contextvars.Token from telemetry.context.set, or None

    def __enter__(self) -> Span | NoopSpan:
        return self.span

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        # Hot path — `span.handle != 0` is a single attribute load
        # shared by both `Span` (dataclass slot) and `NoopSpan`
        # (class-level constant) so we don't need an isinstance call.
        # When handle is 0 the span is the noop singleton and every
        # method is already a no-op; we skip straight to the token
        # reset.
        span = self.span
        if span.handle != 0:
            # Recorded span — propagate exception state + end the slot.
            if exc_value is not None:
                span.set_status(STATUS_ERROR)
                span.set_attr_str("error.type", type(exc_value).__name__)
                span.set_attr_str("error.message", str(exc_value))
            span.end()
        token = self.token
        if token is not None:
            reset(token)

    async def __aenter__(self) -> Span | NoopSpan:
        return self.span

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        # Same body as __exit__ — duplicated rather than delegated
        # to avoid one extra Python frame on the hot async path.
        span = self.span
        if span.handle != 0:
            if exc_value is not None:
                span.set_status(STATUS_ERROR)
                span.set_attr_str("error.type", type(exc_value).__name__)
                span.set_attr_str("error.message", str(exc_value))
            span.end()
        token = self.token
        if token is not None:
            reset(token)


# ── Real span ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Span:
    """A live span backed by a native ring slot.

    Don't construct directly — use `Tracer.start_span()` which
    handles slot claim + contextvar propagation + sampling decision.
    The `handle` is an opaque u64 returned from `_span_start`; `0`
    means unsampled / dropped and all methods no-op.
    """

    handle: int
    context: SpanContext
    _ended: bool = field(default=False, repr=False)

    def set_attr(self, key: str, value: Any) -> None:
        """Add a key/value attribute to the span.

        Value dispatch: str/bytes/bool → string, int → int, float
        → float. Everything else is stringified via repr(). The
        native side stores all attrs as strings in a packed KV
        buffer (see span_ring.zig); int/float go through a
        dedicated FFI path that formats them at the C boundary.

        **Performance note**: callers with statically-known value
        types should prefer the dedicated fast-paths (`set_attr_str`,
        `set_attr_int`, `set_attr_float`) which skip the 4-branch
        isinstance ladder. This generic path is for user code that
        has a truly polymorphic value (e.g., attaching arbitrary
        attributes from a dict).
        """
        if self.handle == 0 or self._ended:
            return
        k = key if isinstance(key, str) else str(key)
        if isinstance(value, bool):
            _span_set_attr_str(self.handle, k, "true" if value else "false")
        elif isinstance(value, int):
            _span_set_attr_int(self.handle, k, value)
        elif isinstance(value, float):
            _span_set_attr_float(self.handle, k, value)
        elif isinstance(value, (str, bytes)):
            v = value if isinstance(value, str) else value.decode("utf-8", "replace")
            _span_set_attr_str(self.handle, k, v)
        else:
            _span_set_attr_str(self.handle, k, repr(value))

    def set_attr_str(self, key: str, value: str) -> None:
        """Fast-path: set a string attribute without type dispatch.

        Used by internal hot paths (`_attach_http_attrs`,
        `_finalize_span_response`) where the value is known to be a
        `str` at call time. Saves 3-4 isinstance calls per attr vs
        the generic `set_attr`. At 4-5 attrs per request this
        compounds to ~15 isinstance calls saved per request.

        Callers are responsible for passing a `str` — if you pass a
        non-str value the native layer will raise. For polymorphic
        callers use `set_attr`.
        """
        if self.handle == 0 or self._ended:
            return
        _span_set_attr_str(self.handle, key, value)

    def set_attr_int(self, key: str, value: int) -> None:
        """Fast-path int attribute. Same rationale as `set_attr_str`."""
        if self.handle == 0 or self._ended:
            return
        _span_set_attr_int(self.handle, key, value)

    def set_attr_float(self, key: str, value: float) -> None:
        """Fast-path float attribute. Same rationale as `set_attr_str`."""
        if self.handle == 0 or self._ended:
            return
        _span_set_attr_float(self.handle, key, value)

    def set_attr_bool(self, key: str, value: bool) -> None:
        """Fast-path bool attribute. Encoded as the strings
        "true"/"false" to match the generic `set_attr` behavior and
        the OpenTelemetry semantic convention for boolean attrs.

        Skips the 4-branch type-dispatch ladder in `set_attr`. For
        heavily-instrumented call sites (feature-flag status, cache
        hit/miss, auth-bypass indicators) this saves ~4 isinstance
        calls per attribute. For one-off bool attrs the generic
        `set_attr` is still fine.
        """
        if self.handle == 0 or self._ended:
            return
        _span_set_attr_str(self.handle, key, "true" if value else "false")

    def add_event(self, name: str) -> None:
        """Add a timestamped event to the span.

        Events are lightweight sub-span markers with a name and a
        nanosecond timestamp captured at call time. Use for state
        transitions, cache misses, retry attempts, etc. — anything
        that wants a timestamp without the overhead of a full child
        span.

        The event is packed into a 128-byte per-slot arena (v0.15.2).
        Each event uses `9 + len(name)` bytes (8-byte timestamp +
        1-byte name_len + name bytes). Overflow is silent — the
        arena holds 4-14 events depending on name lengths.

        Events appear in the drained span dict under `"events"` as
        a list of `{"name": str, "time_unix_nano": int}` dicts,
        matching the OpenTelemetry JSON event schema.

        Usage:
            with tracer.start_span("process_order") as span:
                span.add_event("payment_started")
                result = await charge_card(...)
                span.add_event("payment_completed")
                span.add_event("email_queued")
        """
        if self.handle == 0 or self._ended:
            return
        _span_add_event(self.handle, name)

    def set_status(self, code: int, message: str = "") -> None:
        """Set span status. `code` is 0=unset, 1=ok, 2=error.

        The `message` argument is accepted for API compatibility
        with the OpenTelemetry convention but is not stored in the
        native ring slot — use `set_attr("error.message", message)`
        if you need to record error details.
        """
        if self.handle == 0 or self._ended:
            return
        _span_set_status(self.handle, code)

    def end(self) -> None:
        """Finalize the span — writes end_ns and transitions the
        slot to `complete`. Safe to call multiple times (subsequent
        calls no-op)."""
        if self.handle == 0 or self._ended:
            return
        _span_end(self.handle)
        self._ended = True


# ── Trace ID generation ─────────────────────────────────────────────────────
#
# Trace IDs are 128 bits of cryptographic random — `os.urandom` calls
# /dev/urandom which is fine but EVERY syscall is ~700 ns of pure
# overhead. At 6.5 μs floor cost that's 11% of every request just to
# fetch 16 bytes. We pool 64 IDs (1024 bytes) per syscall instead and
# slice 16 bytes off the front, refilling when empty.
#
# Thread-safety: each pool is per-thread (`threading.local`) so we
# never need a lock and free-threaded Python 3.14t doesn't lose
# parallelism. The cryptographic strength is unchanged — we're
# slicing the same /dev/urandom output, just with fewer syscalls.

_POOL_REFILL_BYTES = 1024  # 64 trace IDs per syscall


class _TraceIdPool(threading.local):
    """Per-thread byte pool that supplies fresh trace_ids cheaply.

    Manual ``__slots__`` required: ``threading.local`` is a C extension
    type that manages per-thread storage internally. ``@dataclass(slots=True)``
    cannot be used with ``threading.local`` subclasses.
    """

    # Subclasses ``threading.local``, a C-extension type that manages
    # per-thread storage internally.
    # slots-required: a @dataclass cannot model a ``threading.local`` subclass.
    __slots__ = ("buf", "pos")

    def __init__(self) -> None:
        # Pre-fill on first construction so the first request doesn't
        # pay refill cost on top of the per-thread pool init.
        self.buf: bytes = os.urandom(_POOL_REFILL_BYTES)
        self.pos: int = 0

    def take(self) -> tuple[int, int]:
        """Return the next (high, low) pair, refilling on exhaustion."""
        if self.pos + 16 > len(self.buf):
            self.buf = os.urandom(_POOL_REFILL_BYTES)
            self.pos = 0
        end = self.pos + 16
        b = self.buf[self.pos : end]
        self.pos = end
        # int.from_bytes is a C built-in — single fast call per half.
        return (
            int.from_bytes(b[:8], "big"),
            int.from_bytes(b[8:], "big"),
        )


_trace_id_pool = _TraceIdPool()


def _new_trace_id() -> tuple[int, int]:
    """Generate a fresh 128-bit trace ID as (high, low) u64s.

    Backed by the per-thread `_TraceIdPool` — 64 trace IDs per
    `os.urandom` syscall instead of one per call. Cryptographic
    strength is unchanged (same source); only the syscall overhead
    is amortized.
    """
    return _trace_id_pool.take()


# ── Tracer ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Tracer:
    """Tracer entry point — one per application.

    The tracer owns the sampling policy and produces `Span`s that
    flow to the native ring. Apps usually construct one tracer at
    module load time and reuse it everywhere.

    Params:
      name:     human-readable service/tracer name (attached as
                `tracer.name` attribute to every root span)
      sampler:  policy applied at root spans. Children always
                inherit the parent's decision via `ParentBased`.
                Default: `ParentBased(RatioSample(0.01))` — 1% head
                sampling at the root.
    """

    name: str = "hyperdjango"
    sampler: SamplingPolicy = field(
        default_factory=lambda: ParentBased(root=RatioSample(0.01))
    )

    def _make_span(self, span_name: str) -> tuple[Span | NoopSpan, Any]:
        """Internal: decide sampling, claim slot, install contextvar.

        Returns (span, context_token) — the token is None if we
        didn't install a new context (noop path). Caller is
        responsible for calling `reset(token)` on exit.

        Performance note: when there's no parent context AND the
        sampler doesn't need a trace_id for its decision (NeverSample,
        AlwaysSample, ParentBased(NeverSample) — see
        `requires_trace_id_for_root_decision` on the sampler), we
        defer the trace_id allocation. For NeverSample (production
        unsampled fallback) the trace_id is never generated at all.
        For AlwaysSample we still need it because the slot claim
        records it, but we delay the work until AFTER the cheap
        sampler decision rather than computing it speculatively.
        """
        # Fast zero-cost path: telemetry disabled
        if not _tel_metrics.is_enabled():
            return _NOOP_SPAN, None

        parent = current()

        # Derive trace IDs: inherit from ANY parent context (even
        # unsampled ones — they still carry a valid trace ID so
        # nested spans share the same trace identity). `parent_handle`
        # is 0 when the parent was unsampled, which is the correct
        # value because there's no actual recorded parent span slot
        # to point back at.
        #
        # Performance: we inline `_trace_id_pool.take()` directly
        # rather than going through the `_new_trace_id` wrapper — one
        # less Python-level function call on the hot path.
        sampler = self.sampler
        if parent is not None:
            trace_high = parent.trace_id_high
            trace_low = parent.trace_id_low
            parent_handle = parent.span_id  # 0 for unsampled parents
            sampled = sampler.should_sample(parent, trace_low)
        else:
            parent_handle = 0
            # Defer trace_id generation when the sampler doesn't need
            # it for the root decision. Saves ~700 ns per request on
            # the unsampled hot path (NeverSample / ParentBased(Never)).
            if sampler.requires_trace_id_for_root_decision:
                trace_high, trace_low = _trace_id_pool.take()
                sampled = sampler.should_sample(None, trace_low)
            else:
                # Cheap decision first — only allocate the trace_id if
                # the span is actually going to be recorded.
                sampled = sampler.should_sample(None, 0)
                if sampled:
                    trace_high, trace_low = _trace_id_pool.take()
                else:
                    trace_high = 0
                    trace_low = 0

        # Native slot claim (or sentinel 0 if unsampled/dropped)
        handle = _span_start(
            trace_high,
            trace_low,
            parent_handle,
            span_name,
            sampled,
        )

        # Build the context for propagation — even unsampled spans
        # need a context so child spans inherit the decision.
        # Positional args are marginally faster than kwargs through the
        # dataclass-generated `__init__`.
        ctx = SpanContext(trace_high, trace_low, handle, parent_handle, sampled)
        token = set(ctx)

        if handle == 0:
            # Unsampled OR dropped — return a NoopSpan but keep the
            # context token so children see a valid trace ID and
            # the sampling decision propagates consistently.
            return _NOOP_SPAN, token

        return Span(handle, ctx), token

    def start_span(self, name: str) -> _SpanCM:
        """Sync context manager for a span.

        Returns a `_SpanCM` — a slotted dataclass with `__enter__`/
        `__exit__` defined directly. This is faster than a
        `@contextmanager`-decorated generator (which adds 4-5
        helper-function calls per `with` statement) and the public
        API is identical.

        Usage:
            with tracer.start_span("work") as span:
                span.set_attr("step", 1)
                ...
        """
        span, token = self._make_span(name)
        return _SpanCM(span=span, token=token)

    def start_span_async(self, name: str) -> _SpanCM:
        """Async context manager for a span.

        Returns the same `_SpanCM` instance — it implements both the
        sync and async context manager protocols. The body of an
        `async with` block can suspend at any await point; the CM
        only touches its own state at enter/exit.

        Usage:
            async with tracer.start_span_async("work") as span:
                span.set_attr("step", 1)
                await do_async()
        """
        span, token = self._make_span(name)
        return _SpanCM(span=span, token=token)

    def trace(self, name: str | None = None) -> Callable[[Callable], Callable]:
        """Decorator that wraps a sync or async function in a span.

        Usage:
            @tracer.trace("list_books")
            async def list_books(request):
                ...

            @tracer.trace()                       # name = function.__qualname__
            def compute_totals():
                ...
        """

        def decorator(fn: Callable) -> Callable:
            span_name = name or fn.__qualname__
            if inspect.iscoroutinefunction(fn):

                @wraps(fn)
                async def async_wrapper(*args, **kwargs):
                    async with self.start_span_async(span_name):
                        return await fn(*args, **kwargs)

                return async_wrapper

            @wraps(fn)
            def sync_wrapper(*args, **kwargs):
                with self.start_span(span_name):
                    return fn(*args, **kwargs)

            return sync_wrapper

        return decorator


# ── Module-level exports ────────────────────────────────────────────────────


def current_span() -> Span | NoopSpan | None:
    """Return the active span for the current task/thread, or None.

    Useful when deeply-nested code wants to attach attributes to
    whatever span happens to be active, without threading a
    tracer/span object through every call.
    """
    ctx = current()
    if ctx is None:
        return None
    if ctx.span_id == 0:
        return _NOOP_SPAN
    return Span(handle=ctx.span_id, context=ctx, _ended=False)


def bind_trace_context(logger: Logger) -> Logger:
    """Return a logger pre-bound with the active trace_id / span_id.

    The right way to attach long debug payloads (stack traces, request
    bodies, structured error data) when the per-slot 128-byte attribute
    budget can't hold them. Logs flow through the full
    `hyperdjango.logging` stack — unbounded, structured, written by
    the file/console/JSON sinks — and the JSON sink auto-promotes
    `trace_id`/`span_id`/`trace_flags` to top-level fields so log
    aggregators can join logs to spans by trace_id with zero extra
    mapping config.

    Usage:

        from hyperdjango.logging import logger
        from hyperdjango.telemetry import bind_trace_context

        log = bind_trace_context(logger)
        log.error("payment failed: {body}", body=long_response_body)

    When there is no active span, returns the logger unchanged — the
    helper is safe to call from any code path.

    The type hint references `hyperdjango.logging._logger.Logger`
    behind a TYPE_CHECKING guard so this module stays a leaf in the
    import graph (telemetry is a peer of logging, not a dependent).

    NOTE: As of v0.15.1, `configure_from_settings()` installs a
    global logger patcher (`auto_log_correlation_patcher`) that does
    this injection automatically for every log emission inside an
    active span. The explicit `bind_trace_context()` is still useful
    for: (a) opting in when telemetry is off, (b) attaching extra
    fields beyond trace_id/span_id, (c) early-boot code that runs
    before `configure_from_settings()` has been called.
    """
    ctx = current()
    if ctx is None:
        return logger
    return logger.bind(**ctx.to_log_extra())


def auto_log_correlation_patcher(record: dict) -> None:
    """Logger patcher that injects active trace context into log records.

    Reads `hyperdjango.telemetry.context.current()` at log emission time
    and adds `trace_id`, `span_id`, and `trace_flags` to the record's
    `extra` dict. The JSON sink in `hyperdjango.logging._sinks` already
    auto-promotes these three keys to top-level fields, so downstream
    log aggregators see flat `{"trace_id": "...", "msg": "..."}` and
    join logs to traces with zero extra config.

    Installed by `configure_from_settings()` when both:
      * `TELEMETRY_ENABLED` is True (master switch)
      * `TELEMETRY_AUTO_LOG_CORRELATION` is True (default)

    Hot-path cost: one `current()` ContextVar lookup per log emission
    when no span is active (~50 ns), one extra dict.update when one
    is. The patcher is a no-op when called outside any traced
    request, so libraries that emit logs at module-load time aren't
    affected.

    Idempotent and side-effect-free apart from the record mutation.
    Set the `TELEMETRY_AUTO_LOG_CORRELATION` setting to False to opt
    out (e.g., when chaining your own correlation logic).
    """
    ctx = current()
    if ctx is None:
        return
    extra = record.get("extra")
    if extra is None:
        # Record schema guarantees `extra` is always a dict, but be
        # defensive — a custom patcher upstream could have removed it.
        record["extra"] = ctx.to_log_extra()
        return
    # In-place merge — preserves existing keys (user's bind() values
    # take precedence over auto-injected trace context if they collide
    # on the same key, which is the principle of least surprise).
    for k, v in ctx.to_log_extra().items():
        if k not in extra:
            extra[k] = v
