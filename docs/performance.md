# Performance

Optimization strategies and benchmarking for HyperDjango applications.

## Architecture

HyperDjango is built for performance at every layer:

```
Request → Zig HTTP Server (24 threads) → Zig Router (808ns) → Python View
    ↓
Zig pg.zig Driver → PostgreSQL (prepared stmt cache, connection pool)
    ↓
Zig Template Engine (41μs render) → Zig Response Builder → Response
```

## Benchmarks

### Database (pg.zig vs psycopg3)

| Operation        | pg.zig        | psycopg3     | Speedup   |
| ---------------- | ------------- | ------------ | --------- |
| SELECT by PK     | 21K ops/sec   | 10K ops/sec  | **2.06x** |
| SELECT range     | 4.18x faster  | baseline     | **4.18x** |
| UPDATE           | 1.52x faster  | baseline     | **1.52x** |
| COPY bulk import | 536K rows/sec | 12K rows/sec | **42.8x** |

### Validation (native Zig)

| Operation                   | Throughput      | Latency |
| --------------------------- | --------------- | ------- |
| Model creation              | 1.6M models/sec | 0.6 μs  |
| Batch int validation (SIMD) | 51.5M ints/sec  | —       |
| Email validation (SIMD)     | —               | 77 ns   |
| Model→JSON                  | 4.2M models/sec | 238 ns  |

### Server

| Component                | Metric                                                                                                   |
| ------------------------ | -------------------------------------------------------------------------------------------------------- |
| HTTP server              | 2.1x faster than uvicorn (13K vs 6K req/s, 18-core); 2.3x at scale (479-548K vs 239K req/s, 64-core pin) |
| Route resolution         | 808ns per resolve                                                                                        |
| Template render (cached) | 36μs                                                                                                     |
| Template compile         | 7.1μs (234x faster than Jinja2)                                                                          |
| JSON parse (SIMD)        | 6-10x faster than stdlib                                                                                 |

## Database Optimization

### Use select_related for FOINs

```python
# BAD: N+1 queries
articles = await Article.objects.all()
for a in articles:
    author = await Author.objects.get(id=a.author_id)  # 1 query per article!

# GOOD: Single JOIN query
articles = await Article.objects.select_related("author").all()
for a in articles:
    print(a.author.name)  # already loaded
```

### Use prefetch_related for Reverse/M2M

```python
# GOOD: 2 queries instead of N+1
authors = await Author.objects.prefetch_related("articles").all()
```

### Use values() When You Don't Need Full Objects

```python
# Returns dicts instead of model instances — less memory, faster
names = await User.objects.values("id", "name").all()
# [{"id": 1, "name": "Alice"}, ...]
```

### Use count() Instead of len()

```python
# BAD: loads all objects into memory
count = len(await Article.objects.all())

# GOOD: COUNT(*) in SQL
count = await Article.objects.count()
```

### Use exists() Instead of count() > 0

```python
# BAD
if await Article.objects.count() > 0:
    ...

# GOOD: SELECT 1 ... LIMIT 1
if await Article.objects.filter(published=True).exists():
    ...
```

### Use F() for Atomic Updates

```python
# BAD: race condition
article = await Article.objects.get(id=1)
article.views += 1
await article.save()

# GOOD: atomic SQL UPDATE
await Article.objects.filter(id=1).update(views=F("views") + 1)
```

### Use bulk_create for Batch Inserts

```python
# BAD: N individual INSERTs
for data in items:
    await Item.objects.create(**data)

# GOOD: Single INSERT with multiple VALUES
items = [Item(**data) for data in items_data]
# Or use COPY for maximum throughput
await db.copy_in("items", columns, rows)
```

## Caching

### Per-Model Query Cache

```python
class Article(Model):
    class Meta:
        table = "articles"
        cache_ttl = 60  # Cache all queries for 60 seconds
```

### View-Level Caching

```python
from hyperdjango.cache import cached


@app.get("/expensive")
@cached(ttl=300)
async def expensive_view(request):
    return await compute_expensive_data()
```

### Two-Tier Cache

```python
from hyperdjango.cache import LocMemCache, DatabaseCache
from hyperdjango.cache_adapters import TwoTierCache

l1 = LocMemCache(max_entries=1000)  # Fast, per-process
l2 = DatabaseCache(db, table="cache")  # Shared, persistent
cache = TwoTierCache(l1=l1, l2=l2)

# L1 hit: ~1μs, L2 hit: ~100μs, miss: compute + write both
await cache.get("key")
await cache.set("key", value, ttl=300)
```

## Profiling

### Built-in Profiler

```python
from hyperdjango.profiling import profile_handler


@app.get("/api/data")
@profile_handler
async def get_data(request): ...


# Response includes X-Profile header with timing breakdown
```

### Performance Dashboard

```python
from hyperdjango.performance import PerformanceMiddleware

app.use(
    PerformanceMiddleware(
        slow_query_threshold_ms=100,
        track_n_plus_1=True,
    )
)
# Dashboard at /debug/performance/
```

### Database Query Tracking

```python
from hyperdjango.pool import QueryTimer, SlowQueryLog

# Auto-time all queries
timer = QueryTimer(db)

# Log slow queries to UNLOGGED table
slow_log = SlowQueryLog(db, threshold_ms=100)
```

## Connection Pool Tuning

```python
# Match pool size to expected concurrent queries
db = Database("postgres://localhost/mydb?pool_size=20")

# Monitor pool health
from hyperdjango.pool import PoolHealthChecker

checker = PoolHealthChecker(db)
stats = checker.get_stats()
# {"healthy": True, "checks": 10, "failures": 0, "last_check_ago_s": 5.2}
```

### Background Health Heartbeat

The `PoolHeartbeat` runs a lightweight `SELECT 1` at configurable intervals to detect dead connections, network partitions, and server restarts. Tracks latency percentiles and consecutive failure counts for alerting.

```python
from hyperdjango.pool import PoolHeartbeat

heartbeat = PoolHeartbeat(
    db,
    interval_seconds=15,  # Check every 15 seconds
    failure_threshold=3,  # 3 consecutive failures → unhealthy
    latency_window=100,  # Track last 100 latency samples
)
heartbeat.start()

# Query health status
stats = heartbeat.stats()
# HeartbeatStats(
#     running=True, healthy=True,
#     total_beats=42, total_failures=0, consecutive_failures=0,
#     avg_latency_ms=0.25, p99_latency_ms=1.2,
#     uptime_ratio=1.0,
# )

# Dict-based stats for metrics integration
heartbeat.get_stats()
# {"healthy": True, "checks": 42, "failures": 0,
#  "avg_latency_ms": 0.25, "p99_latency_ms": 1.2, "uptime_ratio": 1.0}

# Register with Prometheus metrics
from hyperdjango.metrics import register_health_checker

register_health_checker(heartbeat)
```

## Template Performance

The Zig template engine caches compiled templates in a thread-safe LRU cache:

- **First render**: 7μs compile + 41μs render
- **Cached render**: 41μs (compile skipped)
- **Cache size**: 256MB default (configurable)

## Static File Performance

Content-hash manifested files enable aggressive caching:

```python
from hyperdjango.staticfiles import StaticFilesMiddleware

app.use(
    StaticFilesMiddleware(
        static_dir="staticfiles",
        max_age=31536000,  # 1 year
        immutable=True,  # Cache-Control: immutable
        gzip=True,  # Serve pre-compressed
    )
)
```

## Performance Regression Testing

The `hyper benchmark` command runs EXPLAIN ANALYZE queries against a seeded database to detect performance regressions before they reach production.

### Quick Start

```bash
# Seed 50K posts + run 14 benchmark queries
hyper benchmark

# Save baseline for future comparison
hyper benchmark --save-baseline

# Detect regressions (fails if any query > 2x baseline)
hyper benchmark --threshold 2.0

# JSON output for CI pipelines
hyper benchmark --json
```

### What It Tests

14 queries covering critical access patterns:

- **Front page sorts**: hot, new, top, controversial, rising — all must use indexes
- **Single record**: post by PK, vote check — index lookup
- **Pagination**: keyset pagination with composite ordering
- **Aggregation**: author scores, post counts
- **Search**: ILIKE title search
- **Joins**: post + author join

### Regression Detection

The tool compares current results against a `.hyper.benchmark.json` baseline:

- **Timing regression**: query exceeds baseline \* threshold (default 2x) -> FAIL
- **New sequential scan**: index scan regressed to seq scan -> FAIL
- **Improvement**: query < baseline \* 0.5 -> reported

### Query Plan Analysis

Use `db.explain()` directly for ad-hoc query plan analysis:

```python
result = await db.explain(
    "SELECT * FROM posts ORDER BY hot_score DESC LIMIT 30",
    analyze=True,
    buffers=True,
)

# Structured plan access
print(result.execution_time)  # 0.042 ms
print(result.has_seq_scan)  # False
print(result.plan.node_type)  # "Limit"
print(result.index_scans)  # [ExplainNode(index_name="idx_posts_hot")]

# Assert in tests
assert not result.has_seq_scan, f"Seq scan on: {result.seq_scan_tables}"
assert result.execution_time < 5.0, f"Too slow: {result.execution_time}ms"
```

See [Database](database.md) for full `db.explain()` API reference and [CLI Commands](cli.md) for all benchmark options.
