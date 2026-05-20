# Deployment

Production deployment checklist and configuration for HyperDjango applications.

## Production Checklist

### 1. Build Release Binary

```bash
uv run hyper-build --install --release
```

The `--release` flag enables Zig's `ReleaseFast` optimizations — measurably faster request handling, JSON parsing, and template rendering.

### 2. Run in Production Mode

```python
app.run(host="0.0.0.0", port=8000, prod=True)
```

The `prod=True` flag:

- Validates configuration at startup (fails fast on misconfig)
- Installs SIGTERM/SIGINT handlers for graceful shutdown
- Disables debug error pages
- Checks for security headers middleware

### 3. Environment Configuration

```python
import os

app = HyperApp(
    "myapp",
    database=os.environ["DATABASE_URL"],
    templates="templates",
    static_dir="static",
)
```

**Critical environment variables:**

| Variable        | Purpose                                |
| --------------- | -------------------------------------- |
| `DATABASE_URL`  | PostgreSQL connection string           |
| `SECRET_KEY`    | CSRF token signing, session encryption |
| `ALLOWED_HOSTS` | Accepted Host header values            |
| `PORT`          | Server port (default 8000)             |

### 4. Security Middleware

Always enable in production:

```python
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
)

app.use(
    SecurityHeadersMiddleware(
        hsts=True,
        hsts_max_age=31536000,
        frame_options="DENY",
        content_type_nosniff=True,
        referrer_policy="strict-origin-when-cross-origin",
    )
)
app.use(CORSMiddleware(origins=["https://yourdomain.com"]))
app.use(CSRFMiddleware(secret=os.environ["SECRET_KEY"]))
```

### 5. Database Connection Pool

```python
app = HyperApp(
    "myapp",
    database="postgres://user:pass@host/db?pool_size=20&statement_timeout=30000",
)
```

Tune pool size based on your workload:

- **API servers**: 10-20 connections per worker
- **Background workers**: 2-5 connections
- **Mixed**: 15 connections

### 6. Static Files

Collect and serve static files with content-hash fingerprinting:

```bash
uv run hyper collectstatic
```

```python
from hyperdjango.staticfiles import StaticFilesMiddleware

app.use(
    StaticFilesMiddleware(
        static_dir="staticfiles",
        prefix="/static/",
        max_age=31536000,  # 1 year cache (fingerprinted files)
        immutable=True,  # Cache-Control: immutable
        gzip=True,  # Serve .gz variants
    )
)
```

In production, serve static files from a CDN or reverse proxy (nginx) for best performance.

### 7. Logging

```python
from hyperdjango.logging import logger

# File logging with rotation
logger.add("app.log", rotation="100 MB", retention="30 days", compression="gz")

# JSON logging for log aggregators
logger.add("app.json", format="json")

# Error-only file
logger.add("errors.log", level="ERROR")
```

### 8. Health Checks

```python
app.mount_health()
# GET /health  → liveness probe (always 200)
# GET /ready   → readiness probe (checks DB + custom checks)
```

Add custom readiness checks:

```python
app.mount_health(
    checks={"cache": check_cache_connection, "queue": check_queue_connection}
)
```

### 9. Graceful Shutdown

HyperDjango handles SIGTERM and SIGINT automatically via a native Zig signal handler (no Python signal handler needed):

1. Signal handler writes to a self-pipe, waking the accept loop immediately
2. Stops accepting new connections
3. Waits for in-flight requests to complete (30s drain timeout)
4. Runs `@app.on_shutdown` hooks
5. Removes PID file
6. Exits with code 0

For container orchestrators (Kubernetes, ECS), set `terminationGracePeriodSeconds: 45`.

## Reverse Proxy (nginx)

```nginx
upstream hyperdjango {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate /etc/ssl/certs/yourdomain.pem;
    ssl_certificate_key /etc/ssl/private/yourdomain.key;

    # Static files (serve directly)
    location /static/ {
        alias /app/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Proxy to HyperDjango
    location / {
        proxy_pass http://hyperdjango;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://hyperdjango;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## Docker

```dockerfile
FROM python:3.14-slim

# Install Zig
RUN apt-get update && apt-get install -y wget xz-utils && \
    wget https://ziglang.org/download/0.15.2/zig-linux-x86_64-0.15.2.tar.xz && \
    tar xf zig-linux-x86_64-0.15.2.tar.xz && \
    mv zig-linux-x86_64-0.15.2 /opt/zig && \
    ln -s /opt/zig/zig /usr/local/bin/zig

WORKDIR /app
COPY . .

# Install dependencies and build native extension
RUN pip install uv && uv sync
RUN uv run hyper-build --install --release

EXPOSE 8000
CMD ["uv", "run", "hyper", "run"]
```

## Server Management CLI

```bash
# Background daemon
hyper start --app app:app --port 8000 --prod
hyper stop --port 8000          # SIGTERM → drain → clean exit
hyper restart --app app:app     # stop + start
hyper status --port 8000        # PID check
```

PID file at `.hyper.<port>.pid`, log at `.hyper.<port>.log`.

## Systemd

### Bundled services: `hyper service install`

For anything under `services/`, do **not** hand-write a unit. `hyper service
install` derives the whole thing from the service registry — unit name,
description, port, `ExecStart`, working directory, companion dependencies and
the environment file (reusing the service's already-persisted secrets, so
seeded tokens keep verifying):

```bash
uv run hyper service install hypersecret --dry-run --enable --start   # inspect
sudo uv run hyper service install hypersecret --enable --start        # install
sudo systemctl stop hyperdjango.target                                # whole set
sudo uv run hyper service uninstall hypersecret                       # remove
```

It handles what a hand-written unit usually gets wrong: companions bound in both
directions (`Requires=`/`After=` on the parent, `PartOf=` on the companion), the
real PostgreSQL unit name detected on the host, a `pg_isready` `ExecStartPre`
gate because `After=` is not readiness, restart backoff that survives a
boot-time race, a per-service `SyslogIdentifier`, and a `hyperdjango.target`
grouping. Full reference: [Running the bundled
services](running-services.md#running-a-service-permanently-systemd-units).

### Automatic Unit File Generation (your own app)

```bash
# Generate and install (run as root, or copies to current directory)
sudo hyper systemd install --app app:app --port 8000 --enable

# Manage
sudo systemctl status hyperdjango-myapp
sudo systemctl restart hyperdjango-myapp
sudo journalctl -u hyperdjango-myapp -f

# Remove
sudo hyper systemd uninstall
```

The generated unit file includes production-grade defaults:

```ini
[Unit]
Description=HyperDjango — myapp
After=network.target postgresql.service
Wants=postgresql.service
# NOTE: After= orders start jobs, it is NOT a readiness guarantee, and the
# PostgreSQL unit is named differently across distros. For a stronger gate see
# the pg_isready ExecStartPre that `hyper service install` emits.

[Service]
Type=exec
User=app
Group=app
WorkingDirectory=/opt/myapp
Environment="DATABASE_URL=postgres://user:pass@localhost/mydb"
Environment="SECRET_KEY=your-secret-key"
ExecStart=/opt/myapp/.venv/bin/python -m hyperdjango.cli run --host 0.0.0.0 --port 8000 --app app:app --prod
ExecStop=/bin/kill -TERM $MAINPID
TimeoutStopSec=30
KillMode=mixed
KillSignal=SIGTERM
Restart=on-failure
RestartSec=5
# No ExecReload: the native server handles SIGTERM/SIGINT only — there is no
# SIGHUP reload path, so these units are restart-only. `systemctl reload` fails
# loudly rather than pretending to have reloaded.

# Security hardening
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/myapp
NoNewPrivileges=true
LimitNOFILE=65536
LimitNPROC=4096

[Install]
WantedBy=multi-user.target
```

### Manual Installation

If not running as root, `hyper systemd install` writes the unit file to the current directory for manual installation:

```bash
hyper systemd install --app app:app --port 8000
sudo cp hyperdjango-myapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperdjango-myapp
```

## Performance Tuning

### Database

- **Pool size**: Match to expected concurrent queries (not total connections)
- **Statement timeout**: Set `statement_timeout` to catch runaway queries
- **Prepared statements**: Automatic caching — repeated queries skip Parse phase
- **Connection pipelining**: 5.7x faster for batch operations

### Server

- **Thread pool**: 24 threads default (Zig HTTP server)
- **Keep-alive**: Enabled by default, reduces connection overhead
- **Response buffering**: Native Zig response builder, zero-copy where possible

### Caching

```python
from hyperdjango.cache import LocMemCache, DatabaseCache

# In-memory LRU cache for hot data
cache = LocMemCache(max_entries=10000)

# PostgreSQL UNLOGGED table cache for shared data
cache = DatabaseCache(db, table="hyper_cache")
```

### Monitoring

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

### Prometheus Metrics

```python
from hyperdjango.metrics import MetricsMiddleware

app.use(MetricsMiddleware())
# GET /metrics → Prometheus scrape endpoint
```

Exposed metrics:

| Metric                                      | Type      | Description                                    |
| ------------------------------------------- | --------- | ---------------------------------------------- |
| `hyperdjango_info`                          | info      | Build version and Python version               |
| `hyperdjango_uptime_seconds`                | gauge     | Process uptime                                 |
| `hyperdjango_http_requests_total`           | counter   | Requests by method + status                    |
| `hyperdjango_http_request_duration_seconds` | histogram | Request latency (12 buckets, 1ms–10s)          |
| `hyperdjango_http_requests_in_flight`       | gauge     | Currently processing requests                  |
| `hyperdjango_db_pool_total`                 | gauge     | Total pool connections                         |
| `hyperdjango_db_pool_available`             | gauge     | Idle pool connections                          |
| `hyperdjango_db_pool_in_use`                | gauge     | Active pool connections                        |
| `hyperdjango_db_pool_thread_owned`          | gauge     | Thread-pinned connections                      |
| `hyperdjango_stmt_cache_hits_total`         | counter   | Prepared statement cache hits                  |
| `hyperdjango_stmt_cache_misses_total`       | counter   | Prepared statement cache misses                |
| `hyperdjango_stmt_cache_evictions_total`    | counter   | Cache evictions (DEALLOCATE sent)              |
| `hyperdjango_stmt_cache_entries`            | gauge     | Current cache entries                          |
| `hyperdjango_stmt_cache_hit_rate`           | gauge     | Cache hit rate (0.0–1.0)                       |
| `hyperdjango_db_queries_total`              | counter   | Total queries (requires PerformanceMiddleware) |
| `hyperdjango_db_slow_queries_total`         | counter   | Slow queries exceeding threshold               |
| `hyperdjango_n_plus_one_total`              | counter   | N+1 patterns detected                          |

Custom path:

```python
app.use(MetricsMiddleware(metrics_path="/prom/metrics"))
```

Register QueryTimer and PoolHealthChecker for additional metrics:

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

Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: "hyperdjango"
    static_configs:
      - targets: ["localhost:8000"]
    metrics_path: "/metrics"
    scrape_interval: 15s
```

## Rate Limiting in Production

### Basic Rate Limiting

Apply global rate limiting to protect against abuse:

```python
from hyperdjango.ratelimit import RateLimitMiddleware, InMemoryRateLimitBackend

app.use(
    RateLimitMiddleware(
        backend=InMemoryRateLimitBackend(),
        max_requests=100,
        window_seconds=60,
        key_func=lambda r: r.client_ip,
    )
)
```

### RBAC Tiered Rate Limiting

For multi-tier SaaS applications, use `RuleBasedRateLimitMiddleware` with per-path, per-method, per-tier rules stored in PostgreSQL:

```python
from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

tiers = {
    "free": {"max_requests": 100, "window": 60},
    "pro": {"max_requests": 1000, "window": 60},
    "enterprise": {"max_requests": 10000, "window": 60},
}

mw = RuleBasedRateLimitMiddleware(
    tiers=tiers,
    default_tier="free",
    db=db,
    rules_cache_ttl=60,  # Reload rules from DB every 60 seconds
)
await mw.ensure_tables()

app.use(mw)
```

### Production Rule Configuration

Add rules via SQL or admin interface. Rules are evaluated by priority (highest first):

```sql
-- Expensive report endpoint: 20 req/min, costs 5 units each, free tier only
INSERT INTO hyper_rate_limit_rules
  (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority)
VALUES
  ('expensive-reports-free', '/api/reports*', 'GET', 'free', 20, 60, 5, 100);

-- Write API: 50 req/min for free tier
INSERT INTO hyper_rate_limit_rules
  (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority)
VALUES
  ('write-api-free', '/api/*', 'POST', 'free', 50, 60, 1, 50);

-- Bulk import: 5 req/hour, all tiers, high cost
INSERT INTO hyper_rate_limit_rules
  (name, path_pattern, method, tier, max_requests, window_seconds, cost, priority)
VALUES
  ('bulk-import', '/api/import', 'POST', '*', 5, 3600, 10, 200);
```

Each rule gets its own counter — a user rate-limited on `/api/reports*` still has their full quota for other endpoints. Response headers include `X-RateLimit-Tier`, `X-RateLimit-Rule`, and `X-RateLimit-Cost`.

### Tier Assignment

Assign tiers via the `rate_limit_tier` column on `hyper_groups`:

```sql
UPDATE hyper_groups SET rate_limit_tier = 'pro' WHERE name = 'paid_users';
```

Users inherit the highest tier from their group memberships. Tier lookups are cached per-request.

See [Rate Limiting](ratelimit.md) for the full rule model reference, matching logic, and cache management.

## Connection Pool Health

### Background Heartbeat

The `PoolHeartbeat` periodically validates connections and tracks latency:

```python
from hyperdjango.pool import PoolHeartbeat

heartbeat = PoolHeartbeat(
    db,
    interval_seconds=15,  # Check every 15 seconds
    failure_threshold=3,  # 3 consecutive failures → unhealthy
)
heartbeat.start()

# Register with Prometheus metrics
from hyperdjango.metrics import register_health_checker

register_health_checker(heartbeat)
```

The heartbeat detects dead connections, network partitions, and server restarts. It logs state transitions (healthy→unhealthy→recovered) and exports latency percentiles (avg, p99) to `/metrics`.

### Pool Tuning

```python
db = Database("postgres://localhost/mydb", min_size=2, max_size=32)

# Monitor pool utilization
stats = db.pool_stats()
# {"total": 32, "available": 28, "in_use": 4, "thread_owned": 2}
```

See [Performance](performance.md) for pool tuning, slow query logging, and auto-tuner configuration.
