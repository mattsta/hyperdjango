"""
Settings-driven telemetry bootstrap (v0.15.0).

`configure_from_settings()` reads `HYPER_TELEMETRY_*` env vars (via
`conf.get_setting`) and returns a fully wired `TelemetryMiddleware`
ready to `app.use()`. It also registers the middleware's shutdown
hook on `app` so the background drain thread is stopped cleanly on
`SIGTERM` / `hyper stop`.

Typical app-side usage:

    from hyperdjango import HyperApp
    from hyperdjango.telemetry import configure_from_settings

    app = HyperApp()
    telemetry = configure_from_settings(app)
    if telemetry is not None:
        app.get("/metrics")(telemetry.prometheus_sink.handler)

Environment / settings surface:

    HYPER_TELEMETRY_ENABLED=1              # master switch
    HYPER_TELEMETRY_SERVICE_NAME=myapp     # default tracer name
    HYPER_TELEMETRY_SAMPLE_RATIO=0.1       # head sampling rate
    HYPER_TELEMETRY_DRAIN_INTERVAL=0.5     # seconds
    HYPER_TELEMETRY_EXTRACT_TRACEPARENT=1  # honor inbound W3C
    HYPER_TELEMETRY_SINKS=prometheus,stdout  # comma-separated

When `TELEMETRY_ENABLED` is False (the default), this function
returns None and never imports any sink. It's safe to call
unconditionally at app startup — there is zero cost when telemetry
is off.

Return value: an opaque `TelemetryBootstrap` dataclass holding the
middleware + the attached sinks. The `prometheus_sink` attribute is
None when the Prometheus sink wasn't enabled; the `spans_sink` lookup
returns the first non-Prometheus sink (or None).
"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hyperdjango._hyperdjango_native import (
    _span_capacity,
    _span_configure,
    _span_is_operational,
)
from hyperdjango.conf import get_setting
from hyperdjango.exceptions import HTTPException
from hyperdjango.logging import logger
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.telemetry.middleware import TelemetryMiddleware
from hyperdjango.telemetry.sampling import ParentBased, RatioSample
from hyperdjango.telemetry.sinks.base import TelemetrySink
from hyperdjango.telemetry.sinks.memory import InMemorySink
from hyperdjango.telemetry.sinks.prometheus import PrometheusSink
from hyperdjango.telemetry.sinks.stdout import StdoutSink
from hyperdjango.telemetry.tracing import Tracer, auto_log_correlation_patcher

if TYPE_CHECKING:
    from hyperdjango.app import HyperApp


@dataclass(slots=True)
class TelemetryBootstrap:
    """Result of a successful `configure_from_settings()` call.

    Fields:
      middleware:     The `TelemetryMiddleware` — pass to `app.use()`.
      prometheus_sink: The `PrometheusSink` instance if enabled, else
                      None. Mount its handler at `/metrics`.
      sinks:          Ordered list of all attached sinks (for tests).
    """

    middleware: TelemetryMiddleware
    sinks: list[TelemetrySink]
    prometheus_sink: PrometheusSink | None = None
    stdout_sink: StdoutSink | None = None
    memory_sink: InMemorySink | None = None


# ── Sink factory ────────────────────────────────────────────────────────────


def _build_sinks(
    names: list[str],
) -> tuple[
    list[TelemetrySink],
    PrometheusSink | None,
    StdoutSink | None,
    InMemorySink | None,
]:
    """Instantiate sinks from their short names.

    Duplicate names are collapsed — each sink type is created at
    most once per boot.
    """
    prom: PrometheusSink | None = None
    stdout: StdoutSink | None = None
    memory: InMemorySink | None = None
    seen: set[str] = set()
    sinks: list[TelemetrySink] = []
    for raw in names:
        name = raw.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        if name == "prometheus":
            prom = PrometheusSink()
            sinks.append(prom)
        elif name == "stdout":
            stdout = StdoutSink()
            sinks.append(stdout)
        elif name == "memory":
            memory = InMemorySink()
            sinks.append(memory)
        else:
            raise ValueError(
                f"Unknown telemetry sink {name!r}. "
                f"Valid: prometheus, stdout, memory. "
                f"For custom sinks, instantiate TelemetryMiddleware directly."
            )
    return sinks, prom, stdout, memory


# ── Public bootstrap ────────────────────────────────────────────────────────


def configure_from_settings(app: HyperApp | None = None) -> TelemetryBootstrap | None:
    """Build a `TelemetryMiddleware` from `HYPER_TELEMETRY_*` settings.

    Returns None when `TELEMETRY_ENABLED` is False — the caller can
    short-circuit without any further wiring.

    If `app` is passed, the middleware is registered automatically
    via `app.use(middleware)` and `app.on_shutdown(middleware.shutdown)`.
    Otherwise the caller is responsible for wiring both.

    Example:

        telemetry = configure_from_settings(app)
        if telemetry is not None and telemetry.prometheus_sink is not None:
            app.get("/metrics")(telemetry.prometheus_sink.handler)
    """
    if not get_setting("TELEMETRY_ENABLED"):
        return None

    service_name = get_setting("TELEMETRY_SERVICE_NAME")
    sample_ratio = get_setting("TELEMETRY_SAMPLE_RATIO")
    drain_interval = get_setting("TELEMETRY_DRAIN_INTERVAL")
    extract_traceparent = get_setting("TELEMETRY_EXTRACT_TRACEPARENT")
    sink_names = get_setting("TELEMETRY_SINKS") or []
    span_ring_capacity = int(get_setting("TELEMETRY_SPAN_RING_CAPACITY"))

    # Configure the native ring BEFORE the first span is recorded.
    #
    # `_span_configure` semantics:
    #   - ValueError: bad capacity (not power of 2, or out of range)
    #     → fatal, re-raise (the user's setting is broken; better to
    #     fail loud at boot than silently fall back to a default).
    #   - RuntimeError: ring is already operational at a different
    #     capacity (live span recording in progress) → log + ignore;
    #     reconfiguration of a live ring would dangle in-flight
    #     handles. Common in tests that call configure_from_settings
    #     more than once.
    #
    # Skip the call entirely if the requested capacity matches the
    # current configured/live value — saves one FFI call and one log
    # line in the common case where everything is already aligned.
    if span_ring_capacity != _span_capacity():
        try:
            _span_configure(span_ring_capacity)
        except RuntimeError as exc:
            # Distinguish "ring is live and serving" (the safe case)
            # from "init failed and we can't recover" (alarming).
            if _span_is_operational():
                logger.warning(
                    "telemetry: span ring already operational at {live} "
                    "slots; requested {wanted} ignored (live reconfig "
                    "would dangle in-flight handles): {err}",
                    live=_span_capacity(),
                    wanted=span_ring_capacity,
                    err=exc,
                )
            else:
                logger.error(
                    "telemetry: span ring init previously failed; "
                    "configure({wanted}) raised {err}. Telemetry will "
                    "drop every span until init succeeds.",
                    wanted=span_ring_capacity,
                    err=exc,
                )
        except ValueError as exc:
            logger.error(
                "telemetry: invalid TELEMETRY_SPAN_RING_CAPACITY {wanted}: "
                "{err}. Must be a power of 2 in [256, 16777216].",
                wanted=span_ring_capacity,
                err=exc,
            )
            raise

    tracer = Tracer(
        name=str(service_name),
        sampler=ParentBased(root=RatioSample(float(sample_ratio))),
    )
    sinks, prom, stdout, memory = _build_sinks(list(sink_names))
    middleware = TelemetryMiddleware(
        tracer=tracer,
        sinks=sinks,
        drain_interval_seconds=float(drain_interval),
        extract_traceparent=bool(extract_traceparent),
    )

    # Global enable — single source of truth for the fast-path flag.
    _tel_metrics.enable()

    # Auto-correlate every log record with the active span (v0.15.1).
    # Must run AFTER `enable()` so the patcher's `current()` lookup
    # sees a real ContextVar — and BEFORE `app.use(middleware)` so
    # any logs emitted from middleware setup are correlated. Skipped
    # when the user opts out via TELEMETRY_AUTO_LOG_CORRELATION=False.
    #
    # We compose with any existing core.patcher so the user's own
    # patcher (if any) still runs. Order: user patcher first, then
    # the trace correlator — that way the user can mutate the record
    # however they like and we still inject trace_id last (last write
    # wins for any contested keys, but `auto_log_correlation_patcher`
    # is in-place merge with first-write-wins so it never overwrites
    # the user's choice).
    auto_correlate = bool(get_setting("TELEMETRY_AUTO_LOG_CORRELATION"))
    if auto_correlate:
        existing = logger._core.patcher
        if existing is None:
            logger._core.patcher = auto_log_correlation_patcher
        elif existing is not auto_log_correlation_patcher:
            # Chain the existing user patcher and our injector. The
            # user's patcher runs first so the trace context is
            # always the LAST mutation — guarantees consistent
            # ordering regardless of the user's patcher behavior.
            user_patcher = existing

            def _chained_patcher(record: dict) -> None:
                user_patcher(record)
                auto_log_correlation_patcher(record)

            logger._core.patcher = _chained_patcher

    if app is not None:
        app.use(middleware)
        app.on_shutdown(middleware.shutdown)

    return TelemetryBootstrap(
        middleware=middleware,
        sinks=sinks,
        prometheus_sink=prom,
        stdout_sink=stdout,
        memory_sink=memory,
    )


# ── Auth-gated metrics scrape ────────────────────────────────────────────────


def mount_gated_metrics(
    app,
    handler: Callable[..., Awaitable],
    *,
    resolve: Callable[..., object],
    on_deny: Callable[..., object] | None = None,
    path: str = "/metrics",
) -> None:
    """Register an auth-gated Prometheus scrape route on ``app``.

    A ``/metrics`` body exposes a deployment's metric names and traffic shape, so
    the scrape must not be anonymous. This wraps the sink ``handler`` behind an
    identity gate and registers it via ``app.get(path)`` — the single policy both
    serving apps otherwise hand-roll around ``app.get("/metrics")``.

    The framework stays app-agnostic: every hook is a plain callable, so this
    references no application code.

    - ``resolve(request)`` authenticates the caller and raises
      :class:`~hyperdjango.exceptions.HTTPException` on failure (typically a thin
      wrapper over :func:`hyperdjango.identity.resolve_identity` that also records
      the resolved method/fingerprint for the app's audit context). Its return
      value is ignored — any side effect the app needs lives in the wrapper. It
      may be sync or async; a returned awaitable is awaited.
    - ``on_deny(request, exc)`` (optional) runs before the denial is re-raised,
      for the app's audit row and/or a denied-scrape counter. It may be sync or
      async; a returned awaitable is awaited.

    On success the wrapped route returns ``await handler(request)`` — the sink's
    Prometheus response — unchanged. Only an ``HTTPException`` from ``resolve``
    triggers ``on_deny`` and re-raises (fail closed); any other exception
    propagates untouched. Returns ``None``.
    """

    async def _gated(request):
        try:
            resolved = resolve(request)
            if inspect.isawaitable(resolved):
                await resolved
        except HTTPException as exc:
            if on_deny is not None:
                denied = on_deny(request, exc)
                if inspect.isawaitable(denied):
                    await denied
            raise
        return await handler(request)

    app.get(path)(_gated)
    return None
