"""
HyperDjango telemetry — native metrics + span recording (v0.15.0+).

This package is the public entry point for the unified telemetry
system. It sits on top of the runtime-dynamic metric registry shipped
in v0.14.19 (`zig/src/metrics_py.zig`) and the span ring buffer
landing in Phase 3.

Disabled by default. Apps opt in via:

    # 1. Env var (simplest for prod)
    HYPER_TELEMETRY_ENABLED=1

    # 2. Settings
    TELEMETRY = {"enabled": True, "service_name": "myapp"}

    # 3. Programmatic (services, tests)
    from hyperdjango.telemetry import enable
    enable()

When disabled (the default), every public method of every metric and
span class is a single-branch no-op — see the `_enabled` flag gate
in each class. This matches the zero-cost-when-disabled pattern
already used by `hyperdjango/database.py` for query tracking.

No protobuf. No OTLP SDK dependency. Span export is JSON via
`StdoutSink` / `InMemorySink`, or via user-provided sinks that
implement the `TelemetrySink` Protocol (any OTLP-compatible
backend or custom HTTP exporter).

Phase 2 exports (metrics facade only):

    from hyperdjango.telemetry import (
        Counter, CounterVec,
        Gauge,
        Histogram, HistogramVec,
        TelemetrySink,
        PrometheusSink,
        enable, disable, is_enabled,
    )

Phase 3 adds: Tracer, Span, SpanContext, sampling.*
Phase 4 adds: TelemetryMiddleware, StdoutSink, InMemorySink, w3c.*
"""

from hyperdjango.telemetry.assertions import TelemetryAssertions
from hyperdjango.telemetry.context import SpanContext, current
from hyperdjango.telemetry.metrics import (
    Counter,
    CounterVec,
    Gauge,
    Histogram,
    HistogramVec,
    disable,
    enable,
    is_enabled,
    register_sampler,
)
from hyperdjango.telemetry.middleware import TelemetryMiddleware
from hyperdjango.telemetry.sampling import (
    AlwaysSample,
    NeverSample,
    ParentBased,
    RatioSample,
    SamplingPolicy,
)
from hyperdjango.telemetry.setup import (
    TelemetryBootstrap,
    configure_from_settings,
    mount_gated_metrics,
)
from hyperdjango.telemetry.sinks.base import TelemetrySink
from hyperdjango.telemetry.sinks.memory import InMemorySink
from hyperdjango.telemetry.sinks.prometheus import PrometheusSink
from hyperdjango.telemetry.sinks.stdout import StdoutSink
from hyperdjango.telemetry.tracing import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSET,
    NoopSpan,
    Span,
    Tracer,
    auto_log_correlation_patcher,
    bind_trace_context,
    current_span,
)
from hyperdjango.telemetry.w3c import (
    format_traceparent,
    format_tracestate,
    parse_traceparent,
    parse_tracestate,
)

__all__ = [
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_UNSET",
    "AlwaysSample",
    "Counter",
    "CounterVec",
    "Gauge",
    "Histogram",
    "HistogramVec",
    "InMemorySink",
    "NeverSample",
    "NoopSpan",
    "ParentBased",
    "PrometheusSink",
    "RatioSample",
    "SamplingPolicy",
    "Span",
    "SpanContext",
    "StdoutSink",
    "TelemetryAssertions",
    "TelemetryBootstrap",
    "TelemetryMiddleware",
    "TelemetrySink",
    "Tracer",
    "auto_log_correlation_patcher",
    "configure_from_settings",
    "bind_trace_context",
    "current",
    "current_span",
    "disable",
    "enable",
    "format_traceparent",
    "format_tracestate",
    "is_enabled",
    "mount_gated_metrics",
    "parse_traceparent",
    "parse_tracestate",
    "register_sampler",
]
