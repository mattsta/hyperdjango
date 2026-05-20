"""
End-to-end telemetry integration test (P4.8).

# hyper-test: unit

Builds a real `HyperApp` with `TelemetryMiddleware` wired in, makes
in-process HTTP requests via `TestClient`, and verifies the full
round-trip:

    request  → span created + attrs attached
             → W3C trace-context parsed + propagated
             → drain pushes spans to InMemorySink
             → Prometheus handler serves scraped metrics
             → shutdown hook stops the drain thread

This is the integration-level counterpart to:

    - `test_telemetry_metrics.py`   — unit: Python facade
    - `test_telemetry_sinks.py`     — unit: sink contract
    - `test_telemetry_middleware.py` — unit: middleware machinery
    - `test_telemetry_settings.py`   — unit: settings bootstrap
    - `test_span_ring_fuzz.py`       — fuzz: Zig span ring

Together they form the first complete telemetry test pyramid.
"""

import sys
from unittest.mock import patch

from hyperdjango import HyperApp, Response
from hyperdjango.conf import DEFAULTS
from hyperdjango.telemetry import (
    AlwaysSample,
    Counter,
    InMemorySink,
    PrometheusSink,
    TelemetryMiddleware,
    Tracer,
    configure_from_settings,
    current_span,
    disable,
    enable,
    parse_traceparent,
)
from hyperdjango.testing import TestClient

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


def _build_app(middleware: TelemetryMiddleware, prom_sink: PrometheusSink) -> HyperApp:
    """Assemble a minimal HyperApp with telemetry + 3 test routes."""
    app = HyperApp()
    app.use(middleware)
    app.on_shutdown(middleware.shutdown)

    @app.get("/hello")
    async def hello(request):
        return Response.json({"msg": "hello"})

    @app.get("/boom")
    async def boom(request):
        raise RuntimeError("intentional failure")

    @app.get("/child")
    async def child(request):
        # Read the active span via current_span and verify it's
        # populated for the handler as well (propagation check).
        span = current_span()
        return Response.json(
            {
                "has_span": span is not None,
                "span_id_nonzero": getattr(span, "handle", 0) != 0 if span else False,
            }
        )

    app.get("/metrics")(prom_sink.handler)
    return app


# ── E2E flow: one request → one span in the in-memory sink ─────────────────


def test_request_creates_span() -> None:
    print("\n── E2E: request creates span in InMemorySink ──")
    enable()
    memory_sink = InMemorySink()
    prom_sink = PrometheusSink()
    tracer = Tracer("e2e", sampler=AlwaysSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[memory_sink, prom_sink],
        drain_interval_seconds=600.0,
    )
    app = _build_app(mw, prom_sink)
    client = TestClient(app)
    try:
        resp = client.get("/hello")
        check("200 OK", resp.status == 200)
        # Force a drain so we don't depend on the thread ticking
        mw.drain_now()
        check("one span in InMemorySink", len(memory_sink.spans) == 1)
        span = memory_sink.spans[0]
        check("span name GET /hello", span["name"] == "GET /hello")
        check(
            "http.status_code == 200",
            span["attributes"].get("http.status_code") in ("200", 200),
        )
        # Propagated traceparent header on outbound response
        tp = resp.headers.get("traceparent")
        check("outbound traceparent present", tp is not None)
        if tp is not None:
            check("outbound traceparent parses", parse_traceparent(tp) is not None)
    finally:
        mw.shutdown()
        disable()


# ── E2E flow: inbound traceparent is inherited ─────────────────────────────


def test_inbound_traceparent_inherited() -> None:
    print("\n── E2E: inbound traceparent inherited ──")
    enable()
    memory_sink = InMemorySink()
    prom_sink = PrometheusSink()
    tracer = Tracer("e2e", sampler=AlwaysSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[memory_sink, prom_sink],
        drain_interval_seconds=600.0,
    )
    app = _build_app(mw, prom_sink)
    client = TestClient(app)
    try:
        inbound = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        resp = client.get("/hello", headers={"traceparent": inbound})
        mw.drain_now()
        check("one span", len(memory_sink.spans) == 1)
        if not memory_sink.spans:
            return
        span = memory_sink.spans[0]
        check(
            "span trace_id matches inbound",
            span["trace_id"] == "0af7651916cd43dd8448eb211c80319c",
        )
        check(
            "span parent_id is inbound span_id",
            span.get("parent_id") == "b7ad6b7169203331",
        )
        # Outbound header keeps same trace_id
        out_tp = resp.headers.get("traceparent")
        if out_tp is not None:
            out_ctx = parse_traceparent(out_tp)
            assert out_ctx is not None
            expected_high = 0x0AF7651916CD43DD
            expected_low = 0x8448EB211C80319C
            check(
                "outbound trace_id preserved",
                out_ctx.trace_id_high == expected_high
                and out_ctx.trace_id_low == expected_low,
            )
    finally:
        mw.shutdown()
        disable()


# ── E2E flow: exception in handler → ERROR status ─────────────────────────


def test_handler_exception_records_error() -> None:
    print("\n── E2E: handler exception records ERROR status ──")
    enable()
    memory_sink = InMemorySink()
    prom_sink = PrometheusSink()
    tracer = Tracer("e2e", sampler=AlwaysSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[memory_sink, prom_sink],
        drain_interval_seconds=600.0,
    )
    app = _build_app(mw, prom_sink)
    client = TestClient(app)
    try:
        resp = client.get("/boom")
        # HyperApp converts unhandled exceptions to 500 — the span
        # should record either ERROR via error.type (from middleware's
        # exception path) OR status=2 via the 500 response path.
        mw.drain_now()
        check("one span recorded", len(memory_sink.spans) == 1)
        if not memory_sink.spans:
            return
        span = memory_sink.spans[0]
        check("status.code == 2 (ERROR)", span["status"]["code"] == 2)
    finally:
        mw.shutdown()
        disable()


# ── E2E flow: Prometheus scrape handler serves cached exposition ──────────


def test_prometheus_scrape_handler() -> None:
    print("\n── E2E: /metrics handler serves exposition ──")
    enable()
    # Register a counter first so the exposition isn't empty
    counter = Counter("e2e_requests_total", "requests for e2e test")
    counter.inc(5)
    memory_sink = InMemorySink()
    prom_sink = PrometheusSink()
    tracer = Tracer("e2e", sampler=AlwaysSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[memory_sink, prom_sink],
        drain_interval_seconds=600.0,
    )
    app = _build_app(mw, prom_sink)
    client = TestClient(app)
    try:
        # Trigger at least one drain so the PrometheusSink cache is
        # populated — alternatively the handler falls back to
        # collect_prometheus_text() live, which is also fine.
        mw.drain_now()
        resp = client.get("/metrics")
        check("/metrics returns 200", resp.status == 200)
        body = resp.body if isinstance(resp.body, bytes) else resp.body.encode()
        check("exposition contains counter name", b"e2e_requests_total" in body)
        check(
            "content-type is prometheus",
            resp.headers.get("content-type", "").startswith("text/plain"),
        )
    finally:
        mw.shutdown()
        disable()


# ── E2E flow: current_span() accessor visible from handler ────────────────


def test_current_span_visible_in_handler() -> None:
    print("\n── E2E: current_span() visible in handler ──")
    enable()
    memory_sink = InMemorySink()
    prom_sink = PrometheusSink()
    tracer = Tracer("e2e", sampler=AlwaysSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[memory_sink, prom_sink],
        drain_interval_seconds=600.0,
    )
    app = _build_app(mw, prom_sink)
    client = TestClient(app)
    try:
        resp = client.get("/child")
        check("200 OK", resp.status == 200)
        data = resp.json()
        check("handler saw active span", data["has_span"] is True)
        check("active span has nonzero handle", data["span_id_nonzero"] is True)
    finally:
        mw.shutdown()
        disable()


# ── E2E flow: configure_from_settings with full pipeline ──────────────────


def test_http_metrics_emitted() -> None:
    print("\n── E2E: HTTP request metrics appear in Prometheus output ──")
    enable()
    memory_sink = InMemorySink()
    prom_sink = PrometheusSink()
    tracer = Tracer("e2e", sampler=AlwaysSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[memory_sink, prom_sink],
        drain_interval_seconds=600.0,
    )
    app = _build_app(mw, prom_sink)
    client = TestClient(app)
    try:
        for _ in range(3):
            resp = client.get("/hello")
            check("200", resp.status == 200)
        mw.drain_now()
        text = memory_sink.latest_metrics
        check(
            "hyperdjango_http_requests_total present",
            b"hyperdjango_http_requests_total" in text,
        )
        check(
            "GET method label present",
            b'method="GET"' in text,
        )
        check(
            "200 status label present",
            b'status="200"' in text,
        )
        check(
            "duration histogram present",
            b"hyperdjango_http_request_duration_seconds" in text,
        )
    finally:
        mw.shutdown()
        disable()


def test_configure_from_settings_full() -> None:
    print("\n── E2E: configure_from_settings builds the full pipeline ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SERVICE_NAME": "e2e_test",
        "TELEMETRY_SAMPLE_RATIO": 1.0,
        "TELEMETRY_DRAIN_INTERVAL": 600.0,
        "TELEMETRY_EXTRACT_TRACEPARENT": True,
        "TELEMETRY_SINKS": ["prometheus", "memory"],
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            app = HyperApp()
            bootstrap = configure_from_settings(app)
            assert bootstrap is not None
            check("bootstrap returned", bootstrap is not None)
            check("prometheus sink present", bootstrap.prometheus_sink is not None)
            check("memory sink present", bootstrap.memory_sink is not None)

            @app.get("/ping")
            async def ping(request):
                return Response.json({"ok": True})

            app.get("/metrics")(bootstrap.prometheus_sink.handler)

            client = TestClient(app)
            resp = client.get("/ping")
            check("/ping returns 200", resp.status == 200)

            bootstrap.middleware.drain_now()
            check(
                "memory sink captured ping span",
                any(s["name"] == "GET /ping" for s in bootstrap.memory_sink.spans),
            )

            metrics_resp = client.get("/metrics")
            check("/metrics returns 200", metrics_resp.status == 200)
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def main() -> int:
    print("=" * 70)
    print("  E2E telemetry integration (P4.8)")
    print("=" * 70)

    test_request_creates_span()
    test_inbound_traceparent_inherited()
    test_handler_exception_records_error()
    test_prometheus_scrape_handler()
    test_current_span_visible_in_handler()
    test_http_metrics_emitted()
    test_configure_from_settings_full()

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
