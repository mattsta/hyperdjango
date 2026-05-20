# HyperDjango Documentation

Hypermodern web framework with native Zig performance. Django-inspired ergonomics, compiled HTTP server, SIMD validation, native PostgreSQL driver. Free-threaded Python 3.14t.

15,000+ tests. 22 services. 150 configurable settings. Zero external runtime dependencies.

## Quick Start

```bash
uv add hyperdjango
uv run hyper-build --release
uv run hyper new myapp --full
cd myapp && uv run hyper run
```

```python
from hyperdjango import HyperApp, Response

app = HyperApp("myapp", database="postgres://localhost/mydb")


@app.get("/")
async def index(request):
    return {"message": "Hello from HyperDjango!"}


@app.get("/articles/{id:int}")
async def article(request, id: int):
    article = await Article.objects.get(id=id)
    return Response.json(article.to_dict())
```

For the full walkthrough, see the [Getting Started](getting-started.md) guide.

---

## Learn

Start here if you're new to HyperDjango.

| Doc                                   | What you'll learn                                                               |
| ------------------------------------- | ------------------------------------------------------------------------------- |
| [Architecture](architecture.md)       | System diagram, request lifecycle, module dependency map                        |
| [Getting Started](getting-started.md) | Install, build, first app, models, views, templates, forms, admin, REST, deploy |
| [Tutorial](tutorial.md)               | Step-by-step project from scratch                                               |
| [Patterns](patterns.md)               | Standard coding patterns used across all 22 services                            |

---

## Models & Database

Define your data, query it, migrate it.

| Doc                                         | What it covers                                                                                          |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| [Models](models.md)                         | Model class, Field types, Meta options, inheritance (abstract/proxy/STI/unlogged), save/delete, to_dict |
| [Models Guide](models-guide.md)             | Practical guide — defining models, relationships, FKs, composite PKs                                    |
| [Queries Guide](queries-guide.md)           | filter, exclude, order_by, select_related, prefetch_related, annotate, aggregate                        |
| [Expressions](expressions.md)               | F, Q, Value, Count, Sum, Avg, Max, Min, Case/When, Subquery                                             |
| [Lookups](lookups.md)                       | 21 built-in lookups (including pgvector), 12 transforms                                                 |
| [Custom Lookups](custom-lookups.md)         | Register your own lookup and transform types                                                            |
| [Database](database.md)                     | pg.zig native driver, connection pool, prepared statements, COPY protocol                               |
| [Transactions](transactions.md)             | atomic(), savepoints, on_commit()                                                                       |
| [Migrations](migrations.md)                 | Introspect, diff, migrate, rollback, verify, snapshot                                                   |
| [Migrations Guide](migrations-guide.md)     | Practical migration workflows                                                                           |
| [Multi-Database](multi-db.md)               | ConnectionManager, DatabaseRouter, PrimaryReplicaRouter                                                 |
| [PostgreSQL Extensions](postgres-ext.md)    | ArrayField, SearchVector/Query/Rank, trigrams, aggregates, ranges, indexes                              |
| [Pool Optimization](pool.md)                | Connection pool tuning, SlowQueryLog, PoolHealthChecker, heartbeat                                      |
| [Query Cache](query-cache.md)               | Transparent query cache with FK dependency tracking                                                     |
| [DB Instrumentation](db-instrumentation.md) | Query profiling, slow query detection                                                                   |
| [PostgreSQL](postgres.md)                   | PostgreSQL-specific features and types                                                                  |
| [Embeddings](embeddings.md)                 | pgvector embeddings, HNSW indexes, cosine/L2/inner product                                              |

---

## Views, Routing & Templates

Handle requests and render responses.

| Doc                                       | What it covers                                                                        |
| ----------------------------------------- | ------------------------------------------------------------------------------------- |
| [Request & Response](request-response.md) | Request (JSON/form/cookies/files/stream), Response (json/html/text/redirect/sse/file) |
| [Routing](routing.md)                     | URL routing, `{param:type}` syntax, radix trie                                        |
| [Views](views.md)                         | Function views, decorators, shortcuts                                                 |
| [Views Guide](views-guide.md)             | Practical patterns for views                                                          |
| [Class-Based Views](class-based-views.md) | ListView, DetailView, CreateView, UpdateView, DeleteView + auth mixins                |
| [Templates](templates.md)                 | Zig-compiled Jinja2-compatible engine — filters, macros, extends, include             |
| [Templates Guide](templates-guide.md)     | Practical template usage, custom filters, backend config                              |
| [Middleware](middleware.md)               | CORS, Security, RateLimit, Timing, CSRF, Compression, Version, Logging                |
| [Static Files](static-files.md)           | StaticFilesFinder, ManifestStaticFilesStorage, collectstatic, content-hash filenames  |
| [Conditional Views](conditional-views.md) | ETag, Cache-Control, 304 Not Modified                                                 |

---

## REST API

Build production-grade APIs.

| Doc                           | What it covers                                                                                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| [REST Framework](rest.md)     | ModelViewSet, APIRouter, pagination (4 styles), filters (3 backends), permissions, throttling, bulk ops, nested routers, ETag caching, `@action` |
| [Serializers](serializers.md) | ModelSerializer, read/write shapes, nested, computed fields, validation                                                                          |
| [OpenAPI](openapi.md)         | OpenAPI 3.1 auto-generation, Swagger UI, `@api_input`/`@api_output` decorators                                                                   |
| [Pagination](pagination.md)   | PageNumber, LimitOffset, keyset CursorPagination, ServerCursorPagination                                                                         |

---

## Auth & Security

Sessions, OAuth2, RBAC, API keys, CSRF, and access control.

| Doc                                 | What it covers                                                                                                               |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| [Auth & RBAC](auth.md)              | SessionAuth (with db= for RBAC), User/SessionUser/AnonymousUser, hierarchical RBAC (CTE), object permissions, 5 rule types   |
| [Auth Guide](auth-guide.md)         | Practical auth setup — custom backends, LDAP, SAML                                                                           |
| [Guard](guard.md)                   | HyperGuard access control — `Require.authenticated()`, `Require.has_perm()`, `Require.has_active_status()`, WebSocket guards |
| [Sessions](sessions.md)             | Session stores, cookie configuration, session lifecycle                                                                      |
| [Signing](signing.md)               | TokenEngine — HMAC-signed tokens with key rotation, XOR obfuscation, SignedSessionMixin, SignedAPIKeyMixin                   |
| [Security](security.md)             | Security headers, audit log                                                                                                  |
| [Security Guide](security-guide.md) | CSRF, CORS, headers, hardening checklist                                                                                     |
| [CSRF](csrf.md)                     | CSRF protection — double-submit cookie, trusted origins                                                                      |
| [Content Security Policy](csp.md)   | CSP header configuration                                                                                                     |
| [Unified IDs](ids.md)               | Anti-enumeration IDs — 4 modes (raw/encoded/signed/random), HMAC key rotation, time-windowed, per-user                       |
| [Public IDs](public-ids.md)         | Legacy PublicIDMixin reference                                                                                               |

---

## Forms

Validate user input with Django-style forms.

| Doc                           | What it covers                                                     |
| ----------------------------- | ------------------------------------------------------------------ |
| [Forms](forms.md)             | Form, ModelForm, 12 field types, custom validators, error display  |
| [Forms Guide](forms-guide.md) | Practical form patterns — validation, cross-field clean, rendering |
| [FormSets](formsets.md)       | FormSets for multiple form instances                               |

---

## Admin

Auto-generate CRUD admin panels from your models.

| Doc                           | What it covers                                                                                        |
| ----------------------------- | ----------------------------------------------------------------------------------------------------- |
| [Admin](admin.md)             | HyperAdmin — register models, list/search/filter/create/edit/delete, bulk actions, RBAC management UI |
| [Admin Guide](admin-guide.md) | Customization — fieldsets, actions, inlines, themes                                                   |

---

## Real-Time & WebSocket

WebSocket, pub/sub, rooms, live queries, notifications.

| Doc                               | What it covers                                                                                         |
| --------------------------------- | ------------------------------------------------------------------------------------------------------ |
| [Channels](channels.md)           | Channel, ChannelGroup, InMemoryChannelLayer, PgChannelLayer (LISTEN/NOTIFY), presence, history         |
| [Real-time Patterns](realtime.md) | Room (chat/moderation), NotificationManager, LiveQuery (model change subscriptions), ConnectionManager |

---

## Background Tasks

In-process task queue with priority, retry, scheduling, and dead letter queue.

| Doc               | What it covers                                                                                                |
| ----------------- | ------------------------------------------------------------------------------------------------------------- |
| [Tasks](tasks.md) | @task decorator, .delay(), TaskPriority, retry with exponential backoff, TaskScheduler (cron), TaskGroup, DLQ |

---

## Uploads & Streaming

Three-mode file uploads — memory, disk spill, pass-through streaming.

| Doc                                 | What it covers                                                |
| ----------------------------------- | ------------------------------------------------------------- |
| [File Uploads](uploads.md)          | request.files(), request.stream(), UploadedFile API, settings |
| [Files & Storage](files.md)         | FileSystemStorage, MemoryStorage, pluggable backends          |
| [Custom Storage](custom-storage.md) | Build your own storage backend                                |

---

## Caching & Performance

Multiple caching layers and performance monitoring.

| Doc                                       | What it covers                                                                         |
| ----------------------------------------- | -------------------------------------------------------------------------------------- |
| [Cache](cache.md)                         | LocMemCache (LRU) + DatabaseCache (PostgreSQL UNLOGGED), @cached decorator             |
| [Cache Adapters](cache-adapters.md)       | ConsistentHashRing (native Zig, 3x), StampedeProtection (XFetch), TwoTierCache (L1+L2) |
| [Rate Limiting](ratelimit.md)             | InMemory + PostgreSQL UNLOGGED, tiered limits, per-path/method/cost rules              |
| [Performance](performance.md)             | PerformanceMiddleware — query tracking, slow queries, N+1 detection, dashboard         |
| [Performance Guide](performance-guide.md) | Optimization workflows, profiling, benchmarks                                          |
| [Profiling](profiling.md)                 | Nanosecond profiler, @profile_handler, X-Profile header, flame graphs                  |

---

## Telemetry & Observability

Native metrics and distributed tracing.

| Doc                       | What it covers                                                                                |
| ------------------------- | --------------------------------------------------------------------------------------------- |
| [Telemetry](telemetry.md) | Tracer, Span, Counter/Gauge/Histogram, W3C traceparent, sampling, sinks, auto log correlation |
| [Metrics](metrics.md)     | Prometheus-compatible metrics export                                                          |
| [Logging](logging.md)     | Production logging — loguru-compatible API, JSON/console/file sinks, rotation, color markup   |

---

## Multi-Tenancy & Metering

SaaS features — tenant isolation, usage tracking, quotas.

| Doc                           | What it covers                                                                                                    |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| [Multi-Tenancy](tenancy.md)   | TenantMixin, TenantMiddleware, auto-scoped queries, tenant resolvers                                              |
| [Usage Metering](metering.md) | MeterEngine, events, aggregates, quotas, alert hooks                                                              |
| [Timeline](timeline.md)       | StatusTimeline — temporal status tracking (ban/mute/lock/staff with time ranges, actor, history, auto-escalation) |

---

## Asset Versioning

Cache busting, HTMX mismatch detection, blue/green deployments.

| Doc                         | What it covers                                                                                                                                                     |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [Versioning](versioning.md) | AppVersion, VersionMiddleware, X-App-Version + X-App-Version-Action headers, X-Client-Version cohort broadcast, /version endpoint, /cache/bust, blue/green routing |

---

## Internationalization

Translation, locale-aware formatting, plural rules.

| Doc                   | What it covers                                                                                |
| --------------------- | --------------------------------------------------------------------------------------------- |
| [i18n](i18n.md)       | gettext/ngettext, PO file parsing, 40+ language plural rules, LocaleMiddleware, URL prefixing |
| [Formats](formats.md) | Locale-aware date/time/number/currency formatting, template filters                           |

---

## Contrib Modules

Batteries-included modules for common web patterns.

| Doc                           | What it covers                                                                                                |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------- |
| [Custom Fields](fields.md)    | 14 built-in types — MoneyField, EmailField, SlugField, URLField, ColorField, PhoneField, EncryptedField, etc. |
| [Mixins](mixins.md)           | TimestampMixin, SoftDeleteMixin, OwnershipMixin, VersionedMixin                                               |
| [Email](email.md)             | EmailMessage — SMTP/console/memory backends, HTML+text                                                        |
| [Messages](messages.md)       | Flash messages across redirects (session-based)                                                               |
| [Signals](signals.md)         | Signal class — pre/post_save, pre/post_delete, 9 built-in                                                     |
| [Redirects](redirects.md)     | RedirectRegistry, O(1) lookup, prefix matching, open-redirect protection                                      |
| [Flatpages](flatpages.md)     | Simple CMS pages, auth-gated, template rendering                                                              |
| [Sitemaps](sitemaps.md)       | XML sitemap generation, 50K pagination, ETag caching                                                          |
| [Syndication](syndication.md) | RSS/Atom feed generation, enclosures, podcast support                                                         |
| [Humanize](humanize.md)       | ordinal, intcomma, intword, naturaltime, filesizeformat                                                       |
| [Fixtures](fixtures.md)       | dumpdata/loaddata, JSON, FK dependency sorting, natural keys, UPSERT                                          |
| [Search](search.md)           | Full-text search integration                                                                                  |
| [Async](async.md)             | Async reference                                                                                               |
| [Async Guide](async-guide.md) | Async usage patterns                                                                                          |

---

## Production & Deployment

Ship to production with confidence.

| Doc                                     | What it covers                                                                                       |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| [Deployment](deployment.md)             | Deployment reference                                                                                 |
| [Deployment Guide](deployment-guide.md) | Docker, systemd, nginx, env templates, health probes, checklist                                      |
| [Tuning](tuning.md)                     | All configurable tuning parameters — task queue, perf middleware, slow query, rate limit, hot reload |
| [Error Reporting](error-reporting.md)   | Error handling and reporting                                                                         |

---

## Configuration & CLI

Settings, commands, diagnostics.

| Doc                        | What it covers                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------- |
| [Settings](settings.md)    | Validated settings — 4-tier resolution (Django → env → .env → defaults)                           |
| [CLI Commands](cli.md)     | `hyper` CLI — new, run, start, stop, restart, status, routes, migrate, doctor, benchmark, systemd |
| [System Checks](checks.md) | @register, CheckMessage, run_checks, 5 built-in checks                                            |
| [Commands](commands.md)    | Custom management commands — @command decorator, typed args, CLI discovery                        |

---

## Testing

Test your application with built-in tools.

| Doc                               | What it covers                                           |
| --------------------------------- | -------------------------------------------------------- |
| [Testing](testing.md)             | TestClient, auth helpers, cookie persistence             |
| [Test Harness](test-harness.md)   | `testkit` API, marker taxonomy, env contract, CI ladder  |
| [Testing Guide](testing-guide.md) | Practical testing patterns, E2E testing, Hypothesis fuzz |

---

## Internals

For contributors and deep understanding.

| Doc                                     | What it covers                                                         |
| --------------------------------------- | ---------------------------------------------------------------------- |
| [Validation](validation.md)             | Native Zig validation engine (4,350 lines)                             |
| [Server](server.md)                     | Zig HTTP server — 24-thread pool, radix trie router, graceful shutdown |
| [Legacy Databases](legacy-databases.md) | Working with existing databases (inspectdb)                            |
| [API Reference](api.md)                 | Complete API reference                                                 |

---

## Performance

| Component        | Metric          | vs Baseline    |
| ---------------- | --------------- | -------------- |
| HTTP server      | 548K req/s peak | 2.3x uvicorn   |
| Route resolve    | 808ns           | Radix trie     |
| SELECT by PK     | 21K ops/s       | 2.06x psycopg3 |
| COPY import      | 536K rows/s     | 42.8x INSERT   |
| JSON parse       | 94ns            | 6.1x stdlib    |
| Template render  | 36us            | 1.7x Jinja2    |
| Template compile | 7.1us           | 234x Jinja2    |
| Model validation | 0.6us           | 4.3x Python    |
| SQL cache lookup | 520K qps        | 2x+ uncached   |
