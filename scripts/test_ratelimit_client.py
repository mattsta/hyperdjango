"""
Tests for IETF rate limit client (ratelimit_client.py).

Covers:
- Header parsing: RateLimit-Policy, RateLimit, Retry-After
- State tracking: per-policy remaining/reset
- Wait time computation: blocked, exhausted, pacing
- Legacy header fallback
- Problem Details parsing
- Multiple policies (pick most restrictive)
- Partition key handling
- Jitter and max_wait caps
- Roundtrip: server formatters → client parsers
"""

# hyper-test: unit

import json
import sys

sys.path.insert(0, ".")

from hyperdjango.ratelimit import (
    PROBLEM_QUOTA_EXCEEDED,
    PROBLEM_TEMPORARY_REDUCED,
    QuotaPolicy,
    ServiceLimit,
    format_ratelimit,
    format_ratelimit_policy,
)
from hyperdjango.ratelimit_client import (
    RateLimitState,
    parse_problem_detail,
    parse_ratelimit,
    parse_ratelimit_policy,
    parse_retry_after,
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


# ─── A: parse_ratelimit_policy ────────────────────────────────────────────────

print("\n--- A: parse_ratelimit_policy ---")

# Single policy
policies = parse_ratelimit_policy('"default";q=100;w=60')
ok("parse single policy: count", len(policies) == 1)
ok("parse single policy: name", policies[0]["name"] == "default")
ok("parse single policy: q", policies[0]["q"] == 100)
ok("parse single policy: w", policies[0]["w"] == 60)
ok("parse single policy: qu default", policies[0]["qu"] == "requests")

# Multiple policies
multi = parse_ratelimit_policy('"burst";q=100;w=60, "daily";q=5000;w=86400')
ok("parse multi policy: count", len(multi) == 2)
ok("parse multi policy: first name", multi[0]["name"] == "burst")
ok("parse multi policy: second name", multi[1]["name"] == "daily")
ok("parse multi policy: second q", multi[1]["q"] == 5000)

# With quota unit
qu_pol = parse_ratelimit_policy('"bw";q=65535;qu="content-bytes";w=10')
ok("parse qu policy: qu", qu_pol[0]["qu"] == "content-bytes")

# With partition key
pk_pol = parse_ratelimit_policy('"peruser";q=100;w=60;pk=:dXNlcjo0Mg==:')
ok("parse pk policy: pk bytes", pk_pol[0]["pk"] == b"user:42")

# No window
no_w = parse_ratelimit_policy('"burst";q=50')
ok("parse no window: w=0", no_w[0]["w"] == 0)

# Empty string
ok("parse empty: empty list", parse_ratelimit_policy("") == [])
ok("parse whitespace: empty list", parse_ratelimit_policy("   ") == [])


# ─── B: parse_ratelimit ──────────────────────────────────────────────────────

print("\n--- B: parse_ratelimit ---")

# Single limit
limits = parse_ratelimit('"default";r=50;t=30')
ok("parse single limit: count", len(limits) == 1)
ok("parse single limit: name", limits[0]["policy_name"] == "default")
ok("parse single limit: r", limits[0]["r"] == 50)
ok("parse single limit: t", limits[0]["t"] == 30)

# No reset
no_t = parse_ratelimit('"default";r=99')
ok("parse no reset: t=0", no_t[0]["t"] == 0)

# Multiple limits
multi_lim = parse_ratelimit('"hourly";r=900;t=1800, "daily";r=100;t=36000')
ok("parse multi limit: count", len(multi_lim) == 2)
ok("parse multi limit: first r", multi_lim[0]["r"] == 900)
ok("parse multi limit: second r", multi_lim[1]["r"] == 100)

# Empty
ok("parse empty limit: empty list", parse_ratelimit("") == [])


# ─── C: parse_retry_after ─────────────────────────────────────────────────────

print("\n--- C: parse_retry_after ---")

ok("retry-after integer", parse_retry_after("30") == 30)
ok("retry-after zero", parse_retry_after("0") == 0)
ok("retry-after negative → 0", parse_retry_after("-5") == 0)
ok("retry-after empty → 0", parse_retry_after("") == 0)
ok("retry-after garbage → 0", parse_retry_after("not-a-number") == 0)


# ─── D: Roundtrip (server formatters → client parsers) ───────────────────────

print("\n--- D: Roundtrip ---")

# Format with server, parse with client
server_policies = [
    QuotaPolicy(name="api-v1", quota=1000, window=3600),
    QuotaPolicy(name="burst", quota=50, window=10),
]
server_limits = [
    ServiceLimit(policy_name="api-v1", remaining=742, reset=1800),
    ServiceLimit(policy_name="burst", remaining=48, reset=8),
]

policy_hdr = format_ratelimit_policy(server_policies)
limit_hdr = format_ratelimit(server_limits)

parsed_policies = parse_ratelimit_policy(policy_hdr)
parsed_limits = parse_ratelimit(limit_hdr)

ok("roundtrip: policy count", len(parsed_policies) == 2)
ok("roundtrip: policy api-v1 q", parsed_policies[0]["q"] == 1000)
ok("roundtrip: policy api-v1 w", parsed_policies[0]["w"] == 3600)
ok("roundtrip: policy burst q", parsed_policies[1]["q"] == 50)
ok("roundtrip: limit count", len(parsed_limits) == 2)
ok("roundtrip: limit api-v1 r", parsed_limits[0]["r"] == 742)
ok("roundtrip: limit api-v1 t", parsed_limits[0]["t"] == 1800)
ok("roundtrip: limit burst r", parsed_limits[1]["r"] == 48)

# Roundtrip with partition key
pk_policy = QuotaPolicy(
    name="tenant", quota=500, window=60, partition_key=b"org:acme-corp"
)
pk_limit = ServiceLimit(
    policy_name="tenant", remaining=123, reset=45, partition_key=b"org:acme-corp"
)
pk_p_hdr = format_ratelimit_policy([pk_policy])
pk_l_hdr = format_ratelimit([pk_limit])
pp = parse_ratelimit_policy(pk_p_hdr)
pl = parse_ratelimit(pk_l_hdr)
ok("roundtrip pk: policy pk", pp[0]["pk"] == b"org:acme-corp")
ok("roundtrip pk: limit pk", pl[0]["pk"] == b"org:acme-corp")


# ─── E: RateLimitState — basic tracking ───────────────────────────────────────

print("\n--- E: RateLimitState basic ---")

state = RateLimitState(jitter_factor=0.0)  # disable jitter for deterministic tests

# 200 with plenty of remaining
state.update_from_headers(
    200,
    {
        "RateLimit-Policy": '"default";q=100;w=60',
        "RateLimit": '"default";r=95;t=55',
    },
)
ok("state: policy tracked", "default" in state.policies)
ok("state: remaining=95", state.policies["default"].remaining == 95)
ok("state: quota=100", state.policies["default"].quota == 100)
ok("state: should proceed", state.should_proceed())
ok("state: wait_time=0", state.wait_time() == 0.0)
ok("state: not rate limited", not state.is_rate_limited)


# ─── F: RateLimitState — exhausted policy ─────────────────────────────────────

print("\n--- F: RateLimitState exhausted ---")

state2 = RateLimitState(jitter_factor=0.0)
state2.update_from_headers(
    200,
    {
        "RateLimit-Policy": '"api";q=100;w=60',
        "RateLimit": '"api";r=0;t=30',
    },
)
ok("exhausted: remaining=0", state2.policies["api"].remaining == 0)
ok("exhausted: should not proceed", not state2.should_proceed())
ok("exhausted: wait_time > 0", state2.wait_time() > 0)
ok("exhausted: wait_time <= 30", state2.wait_time() <= 30.0)


# ─── G: RateLimitState — 429 with Retry-After ────────────────────────────────

print("\n--- G: RateLimitState 429 ---")

state3 = RateLimitState(jitter_factor=0.0)
state3.update_from_headers(
    429,
    {
        "Retry-After": "60",
        "RateLimit-Policy": '"default";q=100;w=60',
        "RateLimit": '"default";r=0;t=60',
    },
)
ok("429: is_rate_limited", state3.is_rate_limited)
ok("429: should not proceed", not state3.should_proceed())
ok("429: wait_time > 0", state3.wait_time() > 0)
ok("429: wait_time <= 60", state3.wait_time() <= 60.0)


# ─── H: RateLimitState — 503 service unavailable ─────────────────────────────

print("\n--- H: RateLimitState 503 ---")

state4 = RateLimitState(jitter_factor=0.0)
state4.update_from_headers(
    503,
    {
        "Retry-After": "120",
    },
)
ok("503: is_service_unavailable", state4.is_service_unavailable)
ok("503: should not proceed", not state4.should_proceed())
ok("503: wait_time > 0", state4.wait_time() > 0)


# ─── I: RateLimitState — multiple policies ────────────────────────────────────

print("\n--- I: Multiple policies ---")

state5 = RateLimitState(jitter_factor=0.0)
state5.update_from_headers(
    200,
    {
        "RateLimit-Policy": '"hourly";q=1000;w=3600, "daily";q=5000;w=86400',
        "RateLimit": '"hourly";r=500;t=1800, "daily";r=10;t=36000',
    },
)
ok("multi: two policies tracked", len(state5.policies) == 2)
ok("multi: hourly remaining", state5.policies["hourly"].remaining == 500)
ok("multi: daily remaining", state5.policies["daily"].remaining == 10)
most = state5.most_restrictive_policy()
ok("multi: most restrictive is daily", most is not None and most.name == "daily")
ok("multi: should proceed (daily r=10 > 0)", state5.should_proceed())

# Now daily hits 0
state5.update_from_headers(
    200,
    {
        "RateLimit": '"hourly";r=499;t=1799, "daily";r=0;t=35999',
    },
)
ok("multi exhausted: should not proceed", not state5.should_proceed())
ok("multi exhausted: wait > 0", state5.wait_time() > 0)


# ─── J: RateLimitState — max_wait cap ────────────────────────────────────────

print("\n--- J: Max wait cap ---")

state6 = RateLimitState(jitter_factor=0.0, max_wait=10.0)
state6.update_from_headers(429, {"Retry-After": "99999"})
ok("max_wait: capped", state6.wait_time() <= 10.0)


# ─── K: RateLimitState — jitter ──────────────────────────────────────────────

print("\n--- K: Jitter ---")

state7 = RateLimitState(jitter_factor=0.2)
state7.update_from_headers(
    429,
    {
        "Retry-After": "30",
        "RateLimit": '"default";r=0;t=30',
    },
)
# With 20% jitter, wait should be in [30, 36]
wait = state7.wait_time()
ok("jitter: wait >= 30", wait >= 30.0)
ok("jitter: wait <= 36", wait <= 36.0)


# ─── L: RateLimitState — legacy header fallback ──────────────────────────────

print("\n--- L: Legacy fallback ---")

state8 = RateLimitState(jitter_factor=0.0)
state8.update_from_headers(
    200,
    {
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "42",
        "X-RateLimit-Reset": "30",
    },
)
ok("legacy: policy created", "default" in state8.policies)
ok("legacy: quota=100", state8.policies["default"].quota == 100)
ok("legacy: remaining=42", state8.policies["default"].remaining == 42)
ok("legacy: should proceed", state8.should_proceed())


# ─── M: RateLimitState — pace_interval ────────────────────────────────────────

print("\n--- M: Pace interval ---")

state9 = RateLimitState(jitter_factor=0.0)
state9.update_from_headers(
    200,
    {
        "RateLimit-Policy": '"api";q=100;w=60',
        "RateLimit": '"api";r=50;t=50',
    },
)
interval = state9.pace_interval()
ok("pace: interval > 0", interval > 0)
# 50 remaining in 50 seconds → 1 req/sec → interval ~1.0
ok("pace: interval ~1.0", 0.5 < interval < 1.5)


# ─── N: RateLimitState — reset clears all ─────────────────────────────────────

print("\n--- N: Reset ---")

state10 = RateLimitState()
state10.update_from_headers(429, {"Retry-After": "60", "RateLimit": '"x";r=0;t=60'})
ok("pre-reset: blocked", not state10.should_proceed())
state10.reset()
ok("post-reset: unblocked", state10.should_proceed())
ok("post-reset: no policies", len(state10.policies) == 0)


# ─── O: parse_problem_detail ─────────────────────────────────────────────────

print("\n--- O: Problem Details parsing ---")

pd_json = json.dumps(
    {
        "type": PROBLEM_QUOTA_EXCEEDED,
        "title": "Rate limit exceeded",
        "status": 429,
        "detail": "Quota exceeded for policy default",
        "violated-policies": ["default", "burst"],
        "retry_after": 30,
    }
)

pd = parse_problem_detail(pd_json)
ok("pd parse: type", pd.problem_type == PROBLEM_QUOTA_EXCEEDED)
ok("pd parse: title", pd.title == "Rate limit exceeded")
ok("pd parse: status", pd.status == 429)
ok("pd parse: detail", "default" in pd.detail)
ok("pd parse: violated", pd.violated_policies == ["default", "burst"])
ok("pd parse: retry_after", pd.retry_after == 30)

# Parse from dict
pd2 = parse_problem_detail(
    {"type": PROBLEM_TEMPORARY_REDUCED, "status": 503, "title": "Slow down"}
)
ok("pd dict: type", pd2.problem_type == PROBLEM_TEMPORARY_REDUCED)
ok("pd dict: status", pd2.status == 503)

# Parse invalid
pd3 = parse_problem_detail("not json at all {{{")
ok("pd invalid: empty problem", pd3.problem_type == "")

pd4 = parse_problem_detail(b'{"type":"test"}')
ok("pd bytes: parsed", pd4.problem_type == "test")


# ─── P: Partition key routing ─────────────────────────────────────────────────

print("\n--- P: Partition key routing ---")

state11 = RateLimitState(jitter_factor=0.0)
# Server sends partition key
state11.update_from_headers(
    200,
    {
        "RateLimit-Policy": '"peruser";q=100;w=60;pk=:dXNlcjo0Mg==:',
        "RateLimit": '"peruser";r=80;t=45;pk=:dXNlcjo0Mg==:',
    },
)
ok("pk: policy tracked", "peruser" in state11.policies)
ok("pk: partition_key stored", state11.policies["peruser"].partition_key == b"user:42")
ok("pk: remaining=80", state11.policies["peruser"].remaining == 80)


# ─── Summary ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
total = PASS + FAIL
print(f"RateLimit Client: {PASS}/{total} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
