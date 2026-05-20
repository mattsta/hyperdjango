"""
Unit tests for hyperdjango.telemetry.metrics — the Python facade
over the native metric registry (Phase 2 / P2.5).

# hyper-test: unit

Tests:
    1.  enable/disable toggle behavior
    2.  Counter.inc() delegates to native FFI
    3.  Counter.inc() is a no-op when disabled (zero-cost check)
    4.  Gauge set/inc/dec
    5.  Histogram observe + bucket correctness
    6.  CounterVec with dict + tuple label input
    7.  HistogramVec with dict + tuple label input
    8.  collect_prometheus_text() round-trip
    9.  PrometheusSink handler returns cached bytes
    10. PrometheusSink falls back to fresh scrape when cache empty
    11. TelemetrySink Protocol runtime_checkable works
"""

import asyncio
import sys

from hyperdjango.telemetry import (
    Counter,
    CounterVec,
    Gauge,
    Histogram,
    HistogramVec,
    PrometheusSink,
    TelemetrySink,
    disable,
    enable,
    is_enabled,
)
from hyperdjango.telemetry.metrics import collect_prometheus_text

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


# ── 1. enable/disable toggle ────────────────────────────────────────────────
def test_enable_disable():
    print("\n── enable/disable ──")
    disable()
    check("disabled on call", is_enabled() is False)
    enable()
    check("enabled after enable()", is_enabled() is True)
    disable()
    check("disabled again", is_enabled() is False)


# ── 2-3. Counter basic + disabled no-op ─────────────────────────────────────
def test_counter_basic_and_disabled():
    print("\n── Counter basics + disabled no-op ──")
    c = Counter("tf1_counter", "test counter for facade")

    # When disabled, inc is a no-op
    disable()
    c.inc()
    c.inc(5)
    check("counter noop when disabled", c.value() == 0, f"got {c.value()}")

    # When enabled, inc delegates to native
    enable()
    c.inc()
    c.inc(5)
    check("counter inc after enable", c.value() == 6, f"got {c.value()}")

    # Disable again — value stays, future ops no-op
    disable()
    c.inc(100)
    check("counter noop after re-disable", c.value() == 6, f"got {c.value()}")


# ── 4. Gauge ─────────────────────────────────────────────────────────────────
def test_gauge_basic():
    print("\n── Gauge ──")
    enable()
    g = Gauge("tf2_gauge", "test gauge")
    g.set(10)
    g.inc()
    g.inc(4)
    g.dec(5)
    check("gauge 10+1+4-5 = 10", g.value() == 10, f"got {g.value()}")


# ── 5. Histogram ────────────────────────────────────────────────────────────
def test_histogram_basic():
    print("\n── Histogram ──")
    enable()
    h = Histogram("tf3_hist", "test hist", buckets=(0.01, 0.1, 1.0))
    h.observe(0.005)  # bucket 0
    h.observe(0.05)  # bucket 1
    h.observe(0.5)  # bucket 2
    h.observe(5.0)  # +Inf

    text = collect_prometheus_text().decode("utf-8")
    check("hist has 4 observations in count", "tf3_hist_count 4" in text)
    check("hist bucket le=0.01 cumulative 1", 'tf3_hist_bucket{le="0.01"} 1' in text)
    check("hist bucket le=0.1 cumulative 2", 'tf3_hist_bucket{le="0.1"} 2' in text)
    check("hist bucket le=1 cumulative 3", 'tf3_hist_bucket{le="1"} 3' in text)
    check("hist bucket le=+Inf cumulative 4", 'tf3_hist_bucket{le="+Inf"} 4' in text)


# ── 6. CounterVec dict + tuple input ────────────────────────────────────────
def test_counter_vec_dict_and_tuple():
    print("\n── CounterVec dict + tuple ──")
    enable()
    cv = CounterVec(
        "tf4_http",
        "labeled http counter",
        label_names=["method", "status"],
    )
    cv.inc({"method": "GET", "status": "200"}, 3)
    cv.inc({"method": "POST", "status": "201"}, 1)
    cv.inc_tuple(("GET", "200"), 2)  # fast path

    text = collect_prometheus_text().decode("utf-8")
    check(
        "vec GET+200 merged = 5",
        'tf4_http{method="GET",status="200"} 5' in text,
    )
    check(
        "vec POST+201 = 1",
        'tf4_http{method="POST",status="201"} 1' in text,
    )

    # Label declared as tuple at init
    cv2 = CounterVec("tf4_vec2", "", label_names=("region", "ab_test"))
    cv2.inc({"region": "us-east", "ab_test": "control"})
    text = collect_prometheus_text().decode("utf-8")
    check(
        "tuple label_names works",
        'tf4_vec2{region="us-east",ab_test="control"} 1' in text,
    )


# ── 7. HistogramVec ─────────────────────────────────────────────────────────
def test_histogram_vec():
    print("\n── HistogramVec ──")
    enable()
    hv = HistogramVec(
        "tf5_latency",
        "labeled latency",
        label_names=["endpoint"],
        buckets=(0.01, 0.1, 1.0),
    )
    hv.observe({"endpoint": "/api/books"}, 0.05)
    hv.observe({"endpoint": "/api/books"}, 0.5)
    hv.observe({"endpoint": "/api/authors"}, 0.001)
    hv.observe_tuple(("/api/books",), 0.8)

    text = collect_prometheus_text().decode("utf-8")
    check("hvec /api/books present", "/api/books" in text)
    check("hvec /api/authors present", "/api/authors" in text)


# ── 8. collect_prometheus_text returns bytes ────────────────────────────────
def test_collect_returns_bytes():
    print("\n── collect_prometheus_text() ──")
    text = collect_prometheus_text()
    check("returns bytes", isinstance(text, bytes))
    check("contains TYPE lines", b"# TYPE" in text)
    check("non-empty", len(text) > 0)


# ── 9-10. PrometheusSink handler ────────────────────────────────────────────
def test_prometheus_sink_handler():
    print("\n── PrometheusSink handler ──")
    sink = PrometheusSink()
    check("sink implements TelemetrySink", isinstance(sink, TelemetrySink))

    # Empty cache — handler falls back to live scrape
    class _DummyRequest:
        pass

    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(sink.handler(_DummyRequest()))
    finally:
        loop.close()

    check("handler status 200", resp.status == 200)
    check("handler content-type", "text/plain" in resp.headers.get("content-type", ""))
    check("handler body non-empty", len(resp.body) > 0)

    # Populate cache via export_metrics → handler returns cached
    sink.export_metrics(b"# TYPE test_cached counter\ntest_cached 42\n")
    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(sink.handler(_DummyRequest()))
    finally:
        loop.close()
    check(
        "handler returns cached bytes",
        b"test_cached 42" in resp.body,
        f"got: {resp.body[:80]!r}",
    )

    # Export spans is a no-op (doesn't raise)
    try:
        sink.export_spans([{"name": "ignored"}])
        sink.flush()
        sink.close()
        noop_ok = True
    except Exception as e:
        noop_ok = False
        print(f"  unexpected: {e}")
    check("sink.export_spans/flush/close are no-ops", noop_ok)


# ── 11. TelemetrySink Protocol runtime_checkable ────────────────────────────
def test_protocol_runtime_check():
    print("\n── TelemetrySink Protocol ──")

    class MySink:
        def export_metrics(self, prometheus_text):
            pass

        def export_spans(self, spans):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class MissingMethods:
        def export_metrics(self, prometheus_text):
            pass

    check("custom sink matches Protocol", isinstance(MySink(), TelemetrySink))
    check(
        "incomplete sink rejected by Protocol",
        not isinstance(MissingMethods(), TelemetrySink),
    )


# ── 12. mount_gated_metrics helper ──────────────────────────────────────────
def test_mount_gated_metrics():
    print("\n── mount_gated_metrics ──")
    from hyperdjango.exceptions import HTTPException
    from hyperdjango.telemetry import mount_gated_metrics

    routes: dict = {}

    class _App:
        """Minimal stand-in exposing only the app.get(path)(fn) surface the
        helper uses — the helper references no application code."""

        def get(self, path):
            def deco(fn):
                routes[path] = fn
                return fn

            return deco

    class _Resp:
        def __init__(self, body: bytes):
            self.body = body

    async def handler(request):
        return _Resp(b"SCRAPE-BODY")

    # Authorized scrape: resolve succeeds → the handler body flows straight back.
    async def resolve_ok(request):
        return object()

    mount_gated_metrics(_App(), handler, resolve=resolve_ok, path="/metrics")
    authorized_route = routes["/metrics"]
    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(authorized_route(object()))
    finally:
        loop.close()
    check("authorized scrape returns the handler body", resp.body == b"SCRAPE-BODY")

    # Unauthorized scrape: resolve raises → on_deny runs once, the denial
    # re-raises, and the handler is never reached (fail closed).
    deny_calls: list = []
    handler_calls: list = []

    async def handler2(request):
        handler_calls.append(1)
        return _Resp(b"SHOULD-NOT-REACH")

    async def resolve_deny(request):
        raise HTTPException(401, "nope")

    def on_deny(request, exc):
        deny_calls.append(exc)

    mount_gated_metrics(_App(), handler2, resolve=resolve_deny, on_deny=on_deny)
    denied_route = routes["/metrics"]
    raised = False
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(denied_route(object()))
    except HTTPException:
        raised = True
    finally:
        loop.close()
    check("unauthorized scrape re-raises HTTPException", raised)
    check("on_deny invoked exactly once", len(deny_calls) == 1)
    check("handler never called on deny", handler_calls == [])

    # An async on_deny (audit row + counter) is awaited too.
    async_deny_calls: list = []

    async def async_on_deny(request, exc):
        async_deny_calls.append(exc)

    mount_gated_metrics(
        _App(), handler2, resolve=resolve_deny, on_deny=async_on_deny, path="/m2"
    )
    raised = False
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(routes["/m2"](object()))
    except HTTPException:
        raised = True
    finally:
        loop.close()
    check("async on_deny awaited exactly once", raised and len(async_deny_calls) == 1)


def main() -> int:
    print("=" * 70)
    print("  telemetry.metrics Python facade tests (P2.5)")
    print("=" * 70)

    test_enable_disable()
    test_counter_basic_and_disabled()
    test_gauge_basic()
    test_histogram_basic()
    test_counter_vec_dict_and_tuple()
    test_histogram_vec()
    test_collect_returns_bytes()
    test_prometheus_sink_handler()
    test_protocol_runtime_check()
    test_mount_gated_metrics()

    # Final: leave telemetry disabled so we don't affect other
    # tests that import this module
    disable()

    print("\n" + "=" * 70)
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
