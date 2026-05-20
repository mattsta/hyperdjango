"""
Example OTLP/HTTP JSON span exporter sink for HyperDjango telemetry.

Copy this file into your project and register as a TelemetrySink:

    from otlp_sink import OTLPSpanSink

    sink = OTLPSpanSink(service_name="my-service")
    app.use(TelemetryMiddleware(sinks=[PrometheusSink(), sink]))

Or via configure_from_settings with a manual sink:

    telemetry = configure_from_settings(app)
    sink = OTLPSpanSink(service_name="my-service")
    telemetry.middleware._worker.sinks.append(sink)

Sends span batches to an OTLP/HTTP collector in JSON format (not protobuf).
Uses only stdlib (urllib.request) — no opentelemetry SDK, no grpc, no requests.

Configuration via env vars (standard OTel conventions):

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # collector URL
    OTEL_EXPORTER_OTLP_HEADERS=x-api-key=abc123         # extra headers
    OTEL_SERVICE_NAME=my-service                         # service.name resource attr

Compatible with any collector that implements the OTLP/HTTP JSON
receive endpoint — the wire format is the standard one defined by
the OpenTelemetry spec.

Metric export is intentionally NOT included — use PrometheusSink for
metrics and this sink for distributed traces. The two sinks coexist
on the same middleware with zero conflict.

Dependencies: none beyond stdlib + hyperdjango (already installed)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# OTLP status code mapping — our internal codes match OTel's proto enum:
#   0 = STATUS_CODE_UNSET
#   1 = STATUS_CODE_OK
#   2 = STATUS_CODE_ERROR
_OTLP_STATUS_MAP: dict[int, str] = {
    0: "STATUS_CODE_UNSET",
    1: "STATUS_CODE_OK",
    2: "STATUS_CODE_ERROR",
}


def _to_otlp_attributes(attrs: dict) -> list[dict]:
    """Convert a flat {key: value} dict to OTLP attribute array format.

    OTLP attributes are [{key, value: {stringValue|intValue|...}}].
    We dispatch on Python type to pick the right OTLP value wrapper.
    """
    result: list[dict] = []
    for k, v in attrs.items():
        if isinstance(v, bool):
            result.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            result.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            result.append({"key": k, "value": {"doubleValue": v}})
        else:
            result.append({"key": k, "value": {"stringValue": str(v)}})
    return result


def _span_to_otlp(span: dict) -> dict:
    """Convert a HyperDjango span dict to OTLP JSON span format.

    Input shape (from TelemetrySink.export_spans):
        {
            "trace_id":  "abc123...",
            "span_id":   "def456...",
            "parent_id": "..." | "",
            "name":      "GET /api/books",
            "start_time_unix_nano": int,
            "end_time_unix_nano":   int,
            "attributes": dict[str, str|int|float],
            "status":     {"code": 0|1|2, "message": "..."},
        }

    Output: OTLP JSON span object per
    https://opentelemetry.io/docs/specs/otlp/#otlphttp-request
    """
    status_code = span.get("status", {}).get("code", 0)
    otlp_span: dict = {
        "traceId": span.get("trace_id", ""),
        "spanId": span.get("span_id", ""),
        "name": span.get("name", ""),
        "kind": 2,  # SPAN_KIND_SERVER (most HyperDjango spans are HTTP server)
        "startTimeUnixNano": str(span.get("start_time_unix_nano", 0)),
        "endTimeUnixNano": str(span.get("end_time_unix_nano", 0)),
        "status": {
            "code": _OTLP_STATUS_MAP.get(status_code, "STATUS_CODE_UNSET"),
        },
    }
    parent_id = span.get("parent_id", "")
    if parent_id:
        otlp_span["parentSpanId"] = parent_id

    attrs = span.get("attributes")
    if attrs:
        otlp_span["attributes"] = _to_otlp_attributes(attrs)

    status_msg = span.get("status", {}).get("message", "")
    if status_msg:
        otlp_span["status"]["message"] = status_msg

    return otlp_span


@dataclass(slots=True)
class OTLPSpanSink:
    """OTLP/HTTP JSON span exporter — TelemetrySink-compatible.

    Sends span batches to an OpenTelemetry collector via the standard
    OTLP/HTTP JSON protocol (`POST /v1/traces`). Uses only stdlib
    (urllib.request) so there are ZERO extra dependencies.

    Params:
      endpoint:      Collector base URL (without `/v1/traces`).
                     Defaults to OTEL_EXPORTER_OTLP_ENDPOINT env var,
                     or http://localhost:4318 if unset.
      service_name:  Value for the `service.name` resource attribute.
                     Defaults to OTEL_SERVICE_NAME env var, or
                     "hyperdjango".
      extra_headers: Additional HTTP headers (e.g., API keys).
                     Defaults to OTEL_EXPORTER_OTLP_HEADERS env var
                     parsed as `key=value,key=value`.
      timeout_s:     HTTP request timeout in seconds. Default 5.
      verbose:       Print export status to stderr. Default False.

    Metric export:
      This sink ignores `export_metrics` — use PrometheusSink for
      metrics scraping. Prometheus + OTLP traces coexist perfectly.
    """

    endpoint: str = ""
    service_name: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 5.0
    verbose: bool = False
    _url: str = field(init=False, repr=False, default="")
    _resource_attrs: list[dict] = field(init=False, repr=False, default_factory=list)
    _headers: dict[str, str] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        # Resolve endpoint from env if not explicitly set
        if not self.endpoint:
            self.endpoint = os.environ.get(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
            )
        self._url = f"{self.endpoint.rstrip('/')}/v1/traces"

        # Resolve service name
        if not self.service_name:
            self.service_name = os.environ.get("OTEL_SERVICE_NAME", "hyperdjango")

        # Build resource attributes
        self._resource_attrs = [
            {"key": "service.name", "value": {"stringValue": self.service_name}},
            {"key": "telemetry.sdk.name", "value": {"stringValue": "hyperdjango"}},
            {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
        ]

        # Build headers — merge env var headers with explicit ones
        self._headers = {
            "Content-Type": "application/json",
        }
        env_headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
        if env_headers:
            for pair in env_headers.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    self._headers[k.strip()] = v.strip()
        self._headers.update(self.extra_headers)

    # ── TelemetrySink protocol ─────────────────────────────────────────────

    def export_metrics(self, prometheus_text: bytes) -> None:
        """No-op — use PrometheusSink for Prometheus-style metric scraping.

        OTLP metric export requires converting Prometheus text to OTLP
        ResourceMetrics, which is a substantial format transform. For
        production, pair this sink with PrometheusSink and let your
        Prometheus-compatible scraper consume /metrics directly — it's
        simpler and more reliable than proxying through OTLP.
        """
        return

    def export_spans(self, spans: list[dict]) -> None:
        """Convert span batch to OTLP JSON and POST to the collector."""
        if not spans:
            return

        otlp_spans = [_span_to_otlp(s) for s in spans]
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": self._resource_attrs},
                    "scopeSpans": [
                        {
                            "scope": {"name": "hyperdjango"},
                            "spans": otlp_spans,
                        }
                    ],
                }
            ]
        }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            headers=self._headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                if self.verbose:
                    print(
                        f"[otlp-sink] exported {len(spans)} spans → "
                        f"{resp.status} {resp.reason}",
                        file=sys.stderr,
                    )
        except urllib.error.URLError as exc:
            # Collector unreachable — log and move on (best-effort delivery).
            # In production, you'd add retry/backoff or a dead-letter queue.
            if self.verbose:
                print(
                    f"[otlp-sink] export failed ({len(spans)} spans): {exc}",
                    file=sys.stderr,
                )
        except Exception as exc:
            if self.verbose:
                print(
                    f"[otlp-sink] unexpected error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    def flush(self) -> None:
        """No buffering — each export_spans call POSTs immediately."""
        return

    def close(self) -> None:
        """No persistent connections to clean up (urllib is per-request)."""
        return
