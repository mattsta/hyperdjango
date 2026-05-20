"""
Sampling policies — head-based decision at span start (v0.15.0+).

The sampler returns True to record a span, False to drop it. The
decision is made ONCE at span start and propagates to all child
spans via `SpanContext.sampled`, so every span in a single trace
has consistent sampling (never a partially-recorded trace).

Built-in policies:

    AlwaysSample    — record every span. For tests + debug only.
    NeverSample     — record no spans. Disables span data entirely
                      while metric counters keep flowing.
    RatioSample(r)  — record a `r` fraction of spans via a cheap
                      comparison against a per-trace random draw.
                      Default 0.01 (1%) for the framework itself.
    ParentBased     — if parent span is sampled, inherit True; if
                      parent is unsampled OR absent, fall through
                      to the wrapped root policy. This is the
                      default and matches OpenTelemetry semantics.

All policies implement a single method:

    should_sample(parent: SpanContext | None, trace_id_low: int) -> bool

`trace_id_low` is used as the random seed for `RatioSample` — that
way, re-running the same trace ID always produces the same sampling
decision (helpful for idempotent debugging). We use the low 64 bits
because they're already random from the ID generator.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from hyperdjango.telemetry.context import SpanContext


@runtime_checkable
class SamplingPolicy(Protocol):
    """Protocol for head-based sampling decisions.

    Implementations should set the `requires_trace_id_for_root_decision`
    class attribute to True when their `should_sample` decision depends
    on the `trace_id_low` argument for ROOT spans (no parent context).
    The Tracer reads this flag in `_make_span` and skips the cost of
    generating a trace_id when it isn't needed by the sampler — a ~1 μs
    per-request saving on the unsampled hot path.

    For child spans (parent != None), trace_id is always inherited from
    the parent and the flag is not consulted.
    """

    requires_trace_id_for_root_decision: bool

    def should_sample(
        self,
        parent: SpanContext | None,
        trace_id_low: int,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AlwaysSample:
    """Record every span. Zero overhead decision — just returns True."""

    # Decision is constant True; trace_id_low is ignored. Tracer can
    # defer trace_id generation until AFTER `should_sample` returns.
    requires_trace_id_for_root_decision: bool = False

    def should_sample(
        self,
        parent: SpanContext | None,
        trace_id_low: int,
    ) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class NeverSample:
    """Record no spans. Metric counters are unaffected."""

    # Decision is constant False; trace_id_low is ignored. The Tracer
    # never has to call _new_trace_id when this is the active root
    # sampler — a 700-1000 ns per-request saving on the floor cost.
    requires_trace_id_for_root_decision: bool = False

    def should_sample(
        self,
        parent: SpanContext | None,
        trace_id_low: int,
    ) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class RatioSample:
    """Sample a fixed fraction of traces.

    Decision is deterministic on `trace_id_low`: `(trace_id_low &
    0xFFFF_FFFF) < threshold` where `threshold = ratio * 2**32`.
    This means the same trace ID always produces the same decision —
    critical for propagating a sampling choice from an upstream
    service via `traceparent` and having downstream services agree.
    """

    ratio: float = 0.01
    # The decision depends on `trace_id_low`, so the Tracer must
    # generate one before calling `should_sample` (unless a parent
    # context is providing it).
    requires_trace_id_for_root_decision: bool = True

    def __post_init__(self) -> None:
        # frozen dataclass — we validate here but can't mutate. Raise
        # on invalid input so misuse fails fast at construction.
        if not (0.0 <= self.ratio <= 1.0):
            raise ValueError(f"RatioSample ratio must be in [0, 1], got {self.ratio}")

    def should_sample(
        self,
        parent: SpanContext | None,
        trace_id_low: int,
    ) -> bool:
        if self.ratio >= 1.0:
            return True
        if self.ratio <= 0.0:
            return False
        threshold = int(self.ratio * (1 << 32))
        return (trace_id_low & 0xFFFFFFFF) < threshold


@dataclass(frozen=True, slots=True)
class ParentBased:
    """Respect the parent span's sampling decision; if no parent,
    delegate to the wrapped `root` policy.

    This is the standard OpenTelemetry behavior: once a trace has
    been sampled (or not) at the root, every descendant in the
    same trace inherits the decision. Only root spans consult the
    root policy.

    The `requires_trace_id_for_root_decision` flag is forwarded from
    the wrapped root policy via `__post_init__` so the Tracer can
    still skip trace_id generation when ParentBased(NeverSample()) is
    used as the sampler.
    """

    root: SamplingPolicy
    # Inherits from `root` — set by __post_init__ on construction.
    # frozen=True means we use object.__setattr__ to set it.
    requires_trace_id_for_root_decision: bool = field(init=False, default=True)

    def __post_init__(self) -> None:
        # frozen dataclass workaround — bypass __setattr__ to set the
        # forwarded flag. Reads from the wrapped root sampler so
        # ParentBased(NeverSample()) skips trace_id generation just
        # like NeverSample alone would.
        # dynamic-attr: frozen dataclass — object.__setattr__ is the only way to set the forwarded field in __post_init__
        object.__setattr__(
            self,
            "requires_trace_id_for_root_decision",
            # dynamic-attr: self.root is an arbitrary SamplingPolicy (runtime_checkable Protocol); the data flag is advisory ("should set") and may be absent on a user policy
            getattr(self.root, "requires_trace_id_for_root_decision", True),
        )

    def should_sample(
        self,
        parent: SpanContext | None,
        trace_id_low: int,
    ) -> bool:
        # Only a genuinely absent parent (root span) delegates to the root
        # policy. A present-but-unsampled parent has span_id 0 → is_valid
        # False, but its sampling decision (sampled=False) must still be
        # honoured — re-sampling it from scratch would record partial traces
        # whose upstream root was explicitly dropped.
        if parent is not None:
            return parent.sampled
        return self.root.should_sample(parent, trace_id_low)
