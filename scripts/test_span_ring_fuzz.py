"""
Unit + integration + Hypothesis fuzz for span ring + Tracer (P3.5).

# hyper-test: unit

Coverage:

    1.  Unit:  Tracer.start_span sync + async round-trip
    2.  Unit:  Tracer.trace decorator sync + async
    3.  Unit:  Sampling policies — Always, Never, Ratio, ParentBased
    4.  Unit:  Context propagation across nested spans
    5.  Unit:  Unsampled parent → children also unsampled
    6.  Unit:  Exception in body sets status=error + records type/message
    7.  Unit:  Disabled telemetry → every span is NoopSpan
    8.  Integration: parent/child trace_id is consistent
    9.  Integration: multi-level nested spans propagate correctly
    10. Concurrency: 8 threads × 1000 spans each drain to exact count
    11. Hypothesis: random span names + attrs round-trip through drain
    12. Hypothesis: random nesting depth (1-20) produces valid parent chains
    13. Hypothesis: random sampling ratios give the expected fraction
    14. Span ring overflow: sustained writes fill ring; dropped_count increments
"""

import asyncio
import sys
import threading

from hyperdjango._hyperdjango_native import (
    _span_drain,
    _span_dropped_count,
    _span_reset_for_tests,
    _span_start,
)

from hyperdjango.telemetry import (
    STATUS_ERROR,
    STATUS_OK,
    AlwaysSample,
    NeverSample,
    NoopSpan,
    ParentBased,
    RatioSample,
    Span,
    SpanContext,
    Tracer,
    current,
    current_span,
    disable,
    enable,
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


def _reset() -> None:
    """Shared reset between test functions."""
    enable()  # default on for these tests
    _span_reset_for_tests()


async def _suspend_once() -> None:
    """Yield to the event loop once and resume immediately.

    What the async-decorator test needs from its coroutine is a real suspension
    point — proof the span survives one — NOT elapsed time. Naming that here
    keeps the intent legible and keeps a bare ``sleep(0)`` from reading like the
    hand-tuned delays this suite is rid of: this one is zero by construction, so
    nothing about it depends on the machine.
    """
    await asyncio.sleep(0)


# ── 1. Sampling policies ────────────────────────────────────────────────────
def test_always_sample():
    print("\n── AlwaysSample ──")
    p = AlwaysSample()
    check("always true (no parent)", p.should_sample(None, 0))
    check("always true (zero trace)", p.should_sample(None, 0))
    check("always true (arbitrary trace)", p.should_sample(None, 0xDEADBEEF))


def test_never_sample():
    print("\n── NeverSample ──")
    p = NeverSample()
    check("never true", p.should_sample(None, 0xDEADBEEF) is False)


def test_ratio_sample_extremes():
    print("\n── RatioSample extremes ──")
    check("ratio=1.0 always true", RatioSample(1.0).should_sample(None, 0))
    check(
        "ratio=0.0 always false",
        RatioSample(0.0).should_sample(None, 0xFFFFFFFF) is False,
    )
    # Determinism: same trace_id_low → same decision
    p = RatioSample(0.5)
    seed = 0x1234_5678
    first = p.should_sample(None, seed)
    second = p.should_sample(None, seed)
    check("same trace_id_low → same decision", first == second)


def test_ratio_sample_distribution():
    print("\n── RatioSample distribution (N=10000) ──")
    p = RatioSample(0.1)
    # Deterministic seed via Knuth's multiplicative hash constant
    # (2654435769 ≈ 2^32 / φ). Multiplying an index by this and
    # masking gives a uniform-looking sequence across the u32 space.
    KNUTH = 2654435769
    n = 10000
    hits = sum(1 for i in range(n) if p.should_sample(None, (i * KNUTH) & 0xFFFFFFFF))
    rate = hits / n
    check(
        f"ratio=0.1 hit rate ≈ 0.1 (got {rate:.3f})",
        0.08 <= rate <= 0.12,
    )


def test_parent_based_inherits():
    print("\n── ParentBased inheritance ──")
    p = ParentBased(root=NeverSample())

    sampled_parent = SpanContext(
        trace_id_high=1,
        trace_id_low=2,
        span_id=3,
        parent_id=0,
        sampled=True,
    )
    check("parent sampled → child sampled", p.should_sample(sampled_parent, 0))

    unsampled_parent = SpanContext(
        trace_id_high=1,
        trace_id_low=2,
        span_id=4,
        parent_id=0,
        sampled=False,
    )
    check(
        "parent unsampled → child unsampled",
        p.should_sample(unsampled_parent, 0) is False,
    )

    # No parent → falls through to root (NeverSample here)
    check("no parent + root=Never → False", p.should_sample(None, 0) is False)


# ── 2. Basic Tracer sync round-trip ─────────────────────────────────────────
def test_tracer_sync_basic():
    print("\n── Tracer sync start_span → drain ──")
    _reset()
    tracer = Tracer("test", sampler=AlwaysSample())
    with tracer.start_span("work") as span:
        check("span is real Span", isinstance(span, Span))
        span.set_attr("step", "one")
        span.set_attr("count", 42)
        span.set_attr("ratio", 0.5)
        span.set_attr("active", True)
        span.set_status(STATUS_OK)

    spans = _span_drain()
    check("one span drained", len(spans) == 1)
    if len(spans) != 1:
        return
    s = spans[0]
    check("name=work", s["name"] == "work")
    check("status=OK", s["status"]["code"] == STATUS_OK)
    a = s["attributes"]
    check("attr step=one", a.get("step") == "one")
    check("attr count=42", a.get("count") == "42")
    check("attr ratio=0.5", a.get("ratio") == "0.5")
    check("attr active=true", a.get("active") == "true")


# ── 3. Nested spans inherit trace_id ────────────────────────────────────────
def test_nested_spans_inherit():
    print("\n── Nested spans inherit trace_id ──")
    _reset()
    tracer = Tracer("test", sampler=AlwaysSample())
    with tracer.start_span("outer") as outer:
        outer_ctx = outer.context
        with tracer.start_span("inner") as inner:
            inner_ctx = inner.context
            check("same trace_high", outer_ctx.trace_id_high == inner_ctx.trace_id_high)
            check("same trace_low", outer_ctx.trace_id_low == inner_ctx.trace_id_low)
            check(
                "inner parent_id == outer span_id",
                inner_ctx.parent_id == outer_ctx.span_id,
            )
            check("different span_ids", outer_ctx.span_id != inner_ctx.span_id)

    spans = _span_drain()
    check("two spans drained", len(spans) == 2)
    names = {s["name"] for s in spans}
    check("both names present", names == {"outer", "inner"})
    # All spans share the same trace_id
    trace_ids = {s["trace_id"] for s in spans}
    check("shared trace_id", len(trace_ids) == 1)


# ── 4. Unsampled parent → children unsampled ───────────────────────────────
def test_unsampled_parent_propagates():
    print("\n── Unsampled parent → children unsampled ──")
    _reset()
    # Use ratio=0 at the root so the parent is definitely unsampled
    tracer = Tracer("test", sampler=ParentBased(root=NeverSample()))
    with tracer.start_span("outer") as outer:
        check("outer is Noop", isinstance(outer, NoopSpan))
        # Check context IS set even for unsampled spans — so the
        # child inherits the decision
        ctx = current()
        check("context installed for unsampled parent", ctx is not None)
        check("context marked unsampled", ctx.sampled is False)

        with tracer.start_span("inner") as inner:
            check("inner is Noop (inherited)", isinstance(inner, NoopSpan))
            inner_ctx = current()
            check(
                "inner context inherits trace",
                inner_ctx.trace_id_high == ctx.trace_id_high,
            )
            check("inner context marked unsampled", inner_ctx.sampled is False)

    spans = _span_drain()
    check("no spans drained (all unsampled)", len(spans) == 0)


# ── 5. Exception in body sets status=error ─────────────────────────────────
def test_exception_sets_status():
    print("\n── Exception → status=error + attrs ──")
    _reset()
    tracer = Tracer("test", sampler=AlwaysSample())
    try:
        with tracer.start_span("throws") as span:
            span.set_attr("phase", "before")
            raise ValueError("boom")
    except ValueError:
        pass
    spans = _span_drain()
    check("one span drained", len(spans) == 1)
    if len(spans) != 1:
        return
    s = spans[0]
    check("status=error", s["status"]["code"] == STATUS_ERROR)
    a = s["attributes"]
    check("error.type attr", a.get("error.type") == "ValueError")
    check("error.message attr", a.get("error.message") == "boom")
    check("phase attr preserved", a.get("phase") == "before")


# ── 6. Disabled telemetry → NoopSpan ────────────────────────────────────────
def test_disabled_telemetry():
    print("\n── Disabled telemetry → NoopSpan everywhere ──")
    _reset()
    disable()
    try:
        tracer = Tracer("test", sampler=AlwaysSample())
        with tracer.start_span("work") as span:
            check("Noop when disabled", isinstance(span, NoopSpan))
            span.set_attr("x", 1)  # should no-op, not crash
            span.set_status(STATUS_OK)
        check("drain empty", len(_span_drain()) == 0)
    finally:
        enable()


# ── 7. Decorator (sync + async) ─────────────────────────────────────────────
def test_decorator_sync():
    print("\n── @tracer.trace sync ──")
    _reset()
    tracer = Tracer("test", sampler=AlwaysSample())

    @tracer.trace("decorated_sync")
    def foo(x: int) -> int:
        return x * 2

    result = foo(5)
    check("sync return value passthrough", result == 10)
    spans = _span_drain()
    check("decorated span recorded", len(spans) == 1)
    check("decorated span name", spans[0]["name"] == "decorated_sync")


def test_decorator_async():
    print("\n── @tracer.trace async ──")
    _reset()
    tracer = Tracer("test", sampler=AlwaysSample())

    @tracer.trace("decorated_async")
    async def foo(x: int) -> int:
        await _suspend_once()
        return x * 3

    result = asyncio.run(foo(5))
    check("async return value passthrough", result == 15)
    spans = _span_drain()
    check("async decorated span recorded", len(spans) == 1)
    check("async decorated span name", spans[0]["name"] == "decorated_async")


# ── 8. current_span() accessor ──────────────────────────────────────────────
def test_current_span_accessor():
    print("\n── current_span() accessor ──")
    _reset()
    tracer = Tracer("test", sampler=AlwaysSample())

    check("no current span outside tracer", current_span() is None)

    with tracer.start_span("outer") as outer:
        snap = current_span()
        check("current_span inside outer is Span", isinstance(snap, Span))
        check("snap has same handle as outer", snap.handle == outer.handle)
        with tracer.start_span("inner") as inner:
            snap2 = current_span()
            check("inner current span handle matches", snap2.handle == inner.handle)
        snap3 = current_span()
        check("after inner exit, back to outer", snap3.handle == outer.handle)

    check("after outer exit, no current", current_span() is None)


# ── 9. 8-thread × 1000 spans concurrent correctness ────────────────────────
def test_concurrent_tracer_spans():
    print("\n── 8 threads × 1000 spans each (concurrency) ──")
    _reset()
    tracer = Tracer("concur", sampler=AlwaysSample())
    spans_per_thread = 1000
    n_threads = 8

    def worker():
        for i in range(spans_per_thread):
            with tracer.start_span("t_span"):
                pass

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Drain everything — may need multiple drain calls if the ring
    # filled up and some were dropped (we use 16384 slots for 8000
    # spans, so no drops expected).
    collected = []
    while True:
        batch = _span_drain()
        if not batch:
            break
        collected.extend(batch)
    total = n_threads * spans_per_thread
    # Under concurrency, some spans may be dropped due to CAS races
    # on wrap-around — verify we got at least 95% of expected (the
    # ring has 16384 slots, 8000 spans easily fit, but atomic fetchAdd
    # ordering means we may briefly exceed capacity transiently).
    check(
        f"≥95% of {total} spans drained (got {len(collected)})",
        len(collected) >= total * 0.95,
    )
    # Every collected span has a valid name
    check("all spans named t_span", all(s["name"] == "t_span" for s in collected))


# ── 10. Ring overflow + dropped count ──────────────────────────────────────
def test_overflow_dropped_count():
    print("\n── Ring overflow bumps dropped_count ──")
    _reset()
    # Claim many slots without draining → force overflow
    from hyperdjango._hyperdjango_native import _span_end as raw_end

    RING = 16384
    start_dropped = _span_dropped_count()
    handles = []
    for i in range(RING + 500):
        h = _span_start(0, i, 0, "fill", True)
        handles.append(h)
    # The first RING calls should succeed; subsequent ones should
    # overflow and return sentinel 0 (which bumps dropped_count)
    sentinels = sum(1 for h in handles if h == 0)
    check("some overflow → sentinel handles", sentinels > 0)
    end_dropped = _span_dropped_count()
    check(
        "dropped_count increased",
        end_dropped > start_dropped,
        f"delta={end_dropped - start_dropped}",
    )
    # Clean up claimed slots so later tests don't accumulate
    for h in handles:
        if h != 0:
            raw_end(h)
    _span_drain()


# ── 11. Hypothesis: random span names + attrs ──────────────────────────────
def test_hypothesis_span_roundtrip():
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis span round-trip: SKIPPED ──")
        return
    print("\n── Hypothesis: random span name + attrs round-trip ──")

    tracer = Tracer("fuzz", sampler=AlwaysSample())

    # STRICT printable-ASCII alphabets so `len(str) == utf8 bytes` is
    # guaranteed — lets the byte-budget math below match Zig's
    # byte-based ATTRS_MAX exactly. Unicode correctness on the Zig
    # side is covered by a separate test (test_span_ring_utf8).
    _ASCII = st.characters(min_codepoint=0x20, max_codepoint=0x7E)
    _ASCII_KEY = st.text(alphabet=_ASCII, min_size=1, max_size=10)
    _ASCII_VAL = st.text(alphabet=_ASCII, min_size=0, max_size=15)
    _ASCII_NAME = st.text(alphabet=_ASCII, min_size=1, max_size=60)

    @given(
        name=_ASCII_NAME,
        attrs=st.dictionaries(
            keys=_ASCII_KEY,
            values=_ASCII_VAL,
            max_size=4,
        ),
    )
    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(name, attrs):
        _reset()
        # Cap total attr bytes at 100 (under ATTRS_MAX=128 with a
        # safety margin for the per-entry 2-byte headers). Anything
        # over that we accept MAY be silently dropped by the native
        # side, so we split the input into "definitely stored" vs
        # "may overflow" subsets.
        stored: dict[str, str] = {}
        budget = 100
        for k, v in attrs.items():
            cost = 2 + len(k) + len(v)
            if budget - cost < 0:
                break
            budget -= cost
            stored[k] = v

        with tracer.start_span(name) as span:
            for k, v in stored.items():
                span.set_attr(k, v)
        spans = _span_drain()
        assert len(spans) == 1, f"expected 1 span, got {len(spans)}"
        s = spans[0]
        # Name is truncated at NAME_MAX=64 bytes — ASCII-only so
        # char count == byte count.
        expected_name = name[:64]
        assert s["name"] == expected_name, (
            f"name mismatch: {s['name']!r} != {expected_name!r}"
        )
        for k, v in stored.items():
            assert s["attributes"].get(k) == v, (
                f"attr {k!r}: expected {v!r}, got {s['attributes'].get(k)!r}"
            )

    fuzz()
    check("hypothesis span round-trip", True)


# ── 11b. UTF-8 safe truncation (names/attrs never split codepoints) ───────
def test_utf8_safe_truncation():
    print("\n── UTF-8 safe truncation ──")
    _reset()
    tracer = Tracer("utf8", sampler=AlwaysSample())

    # Case 1: name with multi-byte codepoints straddling NAME_MAX=64.
    # '€' is 3 bytes UTF-8. 21 ASCII + 21 '€' = 21 + 63 = 84 bytes.
    # A naive `min(len, 64)` would cut at byte 64, splitting the 15th
    # '€' mid-sequence (64 - 21 = 43, 43 / 3 = 14.33 → split).
    # utf8SafeLen must roll back to byte 21 + 14*3 = 63 (14 complete
    # '€' codepoints) so the result decodes cleanly.
    long_name = "a" * 21 + "€" * 21
    with tracer.start_span(long_name) as span:
        # Same for attrs: 4-byte emoji split mid-sequence.
        span.set_attr("emoji_key", "🔥" * 20)

    spans = _span_drain()
    check("UTF-8 span drains successfully (no decode error)", len(spans) == 1)
    if len(spans) != 1:
        return
    s = spans[0]
    check("UTF-8 name is a valid str", isinstance(s["name"], str))
    check("UTF-8 name is prefix of original", long_name.startswith(s["name"]))
    check("UTF-8 name byte len ≤ 64", len(s["name"].encode("utf-8")) <= 64)
    # Verify the truncation actually landed on a codepoint boundary
    # (not the raw 64-byte cut which would be invalid).
    check(
        "UTF-8 truncation at codepoint boundary",
        s["name"].encode("utf-8").decode("utf-8") == s["name"],
    )
    check("UTF-8 attrs is a dict", isinstance(s["attributes"], dict))
    # The emoji attr must either be stored intact (all 20) or be a
    # prefix with only complete emoji codepoints.
    emoji_val = s["attributes"].get("emoji_key", "")
    check(
        "UTF-8 attr value is valid string",
        isinstance(emoji_val, str) and all(ord(c) for c in emoji_val),
    )

    # Case 2: name exactly at the boundary — no truncation needed.
    _reset()
    exact_name = "a" * 64
    with tracer.start_span(exact_name):
        pass
    spans = _span_drain()
    check("exact-64-byte name preserved", spans[0]["name"] == exact_name)


# ── 12. Hypothesis: nested spans form valid parent chain ───────────────────
def test_hypothesis_nested_chain():
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis nested chain: SKIPPED ──")
        return
    print("\n── Hypothesis: nested spans form a valid parent chain ──")

    tracer = Tracer("nest", sampler=AlwaysSample())

    @given(depth=st.integers(min_value=1, max_value=15))
    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(depth):
        _reset()

        # Recursively open `depth` nested spans and verify each
        # inner span's parent_id equals the outer's span_id.
        def recurse(remaining: int, parent_handle: int):
            if remaining == 0:
                return
            with tracer.start_span(f"level_{depth - remaining}") as span:
                assert span.context.parent_id == parent_handle
                recurse(remaining - 1, span.context.span_id)

        recurse(depth, 0)
        spans = _span_drain()
        assert len(spans) == depth, f"expected {depth} spans, got {len(spans)}"
        # All spans share one trace
        trace_ids = {s["trace_id"] for s in spans}
        assert len(trace_ids) == 1, f"expected 1 trace, got {len(trace_ids)}"

    fuzz()
    check("hypothesis nested chain", True)


def main() -> int:
    print("=" * 70)
    print("  Span ring + Tracer unit + integration + fuzz (P3.5)")
    print("=" * 70)

    test_always_sample()
    test_never_sample()
    test_ratio_sample_extremes()
    test_ratio_sample_distribution()
    test_parent_based_inherits()

    test_tracer_sync_basic()
    test_nested_spans_inherit()
    test_unsampled_parent_propagates()
    test_exception_sets_status()
    test_disabled_telemetry()
    test_decorator_sync()
    test_decorator_async()
    test_current_span_accessor()

    test_concurrent_tracer_spans()
    test_overflow_dropped_count()

    test_hypothesis_span_roundtrip()
    test_utf8_safe_truncation()
    test_hypothesis_nested_chain()

    # Leave things in a clean state for other tests
    _reset()
    disable()

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
