"""
W3C trace-context header parse + format (v0.15.0+).

Implements the two headers defined in https://www.w3.org/TR/trace-context/:

    traceparent: 00-<trace_id_32hex>-<parent_id_16hex>-<flags_2hex>
    tracestate: key1=value1,key2=value2[,...]

`parse_traceparent` accepts a raw header string (usually from
`Request.headers.get("traceparent")`) and returns a `SpanContext` ready
to install via `contextvars.ContextVar` — or `None` if the header is
malformed, blank, or from a future spec version we don't understand.
No exceptions are raised; the caller's middleware gets a clean
"there is no parent context" signal.

`format_traceparent` goes the other way — used by outbound HTTP clients
to propagate the active span into a downstream service. Also returns a
plain string so the caller can stick it straight into a headers dict.

Strict mode:

  * version MUST be "00" (spec allows forward-compat but we reject
    unknown versions rather than guessing — safer for correctness)
  * trace_id MUST NOT be the all-zero reserved value
  * parent_id MUST NOT be the all-zero reserved value
  * all hex MUST be lowercase (spec requirement)
  * exact length check (55 chars for traceparent)

This module is deliberately standalone — zero dependencies on the
native Zig extension, zero dependencies on `Tracer`. It can be imported
in any test or middleware without triggering telemetry initialization.
"""

from hyperdjango.telemetry.context import SpanContext

# ── Format constants ────────────────────────────────────────────────────────

TRACEPARENT_VERSION: str = "00"
TRACEPARENT_LENGTH: int = 55  # "00-" + 32 + "-" + 16 + "-" + 2

TRACE_ID_HEX_LENGTH: int = 32
SPAN_ID_HEX_LENGTH: int = 16
FLAGS_HEX_LENGTH: int = 2

TRACE_ID_ZERO: str = "0" * TRACE_ID_HEX_LENGTH
SPAN_ID_ZERO: str = "0" * SPAN_ID_HEX_LENGTH

# tracestate limits from the spec: up to 32 entries, each up to 256 bytes.
TRACESTATE_MAX_ENTRIES: int = 32
TRACESTATE_MAX_ENTRY_BYTES: int = 256

# Flag bits (only bit 0 "sampled" is defined in W3C level 1)
FLAG_SAMPLED: int = 0x01


# ── traceparent ─────────────────────────────────────────────────────────────


def parse_traceparent(header: str | None) -> SpanContext | None:
    """Parse a W3C `traceparent` header into a `SpanContext`.

    Returns None on any malformed, blank, or unknown-version input so
    callers (e.g. `TelemetryMiddleware`) can treat it as "no inbound
    context" and start a fresh trace. Never raises.

    The returned `SpanContext` has `parent_id=0` (it's a root in our
    process — the incoming span_id becomes our parent_handle when the
    first local span starts) and `sampled` pulled from flag bit 0.
    """
    if header is None:
        return None
    # Spec: a single traceparent value. If multiple are joined with
    # a comma (e.g. via `,`.join on a multi-valued header), we take
    # the first field.
    first = header.split(",", 1)[0].strip()
    if len(first) != TRACEPARENT_LENGTH:
        return None
    # Four dash-separated fields, strict positions (dash at 2, 35, 52)
    if first[2] != "-" or first[35] != "-" or first[52] != "-":
        return None
    version = first[0:2]
    trace_id_hex = first[3:35]
    span_id_hex = first[36:52]
    flags_hex = first[53:55]
    # Only version 00 supported (level 1)
    if version != TRACEPARENT_VERSION:
        return None
    # Forbid all-zero reserved values
    if trace_id_hex == TRACE_ID_ZERO or span_id_hex == SPAN_ID_ZERO:
        return None
    # Lowercase hex only
    if not _is_lower_hex(trace_id_hex):
        return None
    if not _is_lower_hex(span_id_hex):
        return None
    if not _is_lower_hex(flags_hex):
        return None
    trace_high = int(trace_id_hex[0:16], 16)
    trace_low = int(trace_id_hex[16:32], 16)
    span_id = int(span_id_hex, 16)
    flags = int(flags_hex, 16)
    return SpanContext(
        trace_id_high=trace_high,
        trace_id_low=trace_low,
        span_id=span_id,
        parent_id=0,
        sampled=bool(flags & FLAG_SAMPLED),
    )


def format_traceparent(ctx: SpanContext) -> str:
    """Format a `SpanContext` as a W3C `traceparent` header value.

    Always emits version "00" and lowercase hex (spec requirement).
    The `span_id` field carries whatever handle is currently active —
    downstream services will see this as their parent-id.

    Implementation: one combined f-string instead of four separate
    f-strings + concatenations. Saves ~3 intermediate `str` allocs
    per call on the recorded-span hot path — at AlwaysSample this
    runs 1x per request, so the saving compounds across a trace.
    """
    flags_hex = "01" if ctx.sampled else "00"
    return (
        f"{TRACEPARENT_VERSION}-"
        f"{ctx.trace_id_high:016x}{ctx.trace_id_low:016x}-"
        f"{ctx.span_id:016x}-{flags_hex}"
    )


# ── tracestate ──────────────────────────────────────────────────────────────


def parse_tracestate(header: str | None) -> dict[str, str]:
    """Parse a W3C `tracestate` header into a vendor key/value dict.

    Returns an empty dict on blank/malformed input. Per spec, entries
    past the 32-entry limit are silently dropped (not an error).
    Individual malformed entries are also dropped rather than
    rejecting the whole header. Keys + values preserve insertion
    order so tracestate serialization stays stable.

    Spec reference:
        https://www.w3.org/TR/trace-context/#tracestate-header
    """
    if not header:
        return {}
    result: dict[str, str] = {}
    for raw in header.split(","):
        if len(result) >= TRACESTATE_MAX_ENTRIES:
            break
        entry = raw.strip()
        if not entry:
            continue
        if len(entry.encode("utf-8")) > TRACESTATE_MAX_ENTRY_BYTES:
            continue
        eq = entry.find("=")
        if eq <= 0 or eq == len(entry) - 1:
            # No key, missing value, or leading '=' — invalid.
            continue
        key = entry[:eq]
        value = entry[eq + 1 :]
        if not _is_valid_tracestate_key(key):
            continue
        if not _is_valid_tracestate_value(value):
            continue
        # Last-write-wins on duplicate keys (spec says first-wins,
        # but downstream mutation is expected to replace entries —
        # we keep insertion order of the first seen key for stability).
        if key not in result:
            result[key] = value
    return result


def format_tracestate(state: dict[str, str]) -> str:
    """Format a tracestate dict back to the W3C header value.

    Entries are emitted in insertion order. Invalid keys/values are
    silently skipped (same tolerance as `parse_tracestate`). Returns
    an empty string for an empty / all-invalid input.
    """
    parts: list[str] = []
    for key, value in state.items():
        if len(parts) >= TRACESTATE_MAX_ENTRIES:
            break
        if not _is_valid_tracestate_key(key):
            continue
        if not _is_valid_tracestate_value(value):
            continue
        entry = f"{key}={value}"
        if len(entry.encode("utf-8")) > TRACESTATE_MAX_ENTRY_BYTES:
            continue
        parts.append(entry)
    return ",".join(parts)


# ── Internal helpers ────────────────────────────────────────────────────────


def _is_lower_hex(s: str) -> bool:
    """Return True if every character is 0-9 or a-f."""
    for ch in s:
        c = ord(ch)
        if not (
            (48 <= c <= 57)  # 0-9
            or (97 <= c <= 102)  # a-f
        ):
            return False
    return True


def _is_valid_tracestate_key(key: str) -> bool:
    """tracestate key: 1-256 chars, lowercase alnum + `_-*/`, with
    an optional vendor suffix `@<vendor>` (vendor is 1-14 chars of
    lowercase alnum + `_-*/`).

    The spec distinguishes two key formats:
        * simple-key   lcalpha *(lcalphanum / "_" / "-" / "*" / "/")
        * multi-tenant tenant@vendor

    We implement a permissive reading of both.
    """
    if not key or len(key) > 256:
        return False
    # Split off optional `@vendor`
    if "@" in key:
        tenant, vendor = key.split("@", 1)
        if not tenant or not vendor or len(vendor) > 14:
            return False
        return _key_chars_valid(tenant) and _key_chars_valid(vendor)
    return _key_chars_valid(key)


def _key_chars_valid(part: str) -> bool:
    if not part:
        return False
    # First char must be lowercase alpha (a-z) or digit (for vendor).
    # The spec actually says "lcalpha" for the first char of a
    # simple-key but allows digits as the first char of the tenant
    # part of a multi-tenant key. We accept both to keep the two
    # branches of _is_valid_tracestate_key symmetrical; this is a
    # superset of the spec and never produces false rejections on
    # valid input.
    first = ord(part[0])
    if not ((97 <= first <= 122) or (48 <= first <= 57)):
        return False
    for ch in part[1:]:
        c = ord(ch)
        if (
            (97 <= c <= 122)  # a-z
            or (48 <= c <= 57)  # 0-9
            or ch in "_-*/"
        ):
            continue
        return False
    return True


def _is_valid_tracestate_value(value: str) -> bool:
    """tracestate value: printable ASCII (0x20-0x7E) minus `,` and `=`.
    Empty values are invalid per spec; leading/trailing whitespace
    must already have been stripped by the caller.
    """
    if not value:
        return False
    for ch in value:
        c = ord(ch)
        if c < 0x20 or c > 0x7E:
            return False
        if ch == "," or ch == "=":
            return False
    return True
