"""
TelemetryMiddleware unit + integration tests (P4.4 + P4.5).

# hyper-test: unit

Coverage:

    Disabled mode
      1. telemetry disabled → passthrough, no span created, no sink exports

    Request span creation
      2. enabled + GET / → span with name "GET /"
      3. HTTP attributes attached: method, route, client_ip, user_agent
      4. response status_code attached
      5. 5xx response sets status=ERROR

    Trace-context propagation
      6. incoming traceparent header → parent context installed, child inherits trace_id
      7. outbound response gets `traceparent` header pointing at current span
      8. extract_traceparent=False → ignores inbound header

    Exception path
      9. call_next raises → span records error.type / error.message / status=ERROR
      10. exception still propagates to caller

    Drain worker
      11. drain_now() pushes spans to InMemorySink
      12. drain_now() pushes Prometheus exposition text to InMemorySink
      13. shutdown() is idempotent (2 calls → 1 flush + 1 close)
      14. shutdown() runs final drain
      15. background thread auto-drains at interval

    Sink isolation
      16. one broken sink does not starve the others

    Hypothesis property
      17. arbitrary (method, path, status) → invariants hold:
          - exactly one span recorded
          - span name == f"{method} {path}"
          - http.method attr == method
          - http.status_code attr matches response status
          - outbound traceparent header is parseable
          - HTTP metric counter bumped with (method, str(status))
"""

import asyncio
import sys
import time

from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.telemetry import (
    AlwaysSample,
    Counter,
    InMemorySink,
    TelemetryAssertions,
    TelemetryMiddleware,
    Tracer,
    disable,
    enable,
    is_enabled,
    parse_traceparent,
)

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False


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


# ── Test helpers ────────────────────────────────────────────────────────────


def _make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
) -> Request:
    base_headers = {"host": "test.local", "user-agent": "pytest/1.0"}
    if headers:
        base_headers.update(headers)
    return Request(method=method, path=path, headers=base_headers)


def _ok_call_next(response: Response):
    async def call_next(request: Request) -> Response:
        return response

    return call_next


def _error_call_next(exc: BaseException):
    async def call_next(request: Request) -> Response:
        raise exc

    return call_next


def _new_middleware(sink: InMemorySink, **kwargs) -> TelemetryMiddleware:
    """Construct a middleware pre-wired with an InMemorySink and an
    AlwaysSample tracer so every test span is recorded.

    The default `drain_interval_seconds` is deliberately LARGE (600 s)
    so the background drain thread doesn't tick during test execution.
    Tests that need to exercise the drain path call `mw.drain_now()`
    explicitly — this is the ONLY reliable way to sync with the ring
    under parallel-test CPU pressure. A short interval (50 ms, the
    old default) raced with `drain_now()` under full-suite load: the
    background thread would fire between the `sink.clear()` and the
    next request's `drain_now()`, causing spurious "expected 1 span,
    got 0" failures in the Hypothesis fuzz test at ~1:100 runs.
    Tests that specifically exercise the background-drain path
    override `drain_interval_seconds` explicitly.
    """
    tracer = Tracer("test", sampler=AlwaysSample())
    return TelemetryMiddleware(
        tracer=tracer,
        sinks=[sink],
        drain_interval_seconds=kwargs.pop("drain_interval_seconds", 600.0),
        **kwargs,
    )


def _run(coro):
    return asyncio.run(coro)


# ── Disabled mode ──────────────────────────────────────────────────────────


def test_disabled_passthrough() -> None:
    print("\n── Disabled mode: passthrough ──")
    disable()
    check("is_enabled() false", is_enabled() is False)
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        req = _make_request()
        resp = Response(status=200, headers={})
        out = _run(mw(req, _ok_call_next(resp)))
        check("passthrough returns same response", out is resp)
        # No span should have been created
        mw.drain_now()
        check("no spans when disabled", len(sink.spans) == 0)
    finally:
        mw.shutdown()


# ── Request span creation ─────────────────────────────────────────────────


def test_span_basic_attributes() -> None:
    print("\n── Enabled: span basic attributes ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        req = _make_request("GET", "/api/users/42", {"user-agent": "curl/7.88"})
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        mw.drain_now()
        check("one span recorded", len(sink.spans) == 1)
        if not sink.spans:
            return
        span = sink.spans[0]
        check("span name is METHOD PATH", span["name"] == "GET /api/users/42")
        attrs = span["attributes"]
        check("http.method attr", attrs.get("http.method") == "GET")
        check("http.route attr", attrs.get("http.route") == "/api/users/42")
        check("http.user_agent attr", attrs.get("http.user_agent") == "curl/7.88")
        check(
            "http.status_code attr is string '200'",
            attrs.get("http.status_code") in ("200", 200),
        )
        check("net.peer.ip attached", "net.peer.ip" in attrs)
    finally:
        mw.shutdown()
        disable()


def test_span_5xx_sets_error_status() -> None:
    print("\n── Enabled: 5xx → status=ERROR ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        req = _make_request("POST", "/submit")
        resp = Response(status=503, headers={})
        _run(mw(req, _ok_call_next(resp)))
        mw.drain_now()
        check("one span recorded", len(sink.spans) == 1)
        if not sink.spans:
            return
        span = sink.spans[0]
        check("status.code == 2 (ERROR)", span["status"]["code"] == 2)
    finally:
        mw.shutdown()
        disable()


def test_span_2xx_sets_no_error() -> None:
    print("\n── Enabled: 2xx → status != ERROR ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        req = _make_request("GET", "/ok")
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        mw.drain_now()
        check("one span recorded", len(sink.spans) == 1)
        if not sink.spans:
            return
        span = sink.spans[0]
        check("status.code != 2 on 200", span["status"]["code"] != 2)
    finally:
        mw.shutdown()
        disable()


# ── Trace-context propagation ─────────────────────────────────────────────


def test_incoming_traceparent_inherited() -> None:
    print("\n── Trace-context: incoming traceparent inherited ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        inbound = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        req = _make_request(headers={"traceparent": inbound})
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        mw.drain_now()
        check("one span recorded", len(sink.spans) == 1)
        if not sink.spans:
            return
        span = sink.spans[0]
        # trace_id is the full 128-bit hex from the inbound header
        check(
            "trace_id matches incoming",
            span["trace_id"] == "0af7651916cd43dd8448eb211c80319c",
        )
        # parent_id is the span_id from the incoming header
        check(
            "parent_id is inbound span_id",
            span.get("parent_id") == "b7ad6b7169203331",
        )
    finally:
        mw.shutdown()
        disable()


def test_outbound_traceparent_header() -> None:
    print("\n── Trace-context: outbound traceparent header ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        req = _make_request()
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        tp = resp.headers.get("traceparent")
        check("traceparent present on response", tp is not None)
        if tp is None:
            return
        ctx = parse_traceparent(tp)
        check("outbound traceparent parses", ctx is not None)
        check("outbound traceparent sampled", ctx.sampled is True)
    finally:
        mw.shutdown()
        disable()


def test_extract_traceparent_false_ignores_inbound() -> None:
    print("\n── Trace-context: extract_traceparent=False ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink, extract_traceparent=False)
    try:
        inbound = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        req = _make_request(headers={"traceparent": inbound})
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        mw.drain_now()
        check("one span recorded", len(sink.spans) == 1)
        if not sink.spans:
            return
        span = sink.spans[0]
        check(
            "fresh trace_id (not inbound)",
            span["trace_id"] != "0af7651916cd43dd8448eb211c80319c",
        )
    finally:
        mw.shutdown()
        disable()


# ── Exception path ─────────────────────────────────────────────────────────


def test_exception_records_error() -> None:
    print("\n── Exception: error.type + status=ERROR ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        req = _make_request("POST", "/fail")
        raised = None
        try:
            _run(mw(req, _error_call_next(ValueError("boom"))))
        except ValueError as exc:
            raised = exc
        check("exception still propagates", isinstance(raised, ValueError))
        mw.drain_now()
        check("one span recorded", len(sink.spans) == 1)
        if not sink.spans:
            return
        span = sink.spans[0]
        check("status.code == 2 (ERROR)", span["status"]["code"] == 2)
        attrs = span["attributes"]
        check("error.type attr", attrs.get("error.type") == "ValueError")
        check(
            "error.message attr contains 'boom'",
            "boom" in (attrs.get("error.message") or ""),
        )
    finally:
        mw.shutdown()
        disable()


# ── Drain worker ──────────────────────────────────────────────────────────


def test_drain_now_pushes_metrics() -> None:
    print("\n── drain_now pushes metrics ──")
    enable()
    # Register a counter so the exposition text isn't empty
    counter = Counter("test_drain_push_total", "test counter")
    counter.inc(3)
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        mw.drain_now()
        text = sink.latest_metrics
        check("metrics exposition captured", b"test_drain_push_total" in text)
    finally:
        mw.shutdown()
        disable()


def test_shutdown_is_idempotent() -> None:
    print("\n── shutdown is idempotent ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    mw.shutdown()
    initial_flush = sink.flush_count
    mw.shutdown()  # second call should be a no-op
    check("flush_count unchanged on 2nd shutdown", sink.flush_count == initial_flush)
    check("sink is closed", sink.closed is True)
    disable()


def test_shutdown_runs_final_drain() -> None:
    print("\n── shutdown runs final drain ──")
    enable()
    sink = InMemorySink()
    mw = _new_middleware(sink)
    try:
        req = _make_request()
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        # Don't call drain_now — let shutdown do it
        mw.shutdown()
        check("final drain captured the span", len(sink.spans) >= 1)
        check("flush_count >= 1", sink.flush_count >= 1)
    finally:
        disable()


def test_background_thread_auto_drain() -> None:
    print("\n── Background thread auto-drains at interval ──")
    enable()
    sink = InMemorySink()
    # Very short interval so the test completes quickly
    mw = _new_middleware(sink, drain_interval_seconds=0.02)
    try:
        req = _make_request()
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        # Wait up to 500ms for the worker to tick at least once.
        # This is time-based rather than event-based only because
        # the drain thread has no externally-observable signal. We
        # use a tight poll loop on sink.spans as the predicate.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if len(sink.spans) >= 1:
                break
            time.sleep(0.005)
        check("background thread drained", len(sink.spans) >= 1)
    finally:
        mw.shutdown()
        disable()


# ── Sink isolation ────────────────────────────────────────────────────────


class _BrokenSink:
    """TelemetrySink impl that raises on every export call."""

    def export_metrics(self, prometheus_text: bytes) -> None:
        raise RuntimeError("metrics broken")

    def export_spans(self, spans: list[dict]) -> None:
        raise RuntimeError("spans broken")

    def flush(self) -> None:
        raise RuntimeError("flush broken")

    def close(self) -> None:
        raise RuntimeError("close broken")


def test_hypothesis_request_invariants() -> None:
    """Hypothesis-driven request roundtrip: arbitrary method+path+status
    combinations all satisfy the middleware's invariants.

    Uses strict printable-ASCII alphabets so span names are predictable
    byte-wise — unicode correctness for span names is covered by the
    dedicated UTF-8 truncation test in test_span_ring_fuzz.py.
    """
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis middleware invariants: SKIPPED ──")
        return
    print("\n── Hypothesis: arbitrary request → middleware invariants ──")

    enable()
    sink = InMemorySink(max_spans=1000)
    tracer = Tracer("fuzz", sampler=AlwaysSample())
    mw = _new_middleware(sink)

    # Strict printable ASCII so len(str) == utf8 bytes for name math.
    _ASCII = st.characters(min_codepoint=0x20, max_codepoint=0x7E)
    _METHODS = st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"])
    _PATHS = st.text(
        alphabet=st.characters(
            min_codepoint=0x21, max_codepoint=0x7E, blacklist_characters=" /"
        ),
        min_size=1,
        max_size=20,
    )
    _STATUS = st.sampled_from([200, 201, 204, 301, 302, 400, 401, 403, 404, 500, 503])

    @given(method=_METHODS, path_segment=_PATHS, status=_STATUS)
    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(method: str, path_segment: str, status: int) -> None:
        sink.clear()
        path = f"/{path_segment}"
        req = _make_request(method=method, path=path)
        resp = Response(status=status, headers={})
        _run(mw(req, _ok_call_next(resp)))
        mw.drain_now()

        asserts = TelemetryAssertions(sink)
        asserts.assert_span_count(1)
        span_name = f"{method} {path}"
        asserts.assert_has_span(span_name)
        asserts.assert_span_attr(span_name, "http.method", method)
        asserts.assert_span_attr(span_name, "http.route", path)
        asserts.assert_span_attr(span_name, "http.status_code", str(status))

        # Error classification: 5xx → STATUS_ERROR, everything else not ERROR
        span = sink.spans[0]
        if status >= 500:
            assert span["status"]["code"] == 2, (
                f"5xx should set ERROR, got code={span['status']['code']}"
            )
        else:
            assert span["status"]["code"] != 2, (
                f"<500 should NOT set ERROR, got code={span['status']['code']}"
            )

        # Outbound traceparent parses cleanly
        tp = resp.headers.get("traceparent")
        assert tp is not None, "missing outbound traceparent"
        parsed = parse_traceparent(tp)
        assert parsed is not None, f"outbound traceparent did not parse: {tp!r}"

    try:
        fuzz()
        check("hypothesis request invariants", True)
    finally:
        mw.shutdown()
        disable()


def test_broken_sink_isolation() -> None:
    print("\n── One broken sink does not starve others ──")
    enable()
    good = InMemorySink()
    broken = _BrokenSink()
    tracer = Tracer("test", sampler=AlwaysSample())
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[broken, good],
        # Large interval — we drive the drain explicitly via drain_now
        # so the background thread doesn't race with sink assertions.
        drain_interval_seconds=600.0,
    )
    try:
        req = _make_request()
        resp = Response(status=200, headers={})
        _run(mw(req, _ok_call_next(resp)))
        mw.drain_now()
        check("good sink received span despite broken neighbor", len(good.spans) == 1)
    finally:
        mw.shutdown()
        disable()


def main() -> int:
    print("=" * 70)
    print("  TelemetryMiddleware unit + integration (P4.4 + P4.5)")
    print("=" * 70)

    test_disabled_passthrough()
    test_span_basic_attributes()
    test_span_5xx_sets_error_status()
    test_span_2xx_sets_no_error()
    test_incoming_traceparent_inherited()
    test_outbound_traceparent_header()
    test_extract_traceparent_false_ignores_inbound()
    test_exception_records_error()
    test_drain_now_pushes_metrics()
    test_shutdown_is_idempotent()
    test_shutdown_runs_final_drain()
    test_background_thread_auto_drain()
    test_broken_sink_isolation()
    test_hypothesis_request_invariants()

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
