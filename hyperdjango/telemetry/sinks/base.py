"""
TelemetrySink — the plug-in adapter Protocol for exporting metrics + spans.

All built-in sinks (Prometheus, Stdout, InMemory) and any user-
provided adapter (custom HTTP exporter, OTLP-compatible backend,
etc.) implement this Protocol.

Design principles:

  1. **Protocol, not base class.** `@runtime_checkable` so users
     can implement the interface without inheriting from anything.
     `TelemetryMiddleware(sinks=[my_sink])` works as long as the
     sink has the four methods.

  2. **Background-thread delivery.** `export_metrics` and
     `export_spans` are called from a daemon drain thread (Phase 4),
     NEVER from the request path. Sink implementations MUST NOT
     block for long — if you need to batch or retry, do it
     asynchronously inside the sink.

  3. **Best-effort delivery.** If a sink raises during export, the
     drain loop logs and moves on. Sinks must not throw on shutdown
     after `close()` was called.

  4. **No protobuf.** Span batches are `list[dict]` in OpenTelemetry
     JSON schema shape. Metric scrapes are Prometheus text bytes.
     Adapters that need protobuf encode locally using their own
     SDK — not our problem.

Minimal reference implementation:

    @dataclass(slots=True)
    class MyStdoutSink:
        _metrics: bytes = b""

        def export_metrics(self, prometheus_text: bytes) -> None:
            self._metrics = prometheus_text
            print(prometheus_text.decode())

        def export_spans(self, spans: list[dict]) -> None:
            for span in spans:
                print(json.dumps(span))

        def flush(self) -> None:
            sys.stdout.flush()

        def close(self) -> None:
            pass

    app.use(TelemetryMiddleware(sinks=[MyStdoutSink()]))
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TelemetrySink(Protocol):
    """Plug-in export destination for metric scrapes + span batches."""

    def export_metrics(self, prometheus_text: bytes) -> None:
        """Called on every metric scrape interval with the latest
        Prometheus-formatted exposition text. Pull-based sinks
        (PrometheusSink) cache this for later HTTP serving;
        push-based sinks (StdoutSink, OTLP adapters) forward it
        immediately to the upstream collector.

        Must NOT raise on shutdown. Must NOT block for long.
        """
        ...

    def export_spans(self, spans: list[dict]) -> None:
        """Called on every span drain interval with a batch of
        completed spans in OpenTelemetry JSON schema shape:

            {
                "trace_id":  "...",    # 32-char hex
                "span_id":   "...",    # 16-char hex
                "parent_id": "..."|"", # 16-char hex or empty (root)
                "name":      "GET /api/books",
                "start_time_unix_nano": int,
                "end_time_unix_nano":   int,
                "attributes":           dict[str, str|int|float],
                "status":  {"code": 0|1|2, "message": "..."},
            }

        Empty batches (no spans drained) are skipped — this callback
        is never invoked with `spans=[]`.
        """
        ...

    def flush(self) -> None:
        """Called on `@app.on_shutdown` to force immediate export of
        any buffered data. Must complete in bounded time (sinks
        should use a timeout on network operations)."""
        ...

    def close(self) -> None:
        """Called after `flush()` at shutdown. Release connections,
        file handles, etc. After close(), no further method calls
        are made on this sink."""
        ...
