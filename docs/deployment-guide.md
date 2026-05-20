# Deployment Guide

Production deployment for HyperDjango applications: building, configuring, running as a service, and pre-flight checks.

> **Complete working example:** See [`services/deployment/`](https://github.com/mattsta/hyperdjango/tree/main/services/deployment) for a ready-to-use deployment kit with systemd unit, nginx config, env template, logrotate config, health probes, and step-by-step instructions in `DEPLOY.md`.

---

## Table of Contents

- [Production Build](#production-build)
- [Environment Configuration](#environment-configuration)
- [Running in Production](#running-in-production)
- [systemd Service](#systemd-service)
- [Docker Deployment](#docker-deployment)
- [Reverse Proxy with nginx](#reverse-proxy-with-nginx)
- [Pre-Flight Checks](#pre-flight-checks)
- [Database Setup](#database-setup)
- [Static Files](#static-files)
- [Security Checklist](#security-checklist)
- [Monitoring](#monitoring)
- [Migration Notes for Django Users](#migration-notes-for-django-users)

---

## Production Build

The native Zig extension must be compiled with release optimizations for production:

```bash
uv run hyper-build --install --release
```

This compiles with Zig's `ReleaseFast` mode, producing a `.so` (Linux) or `.dylib` (macOS) with:

- Full SIMD optimizations for JSON parsing, validation, and string operations
- Optimized HTTP server with 24-thread pool
- Radix trie router (808ns per route resolution)
- Prepared statement caching for the pg.zig PostgreSQL driver

The debug build (`uv run hyper-build`) includes bounds checking and debug symbols. Never use it in production.

---

## Environment Configuration

HyperDjango reads configuration from environment variables with the `HYPER_` prefix:

```bash
# Required
HYPER_SECRET_KEY=your-256-bit-secret-key-here
HYPER_DATABASE_URL=postgres://user:password@dbhost:5432/mydb

# Recommended
HYPER_DEBUG=false
HYPER_ALLOWED_HOSTS=myapp.com,api.myapp.com

# Optional
HYPER_POOL_SIZE=0              # 0 = auto (THREAD_POOL_SIZE + headroom)
HYPER_CONNECT_TIMEOUT=10000    # ms
HYPER_QUERY_TIMEOUT=30000      # ms
HYPER_MAX_BODY_SIZE=10485760   # 10MB
```

### .env File Support

Place a `.env` file in the project root:

```bash
# .env
SECRET_KEY=your-256-bit-secret-key-here
DATABASE_URL=postgres://user:password@localhost:5432/mydb
DEBUG=false
```

### Application Configuration

```python
# app.py
import os
from hyperdjango import HyperApp

app = HyperApp(
    title="My Production App",
    database=os.environ["HYPER_DATABASE_URL"],
    debug=False,
    max_body_size=10 * 1024 * 1024,  # 10MB
)
```

### Full Environment Variable Reference

| Variable                     | Default   | Description                                      |
| ---------------------------- | --------- | ------------------------------------------------ |
| `HYPER_SECRET_KEY`           | None      | HMAC signing key for sessions and CSRF           |
| `HYPER_DATABASE_URL`         | None      | PostgreSQL connection string                     |
| `HYPER_DEBUG`                | `"false"` | Debug mode (enables detailed errors, hot reload) |
| `HYPER_ALLOWED_HOSTS`        | `"*"`     | Comma-separated allowed hostnames                |
| `HYPER_POOL_SIZE`            | `0`       | DB connection pool size (0 = auto)               |
| `HYPER_CONNECT_TIMEOUT`      | `10000`   | TCP connect timeout in ms                        |
| `HYPER_QUERY_TIMEOUT`        | `0`       | PostgreSQL statement_timeout in ms               |
| `HYPER_PREPARED_STATEMENTS`  | `"true"`  | Enable prepared statement caching                |
| `HYPER_STATEMENT_CACHE_SIZE` | `256`     | LRU prepared statement cache size                |
| `HYPER_POOL_MAX_QUERIES`     | `10000`   | Rotate connections after N queries               |
| `HYPER_POOL_MAX_LIFETIME`    | `3600`    | Max connection age in seconds                    |

---

## Running in Production

### Direct Execution

```python
# app.py
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        prod=True,  # Enables production validation checks
    )
```

```bash
uv run hyper run
```

The `prod=True` flag enables:

- Production validation (secret key set, debug disabled, etc.)
- Graceful shutdown on SIGTERM/SIGINT
- No hot reload or debug error pages

### Binding to Unix Socket

For reverse proxy setups (nginx):

```python
app.run(unix_socket="/run/myapp/myapp.sock", prod=True)
```

---

## systemd Service

For anything under `services/`, generate the unit instead of writing one —
`hyper service install <name>` derives every field from the service registry and
handles the dependency, readiness and grouping details below for you. See
[Running the bundled
services](running-services.md#running-a-service-permanently-systemd-units).

For your own app, create `/etc/systemd/system/myapp.service`:

```ini
[Unit]
Description=MyApp HyperDjango Application
After=network.target postgresql.service
Wants=postgresql.service
StartLimitIntervalSec=600
StartLimitBurst=30

[Service]
Type=exec
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp
EnvironmentFile=/etc/myapp/myapp.env
# PostgreSQL readiness gate — After= only orders start JOBS. On Debian/Ubuntu
# postgresql.service is Type=oneshot ExecStart=/bin/true (a meta unit for the
# per-cluster postgresql@N-main.service instances, which are Type=forking), so
# ordering after it says nothing about the server accepting connections. Pair
# this gate with the restart backoff above so a boot-time race retries into
# success instead of hitting the start limit.
ExecStartPre=/usr/bin/pg_isready --host=localhost --port=5432 --timeout=5
ExecStart=/opt/myapp/.venv/bin/python -m hyperdjango.cli run --host 127.0.0.1 --port 8000 --app app:app --prod
ExecStop=/bin/kill -TERM $MAINPID
# No ExecReload. The native server installs handlers for SIGTERM and SIGINT
# only — there is no SIGHUP reload path — so this unit is RESTART-ONLY. An
# ExecReload=/bin/kill -HUP $MAINPID would report success while doing nothing
# (or killing the process on SIGHUP's default disposition).
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=myapp

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/myapp/media /opt/myapp/static
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true

[Install]
WantedBy=multi-user.target
```

Secrets belong in the 0600 `EnvironmentFile`, never in `Environment=` lines:
`/etc/systemd/system` is world-readable, and `systemctl show` prints
`Environment=` values to anyone who asks.

```bash
sudo install -d -m 700 /etc/myapp
sudo install -m 600 /dev/null /etc/myapp/myapp.env
# HYPER_SECRET_KEY=..., HYPER_DATABASE_URL=..., HYPER_DEBUG=false
```

The unit name in `After=`/`Wants=` varies by distro — Debian and Ubuntu also
ship per-cluster instances such as `postgresql@18-main.service`. Check what your
host actually has (`systemctl list-unit-files 'postgres*'`) before copying the
line, and drop it entirely when the database is on another host: there is no
local unit to order against, and the `pg_isready` gate carries the dependency.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable myapp
sudo systemctl start myapp
sudo systemctl status myapp

# Configuration changed? Restart — this unit does not support reload.
sudo systemctl restart myapp

# View logs
sudo journalctl -u myapp -f
```

---

## Docker Deployment

### Dockerfile

```dockerfile
# Build stage: compile Zig native extension
FROM python:3.14-slim AS builder

# Install Zig
RUN apt-get update && apt-get install -y wget xz-utils && \
    wget -q https://ziglang.org/download/0.15.0/zig-linux-x86_64-0.15.0.tar.xz && \
    tar xf zig-linux-x86_64-0.15.0.tar.xz && \
    mv zig-linux-x86_64-0.15.0 /opt/zig

ENV PATH="/opt/zig:$PATH"

# Install uv
RUN pip install uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .
RUN uv run hyper-build --install --release

# Runtime stage
FROM python:3.14-slim

RUN useradd -r -s /bin/false appuser

WORKDIR /app
COPY --from=builder /app /app

USER appuser
EXPOSE 8000

ENV HYPER_DEBUG=false
ENV HYPER_SECRET_KEY=change-me-in-production

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uv", "run", "hyper", "run"]
```

### docker-compose.yml

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: myapp
      POSTGRES_USER: myapp
      POSTGRES_PASSWORD: secure-db-password
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U myapp"]
      interval: 5s
      timeout: 5s
      retries: 5

  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      HYPER_DATABASE_URL: postgres://myapp:secure-db-password@db:5432/myapp
      HYPER_SECRET_KEY: your-production-secret-key
      HYPER_DEBUG: "false"
    depends_on:
      db:
        condition: service_healthy
    restart: always

volumes:
  pgdata:
```

```bash
docker compose up -d
docker compose logs -f app
```

---

## Reverse Proxy with nginx

In production, HyperDjango should sit behind a reverse proxy. The Zig HTTP server is fast and correct, but a production reverse proxy handles concerns that application servers shouldn't: TLS termination, static file serving with sendfile(2), request buffering against slow clients, connection limits against DDoS, HTTP/2 multiplexing, and access logging at the edge.

### Why a Reverse Proxy

| Concern                  | nginx handles it                          | HyperDjango handles it          |
| ------------------------ | ----------------------------------------- | ------------------------------- |
| TLS/SSL termination      | Yes (OpenSSL, hardware offload)           | No                              |
| HTTP/2 and HTTP/3        | Yes                                       | No (HTTP/1.1 only)              |
| Slow client buffering    | Yes (spools full request before proxying) | No (reads directly from socket) |
| Connection rate limiting | Yes (limit_conn, limit_req)               | Yes (RateLimitMiddleware)       |
| Static file serving      | Yes (sendfile, no copy to userspace)      | Yes (but wastes app threads)    |
| Graceful reload          | Yes (binary upgrade, zero downtime)       | Yes (SIGTERM drain)             |

**Architecture:** Client → nginx (TLS, HTTP/2, static files) → HyperDjango (application logic, DB queries, templates)

### TCP Proxy (localhost)

The simplest setup. nginx connects to HyperDjango on a TCP port:

```nginx
upstream hyperdjango {
    server 127.0.0.1:8000;

    # Keep persistent connections to the backend.
    # The Zig HTTP server handles keepalive natively.
    keepalive 32;
}

server {
    listen 80;
    server_name myapp.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name myapp.com;

    # TLS — Let's Encrypt via certbot
    ssl_certificate     /etc/letsencrypt/live/myapp.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/myapp.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # HSTS — tell browsers to always use HTTPS
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;

    # Static files — served directly by nginx, never hits Python
    location /static/ {
        alias /opt/myapp/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # WebSocket routes — requires HTTP/1.1 upgrade
    location /ws/ {
        proxy_pass http://hyperdjango;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;  # Keep WS connections alive for 24h
    }

    # Health check for upstream monitoring
    location = /health {
        proxy_pass http://hyperdjango;
        proxy_set_header Host $host;
        access_log off;
    }

    # Application — all other requests
    location / {
        proxy_pass http://hyperdjango;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";  # Enable keepalive to upstream

        # Match HyperDjango's MAX_BODY_SIZE (default 10MB)
        client_max_body_size 10m;

        # Buffer the full response before sending to slow clients.
        # This frees the Zig worker thread immediately.
        proxy_buffering on;
        proxy_buffer_size 4k;
        proxy_buffers 8 8k;
    }
}
```

### Unix Socket Proxy (same host, lower latency)

When nginx and HyperDjango run on the same machine, Unix sockets eliminate TCP overhead (no SYN/ACK, no Nagle's algorithm, no port allocation). This saves ~50-100us per request under high concurrency.

**HyperDjango config:**

```python
# app.py — bind to Unix socket instead of TCP
app.run(unix_socket="/run/myapp/myapp.sock", prod=True)
```

Or via CLI:

```bash
uv run hyper start --app app:app --socket /run/myapp/myapp.sock
```

**nginx config** — only the upstream block changes:

```nginx
upstream hyperdjango {
    server unix:/run/myapp/myapp.sock;
    keepalive 32;
}

# The rest of the server {} block is identical to the TCP example above.
```

**Permission setup** — nginx and HyperDjango must both access the socket:

```bash
# Create socket directory with shared group
sudo mkdir -p /run/myapp
sudo chown myapp:www-data /run/myapp
sudo chmod 750 /run/myapp
```

### TLS with Let's Encrypt

```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx

# Issue certificate (nginx must be running)
sudo certbot --nginx -d myapp.com -d www.myapp.com

# Auto-renewal (certbot installs a systemd timer by default)
sudo systemctl status certbot.timer
```

Certbot modifies your nginx config to add `ssl_certificate` and `ssl_certificate_key` directives and sets up automatic renewal.

### Rate Limiting: nginx vs HyperDjango

Use **both layers** — they serve different purposes:

| Layer                                 | Purpose                                    | Key type              | Storage                                     |
| ------------------------------------- | ------------------------------------------ | --------------------- | ------------------------------------------- |
| **nginx** `limit_req`                 | Protect the server from volumetric attacks | IP address            | Shared memory (fast, no DB)                 |
| **HyperDjango** `RateLimitMiddleware` | Per-user/per-tenant business limits        | User ID, API key, org | PostgreSQL UNLOGGED (shared across servers) |

```nginx
# nginx layer: 50 req/sec burst with nodelay (volumetric protection)
limit_req_zone $binary_remote_addr zone=api:10m rate=50r/s;

server {
    location /api/ {
        limit_req zone=api burst=100 nodelay;
        proxy_pass http://hyperdjango;
        # ... proxy headers ...
    }
}
```

```python
# HyperDjango layer: 120 req/min per authenticated user (business logic)
app.use(RateLimitMiddleware(max_requests=120, window=60))
```

### Full Production Config

See [`services/deployment/nginx.conf`](https://github.com/mattsta/hyperdjango/blob/main/services/deployment/nginx.conf) for a complete, annotated production config combining all of the above.

---

## Pre-Flight Checks

Run `hyper doctor` before deploying to catch configuration issues:

```bash
uv run hyper doctor
```

This runs the full diagnostic check set across every category:

| Category   | Checks                                               |
| ---------- | ---------------------------------------------------- |
| build      | Native extension compiled, Zig version, release mode |
| python     | Python version, free-threading, GIL status           |
| database   | Connection, pool health, prepared statements         |
| perf       | Cache config, thread pool, debug mode off            |
| config     | Settings validation, secret key strength             |
| filesystem | File permissions, path accessibility                 |
| security   | Secret key set, debug disabled, CORS configured      |

### CI Integration

```bash
# JSON output for CI pipelines
uv run hyper doctor --json

# Check specific category
uv run hyper doctor --category build
uv run hyper doctor --category security
```

Example CI step (GitHub Actions):

```yaml
- name: Pre-flight checks
  run: |
    uv run hyper-build --install --release
    uv run hyper doctor --json > doctor-report.json
    if jq -e '.failed > 0' doctor-report.json; then
      echo "Doctor checks failed"
      cat doctor-report.json
      exit 1
    fi
```

---

## Database Setup

### Run Migrations

```bash
uv run hyper migrate
```

### Create Admin User

```bash
uv run hyper createsuperuser
```

### Auth Tables

If using the auth system, ensure tables exist:

```python
from hyperdjango.auth import PermissionChecker
from hyperdjango.auth.db_sessions import DatabaseSessionStore

checker = PermissionChecker(app.db)
await checker.ensure_tables()

store = DatabaseSessionStore(app.db)
await store.ensure_table()
```

---

## Static Files

### Collect Static Files

```bash
uv run hyper collectstatic
```

This copies static files from all configured directories into a single output directory with content-hashed filenames for cache busting.

### Production Static Serving

In production, serve static files from a CDN or reverse proxy (nginx), not from the application server:

**nginx:**

```nginx
server {
    listen 80;
    server_name myapp.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name myapp.com;

    ssl_certificate /etc/letsencrypt/live/myapp.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/myapp.com/privkey.pem;

    location /static/ {
        alias /opt/myapp/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 10m;
    }
}
```

---

## Security Checklist

Before deploying to production, verify:

- [ ] `HYPER_SECRET_KEY` is set to a strong, unique value (256+ bits)
- [ ] `HYPER_DEBUG` is `"false"`
- [ ] Database uses a strong password with limited network access
- [ ] Native extension built with `--release` flag
- [ ] HTTPS configured (via reverse proxy or load balancer)
- [ ] CORS origins restricted to your domains
- [ ] Rate limiting enabled
- [ ] Security headers middleware active (HSTS, CSP, X-Frame-Options)
- [ ] CSRF protection enabled for browser-facing endpoints
- [ ] `hyper doctor` passes all checks
- [ ] Session cookies set to `secure=True` and `httponly=True`
- [ ] Database connection uses SSL in production
- [ ] File upload size limited via `max_body_size`
- [ ] Logging configured for production (JSON format, no debug output)

---

## Monitoring

### Health Checks

Register health check endpoints:

```python
@app.health_check("database")
async def check_db():
    """Returns True if DB is responsive."""
    try:
        await app.db.query("SELECT 1")
        return True
    except Exception:
        return False


@app.health_check("disk")
async def check_disk():
    """Returns True if disk space is sufficient."""
    import shutil

    usage = shutil.disk_usage("/")
    return usage.free > 1_000_000_000  # 1GB free
```

These are accessible at:

- `GET /health/live` -- Liveness probe (always 200 if server is running)
- `GET /health/ready` -- Readiness probe (runs all registered health checks)

### Performance Dashboard

```python
from hyperdjango.performance import PerformanceMiddleware

app.use(
    PerformanceMiddleware(
        slow_query_threshold_ms=100,
        enable_dashboard=True,  # Accessible at /debug/performance
    )
)
```

### Pool Statistics

```python
stats = await app.db.pool_stats()
print(stats)  # Pool size, active connections, idle connections, wait queue
```

---

## Migration Notes for Django Users

| Django                                          | HyperDjango                           |
| ----------------------------------------------- | ------------------------------------- |
| `gunicorn myapp.wsgi`                           | `uv run hyper run`                    |
| `python manage.py collectstatic`                | `uv run hyper collectstatic`          |
| `python manage.py migrate`                      | `uv run hyper migrate`                |
| `python manage.py createsuperuser`              | `uv run hyper createsuperuser`        |
| `settings.py` with `DATABASES` dict             | `HYPER_DATABASE_URL` env var          |
| `SECRET_KEY` in settings                        | `HYPER_SECRET_KEY` env var            |
| `ALLOWED_HOSTS` list                            | `HYPER_ALLOWED_HOSTS` comma-separated |
| PostgreSQL UNLOGGED tables for caching/sessions | Built-in                              |
| External task broker for background jobs        | Built-in `@task` decorator            |
| `manage.py check --deploy`                      | `uv run hyper doctor`                 |

---

## Production Checklist

A comprehensive checklist of 25 items to verify before every production deployment. Run through each category systematically.

### Critical Security

1. **SECRET_KEY is set and strong.** `HYPER_SECRET_KEY` must be a random value of at least 256 bits (64 hex characters). Load from an environment variable or secrets manager, never commit to source control.

2. **DEBUG is disabled.** `HYPER_DEBUG=false`. Debug mode leaks source code, local variables, settings, and internal paths in error responses.

3. **ALLOWED_HOSTS is restrictive.** `HYPER_ALLOWED_HOSTS` must list only your actual domain names (e.g., `myapp.com,api.myapp.com`). Never use `*` in production.

4. **HTTPS is enforced.** All traffic must go through TLS. Configure your reverse proxy (nginx) to redirect HTTP to HTTPS. Set `HSTS` headers to prevent downgrade attacks.

5. **HSTS is enabled.** Add `Strict-Transport-Security: max-age=31536000; includeSubDomains` via `SecurityHeadersMiddleware(hsts=True)`. Once set, browsers will refuse plain HTTP for the configured duration.

6. **CSRF protection is active.** For browser-facing endpoints that accept POST/PUT/DELETE, ensure CSRF middleware is enabled. API endpoints using API keys or OAuth2 tokens do not need CSRF.

7. **Session cookies are secure.** Configure `SessionAuth` with `cookie_secure=True` (only sent over HTTPS), `cookie_httponly=True` (not accessible to JavaScript), and `cookie_samesite="Lax"` or `"Strict"`.

8. **Admin access is restricted.** HyperAdmin should not be accessible from the public internet. Restrict by IP, require VPN, or at minimum require `is_staff=True` authentication.

9. **CORS origins are explicit.** `CORSMiddleware(origins=["https://myapp.com"])` -- never use `origins=["*"]` in production.

10. **Security headers are configured.** Enable `SecurityHeadersMiddleware` to set `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, and `Permissions-Policy`.

### Database

11. **Database password is strong and not in source control.** Load from `HYPER_DATABASE_URL` environment variable. The database user should have only the privileges needed by the application.

12. **Database connection uses SSL.** Add `?sslmode=require` to the connection string for cloud databases. For self-hosted, configure `sslmode=verify-full` with the CA certificate.

13. **Connection pool is tuned.** Set `HYPER_POOL_SIZE` appropriate to your workload and PostgreSQL `max_connections`. The default (auto = THREAD_POOL_SIZE + headroom, so the pool is never smaller than the worker-thread count that pins connections) is a reasonable starting point.

14. **(Reserved) `HYPER_PREPARED_STATEMENTS` / `HYPER_STATEMENT_CACHE_SIZE`** are not yet honored by the native pg pool — setting them has no runtime effect today. No action needed.

15. **Query timeout is set.** `HYPER_QUERY_TIMEOUT=30000` (30 seconds) prevents runaway queries from holding connections indefinitely.

16. **Database backups are configured and tested.** Use `pg_dump` on a schedule, or your cloud provider's automated backup feature. Test restoring from a backup at least once.

### Application

17. **Native extension is built with --release.** Run `uv run hyper-build --install --release`. The debug build includes bounds checking and debug symbols that slow production workloads significantly.

18. **Static files are collected.** Run `uv run hyper collectstatic` before deployment. Serve static files from a CDN or reverse proxy with long-lived cache headers (`Cache-Control: public, max-age=31536000, immutable`).

19. **Rate limiting is enabled.** Configure `RateLimitMiddleware` or the rule-based rate limiter to protect against abuse. Set appropriate limits per IP, per user, and per endpoint.

20. **File upload size is limited.** Set `max_body_size` on the app (default 10MB). Match this with your reverse proxy's `client_max_body_size`.

### Logging and Monitoring

21. **Logging is configured for production.** Use JSON-formatted logs (machine-parseable) with appropriate log levels. Disable debug-level logging. Send logs to a centralized log aggregation system.

22. **Error reporting is set up.** Configure an error monitoring service to capture unhandled exceptions with full context. Do not rely on email notifications for error reporting at scale.

23. **Health check endpoints are registered.** Register at least a database health check with `@app.health_check("database")`. Expose `/health/live` for liveness probes and `/health/ready` for readiness probes in container orchestrators.

24. **Monitoring and alerting are active.** Monitor response times (p50, p95, p99), error rates, database connection pool utilization, and disk usage. Set alerts for anomalies.

### Operational

25. **Graceful shutdown is configured.** Use `app.run(prod=True)` which handles `SIGTERM`/`SIGINT` gracefully, draining in-flight requests before stopping. In Docker or Kubernetes, ensure the stop grace period is long enough for request draining.

### Running the Checklist

Automate the most important checks with `hyper doctor`:

```bash
# Run all checks
uv run hyper doctor

# Fail the CI pipeline if any check fails
uv run hyper doctor --json | python -c "
import sys, json
report = json.load(sys.stdin)
if report['failed'] > 0:
    print(f'FAILED: {report[\"failed\"]} checks failed')
    sys.exit(1)
print(f'OK: all {report[\"passed\"]} checks passed')
"
```

`hyper doctor` covers build verification, Python version, database connectivity, pool health, prepared statements, cache configuration, secret key strength, debug mode, and CORS settings.
