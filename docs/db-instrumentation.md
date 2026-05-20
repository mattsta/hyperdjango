# Database Instrumentation

HyperDjango provides built-in tools for monitoring database performance: per-request query tracking, slow query detection, N+1 pattern alerts, connection pool health checks, and request profiling with nanosecond precision.

## PerformanceMiddleware

The central component for query monitoring. It tracks every SQL query per request, detects slow queries and N+1 patterns, and exposes a dashboard.

```python
from hyperdjango.performance import PerformanceMiddleware

perf = PerformanceMiddleware(
    slow_query_threshold_ms=100,  # Queries over 100ms are flagged (default: 100)
    n_plus_one_threshold=5,  # Same SQL repeated 5+ times = N+1 alert (default: 5)
    max_history=1000,  # Ring buffer size for request stats (default: 1000)
    dashboard_path="/debug/performance",  # Dashboard URL (default)
)
app.use(perf)
```

### Response Headers

Every response includes instrumentation headers:

```
X-Query-Count: 12
X-Query-Time: 45.2ms
```

These headers tell you at a glance how many queries a request executed and how long they took in total.

### Recording Queries

The middleware hooks into the database layer. Each query execution is recorded with:

- The normalized SQL text
- Duration in milliseconds
- Timestamp

```python
# The middleware records queries automatically. For manual recording:
perf.record_query("SELECT * FROM users WHERE id = $1", duration_ms=2.3)
```

### Aggregate Statistics

Access aggregate stats programmatically:

```python
stats = perf.get_stats()
# Returns:
# {
#     "total_requests": 15432,
#     "total_queries": 89201,
#     "avg_queries_per_request": 5.8,
#     "slow_query_count": 23,
#     "n_plus_one_count": 7,
#     "recent_slow_queries": [
#         {"sql": "SELECT ... FROM orders WHERE ...", "duration_ms": 234.5},
#         ...
#     ],
#     "recent_n_plus_one": [
#         "SELECT * FROM order_items WHERE order_id = $1",
#         ...
#     ],
# }
```

## Slow Query Detection

### PerformanceMiddleware Threshold

Queries exceeding `slow_query_threshold_ms` are flagged and tracked in the recent history. The default threshold is 100ms.

```python
perf = PerformanceMiddleware(slow_query_threshold_ms=50)  # Flag queries over 50ms
```

### SlowQueryLog (Persistent)

For production, use `SlowQueryLog` to persist slow queries in a PostgreSQL UNLOGGED table. This survives process restarts and provides a queryable history.

```python
from hyperdjango.pool import SlowQueryLog

slow_log = SlowQueryLog(db, threshold_ms=100)
await slow_log.ensure_table()

# The table created:
# CREATE UNLOGGED TABLE hyper_slow_queries (
#     id SERIAL PRIMARY KEY,
#     sql_text TEXT NOT NULL,
#     duration_ms REAL NOT NULL,
#     params_summary TEXT,
#     timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
# )
```

The UNLOGGED table avoids WAL overhead -- slow query logging does not add write-amplification to your production database.

Indexes are created on `timestamp DESC` and `duration_ms DESC` for efficient querying:

```sql
-- Find the 10 slowest queries in the last hour
SELECT sql_text, duration_ms, timestamp
FROM hyper_slow_queries
WHERE timestamp > NOW() - INTERVAL '1 hour'
ORDER BY duration_ms DESC
LIMIT 10;
```

## N+1 Query Detection

The `PerformanceMiddleware` detects N+1 patterns by normalizing SQL and counting repeated patterns within a single request. When the same query pattern appears more than `n_plus_one_threshold` times (default: 5), it is flagged.

```python
# This triggers an N+1 alert:
users = await User.objects.all()
for user in users:
    # Each iteration runs: SELECT * FROM orders WHERE user_id = $1
    orders = await Order.objects.filter(user_id=user.id).all()

# Fix with prefetch_related or select_related:
users = await User.objects.prefetch_related("orders").all()
```

N+1 patterns appear in:

- The performance dashboard
- `perf.get_stats()["recent_n_plus_one"]`
- The JSON stats endpoint at `{dashboard_path}/json`

## QueryTimer

`QueryTimer` wraps `db.query` and `db.execute` to automatically time every query and log slow ones.

```python
from hyperdjango.pool import QueryTimer, SlowQueryLog

slow_log = SlowQueryLog(db, threshold_ms=100)
await slow_log.ensure_table()

timer = QueryTimer(db, slow_log=slow_log, threshold_ms=100)
timer.install()  # Patches db.query and db.execute

# Now all queries are automatically timed.
# Slow queries are written to the hyper_slow_queries table.

# Graceful shutdown: wait for in-flight queries
await timer.drain(timeout_seconds=30)
```

## Performance Dashboard

The `PerformanceMiddleware` serves an HTML dashboard at the configured path (default: `/debug/performance`).

```
GET /debug/performance       -> HTML dashboard with charts and tables
GET /debug/performance/json  -> JSON stats for programmatic access
```

The dashboard shows:

- Total requests and queries
- Average queries per request
- Slow query count and list (sorted by duration)
- N+1 pattern detections
- Recent request timeline

### JSON Endpoint

```bash
curl http://localhost:8000/debug/performance/json
```

```json
{
  "total_requests": 15432,
  "total_queries": 89201,
  "avg_queries_per_request": 5.8,
  "slow_query_count": 23,
  "n_plus_one_count": 7,
  "recent_slow_queries": [...],
  "recent_n_plus_one": [...]
}
```

## Request Profiling

For detailed per-request breakdown, use the profiling system which provides nanosecond-precision timing via the native Zig extension.

```python
from hyperdjango.profiling import profile_handler


# Profile a single route
@app.route("GET", "/users")
@profile_handler
async def list_users(request):
    users = await User.objects.all()
    return Response.json([u.to_dict() for u in users])
```

Profiled requests include an `X-Profile` header with timing breakdown:

```
X-Profile: total=1.2ms handler=0.8ms sql=0.3ms(2q) middleware=0.1ms
```

### App-Wide Profiling

Enable profiling for all routes:

```python
app.profiling = True
```

This adds the `X-Profile` header to every response without code changes to individual handlers.

## Connection Pool Monitoring

### PoolHealthChecker

Periodically validates connections in the pool and removes dead ones.

```python
from hyperdjango.pool import PoolHealthChecker

checker = PoolHealthChecker(db, interval_seconds=30)

# Manual check
await checker.check()

# Background periodic checks
checker.start()

# Stop on shutdown
checker.stop()
```

### Pool Stats

The database connection pool exposes statistics for monitoring:

```python
stats = db.pool_stats()
# Returns pool utilization metrics:
# - total connections
# - idle connections
# - active (in-use) connections
# - wait queue depth
```

Use these stats in health check endpoints or monitoring dashboards.

## Putting It All Together

A production setup combining all instrumentation:

```python
from hyperdjango import HyperApp
from hyperdjango.database import Database
from hyperdjango.performance import PerformanceMiddleware
from hyperdjango.pool import SlowQueryLog, QueryTimer, PoolHealthChecker

app = HyperApp("myapp")


@app.on_startup
async def setup():
    db = Database("postgres://localhost/mydb")
    await db.connect()
    app.db = db

    # Persistent slow query log
    slow_log = SlowQueryLog(db, threshold_ms=100)
    await slow_log.ensure_table()

    # Auto-timing on all queries
    timer = QueryTimer(db, slow_log=slow_log, threshold_ms=100)
    timer.install()

    # Connection health checks every 30 seconds
    checker = PoolHealthChecker(db, interval_seconds=30)
    checker.start()


# Per-request query tracking + N+1 detection + dashboard
app.use(
    PerformanceMiddleware(
        slow_query_threshold_ms=100,
        n_plus_one_threshold=5,
        dashboard_path="/debug/performance",
    )
)

# Enable profiling in development
if app.debug:
    app.profiling = True
```

## See Also

- [Performance Guide](performance-guide.md) -- optimization strategies
- [Pool](pool.md) -- connection pool configuration and optimization
- [Profiling](profiling.md) -- detailed request profiling documentation
- [Database](database.md) -- connection pool, transactions, and query execution
- [Logging](logging.md) -- structured logging for query events
