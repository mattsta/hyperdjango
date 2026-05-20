# Rate Limit Client

IETF-compliant rate limit client that parses `RateLimit-Policy` and `RateLimit` response headers (draft-ietf-httpapi-ratelimit-headers-10) and implements proper backoff.

## Quick Start

```python
from hyperdjango.ratelimit_client import RateLimitState

state = RateLimitState()

# After each HTTP response:
state.update_from_headers(response.status, response.headers)

# Before each request:
if not state.should_proceed():
    await asyncio.sleep(state.wait_time())
```

## RateLimitState

Tracks per-policy quota state from response headers. Computes wait times with jittered backoff.

```python
from hyperdjango.ratelimit_client import RateLimitState

state = RateLimitState(
    max_wait=300.0,  # cap wait time at 5 minutes (prevents DoS via huge reset)
    jitter_factor=0.1,  # add 0-10% random jitter to prevent thundering herd
)
```

### Methods

| Method                                 | Returns     | Description                                             |
| -------------------------------------- | ----------- | ------------------------------------------------------- |
| `update_from_headers(status, headers)` | None        | Parse IETF + legacy headers, track per-policy state     |
| `wait_time()`                          | float       | Seconds to wait before next request (0.0 = proceed)     |
| `should_proceed()`                     | bool        | True if no waiting needed (fast check, no jitter)       |
| `pace_interval()`                      | float       | Optimal seconds between requests to avoid hitting limit |
| `most_restrictive_policy()`            | PolicyState | Policy with lowest remaining quota                      |
| `reset()`                              | None        | Clear all tracked state                                 |

### Properties

| Property                 | Type                   | Description              |
| ------------------------ | ---------------------- | ------------------------ |
| `is_rate_limited`        | bool                   | Last response was 429    |
| `is_service_unavailable` | bool                   | Last response was 503    |
| `policies`               | dict[str, PolicyState] | Per-policy tracked state |

### How It Works

1. **IETF headers parsed first**: `RateLimit-Policy` updates quota/window definitions, `RateLimit` updates remaining/reset per policy
2. **Retry-After respected**: On 429/503, sets global block until retry-after expires
3. **Legacy fallback**: If no IETF headers present, parses `x-ratelimit-*` headers into a "default" policy
4. **Wait computation**: Blocked by Retry-After > exhausted policies (remaining=0) > proceed

## Parsers

### parse_ratelimit_policy

```python
from hyperdjango.ratelimit_client import parse_ratelimit_policy

policies = parse_ratelimit_policy('"burst";q=100;w=60, "daily";q=5000;w=86400')
# [{"name": "burst", "q": 100, "w": 60, "qu": "requests", "pk": b""},
#  {"name": "daily", "q": 5000, "w": 86400, "qu": "requests", "pk": b""}]
```

### parse_ratelimit

```python
from hyperdjango.ratelimit_client import parse_ratelimit

limits = parse_ratelimit('"burst";r=50;t=30')
# [{"policy_name": "burst", "r": 50, "t": 30, "pk": b""}]
```

### parse_retry_after

```python
from hyperdjango.ratelimit_client import parse_retry_after

seconds = parse_retry_after("30")  # 30
seconds = parse_retry_after("0")  # 0
seconds = parse_retry_after("garbage")  # 0 (unparseable)
```

## Problem Details Parser

Parse RFC 9457 Problem Details from 429/503 response bodies.

```python
from hyperdjango.ratelimit_client import parse_problem_detail

problem = parse_problem_detail(response_body)
print(
    problem.problem_type
)  # "https://iana.org/assignments/http-problem-types#quota-exceeded"
print(problem.violated_policies)  # ["burst", "daily"]
print(problem.retry_after)  # 30
```

## Adaptive Pacing

Use `pace_interval()` to spread requests evenly over the reset window instead of bursting.

```python
state = RateLimitState()

# After response with: RateLimit: "api";r=50;t=50
state.update_from_headers(200, headers)

interval = state.pace_interval()  # ~1.0 second (50 remaining / 50 seconds)

# Use in a loop:
for item in items:
    await asyncio.sleep(interval)
    response = await client.post("/api/process", json=item)
    state.update_from_headers(response.status, response.headers)
    interval = state.pace_interval()  # recalculate after each response
```

## Multiple Policies

Servers can advertise multiple policies (e.g., per-minute + per-day). The client tracks all of them independently.

```python
# Server returns:
# RateLimit-Policy: "minute";q=100;w=60, "daily";q=5000;w=86400
# RateLimit: "minute";r=80;t=45, "daily";r=100;t=36000

state.update_from_headers(200, headers)

# Both policies tracked
print(len(state.policies))  # 2

# Most restrictive policy (daily, only 100 remaining)
most = state.most_restrictive_policy()
print(most.name, most.remaining)  # "daily" 100

# wait_time considers ALL exhausted policies
# If daily hits 0, wait_time returns the daily reset time
```

## Partition Keys

Servers can include partition keys to identify per-user or per-tenant quotas.

```python
# Server returns:
# RateLimit: "peruser";r=80;t=45;pk=:dXNlcjo0Mg==:

state.update_from_headers(200, headers)
print(state.policies["peruser"].partition_key)  # b"user:42"
```

## Settings

The server-side IETF headers are controlled by these settings:

| Setting                     | Default | Description                                   |
| --------------------------- | ------- | --------------------------------------------- |
| `RATELIMIT_IETF_HEADERS`    | True    | Emit `RateLimit` + `RateLimit-Policy` headers |
| `RATELIMIT_LEGACY_HEADERS`  | True    | Emit `x-ratelimit-*` headers                  |
| `RATELIMIT_PROBLEM_DETAILS` | True    | RFC 9457 Problem Details JSON on 429          |

## Related

- [Rate Limiting (server-side)](ratelimit.md) — backends, middleware, tiered/rule-based limiting
- [Settings Reference](settings.md) — all rate limit settings
- [Tuning](tuning.md) — rate limit cleanup and header configuration
