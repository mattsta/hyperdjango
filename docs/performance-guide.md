# Performance Optimization Guide

This guide covers techniques for getting maximum performance out of HyperDjango
applications: native Zig acceleration, query optimization, caching, profiling,
and connection pool tuning.

For API references, see [performance.md](performance.md), [cache.md](cache.md),
[query-cache.md](query-cache.md), and [pool.md](pool.md).

---

## Table of Contents

- [Native Zig Performance](#native-zig-performance)
- [Query Optimization](#query-optimization)
- [N+1 Detection](#n1-detection)
- [Caching](#caching)
- [Query Cache](#query-cache)
- [REST Native JSON Fast Path](#rest-native-json-fast-path)
- [Connection Pool Tuning](#connection-pool-tuning)
- [Profiling](#profiling)
- [Slow Query Logging](#slow-query-logging)
- [Admin Acceleration](#admin-acceleration)
- [Production Checklist](#production-checklist)

---

## Native Zig Performance

HyperDjango ships with a compiled Zig native extension that accelerates the
most performance-critical operations. There are no Python fallbacks -- the
extension is required.

### What Is Accelerated

| Component            | Zig vs Python      | Throughput                   |
| -------------------- | ------------------ | ---------------------------- |
| PostgreSQL driver    | pg.zig vs psycopg3 | 2-4x faster SELECTs          |
| COPY bulk import     | Native protocol    | 42.8x faster (536K rows/s)   |
| Model validation     | SIMD batch         | 13.1M models/sec             |
| JSON serialization   | SIMD parser        | 6-10x faster loads           |
| Template compilation | Native parser      | 234x faster (7.1 us)         |
| Template rendering   | Native node walker | 1.7x faster (36 us)          |
| HTML escape          | SIMD               | 2.0-3.4x faster              |
| URL encode/decode    | SIMD               | 4-17x faster                 |
| Email validation     | SIMD               | 63 ns per email              |
| HTTP server          | auto-scaled pool   | 2.1-2.3x faster than uvicorn |
| Consistent hash ring | SIMD scan          | 3x faster than uhashring     |
| Cookie parsing       | Native             | Faster than Python stdlib    |

Build the extension with:

```bash
uv run hyper-build                   # Debug mode (faster builds)
uv run hyper-build --install --release  # Release mode (faster runtime)
```

Always use `--release` for production deployments.

### Prepared Statement Caching

pg.zig automatically caches prepared statements. The first execution of a
query pays the Parse cost; subsequent executions with the same SQL skip
directly to Bind+Execute, saving ~33% per query.

```python
# First call: Parse + Bind + Execute
users = await User.objects.filter(active=True).all()

# Second call: Bind + Execute only (prepared statement reused)
users = await User.objects.filter(active=True).all()
```

### Prepared Statement Warmup

For latency-sensitive paths, warm up prepared statements at startup:

```python
@app.on_startup
async def warm_statements():
    # Execute each critical query once to cache the prepared statement
    await User.objects.filter(id=0).first()
    await Product.objects.filter(active=True).order_by("-created_at").first()
    await Order.objects.filter(status="pending").count()
```

This reduces first-query latency from ~494 us to ~65 us (7.7x improvement).

---

## Query Optimization

### select_related (JOIN loading)

Avoid N+1 queries on foreign key access by JOINing related tables:

```python
# Bad: N+1 queries (1 for orders + N for customers)
orders = await Order.objects.all()
for order in orders:
    print(order.customer.name)  # Each access triggers a query

# Good: Single query with JOIN
orders = await Order.objects.select_related("customer").all()
for order in orders:
    print(order.customer.name)  # Already loaded, no extra query
```

Chain multiple relations:

```python
items = (
    await OrderItem.objects.select_related("order", "order__customer", "product")
    .filter(order__status="pending")
    .all()
)
```

### prefetch_related (Batch loading)

For many-to-many or reverse foreign key relations, use `prefetch_related`
to load related objects in a separate batch query:

```python
# Loads all products, then batch-loads their tags in 1 extra query
products = await Product.objects.prefetch_related("tags").all()
for product in products:
    print(product.tags)  # Already loaded
```

### annotate and aggregate

Push computation into the database:

```python
from hyperdjango.expressions import Count, Sum, Avg, F

# Annotate each category with product count
categories = (
    await Category.objects.annotate(
        product_count=Count("products"),
    )
    .order_by("-product_count")
    .all()
)

# Aggregate across all rows
stats = await Order.objects.filter(status="completed").aggregate(
    total_revenue=Sum("total"),
    avg_order_value=Avg("total"),
    order_count=Count("id"),
)
print(f"Revenue: ${stats['total_revenue']}, AOV: ${stats['avg_order_value']}")

# Use F expressions for database-side math
await Product.objects.filter(stock__lt=F("reorder_point")).all()
```

### values and only

Fetch only the columns you need:

```python
# Returns list of dicts, not full model instances
emails = await User.objects.filter(active=True).values("id", "email").all()
# [{"id": 1, "email": "alice@example.com"}, ...]

# Returns model instances with only specified fields populated
users = await User.objects.only("id", "name").all()
```

### explain

View the query execution plan:

```python
plan = await User.objects.filter(active=True).order_by("name").explain()
print(plan)
# Sort  (cost=... rows=...)
#   ->  Seq Scan on users  (cost=... rows=...)
#         Filter: (active = true)
```

---

## N+1 Detection

`PerformanceMiddleware` automatically detects N+1 query patterns and reports
them in response headers and the debug dashboard.

```python
from hyperdjango.performance import PerformanceMiddleware

perf = PerformanceMiddleware(
    slow_query_threshold_ms=100,  # Flag queries slower than 100ms
    n_plus_one_threshold=5,  # Flag when same SQL runs 5+ times per request
    dashboard_path="/debug/performance",
)
app.use(perf)
```

### Response Headers

Every response includes query statistics:

```
X-Query-Count: 3
X-Query-Time: 2.4ms
```

When N+1 patterns are detected:

```
X-N-Plus-One: SELECT * FROM customers WHERE id = $1 (repeated 25 times)
```

### Performance Dashboard

Visit `/debug/performance` to see:

- Recent request statistics (query count, total time, slow queries)
- N+1 patterns detected across all recent requests
- Aggregate stats (total requests, total queries, slow query count)

```python
# Access stats programmatically
stats = perf.get_stats()
print(f"Total requests: {stats['total_requests']}")
print(f"Total queries: {stats['total_queries']}")
print(f"Slow queries: {stats['slow_count']}")
print(f"N+1 detections: {stats['n_plus_one_count']}")
```

---

## Caching

### LocMemCache (Single Server)

In-process LRU cache with O(log n) expiry cleanup:

```python
from hyperdjango.cache import LocMemCache

cache = LocMemCache(max_size=10000)

cache.set("user:42", {"name": "Alice", "email": "alice@example.com"}, ttl=300)
user = cache.get("user:42")  # Returns dict or None
cache.delete("user:42")
```

### DatabaseCache (Multi-Server)

PostgreSQL UNLOGGED table cache for shared state across servers:

```python
from hyperdjango.cache import DatabaseCache

cache = DatabaseCache(db)
await cache.ensure_table()

await cache.set("config:feature_flags", flags_dict, ttl=60)
flags = await cache.get("config:feature_flags")
```

UNLOGGED tables skip WAL writes, giving near-memory speed while surviving
connection drops (but not server crashes).

### @cached Decorator

Cache function results automatically:

```python
from hyperdjango.cache import cached


@cached(ttl=60)
async def get_dashboard_stats(user_id):
    """Cached for 60 seconds per user_id."""
    orders = await Order.objects.filter(user_id=user_id).aggregate(
        total=Sum("total"),
        count=Count("id"),
    )
    return orders


# First call: executes query, caches result
stats = await get_dashboard_stats(42)

# Second call within 60s: returns cached result
stats = await get_dashboard_stats(42)
```

The cache key is derived from the function name and arguments automatically.

---

## Query Cache

Transparent caching of QuerySet results with signal-driven invalidation:

```python
from hyperdjango.query_cache import configure_query_cache

configure_query_cache(
    backend=LocMemCache(max_size=5000),
    default_ttl=60,
)

# Cached query -- result stored for 120 seconds
users = await User.objects.cache(ttl=120).filter(active=True).all()

# When a User is saved or deleted, the cache is automatically invalidated
await user.save()  # Triggers post_save signal -> invalidates user cache
```

### Per-Model Cache TTL

```python
class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300  # Cache all queries for 5 minutes

    ...
```

### FK Dependency Tracking

The query cache tracks foreign key relationships. When a related table
changes, all cached queries that JOIN to it are invalidated:

```python
# This query JOINs to the customers table
orders = await Order.objects.cache().select_related("customer").all()

# Updating a customer invalidates the cached orders query too
await customer.save()  # Invalidates both customer AND order caches
```

### Cache Stats

```python
cache = get_query_cache()
stats = cache.stats
print(f"Hits: {stats.hits}, Misses: {stats.misses}")
print(f"Hit rate: {stats.hit_rate:.1%}")
print(f"Invalidations: {stats.invalidations}")
```

---

## REST Native JSON Fast Path

The REST framework can use the Zig SIMD JSON serializer for responses:

```python
from hyperdjango.rest import ModelViewSet, ModelSerializer


class ProductSerializer(ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "name", "price", "stock"]


class ProductViewSet(ModelViewSet):
    serializer_class = ProductSerializer
    queryset = Product.objects
    use_native_json = True  # Use Zig json_dumps (200ns per dict)
```

This bypasses Python's `json.dumps` entirely. The native serializer handles
dicts, lists, strings, ints, floats, bools, None, Decimal, datetime, date,
time, and UUID.

Performance: ~200 ns per dict vs ~2 us for Python stdlib (10x faster).

---

## Connection Pool Tuning

pg.zig maintains a connection pool with configurable parameters:

```python
app = HyperApp(
    database="postgres://localhost/mydb",
    pool_size=20,  # Base connections maintained
    pool_max_queries=50000,  # Recycle connection after N queries
    pool_timeout=30,  # Seconds to wait for a connection
    statement_timeout=30000,  # PostgreSQL statement_timeout (ms)
)
```

### Pool Auto-Tuner

The pool auto-tuner adjusts the pool size based on utilization:

```python
from hyperdjango.pool import PoolAutoTuner

tuner = PoolAutoTuner(
    pool=app.database.pool,
    min_size=5,
    max_size=50,
    scale_up_threshold=0.8,  # Scale up when 80% utilized
    scale_down_threshold=0.3,  # Scale down when 30% utilized
    cooldown_seconds=60,  # Wait between scaling events
)
tuner.start()
```

### Pool Health Monitoring

```python
from hyperdjango.pool import PoolHealthChecker

health = PoolHealthChecker(pool=app.database.pool)
status = health.check()
print(f"Active: {status.active_connections}")
print(f"Idle: {status.idle_connections}")
print(f"Utilization: {status.utilization:.1%}")
```

---

## Profiling

### Per-Route Profiling

Profile individual routes with the `@app.profile` decorator:

```python
@app.get("/api/products")
@app.profile
async def list_products(request):
    products = await Product.objects.filter(active=True).all()
    return Response.json([p.to_dict() for p in products])
```

The response includes an `X-Profile` header:

```
X-Profile: total=1.2ms handler=0.8ms sql=0.3ms(2q) middleware=0.1ms
```

### Global Profiling

Enable profiling for all routes:

```python
app.profiling = True
```

### Programmatic Profiling

```python
from hyperdjango.profiling import nanos, elapsed_nanos

start = nanos()
# ... expensive operation ...
elapsed = elapsed_nanos(start)
print(f"Operation took {elapsed / 1_000_000:.2f}ms")
```

The profiler uses Zig `std.time.nanoTimestamp()` for sub-microsecond accuracy.

---

## Slow Query Logging

Track and log queries that exceed a time threshold:

```python
from hyperdjango.pool import SlowQueryLog, QueryTimer

slow_log = SlowQueryLog(db, threshold_ms=100)
await slow_log.ensure_table()

# QueryTimer wraps database operations and records slow queries
timer = QueryTimer(db, slow_log=slow_log)
```

The slow query log is stored in a PostgreSQL UNLOGGED table for fast writes.
Query it to find optimization targets:

```python
recent_slow = await slow_log.recent(limit=20)
for entry in recent_slow:
    print(f"{entry.duration_ms:.0f}ms: {entry.sql[:80]}")
```

---

## Admin Acceleration

HyperAdmin automatically optimizes its database queries:

- **Auto-prefetch**: All foreign key and many-to-many fields displayed in
  list views are automatically `select_related` or `prefetch_related`
- **Search indexes**: `varchar_pattern_ops` indexes are auto-created for
  `search_fields` to accelerate prefix matching
- **FK autocomplete**: Foreign key dropdowns use autocomplete with indexed
  queries instead of loading all records

These optimizations yield 17.7x faster changelist rendering and 41x fewer
queries compared to naive approaches.

---

## Production Checklist

Before deploying to production, verify these settings:

```bash
uv run hyper doctor
```

The doctor checks 30+ items across 7 categories. Key performance checks:

1. **Build mode**: Ensure the native extension is built with `--release`
   (ReleaseFast, not Debug)
2. **Pool size**: Match to your expected concurrent connections
3. **Debug mode**: Disable `app.debug` in production
4. **Cache backend**: Use `DatabaseCache` (not `LocMemCache`) for multi-server
5. **Statement timeout**: Set `statement_timeout` to prevent runaway queries
6. **Profiling**: Disable `app.profiling` in production unless actively debugging
7. **Query cache**: Enable for read-heavy endpoints

```python
# Production configuration example
app = HyperApp(
    title="My App",
    database="postgres://user:pass@db-host/mydb",
    templates="templates",
    debug=False,
    pool_size=20,
    pool_max_queries=50000,
    statement_timeout=30000,
)

# Enable caching
configure_query_cache(
    backend=DatabaseCache(app.database),
    default_ttl=60,
)

# Enable performance monitoring (low overhead)
perf = PerformanceMiddleware(
    slow_query_threshold_ms=200,
    n_plus_one_threshold=10,
    enabled=True,
)
app.use(perf)
```
