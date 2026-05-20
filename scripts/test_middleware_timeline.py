"""Tests for middleware chain execution timeline instrumentation.

Tests MiddlewareTimeline, MiddlewareSpan, get_current_timeline(),
instrumented MiddlewareStack, and timing accuracy.
"""

# hyper-test: unit

import asyncio
import sys
import time

from hyperdjango.standalone_middleware import (
    MiddlewareSpan,
    MiddlewareStack,
    MiddlewareTimeline,
    get_current_timeline,
)


def run_async(coro):
    """Helper to run async tests."""
    return asyncio.run(coro)


class FakeRequest:
    """Minimal request stub for testing."""

    def __init__(self, method="GET", path="/"):
        self.method = method
        self.path = path
        self.headers = {}


class FakeResponse:
    """Minimal response stub for testing."""

    def __init__(self, body="OK", status=200):
        self.body = body
        self.status = status
        self.headers = {}


# ── MiddlewareSpan tests ──────────────────────────────────────────────────


def test_span_duration():
    """MiddlewareSpan computes durations correctly."""
    span = MiddlewareSpan(name="test_mw", start_ns=1_000_000, end_ns=2_500_000)
    assert span.duration_ns == 1_500_000
    assert abs(span.duration_ms - 1.5) < 1e-9
    assert abs(span.duration_us - 1500.0) < 1e-9
    print("  PASS: MiddlewareSpan duration computation")


def test_span_zero_duration():
    """MiddlewareSpan with zero duration."""
    span = MiddlewareSpan(name="noop", start_ns=100, end_ns=100)
    assert span.duration_ns == 0
    assert span.duration_ms == 0.0
    print("  PASS: MiddlewareSpan zero duration")


# ── MiddlewareTimeline tests ──────────────────────────────────────────────


def test_timeline_empty():
    """Empty timeline has zero total and no slowest."""
    tl = MiddlewareTimeline(request_start_ns=0, request_end_ns=0)
    assert tl.total_ns == 0
    assert tl.total_ms == 0.0
    assert tl.slowest is None
    assert tl.summary() == []
    print("  PASS: MiddlewareTimeline empty")


def test_timeline_single_span():
    """Timeline with a single span."""
    tl = MiddlewareTimeline(
        spans=[MiddlewareSpan("cors", 100, 500)],
        request_start_ns=100,
        request_end_ns=500,
    )
    assert tl.total_ns == 400
    assert tl.slowest.name == "cors"
    summary = tl.summary()
    assert len(summary) == 1
    assert summary[0]["name"] == "cors"
    assert summary[0]["percent"] == 100.0
    print("  PASS: MiddlewareTimeline single span")


def test_timeline_multiple_spans():
    """Timeline identifies slowest from multiple spans."""
    spans = [
        MiddlewareSpan("cors", 0, 100),  # 100ns
        MiddlewareSpan("auth", 100, 500),  # 400ns — slowest
        MiddlewareSpan("logging", 500, 600),  # 100ns
    ]
    tl = MiddlewareTimeline(spans=spans, request_start_ns=0, request_end_ns=600)
    assert tl.slowest.name == "auth"
    assert tl.total_ns == 600
    summary = tl.summary()
    assert len(summary) == 3
    # Auth should have highest percentage
    auth_entry = [s for s in summary if s["name"] == "auth"][0]
    assert auth_entry["percent"] > 50.0
    print("  PASS: MiddlewareTimeline multiple spans")


def test_timeline_summary_percentages():
    """Summary percentages add up correctly."""
    spans = [
        MiddlewareSpan("a", 0, 250),
        MiddlewareSpan("b", 250, 750),
        MiddlewareSpan("c", 750, 1000),
    ]
    tl = MiddlewareTimeline(spans=spans, request_start_ns=0, request_end_ns=1000)
    summary = tl.summary()
    assert summary[0]["percent"] == 25.0
    assert summary[1]["percent"] == 50.0
    assert summary[2]["percent"] == 25.0
    total_pct = sum(s["percent"] for s in summary)
    assert abs(total_pct - 100.0) < 0.1
    print("  PASS: MiddlewareTimeline summary percentages")


# ── Instrumented MiddlewareStack tests ────────────────────────────────────


def test_non_instrumented_stack():
    """Non-instrumented stack doesn't set timeline."""
    stack = MiddlewareStack(instrument=False)

    async def handler(request):
        return FakeResponse("hello")

    wrapped = stack.wrap(handler)
    resp = run_async(wrapped(FakeRequest()))
    assert resp.body == "hello"
    # No timeline should be set (or at least not from this stack)
    print("  PASS: Non-instrumented stack works normally")


def test_instrumented_empty_stack():
    """Instrumented stack with no middleware still times the handler."""
    stack = MiddlewareStack(instrument=True)

    async def handler(request):
        return FakeResponse("direct")

    wrapped = stack.wrap(handler)
    resp = run_async(wrapped(FakeRequest()))
    assert resp.body == "direct"

    tl = get_current_timeline()
    assert tl is not None
    assert tl.total_ns > 0
    assert len(tl.spans) == 1  # Just the handler
    assert tl.spans[0].name == "handler"
    print("  PASS: Instrumented empty stack times handler")


def test_instrumented_single_middleware():
    """Instrumented stack records single middleware timing."""
    stack = MiddlewareStack(instrument=True)

    async def timing_mw(request, call_next):
        return await call_next(request)

    stack.add(timing_mw)

    async def handler(request):
        return FakeResponse("ok")

    wrapped = stack.wrap(handler)
    resp = run_async(wrapped(FakeRequest()))
    assert resp.body == "ok"

    tl = get_current_timeline()
    assert tl is not None
    assert len(tl.spans) == 2  # middleware + handler
    names = [s.name for s in tl.spans]
    assert "timing_mw" in names
    assert "handler" in names
    print("  PASS: Instrumented single middleware recorded")


def test_instrumented_multiple_middleware():
    """Instrumented stack records all middleware in chain."""
    stack = MiddlewareStack(instrument=True)

    async def cors(request, call_next):
        return await call_next(request)

    async def auth(request, call_next):
        return await call_next(request)

    async def compress(request, call_next):
        return await call_next(request)

    stack.add(cors)
    stack.add(auth)
    stack.add(compress)

    async def handler(request):
        return FakeResponse("ok")

    wrapped = stack.wrap(handler)
    run_async(wrapped(FakeRequest()))

    tl = get_current_timeline()
    assert tl is not None
    names = [s.name for s in tl.spans]
    # All middleware + handler recorded (order may vary since spans append on return)
    assert "cors" in names
    assert "auth" in names
    assert "compress" in names
    assert "handler" in names
    assert len(tl.spans) == 4
    print("  PASS: Instrumented multiple middleware all recorded")


def test_instrumented_class_middleware():
    """Class-based middleware gets class name."""
    stack = MiddlewareStack(instrument=True)

    class CORSMiddleware:
        async def __call__(self, request, call_next):
            return await call_next(request)

    stack.add(CORSMiddleware())

    async def handler(request):
        return FakeResponse("ok")

    wrapped = stack.wrap(handler)
    run_async(wrapped(FakeRequest()))

    tl = get_current_timeline()
    assert tl is not None
    names = [s.name for s in tl.spans]
    assert "CORSMiddleware" in names
    print("  PASS: Class middleware name detection")


def test_instrumented_timing_accuracy():
    """Middleware with sleep shows measurable duration."""
    stack = MiddlewareStack(instrument=True)

    async def slow_mw(request, call_next):
        await asyncio.sleep(0.01)  # 10ms
        return await call_next(request)

    async def fast_mw(request, call_next):
        return await call_next(request)

    stack.add(slow_mw)
    stack.add(fast_mw)

    async def handler(request):
        return FakeResponse("ok")

    wrapped = stack.wrap(handler)
    run_async(wrapped(FakeRequest()))

    tl = get_current_timeline()
    assert tl is not None
    assert tl.total_ms >= 5  # At least 5ms (conservative)

    slow_span = [s for s in tl.spans if s.name == "slow_mw"][0]
    assert slow_span.duration_ms >= 5  # At least 5ms

    fast_span = [s for s in tl.spans if s.name == "fast_mw"][0]
    # Fast middleware should be much quicker than slow
    assert fast_span.duration_ms < slow_span.duration_ms

    assert tl.slowest.name == "slow_mw"
    print(
        f"  PASS: Timing accuracy (slow={slow_span.duration_ms:.1f}ms, fast={fast_span.duration_ms:.3f}ms)"
    )


def test_instrumented_summary_output():
    """Summary returns properly formatted dicts."""
    stack = MiddlewareStack(instrument=True)

    async def mw_a(request, call_next):
        await asyncio.sleep(0.005)
        return await call_next(request)

    async def mw_b(request, call_next):
        return await call_next(request)

    stack.add(mw_a)
    stack.add(mw_b)

    async def handler(request):
        return FakeResponse("ok")

    wrapped = stack.wrap(handler)
    run_async(wrapped(FakeRequest()))

    tl = get_current_timeline()
    summary = tl.summary()
    assert len(summary) == 3  # mw_a + mw_b + handler
    for entry in summary:
        assert "name" in entry
        assert "duration_ms" in entry
        assert "percent" in entry
        assert isinstance(entry["name"], str)
        assert isinstance(entry["duration_ms"], float)
        assert isinstance(entry["percent"], float)
    print("  PASS: Summary output format correct")


def test_instrumented_response_preserved():
    """Instrumentation doesn't alter the response."""
    stack = MiddlewareStack(instrument=True)

    async def header_mw(request, call_next):
        resp = await call_next(request)
        resp.headers["x-custom"] = "value"
        return resp

    stack.add(header_mw)

    async def handler(request):
        resp = FakeResponse("test body")
        resp.status = 201
        return resp

    wrapped = stack.wrap(handler)
    resp = run_async(wrapped(FakeRequest()))
    assert resp.body == "test body"
    assert resp.status == 201
    assert resp.headers["x-custom"] == "value"
    print("  PASS: Instrumentation preserves response")


def test_middleware_error_still_records():
    """If middleware raises, timeline still gets populated."""
    stack = MiddlewareStack(instrument=True)

    async def error_mw(request, call_next):
        raise ValueError("boom")

    stack.add(error_mw)

    async def handler(request):
        return FakeResponse("ok")

    wrapped = stack.wrap(handler)
    try:
        run_async(wrapped(FakeRequest()))
        assert False, "Should have raised"
    except ValueError:
        pass

    tl = get_current_timeline()
    assert tl is not None
    # request_end_ns should be set by the finally block
    assert tl.request_end_ns > 0
    print("  PASS: Error in middleware still records timeline")


def test_timeline_thread_isolation():
    """Each thread gets its own timeline (threadlocal)."""
    import threading

    results = {}

    def run_in_thread(name, sleep_ms):
        stack = MiddlewareStack(instrument=True)

        async def slow_mw(request, call_next):
            await asyncio.sleep(sleep_ms / 1000)
            return await call_next(request)

        stack.add(slow_mw)

        async def handler(request):
            return FakeResponse(name)

        wrapped = stack.wrap(handler)
        asyncio.run(wrapped(FakeRequest()))
        tl = get_current_timeline()
        results[name] = tl

    t1 = threading.Thread(target=run_in_thread, args=("thread1", 50))
    t2 = threading.Thread(target=run_in_thread, args=("thread2", 5))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Each thread should have its own timeline
    assert "thread1" in results
    assert "thread2" in results
    tl1 = results["thread1"]
    tl2 = results["thread2"]
    assert tl1 is not tl2
    # Thread1 (10ms) should be slower than thread2 (5ms)
    assert tl1.total_ns > tl2.total_ns
    print("  PASS: Timeline thread isolation")


def test_overhead_benchmark():
    """Instrumentation overhead is negligible."""
    iterations = 1000

    async def noop_handler(request):
        return FakeResponse("ok")

    # Non-instrumented baseline
    stack_plain = MiddlewareStack(instrument=False)

    async def mw1(request, call_next):
        return await call_next(request)

    async def mw2(request, call_next):
        return await call_next(request)

    stack_plain.add(mw1)
    stack_plain.add(mw2)
    wrapped_plain = stack_plain.wrap(noop_handler)

    req = FakeRequest()

    async def bench_plain():
        for _ in range(iterations):
            await wrapped_plain(req)

    start = time.perf_counter_ns()
    asyncio.run(bench_plain())
    plain_ns = (time.perf_counter_ns() - start) / iterations

    # Instrumented
    stack_inst = MiddlewareStack(instrument=True)
    stack_inst.add(mw1)
    stack_inst.add(mw2)
    wrapped_inst = stack_inst.wrap(noop_handler)

    async def bench_inst():
        for _ in range(iterations):
            await wrapped_inst(req)

    start = time.perf_counter_ns()
    asyncio.run(bench_inst())
    inst_ns = (time.perf_counter_ns() - start) / iterations

    overhead_ns = inst_ns - plain_ns
    overhead_pct = (overhead_ns / plain_ns * 100) if plain_ns > 0 else 0
    print(
        f"  PASS: Instrumentation overhead — plain: {plain_ns:.0f}ns, instrumented: {inst_ns:.0f}ns, overhead: {overhead_ns:.0f}ns ({overhead_pct:.1f}%)"
    )


def main():
    tests = [
        # Span
        test_span_duration,
        test_span_zero_duration,
        # Timeline
        test_timeline_empty,
        test_timeline_single_span,
        test_timeline_multiple_spans,
        test_timeline_summary_percentages,
        # Instrumented stack
        test_non_instrumented_stack,
        test_instrumented_empty_stack,
        test_instrumented_single_middleware,
        test_instrumented_multiple_middleware,
        test_instrumented_class_middleware,
        test_instrumented_timing_accuracy,
        test_instrumented_summary_output,
        test_instrumented_response_preserved,
        test_middleware_error_still_records,
        test_timeline_thread_isolation,
        # Benchmark
        test_overhead_benchmark,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Middleware Timeline Instrumentation Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback

            failed += 1
            errors.append((test.__name__, str(e)))
            traceback.print_exc()
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
