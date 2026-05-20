# Production Scaling Guide

How to scale HyperDjango from a single server to a globally distributed deployment. This guide covers caching strategy, read replicas, connection pool tuning, monitoring, and failure modes — with code examples and decision guidance at every step.

**Prerequisites:** You should already be familiar with HyperDjango basics ([Getting Started](getting-started.md)) and have a working app deployed ([Deployment Guide](deployment-guide.md)).

---

## Architecture Overview

A production HyperDjango deployment has these layers, each with its own scaling strategy:

```
Client → nginx (TLS, static files, HTTP/2)
    → HyperDjango (24-thread Zig HTTP server, single process)
        → Middleware chain (security, CORS, timing, rate limiting)
        → Python handler (business logic)
            → Caching layer (LocMemCache → DatabaseCache → query cache)
            → Database layer (pg.zig → PostgreSQL primary + read replicas)
            → Template layer (compiled Zig templates, LRU cache)
        → Response
```

HyperDjango is **single-process, multi-threaded**. The Zig HTTP server runs 24 worker threads in one process, sharing memory. This means:

- **In-process caches are shared across all threads** (no per-worker duplication like gunicorn)
- **Connection pools are thread-safe** (pg.zig pins one connection per worker thread)
- **No inter-process communication needed** for cache invalidation or session sharing

To scale horizontally, run multiple HyperDjango instances behind nginx (each instance is one process with 24 threads). Cross-instance coordination uses PostgreSQL (UNLOGGED tables for cache, sessions, and rate limits).

---

## Caching Strategy

### Decision Tree

```
Do you need caching?
├─ < 50 rps → probably not (pg.zig handles 10K+ queries/sec)
├─ 50-500 rps → LocMemCache for hot data
├─ 500-5K rps → TwoTierCache (L1 in-memory + L2 PostgreSQL)
└─ 5K+ rps → TwoTierCache + StampedeProtection + ConsistentHashRing
```

HyperDjango provides **four caching layers** that compose together. Each layer is opt-in — start simple and add layers as profiling shows the need.

### Layer 1: Query Cache (automatic invalidation)

The query cache transparently caches ORM query results with version-based invalidation. When any row in a table is saved or deleted, all cached queries for that table are instantly invalidated — no TTL guessing.

```python
from hyperdjango.cache import LocMemCache
from hyperdjango.query_cache import configure_query_cache

# Enable query cache at app startup
cache = LocMemCache(max_entries=5000)
configure_query_cache(cache)


# Per-model default TTL — queries for this model are cached for 5 minutes
class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300
```

**How invalidation works:** Each table has a version counter. Cache keys include the version. `post_save` and `post_delete` signals bump the version — all cached queries for that table become stale without scanning or deleting individual entries. FK dependencies are tracked automatically (writing to `orders` invalidates cached queries that JOIN to `products`).

**When to use:** Any read-heavy model where the same queries repeat across requests. The cache is zero-config after the initial `configure_query_cache()` call.

**Bypass for critical reads:** Use `.cache(False)` on a queryset to skip the cache for a specific query that must always hit the database:

```python
# Always-fresh balance check (bypass query cache)
balance = await Account.objects.filter(id=acct_id).cache(False).first()
```

See [Query Cache Reference](query-cache.md) for the full API.

### Layer 2: Application Cache (explicit key/value)

For computed values, API responses, or any data that doesn't map 1:1 to a database query:

```python
from hyperdjango.cache import LocMemCache, DatabaseCache, cached

# Single server: in-memory LRU (fastest, single-process only)
cache = LocMemCache(max_entries=10000)

# Multi-server: PostgreSQL UNLOGGED table (shared across all app servers)
cache = DatabaseCache(db, table="hyper_cache", default_ttl=300)
await cache.ensure_table()


# @cached decorator — auto-generates cache key from function name + args
@cached(ttl=60)
async def get_dashboard_stats(org_id: int) -> dict:
    # Expensive aggregation query — cached for 60 seconds
    return await compute_org_stats(org_id)


# Manual cache operations
await cache.set("featured_products", product_list, ttl=120)
products = await cache.get("featured_products")

# Atomic counter — no race conditions (SQL INCREMENT, not read-modify-write)
page_views = await cache.incr(f"views:{page_id}")
```

**LocMemCache** uses an in-process LRU with O(1) get/put. Thread-safe via lock. Entries are lost on process restart. Use when: single server, or when cache misses are cheap.

**DatabaseCache** uses a PostgreSQL UNLOGGED table (no WAL writes, 2-3x faster than regular tables). Shared across all app servers. Survives app restarts but not database crashes (by design — UNLOGGED tables are truncated on crash recovery). Use when: multiple app servers need shared cache state.

See [Cache Reference](cache.md) for the full API.

### Layer 3: TwoTierCache (L1 + L2 composition)

Combines the speed of LocMemCache with the sharing of DatabaseCache:

```python
from hyperdjango.cache import LocMemCache, DatabaseCache
from hyperdjango.cache_adapters import TwoTierCache

l1 = LocMemCache(max_entries=5000)  # Fast, per-process
l2 = DatabaseCache(db, table="hyper_cache", default_ttl=300)  # Shared, durable
await l2.ensure_table()

cache = TwoTierCache(l1, l2, l1_ttl=10, fail_silently=True)
# fail_silently=True: if L2 (database) is down, serve from L1 and log a warning
```

**How it works:**

1. `get("key")` → check L1 (in-memory, ~0.1μs). Hit? Return immediately.
2. L1 miss → check L2 (database, ~1-5ms). Hit? **Promote to L1** and return.
3. L2 miss → return default. Caller computes value and `set()` writes to both tiers.

**L1 TTL is shorter than L2 TTL** (default 10s vs 300s). This means L1 entries expire frequently, triggering L2 lookups that re-promote hot keys. The result: 95%+ L1 hit rate on Zipfian (real-world) workloads, with L2 providing durability and cross-server consistency.

**Graceful degradation:** With `fail_silently=True`, L2 errors (database connection lost, table dropped) are caught and logged. L1 continues serving stale-but-available data. Stats track `l2_errors` for monitoring.

### Layer 4: StampedeProtection (thundering herd prevention)

When a popular cached key expires, all concurrent requests see a miss and simultaneously recompute the value. This is the **cache stampede** problem — it can overload your database with identical expensive queries.

```python
from hyperdjango.cache_adapters import StampedeProtection

protected_cache = StampedeProtection(cache, beta=1.0)

# XFetch: as the key approaches expiry, random requests recompute early
# (spreading the load instead of all requests hitting at once)
stats = await protected_cache.get_or_compute(
    "dashboard_stats",
    compute_fn=compute_expensive_stats,
    ttl=300,
)
```

**How XFetch works:** Each cached value stores `(value, expires_at, compute_time_ms)`. On each read, the algorithm calculates a probability of early recomputation based on remaining TTL and the original compute time. As the TTL approaches zero, the probability approaches 1.0. The `beta` parameter controls aggressiveness:

- `beta=0.5` — conservative, recomputes close to expiry (less extra compute, more stampede risk)
- `beta=1.0` — default, balanced
- `beta=2.0` — aggressive, recomputes earlier (more extra compute, virtually no stampede risk)

### Layer 5: ConsistentHashRing (distributed cache sharding)

When a single cache backend becomes a bottleneck, distribute keys across multiple nodes:

```python
from hyperdjango.cache_adapters import ConsistentHashRing

# Create a ring with multiple cache backends
ring = ConsistentHashRing(
    nodes={
        "cache-1": LocMemCache(max_entries=5000),
        "cache-2": LocMemCache(max_entries=5000),
        "cache-3": LocMemCache(max_entries=5000),
    },
    vnodes=40,  # Virtual nodes per real node (higher = more uniform distribution)
)

# ring.get("key") routes to the correct node automatically
value = ring.get("user:42:profile")
ring.set("user:42:profile", profile_data, ttl=120)
```

**Native Zig implementation** using ketama-compatible MD5 hashing — 3x faster than Python `uhashring`. When a node is added or removed, only ~1/N keys are rerouted (consistent hashing property).

**When to use:** When your cache hit rate is good but individual cache nodes are saturated (high memory pressure, too many connections). Rare for PostgreSQL-backed caches (UNLOGGED tables are extremely fast) — more relevant for in-memory caches in multi-process deployments.

See [Cache Adapters Reference](cache-adapters.md) for the full API.

---

## Writing Custom Cache Backends

HyperDjango's cache adapters work with any backend that implements the cache protocol — `get`, `set`, `delete`, `has`, `clear`:

```python
class CustomCacheBackend:
    """Example custom cache backend."""

    def __init__(self, client, default_ttl: int = 300):
        self.client = client
        self.default_ttl = default_ttl

    async def get(self, key: str, default=None):
        value = await self.client.get(key)
        if value is None:
            return default
        return fast_json_loads(value)

    async def set(self, key: str, value, ttl: int | None = None):
        ttl = ttl or self.default_ttl
        await self.client.setex(key, ttl, fast_json_dumps(value))

    async def delete(self, key: str) -> bool:
        return bool(await self.client.delete(key))

    async def has(self, key: str) -> bool:
        return bool(await self.client.exists(key))

    async def clear(self):
        await self.client.flushdb()
```

This backend composes with `TwoTierCache`, `StampedeProtection`, or `ConsistentHashRing`:

```python
# Custom backend as L2, LocMemCache as L1
l1 = LocMemCache(max_entries=5000)
l2 = CustomCacheBackend(client, default_ttl=300)
cache = TwoTierCache(l1, l2, l1_ttl=10)
```

**Why HyperDjango ships PostgreSQL UNLOGGED as the default L2:** UNLOGGED tables provide shared caching across app servers with zero additional infrastructure. For most deployments (up to thousands of rps), `DatabaseCache` is sufficient and eliminates an entire service from your stack.

---

## Read Replicas

### When to Use Replicas

Read replicas reduce load on your primary database by routing read queries to one or more standby servers. Use them when:

- Your primary's CPU/IO is saturated by read queries
- You want geographic read distribution (replica near users, primary near writers)
- You need a hot standby for failover

Don't use them when:

- Your app is write-heavy (replicas don't help with writes)
- You have < 500 rps (a single PostgreSQL instance handles this easily with pg.zig)
- You can't tolerate eventual consistency (replicas lag behind primary)

### Setup

```python
from hyperdjango.multi_db import ConnectionManager, PrimaryReplicaRouter

# Register databases
connections = ConnectionManager()
connections.configure(
    {
        "default": "postgres://primary-host:5432/mydb",
        "replica": "postgres://replica-host:5432/mydb",
    }
)

# Automatic read/write splitting
connections.router = PrimaryReplicaRouter(
    primary="default",
    replica="replica",
)

# All reads automatically go to replica:
articles = await Article.objects.all()  # → replica

# All writes automatically go to primary:
await article.save()  # → primary

# Explicit override when you need guaranteed-fresh data:
fresh = await Article.objects.using("default").get(id=42)  # → primary
```

### Handling Replication Lag

PostgreSQL streaming replication typically has 100ms-1s lag. After a write, the replica may serve stale data. Use **sticky routing** to keep reads on the primary for a brief window after writes:

```python
# After a write, subsequent reads in the same request hit the primary
@app.post("/articles")
async def create_article(request):
    article = Article(**data)
    await article.save()  # → primary

    # This read must see the just-written data
    fresh = await Article.objects.using("default").get(id=article.id)  # → primary
    return Response.json(fresh.to_dict(), status=201)
```

For cross-request consistency (e.g., POST then redirect to GET), use a short delay or version-based cache invalidation.

### Connection Pool Sizing

Each database gets its own connection pool. Size them based on your thread count:

```python
# Pool size should be >= Zig HTTP worker thread count (default 24)
# pg.zig pins one connection per thread for the lifetime of the thread
connections.configure(
    {
        "default": {"url": "postgres://primary/mydb", "pool_size": 32},
        "replica": {"url": "postgres://replica/mydb", "pool_size": 32},
    }
)
```

See [Multi-Database Reference](multi-db.md) for custom routers, per-model database binding, and health checks.

---

## Monitoring

### Key Metrics

HyperDjango exposes Prometheus-compatible metrics via the telemetry subsystem:

```python
from hyperdjango.telemetry import configure_from_settings

telemetry = configure_from_settings(app)
app.get("/metrics")(telemetry.prometheus_sink.handler)
```

**Critical metrics to monitor:**

| Metric                                          | Alert threshold | What it means                               |
| ----------------------------------------------- | --------------- | ------------------------------------------- |
| `hyperdjango_http_request_duration_seconds` p99 | > 500ms         | Requests are slow — check DB or cache       |
| `hyperdjango_http_requests_total{status="5xx"}` | > 0.1% of total | Server errors — check logs                  |
| Cache hit rate (via `cache.stats`)              | < 90%           | Cache is too small or TTLs are wrong        |
| `hyperdjango_pool_waiters`                      | > 0 sustained   | Pool is undersized — increase POOL_SIZE     |
| `hyperdjango_pool_available`                    | = 0 sustained   | All connections busy — DB is the bottleneck |
| `hyperdjango_rate_limit_hits_total`             | Sudden spike    | Possible abuse or misconfigured limits      |

### Health Check Endpoint

```python
app.mount_health()  # GET /health → {"status": "ok"}
```

For deeper health checks (database connectivity, pool health):

```python
@app.get("/health/ready")
async def readiness(request):
    db = get_db()
    try:
        await db.query_val("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return Response.json({"status": "error", "db": str(e)}, status=503)
```

Wire this to nginx upstream health checks or Kubernetes readiness probes.

---

## Failure Modes and Graceful Degradation

| Failure                           | Impact                                      | Mitigation                                                                      |
| --------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------- |
| **L2 cache (DatabaseCache) down** | L1 still serves; new misses hit DB directly | `TwoTierCache(fail_silently=True)` logs warning, continues serving              |
| **Read replica down**             | Reads fail on replica                       | PrimaryReplicaRouter falls back to primary (configure `fallback=True`)          |
| **Primary DB overloaded**         | All writes slow                             | Scale connection pool, add read replicas, cache write-heavy reads               |
| **Cache stampede**                | Burst of identical expensive queries        | StampedeProtection spreads recomputation via XFetch                             |
| **Hash ring node removed**        | ~1/N keys rerouted                          | Consistent hashing limits blast radius; rerouted keys recompute on first access |
| **App server crashed**            | nginx routes to remaining instances         | Run 2+ instances behind nginx upstream with health checks                       |

---

## Configuration Reference

| Setting                    | Default                         | What it controls                    |
| -------------------------- | ------------------------------- | ----------------------------------- |
| `POOL_SIZE`                | `max(THREAD_POOL_SIZE + 8, 32)` | pg.zig connection pool per database |
| `THREAD_POOL_SIZE`         | `24`                            | Zig HTTP worker threads             |
| `MAX_BODY_SIZE`            | `10485760` (10MB)               | Maximum request body size           |
| `CACHE_TTL`                | `300`                           | Default cache TTL (seconds)         |
| `TELEMETRY_ENABLED`        | `False`                         | Enable Prometheus metrics           |
| `TELEMETRY_DRAIN_INTERVAL` | `1.0`                           | Metric flush interval (seconds)     |
| `TELEMETRY_SAMPLE_RATIO`   | `0.01`                          | Trace sampling ratio (1% default)   |
| `RATELIMIT_IETF_HEADERS`   | `True`                          | Emit IETF RateLimit-Policy headers  |

See [Settings Reference](settings.md) and [Tuning Guide](tuning.md) for the full list.
