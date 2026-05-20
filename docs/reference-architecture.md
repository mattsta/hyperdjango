# Reference Architecture

A complete production deployment of HyperDjango — every layer explained, from the client's browser to the PostgreSQL row and back.

This document is for senior engineers evaluating the framework. It shows how the pieces fit together and where the performance comes from.

---

## Deployment Topology

```
                         Internet
                            │
                     ┌──────┴──────┐
                     │   nginx     │  TLS termination, HTTP/2, static files,
                     │  (reverse   │  rate limiting (volumetric), gzip,
                     │   proxy)    │  WebSocket upgrade, health checks
                     └──────┬──────┘
                            │ HTTP/1.1 (keepalive) or Unix socket
               ┌────────────┼────────────┐
               │            │            │
        ┌──────┴──────┐  ┌──┴───┐  ┌────┴─────┐
        │ HyperDjango │  │ HD 2 │  │  HD 3    │  Multiple instances for
        │  (24 Zig    │  │      │  │          │  horizontal scaling.
        │   threads)  │  │      │  │          │  Each is single-process,
        └─-────┬──────┘  └──┬───┘  └────┬─────┘  multi-threaded.
               │            │            │
               └────────────┼────────────┘
                            │
                  ┌─────────┴─────────┐
                  │                   │
           ┌──────┴──────┐    ┌────────────┐
           │ PostgreSQL  │    │ PostgreSQL │
           │  PRIMARY    │───▶│  REPLICA   │  Streaming replication.
           │  (writes)   │    │  (reads)   │  Writes → primary only.
           └─────────────┘    └────────────┘
```

**Each HyperDjango instance** is a single Python process with 24 Zig HTTP worker threads. All threads share:

- In-memory template LRU cache (compiled Zig node trees)
- In-memory LocMemCache (L1 of TwoTierCache)
- pg.zig connection pool (one pinned connection per thread)
- Middleware chain, auth state, rate limit counters (in-memory)

**Cross-instance coordination** happens through PostgreSQL:

- DatabaseCache (UNLOGGED table) for shared cache state
- DatabaseSessionStore (UNLOGGED table) for sessions
- DatabaseRateLimitBackend (UNLOGGED table) for rate limits
- LISTEN/NOTIFY for real-time pub/sub (PgChannelLayer)

No external message broker. PostgreSQL is the single coordination point.

---

## Request Lifecycle

A complete request through all layers, with timing at each step:

```
1. Client sends HTTPS request
   │
2. nginx: TLS decrypt, parse HTTP/2, buffer request body         ~0.5ms
   │
3. nginx → HyperDjango: proxy_pass (TCP or Unix socket)          ~0.05ms
   │
4. Zig HTTP server: accept(), parse HTTP headers (8KB max)        ~0.01ms
   │       ╰─ SIMD-accelerated header parsing
   │
5. Zig router: radix trie lookup → find handler                   ~0.8μs
   │       ╰─ Returns (handler, params) or 404
   │
6. Zig → Python: acquire GIL, dispatch to Python handler          ~1μs
   │
7. Middleware chain (outermost → innermost):
   │   SecurityHeadersMiddleware    set response headers           ~0.5μs
   │   TimingMiddleware             start timer                    ~0.1μs
   │   CORSMiddleware               check origin                   ~0.3μs
   │   RateLimitMiddleware           check counter, emit IETF hdrs ~2μs
   │   CSRFMiddleware                validate token (POST only)    ~1μs
   │   SessionAuth                   decode cookie, load session   ~5μs
   │   TelemetryMiddleware           start span, emit metrics      ~4μs
   │
8. Python handler runs:
   │   QuerySet.filter().all()  → compiled SQL cache lookup        ~0.2μs
   │       ╰─ Cache hit: return cached SQL + params
   │       ╰─ Cache miss: compile WhereNode tree (Zig FNV-1a)
   │
   │   pg.zig query execution:
   │       acquire pool connection (thread-pinned, ~0)             ~0
   │       prepared statement cache hit? Skip Parse phase          ~0
   │       Bind + Execute → PostgreSQL                             ~1-5ms
   │       Parse result rows → Python dicts (pre-interned keys)   ~0.05ms
   │
   │   Serializer.serialize(queryset):
   │       Plan closure loop (precomputed at class creation)       ~0.01ms
   │       JSON serialization (Zig SIMD fast_json_dumps)           ~0.2μs
   │
9. Template render (if HTML response):
   │   LRU cache hit → walk Zig node tree                          ~36μs
   │       ╰─ Variable resolution: sentinel-terminated VarPath
   │       ╰─ Filter chain: 49 native filters
   │       ╰─ Output: contiguous Zig buffer → Python bytes
   │
10. Response flows back through middleware (innermost → outermost)
    │   TelemetryMiddleware: end span, record metrics
    │   TimingMiddleware: set X-Response-Time header
    │
11. Zig HTTP server: serialize response, write to socket           ~0.01ms
    │
12. nginx: buffer response, send to client (TLS encrypt)           ~0.5ms
```

**Total overhead of the framework** (excluding your handler logic and DB query time): ~50-100μs. At typical 1-10ms handler execution times, framework overhead is 0.5-5% of total request time.

---

## Caching at Each Layer

```
Layer           Cache Type              TTL        Shared Across
─────           ──────────              ───        ─────────────
nginx           proxy_cache             minutes    All clients
CacheMiddleware Full-page response      seconds    All threads (per-instance)
TwoTierCache    Application data
  L1            LocMemCache (in-proc)   10s        All threads (per-instance)
  L2            DatabaseCache (PG)      300s       All instances
QueryCache      ORM query results       per-model  All threads (per-instance)
TemplateEngine  Compiled node trees     permanent  All threads (per-instance)
pg.zig          Prepared statements     permanent  Per-connection
```

**Invalidation strategy per layer:**

- **nginx proxy_cache:** TTL-based. Use `Cache-Control` headers from HyperDjango.
- **CacheMiddleware:** TTL-based. Skips authenticated users by default.
- **TwoTierCache:** TTL-based (L1 short, L2 long). Manual `delete()` for explicit invalidation.
- **QueryCache:** Version-based. `post_save`/`post_delete` signals bump table version — all cached queries for that table are instantly stale without scanning.
- **TemplateEngine:** Source mtime check (dev mode) or permanent (production with `auto_reload=False`).
- **pg.zig prepared statements:** Permanent per-connection. LRU eviction at 256 entries via DEALLOCATE.

---

## Database Topology

### UNLOGGED Tables

HyperDjango uses PostgreSQL UNLOGGED tables for three subsystems that need speed but don't need crash durability:

| Subsystem   | Table               | Why UNLOGGED                                                                |
| ----------- | ------------------- | --------------------------------------------------------------------------- |
| Cache       | `hyper_cache`       | Cache is ephemeral by nature. 2-3x faster writes without WAL.               |
| Sessions    | `hyper_sessions`    | Session loss on crash = users log in again. Acceptable trade-off for speed. |
| Rate limits | `hyper_rate_limits` | Rate limit counters reset naturally. No value in persisting across crash.   |

UNLOGGED tables are **not replicated** to read replicas (by design). This is correct — cache, session, and rate limit data should be written to the primary and read from the primary.

### Connection Pool Architecture

pg.zig uses **thread-owned connection pinning**: each Zig HTTP worker thread acquires one connection from the pool and keeps it for the thread's lifetime. This eliminates per-request pool acquire/release overhead.

```
Thread 1 ──── Connection 1 ──── PostgreSQL backend 1
Thread 2 ──── Connection 2 ���─── PostgreSQL backend 2
  ...           ...                  ...
Thread 24 ─── Connection 24 ─── PostgreSQL backend 24
              Connection 25-32   spare capacity for transactions, background tasks
```

**Pool sizing rule:** `POOL_SIZE >= THREAD_POOL_SIZE + 8`. The extra 8 connections provide headroom for pinned transactions (`atomic()` blocks), background tasks, and the pg.zig health checker.

---

## Zero-Downtime Deploys

### Rolling Restart Behind nginx

```bash
# 1. Deploy new code to server
git pull && uv sync && uv run hyper-build --release

# 2. Graceful restart (SIGTERM → drain active requests → exit → start new)
uv run hyper restart --app app:app

# 3. nginx health check detects the brief gap and routes to other instances
```

`hyper restart` sends SIGTERM to the running process. The Zig server:

1. Stops accepting new connections
2. Waits for active requests to complete (configurable drain timeout, default 30s)
3. Closes all connections and exits cleanly
4. The new process starts and begins accepting

With 2+ instances behind nginx, one instance restarts while others serve traffic. nginx `upstream` health checks (hitting `/health`) detect unavailable instances and route around them.

### Database Migrations

```bash
# Run migrations before restarting (additive changes are safe during rolling restart)
uv run hyper migrate --app app:app

# For destructive migrations (column drops, table renames):
# 1. Deploy code that handles both old and new schema
# 2. Run migration
# 3. Deploy code that only handles new schema
# 4. Remove backward-compat code
```

---

## Monitoring Stack

### Prometheus

```python
from hyperdjango.telemetry import configure_from_settings

telemetry = configure_from_settings(app)
app.get("/metrics")(telemetry.prometheus_sink.handler)
```

**Key dashboards:**

1. **Request Rate & Latency** — `hyperdjango_http_requests_total`, `hyperdjango_http_request_duration_seconds` (p50, p95, p99)
2. **Error Rate** — `hyperdjango_http_requests_total{status="5xx"}` / total
3. **Database Pool** — `hyperdjango_pool_available`, `hyperdjango_pool_waiters`, `hyperdjango_pool_acquires`
4. **Cache** — hit rate from `cache.stats`, `hyperdjango_cache_operations_total`
5. **Rate Limiting** �� `hyperdjango_rate_limit_hits_total` by backend

### Log Aggregation

```python
from hyperdjango.logging import logger

# JSON sink for structured logging
logger.add(json_sink, level="INFO", serialize=True)
```

With `TELEMETRY_AUTO_LOG_CORRELATION=True`, every log entry inside an active span automatically includes `trace_id` and `span_id` — log aggregators join logs to traces without custom configuration.

---

## Security Posture

| Layer                         | Protection                                                                      | Implementation                     |
| ----------------------------- | ------------------------------------------------------------------------------- | ---------------------------------- |
| **nginx**                     | TLS, HTTP/2, volumetric rate limiting, request buffering                        | Config-level                       |
| **SecurityHeadersMiddleware** | X-Frame-Options, X-Content-Type-Options, X-XSS-Protection, Referrer-Policy, CSP | Automatic                          |
| **CORSMiddleware**            | Origin validation, preflight caching                                            | Allowlist                          |
| **CSRFMiddleware**            | HMAC double-submit cookie                                                       | Automatic on POST/PUT/PATCH/DELETE |
| **RateLimitMiddleware**       | Per-IP, per-user, per-org rate limits with IETF headers                         | PostgreSQL-backed                  |
| **SessionAuth**               | HMAC-signed session cookies, argon2id password hashing                          | Token rotation                     |
| **HyperGuard**                | Declarative RBAC (roles, permissions, field-level access)                       | `@guard(Require.role("admin"))`    |
| **PublicIDMixin**             | HMAC-signed opaque IDs (anti-enumeration)                                       | Per-model                          |
| **is_safe_redirect_url**      | Open redirect prevention on `?next=` parameters                                 | Centralized validation             |
| **Input validation**          | Native Zig SIMD validation at 1.6M models/sec                                   | Per-field type specs               |
| **SQL injection**             | All queries parameterized via ORM; DDL from Model metadata only                 | Framework-enforced                 |

See [Security Guide](security-guide.md) for the full security checklist.
