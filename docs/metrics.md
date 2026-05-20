# Metrics

Prometheus-compatible metrics export for monitoring HyperDjango applications in production.

## Quick Start

```python
from hyperdjango import HyperApp, MetricsMiddleware

app = HyperApp("myapp")
app.use(MetricsMiddleware())

# GET /metrics → Prometheus scrape endpoint
```

## Exposed Metrics

### Process

| Metric                                   | Type  | Description                        |
| ---------------------------------------- | ----- | ---------------------------------- |
| `hyperdjango_info`                       | gauge | Build info labels: version, python |
| `hyperdjango_uptime_seconds`             | gauge | Process uptime in seconds          |
| `hyperdjango_process_start_time_seconds` | gauge | Unix timestamp of process start    |

### HTTP Requests

| Metric                                      | Type      | Description                                      |
| ------------------------------------------- | --------- | ------------------------------------------------ |
| `hyperdjango_http_requests_total`           | counter   | Total requests, labeled by `method` and `status` |
| `hyperdjango_http_request_duration_seconds` | histogram | Request latency (12 buckets: 1ms to 10s)         |
| `hyperdjango_http_requests_in_flight`       | gauge     | Requests currently being processed               |

### Database Pool

| Metric                             | Type  | Description               |
| ---------------------------------- | ----- | ------------------------- |
| `hyperdjango_db_pool_total`        | gauge | Total connections in pool |
| `hyperdjango_db_pool_available`    | gauge | Idle connections          |
| `hyperdjango_db_pool_in_use`       | gauge | Active connections        |
| `hyperdjango_db_pool_thread_owned` | gauge | Thread-pinned connections |

### Prepared Statement Cache

| Metric                                   | Type    | Description                               |
| ---------------------------------------- | ------- | ----------------------------------------- |
| `hyperdjango_stmt_cache_hits_total`      | counter | Global cache hits                         |
| `hyperdjango_stmt_cache_misses_total`    | counter | Global cache misses                       |
| `hyperdjango_stmt_cache_evictions_total` | counter | Evictions (DEALLOCATE sent to PostgreSQL) |
| `hyperdjango_stmt_cache_entries`         | gauge   | Current entries in cache                  |
| `hyperdjango_stmt_cache_hit_rate`        | gauge   | Hit rate (0.0 to 1.0)                     |

### Query Performance

Requires `PerformanceMiddleware` to be active:

| Metric                                   | Type    | Description                      |
| ---------------------------------------- | ------- | -------------------------------- |
| `hyperdjango_db_queries_total`           | counter | Total queries executed           |
| `hyperdjango_db_slow_queries_total`      | counter | Queries exceeding slow threshold |
| `hyperdjango_n_plus_one_total`           | counter | N+1 patterns detected            |
| `hyperdjango_db_avg_queries_per_request` | gauge   | Average queries per HTTP request |

### Pool Health

Requires `register_health_checker()`:

| Metric                                      | Type    | Description          |
| ------------------------------------------- | ------- | -------------------- |
| `hyperdjango_db_pool_healthy`               | gauge   | Pool healthy: 1 or 0 |
| `hyperdjango_db_pool_health_checks_total`   | counter | Total health checks  |
| `hyperdjango_db_pool_health_failures_total` | counter | Failed health checks |

### Query Timer

Requires `register_query_timer()`:

| Metric                                   | Type    | Description                 |
| ---------------------------------------- | ------- | --------------------------- |
| `hyperdjango_db_queries_in_flight`       | gauge   | Currently executing queries |
| `hyperdjango_db_query_duration_ms_total` | counter | Cumulative query time (ms)  |

## Configuration

### Custom Path

```python
app.use(MetricsMiddleware(metrics_path="/prom/metrics"))
```

### Disable Metrics

```python
app.use(MetricsMiddleware(enabled=False))
```

### Register Pool Monitoring

```python
from hyperdjango.metrics import register_query_timer, register_health_checker
from hyperdjango.pool import QueryTimer, PoolHealthChecker

timer = QueryTimer(db, threshold_ms=100)
timer.install()
register_query_timer(timer)

checker = PoolHealthChecker(db, interval_seconds=30)
checker.start()
register_health_checker(checker)
```

### Combined with PerformanceMiddleware

```python
from hyperdjango.performance import PerformanceMiddleware

# Order matters: MetricsMiddleware wraps PerformanceMiddleware
app.use(MetricsMiddleware())
app.use(PerformanceMiddleware(slow_query_threshold_ms=100))
```

## Prometheus Integration

### Scrape Config

```yaml
scrape_configs:
  - job_name: "hyperdjango"
    static_configs:
      - targets: ["localhost:8000"]
    metrics_path: "/metrics"
    scrape_interval: 15s
```

### Dashboard Queries (PromQL)

```promql
# Request rate
rate(hyperdjango_http_requests_total[5m])

# P99 latency
histogram_quantile(0.99, rate(hyperdjango_http_request_duration_seconds_bucket[5m]))

# Pool utilization
hyperdjango_db_pool_in_use / hyperdjango_db_pool_total

# Cache hit rate
hyperdjango_stmt_cache_hit_rate

# Error rate
sum(rate(hyperdjango_http_requests_total{status=~"5.."}[5m])) / sum(rate(hyperdjango_http_requests_total[5m]))
```

## Programmatic Access

```python
from hyperdjango.metrics import collect_metrics

# Get all metrics as Prometheus text format
text = collect_metrics()
print(text)
```

### Direct Metric Access

```python
from hyperdjango.metrics import (
    http_requests_total,
    http_request_duration,
    http_requests_in_flight,
)

# Observe a value
http_request_duration.observe(0.042)

# Increment a counter
http_requests_total.inc(("GET", "200"))

# Read gauge
print(http_requests_in_flight.value)
```

## Output Format

The `/metrics` endpoint returns Prometheus text exposition format (version 0.0.4):

```
# HELP hyperdjango_info HyperDjango build information.
# TYPE hyperdjango_info gauge
hyperdjango_info{version="0.1.0",python="3.14.0a2"} 1

# HELP hyperdjango_uptime_seconds Process uptime in seconds.
# TYPE hyperdjango_uptime_seconds gauge
hyperdjango_uptime_seconds 3621.042

# HELP hyperdjango_http_requests_total Total HTTP requests by method and status code.
# TYPE hyperdjango_http_requests_total counter
hyperdjango_http_requests_total{method="GET",status="200"} 15234
hyperdjango_http_requests_total{method="POST",status="201"} 892

# HELP hyperdjango_http_request_duration_seconds HTTP request duration in seconds.
# TYPE hyperdjango_http_request_duration_seconds histogram
hyperdjango_http_request_duration_seconds_bucket{le="0.001"} 4521
hyperdjango_http_request_duration_seconds_bucket{le="0.005"} 9832
...
```

Response headers:

- `Content-Type: text/plain; version=0.0.4; charset=utf-8`
- `Cache-Control: no-store`
