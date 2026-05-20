"""
PrometheusSink — pull-based metric export via a `/metrics` HTTP endpoint.

Prometheus is a pull-based system: the Prometheus server scrapes
`/metrics` on the app at a configured interval (typically 15s).
This sink:

  1. On every `export_metrics(bytes)` callback from the drain loop,
     caches the latest Prometheus text exposition in `_cached_text`.
  2. Serves that cached bytes object directly from the HTTP
     handler with zero per-scrape computation — the handler just
     reads the atomic reference and returns it in a Response.

Span export is a no-op (Prometheus is metrics-only). Users who want
span data send it to a different sink (Stdout, InMemory, or a
user-provided adapter).

Usage:

    prom = PrometheusSink()
    app.use(TelemetryMiddleware(sinks=[prom]))   # Phase 4
    # OR mount the handler directly without middleware:
    app.get("/metrics")(prom.handler)

Background-thread safety: `_cached_text` is a plain reference
assignment guarded by an atomic publish. On CPython 3.14t (free-
threading) single-assignment to a module/attribute slot is
thread-safe — the reader sees either the old bytes or the new
bytes, never a torn value. We don't need a lock.
"""

from dataclasses import dataclass, field

from hyperdjango.response import Response
from hyperdjango.telemetry.metrics import collect_prometheus_text


@dataclass(slots=True)
class PrometheusSink:
    """Pull-based sink serving `/metrics` from cached exposition text.

    The cached text is populated on every drain interval by the
    TelemetryMiddleware's background drain thread. If no drain has
    happened yet, the handler falls back to computing the exposition
    text on demand — this makes the sink usable WITHOUT the
    middleware (just mount `prom.handler` manually).
    """

    _cached_text: bytes = field(default=b"", init=False, repr=False)

    # ── TelemetrySink protocol ──────────────────────────────────────────────

    def export_metrics(self, prometheus_text: bytes) -> None:
        """Receive the latest exposition text from the drain thread."""
        self._cached_text = prometheus_text

    def export_spans(self, spans: list[dict]) -> None:
        """No-op — Prometheus is metrics-only."""
        # The TelemetryMiddleware drain loop calls this with batches
        # of spans; we silently drop them. Users who want span
        # export register a different sink alongside this one.
        return

    def flush(self) -> None:
        """Nothing to flush — we're pull-based."""
        return

    def close(self) -> None:
        """Nothing to release."""
        return

    # ── HTTP handler ────────────────────────────────────────────────────────

    async def handler(self, request) -> Response:
        """Prometheus scrape handler — serve `_cached_text` directly.

        Falls back to a fresh `collect_prometheus_text()` call if
        the drain thread hasn't populated the cache yet. This lets
        apps use the sink without registering TelemetryMiddleware.
        """
        body = self._cached_text
        if not body:
            body = collect_prometheus_text()
        return Response(
            status=200,
            body=body,
            headers={
                "content-type": "text/plain; version=0.0.4; charset=utf-8",
                "cache-control": "no-store",
            },
        )
