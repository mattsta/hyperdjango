# OTLP Span Sink

Reference implementation of an OTLP/HTTP JSON span exporter that plugs into HyperDjango's telemetry system. Converts HyperDjango span batches to the standard OpenTelemetry OTLP/HTTP JSON format and POSTs them to any OTLP-compatible collector. Uses only stdlib (urllib.request) with zero extra dependencies.

## Features

- TelemetrySink-compatible: drop-in for HyperDjango's TelemetryMiddleware
- OTLP/HTTP JSON protocol (not protobuf) -- simple and debuggable
- Zero external dependencies beyond stdlib + hyperdjango
- Configurable via standard OTel environment variables
- Resource attributes: service.name, telemetry.sdk.name, telemetry.sdk.language
- Proper OTLP attribute type dispatch (string, int, float, bool)
- Best-effort delivery: collector failures logged, never crash the app
- Coexists with PrometheusSink (traces via OTLP, metrics via Prometheus)

## Compatible Collectors

Any collector that implements the OTLP/HTTP JSON receive endpoint will work
unchanged — the sink emits the standard OTLP/HTTP JSON wire format.

## Setup

This is a library module, not a standalone app. Copy or import it into your project:

```python
from services.otlp_sink.otlp_sink import OTLPSpanSink

# Option 1: Pass directly to TelemetryMiddleware
sink = OTLPSpanSink(service_name="my-service")
app.use(TelemetryMiddleware(sinks=[PrometheusSink(), sink]))

# Option 2: Append to an existing telemetry setup
telemetry = configure_from_settings(app)
sink = OTLPSpanSink(service_name="my-service")
telemetry.middleware._worker.sinks.append(sink)
```

## Configuration

Standard OpenTelemetry environment variables:

```bash
# Collector base URL (without /v1/traces)
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

# Extra HTTP headers (e.g., API keys for hosted collectors)
export OTEL_EXPORTER_OTLP_HEADERS=x-api-key=abc123,x-team=backend

# Service name for the resource attribute
export OTEL_SERVICE_NAME=my-service
```

Or pass values directly:

```python
sink = OTLPSpanSink(
    endpoint="https://collector.example.com:4318",
    service_name="my-service",
    extra_headers={"Authorization": "Bearer token123"},
    timeout_s=10.0,
    verbose=True,  # Print export status to stderr
)
```

## Architecture

The sink implements the `TelemetrySink` protocol with four methods:

- `export_spans(spans)` -- Converts spans to OTLP JSON and POSTs to `/v1/traces`
- `export_metrics(prometheus_text)` -- No-op (use PrometheusSink for metrics)
- `flush()` -- No-op (no internal buffering, each batch POSTs immediately)
- `close()` -- No-op (urllib uses per-request connections)

Metric export is intentionally excluded. The recommended production setup is PrometheusSink for metrics (scraped via the standard `/metrics` endpoint) and this OTLP sink for distributed traces.
