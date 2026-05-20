# Rate Limiting

Pluggable rate limiter with multi-server coordination, multi-tenant support, tiered limits per RBAC group, and per-path/method/cost rule-based limiting.

## Quick Start

```python
from hyperdjango.ratelimit import RateLimitMiddleware

# 100 requests per minute per IP (default)
app.use(RateLimitMiddleware(max_requests=100, window=60))
```

## Backends

### InMemoryRateLimitBackend (Default)

Per-process windowed token bucket. Fast, single-server only. State lost on restart. Thread-safe and sharded — buckets and locks are split 16 ways by `hash(key)` so unrelated keys never contend on one lock.

```python
# This is the default -- no configuration needed
app.use(RateLimitMiddleware(max_requests=100, window=60))
```

The in-memory backend keeps four numbers per key (token level, last-refill time, fixed-window index, admissions-this-window) — O(1) memory and O(1) per check, versus the old design that stored every request timestamp. It enforces two limits together: a hard per-window admission cap (never more than `max_requests` units in any fixed `window`-second window) and a continuously-refilling token bucket that smooths bursts within a window. This guarantees at most `max_requests` admissions per fixed window; a burst straddling a window boundary can admit up to `max_requests` in each of the two adjacent windows, the standard fixed-window property.

Methods:

| Method                                                        | Description                                                                       |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `check_and_increment(key, max_requests, window, increment=1)` | Check limit and increment counter. Returns `(allowed, remaining, reset_seconds)`. |
| `reset(key)`                                                  | Reset rate limit for a key.                                                       |
| `cleanup()`                                                   | Remove all entries older than 1 hour.                                             |

### DatabaseRateLimitBackend (Multi-Server)

PostgreSQL UNLOGGED table for cross-server coordination. Uses fixed time windows (not sliding) for efficient SQL aggregation. UNLOGGED = no WAL overhead.

```python
from hyperdjango.ratelimit import RateLimitMiddleware, DatabaseRateLimitBackend

backend = DatabaseRateLimitBackend(db)
await backend.ensure_table()

app.use(RateLimitMiddleware(max_requests=100, window=60, backend=backend))
```

The database backend creates an UNLOGGED table:

```sql
CREATE UNLOGGED TABLE IF NOT EXISTS hyper_rate_limits (
    key VARCHAR(255) NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (key, window_start)
)
```

Uses PostgreSQL `INSERT ON CONFLICT` for atomic upsert. Each `(key, window_start)` pair tracks a count. Window start is truncated to the second.

Methods:

| Method                                                        | Description                                                           |
| ------------------------------------------------------------- | --------------------------------------------------------------------- |
| `ensure_table()`                                              | Create the UNLOGGED table and index.                                  |
| `check_and_increment(key, max_requests, window, increment=1)` | Atomic check + upsert. Returns `(allowed, remaining, reset_seconds)`. |
| `reset(key)`                                                  | Delete all entries for a key.                                         |
| `cleanup()`                                                   | Remove entries older than 1 hour.                                     |
| `get_usage(key, window)`                                      | Get current usage stats: `{"count": int, "first_request": datetime}`. |

## Key Functions

Key functions determine **what** gets rate limited. They receive the request and return a string key.

### `ip_key` (Default)

Rate limit by client IP address:

```python
# This is the default
app.use(RateLimitMiddleware(max_requests=100, window=60))
# Equivalent to:
app.use(RateLimitMiddleware(max_requests=100, window=60, key_func=ip_key))
```

Key format: `ip:192.168.1.1`

### `user_key`

Rate limit by authenticated user ID. Falls back to `ip_key` for unauthenticated requests:

```python
from hyperdjango.ratelimit import RateLimitMiddleware, user_key

app.use(RateLimitMiddleware(max_requests=100, window=60, key_func=user_key))
```

Key format: `user:42`. Looks for `id`, `user_id`, or `username` on `request.user` (supports both dict and object).

### `org_key`

Rate limit by organization (multi-tenant). The entire organization shares one rate limit pool. When the org hits its limit, **all users in that org** are blocked:

```python
from hyperdjango.ratelimit import RateLimitMiddleware, org_key

app.use(
    RateLimitMiddleware(
        max_requests=5000,
        window=3600,  # 5K requests per hour per org
        key_func=org_key,
    )
)
```

Key format: `org:5`. Looks for `org_id`, `organization_id`, or `tenant_id` on `request.user`. Falls back to `user_key` if no org found, then to `ip_key` if unauthenticated.

### `composite_key`

Combine multiple key strategies into one:

```python
from hyperdjango.ratelimit import composite_key, org_key, user_key

# Rate limit per user WITHIN each org
app.use(
    RateLimitMiddleware(
        max_requests=100,
        window=60,
        key_func=composite_key(org_key, user_key),
    )
)
```

Key format: `org:5:user:42`. Joins the results of each function with `:`.

### Custom Key Functions

Rate limit by any attribute:

```python
def plan_key(request):
    """Rate limit by billing plan."""
    if request.user and isinstance(request.user, dict):
        return f"plan:{request.user.get('plan', 'free')}"
    return f"ip:{request.client_ip}"


app.use(RateLimitMiddleware(max_requests=1000, window=60, key_func=plan_key))
```

A key function must accept a request and return a string. The string is used as the rate limit bucket identifier.

## RateLimitMiddleware

The base rate limiter middleware.

### Parameters

| Parameter      | Type                                                   | Default                      | Description                                     |
| -------------- | ------------------------------------------------------ | ---------------------------- | ----------------------------------------------- |
| `max_requests` | `int`                                                  | `100`                        | Maximum requests allowed per window             |
| `window`       | `int`                                                  | `60`                         | Window size in seconds                          |
| `key_func`     | `Callable`                                             | `ip_key`                     | Function to extract rate limit key from request |
| `backend`      | `InMemoryRateLimitBackend \| DatabaseRateLimitBackend` | `InMemoryRateLimitBackend()` | Storage backend                                 |

### Behavior

On each request, the middleware:

1. Calls `key_func(request)` to get the rate limit key
2. Calls `backend.check_and_increment(key, max_requests, window)` to check and increment
3. If **allowed**: calls the next middleware/handler, then adds rate limit headers to the response
4. If **denied**: returns HTTP 429 with rate limit headers and a JSON body

## Response Headers

Every response includes rate limit headers:

```
X-RateLimit-Limit: 100        # Max requests allowed in window
X-RateLimit-Remaining: 73     # Requests remaining in current window
X-RateLimit-Reset: 42         # Seconds until window resets
```

When rate limited (HTTP 429), the response includes:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 42
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 42
Content-Type: application/json

{"detail": "Rate limit exceeded", "retry_after": 42}
```

The tiered and rule-based middlewares add additional headers:

```
X-RateLimit-Tier: pro          # User's resolved tier (tiered/rule-based only)
X-RateLimit-Rule: heavy-report # Matched rule name (rule-based only)
X-RateLimit-Cost: 5            # Cost of this request (rule-based, when > 1)
```

## Hierarchical Rate Limits

Stack multiple rate limiters for layered protection. Each middleware runs independently:

```python
from hyperdjango.ratelimit import (
    RateLimitMiddleware,
    DatabaseRateLimitBackend,
    ip_key,
    user_key,
    org_key,
)

# Layer 1: Anti-DDoS -- 10 requests/second per IP
app.use(RateLimitMiddleware(max_requests=10, window=1))

# Layer 2: Anti-abuse -- 100 requests/minute per user
app.use(RateLimitMiddleware(max_requests=100, window=60, key_func=user_key))

# Layer 3: Billing tier -- 5K requests/hour per organization
app.use(
    RateLimitMiddleware(
        max_requests=5000,
        window=3600,
        key_func=org_key,
        backend=DatabaseRateLimitBackend(db),  # multi-server coordination
    )
)
```

The first middleware that denies the request short-circuits the chain.

## TieredRateLimitMiddleware

Per-group tiered rate limiting. Each RBAC group can have a `rate_limit_tier` field (e.g., `"free"`, `"pro"`, `"enterprise"`). The middleware resolves the user's highest-priority group tier and applies the corresponding limit.

### Setup

```python
from hyperdjango.ratelimit import TieredRateLimitMiddleware

tiers = {
    "free": {"max_requests": 100, "window": 60},
    "pro": {"max_requests": 1000, "window": 60},
    "enterprise": {"max_requests": 10000, "window": 60},
}

mw = TieredRateLimitMiddleware(tiers=tiers, default_tier="free", db=db)
await mw.ensure_column()  # Adds rate_limit_tier column to hyper_groups

app.use(mw)
```

### Parameters

| Parameter      | Type                        | Default                      | Description                                                    |
| -------------- | --------------------------- | ---------------------------- | -------------------------------------------------------------- |
| `tiers`        | `dict[str, dict[str, int]]` | required                     | Tier definitions: `{"name": {"max_requests": N, "window": S}}` |
| `default_tier` | `str`                       | `"free"`                     | Fallback tier for anonymous users or users without a tier      |
| `db`           | database                    | `None`                       | Database connection for tier resolution                        |
| `backend`      | backend                     | `InMemoryRateLimitBackend()` | Storage backend for counters                                   |
| `key_func`     | `Callable`                  | `user_key`                   | Key extraction function                                        |

### Tier Resolution

1. Get `user_id` from `request.user`
2. Query `hyper_groups` via `hyper_user_groups` join, filtering for groups with a non-empty `rate_limit_tier`, ordered by `priority DESC`
3. Take the highest-priority group's tier
4. Cache the result per-process (cleared on restart or via `clear_tier_cache()`)

```sql
SELECT g.rate_limit_tier FROM hyper_groups g
JOIN hyper_user_groups ug ON g.id = ug.group_id
WHERE ug.user_id = $1 AND g.rate_limit_tier != ''
ORDER BY g.priority DESC LIMIT 1
```

### Cache Management

```python
mw.clear_tier_cache()  # Clear all cached tiers
mw.clear_tier_cache(user_id=42)  # Clear tier for specific user
```

Call `clear_tier_cache()` when a user's group membership or a group's tier assignment changes.

### Database Column

`ensure_column()` adds the `rate_limit_tier` column to the `hyper_groups` table:

```sql
ALTER TABLE hyper_groups ADD COLUMN IF NOT EXISTS rate_limit_tier VARCHAR(50) DEFAULT ''
```

Set a group's tier via SQL or the admin UI:

```sql
UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE name = 'paid_users';
```

## RuleBasedRateLimitMiddleware

Multi-dimensional rate limiting with per-path, per-method, per-tier rules. Supports cost-based rate limiting where expensive endpoints consume more quota units per request.

### Setup

```python
from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

tiers = {
    "free": {"max_requests": 100, "window": 60},
    "pro": {"max_requests": 1000, "window": 60},
    "enterprise": {"max_requests": 10000, "window": 60},
}

mw = RuleBasedRateLimitMiddleware(tiers=tiers, default_tier="free", db=db)
await mw.ensure_tables()  # Creates rules table + tier column

app.use(mw)
```

### Parameters

| Parameter         | Type                        | Default                      | Description                          |
| ----------------- | --------------------------- | ---------------------------- | ------------------------------------ |
| `tiers`           | `dict[str, dict[str, int]]` | required                     | Fallback tier definitions            |
| `default_tier`    | `str`                       | `"free"`                     | Default tier for unmatched users     |
| `db`              | database                    | `None`                       | Database connection                  |
| `backend`         | backend                     | `InMemoryRateLimitBackend()` | Counter storage                      |
| `key_func`        | `Callable`                  | `user_key`                   | Key extraction function              |
| `rules_cache_ttl` | `int`                       | `60`                         | Seconds between rule reloads from DB |

### RateLimitRule Model

Rules are stored in the `hyper_rate_limit_rules` table:

```sql
CREATE TABLE IF NOT EXISTS hyper_rate_limit_rules (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    path_pattern VARCHAR(500) NOT NULL DEFAULT '*',
    method VARCHAR(10) NOT NULL DEFAULT '*',
    tier VARCHAR(50) NOT NULL DEFAULT '*',
    max_requests INTEGER NOT NULL DEFAULT 100,
    window_seconds INTEGER NOT NULL DEFAULT 60,
    cost INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
```

| Column           | Description                                                                    |
| ---------------- | ------------------------------------------------------------------------------ |
| `name`           | Human-readable rule name (appears in `X-RateLimit-Rule` header)                |
| `path_pattern`   | Glob pattern (`fnmatch`). `"*"` matches all, `"/api/reports*"` matches prefix. |
| `method`         | HTTP method or `"*"` for all methods                                           |
| `tier`           | Rate limit tier name or `"*"` for all tiers                                    |
| `max_requests`   | Maximum requests per window for this rule                                      |
| `window_seconds` | Window size in seconds                                                         |
| `cost`           | How many quota units each matching request consumes (default 1)                |
| `priority`       | Higher priority rules match first (evaluated in DESC order)                    |
| `is_active`      | Inactive rules are ignored                                                     |

### Adding Rules

Via SQL:

```sql
-- Expensive report endpoint: 20 requests/minute, costs 5 units each, for free tier
INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority)
VALUES ('expensive-reports-free', '/api/reports*', 'GET', 'free', 20, 60, 5, 100);

-- Write API: 50 requests/minute for free tier
INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority)
VALUES ('write-api-free', '/api/*', 'POST', 'free', 50, 60, 1, 50);

-- Bulk import: 5 requests/hour, costs 10 units, all tiers
INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority)
VALUES ('bulk-import', '/api/import', 'POST', '*', 5, 3600, 10, 200);
```

Via the `RateLimitRule` model:

```python
from hyperdjango.ratelimit import RateLimitRule

rule = RateLimitRule(
    name="expensive-reports-free",
    path_pattern="/api/reports*",
    method="GET",
    tier="free",
    max_requests=20,
    window_seconds=60,
    cost=5,
    priority=100,
)
await rule.save(db)
```

### Rule Matching

Rules are evaluated in priority order (highest first). The first matching rule wins. Matching logic:

1. **Method**: exact match or `"*"` wildcard
2. **Tier**: exact match or `"*"` wildcard
3. **Path**: `fnmatch` glob pattern matching

If no rule matches, the tier's default limits from the `tiers` dict apply.

### Separate Counters

Each rule gets its own counter key. A user rate-limited on `/api/reports*` by the "expensive-reports-free" rule still has their full quota for other endpoints:

```
user:42:rule:1    # Counter for rule ID 1 (expensive-reports)
user:42:rule:2    # Counter for rule ID 2 (write-api)
user:42:tier:free # Counter for tier default (when no rule matches)
```

### Cost Multipliers

The `cost` field controls how many quota units each request consumes. With `cost=5` and `max_requests=20`, the effective limit is 4 requests (4 \* 5 = 20 units).

### Cache Management

Rules are cached in-process with TTL-based refresh (default 60 seconds):

```python
mw.clear_rules_cache()  # Force reload on next request
mw.clear_tier_cache()  # Clear user tier cache
mw.clear_tier_cache(user_id=42)  # Clear for specific user
```

### 429 Response Body

When rate limited by a rule:

```json
{
  "detail": "Rate limit exceeded",
  "retry_after": 42,
  "tier": "free",
  "rule": "expensive-reports-free",
  "cost": 5
}
```

## Usage Stats (Database Backend)

Query current usage for a key:

```python
backend = DatabaseRateLimitBackend(db)
usage = await backend.get_usage("user:42", window=60)
# {"count": 73, "first_request": datetime(...)}
```

## Cleanup

Database backend entries expire naturally but accumulate. Run periodic cleanup:

```python
await backend.cleanup()  # Removes entries older than 1 hour
```

For the in-memory backend, cleanup removes all expired entries across all keys:

```python
backend = InMemoryRateLimitBackend()
backend.cleanup()
```

## Complete Example

Combining all three levels of rate limiting:

```python
from hyperdjango import HyperApp
from hyperdjango.ratelimit import (
    RateLimitMiddleware,
    TieredRateLimitMiddleware,
    RuleBasedRateLimitMiddleware,
    DatabaseRateLimitBackend,
    ip_key,
)

app = HyperApp("myapi")

# Option A: Simple -- just basic IP rate limiting
app.use(RateLimitMiddleware(max_requests=100, window=60))

# Option B: Tiered -- per-group limits
tiers = {
    "free": {"max_requests": 100, "window": 60},
    "pro": {"max_requests": 1000, "window": 60},
    "enterprise": {"max_requests": 10000, "window": 60},
}
mw = TieredRateLimitMiddleware(tiers=tiers, default_tier="free", db=db)
await mw.ensure_column()
app.use(mw)

# Option C: Rule-based -- per-path/method/cost with tier integration
mw = RuleBasedRateLimitMiddleware(tiers=tiers, default_tier="free", db=db)
await mw.ensure_tables()
app.use(mw)

# All options: add a DDoS layer on top
app.use(RateLimitMiddleware(max_requests=10, window=1, key_func=ip_key))
```
