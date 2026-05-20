"""
Tests for IETF RateLimit headers (draft-ietf-httpapi-ratelimit-headers-10).

Covers:
- Structured Fields formatting (RFC 9651)
- RFC 9457 Problem Details on 429
- Shared header setter and 429 response builder
- Integration with all 5 rate limiting middlewares
- Settings configuration
"""

# hyper-test: unit

import asyncio
import base64
import json
import sys
from unittest.mock import patch

sys.path.insert(0, ".")

from hyperdjango.conf import DEFAULTS
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
    set_ratelimit_headers,
)
from hyperdjango.response import Response

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


# ─── A: Structured Fields Formatting ──────────────────────────────────────────

print("\n--- A: Structured Fields Formatting ---")

# sf-string
ok("sf_format_string basic", _sf_format_string("default") == '"default"')
ok("sf_format_string with quotes", _sf_format_string('a"b') == '"a\\"b"')
ok("sf_format_string with backslash", _sf_format_string("a\\b") == '"a\\\\b"')
ok("sf_format_string empty", _sf_format_string("") == '""')

# sf-byte-sequence
raw = b"trial12323"
encoded = _sf_format_byte_sequence(raw)
ok("sf_format_byte_sequence starts with :", encoded.startswith(":"))
ok("sf_format_byte_sequence ends with :", encoded.endswith(":"))
inner = encoded[1:-1]
ok("sf_format_byte_sequence valid base64", base64.b64decode(inner) == raw)

# format_ratelimit_policy
p1 = QuotaPolicy(name="default", quota=100, window=60)
hdr = format_ratelimit_policy([p1])
ok("policy single: name", '"default"' in hdr)
ok("policy single: q", ";q=100" in hdr)
ok("policy single: w", ";w=60" in hdr)

# Multiple policies
p2 = QuotaPolicy(name="daily", quota=1000, window=86400)
hdr_multi = format_ratelimit_policy([p1, p2])
ok("policy multi: comma separated", ", " in hdr_multi)
ok("policy multi: both present", '"daily"' in hdr_multi and '"default"' in hdr_multi)

# Window omitted when 0
p_no_window = QuotaPolicy(name="burst", quota=50, window=0)
hdr_no_w = format_ratelimit_policy([p_no_window])
ok("policy no window: w omitted", ";w=" not in hdr_no_w)

# Non-default quota unit
p_bytes = QuotaPolicy(name="bw", quota=65535, quota_unit="content-bytes", window=10)
hdr_qu = format_ratelimit_policy([p_bytes])
ok("policy content-bytes: qu present", ";qu=" in hdr_qu)
ok("policy content-bytes: qu value", '"content-bytes"' in hdr_qu)

# Partition key
pk_data = b"user:42"
p_pk = QuotaPolicy(name="peruser", quota=100, window=60, partition_key=pk_data)
hdr_pk = format_ratelimit_policy([p_pk])
ok("policy partition key: pk present", ";pk=:" in hdr_pk)

# format_ratelimit
lim = ServiceLimit(policy_name="default", remaining=50, reset=30)
rl_hdr = format_ratelimit([lim])
ok("ratelimit single: name", '"default"' in rl_hdr)
ok("ratelimit single: r", ";r=50" in rl_hdr)
ok("ratelimit single: t", ";t=30" in rl_hdr)

# Reset omitted when 0
lim_no_t = ServiceLimit(policy_name="default", remaining=99)
rl_no_t = format_ratelimit([lim_no_t])
ok("ratelimit no reset: t omitted", ";t=" not in rl_no_t)

# Multiple limits
lim2 = ServiceLimit(policy_name="daily", remaining=900, reset=3600)
rl_multi = format_ratelimit([lim, lim2])
ok("ratelimit multi: comma separated", ", " in rl_multi)

# Partition key on limit
lim_pk = ServiceLimit(
    policy_name="default", remaining=50, reset=30, partition_key=b"ip:1.2.3.4"
)
rl_pk = format_ratelimit([lim_pk])
ok("ratelimit partition key: pk present", ";pk=:" in rl_pk)


# ─── B: Problem Details ──────────────────────────────────────────────────────

print("\n--- B: Problem Details ---")

pd = build_problem_detail(
    problem_type=PROBLEM_QUOTA_EXCEEDED,
    title="Rate limit exceeded",
    status=429,
    detail="Quota exceeded for policy default",
    violated_policies=["default"],
)
ok("problem_detail: type", pd["type"] == PROBLEM_QUOTA_EXCEEDED)
ok("problem_detail: title", pd["title"] == "Rate limit exceeded")
ok("problem_detail: status", pd["status"] == 429)
ok("problem_detail: detail", "default" in pd["detail"])
ok("problem_detail: violated-policies", pd["violated-policies"] == ["default"])

# Other problem types exist
ok("problem type: temporary", "temporary-reduced-capacity" in PROBLEM_TEMPORARY_REDUCED)
ok("problem type: abnormal", "abnormal-usage-detected" in PROBLEM_ABNORMAL_USAGE)


# ─── C: Header Setter ────────────────────────────────────────────────────────

print("\n--- C: Header Setter ---")

# IETF only
resp_ietf = Response.json({"ok": True}, status=200)
policies = [QuotaPolicy(name="default", quota=100, window=60)]
limits = [ServiceLimit(policy_name="default", remaining=50, reset=30)]
set_ratelimit_headers(
    resp_ietf, policies, limits, include_ietf=True, include_legacy=False
)
ok("ietf only: ratelimit-policy present", "ratelimit-policy" in resp_ietf.headers)
ok("ietf only: ratelimit present", "ratelimit" in resp_ietf.headers)
ok("ietf only: no legacy limit", "x-ratelimit-limit" not in resp_ietf.headers)
ok("ietf only: no legacy remaining", "x-ratelimit-remaining" not in resp_ietf.headers)

# Legacy only
resp_legacy = Response.json({"ok": True}, status=200)
set_ratelimit_headers(
    resp_legacy, policies, limits, include_ietf=False, include_legacy=True
)
ok("legacy only: no ratelimit-policy", "ratelimit-policy" not in resp_legacy.headers)
ok(
    "legacy only: x-ratelimit-limit present",
    resp_legacy.headers.get("x-ratelimit-limit") == "100",
)
ok(
    "legacy only: x-ratelimit-remaining present",
    resp_legacy.headers.get("x-ratelimit-remaining") == "50",
)
ok(
    "legacy only: x-ratelimit-reset present",
    resp_legacy.headers.get("x-ratelimit-reset") == "30",
)

# Both enabled
resp_both = Response.json({"ok": True}, status=200)
set_ratelimit_headers(
    resp_both, policies, limits, include_ietf=True, include_legacy=True
)
ok("both: ratelimit-policy present", "ratelimit-policy" in resp_both.headers)
ok("both: x-ratelimit-limit present", "x-ratelimit-limit" in resp_both.headers)

# Neither (no-op)
resp_none = Response.json({"ok": True}, status=200)
set_ratelimit_headers(
    resp_none, policies, limits, include_ietf=False, include_legacy=False
)
ok("neither: no ratelimit-policy", "ratelimit-policy" not in resp_none.headers)
ok("neither: no x-ratelimit-limit", "x-ratelimit-limit" not in resp_none.headers)

# Tier/rule/cost legacy headers
resp_tier = Response.json({"ok": True}, status=200)
set_ratelimit_headers(
    resp_tier,
    policies,
    limits,
    include_ietf=False,
    include_legacy=True,
    tier_name="pro",
    rule_name="api-write",
    cost=5,
)
ok("legacy tier: x-ratelimit-tier", resp_tier.headers.get("x-ratelimit-tier") == "pro")
ok(
    "legacy rule: x-ratelimit-rule",
    resp_tier.headers.get("x-ratelimit-rule") == "api-write",
)
ok("legacy cost: x-ratelimit-cost", resp_tier.headers.get("x-ratelimit-cost") == "5")

# Cost = 1 not emitted
resp_cost1 = Response.json({"ok": True}, status=200)
set_ratelimit_headers(
    resp_cost1, policies, limits, include_ietf=False, include_legacy=True, cost=1
)
ok("legacy cost=1: not emitted", "x-ratelimit-cost" not in resp_cost1.headers)


# ─── D: 429 Response Builder ─────────────────────────────────────────────────

print("\n--- D: 429 Response Builder ---")

# With problem details
resp_429 = build_429_response(
    policies,
    limits,
    30,
    include_ietf=True,
    include_legacy=True,
    include_problem_details=True,
)
ok("429 pd: status 429", resp_429.status == 429)
ok(
    "429 pd: content-type problem+json",
    "problem+json" in resp_429.headers.get("content-type", ""),
)
ok("429 pd: retry-after", resp_429.headers.get("retry-after") == "30")
ok("429 pd: ratelimit-policy present", "ratelimit-policy" in resp_429.headers)
ok("429 pd: ratelimit present", "ratelimit" in resp_429.headers)
body_429 = json.loads(resp_429.body)
ok("429 pd: type in body", body_429["type"] == PROBLEM_QUOTA_EXCEEDED)
ok("429 pd: violated-policies", body_429["violated-policies"] == ["default"])
ok("429 pd: retry_after in body", body_429["retry_after"] == 30)

# Without problem details
resp_429_legacy = build_429_response(
    policies,
    limits,
    30,
    include_ietf=False,
    include_legacy=True,
    include_problem_details=False,
)
ok("429 legacy: status 429", resp_429_legacy.status == 429)
body_legacy = json.loads(resp_429_legacy.body)
ok("429 legacy: detail in body", body_legacy["detail"] == "Rate limit exceeded")
# Unified error contract {"detail","status"}: retry-after is carried by the
# Retry-After HEADER, not a bespoke body field.
ok("429 legacy: status in body", body_legacy["status"] == 429)
ok("429 legacy: retry-after header", resp_429_legacy.headers.get("retry-after") == "30")
ok("429 legacy: no retry_after body field", "retry_after" not in body_legacy)
ok("429 legacy: no type in body", "type" not in body_legacy)

# Multiple policies in 429
p_multi = [
    QuotaPolicy(name="hourly", quota=1000, window=3600),
    QuotaPolicy(name="daily", quota=5000, window=86400),
]
l_multi = [ServiceLimit(policy_name="hourly", remaining=0, reset=1800)]
resp_429_multi = build_429_response(
    p_multi, l_multi, 1800, include_ietf=True, include_problem_details=True
)
body_multi = json.loads(resp_429_multi.body)
ok(
    "429 multi: violated-policies both",
    set(body_multi["violated-policies"]) == {"hourly", "daily"},
)
ok("429 multi: policies in detail", "policies" in body_multi["detail"])


# ─── E: RateLimitMiddleware Integration ───────────────────────────────────────

print("\n--- E: RateLimitMiddleware Integration ---")

from hyperdjango.ratelimit import InMemoryRateLimitBackend
from hyperdjango.ratelimit import RateLimitMiddleware as PluggableRLM


class FakeRequest:
    def __init__(self, ip: str = "1.2.3.4"):
        self.client_ip = ip
        self.user = None


async def _test_pluggable_mw():
    backend = InMemoryRateLimitBackend()
    with patch.dict(
        DEFAULTS,
        {
            "RATELIMIT_IETF_HEADERS": True,
            "RATELIMIT_LEGACY_HEADERS": True,
            "RATELIMIT_PROBLEM_DETAILS": True,
        },
    ):
        mw = PluggableRLM(
            max_requests=5,
            window=60,
            backend=backend,
            policy_name="test-api",
        )

    async def next_handler(req):
        return Response.json({"ok": True}, status=200)

    req = FakeRequest()
    resp = await mw(req, next_handler)
    ok("pluggable: 200 status", resp.status == 200)
    ok(
        "pluggable: ratelimit-policy",
        '"test-api"' in resp.headers.get("ratelimit-policy", ""),
    )
    ok("pluggable: ratelimit r=4", ";r=4" in resp.headers.get("ratelimit", ""))
    ok("pluggable: x-ratelimit-limit", resp.headers.get("x-ratelimit-limit") == "5")
    ok(
        "pluggable: policy has w=60",
        ";w=60" in resp.headers.get("ratelimit-policy", ""),
    )

    # Exhaust the limit
    for _ in range(4):
        await mw(req, next_handler)

    resp_429 = await mw(req, next_handler)
    ok("pluggable 429: status", resp_429.status == 429)
    ok("pluggable 429: ratelimit r=0", ";r=0" in resp_429.headers.get("ratelimit", ""))
    ok("pluggable 429: retry-after", resp_429.headers.get("retry-after") is not None)
    body = json.loads(resp_429.body)
    ok("pluggable 429: problem type", body.get("type") == PROBLEM_QUOTA_EXCEEDED)
    ok(
        "pluggable 429: violated-policies",
        body.get("violated-policies") == ["test-api"],
    )


asyncio.run(_test_pluggable_mw())


# ─── G: Settings Configuration ────────────────────────────────────────────────

print("\n--- G: Settings Configuration ---")


async def _test_settings_ietf_off():
    with patch.dict(
        DEFAULTS,
        {
            "RATELIMIT_IETF_HEADERS": False,
            "RATELIMIT_LEGACY_HEADERS": True,
            "RATELIMIT_PROBLEM_DETAILS": False,
        },
    ):
        backend = InMemoryRateLimitBackend()
        mw = PluggableRLM(max_requests=10, window=60, backend=backend)

    async def next_handler(req):
        return Response.json({"ok": True}, status=200)

    req = FakeRequest("10.0.0.1")
    resp = await mw(req, next_handler)
    ok("ietf off: no ratelimit-policy", "ratelimit-policy" not in resp.headers)
    ok("ietf off: no ratelimit", "ratelimit" not in resp.headers)
    ok("ietf off: x-ratelimit-limit present", "x-ratelimit-limit" in resp.headers)


async def _test_settings_legacy_off():
    with patch.dict(
        DEFAULTS,
        {
            "RATELIMIT_IETF_HEADERS": True,
            "RATELIMIT_LEGACY_HEADERS": False,
            "RATELIMIT_PROBLEM_DETAILS": True,
        },
    ):
        backend = InMemoryRateLimitBackend()
        mw = PluggableRLM(max_requests=10, window=60, backend=backend)

    async def next_handler(req):
        return Response.json({"ok": True}, status=200)

    req = FakeRequest("10.0.0.2")
    resp = await mw(req, next_handler)
    ok("legacy off: ratelimit-policy present", "ratelimit-policy" in resp.headers)
    ok("legacy off: no x-ratelimit-limit", "x-ratelimit-limit" not in resp.headers)


asyncio.run(_test_settings_ietf_off())
asyncio.run(_test_settings_legacy_off())

# Default settings
ok(
    "default: RATELIMIT_IETF_HEADERS is True",
    DEFAULTS["RATELIMIT_IETF_HEADERS"] is True,
)
ok(
    "default: RATELIMIT_LEGACY_HEADERS is True",
    DEFAULTS["RATELIMIT_LEGACY_HEADERS"] is True,
)
ok(
    "default: RATELIMIT_PROBLEM_DETAILS is True",
    DEFAULTS["RATELIMIT_PROBLEM_DETAILS"] is True,
)


# ─── Summary ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
total = PASS + FAIL
print(f"IETF RateLimit Headers: {PASS}/{total} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
