# Architecture Overview

HyperDjango is a Django-inspired web framework with native Zig acceleration. This
document maps the module structure, data flow, and extension points.

## System Architecture

```
                          ┌──────────────────────────────────┐
                          │       Python Application         │
                          │  HyperApp, Models, Views, Auth   │
                          └──────────┬───────────────────────┘
                                     │
                          ┌──────────▼───────────────────────┐
                          │     HyperDjango Framework        │
                          │  ORM, REST, Forms, Admin, Tasks  │
                          │  Middleware, Auth, Templates     │
                          └──────────┬───────────────────────┘
                                     │ C FFI (PyMethodDef)
                          ┌──────────▼───────────────────────┐
                          │  _hyperdjango_native.so (Zig)    │
                          │  HTTP Server  │  pg.zig Driver   │
                          │  JSON (SIMD)  │  Multipart       │
                          │  Router       │  Validator       │
                          │  Templates    │  String Ops      │
                          └──────────┬───────────────────────┘
                                     │
                          ┌──────────▼───────────────────────┐
                          │    OS / Network / PostgreSQL     │
                          └──────────────────────────────────┘
```

## Request Lifecycle

```
TCP socket (Zig epoll/kqueue)
  │
  ▼
Parse HTTP request (Zig — headers, method, path)
  │
  ├─ Route resolve (Zig radix trie, ~500ns)
  │
  ├─ Pre-GIL validation (Zig — JSON schema, field types)
  │
  ├─ Multipart detection + pre-parse (Zig — boundary extraction)
  │
  ├─ Body handling decision:
  │   ├─ Small body (< MAX_BODY_SIZE): buffer in memory, pass as PyBytes
  │   └─ Large body (>= MAX_BODY_SIZE): streaming mode (ThreadLocal socket state)
  │
  ▼
Attach Python thread state (free-threaded 3.14t — no GIL)
  │
  ▼
Python middleware chain (CORS → Security → RateLimit → Timing → Auth → ...)
  │
  ▼
Route handler (async def)
  │
  ├─ request.json() / request.form() / request.files() / request.stream()
  ├─ ORM queries via pg.zig (prepared stmts, connection pool)
  ├─ Template rendering (Zig compiled engine)
  │
  ▼
Response (JSON / HTML / redirect / SSE / file / stream)
  │
  ▼
Zig HTTP response writer → TCP socket
```

## Module Dependency Map

### Core Layer (zero dependencies within framework)

| Module        | Purpose                                                          |
| ------------- | ---------------------------------------------------------------- |
| `conf.py`     | Settings with 4-tier resolution (Django → env → .env → DEFAULTS) |
| `response.py` | Response.json/html/text/redirect/stream/sse/file                 |
| `request.py`  | Request with lazy JSON/form/cookie/files parsing                 |

### ORM Layer (depends on: core, pg.zig)

| Module           | Purpose                                              |
| ---------------- | ---------------------------------------------------- |
| `models.py`      | Model base class, Field, Meta, QuerySet, save/delete |
| `where.py`       | WhereNode — composable WHERE clause tree             |
| `query.py`       | QuerySet — compiled SQL cache, 520K qps              |
| `lookups.py`     | 21 lookups + 6 transforms + custom registration      |
| `expressions.py` | F, Q, Value, Count, Sum, Avg, Case/When, Subquery    |
| `database.py`    | Connection pool, transactions, atomic(), on_commit() |

### Web Layer (depends on: core, ORM)

| Module                     | Purpose                                                       |
| -------------------------- | ------------------------------------------------------------- |
| `app.py`                   | HyperApp — routes, middleware, tasks, listen, native server   |
| `router.py`                | Radix trie router with `{param:type}` syntax                  |
| `standalone_middleware.py` | CORS, Security, RateLimit, Timing, CSRF, Compression, Version |
| `templating.py`            | Jinja2 integration with Zig-compiled engine                   |
| `staticfiles.py`           | Static files finder, manifest, collectstatic, dev `?v=hash`   |
| `versioning.py`            | AppVersion, cache busting, HTMX mismatch detection            |

### Auth Layer (depends on: core, ORM)

| Module                | Purpose                                        |
| --------------------- | ---------------------------------------------- |
| `auth/sessions.py`    | SessionAuth — signed cookies via TokenEngine   |
| `auth/user.py`        | User model, SessionUser wrapper, AnonymousUser |
| `auth/oauth2.py`      | OAuth2 providers (Google, GitHub, Auth0)       |
| `auth/permissions.py` | Hierarchical RBAC with CTE role inheritance    |

### API Layer (depends on: core, ORM, web, auth)

| Module           | Purpose                                                       |
| ---------------- | ------------------------------------------------------------- |
| `rest.py`        | ModelViewSet, serializers, pagination, permissions, throttles |
| `serializers.py` | Read/write shapes, nested, computed fields, validation        |
| `openapi.py`     | OpenAPI 3.1 + Swagger UI + `@api_input`/`@api_output`         |

### Admin (depends on: core, ORM, web, auth)

| Module               | Purpose                                      |
| -------------------- | -------------------------------------------- |
| `admin/__init__.py`  | HyperAdmin — auto-CRUD, RBAC management UI   |
| `admin/fields.py`    | AdminField, Fieldset, Action, InlineConfig   |
| `admin/templates.py` | All HTML templates including RBAC management |

### Infrastructure (depends on: core)

| Module         | Purpose                                                       |
| -------------- | ------------------------------------------------------------- |
| `tasks.py`     | Background task queue (@task, .delay(), priority, retry, DLQ) |
| `cache.py`     | LocMemCache (LRU) + DatabaseCache (PostgreSQL UNLOGGED)       |
| `ratelimit.py` | InMemory + PostgreSQL UNLOGGED rate limiting                  |
| `signing.py`   | TokenEngine — HMAC-signed tokens with key rotation            |
| `channels.py`  | WebSocket pub/sub with InMemory + PgChannelLayer              |
| `realtime.py`  | Room, NotificationManager, LiveQuery, ConnectionManager       |

### Telemetry (depends on: core)

| Module                    | Purpose                                     |
| ------------------------- | ------------------------------------------- |
| `telemetry/metrics.py`    | Counter, Gauge, Histogram with Vec variants |
| `telemetry/tracing.py`    | Tracer, Span, SpanContext                   |
| `telemetry/middleware.py` | TelemetryMiddleware + drain worker          |
| `telemetry/sinks/`        | Prometheus, Stdout, InMemory sinks          |

### Zig Native Extension

| Module                 | Purpose                                                           |
| ---------------------- | ----------------------------------------------------------------- |
| `server.zig`           | HTTP server, request parsing, streaming body, graceful shutdown   |
| `router.zig`           | Radix trie router                                                 |
| `router_bridge.zig`    | C API exposing radix trie router to Python                        |
| `db.zig`               | pg.zig PostgreSQL driver + COPY + LISTEN/NOTIFY                   |
| `json_parser.zig`      | SIMD JSON parser (model_dump(), **dict**, bigint)                 |
| `multipart.zig`        | Zero-copy multipart form parser                                   |
| `model_validator.zig`  | Native model validation (compile specs, init, validate)           |
| `batch_validator.zig`  | SIMD batch validation (51M ints/sec)                              |
| `dhi_validator.zig`    | Pre-GIL JSON schema validation for request bodies                 |
| `string_ops.zig`       | SIMD html_escape, url_encode/decode, xor_bytes                    |
| `websocket_server.zig` | RFC 6455 WebSocket with SIMD XOR unmask                           |
| `template_engine.zig`  | Jinja2-compatible compiled template engine with native filters    |
| `where_compiler.zig`   | WhereNode FNV-1a cache key + SQL compile acceleration             |
| `guard_eval.zig`       | HyperGuard bytecode evaluator — sub-50ns permission checks        |
| `hashring.zig`         | Consistent hash ring (ketama-compatible, SIMD scan, 3x uhashring) |
| `metrics_py.zig`       | Runtime-dynamic metric registry for Python FFI                    |
| `response.zig`         | Native HTTP response builder                                      |
| `static_helpers.zig`   | Native static file hashing (MD5) and gzip compression             |
| `file_watcher.zig`     | kqueue (macOS) / inotify (Linux) native file watcher              |
| `log_helpers.zig`      | Native logging helpers (ISO timestamp, basename, module name)     |
| `profiler.zig`         | Nanosecond precision timing primitives                            |
| `py.zig`               | Python C-API wrappers and type re-exports                         |
| `main.zig`             | C extension entry — 55+ PyMethodDef functions                     |

## Settings Resolution

```
1. Django settings (HYPERDJANGO_*)     ← highest priority
2. Environment variables (HYPER_*)
3. .env file
4. DEFAULTS dict in conf.py            ← lowest priority
```

All settings read via `get_setting()` with two-tier cache. No per-request env reads.
Every setting is declared with validation in `SETTING_DEFINITIONS`.

## Auth Architecture

Two auth paths, both producing the same user interface:

```
SessionAuth path (lightweight):
  Session cookie → TokenEngine verify → Session store lookup
  → SessionUser(_data=dict) → request.user
  Properties: .id, .username, .is_staff, .has_perm()

SessionAuth path (full RBAC, db=):
  Session cookie → Load real User from DB every request
  → User model instance → request.user
  Adds: real DB-backed permissions, role hierarchy, object perms
```

`request.user` is always `SessionUser | User | AnonymousUser | None`.

## Upload Architecture

```
Small body (< MAX_BODY_SIZE, default 10MB):
  Zig reads full body → PyBytes → request.body → parse multipart
  → UploadedFile(memory) if file < FILE_UPLOAD_MAX_MEMORY_SIZE
  → UploadedFile(disk) if file >= FILE_UPLOAD_MAX_MEMORY_SIZE

Large body (>= MAX_BODY_SIZE):
  Zig stores socket + Content-Length in thread-local StreamBodyState
  Python calls request.stream() → _read_body_chunk FFI → yields chunks
  Same Zig worker thread — no cross-thread coordination
```

## Bundled Services

17 production-ready apps in `services/`:

| App             | Showcases                                                      |
| --------------- | -------------------------------------------------------------- |
| hello           | Minimal starter                                                |
| rest_api        | CRUD, opaque IDs, session + API key auth, OpenAPI              |
| full_stack      | Templates, forms, admin panel                                  |
| benchmark_app   | EXPLAIN ANALYZE performance testing                            |
| hypernews       | Voting, eigenvector ranking, keyset pagination, StatusTimeline |
| hyperai         | LLM chat, SSE streaming, tiered rate limits                    |
| semantic_search | pgvector, HNSW cosine, OpenAI-compatible embeddings            |
| websocket_chat  | Zig RFC 6455, rooms, presence, typing                          |
| bookstore_api   | ViewSets, serializers, CursorPagination, ETag, nested routers  |
| task_queue      | @app.task, priorities, retry, DLQ, TaskGroup, cron             |
| multi_tenant    | TenantMixin, tenant isolation, StatusTimeline                  |
| content_hub     | Q objects, OneToOneField, STI, HyperAdmin, custom actions      |
| forms_demo      | Form validation, ModelForm, cross-field clean                  |
| deployment      | systemd, nginx, env template, health probes                    |
| hyperticket     | Multi-tenant SaaS, guard access control, 27+ models            |
| notes_api       | Session auth, F expression updates, CursorPagination           |
| otlp_sink       | OTLP/HTTP JSON span exporter sink for telemetry                |

Each app is self-contained with `app.py`, `seed.py`, and E2E tests.
