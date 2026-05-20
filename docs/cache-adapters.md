# Cache Adapters

Advanced caching patterns: consistent hashing, stampede protection, two-tier caching, and full-page HTTP caching. All adapters use PostgreSQL UNLOGGED tables or in-memory storage.

## Overview

HyperDjango's cache adapter system builds on top of the core cache framework (`LocMemCache` and `DatabaseCache`) with production-ready distributed caching patterns:

| Adapter              | Purpose                                                  |
| -------------------- | -------------------------------------------------------- |
| `ConsistentHashRing` | Distribute keys across multiple cache nodes              |
| `StampedeProtection` | Prevent thundering herd on cache miss (XFetch algorithm) |
| `TwoTierCache`       | L1 in-memory + L2 database layered cache                 |
| `CacheMiddleware`    | Full-page HTTP response caching                          |

All adapters implement the `CacheAdapter` protocol:

```python
class CacheAdapter(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any, ttl: int | None = None): ...
    def delete(self, key: str) -> bool: ...
    def clear(self): ...
    def has(self, key: str) -> bool: ...
```

## ConsistentHashRing

Native Zig ketama-compatible hash ring for distributing cache keys across multiple nodes. Uses SIMD-optimized scanning of a contiguous sorted array for cache-friendly binary search lookups. Benchmarks at 3x faster than the Python `uhashring` package.

### Constructor

```python
from hyperdjango.cache_adapters import ConsistentHashRing

# Basic: equal-weight nodes
ring = ConsistentHashRing(
    nodes={
        "cache1": cache_backend_1,
        "cache2": cache_backend_2,
        "cache3": cache_backend_3,
    }
)

# With custom replicas and vnodes
ring = ConsistentHashRing(
    nodes={"shard1": cache1, "shard2": cache2},
    replicas=4,  # Number of hash function replicas per node
    vnodes=40,  # Virtual nodes per real node (higher = better distribution)
)

# With weight function (e.g., more capacity = higher weight)
ring = ConsistentHashRing(
    nodes={"big": cache_big, "small": cache_small},
    weight_fn=lambda name: 3 if name == "big" else 1,
)
```

### Constructor Parameters

| Parameter   | Type             | Default | Description                                |
| ----------- | ---------------- | ------- | ------------------------------------------ |
| `nodes`     | `dict[str, Any]` | `None`  | Map of node name to cache backend instance |
| `replicas`  | `int`            | `4`     | Hash function replicas per node            |
| `vnodes`    | `int`            | `40`    | Virtual nodes per real node                |
| `weight_fn` | `Callable`       | `None`  | Function returning weight for a node name  |

### Key Methods

**`get_node(key)`** -- returns the cache backend responsible for a key:

```python
node = ring.get_node("user:42")  # Returns the cache backend object
await node.set("user:42", user_data, ttl=300)
```

**`get_node_name(key)`** -- returns the name of the responsible node:

```python
name = ring.get_node_name("user:42")  # "cache2"
```

**`add_node(name, backend, weight=1)`** -- add a node to the ring:

```python
ring.add_node("cache4", new_cache_backend, weight=2)
# Ring is automatically rebuilt after adding
```

**`remove_node(name)`** -- remove a node from the ring:

```python
ring.remove_node("cache2")
# Keys previously on cache2 are redistributed to remaining nodes
```

**`get_stats()`** -- ring statistics including per-node point distribution:

```python
stats = ring.get_stats()
# {
#     "total_points": 480,
#     "node_count": 3,
#     "distribution": {"cache1": 160, "cache2": 160, "cache3": 160}
# }
```

**`hash_key(key)`** -- hash a key to a ketama-compatible 32-bit integer (static method):

```python
h = ConsistentHashRing.hash_key("user:42")  # 2847361052
```

### Node Distribution

The hash ring uses ketama-compatible MD5 hashing. Each node gets `vnodes * weight` virtual points on the ring. When a key is looked up, it is hashed and the nearest virtual point clockwise determines the owning node.

With default settings (`vnodes=40`, equal weights), key distribution across 3 nodes is within 2% of perfectly uniform. Increasing `vnodes` improves uniformity at the cost of slightly more memory.

### How It Works

1. Each node is hashed to multiple points on a 32-bit integer ring
2. Points are stored in a contiguous sorted array (batch sort, not insort)
3. Key lookup uses binary search on the sorted array (cache-friendly)
4. The Zig implementation uses SIMD for scanning when the ring is large
5. Adding/removing nodes triggers a rebuild of the sorted array

## StampedeProtection (XFetch)

Prevents thundering herd on cache miss using the XFetch algorithm. When a cached value approaches expiry, individual requests have an increasing probability of recomputing it -- spreading regeneration across multiple requests instead of all hitting at once.

### How XFetch Works

The standard cache stampede problem: when a popular key expires, all concurrent requests see a cache miss and simultaneously recompute the value, overwhelming the backend.

XFetch solves this by storing metadata alongside each cached value:

- `expires_at` -- when the value actually expires
- `compute_time` -- how long it took to compute the value

On each `get()`, XFetch calculates a probability of early recomputation:

```
P(recompute) = beta * compute_time * ln(random()) + expires_at <= now
```

The `beta` parameter controls aggressiveness:

| Beta  | Behavior                                          |
| ----- | ------------------------------------------------- |
| `0.5` | Conservative -- recompute very close to expiry    |
| `1.0` | Default -- balanced early refresh                 |
| `2.0` | Aggressive -- start refreshing well before expiry |

### Usage

```python
from hyperdjango.cache_adapters import StampedeProtection
from hyperdjango.cache import LocMemCache

backend = LocMemCache(max_entries=10000)
cache = StampedeProtection(backend=backend, beta=1.0)

# Set a value with compute time metadata
start = time.time()
value = await expensive_database_query()
compute_ms = (time.time() - start) * 1000

cache.set("dashboard:stats", value, ttl=300, compute_time_ms=compute_ms)

# Get -- may return None early to trigger one request to recompute
result = cache.get("dashboard:stats")
if result is None:
    # This request is the "chosen one" to recompute
    value = await expensive_database_query()
    cache.set("dashboard:stats", value, ttl=300, compute_time_ms=50)
    result = value
```

### Get-or-Set Pattern

For the common pattern of fetching from cache or computing on miss:

```python
async def get_dashboard_stats():
    result = cache.get("dashboard:stats")
    if result is not None:
        return result

    # Cache miss (or XFetch early expiry)
    start = time.time()
    stats = await compute_stats()
    compute_ms = (time.time() - start) * 1000
    cache.set("dashboard:stats", stats, ttl=300, compute_time_ms=compute_ms)
    return stats
```

### API Reference

| Method                                        | Description                                     |
| --------------------------------------------- | ----------------------------------------------- |
| `get(key, default=None)`                      | Get value with probabilistic early expiry       |
| `set(key, value, ttl=300, compute_time_ms=0)` | Set value with stampede metadata                |
| `delete(key)`                                 | Delete a cached value                           |
| `clear()`                                     | Clear all cached values                         |
| `has(key)`                                    | Check if a key exists (subject to early expiry) |

## TwoTierCache

Layered cache with L1 (in-process `LocMemCache`) and L2 (shared `DatabaseCache`). L1 provides sub-microsecond access for hot keys, while L2 provides shared storage visible to all server processes.

### Architecture

```
Request
  │
  ├── L1 Hit (LocMemCache) ──→ Return immediately (~0.1 us)
  │
  ├── L1 Miss, L2 Hit (DatabaseCache) ──→ Promote to L1, return (~1 ms)
  │
  └── L1 Miss, L2 Miss ──→ Return default / compute value
```

### Configuration

```python
from hyperdjango.cache import LocMemCache, DatabaseCache
from hyperdjango.cache_adapters import TwoTierCache

l1 = LocMemCache(max_entries=1000)
l2 = DatabaseCache(db, table="hyper_cache")

cache = TwoTierCache(
    l1=l1,
    l2=l2,
    l1_ttl=10,  # L1 entries expire after 10 seconds
)
```

| Parameter | Type           | Default  | Description                                   |
| --------- | -------------- | -------- | --------------------------------------------- |
| `l1`      | `CacheAdapter` | required | Fast local cache (typically `LocMemCache`)    |
| `l2`      | `CacheAdapter` | required | Shared cache (typically `DatabaseCache`)      |
| `l1_ttl`  | `int`          | `10`     | L1 TTL in seconds (shorter = more consistent) |

### Operations

**Sync API** (when L2 is sync):

```python
# Get: checks L1 first, then L2, promotes on L2 hit
value = cache.get("user:42")

# Set: writes to both L1 and L2
cache.set("user:42", user_data, ttl=300)

# Delete: removes from both tiers
cache.delete("user:42")

# Clear: clears both tiers and resets stats
cache.clear()
```

**Async API** (when L2 is async, like `DatabaseCache`):

```python
value = await cache.aget("user:42")
await cache.aset("user:42", user_data, ttl=300)
```

### Write-Through Behavior

On `set()`, the value is written to both L1 and L2. L1 uses the shorter `l1_ttl`, while L2 uses the full `ttl`. This means:

- L1 entries expire quickly, limiting staleness across processes
- L2 entries persist longer, reducing database recomputation
- On L1 miss + L2 hit, the value is promoted back to L1

### Statistics

```python
stats = cache.get_stats()
# {
#     "l1_hits": 8432,
#     "l2_hits": 1204,
#     "misses": 364,
#     "total_requests": 10000,
#     "l1_hit_rate": 0.8432,
#     "l2_hit_rate": 0.1204,
#     "overall_hit_rate": 0.9636,
# }
```

A healthy two-tier cache should show L1 hit rate > 80% for frequently accessed keys. If L2 hit rate is high but L1 is low, consider increasing `l1_ttl` or `max_entries`.

## CacheMiddleware

Full-page HTTP response caching middleware. Caches GET responses and serves them with `X-Cache: HIT/MISS` headers.

### Configuration

```python
from hyperdjango.cache_adapters import CacheMiddleware

app.use(
    CacheMiddleware(
        cache=my_cache,  # Any CacheAdapter
        ttl=60,  # Cache responses for 60 seconds
        exclude=["/admin", "/api/auth"],  # Don't cache these path prefixes
        cache_authenticated=False,  # Skip caching for logged-in users
        vary_headers=["Accept-Language"],  # Include these headers in cache key
    )
)
```

| Parameter             | Type           | Default  | Description                                        |
| --------------------- | -------------- | -------- | -------------------------------------------------- |
| `cache`               | `CacheAdapter` | required | Cache backend to use                               |
| `ttl`                 | `int`          | `60`     | Response cache TTL in seconds                      |
| `exclude`             | `list[str]`    | `[]`     | Path prefixes to exclude from caching              |
| `cache_authenticated` | `bool`         | `False`  | Whether to cache responses for authenticated users |
| `vary_headers`        | `list[str]`    | `[]`     | Headers to include in the cache key                |

### Cache Key Generation

Cache keys are built from:

1. Request path (`/api/users`)
2. Query string (`?page=2&sort=name`)
3. Vary headers (e.g., `Accept-Language=en`)
4. User identity (when `cache_authenticated=True`)

If the combined key exceeds 200 characters, it is hashed with MD5 to keep keys compact.

Example keys:

```
page:/api/users                           # Simple path
page:/api/users|page=2&sort=name          # With query string
page:/api/users|Accept-Language=en        # With vary header
page:a1b2c3d4e5f6...                      # MD5 hash for long keys
```

### Cache-Control Headers

The middleware adds `X-Cache` headers to indicate cache status:

```
# Cache hit
X-Cache: HIT

# Cache miss (response was cached for next request)
X-Cache: MISS
```

### What Gets Cached

- Only `GET` requests
- Only `2xx` responses
- Skips paths matching any `exclude` prefix
- Skips authenticated users unless `cache_authenticated=True`

## Adapter Registry

Register custom cache adapters by name for configuration-driven cache selection:

```python
from hyperdjango.cache_adapters import register_adapter, get_adapter, list_adapters

# Register a custom adapter
register_adapter("custom", CustomAdapter)

# Retrieve by name
adapter_class = get_adapter("custom")
cache = adapter_class(**adapter_config)

# List all registered adapters
names = list_adapters()  # ["custom"]
```

This pattern is useful when cache backend selection is driven by configuration files or environment variables:

```python
cache_type = os.environ.get("CACHE_BACKEND", "locmem")
adapter_class = get_adapter(cache_type)
if adapter_class is None:
    raise ValueError(f"Unknown cache backend: {cache_type}")
cache = adapter_class(**cache_config)
```

---

## Architecture: How the Adapters Compose

The adapters are designed to layer on top of each other. Here's how they compose for different scale levels:

### Single Server (< 500 rps)

```
Request → LocMemCache (in-process LRU, ~0.1μs)
              ↓ miss
          Database query (~1-5ms)
```

Just use `LocMemCache` directly. No adapters needed.

### Multi-Server (500-5K rps)

```
Request → TwoTierCache
              ├─ L1: LocMemCache (per-process, ~0.1μs) — 95%+ hit rate
              └─ L2: DatabaseCache (shared PostgreSQL UNLOGGED, ~1-5ms)
                         ↓ miss
                     Database query
```

TwoTierCache gives each server process a fast local cache (L1) backed by a shared cache (L2) that all servers can read/write. L1 entries expire quickly (10s), triggering L2 lookups that re-promote hot keys. Result: most requests never leave the process.

### High Traffic (5K+ rps)

```
Request → StampedeProtection (XFetch early recompute)
              ↓
          TwoTierCache
              ├─ L1: LocMemCache
              └─ L2: DatabaseCache
                         ↓ miss
                     Database query (spread across time, no thundering herd)
```

Add StampedeProtection when cache misses cause expensive queries that many users hit simultaneously. XFetch spreads recomputation across requests rather than all hitting at expiry.

### Sharded (horizontal scaling beyond single DB)

```
Request → ConsistentHashRing (routes to correct shard)
              ├─ Shard 1: TwoTierCache + StampedeProtection
              ├─ Shard 2: TwoTierCache + StampedeProtection
              └─ Shard 3: TwoTierCache + StampedeProtection
```

ConsistentHashRing distributes keys deterministically across cache shards. Each shard can be a TwoTierCache with its own L1/L2. Adding/removing shards only reroutes ~1/N keys.

---

## Writing Custom Cache Backends

Any object implementing the `CacheAdapter` protocol works with all adapters. The protocol is small — just `get`, `set`, `delete`, `has`, `clear`:

```python
from hyperdjango.native import fast_json_dumps, fast_json_loads


class CustomCache:
    """Custom cache backend compatible with all HyperDjango cache adapters."""

    def __init__(self, client, default_ttl: int = 300):
        self.client = client
        self.default_ttl = default_ttl

    async def get(self, key: str, default=None):
        raw = await self.client.get(key)
        if raw is None:
            return default
        return fast_json_loads(raw)

    async def set(self, key: str, value, ttl: int | None = None):
        ttl = ttl or self.default_ttl
        await self.client.setex(key, ttl, fast_json_dumps(value))

    async def delete(self, key: str) -> bool:
        return bool(await self.client.delete(key))

    async def has(self, key: str) -> bool:
        return bool(await self.client.exists(key))

    async def clear(self):
        await self.client.flushdb()


# Use it as L2 in TwoTierCache:
cache = TwoTierCache(
    l1=LocMemCache(max_entries=5000),
    l2=CustomCache(client, default_ttl=300),
    l1_ttl=10,
)

# Or in a ConsistentHashRing:
ring = ConsistentHashRing(
    nodes={
        "shard-1": CustomCache(client_1),
        "shard-2": CustomCache(client_2),
    }
)
```

### Why PostgreSQL UNLOGGED Is Often Enough

HyperDjango's `DatabaseCache` uses PostgreSQL `UNLOGGED` tables. The advantages:

- **Zero additional infrastructure** — uses the database you already have, no separate service to deploy, monitor, or secure.
- **Atomic operations** — `INSERT ... ON CONFLICT`, `UPDATE ... RETURNING`, race-safe `get_or_set`.
- **Shared across servers** — every app server reads/writes the same UNLOGGED table.
- **Connection pooling** — pg.zig's pool is already in the request path; no second pool to size.
- **Write throughput** — no WAL = 2-3x faster than regular tables.
- **Persistence** — survives app restarts; cleared on DB crash (by design — this is a cache, not a database).

`DatabaseCache` with a `TwoTierCache` L1 (`LocMemCache`, ~0.1 μs hit) gives sub-millisecond reads for 95%+ of requests with no additional infrastructure.

---

## Failure Modes

| Scenario                                         | What happens                                                                                      | Recovery                                                           |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| **L1 (LocMemCache) full**                        | LRU eviction of least-recently-used entries                                                       | Automatic — hot entries stay, cold entries evict                   |
| **L2 (DatabaseCache) down**                      | `TwoTierCache(fail_silently=True)` logs warning, serves from L1                                   | Fix DB connection; L2 auto-resumes on next successful query        |
| **L2 (DatabaseCache) slow**                      | L1 absorbs 95%+ of reads; only cache misses hit slow L2                                           | Monitor `l2_errors` in cache stats                                 |
| **Hash ring node removed**                       | ~1/N keys reroute to other nodes; those keys are cache misses until recomputed                    | Automatic — consistent hashing limits blast radius                 |
| **Hash ring node added**                         | ~1/N keys reroute to new node; slight bump in cache misses                                        | Automatic — new node warms up quickly from promoted L2 hits        |
| **Cache stampede**                               | StampedeProtection spreads recomputation; one request recomputes, others get slightly-stale value | Automatic via XFetch algorithm                                     |
| **UNLOGGED table truncated** (DB crash recovery) | All L2 cache entries lost                                                                         | Automatic — L1 continues serving; L2 repopulates from cache misses |
