# Connection Pool Optimization

Slow query logging, query timing, connection health checks, and graceful drain for production database pool management.

## Quick Start

```python
from hyperdjango.pool import SlowQueryLog, QueryTimer, PoolHealthChecker

db = Database("postgres://localhost/mydb")
await db.connect()

# Slow query logging (persistent)
slow_log = SlowQueryLog(db, threshold_ms=100)
await slow_log.ensure_table()

# Auto-timing (wraps db.query/execute with timing)
timer = QueryTimer(db, slow_log=slow_log, threshold_ms=100)
timer.install()

# Health checks
checker = PoolHealthChecker(db, interval_seconds=30)
await checker.check()  # Manual check
checker.start()  # Background periodic checks

# Graceful drain before shutdown
success = await timer.drain(timeout_seconds=30)
```

## SlowQueryLog

Persistent slow query log backed by a PostgreSQL UNLOGGED table. Records queries exceeding a threshold for offline analysis.

### Constructor

```python
slow_log = SlowQueryLog(db, threshold_ms=100.0)
```

| Parameter      | Type       | Default  | Description                         |
| -------------- | ---------- | -------- | ----------------------------------- |
| `db`           | `Database` | required | Database connection                 |
| `threshold_ms` | `float`    | `100.0`  | Queries slower than this are logged |

### ensure_table()

Create the `hyper_slow_queries` table and indexes:

```python
await slow_log.ensure_table()
```

Attempts UNLOGGED first. Falls back to a regular table if UNLOGGED is not supported (e.g., some test environments). Safe to call multiple times (`IF NOT EXISTS`).

### record()

Record a slow query. Only stores if `duration_ms >= threshold_ms`:

```python
await slow_log.record(
    "SELECT * FROM users WHERE email = $1",
    duration_ms=250.5,
    params=["alice@example.com"],
)
```

| Parameter     | Type           | Description                               |
| ------------- | -------------- | ----------------------------------------- |
| `sql`         | `str`          | SQL query text (truncated to 2000 chars)  |
| `duration_ms` | `float`        | Query execution time in milliseconds      |
| `params`      | `list \| None` | Query parameters (truncated to 500 chars) |

Recording failures are silently suppressed to avoid breaking the application.

### get_recent()

Get the most recent slow queries:

```python
recent = await slow_log.get_recent(limit=50)
# Returns list of row dicts with: id, sql_text, duration_ms, params_summary, timestamp
```

### get_slowest()

Get the slowest queries ever recorded:

```python
slowest = await slow_log.get_slowest(limit=20)
```

### get_stats()

Get aggregate statistics across all recorded slow queries:

```python
stats = await slow_log.get_stats()
# Returns: {"total": 42, "avg_ms": 150.3, "max_ms": 2500.0, "min_ms": 100.1}
```

| Field    | Type    | Description                                 |
| -------- | ------- | ------------------------------------------- |
| `total`  | `int`   | Total number of slow queries recorded       |
| `avg_ms` | `float` | Average duration in milliseconds            |
| `max_ms` | `float` | Slowest query duration                      |
| `min_ms` | `float` | Fastest query that still exceeded threshold |

### cleanup()

Delete entries older than N days:

```python
await slow_log.cleanup(days=7)
```

Run this periodically (e.g., daily cron or background task) to prevent unbounded table growth.

### count Property

Number of slow queries recorded in the current process session:

```python
print(slow_log.count)  # 42
```

This is an in-memory counter, not a database query.

### Table Schema

```sql
CREATE UNLOGGED TABLE hyper_slow_queries (
    id SERIAL PRIMARY KEY,
    sql_text TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    params_summary TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_slow_ts ON hyper_slow_queries (timestamp DESC);
CREATE INDEX idx_slow_dur ON hyper_slow_queries (duration_ms DESC);
```

UNLOGGED tables skip WAL writes for fast inserts. Data survives process restarts but not crashes (acceptable for diagnostic logs).

## QueryTimer

Wraps `Database.query()` and `Database.execute()` with automatic timing. Records slow queries and tracks in-flight query count for graceful drain.

### Constructor

```python
timer = QueryTimer(
    db,
    slow_log=slow_log,  # Optional: persistent slow query log
    threshold_ms=100.0,  # Threshold for slow query recording
)
```

| Parameter      | Type                   | Default  | Description                                 |
| -------------- | ---------------------- | -------- | ------------------------------------------- |
| `db`           | `Database`             | required | Database connection to instrument           |
| `slow_log`     | `SlowQueryLog \| None` | `None`   | Persistent log for slow queries             |
| `threshold_ms` | `float`                | `100.0`  | Queries exceeding this are recorded as slow |

### install()

Patches `db.query` and `db.execute` with timed wrappers:

```python
timer.install()
```

After installation, every query through the database connection is automatically timed. Slow queries are recorded to both the `PerformanceMiddleware` (if configured) and the `SlowQueryLog`.

Safe to call multiple times -- subsequent calls are no-ops.

### uninstall()

Remove timing patches. Useful for testing:

```python
timer.uninstall()
```

Restores the original `query` and `execute` methods on the database object.

### get_stats()

Get query timing statistics:

```python
stats = timer.get_stats()
```

Returns:

| Field           | Type    | Description                           |
| --------------- | ------- | ------------------------------------- |
| `total_queries` | `int`   | Total queries executed since install  |
| `total_time_ms` | `float` | Cumulative query time in milliseconds |
| `avg_query_ms`  | `float` | Average query time                    |
| `in_flight`     | `int`   | Queries currently executing           |
| `threshold_ms`  | `float` | Slow query threshold                  |

Example output:

```python
{
    "total_queries": 1500,
    "total_time_ms": 4523.45,
    "avg_query_ms": 3.02,
    "in_flight": 2,
    "threshold_ms": 100.0,
}
```

### In-Flight Tracking

Thread-safe counter of currently executing queries:

```python
print(timer.in_flight)  # 2
```

Protected by a threading lock for free-threaded Python (3.14t) safety.

### Graceful Drain

Wait for all in-flight queries to complete before shutdown:

```python
success = await timer.drain(timeout_seconds=30.0)
if success:
    print("All queries completed")
else:
    print(f"Timeout: {timer.in_flight} queries still in flight")

await db.close()
```

The drain polls every 50ms until either all queries finish or the timeout expires. Returns `True` if drained successfully, `False` if timed out.

## PoolHealthChecker

Periodic connection pool health validation. Runs `SELECT 1` to verify connections are alive.

### Constructor

```python
checker = PoolHealthChecker(
    db,
    interval_seconds=30.0,  # Background check interval
)
```

| Parameter          | Type       | Default  | Description                       |
| ------------------ | ---------- | -------- | --------------------------------- |
| `db`               | `Database` | required | Database connection to check      |
| `interval_seconds` | `float`    | `30.0`   | Seconds between background checks |

### check()

Run a manual health check:

```python
healthy = await checker.check()  # Returns True if SELECT 1 succeeds
```

Updates internal stats on each call (success/failure count, last check time).

### start() / stop()

Start or stop background periodic health checks:

```python
checker.start()  # Starts asyncio background task
# ... application runs ...
checker.stop()  # Cancels background task
```

The background task runs `check()` every `interval_seconds`. Exceptions are caught and recorded as failures without crashing the application.

### get_stats()

Get health check statistics:

```python
stats = checker.get_stats()
```

Returns:

| Field              | Type            | Description                                        |
| ------------------ | --------------- | -------------------------------------------------- |
| `healthy`          | `bool`          | Last check result                                  |
| `checks`           | `int`           | Total checks run                                   |
| `failures`         | `int`           | Failed checks                                      |
| `last_check_ago_s` | `float \| None` | Seconds since last check (`None` if never checked) |

Example:

```python
{
    "healthy": True,
    "checks": 42,
    "failures": 1,
    "last_check_ago_s": 15.3,
}
```

## Production Setup

A complete production setup combining all pool optimization components:

```python
from hyperdjango.pool import SlowQueryLog, QueryTimer, PoolHealthChecker
from hyperdjango.database import Database


async def setup_pool(db: Database):
    # Persistent slow query log
    slow_log = SlowQueryLog(db, threshold_ms=50)
    await slow_log.ensure_table()

    # Auto-timing with slow query recording
    timer = QueryTimer(db, slow_log=slow_log, threshold_ms=50)
    timer.install()

    # Background health checks every 30 seconds
    checker = PoolHealthChecker(db, interval_seconds=30)
    checker.start()

    return timer, checker


async def shutdown(timer: QueryTimer, checker: PoolHealthChecker):
    # Stop health checks
    checker.stop()

    # Wait for in-flight queries
    await timer.drain(timeout_seconds=30)

    # Now safe to close the pool
    # await db.close()
```

## Pool Stats Dashboard Pattern

Expose pool stats via an API endpoint for monitoring:

```python
@app.get("/admin/pool-stats")
async def pool_stats(request):
    return {
        "timer": timer.get_stats(),
        "health": checker.get_stats(),
        "slow_queries": await slow_log.get_stats(),
        "recent_slow": await slow_log.get_recent(limit=10),
    }
```

This gives you a single endpoint for any monitoring tool to scrape.

## Connection Lifecycle

Understanding the connection lifecycle helps with pool tuning:

1. **Startup**: `await db.connect()` creates the connection pool with `min_connections` idle connections
2. **Acquire**: Each query acquires a connection from the pool. If none are idle, a new one is created up to `max_connections`
3. **Execute**: The query runs on the acquired connection with optional `statement_timeout`
4. **Release**: The connection returns to the pool for reuse
5. **Health check**: Periodic `SELECT 1` validates idle connections
6. **Drain**: On shutdown, wait for in-flight queries, then close all connections

## Pool Sizing Guide

### OLTP (Web Applications)

High-concurrency, short queries:

```python
db = Database(
    "postgres://localhost/mydb",
    min_connections=5,
    max_connections=20,
)
timer = QueryTimer(db, threshold_ms=50)  # Flag queries over 50ms
```

### OLAP (Analytics, Reports)

Low-concurrency, long-running queries:

```python
db = Database(
    "postgres://localhost/analytics",
    min_connections=2,
    max_connections=5,
)
timer = QueryTimer(db, threshold_ms=5000)  # 5-second threshold
```

### Mixed Workloads

Use separate pools for different workload types:

```python
web_db = Database("postgres://localhost/mydb", max_connections=20)
report_db = Database("postgres://localhost/mydb", max_connections=5)

web_timer = QueryTimer(web_db, threshold_ms=50)
report_timer = QueryTimer(report_db, threshold_ms=5000)
```

### General Sizing Formula

A good starting point: `max_connections = (2 * CPU_cores) + effective_spindle_count`. For SSDs, treat effective spindles as 200. For most web apps, 10-20 connections is sufficient.

## Statement Timeout Configuration

Set a per-connection statement timeout via PostgreSQL startup parameters to prevent runaway queries:

```python
db = Database(
    "postgres://localhost/mydb",
    statement_timeout=30000,  # 30 seconds in milliseconds
)
```

This sets `statement_timeout` at the PostgreSQL session level. Queries exceeding the timeout are cancelled by PostgreSQL.

## Connection Warm-up

Use prepared statement warmup to reduce first-query latency by 7.7x (494us to 65us):

```python
await db.warmup(
    [
        "SELECT * FROM users WHERE id = $1",
        "SELECT * FROM products WHERE category = $1 ORDER BY price LIMIT $2",
        "INSERT INTO orders (user_id, product_id, quantity) VALUES ($1, $2, $3)",
    ]
)
```

Warmup sends `PARSE` messages for common queries so the first execution skips the parse phase.

## Integration with PerformanceMiddleware

When `PerformanceMiddleware` is configured, `QueryTimer` automatically feeds query timing data to it. This enables the performance dashboard to show per-request query counts and timings without additional setup.

```python
from hyperdjango.performance import PerformanceMiddleware

app.use(PerformanceMiddleware())

# QueryTimer.install() will automatically detect and feed data to it
timer = QueryTimer(db, slow_log=slow_log)
timer.install()
```

## Monitoring Integration

### Prometheus

Export pool metrics in Prometheus format:

```python
@app.get("/metrics")
async def metrics(request):
    t = timer.get_stats()
    h = checker.get_stats()
    s = await slow_log.get_stats()

    lines = [
        f"db_queries_total {t['total_queries']}",
        f"db_query_time_ms_total {t['total_time_ms']}",
        f"db_query_time_ms_avg {t['avg_query_ms']}",
        f"db_queries_in_flight {t['in_flight']}",
        f"db_slow_queries_total {s['total']}",
        f"db_slow_query_max_ms {s['max_ms']}",
        f"db_pool_healthy {1 if h['healthy'] else 0}",
        f"db_health_checks_total {h['checks']}",
        f"db_health_failures_total {h['failures']}",
    ]
    return Response.text(
        "\n".join(lines),
        headers={
            "Content-Type": "text/plain; version=0.0.4",
        },
    )
```

### Alerting Thresholds

Suggested alert thresholds for production monitoring:

| Metric                       | Warning                  | Critical          |
| ---------------------------- | ------------------------ | ----------------- |
| `avg_query_ms`               | > 50ms                   | > 200ms           |
| `in_flight`                  | > max_connections \* 0.8 | = max_connections |
| `slow_queries` (per hour)    | > 50                     | > 200             |
| `health_failures` (per hour) | > 1                      | > 5               |
| `last_check_ago_s`           | > 120s                   | > 300s            |

## Pool Auto-Tuning

Dynamic connection pool sizing based on load metrics. The `PoolAutoTuner` monitors pool utilization and makes conservative scaling recommendations with hysteresis to prevent flapping.

### Constructor

```python
from hyperdjango.pool import PoolAutoTuner

tuner = PoolAutoTuner(
    db,
    check_interval=10,  # Seconds between samples
    scale_up_threshold=0.8,  # Utilization above this triggers scale-up
    scale_down_threshold=0.3,  # Utilization below this triggers scale-down
    cooldown_periods=6,  # Consecutive low samples before scale-down
    scale_step=2,  # Connections to add/remove per adjustment
)
```

| Parameter              | Type       | Default  | Description                                                    |
| ---------------------- | ---------- | -------- | -------------------------------------------------------------- |
| `db`                   | `Database` | required | Database connection to monitor                                 |
| `check_interval`       | `int`      | `10`     | Seconds between utilization samples                            |
| `scale_up_threshold`   | `float`    | `0.8`    | Utilization ratio that triggers scale-up                       |
| `scale_down_threshold` | `float`    | `0.3`    | Utilization ratio that triggers scale-down                     |
| `cooldown_periods`     | `int`      | `6`      | Consecutive low-utilization samples required before scale-down |
| `scale_step`           | `int`      | `2`      | Number of connections to add or remove                         |

### Scaling Signals

- **Scale up**: utilization exceeds `scale_up_threshold`, or available connections reach 0, or thread-owned slots exceed 75% of 64
- **Scale down**: utilization stays below `scale_down_threshold` for `cooldown_periods` consecutive checks with no recent scale-up

### start() / stop()

Start or stop the background monitoring task:

```python
tuner.start()  # Starts asyncio background task
# ... application runs ...
tuner.stop()  # Cancels background task
```

Safe to call `start()` multiple times -- subsequent calls are no-ops.

### stats()

Get auto-tuner statistics:

```python
stats = tuner.stats()
```

Returns:

| Field                         | Type                            | Description                                 |
| ----------------------------- | ------------------------------- | ------------------------------------------- |
| `running`                     | `bool`                          | Whether the tuner is active                 |
| `check_interval`              | `int`                           | Configured check interval                   |
| `total_samples`               | `int`                           | Total utilization samples collected         |
| `scale_up_recommendations`    | `int`                           | Times scale-up was recommended              |
| `scale_down_recommendations`  | `int`                           | Times scale-down was recommended            |
| `consecutive_low_utilization` | `int`                           | Current streak of low-utilization samples   |
| `recent_samples`              | `list[dict[str, int \| float]]` | Last 10 samples with utilization and action |

### recommendation()

Get the current scaling recommendation based on the last 3 samples:

```python
rec = tuner.recommendation()
# Returns: "scale_up", "scale_down", "hold", or "insufficient_data"
```

- `"scale_up"` -- 2 or more of the last 3 samples recommended scale-up
- `"scale_down"` -- 2 or more of the last 3 samples recommended scale-down
- `"hold"` -- no clear signal in either direction
- `"insufficient_data"` -- no samples collected yet

### is_saturated

Check if the pool needs immediate attention:

```python
if tuner.is_saturated:
    alert("Connection pool exhausted!")
```

Returns `True` when available connections reach 0 or utilization exceeds 95%.

### utilization_history

Recent utilization values for trend analysis:

```python
history = tuner.utilization_history  # list[float], last 20 values
```

### Hysteresis and Cooldown

The auto-tuner uses hysteresis to prevent rapid flip-flopping between scale-up and scale-down:

1. **Scale-up is immediate**: any single sample above the threshold triggers a recommendation
2. **Scale-down requires sustained low utilization**: the utilization must stay below `scale_down_threshold` for `cooldown_periods` consecutive checks (default: 6 checks = 60 seconds at the default interval)
3. **Any scale-up event resets the cooldown counter**: a single spike during the cooldown period restarts the countdown

This means the pool scales up aggressively to handle load spikes but scales down conservatively to avoid thrashing.

### Usage Example

```python
from hyperdjango.pool import PoolAutoTuner

db = Database("postgres://localhost/mydb", min_size=2, max_size=20)
await db.connect()

tuner = PoolAutoTuner(db, check_interval=10)
tuner.start()


# Monitor via API endpoint
@app.get("/admin/pool-tuner")
async def pool_tuner_stats(request):
    return {
        "stats": tuner.stats(),
        "recommendation": tuner.recommendation(),
        "saturated": tuner.is_saturated,
        "history": tuner.utilization_history,
    }


# On shutdown
tuner.stop()
```
