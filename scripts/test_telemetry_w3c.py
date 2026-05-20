"""
Unit + Hypothesis property tests for hyperdjango/telemetry/w3c.py.

# hyper-test: unit

Coverage:

    1.  parse_traceparent — valid level-1 header round-trips
    2.  parse_traceparent — every documented rejection path
    3.  format_traceparent — version, hex width, flag bit
    4.  round-trip: parse(format(ctx)) == ctx
    5.  parse_tracestate — single entry, multi entry, vendor suffix
    6.  parse_tracestate — rejects bad keys/values, honors 32-entry cap
    7.  format_tracestate — insertion order + invalid-entry drop
    8.  Hypothesis: random 128-bit trace IDs + 64-bit span IDs round-trip
    9.  Hypothesis: mutations of the canonical traceparent are rejected
"""

import sys

from hyperdjango.telemetry.context import SpanContext
from hyperdjango.telemetry.w3c import (
    TRACEPARENT_LENGTH,
    format_traceparent,
    format_tracestate,
    parse_traceparent,
    parse_tracestate,
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


# Canonical sample from the W3C spec examples:
# version=00, trace-id=0af7651916cd43dd8448eb211c80319c
# parent-id=b7ad6b7169203331, flags=01
CANON = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


# ── 1. parse_traceparent happy path ────────────────────────────────────────
def test_parse_happy_path() -> None:
    print("\n── parse_traceparent: happy path ──")
    ctx = parse_traceparent(CANON)
    check("parse returns SpanContext", isinstance(ctx, SpanContext))
    assert ctx is not None
    check("trace_id_high parsed", ctx.trace_id_high == 0x0AF7651916CD43DD)
    check("trace_id_low parsed", ctx.trace_id_low == 0x8448EB211C80319C)
    check("span_id parsed", ctx.span_id == 0xB7AD6B7169203331)
    check("parent_id = 0 (root in our process)", ctx.parent_id == 0)
    check("sampled flag set", ctx.sampled is True)

    ctx_unsampled = parse_traceparent(
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"
    )
    assert ctx_unsampled is not None
    check("sampled flag clear", ctx_unsampled.sampled is False)


# ── 2. parse_traceparent rejection paths ───────────────────────────────────
def test_parse_rejects_garbage() -> None:
    print("\n── parse_traceparent: rejection paths ──")

    # None / empty
    check("None returns None", parse_traceparent(None) is None)
    check("empty returns None", parse_traceparent("") is None)
    check("whitespace returns None", parse_traceparent("   ") is None)

    # Wrong length
    check("too short returns None", parse_traceparent("00-abc-def-01") is None)
    check("too long returns None", parse_traceparent(CANON + "-extra") is None)

    # Missing dash separators at required positions
    check(
        "dash missing returns None",
        parse_traceparent(CANON.replace("-", "_", 1)) is None,
    )

    # Unknown version (spec says forward compat, we reject)
    check(
        "version != 00 returns None",
        parse_traceparent("ff" + CANON[2:]) is None,
    )
    check(
        "version=99 returns None",
        parse_traceparent("99" + CANON[2:]) is None,
    )

    # Reserved all-zero trace_id
    all_zero_trace = "00-00000000000000000000000000000000-b7ad6b7169203331-01"
    check("all-zero trace_id rejected", parse_traceparent(all_zero_trace) is None)

    # Reserved all-zero span_id
    all_zero_span = "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01"
    check("all-zero span_id rejected", parse_traceparent(all_zero_span) is None)

    # Uppercase hex — spec forbids
    upper = "00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01"
    check("uppercase hex rejected", parse_traceparent(upper) is None)

    # Non-hex character mid-trace
    bad = "00-0af7651916cd43dd8448eb211c80319g-b7ad6b7169203331-01"
    check("non-hex char rejected", parse_traceparent(bad) is None)

    # Multi-valued header — take first entry
    # (first valid entry still decodes, second is ignored)
    multi = CANON + ",foo=bar"
    mctx = parse_traceparent(multi)
    check("multi-valued header takes first entry", mctx is not None)


# ── 3. format_traceparent ──────────────────────────────────────────────────
def test_format() -> None:
    print("\n── format_traceparent ──")
    ctx = SpanContext(
        trace_id_high=0x0AF7651916CD43DD,
        trace_id_low=0x8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        parent_id=0,
        sampled=True,
    )
    header = format_traceparent(ctx)
    check("format produces CANON", header == CANON, f"got {header!r}")
    check("format length is 55", len(header) == TRACEPARENT_LENGTH)

    ctx_unsampled = SpanContext(
        trace_id_high=0x0AF7651916CD43DD,
        trace_id_low=0x8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        parent_id=0,
        sampled=False,
    )
    unsampled_header = format_traceparent(ctx_unsampled)
    check("unsampled ends in -00", unsampled_header.endswith("-00"))
    check("sampled ends in -01", header.endswith("-01"))


# ── 4. round-trip ──────────────────────────────────────────────────────────
def test_round_trip() -> None:
    print("\n── parse(format(ctx)) round-trip ──")
    samples: list[SpanContext] = [
        SpanContext(
            trace_id_high=0x0AF7651916CD43DD,
            trace_id_low=0x8448EB211C80319C,
            span_id=0xB7AD6B7169203331,
            parent_id=0,
            sampled=True,
        ),
        SpanContext(
            trace_id_high=0x1,
            trace_id_low=0x1,
            span_id=0x1,
            parent_id=0,
            sampled=False,
        ),
        SpanContext(
            trace_id_high=0xFFFFFFFFFFFFFFFF,
            trace_id_low=0xFFFFFFFFFFFFFFFF,
            span_id=0xFFFFFFFFFFFFFFFF,
            parent_id=0,
            sampled=True,
        ),
    ]
    for i, ctx in enumerate(samples):
        header = format_traceparent(ctx)
        back = parse_traceparent(header)
        assert back is not None, f"roundtrip produced None for {ctx}"
        check(
            f"round-trip #{i} trace_id_high",
            back.trace_id_high == ctx.trace_id_high,
        )
        check(
            f"round-trip #{i} trace_id_low",
            back.trace_id_low == ctx.trace_id_low,
        )
        check(f"round-trip #{i} span_id", back.span_id == ctx.span_id)
        check(f"round-trip #{i} sampled", back.sampled == ctx.sampled)


# ── 5. parse_tracestate happy path ─────────────────────────────────────────
def test_parse_tracestate_happy() -> None:
    print("\n── parse_tracestate: happy path ──")
    simple = parse_tracestate("vendora=abc123")
    check("single entry parsed", simple == {"vendora": "abc123"})

    multi = parse_tracestate("vendora=abc,vendorb=def,vendorc=ghi")
    check("multi entries parsed", len(multi) == 3)
    check("multi preserves keys", list(multi) == ["vendora", "vendorb", "vendorc"])

    tenant = parse_tracestate("rojo@nr=00f067aa0ba902b7")
    check("tenant@vendor key parsed", tenant == {"rojo@nr": "00f067aa0ba902b7"})

    with_spaces = parse_tracestate(" vendor=value , other=x ")
    check("stripped whitespace", with_spaces == {"vendor": "value", "other": "x"})

    # Empty / None
    check("None → {}", parse_tracestate(None) == {})
    check("empty → {}", parse_tracestate("") == {})
    check("just commas → {}", parse_tracestate(",,,,") == {})


# ── 6. parse_tracestate rejection paths ────────────────────────────────────
def test_parse_tracestate_rejects() -> None:
    print("\n── parse_tracestate: rejection paths ──")
    # Bad key — uppercase
    res = parse_tracestate("VENDOR=x,good=y")
    check("uppercase key dropped", res == {"good": "y"})

    # Missing '='
    res = parse_tracestate("novalue,good=y")
    check("missing = dropped", res == {"good": "y"})

    # Trailing '='
    res = parse_tracestate("empty=,good=y")
    check("empty value dropped", res == {"good": "y"})

    # Leading '='
    res = parse_tracestate("=orphan,good=y")
    check("orphan leading = dropped", res == {"good": "y"})

    # Bad value — comma embedded (would be split earlier)
    res = parse_tracestate("k=bad=val,good=y")
    check("equals-in-value dropped", res == {"good": "y"})

    # 32-entry cap
    many = ",".join(f"k{i}=v{i}" for i in range(40))
    res = parse_tracestate(many)
    check("32-entry cap honored", len(res) == 32)


# ── 7. format_tracestate ────────────────────────────────────────────────────
def test_format_tracestate() -> None:
    print("\n── format_tracestate ──")
    formatted = format_tracestate({"vendora": "x", "vendorb": "y"})
    check("insertion order preserved", formatted == "vendora=x,vendorb=y")

    # Invalid entries dropped
    formatted = format_tracestate({"VENDOR": "x", "good": "y", "bad": ""})
    check("invalid keys/values dropped", formatted == "good=y")

    # Round-trip through parse
    original = {"vendora": "abc", "vendorb": "def"}
    formatted = format_tracestate(original)
    reparsed = parse_tracestate(formatted)
    check("tracestate round-trip", reparsed == original)


# ── 8. Hypothesis: random IDs round-trip ───────────────────────────────────
def test_hypothesis_round_trip() -> None:
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis round-trip: SKIPPED ──")
        return
    print("\n── Hypothesis: random SpanContext round-trip ──")

    @given(
        trace_high=st.integers(min_value=1, max_value=(1 << 64) - 1),
        trace_low=st.integers(min_value=0, max_value=(1 << 64) - 1),
        span_id=st.integers(min_value=1, max_value=(1 << 64) - 1),
        sampled=st.booleans(),
    )
    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(trace_high: int, trace_low: int, span_id: int, sampled: bool) -> None:
        ctx = SpanContext(
            trace_id_high=trace_high,
            trace_id_low=trace_low,
            span_id=span_id,
            parent_id=0,
            sampled=sampled,
        )
        header = format_traceparent(ctx)
        assert len(header) == TRACEPARENT_LENGTH
        back = parse_traceparent(header)
        assert back is not None
        assert back.trace_id_high == ctx.trace_id_high
        assert back.trace_id_low == ctx.trace_id_low
        assert back.span_id == ctx.span_id
        assert back.sampled == ctx.sampled

    fuzz()
    check("hypothesis round-trip", True)


# ── 9. Hypothesis: mutations of CANON are rejected ─────────────────────────
def test_hypothesis_mutations_rejected() -> None:
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis mutations: SKIPPED ──")
        return
    print("\n── Hypothesis: arbitrary mutations of a valid header ──")

    # Build a fresh valid header each iteration (not the literal CANON
    # so shrinking has room to move).
    @given(
        pos=st.integers(min_value=0, max_value=TRACEPARENT_LENGTH - 1),
        replacement=st.characters(
            min_codepoint=0x20,
            max_codepoint=0x7E,
            blacklist_characters="-",
        ),
    )
    @settings(
        max_examples=150,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(pos: int, replacement: str) -> None:
        # Skip positions where replacing with a non-dash ASCII is
        # still valid (e.g. hex digit swap inside the trace-id).
        mutated = CANON[:pos] + replacement + CANON[pos + 1 :]
        if mutated == CANON:
            return
        ctx = parse_traceparent(mutated)
        # Mutation is valid only if it's a hex swap in trace_id /
        # span_id / flags slot. Position 0-1 is version, 2 is dash,
        # 3-34 is trace_id, 35 is dash, 36-51 is span_id, 52 is dash,
        # 53-54 is flags.
        in_version = pos in (0, 1)
        in_dash = pos in (2, 35, 52)
        in_hex_slot = 3 <= pos <= 34 or 36 <= pos <= 51 or 53 <= pos <= 54
        if in_version:
            # Only '0' at pos 0 or pos 1 is valid, anything else rejects
            expected_reject = replacement != "0"
            if expected_reject:
                assert ctx is None, f"expected reject at version pos, got {ctx!r}"
        elif in_dash:
            # Dash was replaced with non-dash → format broken → reject
            assert ctx is None, f"expected reject at dash pos, got {ctx!r}"
        elif in_hex_slot:
            # Hex slot — valid only for [0-9a-f]
            is_hex = "0" <= replacement <= "9" or "a" <= replacement <= "f"
            if not is_hex:
                assert ctx is None, f"expected reject at hex pos {pos}, got {ctx!r}"

    fuzz()
    check("hypothesis mutation fuzz", True)


def main() -> int:
    print("=" * 70)
    print("  W3C traceparent + tracestate parse/format unit + fuzz")
    print("=" * 70)

    test_parse_happy_path()
    test_parse_rejects_garbage()
    test_format()
    test_round_trip()
    test_parse_tracestate_happy()
    test_parse_tracestate_rejects()
    test_format_tracestate()
    test_hypothesis_round_trip()
    test_hypothesis_mutations_rejected()

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
