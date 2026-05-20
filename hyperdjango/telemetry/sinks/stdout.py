"""
StdoutSink — JSON-lines span + plaintext metric export to stdout.

The simplest possible sink: dump everything to stdout. Spans are
emitted one-per-line in JSON lines format so log forwarders and
aggregators can pick them up without extra parsing. Metric scrapes
are emitted as a fenced Prometheus text block so they round-trip
through standard log scraping tooling.

Use this sink for:

  * Development — see exactly what the span ring is drainingright
    before wiring up a real collector
  * Cloud deployments (Heroku, Fly, Render, Cloud Run) where the
    platform already captures stdout and forwards it to a logs stack
  * CI — capture spans for post-run inspection without needing a
    collector process

Background-thread safety: `sys.stdout.write` is thread-safe on
CPython (the stream's internal buffer takes a lock per write). We
build the full JSON-lines payload up front and emit it in ONE
`write` call so individual span lines never interleave with other
threads' stdout output.

Schema: spans are in OpenTelemetry JSON shape (see
`sinks/base.py::TelemetrySink.export_spans` for the field contract).
"""

import contextlib
import sys
from dataclasses import dataclass, field

from hyperdjango.native import fast_json_dumps


@dataclass(slots=True)
class StdoutSink:
    """JSON-lines span + Prometheus-text metric dump to stdout.

    Params:
      stream: file-like object to write to. Defaults to `sys.stdout`.
              Tests can pass an `io.StringIO()` to capture output.
      include_metrics: set False to suppress the metrics block (e.g.,
                       when pairing with a separate Prometheus sink
                       that owns metric export).
      span_prefix: optional string prepended to every span line. Useful
                   when piping into grep / jq. Default: ``""``.
    """

    stream: object = field(default=None)
    include_metrics: bool = True
    span_prefix: str = ""

    def __post_init__(self) -> None:
        if self.stream is None:
            self.stream = sys.stdout

    # ── TelemetrySink protocol ──────────────────────────────────────────────

    def export_metrics(self, prometheus_text: bytes) -> None:
        """Emit the current Prometheus exposition as a fenced block.

        Each scrape is framed with `# HYPER_METRICS_BEGIN` and
        `# HYPER_METRICS_END` markers so downstream consumers can
        trivially extract the block from an interleaved log stream.
        """
        if not self.include_metrics:
            return
        if not prometheus_text:
            return
        text = prometheus_text.decode("utf-8", errors="replace")
        payload = (
            "# HYPER_METRICS_BEGIN\n"
            + text
            + ("\n" if not text.endswith("\n") else "")
            + "# HYPER_METRICS_END\n"
        )
        self.stream.write(payload)
        self.stream.flush()

    def export_spans(self, spans: list[dict]) -> None:
        """Emit each span as a single JSON line.

        Writes are coalesced into one `write` call per batch so the
        lines stay grouped in the output stream even under concurrent
        logging from other threads.
        """
        if not spans:
            return
        parts: list[str] = []
        prefix = self.span_prefix
        for span in spans:
            body = fast_json_dumps(span)
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            if prefix:
                parts.append(prefix)
                parts.append(body)
                parts.append("\n")
            else:
                parts.append(body)
                parts.append("\n")
        self.stream.write("".join(parts))
        self.stream.flush()

    def flush(self) -> None:
        """Flush the underlying stream. Called on shutdown."""
        with contextlib.suppress(Exception):
            self.stream.flush()

    def close(self) -> None:
        """No-op — we don't own stdout."""
        return
