"""
InMemorySink — in-process buffer for tests + assertions.

The canonical test sink. Captures every metric scrape and every span
drain into in-memory lists so integration tests can assert on exactly
what the telemetry system produced end-to-end without spinning up an
external collector.

Usage in a test:

    from hyperdjango.telemetry.sinks.memory import InMemorySink
    from hyperdjango.telemetry import Tracer, enable

    sink = InMemorySink()
    enable()
    tracer = Tracer("t")
    with tracer.start_span("work") as span:
        span.set_attr("user_id", 42)
    # Manually trigger the drain the middleware would normally drive:
    from hyperdjango._hyperdjango_native import _span_drain
    sink.export_spans(_span_drain())

    assert len(sink.spans) == 1
    assert sink.spans[0]["name"] == "work"
    assert sink.spans[0]["attributes"]["user_id"] == 42

The sink is thread-safe: both `export_metrics` and `export_spans`
append under a `threading.Lock`, so concurrent writes from the drain
thread and the test assertion thread never race. Readers
(`sink.spans`, `sink.latest_metrics`) take a snapshot copy under the
lock — the returned list is the caller's to mutate.

Bounded by `max_spans` (default 10,000). Older spans are discarded
FIFO when the buffer fills so a runaway test doesn't OOM. The
`overflow_count` field reports how many spans were dropped.
"""

import threading
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class InMemorySink:
    """Thread-safe in-process buffer for spans and metric scrapes.

    Params:
      max_spans:    FIFO ring bound. When exceeded, the oldest spans
                    are evicted and `overflow_count` increments.
                    Default 10,000 — plenty for tests, small enough
                    to catch runaways.
      max_metric_scrapes: How many historical Prometheus scrapes to
                          retain. Default 64. The most recent is
                          always accessible via `latest_metrics`.
    """

    max_spans: int = 10_000
    max_metric_scrapes: int = 64

    _spans: deque = field(init=False, repr=False)
    _metric_scrapes: deque = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)
    overflow_count: int = field(default=0, init=False)
    flush_count: int = field(default=0, init=False)
    closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._spans = deque(maxlen=self.max_spans)
        self._metric_scrapes = deque(maxlen=self.max_metric_scrapes)
        self._lock = threading.Lock()

    # ── TelemetrySink protocol ──────────────────────────────────────────────

    def export_metrics(self, prometheus_text: bytes) -> None:
        """Buffer the latest scrape. Discards oldest scrape when the
        ring wraps."""
        if self.closed:
            return
        with self._lock:
            self._metric_scrapes.append(prometheus_text)

    def export_spans(self, spans: list[dict]) -> None:
        """Append a batch of spans to the FIFO buffer."""
        if self.closed:
            return
        if not spans:
            return
        with self._lock:
            for span in spans:
                if len(self._spans) == self.max_spans:
                    self.overflow_count += 1
                self._spans.append(span)

    def flush(self) -> None:
        """Increment flush_count so tests can assert shutdown hooks
        ran. No data is moved — this sink is already in-memory."""
        if self.closed:
            return
        with self._lock:
            self.flush_count += 1

    def close(self) -> None:
        """Mark the sink closed. Subsequent exports are silent no-ops.
        Buffers remain readable so post-shutdown assertions still
        work."""
        self.closed = True

    # ── Read API (for tests) ────────────────────────────────────────────────

    @property
    def spans(self) -> list[dict]:
        """Snapshot of all buffered spans in arrival order."""
        with self._lock:
            return list(self._spans)

    @property
    def latest_metrics(self) -> bytes:
        """Most recent Prometheus scrape text, or b"" if none."""
        with self._lock:
            if not self._metric_scrapes:
                return b""
            return self._metric_scrapes[-1]

    @property
    def metric_scrapes(self) -> list[bytes]:
        """All buffered Prometheus scrapes in arrival order."""
        with self._lock:
            return list(self._metric_scrapes)

    def clear(self) -> None:
        """Drop all buffered data. Useful between test cases."""
        with self._lock:
            self._spans.clear()
            self._metric_scrapes.clear()
            self.overflow_count = 0
            self.flush_count = 0
