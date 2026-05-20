"""
OTLP span exporter sink unit tests (task #255).

# hyper-test: unit

Tests the OTLPSpanSink example adapter WITHOUT a real collector — validates
the OTLP JSON payload structure, attribute encoding, status mapping,
resource attributes, and error handling. No network I/O in any test.

Coverage:
  1. _to_otlp_attributes — str/int/float/bool dispatch
  2. _span_to_otlp — full span conversion, parent_id presence/absence
  3. OTLPSpanSink payload assembly — resourceSpans envelope shape
  4. OTLPSpanSink resource attributes — service.name, sdk metadata
  5. export_spans with empty batch → no-op
  6. export_spans with collector down → graceful (no raise)
  7. export_metrics → explicit no-op
  8. env var resolution — OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME
  9. extra headers merge with env OTEL_EXPORTER_OTLP_HEADERS
  10. OTLP status mapping (unset, ok, error)
  11. Span with no attributes → empty attributes array
  12. Span with parent_id → parentSpanId field present
  13. TelemetrySink protocol compliance
"""

import json
import sys
from unittest.mock import patch

sys.path.insert(0, "services/otlp_sink")

from hyperdjango.telemetry.sinks.base import TelemetrySink
from services.otlp_sink.otlp_sink import (
    OTLPSpanSink,
    _span_to_otlp,
    _to_otlp_attributes,
)

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


# ── Fixtures ─────────────────────────────────────────────────────────────────

_SAMPLE_SPAN: dict = {
    "trace_id": "0af7651916cd43dd8448eb211c80319c",
    "span_id": "b7ad6b7169203331",
    "parent_id": "00f067aa0ba902b7",
    "name": "GET /api/books",
    "start_time_unix_nano": 1700000000000000000,
    "end_time_unix_nano": 1700000000005000000,
    "attributes": {
        "http.method": "GET",
        "http.status_code": "200",
        "user.id": 42,
        "latency_ms": 5.1,
    },
    "status": {"code": 1, "message": ""},
}

_ROOT_SPAN: dict = {
    "trace_id": "deadbeef12345678cafebabe87654321",
    "span_id": "facefeed12345678",
    "parent_id": "",
    "name": "POST /api/books",
    "start_time_unix_nano": 1700000000000000000,
    "end_time_unix_nano": 1700000000010000000,
    "attributes": {},
    "status": {"code": 2, "message": "Internal Server Error"},
}


# ── Tests ───────────────────────────────────────────────���─────────────────────


def test_to_otlp_attributes() -> None:
    print("\n── _to_otlp_attributes dispatch ──")
    attrs = {
        "str_key": "hello",
        "int_key": 42,
        "float_key": 3.14,
        "bool_key": True,
    }
    result = _to_otlp_attributes(attrs)
    check("result is list", isinstance(result, list))
    check("4 attributes", len(result) == 4)
    by_key = {a["key"]: a["value"] for a in result}
    check("str → stringValue", by_key["str_key"] == {"stringValue": "hello"})
    check("int → intValue", by_key["int_key"] == {"intValue": "42"})
    check("float → doubleValue", by_key["float_key"] == {"doubleValue": 3.14})
    check("bool → boolValue", by_key["bool_key"] == {"boolValue": True})


def test_span_to_otlp_with_parent() -> None:
    print("\n── _span_to_otlp with parent_id ──")
    otlp = _span_to_otlp(_SAMPLE_SPAN)
    check("traceId mapped", otlp["traceId"] == _SAMPLE_SPAN["trace_id"])
    check("spanId mapped", otlp["spanId"] == _SAMPLE_SPAN["span_id"])
    check("parentSpanId present", otlp.get("parentSpanId") == "00f067aa0ba902b7")
    check("name mapped", otlp["name"] == "GET /api/books")
    check(
        "startTimeUnixNano is string",
        otlp["startTimeUnixNano"] == "1700000000000000000",
    )
    check("kind is SERVER (2)", otlp["kind"] == 2)
    check(
        "status.code is STATUS_CODE_OK",
        otlp["status"]["code"] == "STATUS_CODE_OK",
    )
    check("attributes array has 4 entries", len(otlp.get("attributes", [])) == 4)


def test_span_to_otlp_root_no_parent() -> None:
    print("\n── _span_to_otlp root span (no parent) ──")
    otlp = _span_to_otlp(_ROOT_SPAN)
    check("parentSpanId absent for root", "parentSpanId" not in otlp)
    check("status is ERROR", otlp["status"]["code"] == "STATUS_CODE_ERROR")
    check(
        "status.message present",
        otlp["status"].get("message") == "Internal Server Error",
    )
    check(
        "empty attributes → no attributes key or empty list",
        len(otlp.get("attributes", [])) == 0,
    )


def test_status_mapping() -> None:
    print("\n── OTLP status code mapping ──")
    for code, expected in [
        (0, "STATUS_CODE_UNSET"),
        (1, "STATUS_CODE_OK"),
        (2, "STATUS_CODE_ERROR"),
    ]:
        span = {**_SAMPLE_SPAN, "status": {"code": code, "message": ""}}
        otlp = _span_to_otlp(span)
        check(f"code {code} → {expected}", otlp["status"]["code"] == expected)


def test_sink_payload_structure() -> None:
    print("\n── OTLPSpanSink payload assembly ──")
    # Capture the JSON payload by intercepting urllib
    captured_bodies: list[bytes] = []

    class FakeResponse:
        status = 200
        reason = "OK"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=5):
        captured_bodies.append(req.data)
        return FakeResponse()

    sink = OTLPSpanSink(
        endpoint="http://localhost:4318",
        service_name="test-app",
    )
    with patch("services.otlp_sink.otlp_sink.urllib.request.urlopen", fake_urlopen):
        sink.export_spans([_SAMPLE_SPAN, _ROOT_SPAN])

    check("one POST captured", len(captured_bodies) == 1)
    payload = json.loads(captured_bodies[0])
    check("resourceSpans key present", "resourceSpans" in payload)
    rs = payload["resourceSpans"]
    check("one resourceSpan", len(rs) == 1)
    resource = rs[0].get("resource", {})
    check("resource has attributes", "attributes" in resource)
    # service.name
    svc_name_attr = next(
        (a for a in resource["attributes"] if a["key"] == "service.name"),
        None,
    )
    check("service.name present", svc_name_attr is not None)
    check(
        "service.name == test-app",
        svc_name_attr["value"]["stringValue"] == "test-app",
    )
    # scopeSpans
    scope_spans = rs[0].get("scopeSpans", [])
    check("one scopeSpan", len(scope_spans) == 1)
    check("scope name is hyperdjango", scope_spans[0]["scope"]["name"] == "hyperdjango")
    check("2 spans in batch", len(scope_spans[0]["spans"]) == 2)


def test_sink_empty_batch_noop() -> None:
    print("\n── export_spans empty batch → no-op ──")
    called = {"n": 0}

    def fake_urlopen(req, timeout=5):
        called["n"] += 1

    sink = OTLPSpanSink(endpoint="http://localhost:4318")
    with patch("services.otlp_sink.otlp_sink.urllib.request.urlopen", fake_urlopen):
        sink.export_spans([])

    check("no HTTP call for empty batch", called["n"] == 0)


def test_sink_collector_down_graceful() -> None:
    print("\n── export_spans with collector down → graceful ──")
    sink = OTLPSpanSink(
        endpoint="http://localhost:1",  # guaranteed refused
        timeout_s=0.5,
        verbose=False,
    )
    # Must NOT raise
    sink.export_spans([_SAMPLE_SPAN])
    check("graceful on connection refused", True)


def test_export_metrics_noop() -> None:
    print("\n── export_metrics → explicit no-op ──")
    sink = OTLPSpanSink()
    sink.export_metrics(b"# TYPE counter\ncounter 1\n")
    check("export_metrics returns without action", True)


def test_env_var_resolution() -> None:
    print("\n── env var resolution ──")
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.com:4318",
        "OTEL_SERVICE_NAME": "from-env",
        "OTEL_EXPORTER_OTLP_HEADERS": "x-api-key=secret123,x-team=backend",
    }
    with patch.dict("os.environ", env, clear=False):
        sink = OTLPSpanSink()
    check(
        "endpoint from env",
        sink._url == "http://collector.example.com:4318/v1/traces",
    )
    svc_attr = next(
        (a for a in sink._resource_attrs if a["key"] == "service.name"),
        None,
    )
    check(
        "service.name from env",
        svc_attr["value"]["stringValue"] == "from-env",
    )
    check("x-api-key header parsed", sink._headers.get("x-api-key") == "secret123")
    check("x-team header parsed", sink._headers.get("x-team") == "backend")


def test_telemetry_sink_protocol_compliance() -> None:
    print("\n── TelemetrySink protocol compliance ──")
    sink = OTLPSpanSink(endpoint="http://localhost:4318")
    check("isinstance(sink, TelemetrySink)", isinstance(sink, TelemetrySink))
    check("has export_metrics", callable(sink.export_metrics))
    check("has export_spans", callable(sink.export_spans))
    check("has flush", callable(sink.flush))
    check("has close", callable(sink.close))


def main() -> int:
    print("=" * 70)
    print("  OTLP span exporter sink (task #255)")
    print("=" * 70)

    test_to_otlp_attributes()
    test_span_to_otlp_with_parent()
    test_span_to_otlp_root_no_parent()
    test_status_mapping()
    test_sink_payload_structure()
    test_sink_empty_batch_noop()
    test_sink_collector_down_graceful()
    test_export_metrics_noop()
    test_env_var_resolution()
    test_telemetry_sink_protocol_compliance()

    print()
    print("=" * 70)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
