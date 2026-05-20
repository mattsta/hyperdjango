"""
Telemetry sink (adapter) implementations.

Sinks are plug-in export destinations. Each sink implements the
`TelemetrySink` Protocol from `base.py`. Built-in sinks:

    PrometheusSink  — pull-based /metrics endpoint
    StdoutSink      — push-based JSON lines (Phase 4)
    InMemorySink    — test-only, captures spans + metrics (Phase 4)

User-provided adapters (any OTLP-compatible backend or custom HTTP
exporter) implement the same Protocol and pass to
`TelemetryMiddleware(sinks=[...])`.
"""

from hyperdjango.telemetry.sinks.base import TelemetrySink
from hyperdjango.telemetry.sinks.memory import InMemorySink
from hyperdjango.telemetry.sinks.prometheus import PrometheusSink
from hyperdjango.telemetry.sinks.stdout import StdoutSink

__all__ = ["InMemorySink", "PrometheusSink", "StdoutSink", "TelemetrySink"]
