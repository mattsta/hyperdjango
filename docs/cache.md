# Cache Framework

Two backends: in-memory LRU or PostgreSQL UNLOGGED table. PostgreSQL UNLOGGED tables skip WAL writes and provide fast ephemeral storage with multi-server coordination.

HyperDjango's cache framework provides a unified API across both backends, a `@cached` decorator for function-level caching, and integration with the query cache and model system for automatic invalidation.

## Backends

### LocMemCache (Development / Single-Server)

In-process LRU cache using `OrderedDict` with O(log n) expiry cleanup via `SortedList`. Thread-safe. Fast for single-server deployments but not shared across processes.

```python
from hyperdjango.cache import LocMemCache

cache = LocMemCache(max_size=10000)
```

**Constructor:**

```python
class LocMemCache:
    def __init__(
        self, max_size: int = 10000, *, clock: Callable[[], float] = time.time
    ): ...
```

| Parameter  | Type                  | Default     | Description                                                                        |
| ---------- | --------------------- | ----------- | ---------------------------------------------------------------------------------- |
| `max_size` | `int`                 | `10000`     | Maximum number of entries. When exceeded, least-recently-used entries are evicted. |
| `clock`    | `Callable[[], float]` | `time.time` | Time source for every TTL decision.                                                |

`clock` lets a caller drive expiry by advancing time rather than waiting for it —
including through a `TwoTierCache` or `QueryCacheManager` built on this backend:

```python
now = [1000.0]
cache = LocMemCache(max_size=100, clock=lambda: now[0])
cache.set("k", "v", ttl=60)
assert cache.get("k") == "v"
now[0] += 61  # 61 virtual seconds later
assert cache.get("k") is None
```

That states both sides of the TTL boundary exactly. Sleeping past a short TTL
instead makes the result depend on machine speed: a loaded machine oversleeps,
which expires entries a test still expects to be live.

**Behavior:**

- **LRU eviction**: when `max_size` is reached, the oldest unused entry is removed
- **Expiry**: entries with TTL are tracked in a `SortedList` sorted by expiry time; cleanup is O(log n + k) where k is the number of expired entries
- **None handling**: `None` can be stored as a value; a sentinel distinguishes cache misses from stored `None`
- **Synchronous API**: all methods are synchronous (no `await`)

### DatabaseCache (Production / Multi-Server)

PostgreSQL UNLOGGED table for cross-server cache coordination. UNLOGGED tables skip Write-Ahead Log (WAL) writes, giving near-memory speed with database durability characteristics.

```python
from hyperdjango.cache import DatabaseCache

cache = DatabaseCache(db, default_ttl=300)
await cache.ensure_table()
```

**Constructor:**

```python
class DatabaseCache:
    def __init__(self, db: Database, default_ttl: int = 300): ...
```

| Parameter     | Type       | Default    | Description                                                |
| ------------- | ---------- | ---------- | ---------------------------------------------------------- |
| `db`          | `Database` | (required) | Database connection instance                               |
| `default_ttl` | `int`      | `300`      | Default TTL in seconds when `ttl` not specified in `set()` |

### Table Schema

`ensure_table()` creates the following UNLOGGED table:

```sql
CREATE UNLOGGED TABLE IF NOT EXISTS hyper_cache (
    key VARCHAR(255) PRIMARY KEY,
    value JSONB,
    counter BIGINT,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON hyper_cache (expires_at);
```

**Two-column value design:**

- `value` (JSONB): structured data -- pg.zig returns native Python objects on read
- `counter` (BIGINT): dedicated column for atomic integer counters -- clean SQL arithmetic, no type casting

When `counter` is set (via `incr`/`decr`), it takes priority over `value` on read.

### Why UNLOGGED Tables?

| Property       | Regular Table          | UNLOGGED Table     |
| -------------- | ---------------------- | ------------------ |
| WAL writes     | Yes (fsync each write) | No                 |
| Crash recovery | Full                   | Data lost on crash |
| Replication    | Yes                    | No                 |
| Write speed    | Baseline               | 2-3x faster        |
| Multi-server   | Yes                    | Yes (shared DB)    |

UNLOGGED tables are ideal for cache because:

- **Faster writes**: no fsync overhead for each insert/update
- **Multi-server**: multiple app servers share cache through the PostgreSQL connection
- **Crash recovery**: data loss on PostgreSQL crash is acceptable for cache data
- **Single infrastructure dependency**: PostgreSQL handles everything; one fewer service to monitor and maintain

If UNLOGGED is not supported (e.g., some managed PostgreSQL providers), `ensure_table()` falls back to a regular table.

## Complete API Reference

### get

```python
# LocMemCache (sync)
def get(self, key: str, default: Any = None) -> Any

# DatabaseCache (async)
async def get(self, key: str, default: Any = None) -> Any
```

Get a value by key. Returns `default` if the key is missing or expired.

```python
# LocMemCache
value = cache.get("user:42")  # Returns None if missing
value = cache.get("user:42", default={"name": "unknown"})

# DatabaseCache
value = await cache.get("user:42")
value = await cache.get("user:42", default={"name": "unknown"})
```

### set

```python
# LocMemCache (sync)
def set(self, key: str, value: Any, ttl: int | None = None) -> None

# DatabaseCache (async)
async def set(self, key: str, value: Any, ttl: int | None = None) -> None
```

Set a value with optional TTL in seconds. If `ttl` is `None`, LocMemCache stores without expiry; DatabaseCache uses `default_ttl`.

```python
# Store with 5-minute TTL
cache.set("user:42", {"name": "Alice", "age": 30}, ttl=300)

# Store without expiry (LocMemCache only)
cache.set("config:site_name", "My App")

# DatabaseCache always has a TTL (defaults to default_ttl)
await cache.set("user:42", {"name": "Alice"}, ttl=600)
```

Values are serialized to JSON (via `fast_json_dumps` from the native extension) for DatabaseCache. Any JSON-serializable Python value works: dicts, lists, strings, numbers, booleans, `None`.

### delete

```python
# LocMemCache (sync)
def delete(self, key: str) -> bool

# DatabaseCache (async)
async def delete(self, key: str) -> bool
```

Delete a key. Returns `True` if the key existed, `False` otherwise.

```python
existed = cache.delete("user:42")  # True
existed = cache.delete("user:42")  # False (already deleted)
```

### has

```python
# LocMemCache (sync)
def has(self, key: str) -> bool

# DatabaseCache (async)
async def has(self, key: str) -> bool
```

Check if a key exists and is not expired. Does not return the value.

```python
if cache.has("user:42"):
    user = cache.get("user:42")
```

### clear

```python
# LocMemCache (sync)
def clear(self) -> None

# DatabaseCache (async)
async def clear(self) -> None
```

Remove all entries from the cache.

```python
cache.clear()  # LocMemCache: clears dict + expiry index
await cache.clear()  # DatabaseCache: DELETE FROM hyper_cache
```

### count

```python
# LocMemCache (sync)
def count(self) -> int

# DatabaseCache (async)
async def count(self) -> int
```

Count non-expired entries. LocMemCache runs expiry cleanup first.

```python
n = cache.count()  # LocMemCache
n = await cache.count()  # DatabaseCache: SELECT COUNT(*) WHERE expires_at > NOW()
```

### get_or_set

```python
# LocMemCache (sync)
def get_or_set(self, key: str, default_func: Callable, ttl: int | None = None) -> Any

# DatabaseCache (async)
async def get_or_set(self, key: str, default_func: Callable, ttl: int | None = None) -> Any
```

Get a value or compute and cache it if missing. The `default_func` is a callable that returns the value to store. Uses a sentinel to correctly handle `None` as a cached value.

```python
# Sync
config = cache.get_or_set("config", lambda: load_config_from_file(), ttl=3600)

# Async
user = await cache.get_or_set(
    f"user:{user_id}", lambda: fetch_user_from_db(user_id), ttl=300
)
```

**Race-safety (DatabaseCache)**: the async implementation uses atomic
`INSERT ... ON CONFLICT ... RETURNING` so two concurrent processes racing on a
missing key will not clobber each other. If a second caller writes between the
first caller's check and its insert, the existing unexpired value wins (the
`ON CONFLICT` branch preserves `value`/`counter` when `expires_at > NOW()`),
and both callers observe the same value. Only the expired-key replacement path
overwrites existing state. Fully covered by `scripts/test_cache.py` (including
a 10-way concurrent `asyncio.gather` race test).

**Exception handling**: if `default_func` raises, the exception propagates to
the caller and no cache entry is created. The next call will re-invoke
`default_func` — failures are not memoized.

### get_many (DatabaseCache only)

```python
async def get_many(self, keys: list[str]) -> dict[str, Any]
```

Get multiple values in a single query. Returns a dict of only the found (non-expired) keys.

```python
results = await cache.get_many(["user:1", "user:2", "user:3"])
# {"user:1": {...}, "user:3": {...}}  -- user:2 was missing/expired
```

Uses a single SQL query with `IN (...)` clause.

### set_many (DatabaseCache only)

```python
async def set_many(self, mapping: dict[str, Any], ttl: int | None = None) -> None
```

Set multiple key-value pairs. Each key is set individually (uses upsert).

```python
await cache.set_many(
    {
        "user:1": {"name": "Alice"},
        "user:2": {"name": "Bob"},
        "user:3": {"name": "Charlie"},
    },
    ttl=300,
)
```

### delete_many (DatabaseCache only)

```python
async def delete_many(self, keys: list[str]) -> None
```

Delete multiple keys in a single query.

```python
await cache.delete_many(["user:1", "user:2", "user:3"])
```

### incr (DatabaseCache only)

```python
async def incr(self, key: str, delta: int = 1) -> int
```

Atomic increment. Creates the key with `delta` as the initial value if it does not exist. Uses the dedicated `counter` BIGINT column for clean SQL arithmetic.

```python
# Atomic page view counter
views = await cache.incr("page:home:views")  # 1
views = await cache.incr("page:home:views")  # 2
views = await cache.incr("page:home:views", 10)  # 12

# Decrement by passing negative delta
views = await cache.incr("page:home:views", -1)  # 11
```

The increment is atomic at the database level using:

```sql
INSERT INTO hyper_cache (key, counter, expires_at)
VALUES ($1, $2, NOW() + $3 * INTERVAL '1 second')
ON CONFLICT (key) DO UPDATE SET
counter = COALESCE(hyper_cache.counter, 0) + $4
RETURNING counter
```

### cleanup (DatabaseCache only)

```python
async def cleanup(self) -> None
```

Delete all expired entries. Call periodically (e.g., via a background task) to reclaim space.

```python
await cache.cleanup()  # DELETE FROM hyper_cache WHERE expires_at < NOW()
```

## @cached Decorator

Cache function results automatically. Works with both sync and async functions.

```python
from hyperdjango.cache import cached


@cached(ttl=60)
async def get_user(user_id: int) -> dict:
    return await db.query_one("SELECT * FROM users WHERE id = $1", user_id)


# First call hits the database
user = await get_user(42)

# Subsequent calls return cached value (within 60s TTL)
user = await get_user(42)  # From cache
```

### Decorator Parameters

```python
def cached(
    ttl: int = 300,
    key_prefix: str = "",
    cache: LocMemCache | DatabaseCache | None = None,
) -> Callable
```

| Parameter    | Type           | Default | Description                                       |
| ------------ | -------------- | ------- | ------------------------------------------------- |
| `ttl`        | `int`          | `300`   | Time-to-live in seconds                           |
| `key_prefix` | `str`          | `""`    | Prefix for cache keys. Defaults to function name. |
| `cache`      | cache instance | `None`  | Cache backend to use. `None` uses global cache.   |

### Cache Key Generation

Keys are generated from `key_prefix` (or function name) + stringified arguments:

```python
@cached(ttl=60, key_prefix="users")
async def get_user(user_id: int, include_posts: bool = False): ...


# Key for get_user(42) -> "users:42"
# Key for get_user(42, include_posts=True) -> "users:42:include_posts=True"
```

Keys longer than 200 characters are automatically hashed with MD5:

```python
# Long key -> "users:a1b2c3d4e5f6..."  (MD5 hash)
```

### Sync and Async Support

The decorator detects whether the wrapped function is async and handles both:

```python
@cached(ttl=300)
def compute_stats(date_range: str) -> dict:
    """Sync function -- uses sync cache API."""
    return expensive_computation(date_range)


@cached(ttl=300)
async def fetch_report(report_id: int) -> dict:
    """Async function -- uses async cache API if backend supports it."""
    return await db.query_one("SELECT * FROM reports WHERE id = $1", report_id)
```

### Using with Specific Cache Backend

```python
from hyperdjango.cache import LocMemCache, cached

fast_cache = LocMemCache(max_size=1000)


@cached(ttl=10, cache=fast_cache)
async def get_hot_data(key: str): ...
```

## Global Cache Instance

```python
from hyperdjango.cache import get_cache, set_cache

# Set during app initialization
set_cache(DatabaseCache(db, default_ttl=300))

# Retrieve anywhere
cache = get_cache()  # Returns global cache, or LocMemCache() fallback
```

## Cache Key Patterns

Consistent key naming prevents collisions and enables targeted invalidation:

| Pattern             | Example                 | Use Case                |
| ------------------- | ----------------------- | ----------------------- |
| `model:pk`          | `user:42`               | Single object cache     |
| `model:pk:field`    | `user:42:profile`       | Specific field/relation |
| `model:list:filter` | `user:list:active=true` | Filtered query results  |
| `page:path`         | `page:/dashboard`       | Full page cache         |
| `counter:name`      | `counter:page_views`    | Atomic counters         |
| `session:id`        | `session:abc123`        | Session data            |
| `rate:ip`           | `rate:192.168.1.1`      | Rate limiting counters  |

## Per-View Caching

Cache entire view responses using the `@cached` decorator on view handlers:

```python
from hyperdjango.cache import cached


@app.get("/dashboard")
@cached(ttl=60, key_prefix="view:dashboard")
async def dashboard(request):
    stats = await compute_dashboard_stats()
    return Response.json(stats)
```

For more granular control, use the `CacheMiddleware` from `cache_adapters`:

```python
from hyperdjango.cache_adapters import CacheMiddleware

app.use(
    CacheMiddleware(
        cache=cache,
        ttl=60,
        exclude=["/admin", "/api/auth", "/api/webhook"],
    )
)
```

## Per-Model Caching (Query Cache)

The query cache system automatically caches ORM queries with signal-driven invalidation:

```python
from hyperdjango.query_cache import configure_query_cache

# Configure at startup
configure_query_cache(backend=LocMemCache(max_size=5000), default_ttl=60)
```

### Meta.cache_ttl

Set a default cache TTL on model queries:

```python
class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300  # Cache all queries for 5 minutes

    name: str = Field()
    price: Decimal = Field(ge=0)


# Automatically cached for 300s
products = await Product.objects.filter(active=True).all()

# Manual cache control
products = await Product.objects.cache(ttl=120).filter(category="electronics").all()
```

### Cache Invalidation

The query cache invalidates automatically via signals:

- **Table-level**: any `save()` or `delete()` on a model invalidates all cached queries for that table
- **Row-level**: specific PK changes invalidate row-specific cache entries
- **FK dependency tracking**: writing to a table also invalidates tables that JOIN to it

```python
from hyperdjango.query_cache import get_query_cache

cache = get_query_cache()

# Manual invalidation
cache.invalidate_table("users")  # All cached user queries
cache.invalidate_row("users", 42)  # Specific user

# Stats
stats = cache.stats
print(f"Hit rate: {stats.hit_rate:.1%}")
print(f"Hits: {stats.hits}, Misses: {stats.misses}")
print(f"Invalidations: {stats.invalidations}")
```

## Advanced Cache Adapters

For production deployments, `hyperdjango.cache_adapters` provides distributed caching patterns:

### TwoTierCache (L1 + L2)

Fast in-process cache backed by shared database cache:

```python
from hyperdjango.cache_adapters import TwoTierCache

cache = TwoTierCache(
    l1=LocMemCache(max_size=1000),  # Fast, per-process
    l2=DatabaseCache(db),  # Shared, multi-server
    l1_ttl=10,  # L1 TTL (short)
)

await cache.set("key", value, ttl=300)  # Sets in both L1 and L2
result = await cache.get("key")  # Checks L1 first, then L2
```

### StampedeProtection (XFetch)

Prevents thundering herd on popular keys using probabilistic early expiration:

```python
from hyperdjango.cache_adapters import StampedeProtection

cache = StampedeProtection(
    backend=LocMemCache(),
    beta=1.0,  # Higher beta = more aggressive early recomputation
)
```

### ConsistentHashRing

Distribute keys across multiple cache nodes. Uses native Zig implementation (3x faster than uhashring):

```python
from hyperdjango.cache_adapters import ConsistentHashRing

ring = ConsistentHashRing(
    nodes={
        "node1": cache_instance_1,
        "node2": cache_instance_2,
        "node3": cache_instance_3,
    }
)

node = ring.get_node("user:42")  # Deterministic routing
# Adding/removing nodes only redistributes ~1/N keys
```

## Performance

| Operation            | LocMemCache | DatabaseCache         |
| -------------------- | ----------- | --------------------- |
| `get` (hit)          | ~50ns       | ~100us (network)      |
| `set`                | ~100ns      | ~200us (network)      |
| `delete`             | ~50ns       | ~100us (network)      |
| `get_many` (10 keys) | N/A         | ~150us (single query) |
| `incr` (atomic)      | N/A         | ~200us (single query) |
| Expiry cleanup       | O(log n)    | O(n expired)          |

LocMemCache is 1000-2000x faster per-operation because it avoids network round-trips. Use it as an L1 tier in front of DatabaseCache for hot keys.

### Choosing a Backend

| Scenario                   | Backend              | Reason                      |
| -------------------------- | -------------------- | --------------------------- |
| Development                | `LocMemCache`        | Zero setup, fast            |
| Single server, production  | `LocMemCache`        | No network overhead         |
| Multiple servers           | `DatabaseCache`      | Shared state via PostgreSQL |
| Multiple servers, hot keys | `TwoTierCache`       | L1 local + L2 shared        |
| High-traffic popular keys  | `StampedeProtection` | Prevents thundering herd    |
