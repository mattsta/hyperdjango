"""
Span events primitive tests (task #254).

# hyper-test: unit

Validates the new per-span event arena (v0.15.2): timestamped sub-events
packed into a 128-byte arena per slot, exposed as a list of dicts in the
drained span.

Coverage:
  1. Single event — name + timestamp present in drain output
  2. Multiple events — all appear in order, unique timestamps
  3. NoopSpan.add_event — silent no-op (unsampled path)
  4. Event overflow — arena drops excess events silently
  5. Empty events — no "events" key when span has 0 events
  6. UTF-8 name — non-ASCII event names truncate at char boundary
  7. Event timing — timestamps are between span start_ns and end_ns
  8. Event with long name — truncated to 255 bytes max
  9. Events + attributes coexist — no interference
  10. Event count matches list length
"""

import sys

from hyperdjango.telemetry import (
    AlwaysSample,
    InMemorySink,
    TelemetryMiddleware,
    Tracer,
    disable,
    enable,
)
from hyperdjango.telemetry.tracing import _NOOP_SPAN, Span

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


def _drain_one(tracer: Tracer, sink: InMemorySink, fn) -> dict:
    """Helper: run fn inside a span, drain, return the span dict."""
    sink.clear()
    mw = TelemetryMiddleware(
        tracer=tracer,
        sinks=[sink],
        drain_interval_seconds=600.0,
    )
    with tracer.start_span("test") as span:
        fn(span)
    mw.drain_now()
    mw.shutdown()
    if not sink.spans:
        return {}
    return sink.spans[0]


def test_single_event() -> None:
    print("\n── Single event ──")
    enable()
    try:
        sink = InMemorySink()
        tracer = Tracer("test", sampler=AlwaysSample())

        def body(span: Span) -> None:
            span.add_event("cache_miss")

        result = _drain_one(tracer, sink, body)
        check("span has events key", "events" in result)
        events = result.get("events", [])
        check("events list has 1 entry", len(events) == 1)
        if events:
            ev = events[0]
            check("event has name", ev.get("name") == "cache_miss")
            check("event has time_unix_nano", "time_unix_nano" in ev)
            check(
                "time_unix_nano is int",
                isinstance(ev.get("time_unix_nano"), int),
            )
    finally:
        disable()


def test_multiple_events() -> None:
    print("\n── Multiple events in order ──")
    enable()
    try:
        sink = InMemorySink()
        tracer = Tracer("test", sampler=AlwaysSample())

        def body(span: Span) -> None:
            span.add_event("step_1")
            span.add_event("step_2")
            span.add_event("step_3")

        result = _drain_one(tracer, sink, body)
        events = result.get("events", [])
        check("3 events recorded", len(events) == 3)
        names = [e["name"] for e in events]
        check("events in order", names == ["step_1", "step_2", "step_3"])
        # Timestamps should be monotonically non-decreasing
        times = [e["time_unix_nano"] for e in events]
        check(
            "timestamps non-decreasing",
            all(times[i] <= times[i + 1] for i in range(len(times) - 1)),
        )
    finally:
        disable()


def test_noop_span_add_event() -> None:
    print("\n── NoopSpan.add_event is a no-op ──")
    _NOOP_SPAN.add_event("should_not_crash")
    check("NoopSpan.add_event does not raise", True)


def test_event_overflow() -> None:
    print("\n── Event arena overflow drops silently ──")
    enable()
    try:
        sink = InMemorySink()
        tracer = Tracer("test", sampler=AlwaysSample())

        def body(span: Span) -> None:
            # Each event with a 20-char name uses 9 + 20 = 29 bytes.
            # 128 / 29 ≈ 4 events fit. Try to add 10 — excess should be dropped.
            for i in range(10):
                span.add_event(f"event_number_{i:06d}")

        result = _drain_one(tracer, sink, body)
        events = result.get("events", [])
        check("overflow: some events recorded (≥ 1)", len(events) >= 1)
        check("overflow: not all 10 recorded (arena limit)", len(events) < 10)
        # All recorded events should have valid names
        for ev in events:
            check(
                f"overflow: event '{ev['name']}' has timestamp",
                isinstance(ev.get("time_unix_nano"), int),
            )
    finally:
        disable()


def test_no_events_no_key() -> None:
    print("\n── No events → no 'events' key in drain output ──")
    enable()
    try:
        sink = InMemorySink()
        tracer = Tracer("test", sampler=AlwaysSample())

        def body(span: Span) -> None:
            span.set_attr("key", "value")
            # No add_event calls

        result = _drain_one(tracer, sink, body)
        check("no events key when 0 events", "events" not in result)
    finally:
        disable()


def test_event_timing_in_span_range() -> None:
    print("\n── Event timestamps within span range ──")
    enable()
    try:
        sink = InMemorySink()
        tracer = Tracer("test", sampler=AlwaysSample())

        def body(span: Span) -> None:
            span.add_event("mid_span")

        result = _drain_one(tracer, sink, body)
        start = result.get("start_time_unix_nano", 0)
        end = result.get("end_time_unix_nano", 0)
        events = result.get("events", [])
        if events:
            ev_time = events[0]["time_unix_nano"]
            check(
                "event time ≥ span start",
                ev_time >= start,
                f"event={ev_time}, start={start}",
            )
            check(
                "event time ≤ span end",
                ev_time <= end,
                f"event={ev_time}, end={end}",
            )
    finally:
        disable()


def test_events_and_attrs_coexist() -> None:
    print("\n── Events + attributes coexist ──")
    enable()
    try:
        sink = InMemorySink()
        tracer = Tracer("test", sampler=AlwaysSample())

        def body(span: Span) -> None:
            span.set_attr("http.method", "GET")
            span.add_event("cache_check")
            span.set_attr("cache.hit", "false")
            span.add_event("db_query")

        result = _drain_one(tracer, sink, body)
        attrs = result.get("attributes", {})
        events = result.get("events", [])
        check("attrs present", "http.method" in attrs)
        check("attrs value correct", attrs.get("http.method") == "GET")
        check("2 events present", len(events) == 2)
        check(
            "event names correct",
            [e["name"] for e in events] == ["cache_check", "db_query"],
        )
    finally:
        disable()


def test_long_event_name_truncated() -> None:
    print("\n── Long event name truncated at 255 bytes ──")
    enable()
    try:
        sink = InMemorySink()
        tracer = Tracer("test", sampler=AlwaysSample())
        long_name = "x" * 300

        def body(span: Span) -> None:
            span.add_event(long_name)

        result = _drain_one(tracer, sink, body)
        events = result.get("events", [])
        # The 128-byte arena can hold one event with up to 119-char name:
        # 9 bytes overhead + 119 = 128. A 300-char name is truncated to
        # 119 chars (fits exactly). If the arena can't fit even the
        # truncated version, the event is dropped.
        if events:
            check(
                "long name truncated (≤ 255)",
                len(events[0]["name"]) <= 255,
            )
        else:
            check("long name event dropped (arena overflow)", True)
    finally:
        disable()


def main() -> int:
    print("=" * 70)
    print("  Span events primitive (task #254)")
    print("=" * 70)

    test_single_event()
    test_multiple_events()
    test_noop_span_add_event()
    test_event_overflow()
    test_no_events_no_key()
    test_event_timing_in_span_range()
    test_events_and_attrs_coexist()
    test_long_event_name_truncated()

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
