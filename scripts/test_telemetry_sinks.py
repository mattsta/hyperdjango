"""
Unit tests for StdoutSink + InMemorySink (P4.2 + P4.3).

# hyper-test: unit

Coverage:

    StdoutSink
      1. export_spans emits one JSON line per span to the stream
      2. empty span batch → no write
      3. span_prefix prepends per line
      4. export_metrics emits fenced block on include_metrics=True
      5. export_metrics no-op when include_metrics=False
      6. flush() calls stream.flush()
      7. close() is a no-op but safe
      8. stream.flush failure is swallowed (post-shutdown)

    InMemorySink
      9.  export_spans buffers into `spans`
      10. empty batch is a no-op
      11. max_spans ring eviction increments overflow_count
      12. export_metrics buffers latest + history
      13. latest_metrics returns most recent
      14. flush() increments flush_count
      15. close() makes subsequent exports no-op
      16. clear() wipes buffers and counters
      17. Thread safety: 8 threads × 500 exports each → 4000 spans total
      18. Read-property snapshots are decoupled from the internal deque

    Protocol conformance
      19. runtime_checkable TelemetrySink on both classes

    Hypothesis property
      20. arbitrary batches of spans → FIFO order + overflow invariant
"""

import io
import sys
import threading

from hyperdjango.telemetry.sinks import (
    InMemorySink,
    StdoutSink,
    TelemetrySink,
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


# ── Shared span fixture ────────────────────────────────────────────────────


def _make_span(
    name: str,
    span_id_hex: str = "b7ad6b7169203331",
    trace_id_hex: str = "0af7651916cd43dd8448eb211c80319c",
) -> dict:
    return {
        "trace_id": trace_id_hex,
        "span_id": span_id_hex,
        "parent_id": "",
        "name": name,
        "start_time_unix_nano": 1_000_000_000,
        "end_time_unix_nano": 1_000_000_500,
        "attributes": {"user_id": 42},
        "status": {"code": 1, "message": ""},
    }


# ── StdoutSink tests ───────────────────────────────────────────────────────


def test_stdout_export_spans_basic() -> None:
    print("\n── StdoutSink: export_spans basic ──")
    buf = io.StringIO()
    sink = StdoutSink(stream=buf)
    sink.export_spans([_make_span("work"), _make_span("child", "aaaa111122223333")])
    out = buf.getvalue()
    check("two lines emitted", out.count("\n") == 2)
    check("first span name present", '"name":"work"' in out)
    check("second span name present", '"name":"child"' in out)


def test_stdout_empty_batch_noop() -> None:
    print("\n── StdoutSink: empty batch no-op ──")
    buf = io.StringIO()
    sink = StdoutSink(stream=buf)
    sink.export_spans([])
    check("no output on empty batch", buf.getvalue() == "")


def test_stdout_span_prefix() -> None:
    print("\n── StdoutSink: span_prefix prepend ──")
    buf = io.StringIO()
    sink = StdoutSink(stream=buf, span_prefix="SPAN ")
    sink.export_spans([_make_span("work")])
    out = buf.getvalue()
    check("prefix before JSON", out.startswith("SPAN "))
    check("JSON still valid after prefix", '"name":"work"' in out)


def test_stdout_export_metrics_fenced() -> None:
    print("\n── StdoutSink: export_metrics fenced block ──")
    buf = io.StringIO()
    sink = StdoutSink(stream=buf)
    sink.export_metrics(b"hyper_requests_total 42\n")
    out = buf.getvalue()
    check("BEGIN marker", "# HYPER_METRICS_BEGIN" in out)
    check("END marker", "# HYPER_METRICS_END" in out)
    check("body preserved", "hyper_requests_total 42" in out)


def test_stdout_include_metrics_false() -> None:
    print("\n── StdoutSink: include_metrics=False ──")
    buf = io.StringIO()
    sink = StdoutSink(stream=buf, include_metrics=False)
    sink.export_metrics(b"hyper_requests_total 42\n")
    check("no metrics output when disabled", buf.getvalue() == "")


def test_stdout_empty_metrics_noop() -> None:
    print("\n── StdoutSink: empty metrics batch no-op ──")
    buf = io.StringIO()
    sink = StdoutSink(stream=buf)
    sink.export_metrics(b"")
    check("no output on empty metrics", buf.getvalue() == "")


def test_stdout_flush_calls_underlying() -> None:
    print("\n── StdoutSink: flush() calls stream.flush ──")

    class CountingStream:
        def __init__(self) -> None:
            self.writes = 0
            self.flushes = 0

        def write(self, data: str) -> None:
            self.writes += 1

        def flush(self) -> None:
            self.flushes += 1

    stream = CountingStream()
    sink = StdoutSink(stream=stream)
    sink.flush()
    check("flush reached stream", stream.flushes == 1)


def test_stdout_flush_swallows_errors() -> None:
    print("\n── StdoutSink: flush() swallows post-shutdown errors ──")

    class BrokenStream:
        def write(self, data: str) -> None:
            pass

        def flush(self) -> None:
            raise ValueError("stream closed")

    sink = StdoutSink(stream=BrokenStream())
    try:
        sink.flush()
        check("flush does not raise", True)
    except Exception as exc:
        check("flush does not raise", False, str(exc))


def test_stdout_close_noop() -> None:
    print("\n── StdoutSink: close() is no-op ──")
    sink = StdoutSink(stream=io.StringIO())
    sink.close()  # Must not raise
    check("close does not raise", True)


# ── InMemorySink tests ─────────────────────────────────────────────────────


def test_memory_export_spans_buffers() -> None:
    print("\n── InMemorySink: export_spans buffers ──")
    sink = InMemorySink()
    sink.export_spans([_make_span("a"), _make_span("b")])
    check("spans buffered", len(sink.spans) == 2)
    check("first span name", sink.spans[0]["name"] == "a")
    check("second span name", sink.spans[1]["name"] == "b")


def test_memory_empty_batch_noop() -> None:
    print("\n── InMemorySink: empty batch no-op ──")
    sink = InMemorySink()
    sink.export_spans([])
    check("buffer empty after empty batch", len(sink.spans) == 0)
    check("overflow_count unchanged", sink.overflow_count == 0)


def test_memory_ring_eviction() -> None:
    print("\n── InMemorySink: FIFO ring eviction ──")
    sink = InMemorySink(max_spans=3)
    sink.export_spans([_make_span(f"s{i}") for i in range(5)])
    check("buffer capped at max_spans", len(sink.spans) == 3)
    check("oldest evicted (s0 gone)", sink.spans[0]["name"] == "s2")
    check("newest preserved (s4)", sink.spans[-1]["name"] == "s4")
    check("overflow_count incremented", sink.overflow_count == 2)


def test_memory_metric_history() -> None:
    print("\n── InMemorySink: metric scrape history ──")
    sink = InMemorySink(max_metric_scrapes=3)
    for i in range(5):
        sink.export_metrics(f"scrape_{i}".encode())
    check("latest is scrape_4", sink.latest_metrics == b"scrape_4")
    scrapes = sink.metric_scrapes
    check("history capped at max_metric_scrapes", len(scrapes) == 3)
    check("oldest retained is scrape_2", scrapes[0] == b"scrape_2")


def test_memory_latest_metrics_empty() -> None:
    print("\n── InMemorySink: latest_metrics on empty history ──")
    sink = InMemorySink()
    check("empty history returns b''", sink.latest_metrics == b"")


def test_memory_flush_counts() -> None:
    print("\n── InMemorySink: flush_count ──")
    sink = InMemorySink()
    sink.flush()
    sink.flush()
    check("flush_count increments", sink.flush_count == 2)


def test_memory_close_suppresses_exports() -> None:
    print("\n── InMemorySink: close() suppresses exports ──")
    sink = InMemorySink()
    sink.export_spans([_make_span("before")])
    sink.close()
    sink.export_spans([_make_span("after")])
    sink.export_metrics(b"after_metrics")
    check("span after close() dropped", len(sink.spans) == 1)
    check("metric after close() dropped", sink.latest_metrics == b"")
    check("closed flag set", sink.closed is True)


def test_memory_clear() -> None:
    print("\n── InMemorySink: clear() wipes state ──")
    sink = InMemorySink()
    sink.export_spans([_make_span("x")])
    sink.export_metrics(b"m")
    sink.flush()
    sink.clear()
    check("spans wiped", len(sink.spans) == 0)
    check("metrics wiped", len(sink.metric_scrapes) == 0)
    check("flush_count reset", sink.flush_count == 0)
    check("overflow_count reset", sink.overflow_count == 0)


def test_memory_read_snapshot_independence() -> None:
    print("\n── InMemorySink: read snapshot is independent ──")
    sink = InMemorySink()
    sink.export_spans([_make_span("a")])
    snap = sink.spans
    snap.append({"mutated": True})
    check("mutating snapshot does not affect sink", len(sink.spans) == 1)


def test_memory_thread_safety() -> None:
    print("\n── InMemorySink: thread safety under 8×500 exports ──")
    sink = InMemorySink(max_spans=10_000)
    N_THREADS = 8
    PER_THREAD = 500

    def worker(tid: int) -> None:
        for i in range(PER_THREAD):
            sink.export_spans([_make_span(f"t{tid}_i{i}")])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(
        f"all {N_THREADS * PER_THREAD} spans buffered",
        len(sink.spans) == N_THREADS * PER_THREAD,
    )
    check("no overflow at this size", sink.overflow_count == 0)


# ── Protocol conformance ───────────────────────────────────────────────────


def test_hypothesis_memory_fifo_invariant() -> None:
    """Hypothesis: arbitrary batch sequences preserve FIFO order and
    overflow_count == max(0, total_in - max_spans).

    Verifies the ring buffer's core invariant under random batch
    sizes — catches off-by-one errors in the eviction logic that
    targeted unit tests could miss on specific sizes.
    """
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis FIFO invariant: SKIPPED ──")
        return
    print("\n── Hypothesis: InMemorySink FIFO + overflow invariant ──")

    @given(
        max_spans=st.integers(min_value=1, max_value=20),
        batches=st.lists(
            st.integers(min_value=0, max_value=15),
            min_size=1,
            max_size=10,
        ),
    )
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(max_spans: int, batches: list[int]) -> None:
        sink = InMemorySink(max_spans=max_spans)
        counter = 0
        for batch_size in batches:
            batch = [_make_span(f"s{counter + i}") for i in range(batch_size)]
            counter += batch_size
            sink.export_spans(batch)

        # Invariant 1: buffer length == min(total_in, max_spans)
        expected_len = min(counter, max_spans)
        assert len(sink.spans) == expected_len, (
            f"len={len(sink.spans)} expected={expected_len} "
            f"max_spans={max_spans} total_in={counter}"
        )

        # Invariant 2: overflow_count == max(0, total_in - max_spans)
        expected_overflow = max(0, counter - max_spans)
        assert sink.overflow_count == expected_overflow, (
            f"overflow_count={sink.overflow_count} expected={expected_overflow}"
        )

        # Invariant 3: FIFO ordering — the retained tail is the LAST
        # `expected_len` spans of the counter sequence
        first_retained = counter - expected_len
        expected_names = [f"s{first_retained + i}" for i in range(expected_len)]
        actual_names = [s["name"] for s in sink.spans]
        assert actual_names == expected_names, (
            f"FIFO order broken:\n  expected: {expected_names}\n  actual:   {actual_names}"
        )

    fuzz()
    check("hypothesis InMemorySink FIFO invariant", True)


def test_protocol_conformance() -> None:
    print("\n── TelemetrySink Protocol conformance ──")
    check(
        "StdoutSink is TelemetrySink",
        isinstance(StdoutSink(stream=io.StringIO()), TelemetrySink),
    )
    check("InMemorySink is TelemetrySink", isinstance(InMemorySink(), TelemetrySink))


def main() -> int:
    print("=" * 70)
    print("  StdoutSink + InMemorySink unit tests (P4.2 + P4.3)")
    print("=" * 70)

    # StdoutSink
    test_stdout_export_spans_basic()
    test_stdout_empty_batch_noop()
    test_stdout_span_prefix()
    test_stdout_export_metrics_fenced()
    test_stdout_include_metrics_false()
    test_stdout_empty_metrics_noop()
    test_stdout_flush_calls_underlying()
    test_stdout_flush_swallows_errors()
    test_stdout_close_noop()

    # InMemorySink
    test_memory_export_spans_buffers()
    test_memory_empty_batch_noop()
    test_memory_ring_eviction()
    test_memory_metric_history()
    test_memory_latest_metrics_empty()
    test_memory_flush_counts()
    test_memory_close_suppresses_exports()
    test_memory_clear()
    test_memory_read_snapshot_independence()
    test_memory_thread_safety()
    test_hypothesis_memory_fifo_invariant()

    # Protocol
    test_protocol_conformance()

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
