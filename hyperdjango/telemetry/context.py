"""
Span context propagation via `contextvars.ContextVar` (v0.15.0+).

The active `SpanContext` is stored in a `ContextVar` so it
propagates automatically across `await` boundaries in async
handlers and across child tasks spawned via `asyncio.gather`.
Free-threading safe because each thread has its own context stack.

Usage (rarely called directly — `Tracer.start_span()` is the
canonical entry point; context is what `Span.current()` reads):

    from hyperdjango.telemetry.context import SpanContext, current, set, reset

    ctx = SpanContext(trace_id_high=..., trace_id_low=..., span_id=..., sampled=True)
    token = set(ctx)
    try:
        ...  # child spans inherit
    finally:
        reset(token)
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpanContext:
    """Immutable active-span identity for contextvar propagation.

    Fields match the W3C trace-context model:

        trace_id_high / trace_id_low:   the 128-bit trace ID, split so
                                        we can pass it as two u64s to
                                        the Zig FFI without needing a
                                        u128 dance at the C boundary
        span_id:                        64-bit span handle from the
                                        native ring (or 0 for
                                        unsampled/sentinel spans)
        parent_id:                      parent span handle (for child
                                        linkage) — 0 for root spans
        sampled:                        head-based sampling decision;
                                        children inherit so all spans
                                        in a trace have consistent
                                        sampling
    """

    trace_id_high: int
    trace_id_low: int
    span_id: int
    parent_id: int
    sampled: bool

    @property
    def is_valid(self) -> bool:
        """True if this context carries a real span (non-sentinel)."""
        return self.span_id != 0

    @property
    def trace_id_hex(self) -> str:
        """Lowercase 32-char hex of the full 128-bit trace ID.

        Matches the W3C trace-context wire format used by every major
        OpenTelemetry collector. Use this when correlating logs +
        spans across services.
        """
        return f"{self.trace_id_high:016x}{self.trace_id_low:016x}"

    @property
    def span_id_hex(self) -> str:
        """Lowercase 16-char hex of the 64-bit span ID."""
        return f"{self.span_id:016x}"

    def to_log_extra(self) -> dict[str, str]:
        """Return a dict suitable for `logger.bind(**ctx.to_log_extra())`.

        Produces the three fields the JSON sink promotes to top-level
        for trace correlation:

            {"trace_id": "<hex>", "span_id": "<hex>", "trace_flags": "01"|"00"}

        These match the W3C/OTel field names so log aggregators
        can join logs and traces by trace_id without any extra
        mapping configuration.
        """
        return {
            "trace_id": self.trace_id_hex,
            "span_id": self.span_id_hex,
            "trace_flags": "01" if self.sampled else "00",
        }


# Module-level ContextVar. Default `None` means "no active span".
_current_span: ContextVar[SpanContext | None] = ContextVar(
    "hyper_telemetry_span",
    default=None,
)


def current() -> SpanContext | None:
    """Return the active span context for the current task/thread.

    Returns None if no span has been started in this context yet —
    meaning we're outside any traced handler. Hot-path callers (e.g.
    `record_query` in the DB layer) should check for None before
    dereferencing.
    """
    return _current_span.get()


def set(ctx: SpanContext) -> Token:
    """Install `ctx` as the active span context.

    Returns a `Token` that MUST be passed to `reset(token)` on the
    same task/thread to restore the previous context. Use
    `contextmanager`-style try/finally or — preferred —
    `Tracer.start_span()` which handles this automatically.
    """
    return _current_span.set(ctx)


def reset(token: Token) -> None:
    """Restore the previous span context. Pairs with `set()`."""
    _current_span.reset(token)
