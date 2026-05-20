"""
TelemetryMiddleware — per-request spans + background drain thread (v0.15.0).

One middleware, two responsibilities:

  1. **Per-request span**: wrap every request in a `tracer.start_span`
     that auto-propagates W3C trace-context (reads incoming
     `traceparent` header, re-emits on outbound child calls),
     attaches the usual HTTP attributes, and records error status
     on exceptions. Zero-cost when telemetry is disabled: one
     `is_enabled()` branch and out.

  2. **Background drain thread**: a daemon thread started at
     construction time drains the native span ring into the
     configured sinks every `drain_interval_seconds` (default
     1.0). On the same interval it pulls the latest Prometheus
     exposition text and pushes to every sink's `export_metrics`.
     Shutdown is coordinated via a `threading.Event` so the final
     drain runs as part of `@app.on_shutdown`.

Wiring:

    from hyperdjango.telemetry import (
        Tracer, enable, PrometheusSink, StdoutSink, TelemetryMiddleware,
    )

    tracer = Tracer("myapp")
    prom = PrometheusSink()
    middleware = TelemetryMiddleware(
        tracer=tracer,
        sinks=[prom, StdoutSink()],
    )
    app.use(middleware)
    app.on_shutdown(middleware.shutdown)
    app.get("/metrics")(prom.handler)
    enable()

The middleware takes a `Tracer` so you can share sampling policy
across the middleware and your own direct `tracer.start_span()`
calls in handlers. If you pass `tracer=None`, a default
`Tracer("hyperdjango", ParentBased(RatioSample(0.01)))` is created
for you.

Shutdown contract: call `middleware.shutdown()` (or register with
`app.on_shutdown`). This stops the drain thread, runs one final
drain, calls `flush()` + `close()` on every sink, and unblocks any
pending scrape. Idempotent — safe to call twice.

Thread safety: the drain thread is the only writer to sinks under
the middleware's control. If user code also calls `sink.export_*`
directly (e.g., manually draining in tests), sinks must handle
concurrent access — `InMemorySink` does, `StdoutSink` inherits
CPython's per-write stdout lock, and user sinks take the same
contract. See `sinks/base.py` for the `TelemetrySink` protocol.
"""

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from hyperdjango._hyperdjango_native import _span_drain
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.telemetry.context import reset as _ctx_reset
from hyperdjango.telemetry.context import set as _ctx_set
from hyperdjango.telemetry.sampling import ParentBased, RatioSample
from hyperdjango.telemetry.sinks.base import TelemetrySink
from hyperdjango.telemetry.tracing import STATUS_ERROR, Span, Tracer
from hyperdjango.telemetry.w3c import format_traceparent, parse_traceparent

# ── HTTP-level native metrics (P5.1) ────────────────────────────────────────
#
# Registered at module-load time so every TelemetryMiddleware instance
# shares the same underlying native handles — one process-wide time
# series per (method, status) pair, as Prometheus convention demands.
# Zero cost when telemetry is disabled via the _enabled flag gate in
# Counter / HistogramVec.

_http_requests_total = _tel_metrics.CounterVec(
    "hyperdjango_http_requests_total",
    "Total HTTP requests",
    label_names=("method", "status"),
)
_http_request_duration_seconds = _tel_metrics.HistogramVec(
    "hyperdjango_http_request_duration_seconds",
    "HTTP request duration in seconds",
    label_names=("method",),
)

# Pre-interned status-code strings for the common HTTP responses.
# `str(status_code)` allocates a fresh string per request otherwise;
# with this cache the hot path just does a dict lookup and gets an
# interned string back. Unknown statuses fall through to `str()`.
#
# The cache is built dynamically at module-load time from a canonical
# set of common HTTP statuses (2xx, 3xx, 4xx, 5xx). Adding new codes
# is a one-line edit to the frozenset — no need to keep the dict and
# the list in sync. The `0 → "500"` sentinel is added separately so
# exception paths that never set `status_code` still get classified
# as 500 in the Prometheus label.

_COMMON_HTTP_STATUSES: frozenset[int] = frozenset(
    {
        200,
        201,
        202,
        204,
        206,
        301,
        302,
        303,
        304,
        307,
        308,
        400,
        401,
        403,
        404,
        405,
        406,
        408,
        409,
        410,
        411,
        412,
        413,
        414,
        415,
        418,
        422,
        423,
        424,
        429,
        500,
        501,
        502,
        503,
        504,
        505,
        507,
        511,
    }
)

# `{code: str(code)}` is Python's idiomatic way to intern per-value
# strings; the result is a tight readonly dict owning the canonical
# string for each common status.
_STATUS_STR_CACHE: dict[int, str] = {code: str(code) for code in _COMMON_HTTP_STATUSES}
# Sentinel — treat "never set" (0) as 500 for metric classification.
_STATUS_STR_CACHE[0] = "500"


def _status_str(code: int) -> str:
    """Return interned string form of an HTTP status code.

    Hot-path helper for the CounterVec label tuple — `str(200)` would
    allocate every request. For the top-30+ common statuses we return
    a pre-interned string; exotic ones fall through to `str()`. The
    cache is built at module load time from `_COMMON_HTTP_STATUSES`
    so adding new codes is a one-line frozenset edit.
    """
    cached = _STATUS_STR_CACHE.get(code)
    if cached is not None:
        return cached
    return str(code)


# ── Drain worker ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _DrainWorker:
    """Background thread that periodically pulls the span ring and
    metric exposition, fanning results out to the sinks.

    Internal — end users get this automatically when they wire up
    `TelemetryMiddleware`. The only knob exposed publicly is the
    interval (drain_interval_seconds in the middleware).
    """

    sinks: list[TelemetrySink]
    interval: float
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = field(default=None, init=False)
    _started: bool = field(default=False, init=False)
    _stopped: bool = field(default=False, init=False)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        t = threading.Thread(
            target=self._run,
            name="hyper-telemetry-drain",
            daemon=True,
        )
        self._thread = t
        t.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the worker to exit, perform a final drain, join."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        # One last drain after the loop has exited — picks up anything
        # that completed between the previous tick and shutdown.
        self.drain_once()
        for sink in self.sinks:
            try:
                sink.flush()
            # blind-except: sink flush during shutdown teardown must never propagate; failure is reported to stderr and teardown continues
            except Exception as exc:
                # Sink exceptions during shutdown must never propagate
                _print_sink_error("flush", sink, exc)
        for sink in self.sinks:
            try:
                sink.close()
            # blind-except: sink close during shutdown teardown must never propagate; every sink must still get a close attempt
            except Exception as exc:
                _print_sink_error("close", sink, exc)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            # Wait up to `interval` seconds, break early if shutdown.
            if self._stop_event.wait(timeout=self.interval):
                break
            self.drain_once()

    def drain_once(self) -> None:
        """Pull spans + metrics from native, push to sinks.

        Public because tests and the `TelemetryMiddleware.drain_now`
        shortcut need to be able to force a synchronous drain without
        waiting on the daemon thread interval.

        Errors on any single sink are isolated so one broken sink
        cannot starve the others. Failures are printed (not logged
        through `hyperdjango.logging` to avoid a reentrant loop if
        the logger itself is instrumented).
        """
        # Periodic samplers — give subsystems that own external state
        # (pg.zig pool counters, queue depths, etc.) a chance to push
        # their latest snapshot into the metric registry BEFORE we
        # collect the Prometheus exposition. Errors are isolated per
        # sampler via `_run_samplers` and reported here.
        for exc in _tel_metrics._run_samplers():
            _print_sink_error("sampler", None, exc)
        # Spans
        try:
            spans = _span_drain()
        # blind-except: a failed native span drain must not crash the daemon drain thread; error is reported and drain proceeds with no spans
        except Exception as exc:
            _print_sink_error("span_drain", None, exc)
            spans = []
        if spans:
            for sink in self.sinks:
                try:
                    sink.export_spans(spans)
                # blind-except: one broken sink's span export must not starve the other sinks or crash the drain thread; error is reported per sink
                except Exception as exc:
                    _print_sink_error("export_spans", sink, exc)
        # Metrics exposition
        try:
            text = _tel_metrics.collect_prometheus_text()
        # blind-except: a failed Prometheus exposition build must not crash the daemon drain thread; error is reported and drain proceeds with empty text
        except Exception as exc:
            _print_sink_error("collect_prometheus_text", None, exc)
            text = b""
        if text:
            for sink in self.sinks:
                try:
                    sink.export_metrics(text)
                # blind-except: one broken sink's metrics export must not starve the other sinks or crash the drain thread; error is reported per sink
                except Exception as exc:
                    _print_sink_error("export_metrics", sink, exc)


def _print_sink_error(op: str, sink: TelemetrySink | None, exc: BaseException) -> None:
    """Best-effort error reporter for drain/sink failures.

    Uses plain `print(..., flush=True)` rather than the framework
    logger to avoid a reentrant logging loop if the logger itself is
    instrumented with telemetry. Routed to stderr so log collectors
    pick it up at WARN/ERROR level by convention.
    """
    name = type(sink).__name__ if sink is not None else "telemetry"
    print(
        f"[hyper-telemetry] {name}.{op} raised {type(exc).__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )


# ── TelemetryMiddleware ─────────────────────────────────────────────────────


def _default_span_name(request: Request) -> str:
    return f"{request.method} {request.path}"


@dataclass(slots=True)
class TelemetryMiddleware:
    """Request-span middleware + background drain thread.

    Params:
      tracer:        Shared `Tracer` used for root spans on every
                     request. Defaults to `Tracer("hyperdjango",
                     ParentBased(RatioSample(0.01)))`.
      sinks:         List of `TelemetrySink` implementations (any
                     combination of built-ins + user adapters).
      drain_interval_seconds: How often the background thread
                     pulls the ring and emits to sinks. Default 1.0.
      span_name_fn:  Optional hook to derive the span name from
                     the incoming request. Default:
                     `f"{request.method} {request.path}"`.
      extract_traceparent: If True (default), parse the incoming
                     `traceparent` header and use it as the parent
                     context. Set False to always start a fresh
                     trace (useful for internal-only services).
    """

    tracer: Tracer | None = None
    sinks: list[TelemetrySink] = field(default_factory=list)
    drain_interval_seconds: float = 1.0
    extract_traceparent: bool = True
    span_name_fn: Callable[[Request], str] = field(default=_default_span_name)
    _worker: _DrainWorker = field(init=False, repr=False)
    _shutdown_done: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.tracer is None:
            self.tracer = Tracer(
                name="hyperdjango",
                sampler=ParentBased(root=RatioSample(0.01)),
            )
        self._worker = _DrainWorker(
            sinks=list(self.sinks),
            interval=self.drain_interval_seconds,
        )
        self._worker.start()

    # ── ASGI / HyperApp middleware protocol ────────────────────────────────

    async def __call__(self, request: Request, call_next):
        # Fast zero-cost path: telemetry globally disabled
        if not _tel_metrics.is_enabled():
            return await call_next(request)

        # Extract W3C trace-context from the inbound request (if any)
        # BEFORE starting our span so the tracer sees a pre-populated
        # context. The tracer's _make_span reads `current()` for
        # parent linkage — we install the parsed context under that
        # key for the duration of the request.
        parent_token = None
        # Initialise the metrics inputs BEFORE the try so the finally can
        # always emit them even if traceparent parsing or span_name_fn
        # raises. `request.method` / monotonic_ns are cheap, exception-free
        # attribute/clock reads.
        start_ns = time.monotonic_ns()
        method = request.method
        status_code = 0  # sentinel: set below once we know the result
        try:
            # _ctx_set and span-name computation live INSIDE the try so a
            # failure after the contextvar is set still reaches the finally
            # that resets it — otherwise parent_token would leak.
            if self.extract_traceparent:
                header = request.headers.get("traceparent")
                parent_ctx = parse_traceparent(header)
                if parent_ctx is not None:
                    parent_token = _ctx_set(parent_ctx)

            span_name = self.span_name_fn(request)
            with self.tracer.start_span(span_name) as span:
                # Cache the "is recorded" check once — `span.handle`
                # is a single attribute load on both `Span` (dataclass
                # slot) and `NoopSpan` (class-level constant == 0) so
                # no isinstance call is needed. Saves 3x isinstance
                # per request on the recorded-span hot path.
                is_recorded = span.handle != 0
                if is_recorded:
                    _attach_http_attrs(span, request)
                try:
                    response = await call_next(request)
                except Exception:
                    status_code = 500
                    # error.type/error.message/status are written once by
                    # _SpanCM.__exit__ as the exception propagates through the
                    # `with self.tracer.start_span(...)` block — writing them
                    # here too duplicated keys and churned the attr buffer.
                    raise
                else:
                    status_code = int(response.status)
                    if is_recorded:
                        _finalize_span_response(span, response)
                    return response
        finally:
            if parent_token is not None:
                _ctx_reset(parent_token)
            # Native HTTP metrics — always emitted when telemetry is
            # enabled (we already took the fast-path branch above).
            # Using inc_tuple/observe_tuple to skip the dict lookup on
            # the hot path. `_status_str` caches the str(status_code)
            # conversion so common statuses don't reallocate per req.
            duration_s = (time.monotonic_ns() - start_ns) / 1e9
            _http_requests_total.inc_tuple((method, _status_str(status_code)))
            _http_request_duration_seconds.observe_tuple((method,), duration_s)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop the background drain, flush, close all sinks.

        Idempotent. Safe to register with `app.on_shutdown`.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._worker.stop(timeout=timeout)

    def drain_now(self) -> None:
        """Synchronously trigger one drain cycle.

        Tests use this to force a flush without waiting on the
        drain interval. Never call this from a hot handler — it
        blocks on the native span_drain FFI and every sink's
        export method in sequence.
        """
        self._worker.drain_once()


# ── Per-request helpers ─────────────────────────────────────────────────────


def _attach_http_attrs(span: Span, request: Request) -> None:
    """Attach OpenTelemetry HTTP semantic convention attributes.

    All values are statically-known strings so we take the
    `set_attr_str` fast-path — skips the 4-branch isinstance ladder
    in `set_attr` that would otherwise fire for every attribute.
    Saves ~15 isinstance calls per recorded request.
    """
    span.set_attr_str("http.method", request.method)
    span.set_attr_str("http.route", request.path)
    span.set_attr_str("net.peer.ip", request.client_ip)
    user_agent = request.headers.get("user-agent")
    if user_agent is not None:
        span.set_attr_str("http.user_agent", user_agent)


def _finalize_span_response(span: Span, response: Response) -> None:
    """Attach the response status to the span and propagate the
    active trace-context via an outbound `traceparent` header so
    downstream consumers can continue the trace.
    """
    status_int = int(response.status)
    span.set_attr_int("http.status_code", status_int)
    if status_int >= 500:
        span.set_status(STATUS_ERROR)
    # The response.headers dict is always writable on Response
    response.headers["traceparent"] = format_traceparent(span.context)
