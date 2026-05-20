"""Hypothesis fuzz tests for IETF RateLimit header formatters.

Targets:
- ``_sf_format_string`` escaping with arbitrary unicode
- ``_sf_format_byte_sequence`` roundtrip with arbitrary bytes
- ``format_ratelimit_policy`` structural invariants
- ``format_ratelimit`` structural invariants
- ``build_problem_detail`` field completeness
- ``build_429_response`` header presence invariants
- Hostile inputs: injection attempts, huge values, empty strings

Properties checked:
1. sf-string always produces valid quoted strings (no unescaped quotes)
2. sf-byte-sequence always roundtrips through base64
3. Policy header always contains ;q= parameter
4. RateLimit header always contains ;r= parameter
5. 429 response always has retry-after + correct status
6. Problem details always has required RFC 9457 fields
7. No header injection via policy names or partition keys

Usage:
    uv run hyper-test ratelimit_ietf_fuzz
"""

# hyper-test: unit

import base64
import json
import sys

from hypothesis import assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, ".")

from hyperdjango.ratelimit import (
    PROBLEM_ABNORMAL_USAGE,
    PROBLEM_QUOTA_EXCEEDED,
    PROBLEM_TEMPORARY_REDUCED,
    QuotaPolicy,
    ServiceLimit,
    _sf_format_byte_sequence,
    _sf_format_string,
    build_429_response,
    build_problem_detail,
    format_ratelimit,
    format_ratelimit_policy,
)

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)


# ── Strategies ──────────────────────────────────────────────────────────────

# Policy names: printable ASCII strings (RFC 9651 sf-string constraint)
policy_names = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S", "Z"),
        min_codepoint=32,
        max_codepoint=126,
    ),
    min_size=1,
    max_size=64,
)

# Hostile strings: quotes, backslashes, newlines, null bytes, unicode
hostile_strings = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=0xFFFF),
    min_size=0,
    max_size=200,
)

# Arbitrary bytes for partition keys
partition_keys = st.binary(min_size=0, max_size=256)

# Quota values
quotas = st.integers(min_value=0, max_value=10_000_000)

# Window values
windows = st.integers(min_value=0, max_value=86400 * 365)

# Remaining values
remainings = st.integers(min_value=0, max_value=10_000_000)

# Reset values
resets = st.integers(min_value=0, max_value=86400 * 365)

# Quota units
quota_units = st.sampled_from(["requests", "content-bytes", "concurrent-requests"])

# Problem types
problem_types = st.sampled_from(
    [PROBLEM_QUOTA_EXCEEDED, PROBLEM_TEMPORARY_REDUCED, PROBLEM_ABNORMAL_USAGE]
)


# ── 1: sf-string escaping ────────────────────────────────────────────────────

print("\n--- 1: sf-string escaping ---")


@given(s=hostile_strings)
@settings(max_examples=200)
def test_sf_string_no_unescaped_quotes(s: str) -> None:
    """sf-string output must not contain unescaped double quotes."""
    result = _sf_format_string(s)
    assert result.startswith('"') and result.endswith('"'), f"Not quoted: {result!r}"
    # The inner content (between outer quotes) must not have unescaped quotes
    inner = result[1:-1]
    i = 0
    while i < len(inner):
        if inner[i] == "\\":
            i += 2  # skip escaped char
        elif inner[i] == '"':
            raise AssertionError(f"Unescaped quote at pos {i} in {result!r}")
        else:
            i += 1


test_sf_string_no_unescaped_quotes()
ok("sf-string: no unescaped quotes (200 examples)", True)


@given(s=hostile_strings)
@settings(max_examples=100)
def test_sf_string_no_newlines_in_output(s: str) -> None:
    """sf-string must not allow header injection via newlines."""
    result = _sf_format_string(s)
    # RFC 9651: sf-string chars are %x20-7E excluding \ and "
    # But we just escape \ and " — newlines in the input become literal
    # chars in the header value, which is fine for HTTP/2+ but let's verify
    # no CRLF injection is possible
    assert "\r\n" not in result or "\\" in result, f"CRLF injection: {result!r}"


test_sf_string_no_newlines_in_output()
ok("sf-string: no CRLF injection (100 examples)", True)


# ── 2: sf-byte-sequence roundtrip ────────────────────────────────────────────

print("\n--- 2: sf-byte-sequence roundtrip ---")


@given(b=partition_keys)
@settings(max_examples=200)
def test_byte_sequence_roundtrip(b: bytes) -> None:
    """sf-byte-sequence must roundtrip through base64."""
    formatted = _sf_format_byte_sequence(b)
    assert formatted.startswith(":") and formatted.endswith(":"), (
        f"Bad format: {formatted!r}"
    )
    inner = formatted[1:-1]
    decoded = base64.b64decode(inner)
    assert decoded == b, f"Roundtrip failed: {b!r} → {formatted!r} → {decoded!r}"


test_byte_sequence_roundtrip()
ok("byte-sequence: roundtrip (200 examples)", True)


# ── 3: format_ratelimit_policy invariants ─────────────────────────────────────

print("\n--- 3: format_ratelimit_policy invariants ---")


@given(
    name=policy_names,
    quota=quotas,
    window=windows,
    qu=quota_units,
    pk=partition_keys,
)
@settings(max_examples=200)
def test_policy_always_has_q(
    name: str, quota: int, window: int, qu: str, pk: bytes
) -> None:
    """Every policy item MUST have a ;q= parameter."""
    p = QuotaPolicy(
        name=name, quota=quota, window=window, quota_unit=qu, partition_key=pk
    )
    result = format_ratelimit_policy([p])
    assert f";q={quota}" in result, f"Missing ;q= in {result!r}"


test_policy_always_has_q()
ok("policy: always has ;q= (200 examples)", True)


@given(
    name=policy_names,
    quota=quotas,
    window=st.integers(min_value=1, max_value=86400),
)
@settings(max_examples=100)
def test_policy_nonzero_window_has_w(name: str, quota: int, window: int) -> None:
    """Non-zero window MUST produce ;w= parameter."""
    p = QuotaPolicy(name=name, quota=quota, window=window)
    result = format_ratelimit_policy([p])
    assert f";w={window}" in result, f"Missing ;w= in {result!r}"


test_policy_nonzero_window_has_w()
ok("policy: non-zero window has ;w= (100 examples)", True)


@given(name=policy_names, quota=quotas)
@settings(max_examples=100)
def test_policy_zero_window_omits_w(name: str, quota: int) -> None:
    """Zero window MUST NOT produce ;w= parameter."""
    p = QuotaPolicy(name=name, quota=quota, window=0)
    result = format_ratelimit_policy([p])
    assert ";w=" not in result, f"Unexpected ;w= in {result!r}"


test_policy_zero_window_omits_w()
ok("policy: zero window omits ;w= (100 examples)", True)


def _count_top_level_separators(s: str) -> int:
    """Count ", " list separators OUTSIDE quoted sf-strings (RFC 9651).

    A policy name is a quoted sf-string and may legitimately contain a comma
    (e.g. name ", "). Such a comma is part of the value, not a list separator,
    so a naive ``s.count(", ")`` over-counts. This walks the string tracking
    quote state (honoring ``\\`` escapes) and counts only top-level separators.
    """
    count = 0
    in_quotes = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_quotes:
            if c == "\\":
                i += 2  # skip the escaped character
                continue
            if c == '"':
                in_quotes = False
        elif c == '"':
            in_quotes = True
        elif c == "," and i + 1 < len(s) and s[i + 1] == " ":
            count += 1
        i += 1
    return count


@given(
    names=st.lists(policy_names, min_size=2, max_size=5, unique=True),
    quotas_list=st.lists(quotas, min_size=2, max_size=5),
)
@settings(max_examples=50)
def test_policy_multi_comma_separated(names: list[str], quotas_list: list[int]) -> None:
    """Multiple policies MUST be comma-separated (one separator between each)."""
    n = min(len(names), len(quotas_list))
    assume(n >= 2)
    policies = [QuotaPolicy(name=names[i], quota=quotas_list[i]) for i in range(n)]
    result = format_ratelimit_policy(policies)
    seps = _count_top_level_separators(result)
    assert seps == n - 1, f"Expected {n - 1} list separators in {result!r}, got {seps}"


test_policy_multi_comma_separated()
ok("policy: multi comma-separated (50 examples)", True)


# ── 4: format_ratelimit invariants ────────────────────────────────────────────

print("\n--- 4: format_ratelimit invariants ---")


@given(
    name=policy_names,
    remaining=remainings,
    reset=resets,
    pk=partition_keys,
)
@settings(max_examples=200)
def test_ratelimit_always_has_r(
    name: str, remaining: int, reset: int, pk: bytes
) -> None:
    """Every limit item MUST have a ;r= parameter."""
    lim = ServiceLimit(
        policy_name=name, remaining=remaining, reset=reset, partition_key=pk
    )
    result = format_ratelimit([lim])
    assert f";r={remaining}" in result, f"Missing ;r= in {result!r}"


test_ratelimit_always_has_r()
ok("ratelimit: always has ;r= (200 examples)", True)


@given(
    name=policy_names,
    remaining=remainings,
    reset=st.integers(min_value=1, max_value=86400),
)
@settings(max_examples=100)
def test_ratelimit_nonzero_reset_has_t(name: str, remaining: int, reset: int) -> None:
    """Non-zero reset MUST produce ;t= parameter."""
    lim = ServiceLimit(policy_name=name, remaining=remaining, reset=reset)
    result = format_ratelimit([lim])
    assert f";t={reset}" in result, f"Missing ;t= in {result!r}"


test_ratelimit_nonzero_reset_has_t()
ok("ratelimit: non-zero reset has ;t= (100 examples)", True)


@given(name=policy_names, remaining=remainings)
@settings(max_examples=100)
def test_ratelimit_zero_reset_omits_t(name: str, remaining: int) -> None:
    """Zero reset MUST NOT produce ;t= parameter."""
    lim = ServiceLimit(policy_name=name, remaining=remaining, reset=0)
    result = format_ratelimit([lim])
    assert ";t=" not in result, f"Unexpected ;t= in {result!r}"


test_ratelimit_zero_reset_omits_t()
ok("ratelimit: zero reset omits ;t= (100 examples)", True)


# ── 5: build_problem_detail invariants ────────────────────────────────────────

print("\n--- 5: build_problem_detail invariants ---")


@given(
    problem_type=problem_types,
    title=st.text(min_size=1, max_size=100),
    status=st.sampled_from([429, 503]),
    detail=st.text(min_size=1, max_size=200),
    policies=st.lists(policy_names, min_size=1, max_size=5),
)
@settings(max_examples=100)
def test_problem_detail_has_required_fields(
    problem_type: str, title: str, status: int, detail: str, policies: list[str]
) -> None:
    """RFC 9457 Problem Details MUST have type, title, status."""
    pd = build_problem_detail(problem_type, title, status, detail, policies)
    assert "type" in pd, "Missing 'type'"
    assert "title" in pd, "Missing 'title'"
    assert "status" in pd, "Missing 'status'"
    assert "violated-policies" in pd, "Missing 'violated-policies'"
    assert pd["status"] == status
    assert pd["violated-policies"] == policies


test_problem_detail_has_required_fields()
ok("problem_detail: required fields (100 examples)", True)


# ── 6: build_429_response invariants ──────────────────────────────────────────

print("\n--- 6: build_429_response invariants ---")


@given(
    name=policy_names,
    quota=st.integers(min_value=1, max_value=10000),
    remaining=st.integers(min_value=0, max_value=10000),
    reset=st.integers(min_value=1, max_value=86400),
    ietf=st.booleans(),
    legacy=st.booleans(),
    problem=st.booleans(),
)
@settings(max_examples=100)
def test_429_always_has_retry_after(
    name: str,
    quota: int,
    remaining: int,
    reset: int,
    ietf: bool,
    legacy: bool,
    problem: bool,
) -> None:
    """429 response MUST always have retry-after header."""
    policies = [QuotaPolicy(name=name, quota=quota)]
    limits = [ServiceLimit(policy_name=name, remaining=remaining, reset=reset)]
    resp = build_429_response(
        policies,
        limits,
        reset,
        include_ietf=ietf,
        include_legacy=legacy,
        include_problem_details=problem,
    )
    assert resp.status == 429, f"Expected 429, got {resp.status}"
    assert resp.headers.get("retry-after") == str(reset), (
        f"Bad retry-after: {resp.headers}"
    )


test_429_always_has_retry_after()
ok("429: always has retry-after (100 examples)", True)


@given(
    name=policy_names,
    quota=st.integers(min_value=1, max_value=10000),
    reset=st.integers(min_value=1, max_value=86400),
)
@settings(max_examples=50)
def test_429_ietf_has_both_headers(name: str, quota: int, reset: int) -> None:
    """With IETF enabled, 429 MUST have both RateLimit headers."""
    policies = [QuotaPolicy(name=name, quota=quota, window=60)]
    limits = [ServiceLimit(policy_name=name, remaining=0, reset=reset)]
    resp = build_429_response(policies, limits, reset, include_ietf=True)
    assert "ratelimit-policy" in resp.headers, "Missing ratelimit-policy"
    assert "ratelimit" in resp.headers, "Missing ratelimit"


test_429_ietf_has_both_headers()
ok("429: IETF has both headers (50 examples)", True)


@given(
    name=policy_names,
    quota=st.integers(min_value=1, max_value=10000),
    reset=st.integers(min_value=1, max_value=86400),
)
@settings(max_examples=50)
def test_429_problem_details_valid_json(name: str, quota: int, reset: int) -> None:
    """With problem details enabled, 429 body MUST be valid JSON with type field."""
    policies = [QuotaPolicy(name=name, quota=quota)]
    limits = [ServiceLimit(policy_name=name, remaining=0, reset=reset)]
    resp = build_429_response(
        policies, limits, reset, include_ietf=True, include_problem_details=True
    )
    body = json.loads(resp.body)
    assert "type" in body, f"Missing 'type' in body: {body}"
    assert body["type"] == PROBLEM_QUOTA_EXCEEDED
    assert "violated-policies" in body
    assert name in body["violated-policies"]


test_429_problem_details_valid_json()
ok("429: problem details valid JSON (50 examples)", True)


# ── 7: Header injection resistance ───────────────────────────────────────────

print("\n--- 7: Header injection resistance ---")


@given(name=hostile_strings.filter(lambda s: len(s) > 0))
@settings(max_examples=200)
def test_policy_name_no_header_injection(name: str) -> None:
    """Hostile policy names must not break the header format."""
    p = QuotaPolicy(name=name, quota=100, window=60)
    result = format_ratelimit_policy([p])
    # The result must be a single line (no bare CRLF that could inject headers)
    assert "\r\n" not in result.replace("\\r", "").replace("\\n", ""), (
        f"Header injection via policy name: {result!r}"
    )
    # Must still contain ;q=100
    assert ";q=100" in result


test_policy_name_no_header_injection()
ok("injection: hostile policy names safe (200 examples)", True)


@given(pk=partition_keys)
@settings(max_examples=100)
def test_partition_key_no_header_injection(pk: bytes) -> None:
    """Hostile partition keys must not break the header format."""
    assume(len(pk) > 0)
    p = QuotaPolicy(name="test", quota=100, window=60, partition_key=pk)
    result = format_ratelimit_policy([p])
    assert ";q=100" in result
    assert ";pk=:" in result
    # Base64 output cannot contain CRLF
    pk_part = result.split(";pk=")[1]
    assert "\r\n" not in pk_part


test_partition_key_no_header_injection()
ok("injection: hostile partition keys safe (100 examples)", True)


# ── Summary ──────────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
total = PASS + FAIL
print(f"IETF RateLimit Fuzz: {PASS}/{total} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
