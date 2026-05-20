# HyperDjango

**Hypermodern web framework with native Zig performance.** Django-inspired API, compiled HTTP server, SIMD validation, native PostgreSQL driver. Free-threaded Python 3.14t, no GIL.

- **2-10x faster** than equivalent Python libraries across every hot path
- **Django-compatible** — drop into existing Django apps, or build new standalone apps
- **Production-grade** — a fully counted, zero-failure test suite; services; RBAC, rate limiting, telemetry, graceful shutdown
- **Single binary** — no external dependencies beyond PostgreSQL

## Two starting points

| You want to…                                                    | Go to                                                                 |
| --------------------------------------------------------------- | --------------------------------------------------------------------- |
| **Run this repository's bundled services** (you just cloned it) | [Clone → running service](#clone--running-service-3-commands) — below |
| **Start your own new project** from the published package       | [Quickstart](#quickstart-5-minutes) — further below                   |

---

## Clone → running service (3 commands)

You cloned this repository and want to see a real service running. The `services/`
tree holds production-quality services — complete applications with models,
auth, seeded data and an admin panel — and one command takes any of them from
nothing to serving.

```bash
git clone https://github.com/mattsta/hyperdjango.git
cd hyperdjango

make bootstrap-db                      # uv + CPython 3.14t + venv + deps + Zig
                                       # + native build + PostgreSQL provisioning

uv run hyper service run hypernews     # database, secrets, seed, serve
```

That prints every URL the service actually answers on — app root, `/admin/`,
health probes, `/docs` where it mounts them — plus the demo credentials its seed
used. Ctrl-C stops the service and any companions it started.

```bash
uv run hyper service list              # every service: port, needs, what it shows
uv run hyper service info hypersecret  # one service in full + the manual commands
uv run hyper service stop hypersecret  # stop it (and its companions) from elsewhere
```

**You must supply:** a PostgreSQL server (`make bootstrap-db` installs one on
Ubuntu; on macOS `brew install postgresql@18`), passwordless `sudo` for that
provisioning step, and network access. **Do not install Zig by hand** — the
bootstrap downloads a pinned, SHA-verified toolchain into `.toolchain/`.

Full walkthrough, per-service detail and a troubleshooting section keyed to real
failure modes: **[docs/running-services.md](docs/running-services.md)**.
Diagnose anything that goes wrong with `uv run hyper doctor`.

---

## Quickstart (5 minutes)

_(This section is the other journey: creating a **new, empty project** from the
published package. To run the services in this repository, use
[Clone → running service](#clone--running-service-3-commands) above.)_

**Prerequisites:** Python 3.14+, [Zig 0.16+](https://ziglang.org/download/), PostgreSQL 16+, [uv](https://docs.astral.sh/uv/)

```bash
# Create a new project
mkdir myapp && cd myapp
uv init && uv add hyperdjango

# Build the native Zig extension (required, one-time)
uv run hyper-build

# Scaffold a starter app
uv run hyper new myapp
```

This creates `app.py` with a working hello world:

```python
from hyperdjango import HyperApp, Response

app = HyperApp(title="My App")


@app.get("/")
async def index(request):
    return {"message": "Hello from HyperDjango!"}


@app.get("/greet/{name}")
async def greet(request, name):
    return {"greeting": f"Hello, {name}!"}


app.mount_health()  # GET /health → {"status": "ok"}

if __name__ == "__main__":
    app.run(port=8000)
```

```bash
uv run python app.py
# Visit http://localhost:8000 → {"message": "Hello from HyperDjango!"}
# Visit http://localhost:8000/greet/world → {"greeting": "Hello, world!"}
```

That's it. No database required for the hello world. When you're ready for models and persistence:

```bash
# Add a database (edit DATABASE in app.py, or set DATABASE_URL env var)
uv run hyper setup --app app:app          # Create tables from model definitions
uv run hyper setup --app app:app --seed seed:run  # Create tables + seed data
uv run hyper start --app app:app          # Start production server (background, PID file)
uv run hyper stop                         # Graceful shutdown
```

**Full documentation:** <https://mattsta.github.io/hyperdjango/> (built from `docs/` via MkDocs Material)

**Next steps:** See [Getting Started Guide](docs/getting-started.md) for models, auth, and REST APIs. Browse [22 Services](docs/services.md) for production patterns. Read the [Production Scaling Guide](docs/production-scaling.md) for caching, read replicas, and deployment.

---

## Two Ways to Use HyperDjango

### 1. Accelerate Existing Django Apps

Change your settings, keep your code. Everything gets faster.

```python
# settings.py — this is all you need
DATABASES = {
    "default": {
        "ENGINE": "hyperdjango.db",  # 2-5x faster PostgreSQL
        "NAME": "mydb",
        "HOST": "localhost",
    }
}

TEMPLATES = [
    {
        "BACKEND": "hyperdjango.serving.template_backend.ZigTemplates",  # 1.7x faster templates
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

MIDDLEWARE = [
    "hyperdjango.serving.django_middleware.HyperSecurityMiddleware",  # security headers
    "hyperdjango.serving.django_middleware.HyperCORSMiddleware",  # CORS (replaces django-cors-headers)
    "hyperdjango.serving.django_middleware.HyperTimingMiddleware",  # X-Response-Time
    "hyperdjango.serving.django_middleware.HyperRateLimitMiddleware",  # rate limiting (PostgreSQL-backed)
    "hyperdjango.serving.django_middleware.HyperPerformanceMiddleware",  # query tracking + N+1 detection
    "django.middleware.common.CommonMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "hyperdjango.serving.auth_backends.OAuth2Backend",  # Google/GitHub/Auth0 login
    "django.contrib.auth.backends.ModelBackend",  # password fallback
]
```

Then run your Django app normally. Models, admin, migrations, DRF, forms — everything works. Just faster.

```bash
python manage.py runziserver   # native Zig HTTP server (2x faster than gunicorn)
# or
python manage.py runserver     # standard Django dev server (still uses fast DB + templates)
```

### 2. Build New Apps with HyperApp

For new projects, HyperApp provides a full-featured async framework with Django-inspired ergonomics and the full power of the native Zig runtime:

```python
from hyperdjango import HyperApp, Model, Field, Response
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.mixins import TimestampMixin
from hyperdjango.rest import ModelSerializer, ModelViewSet, APIRouter, CursorPagination
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)

app = HyperApp(title="My API", database="postgres://localhost/mydb")

# Security + observability middleware (order matters: outermost first)
app.use(SecurityHeadersMiddleware())
app.use(TimingMiddleware())
app.use(CORSMiddleware(origins=["https://myapp.com"]))
app.use(RateLimitMiddleware(max_requests=120, window=60))

# Auth with signed session cookies
token_engine = TokenEngine(
    keys=[SigningKey(secret="change-me-in-production", version=1)]
)
auth = SessionAuth(secret="session-secret-change-me", token_engine=token_engine)
app.use(auth)


# Models — type annotations define the schema, `hyper setup` generates DDL
class Article(TimestampMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    body: str = Field()
    published: bool = Field(default=False)


# REST API with serializer, pagination, and auto-generated OpenAPI
class ArticleSerializer(ModelSerializer):
    class Meta:
        model = Article
        fields = ["id", "title", "body", "published", "created_at"]


class ArticleViewSet(ModelViewSet):
    serializer_class = ArticleSerializer
    model = Article
    pagination_class = CursorPagination  # HMAC-signed keyset pagination


router = APIRouter(prefix="/api/v1")
router.register("articles", ArticleViewSet)
router.mount(app.router, namespace="api")

app.mount_health()
app.mount_docs()  # OpenAPI 3.1 at /openapi.json, Swagger UI at /docs
```

```bash
uv run hyper-build                                    # Build native Zig extension
uv run hyper setup --app app:app --seed seed:run      # Create tables + seed data
uv run hyper start --app app:app                      # Start server
# GET /api/v1/articles/ → paginated list with HMAC-signed cursor links
# GET /docs → interactive Swagger UI
```

The services in `services/` demonstrate REST APIs, full-stack templates, real-time WebSocket, multi-tenant SaaS, background tasks, admin panels, semantic search, and more. The full test suite runs with zero failures (`uv run hyper-test`).

---

## Performance

Performance isn't an afterthought in HyperDjango — it's the entire reason the project exists. Every hot path runs in compiled Zig native code with zero Python overhead. The framework is built on the principle that the floor matters: the slowest sane configuration should already be fast, and nothing should require expert tuning to perform well.

### Architecture

```
HTTP request
    ↓
Zig HTTP Server (24-thread pool, GIL released for parsing)
    ↓
Zig Radix Trie Router (808 ns route resolve)
    ↓
Python Handler (only the application logic runs in Python)
    ↓
Zig pg.zig (native PostgreSQL wire protocol)
    ↓
Zig Template Engine + Zig JSON Serializer (SIMD)
    ↓
HTTP response
```

A single 581 KB compiled artifact (`_hyperdjango_native.so`) contains the HTTP server, PostgreSQL driver, template engine, JSON parser, model validator, multipart parser, WebSocket frame codec, SIMD string ops, and consistent hash ring. The serving runtime has no Python fallbacks — the native extension is required to run an app. Only the bootstrap tooling (the `hyper-build` front-end, the test runner's classification, the source-invariant gates, and the logging spine they share) imports without it, so the extension can be built from a clean checkout.

### What Gets Faster

| Component              | Speedup  | How                                                                                    |
| ---------------------- | -------- | -------------------------------------------------------------------------------------- |
| **PostgreSQL queries** | 2-5x     | Native pg.zig wire protocol, prepared statement caching, connection pooling            |
| **Dict query results** | 1.85x    | Native `_db_query_dicts` builds Python dicts in Zig with pre-interned column keys      |
| **JSON query results** | 2.66x    | Native `_db_query_json` builds JSON bytes directly from PostgreSQL wire protocol       |
| **Template rendering** | 1.7x     | Zig-compiled Jinja2-compatible engine with proper tokenizer + recursive descent parser |
| **JSON parsing**       | 6-10x    | SIMD-accelerated JSON parser (32-byte vector processing)                               |
| **Form validation**    | 4x+      | Native model validation via compiled field specs                                       |
| **Batch queries**      | 5.7x     | Connection pipelining — 20 queries in 1 round-trip instead of 20                       |
| **Batch execute**      | N×faster | `_db_exec_many`: Parse+Describe once, N×(Bind+Execute) with 256KB flush batches        |
| **HTTP serving**       | 2x       | Native Zig HTTP server with radix trie routing                                         |
| **JSON→Model**         | 2x       | Single-pass JSON parsing directly into validated model (no intermediate dict)          |

All speedups are measured, not estimated. Run `make bench` to verify on your hardware.

### HTTP server modes: threaded vs reactor — when to use which

The native HTTP server has two connection-handling models, selected by
`HYPER_HTTP_SERVER_MODEL`. **`reactor` is the default** because it degrades
gracefully under many connections; `threaded` is an opt-in max-throughput mode
for workloads whose connection count you _know_ is bounded.

|                                     | **Reactor** (default · safe)                                                                                                        | **Threaded** (opt-in)                                                                     |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Connection model                    | kqueue/epoll multiplexing: an idle connection costs only an fd; a worker touches a connection **only while a request is in flight** | thread-per-connection: a worker is pinned to a keep-alive connection for its whole life   |
| Concurrent-connection capacity      | thousands (bounded by fds/memory, not workers); **serves them all fairly**                                                          | ~W connections get served; the rest **starve** (see the ceiling note below)               |
| Peak throughput, _busy_ connections | ~10% lower (a per-request dispatch hop)                                                                                             | **highest** — no dispatch overhead                                                        |
| Per-request latency, _busy_         | ~0.5 ms floor (dispatch/wakeup)                                                                                                     | **lowest** (0.02–0.3 ms)                                                                  |
| Behaviour under load                | graceful — holds connections, latency rises evenly                                                                                  | **fairness collapse** — a few connections monopolise, the rest get ~1 response and stall  |
| Best for                            | **public / general web serving**, many idle browser keep-alives, C10K+ (the default)                                                | CPU-bound / high-RPS internal APIs behind a connection-pooling proxy, known-bounded conns |

**Measured on an 18-core box (`make bench`), W=18:**

| Workload                                      | Reactor                       | Threaded                                 |
| --------------------------------------------- | ----------------------------- | ---------------------------------------- |
| Max throughput, plaintext, busy connections   | ≈169k req/s, p50 0.53 ms      | **≈188k req/s**, p50 0.08 ms             |
| 1024 idle keep-alive conns (25 ms think-time) | **37,180 req/s**              | 666 req/s (≈W conns monopolise)          |
| 4096 idle keep-alive conns                    | **69,140 req/s, 100% served** | 666 req/s, only **43%** got any response |
| Few workers (W=1) vs 128 connections          | **120k req/s** (2.7×)         | 45k req/s                                |

**Measured on a 2× EPYC 7702 (server pinned to 64 physical cores, client to 64
disjoint cores, 10 s cells, `performance` governor, 2xx-only throughput — see
`docs/benchmarks.md` "Measurement-validity rules").**

_Many busy connections (c=1024 ≫ W) — the reactor's regime:_

| W (parallelism) | hyperdjango reactor                    | FastAPI (uvicorn, W procs)      | Flask (gunicorn, W×4 threads)  |
| --------------- | -------------------------------------- | ------------------------------- | ------------------------------ |
| 8               | 151k req/s                             | 41k req/s                       | 99k req/s                      |
| 16              | 269k req/s                             | 81k req/s                       | 151k req/s                     |
| 32              | 360k req/s                             | 149k req/s                      | 171k req/s                     |
| 64 (= cores)    | **479–548k req/s, p99 3.4 ms, 91 MiB** | 239k req/s, p99 6.3 ms, 4.1 GiB | 179k req/s, p99 35 ms, 3.6 GiB |

The reactor scales monotonically to the core budget — **2.3× FastAPI and 3×
Flask at peak, in ~1/45th the memory** — then declines past it like every
other model (scheduler oversubscription; the sweep tags those cells). The
threaded model at c ≫ W spends its capacity load-shedding by design (fast
503s past `W×8` backlog), so it is measured in its own regime below.

_Bounded connections (c ≤ 2W) — the threaded model's regime, same box:_

| Workload    | Threaded                   | Reactor                 |
| ----------- | -------------------------- | ----------------------- |
| W=8, c=8    | **109k req/s, p99 85 µs**  | 91k req/s, p99 103 µs   |
| W=16, c=16  | **209k req/s, p99 92 µs**  | 172k req/s, p99 119 µs  |
| W=32, c=32  | 232k req/s, p99 487 µs     | 234k req/s, p99 403 µs  |
| W=64, c=128 | **561k req/s, p99 299 µs** | 564k req/s, p99 1.76 ms |

Both models reach the same ~560k req/s machine ceiling at saturation; the
threaded model gets there with **~6× lower p99** (direct worker-per-connection
handoff, no dispatch hop) — its win when the connection count is genuinely
bounded. (At exactly c=W each worker idle-wakes per request, so throughput is
wakeup-latency-bound, not capacity-bound — drive c ≈ 2W to saturate.)

**Rule of thumb:** the reactor wins for anything public or connection-heavy (the
default). Switch to threaded only for an internal, high-RPS service whose
concurrent-connection count is _known_ to stay ≤ the thread pool — there it's
~10% faster with lower latency.

**Configuration** (env vars, `HYPER_`-prefixed; all safe by default):

| Setting                    | Default     | Effect                                                                                            |
| -------------------------- | ----------- | ------------------------------------------------------------------------------------------------- |
| `HYPER_HTTP_SERVER_MODEL`  | `reactor`   | `reactor` (safe, scales to many connections) or `threaded` (max throughput, bounded conns only)   |
| `HYPER_THREAD_POOL_SIZE`   | ≈ CPU cores | worker threads (W). More parallelism up to the core count; past cores adds context-switch latency |
| `HYPER_HTTP_REACTOR_COUNT` | auto (W/32) | reactor shards (reactor mode only). Auto keeps ≤ ~32 workers per queue; explicit values pin it    |
| `HYPER_HTTP_MAX_PENDING`   | W × 8       | threaded-mode load-shedding cap: past this backlog, new connections get a fast `503`. `0` = off   |
| `HYPER_DEBUG`              | `0`         | `1` = per-request trace logging (20–30% slower — dev only)                                        |

```bash
# Default — safe for public web serving, holds many idle keep-alives:
uv run hyper-serve                                    # reactor, W ≈ CPU cores

# Opt-in max-throughput for an internal API with bounded connections:
HYPER_HTTP_SERVER_MODEL=threaded HYPER_THREAD_POOL_SIZE=18 uv run hyper-serve
```

> #### ⚠ The threaded-mode connection ceiling
>
> In `threaded` mode a worker is pinned to a keep-alive connection for that
> connection's whole life (it blocks reading the next request between requests), so
> **at most `THREAD_POOL_SIZE` (W) connections are served at once.** Past `W` this
> is a fairness collapse, not an even slow-down: with `W=4` and 64 idle keep-alive
> connections (25 ms think-time), 4 connections receive ~76 requests each while the
> other 60 receive a single response and then get no worker again. That is why
> `reactor` is the default — use `threaded` only when concurrent connections are
> known to stay ≤ W.
>
> Threaded mode sheds load at its margin: once the accept backlog exceeds
> `HYPER_HTTP_MAX_PENDING` (default W × 8), new connections receive an immediate
> `503 Service Unavailable` + `Connection: close` rather than being queued to
> starve (`W=4`, 64 connections, cap 4 → 8 served, 56 get a `503` in ~4 ms).
> `HYPER_HTTP_MAX_PENDING=0` disables shedding. Details:
> `docs/design/http-connection-reactor.md`.

Reproduce and explore all of this interactively:
`uv run python -m benchmarks.core.runner --suite all` → open
`benchmarks/out/index.html` (a self-contained dashboard: HTTP + WebSocket, every
sweep, run history + compare, per-chart purpose descriptions and a
"who-wins-by-how-much" leaderboard).

### Subsystem Numbers

**Database (pg.zig vs psycopg3):**

- SELECT by PK: **2.06x** (21K vs 10K ops/sec)
- SELECT range: **4.18x**
- UPDATE: **1.52x**
- COPY bulk import: **42.8x** (536K rows/sec vs 12K INSERT)
- Micro-benchmark SELECT 50 rows: **365x** (69 µs vs 25 ms)
- Pipeline: 20 queries in 0.24 ms vs 1.40 ms sequential (**5.74x**, single round-trip)
- Prepared statement warmup: **7.7x** faster first-query latency (494 µs → 65 µs)

**Native template engine:**

- Compile: **7.1 µs** (234x faster than Jinja2)
- Render (cached): **36 µs** (1.7x faster)
- Native filters and is-tests with full Jinja2 parity
- Three-tier disk cache (`.hztc` files), Merkle dependency hashing
- Thread-safe LRU cache, default 256 MB

**SIMD JSON:**

- `json_loads` tiny object: **94 ns** (6.1x faster than stdlib)
- `json_loads` integer: **48 ns** (9.8x)
- `json_loads` boolean: **49 ns** (9.0x)
- `json_dumps` dict: **196 ns**

**Model validation (native, single FFI call per model):**

- Model creation: **1.6M models/sec** (0.6 µs, 4.3x faster than Python)
- Per-field validation: **6.7M fields/sec** (149 ns)
- Batch model validation: **13.1M models/sec** (SIMD 4-wide, 8.2x faster than individual)
- Email validation (SIMD): **63 ns** per email
- JSON → model (single-pass): **1.67M models/sec** (2.05x faster than `json.loads + init`)

**SIMD string ops:**

- HTML escape: **2.0-3.4x**
- URL encode: **4.0-12.3x**
- URL decode: **17.1x** (percent-encoded)
- Multipart boundary scan: **20.4 GB/s** on 100KB bodies

**Server:**

- Native Zig HTTP server: **2.1x** faster than uvicorn (13K vs 6K req/sec, 18-core box); **2.3x** at scale (479–548K vs 239K req/s, 64-core pin on 2× EPYC 7702, W=64, c=1024, TCP_NODELAY-corrected uvicorn)
- Radix trie routing: **808 ns/resolve** (dynamic routes)
- WebSocket: RFC 6455 with SIMD XOR unmasking

### Build for Production

The single most impactful tuning step is the build flag. Always use `--release` in production:

```bash
# Debug build (default) — assertions on, trace logging on every query, 20-30% slower
uv run hyper-build

# Release build — optimized, no assertions, no traces
uv run hyper-build --install --release
```

Release builds strip per-query trace logging, connection-acquire trace logging, and cache-operation trace logging — these alone can add 20-30% overhead to every request.

### Profile-Driven Optimization

Every performance improvement in HyperDjango follows the same workflow: a `cProfile` top-15 self-time analysis identifies the hotspot, the fix lands in ≤ ~40 lines of Python or Zig, and both `cProfile` (structural) AND `wrk` (wire-speed) verify the win under the **Stability Rule** (≥5s/run, multi-run median, jitter < 5%). If a proposed optimization isn't visible in the cProfile top-30, it's not worth doing.

The methodology is documented in `docs/profiling.md` ("Profile-Driven Optimization Workflow").

**Reusable benchmark + profile infrastructure**: 11 scripts in `scripts/` cover hypernews, bookstore, HyperAdmin, OpenAPI, task_queue, channels, and pool contention via both in-process cProfile (structural identification) and real wrk (wire-speed verification). All multi-run median + per-run jitter, all support `HYPER_POOL_SIZE` overrides, all output structured JSON + readable text in `logs/`.

```bash
# cProfile (structural / per-call self-time — trustworthy for small wins)
uv run python scripts/profile_hypernews_cprofile.py    # multi-endpoint hypernews
uv run python scripts/profile_list_cprofile.py         # bookstore List
uv run python scripts/profile_write_cprofile.py        # bookstore POST/write path
uv run python scripts/profile_admin_cprofile.py        # HyperAdmin changelist
uv run python scripts/profile_openapi_cprofile.py      # OpenAPI spec build + HTTP
uv run python scripts/profile_queue_channel_cprofile.py  # task_queue + channels

# wrk (wire-speed — noisier but catches regressions cProfile misses)
uv run python scripts/bench_hypernews_wrk.py           # hypernews wire-speed
uv run python scripts/bench_bookstore_wrk.py           # bookstore wire-speed
HYPER_POOL_SIZE=4 uv run python scripts/bench_pool_queue_depth.py  # pool contention histogram

# Microbench (isolated subsystem comparisons)
uv run python scripts/bench_db_query_dicts.py          # _db_query vs _db_query_dicts
```

Building a new wrk benchmark for your own app is ~30-60 lines: import `WrkBenchmark`, `WrkTarget`, `run_wrk_benchmark` from `scripts/_wrk_bench.py`, declare your targets, pass your setup args. See `scripts/bench_bookstore_wrk.py` (68 lines) for the minimal example.

### Production Checklist

Performance work is wasted if production isn't configured to use it. The defaults are good, but these are the levers worth knowing:

1. **Build with `--release`** — strips trace logging, enables optimization (-20-30% overhead removed)
2. **Set `auto_reload=False`** on `TemplateEngine` in production (skips per-request mtime checks)
3. **Use `cache_name=` on hot-path queries** — repeated queries skip the Parse phase (33% faster)
4. **Use `select_related()` / `prefetch_related()`** to avoid N+1 patterns (the `PerformanceMiddleware` will detect them in dev)
5. **Use `DatabaseCache`** (UNLOGGED PostgreSQL table) for multi-server cache coordination — no external cache service required
6. **Use `StaticFilesMiddleware` with `immutable_prefix=`** for hashed assets (Cache-Control: immutable)
7. **Set `pool_size`** appropriate to your workload — defaults to `THREAD_POOL_SIZE + 8`, override with `HYPER_POOL_SIZE` env var
8. **Wire `PerformanceMiddleware` + `PoolHealthChecker`** in production — `/debug/performance` dashboard, percentile tracking, drain stats
9. **Use `COPY` for bulk imports** — 42.8x faster than individual INSERTs (`db.copy_from(table, columns, rows)`)
10. **Profile with `X-Profile: 1` header** before optimizing — never optimize without a profile showing the hotspot
11. **Configure `RuleBasedRateLimitMiddleware`** with per-endpoint cost multipliers for expensive APIs
12. **Set `rate_limit_tier`** on RBAC groups for tiered rate limiting
13. **Export RBAC policy** (`checker.export_policy()`) as part of your backup strategy

---

## Installation

```bash
uv add hyperdjango

# Build the native extension (required, Zig 0.16+):
uv run hyper-build --release
```

The native Zig extension is required. All database, template, and HTTP operations use compiled Zig code. There are no Python fallbacks.

---

## Features

### Database: `ENGINE = 'hyperdjango.db'`

Drop-in replacement for Django's PostgreSQL backend. Uses pg.zig — a native Zig implementation of the PostgreSQL wire protocol.

- **Full Django ORM compatibility**: migrations (819/819 pass), admin, QuerySets, raw SQL
- **Prepared statement caching**: repeated queries skip the Parse phase (33% faster)
- **Connection pooling**: auto-tuned pool size (CPU cores \* 2), thread-owned fast path
- **30+ PostgreSQL types**: int, float, bool, text, JSON, UUID, bytea, arrays, HSTORE, custom enums
- **COPY protocol**: 42.8x faster bulk imports (536K rows/sec)
- **Connection pipelining**: batch N queries in a single round-trip
- **Native dict results**: `_db_query_dicts` — 1.85x faster than tuple + Python dict construction
- **Native JSON results**: `_db_query_json` — 2.66x faster than dicts + json.dumps
- **Batch execute**: `_db_exec_many` — Parse+Describe once, N executions with 256KB flush batches
- **COPY FROM/TO API**: `Database.copy_from(table, columns, rows)` and `Database.copy_to(sql)`

```python
# Works exactly like Django's PostgreSQL backend
from django.db import models


class Article(models.Model):
    title = models.CharField(max_length=200)
    content = models.TextField()
    status = models.CharField(max_length=20, default="draft")
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


# Django ORM works unchanged
articles = Article.objects.filter(status="published").order_by("-created_at")[:10]
```

#### HyperManager: Pipeline + Bulk Load

For performance-critical paths, add `HyperManager` to your models:

```python
from hyperdjango.serving.django_managers import HyperManager


class Article(models.Model):
    title = models.CharField(max_length=200)
    objects = HyperManager()  # extends Django's default Manager


# Pipeline: 3 queries in 1 round-trip (5.7x faster than sequential)
results = Article.objects.pipeline(
    [
        "SELECT * FROM articles WHERE id = 1",
        "SELECT * FROM articles WHERE id = 2",
        "SELECT COUNT(*) FROM articles",
    ]
)

# Bulk load by PKs (single pipeline)
articles = Article.objects.bulk_load([1, 2, 3, 4, 5])
```

### Templates: `BACKEND = 'hyperdjango.serving.template_backend.ZigTemplates'`

Jinja2-compatible template engine compiled to native Zig. Works with Django's `render()`, `TemplateResponse`, and template tags.

- **1.7x faster** than Jinja2 for cached renders
- **38x faster** for uncached (compile + render)
- 49 native filters + 7 humanize filters, 28 `is` tests
- Math expressions: `{{ price * qty }}`, `{{ (a + b) * 2 }}`
- String concat: `{{ "Hello" ~ name }}`
- Ternary: `{{ "active" if user.is_active else "inactive" }}`
- List/dict/tuple literals: `{% for x in [1, 2, 3] %}`
- For-loop tuple unpacking: `{% for k, v in items.items() %}`
- `{% with %}` scoped blocks, macros with params/defaults

```python
# Works with Django's standard render()
from django.shortcuts import render


def article_list(request):
    articles = Article.objects.all()
    return render(request, "articles/list.html", {"articles": articles})
```

### Middleware: Drop-in Django Middleware Classes

Replace third-party packages with zero-dependency built-in middleware:

| HyperDjango                  | Replaces                       | Config                                                                  |
| ---------------------------- | ------------------------------ | ----------------------------------------------------------------------- |
| `HyperCORSMiddleware`        | django-cors-headers            | `HYPERDJANGO_CORS_ORIGINS = ['https://example.com']`                    |
| `HyperSecurityMiddleware`    | Django SecurityMiddleware      | `HYPERDJANGO_FRAME_OPTIONS = 'DENY'`                                    |
| `HyperTimingMiddleware`      | (none)                         | Adds `X-Response-Time` header                                           |
| `HyperRateLimitMiddleware`   | django-ratelimit               | `HYPERDJANGO_RATE_LIMIT = 100` (per minute, PostgreSQL-backed)          |
| `HyperPerformanceMiddleware` | django-debug-toolbar (partial) | `X-Query-Count`, `X-N-Plus-One` headers, `/debug/performance` dashboard |

### Auth: OAuth2 via Django's Auth System

OAuth2 authorization code flow that creates real Django `User` objects:

```python
# settings.py
AUTHENTICATION_BACKENDS = [
    "hyperdjango.serving.auth_backends.OAuth2Backend",
    "django.contrib.auth.backends.ModelBackend",
]

HYPERDJANGO_OAUTH2_PROVIDERS = {
    "google": {"client_id": "...", "client_secret": "..."},
    "github": {"client_id": "...", "client_secret": "..."},
}
```

After OAuth2 login, `request.user` is a standard Django User. `@login_required`, Django admin login, and all Django auth features work normally.

### Validation: Faster Django Forms

```python
from hyperdjango.validation.forms import HyperForm
from django import forms


class SignupForm(HyperForm):  # drop-in for forms.Form
    name = forms.CharField(max_length=100)
    email = forms.EmailField()
    age = forms.IntegerField(min_value=13)


# 4.3x faster validation, same Django rendering/widgets
```

### Admin: Performance Overlay + Auto-Prefetch

Replace Django's default admin with `HyperAdminSite` for built-in performance monitoring:

```python
# admin.py
from django.contrib import admin
from hyperdjango.serving.admin import HyperAdminSite, HyperModelAdmin

admin_site = HyperAdminSite(name="admin")


class ArticleAdmin(HyperModelAdmin):
    list_display = ["title", "author", "category", "created_at"]
    # FK fields in list_display are auto-prefetched — no N+1 queries!


admin_site.register(Article, ArticleAdmin)

# urls.py
urlpatterns = [path("admin/", admin_site.urls)]
```

`HyperModelAdmin` automatically calls `select_related()` for ForeignKey fields and `prefetch_related()` for ManyToMany fields that appear in `list_display`. The `HyperPerformanceMiddleware` adds `X-Query-Count` and `X-N-Plus-One` headers to every response.

### N+1 Detection: Auto-Prefetch Middleware

Add `HyperAutoPrefetchMiddleware` to automatically detect N+1 query patterns:

```python
# settings.py
MIDDLEWARE = ["hyperdjango.serving.django_middleware.HyperAutoPrefetchMiddleware", ...]
HYPERDJANGO_N_PLUS_ONE_THRESHOLD = 5  # trigger after 5 repeated queries
```

The middleware:

1. Analyzes all SQL queries per request (requires `DEBUG = True`)
2. Detects repeated `SELECT ... WHERE fk_id = ?` patterns
3. Adds `X-N-Plus-One-Fix` headers with suggested `.select_related()` calls
4. Remembers patterns per view path for future requests

### DataLoader: N+1 Prevention for Custom Code

For views that make multiple DB calls, use `DataLoader` to batch them:

```python
from hyperdjango.dataloader import DataLoader


async def batch_users(user_ids):
    results = await db.pipeline(
        [f"SELECT * FROM users WHERE id = {uid}" for uid in user_ids]
    )
    return [r[0] if r else None for r in results]


loader = DataLoader(batch_fn=batch_users)

# These 3 calls are batched into 1 pipelined query:
user1 = await loader.load(1)
user2 = await loader.load(2)
user3 = await loader.load(3)
```

### Performance Monitoring

`HyperPerformanceMiddleware` tracks every query and provides a dashboard:

```python
# settings.py
MIDDLEWARE = ["hyperdjango.serving.django_middleware.HyperPerformanceMiddleware", ...]
# Dashboard at /debug/performance (DEBUG mode only)
# JSON API at /debug/performance/json
```

Every response includes headers:

- `X-Query-Count: 5` — number of DB queries
- `X-Query-Time: 12.3ms` — total DB time
- `X-N-Plus-One: 2` — number of N+1 patterns detected

### Management Commands

```bash
python manage.py runziserver    # Zig HTTP server + Django middleware (2x faster)
python manage.py dbpool         # Connection pool status (--json, --watch)
python manage.py poolui         # Live web dashboard for pool monitoring
python manage.py hypercheck     # Verify native feature availability
```

### Diagnostics: `hyper doctor`

Runs its checks across the build, python, database, perf, config, filesystem, and security categories in under 250ms:

```bash
uv run hyper doctor              # Color terminal output with pass/warn/fail
uv run hyper doctor --json       # JSON output for programmatic consumption
uv run hyper doctor --ci         # CI-friendly output (non-zero exit on failure)
uv run hyper doctor --no-db      # Skip database checks (no connection needed)
uv run hyper doctor --category build   # Run only build-related checks
uv run hyper doctor --verbose    # Show details for all checks, not just failures
```

Extensible via the `@doctor_check` decorator or entry points for third-party plugins.

### Locale-Aware Formatting: `hyperdjango.formats`

Date/time, number, and currency formatting wired to 15 i18n settings. Django-compatible format characters.

```python
from hyperdjango.formats import (
    format_date,
    format_number,
    format_currency,
    now,
    localtime,
)
from datetime import date

format_date(date.today(), "F jS, Y")  # "March 29th, 2026"
format_number(1234567.89)  # "1,234,567.89"
format_currency(1234.5)  # "$1,234.50"

dt = now()  # UTC-aware datetime
local = localtime(dt, "US/Eastern")  # convert timezone
```

Template filters: `{{ value|date:"m/d/Y" }}`, `{{ value|currency:"$" }}`, `{{ value|number:"2" }}`. Parse input with `parse_date()` and `parse_datetime()`. See [docs/formats.md](docs/formats.md).

### Internationalization: `hyperdjango.i18n`

Complete translation framework: PO file loading, 40+ language plural rules, lazy strings, `LocaleMiddleware`, template `{% trans %}`, URL i18n prefixing, and message extraction.

```python
from hyperdjango.i18n import gettext as _, ngettext, activate, override

activate("fr")
print(_("Hello"))  # "Bonjour"

with override("de"):
    print(_("Hello"))  # "Hallo"

msg = ngettext("%(count)d item", "%(count)d items", 3) % {"count": 3}
```

PO file workflow: `extract_messages()` scans source code, `create_po_file()` generates `.po` files, `load_translations()` loads them at startup. See [docs/i18n.md](docs/i18n.md).

### Custom Fields: `hyperdjango.fields`

14 built-in domain-specific field types that integrate with Model, Serializer, Form, and Admin. Create your own with the `CustomField` base class.

```python
from hyperdjango import Model, Field
from hyperdjango.fields import create_field, MoneyField, EmailField, SlugField
from decimal import Decimal


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    price: Decimal = create_field(MoneyField(currency="USD"))
    email: str = create_field(EmailField())
    slug: str = create_field(SlugField(max_length=100))
```

Built-in: MoneyField, ColorField, EmailField, URLField, SlugField, PhoneField, IPAddressField, CIDRField, UUIDField, JSONField, ChoiceField, EncryptedField, DurationField, PercentField. See [docs/fields.md](docs/fields.md).

### Background Tasks: `hyperdjango.tasks`

In-process task queue with priority levels, result tracking, retry with exponential backoff, timer-based scheduling, dead letter queue, task groups, and lifecycle hooks. Pure Python free-threading, no external dependencies. **Not persistent** — see [tasks docs](docs/tasks.md) for limitations and the `pyjobby` roadmap for durable job scheduling.

```python
from hyperdjango.tasks import task, TaskPriority, TaskScheduler


@task(
    priority=TaskPriority.HIGH,
    max_retries=3,
    retry_delay=2.0,
    retry_backoff=2.0,
    retry_on=(ConnectionError,),
)
async def process_payment(order_id, amount):
    return await payment_api.charge(order_id, amount)


handle = process_payment.delay(42, 99.99)
handle.status()  # TaskStatus.PENDING / RUNNING / SUCCESS / FAILED
handle.result()  # blocks until done

# Cron scheduling
scheduler = TaskScheduler()
scheduler.add(cleanup_task, cron="0 3 * * *")  # 3 AM daily
scheduler.start()
```

See [docs/tasks.md](docs/tasks.md).

---

## Platform Features

Beyond Django acceleration, HyperDjango is a complete application platform. Every feature below works in both modes (Django integration and standalone HyperApp).

### ORM — Django-Compatible QuerySet with Compiled SQL Cache

Full QuerySet API with a compiled SQL cache (Zig FNV-1a hash) delivering 520K queries/sec — 2x+ over uncached:

```python
from hyperdjango.expressions import F, Q, Count, SearchVector, SearchQuery, SearchRank

# Full-text search with ranking
vector = SearchVector(["title", "body"], config="english")
query = SearchQuery(q, search_type="websearch")
results = (
    await Note.objects.annotate(rank=SearchRank(vector, query))
    .order_by("-rank")
    .limit(20)
    .all()
)

# Complex filters with Q objects
hot = (
    await Post.objects.filter(
        Q(score__gte=100) | Q(comment_count__gte=50),
        status=PostStatus.PUBLISHED,
    )
    .order_by("-hot_score")
    .all()
)

# Atomic counter increment — no race conditions
await Post.objects.filter(id=post_id).update(score=F("score") + 1)

# UPDATE...RETURNING in one roundtrip
updated = await Post.objects.filter(id=post_id).update(
    score=F("score") + 1, returning=["id", "score"]
)  # → [{"id": 42, "score": 13}]
```

21 lookups (including 4 pgvector distance operators), 12 transforms, `select_related`, `prefetch_related`, `annotate`, `aggregate`, `bulk_update`, `get_or_create`, `update_or_create`, `explain()`.
→ [Models docs](docs/models.md) · [Queries guide](docs/queries-guide.md) · [Expressions](docs/expressions.md) · [pgvector](docs/postgres-ext.md)

---

### REST API — Full Framework in ~30 Lines

Define a serializer, a viewset, register with a router — you get CRUD, pagination, filtering, permissions, OpenAPI, and ETag caching:

```python
from hyperdjango.rest import (
    ModelSerializer,
    ModelViewSet,
    APIRouter,
    CursorPagination,
    FieldFilter,
    SearchFilter,
    OrderingFilter,
    IsAuthenticatedOrReadOnly,
)


class BookSerializer(ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "author_id", "published", "created_at"]


class BookViewSet(ModelViewSet):
    serializer_class = BookSerializer
    model = Book
    permission_classes = (IsAuthenticatedOrReadOnly,)
    pagination_class = CursorPagination  # HMAC-signed keyset pagination
    filter_backends = (FieldFilter, SearchFilter, OrderingFilter)
    filterset_fields = ("published", "author_id")
    search_fields = ("title", "description")  # Full-text with prefix ops


router = APIRouter(prefix="/api/v1")
router.register("books", BookViewSet)
router.mount(app.router, namespace="api")

# GET /api/v1/books/?search=django&published=true&ordering=-created_at
# Auto-generates OpenAPI 3.1 at /openapi.json + Swagger UI at /docs
```

4 pagination styles (PageNumber, LimitOffset, keyset Cursor, real DECLARE CURSOR), composable permissions (`IsAdmin & HasModelPerm | IsOwner`), bulk operations, nested routers, ETag/304 caching, throttling.
→ [REST docs](docs/rest.md) · [Serializers](docs/serializers.md) · [OpenAPI](docs/openapi.md) · [Pagination](docs/pagination.md)

---

### WebSocket — Native Zig RFC 6455 with Rooms

Real-time communication with native frame processing (SIMD XOR unmask), rooms, presence, and pub/sub:

```python
from hyperdjango.realtime import Room
from hyperdjango.guard.websocket import guard_websocket, Require

rooms: dict[str, Room] = {}


@app.websocket("/ws/chat/{room_id}")
@guard_websocket(auth, Require.authenticated())
async def chat_ws(ws, room_id):
    user = ws.user
    room = rooms.setdefault(room_id, Room(room_id))
    await room.join(str(user.id), user.username, ws=ws)

    try:
        async for data in ws.iter_json():
            msg = ChatMessage(room_id=room_id, user_id=user.id, content=data["content"])
            await msg.save()
            await room.broadcast(
                {"type": "message", "user": user.username, "content": data["content"]}
            )
    finally:
        await room.leave(str(user.id))
```

Also: `PgChannelLayer` (PostgreSQL LISTEN/NOTIFY for multi-process pub/sub), `LiveQuery` (subscribe to model changes), `NotificationManager`, `WebSocketRateLimiter`.
→ [Channels docs](docs/channels.md) · [Real-time patterns](docs/realtime.md)

---

### Auth — Sessions, OAuth2, RBAC, API Keys

HMAC-signed session cookies with key rotation, OAuth2 providers, hierarchical RBAC:

```python
from hyperdjango.auth import SessionAuth, hash_password, verify_password, require_auth
from hyperdjango.signing import TokenEngine, SigningKey

engine = TokenEngine(keys=[SigningKey(secret="rotate-me-quarterly", version=1)])
auth = SessionAuth(secret="session-secret", token_engine=engine)
app.use(auth)


@app.post("/login")
async def login(request):
    data = await request.json()
    user = await User.objects.filter(username=data["username"]).first()
    if not user or not verify_password(data["password"], user.password_hash):
        return Response.json({"error": "Invalid credentials"}, status=401)
    resp = Response.json({"user": user.username})
    auth.login(resp, {"id": user.id, "username": user.username})
    return resp


@app.get("/me")
@require_auth()
async def me(request):
    return Response.json({"id": request.user.id, "username": request.user.username})
```

OAuth2 (Google/GitHub/Auth0) is one call: `app.oauth2([google(client_id="...", client_secret="...")])`. Hierarchical RBAC adds CTE role inheritance, object permissions, 5 rule types, and field-level access.
→ [Auth docs](docs/auth.md) · [Sessions](docs/sessions.md) · [Security guide](docs/security-guide.md) · [Signing](docs/signing.md)

---

### Background Tasks — Priorities, Retry, Scheduling

In-process task queue with priority levels, exponential backoff retry, cron scheduling, and dead letter queue:

```python
from hyperdjango.tasks import TaskPriority


@app.task(
    priority=TaskPriority.HIGH,
    max_retries=3,
    retry_delay=2.0,
    retry_backoff=2.0,
    retry_on=(ConnectionError,),
)
async def send_email(to: str, subject: str, body: str):
    await smtp.send(to=to, subject=subject, body=body)
    return f"Sent to {to}"


# Enqueue — returns immediately
handle = send_email.delay(to="user@example.com", subject="Welcome", body="...")
handle.status()  # PENDING → RUNNING → SUCCESS
handle.result()  # Blocks until done, or raises on failure

# Cron scheduling
scheduler = TaskScheduler()
scheduler.add(cleanup_expired_sessions, cron="0 3 * * *")  # 3 AM daily
```

→ [Tasks docs](docs/tasks.md)

---

### Multi-Tenancy — Automatic Query Isolation

`TenantMixin` auto-scopes every query to the current tenant. No manual `WHERE tenant_id = ?` needed:

```python
from hyperdjango.tenancy import TenantMixin, TenantMiddleware

app.use(TenantMiddleware(resolve_tenant=resolve_from_header))


class Project(TenantMixin, TimestampMixin, Model):
    class Meta:
        table = "mt_projects"

    id: int = Field(primary_key=True, auto=True)
    tenant_id: int = Field(foreign_key=Org)
    name: str = Field()


# All queries automatically filtered by request.tenant:
projects = await Project.objects.all()  # → WHERE tenant_id = <current>
await Project.objects.unscoped().all()  # Escape hatch for admin
```

→ [Tenancy docs](docs/tenancy.md)

---

### File Uploads — Three Modes, One API

The framework selects the right upload mode automatically. Your handler code doesn't change:

```python
@app.post("/upload")
async def upload(request):
    files = await request.files()  # Works for all three modes
    photo = files["photo"]
    photo.data  # bytes (reads from disk if spilled)
    photo.size  # int
    photo.in_memory  # True if < 2.5 MB, False if spilled to disk
    photo.path  # Temp file path (disk mode) or None

    async for chunk in photo.chunks():  # Stream from any mode
        await process(chunk)


@app.post("/proxy-to-s3")
async def proxy(request):
    # Pass-through streaming for multi-GB uploads — bounded memory, zero disk
    async for chunk in request.stream():
        await s3_client.upload_part(chunk)
```

Memory (< 2.5 MB), disk spill (> 2.5 MB with temp files), pass-through streaming (> 10 MB directly from TCP socket). Settings: `FILE_UPLOAD_MAX_MEMORY_SIZE`, `MAX_BODY_SIZE`, `STREAM_BODY_CHUNK_SIZE`.
→ [Uploads docs](docs/uploads.md)

---

### Telemetry — Metrics, Tracing, Log Correlation

Zig-backed metrics (zero cost when disabled), W3C traceparent propagation, auto log correlation:

```python
from hyperdjango.telemetry import (
    Tracer,
    RatioSample,
    TelemetryMiddleware,
    PrometheusSink,
)

tracer = Tracer(name="myapp", sampler=RatioSample(0.05))  # Sample 5% of requests
app.use(TelemetryMiddleware(tracer=tracer, sinks=[PrometheusSink()]))

# Every request automatically gets:
# - A span with method, path, status code, duration
# - Counter/histogram metrics emitted to Prometheus at /metrics
# - trace_id/span_id injected into log records
# - Inbound W3C traceparent headers honored
```

8 subsystems auto-emit metrics: rate limiter, CSRF, sessions, guard, DataLoader, templates, admin, pg.zig pool.
→ [Telemetry docs](docs/telemetry.md) · [Metrics](docs/metrics.md)

---

### Admin Panel — Auto-CRUD from Models

Register your models, get a full admin UI with list/search/filter/create/edit/delete:

```python
from hyperdjango.admin import HyperAdmin

admin = HyperAdmin(app, prefix="/admin", title="My Admin")

admin.register(
    User,
    list_display=["id", "username", "email", "created_at"],
    search_fields=["username", "email"],
)
admin.register(
    Post,
    list_display=["id", "title", "author_id", "score", "status"],
    search_fields=["title"],
    ordering="-created_at",
)
admin.register(Comment, list_display=["id", "post_id", "author_id", "score"])

# → Full CRUD UI at /admin/ with:
#   - Searchable/sortable list views with pagination
#   - Add/edit forms with FK autocomplete
#   - Bulk actions (delete, custom)
#   - RBAC permission management (effective perms viewer, permission checker)
```

→ [Admin docs](docs/admin.md) · [Admin guide](docs/admin-guide.md)

---

### Forms — Validation, Cross-Field Clean, Error Display

Django-style forms with 12 field types, custom validators, and cross-field validation:

```python
from hyperdjango.forms import Form, CharField, EmailField, PasswordField, ChoiceField

class RegisterForm(Form):
    username = CharField(min_length=3, max_length=50)
    email = EmailField()
    password = PasswordField(min_length=8)
    password_confirm = PasswordField()

    def clean(self):
        if self.cleaned_data.get("password") != self.cleaned_data.get("password_confirm"):
            self.add_error("password_confirm", "Passwords don't match")

@app.post("/register")
async def register(request):
    form = RegisterForm(await request.form())
    if not form.is_valid():
        return Response.html(render("register.html", {"form": form}), status=400)
    user = User(username=form.cleaned_data["username"], ...)
    await user.save()
    return redirect("/login")
```

Also: `ModelForm` (auto-generates fields from Model), `DateField`, `IntegerField`, `DecimalField`, `BooleanField`, `FileField`, `ChoiceField` with Enum support.
→ [Forms docs](docs/forms.md) · [Forms guide](docs/forms-guide.md)

---

### Production Infrastructure

```bash
uv run hyper doctor                              # environment diagnosis in <250ms
uv run hyper doctor --json                       # JSON for CI pipelines
uv run hyper benchmark                           # EXPLAIN ANALYZE regression tests
uv run hyper benchmark --save-baseline           # Save for CI comparison
uv run hyper systemd install --app app:app       # Generate hardened systemd unit
```

- **Rate limiting** — InMemory + PostgreSQL UNLOGGED, tiered limits, per-path/method/cost rules
- **Caching** — LocMemCache (LRU) + DatabaseCache (PostgreSQL UNLOGGED), `@cached` decorator, ConsistentHashRing (native Zig, 3x uhashring), StampedeProtection (XFetch), TwoTierCache (L1+L2)
- **Logging** — loguru-compatible API, JSON/console/file sinks, rotation, retention, compression, ANSI color markup
- **Migrations** — introspect schema, diff, generate SQL, migrate, rollback, verify, snapshot
- **Asset versioning** — content-hash filenames, `X-App-Version` + `X-App-Version-Action` headers, `X-Client-Version` cohort broadcast for load-balancer routing, operator-owned stale-client policy, blue/green routing
- **StatusTimeline** — temporal status tracking replacing boolean flags (`is_banned` → time-bounded events with actor, history, auto-escalation)
- **Usage metering** — multi-dimensional tracking, quotas, alert hooks
- **150 configurable settings** — 4-tier resolution (Django → env → .env → defaults), validated at startup

→ [Deployment guide](docs/deployment-guide.md) · [Tuning](docs/tuning.md) · [Settings](docs/settings.md) · [Versioning](docs/versioning.md) · [Performance](docs/performance.md)

---

## Detailed Examples

Both modes (Django acceleration + standalone HyperApp) share the same native Zig engine. See the top of this README for the two setup patterns.

### Unified ID System — Anti-Enumeration Public Identifiers

Sequential integer PKs leak information (total counts, creation order) and enable IDOR/BOLA attacks. HyperDjango's ID system maps internal PKs to opaque, model-specific external identifiers with four modes of operation.

**Standalone HyperApp models:**

```python
from hyperdjango import Model, Field
from hyperdjango.public_id import IDMixin, IDMode, KeySlot, generate_alphabet

# Step 1: Generate a unique alphabet per model (one-time, copy into code)
# print(generate_alphabet("olc32"))  # "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"


class Post(IDMixin, Model):
    class Meta:
        table = "posts"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = [KeySlot("key-2025-q1", offset=50_000, epoch=1704240000)]

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
```

**Four ID modes:**

| Mode      | Behavior                                        | Use case                                       |
| --------- | ----------------------------------------------- | ---------------------------------------------- |
| `raw`     | Integer PK exposed directly                     | Internal/admin APIs only                       |
| `encoded` | Bijection encoding (reversible, no secret)      | Obfuscation without security requirement       |
| `signed`  | HMAC-signed with key rotation and offset        | Production APIs — tamper-proof, non-sequential |
| `random`  | Cryptographic random string stored in DB column | Maximum opacity, no PK relationship            |

**Key rotation** — roll HMAC keys without breaking existing IDs:

```python
class IDConfig:
    mode = IDMode.SIGNED
    alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
    hmac_keys = [
        KeySlot("key-2025-q2", offset=100_000, epoch=1704240000),  # current
        KeySlot(
            "key-2025-q1", offset=50_000, epoch=1704240000
        ),  # previous (still decodes)
    ]
```

**Time-windowed IDs** — IDs that expire or activate at specific times:

```python
manager = Post.id_manager()
external_id = manager.encode(
    pk=42,
    valid_after=datetime(2025, 1, 1, tzinfo=UTC),
    valid_until=datetime(2025, 12, 31, tzinfo=UTC),
)
# Produces 3-part ID: {encoded_pk}.{time_window}.{hmac}
# Decoding after valid_until raises ExpiredIDError
```

**Per-user signing** — same PK produces different external IDs per user, preventing URL sharing:

```python
external_id = manager.encode(pk=42, user_id=request.user.id)
# User A gets "Xf7RgW3p.9c.hK2m", User B gets "Xf7RgW3p.9c.qP4n"
# Decoding with wrong user_id fails HMAC verification
```

The REST framework auto-encodes PKs in responses and auto-decodes in URL parameters when a model has an `IDConfig`. The ID-system test suite covers the full surface including native Zig base-encoding parity.

### Standalone HyperApp Examples

For new projects that don't need Django's admin/ORM/migrations, HyperApp provides a lightweight async framework with the same native Zig performance:

```python
from hyperdjango import HyperApp, Response

app = HyperApp(title="My API", database="postgres://localhost/mydb")


# ── Routes ────────────────────────────────────────────
@app.get("/users/{id:int}")
async def get_user(request, id):
    user = await app.db.query_one("SELECT * FROM users WHERE id = $1", id)
    return user  # auto-serialized to JSON


@app.post("/users")
async def create_user(request):
    data = await request.json()
    await app.db.execute(
        "INSERT INTO users (name, email) VALUES ($1, $2)", data["name"], data["email"]
    )
    return Response.json(data, status=201)


@app.get("/search")
async def search(request):
    q = request.query("q", "")
    page = int(request.query("page", "1"))
    return {"query": q, "page": page}


# ── Middleware ────────────────────────────────────────
from hyperdjango.standalone_middleware import CORSMiddleware, RateLimitMiddleware

app.use(CORSMiddleware(origins=["https://myapp.com"]))
app.use(RateLimitMiddleware(limit=100, window=60))

# ── Auth ──────────────────────────────────────────────
from hyperdjango.auth import SessionAuth, require_auth

sa = SessionAuth(secret="your-secret-key")
app.use(sa)


@app.post("/login")
async def login(request):
    data = await request.json()
    # validate credentials...
    resp = Response.json({"ok": True})
    sa.login(resp, {"user_id": 1, "role": "admin"}, request)
    return resp


@app.get("/protected")
@require_auth()
async def protected(request):
    return {"user": request.user}


# ── OAuth2 ────────────────────────────────────────────
from hyperdjango.auth.oauth2 import google, github

app.oauth2(
    [
        google(client_id="...", client_secret="..."),
        github(client_id="...", client_secret="..."),
    ],
    secret="your-secret",
)
# Login: GET /auth/google/login → redirects to Google
# Callback: GET /auth/google/callback → creates session


# ── Templates ─────────────────────────────────────────
@app.get("/page")
async def page(request):
    return app.render("index.html", {"title": "Hello", "items": [1, 2, 3]})


# ── Pipeline (batch queries) ──────────────────────────
results = await app.db.pipeline(
    [
        "SELECT * FROM users WHERE id = 1",
        "SELECT * FROM users WHERE id = 2",
        "SELECT COUNT(*) FROM orders",
    ]
)


# ── WebSocket ─────────────────────────────────────────
@app.websocket("/ws/chat")
async def chat(ws):
    async for message in ws:
        await ws.send(f"Echo: {message}")


# ── Background Tasks ─────────────────────────────────
@app.task
async def send_email(to, subject): ...


send_email.delay("user@example.com", "Welcome!")

# ── Run ───────────────────────────────────────────────
app.run(host="0.0.0.0", port=8000)
```

#### Testing Standalone Apps

```python
from hyperdjango.testing import TestClient

client = TestClient(app)


def test_get_user():
    resp = client.get("/users/1")
    assert resp.ok
    assert resp.json()["id"] == 1


def test_login():
    resp = client.post("/login", json={"username": "admin", "password": "secret"})
    assert resp.ok
    # Session cookie auto-persisted
    resp = client.get("/protected")
    assert resp.ok


def test_oauth2():
    client.login_oauth2("google", {"email": "test@gmail.com", "name": "Test"})
    resp = client.get("/protected")
    assert resp.ok
```

**Choose Mode 1** if you want Django's ecosystem (admin, ORM, migrations, 20+ years of packages). **Choose Mode 2** if you want maximum performance with minimal overhead for a new API or microservice.

---

## HyperAdmin — Production-Ready Admin Platform

HyperAdmin auto-generates a full CRUD admin from your Model definitions. No Django dependency. HTMX-powered dynamic UI, auth/RBAC, audit logging, inline editing — a complete admin platform.

### Quick Start

```python
from hyperdjango import HyperApp, Model, Field
from hyperdjango.admin import HyperAdmin, Fieldset, InlineConfig, Action

app = HyperApp(title="My App", database="postgres://localhost/mydb")
admin = HyperAdmin(app, prefix="/admin", secret_key="your-secret-key")


# Define models
class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    price: float = Field(ge=0.0, default=0.0)
    category: str = Field(max_length=50, default="general")
    is_active: bool = Field(default=True)


class Review(Model):
    class Meta:
        table = "reviews"

    id: int = Field(primary_key=True, auto=True)
    product_id: int = Field(foreign_key="products")
    text: str = Field(max_length=1000)
    rating: int = Field(ge=1, le=5)


# Register with full configuration
admin.register(
    Product,
    list_display=["name", "price", "category", "is_active"],
    search_fields=["name"],
    list_filter=["category", "is_active"],
    fieldsets=[
        Fieldset(title="Product Info", fields=["name", "category"]),
        Fieldset(title="Pricing", fields=["price"]),
        Fieldset(title="Status", fields=["is_active"], classes=["collapse"]),
    ],
    inlines=[InlineConfig(model_class=Review, fields=["text", "rating"], extra=1)],
    ordering="-id",
    per_page=25,
)

# Register auth models so admin can manage its own users
admin.register_auth_models()

if __name__ == "__main__":
    app.run()
```

This generates: dashboard, list views (paginated, searchable, sortable, filterable), add/edit forms (with fieldsets), inline editing, delete with confirmation dialog, login/logout, user management, audit trail — all with HTMX dynamic interactions.

### Features

**HTMX Dynamic UI (zero jQuery):**

- Live search with 300ms debounce
- Partial page swaps for sort, filter, pagination (no full reload)
- Delete confirmation via `<dialog>` modal
- Field-level validation on blur
- Toast notifications (auto-dismiss)
- HTMX "Add row" for inline formsets (server-rendered, no client-side cloning)

**Admin Configuration:**

```python
admin.register(
    MyModel,
    # List view
    list_display=["field1", "field2", "computed_col"],
    list_display_callables={"computed_col": lambda row: f"${row['price']:.2f}"},
    search_fields=["name", "description"],
    list_filter=["status", "category"],
    list_editable=["status"],
    ordering="-created_at",
    per_page=25,
    # Form view
    fieldsets=[
        Fieldset(title="Basic", fields=["name", "description"]),
        Fieldset(title="Details", fields=["price", "status"], classes=["collapse"]),
    ],
    readonly_fields=["created_at"],
    exclude_fields=["internal_field"],
    formfield_overrides={str: {"widget": "textarea", "attrs": {"rows": "3"}}},
    # Related objects
    inlines=[InlineConfig(model_class=Comment, fields=["text"], extra=2)],
    # Lifecycle hooks
    save_hooks=[my_before_save],  # async callable(values, is_edit) → values
    delete_hooks=[my_before_delete],  # async callable(pk)
    # Bulk actions
    actions=[Action(name="publish", label="Publish selected", handler=publish_handler)],
    # Template overrides
    list_template="products/list.html",  # 3-level cascade: model → admin → default
    media_css=["/static/products.css"],
    media_js=["/static/charts.js"],
)
```

**Auth / RBAC:**

```python
from hyperdjango.auth import PermissionChecker, User, Group, Permission
from hyperdjango.auth.sessions import build_session_data
from hyperdjango.auth.seed_utils import seed_password

# Admin login handled at /admin/login/ — users need "staff" group membership

# Create users + groups programmatically
checker = PermissionChecker(db)
await checker.ensure_tables()
admin = await checker.ensure_admin_user()  # resolves ADMIN_PASSWORD via settings

# Groups (RBAC groups replace boolean flags)
editors = await checker.ensure_group("editors")
await checker.add_user_to_group(admin.id, editors.id)

# Permissions
await checker.create_default_permissions("product", "Product")
await checker.grant_group_perm(editors.id, "change_product", "product")

# Login populates session with RBAC groups + permissions
session = await build_session_data(user.id, db, username=user.username)
auth.login(response, session)
# session["groups"] = ["staff", "editors"], session["permissions"] = ["change_product"]

# Route guards — typed SessionUser with frozenset groups/permissions
from hyperdjango.guard import guard, Require


@app.get("/api/products")
@guard(Require.role("editors"))  # user.in_group("editors") — O(1)
async def list_products(request): ...


@app.post("/admin/settings")
@guard(Require.permission("change_product"))  # user.has_perm("change_product") — O(1)
async def update_settings(request): ...


# Dynamic seed passwords (never hardcoded)
password = seed_password(
    "demo"
)  # HYPER_SEED_PASSWORD_DEMO → HYPER_SEED_PASSWORD → random
```

Password hashing uses **argon2id** (PHC winner, memory-hard) via `argon2-cffi`. No legacy hashers. SessionUser materializes `groups: frozenset[str]` and `permissions: frozenset[str]` at construction for O(1) membership checks.

**Audit Log:**

Every admin create/update/delete is automatically logged with JSON diffs:

```python
# Automatic — no setup needed. Just works when admin saves/deletes.
# View history at /admin/{model}/{id}/history/

# Programmatic access
from hyperdjango.auth.audit import AuditLog

audit = AuditLog(db)
history = await audit.get_object_history("product", "42")
recent = await audit.get_recent(limit=50)
activity = await audit.get_user_activity(user_id=1)
```

**Self-Managing Admin:**

```python
admin.register_auth_models()  # One call registers User, Group, Permission in admin
# Admin can now manage its own users, set passwords, assign groups/permissions
# Password field renders as type="password", hashed via argon2id on save
```

**Database Sessions (PostgreSQL UNLOGGED — no WAL overhead):**

```python
from hyperdjango.auth.db_sessions import DatabaseSessionStore
from hyperdjango.auth.sessions import SessionAuth

# PostgreSQL UNLOGGED table — fast writes, multi-server coordination
store = DatabaseSessionStore(db, max_age=86400)
await store.ensure_table()  # creates hyper_sessions UNLOGGED table

# Wire into SessionAuth middleware
app.use(SessionAuth(secret="your-secret", store=store))

# Sessions persist across restarts, coordinate across multiple app servers
# On password change, invalidate all sessions:
await store.invalidate_for_user(user_id)
```

### Cache Framework

Two backends: in-memory (development) or PostgreSQL UNLOGGED (production, multi-server):

```python
from hyperdjango.cache import LocMemCache, DatabaseCache, cached

# In-memory LRU cache (single server)
cache = LocMemCache(max_size=10000)

# PostgreSQL UNLOGGED table (multi-server, survives app restart)
cache = DatabaseCache(db, default_ttl=300)
await cache.ensure_table()  # creates hyper_cache UNLOGGED table

# Key-value API
await cache.set("user:42", {"name": "Alice"}, ttl=300)
user = await cache.get("user:42")
await cache.delete("user:42")

# Batch operations
await cache.set_many({"k1": "v1", "k2": "v2"})
results = await cache.get_many(["k1", "k2"])

# Atomic increment (thread-safe via PostgreSQL)
count = await cache.incr("page_views", 1)


# @cached decorator (works with sync + async functions)
@cached(ttl=60)
async def get_expensive_data(user_id):
    return await db.query_one("SELECT * FROM users WHERE id = $1", user_id)
```

### Rate Limiting — Pluggable, Multi-Tenant, Multi-Server

```python
from hyperdjango.ratelimit import (
    RateLimitMiddleware,
    DatabaseRateLimitBackend,
    ip_key,
    user_key,
    org_key,
    composite_key,
)

# ── Simple: IP-based, in-memory (single server) ──────────────
app.use(RateLimitMiddleware(max_requests=100, window=60))

# ── Multi-server: PostgreSQL UNLOGGED backend ────────────────
backend = DatabaseRateLimitBackend(db)
await backend.ensure_table()

app.use(
    RateLimitMiddleware(
        max_requests=100,
        window=60,
        backend=backend,
    )
)

# ── Per-user rate limiting ────────────────────────────────────
app.use(
    RateLimitMiddleware(
        max_requests=100,
        window=60,
        key_func=user_key,  # rate limit per authenticated user
    )
)

# ── Per-organization (multi-tenant) ──────────────────────────
# If user has org_id/organization_id/tenant_id, the ENTIRE org
# shares one rate limit pool. Org hits limit → all org users blocked.
app.use(
    RateLimitMiddleware(
        max_requests=5000,
        window=3600,
        key_func=org_key,
        backend=backend,
    )
)

# ── Hierarchical: stack multiple rate limiters ───────────────
# Layer 1: 10/sec per IP (DDoS protection)
app.use(RateLimitMiddleware(max_requests=10, window=1))
# Layer 2: 100/min per user (abuse prevention)
app.use(RateLimitMiddleware(max_requests=100, window=60, key_func=user_key))
# Layer 3: 5K/hr per organization (billing tier enforcement)
app.use(
    RateLimitMiddleware(
        max_requests=5000, window=3600, key_func=org_key, backend=backend
    )
)


# ── Custom key function ──────────────────────────────────────
# Rate limit by any attribute of the request/user
def plan_key(request):
    plan = request.user.get("plan", "free") if request.user else "anon"
    return f"plan:{plan}"


app.use(RateLimitMiddleware(max_requests=1000, window=60, key_func=plan_key))
```

All rate-limited responses include standard headers:

- `X-RateLimit-Limit` — max requests allowed
- `X-RateLimit-Remaining` — requests remaining in window
- `X-RateLimit-Reset` — seconds until window resets
- `Retry-After` — seconds until rate limit lifts (on 429)

### Model Inheritance

```python
from hyperdjango import Model, Field


# ── Abstract models: share fields, no table ──────────────────
class TimestampMixin(Model):
    class Meta:
        abstract = True

    created_at: str = Field(default="")
    updated_at: str = Field(default="")


class BaseEntity(Model):
    class Meta:
        abstract = True

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)


# Concrete subclass inherits ALL abstract parent fields
class Product(BaseEntity, TimestampMixin):
    class Meta:
        table = "products"

    price: float = Field(ge=0, default=0.0)
    # → table has: id, name, created_at, updated_at, price


# ── Proxy models: same table, different Python class ────────
class PremiumProduct(Product):
    class Meta:
        proxy = True

    def is_premium(self):
        return self.price > 100


# PremiumProduct queries the SAME "products" table
# but adds custom methods / behavior
premiums = await PremiumProduct.objects.filter(price__gt=100).all()
```

### Migration Framework (HyperUltimateMigrationSystem)

Schema migration engine that diffs your Model definitions against the live database:

```python
# CLI commands
hyper makemigrations                  # Diff models vs live DB → generate SQL
hyper makemigrations --dry-run        # Show SQL without writing file
hyper migrate                         # Apply pending migrations
hyper showmigrations                  # List applied/pending
hyper rollback                        # Rollback most recent migration
hyper db verify                       # Check for schema drift
hyper db snapshot                     # Save schema checkpoint

# Every operation has forward + reverse SQL
# Deployment safety analysis warns about:
#   - NOT NULL without DEFAULT (table rewrite)
#   - CREATE INDEX without CONCURRENTLY (table lock)
#   - DROP TABLE (data loss)
#   - ALTER COLUMN TYPE on large tables
```

### Testing

```python
from hyperdjango.testing import TestCase, TestClient


# ── TestCase with DB savepoint rollback ──────────────────
class TestProducts(TestCase):
    db_url = "postgres://localhost/mydb_test"

    async def test_create_product(self):
        await self.db.execute("INSERT INTO products (name) VALUES ($1)", "Widget")
        rows = await self.db.query("SELECT * FROM products")
        self.assertEqual(len(rows), 1)
        # DB is rolled back after each test — no cleanup needed!

    async def test_table_is_empty(self):
        # Previous test's INSERT was rolled back via savepoint
        rows = await self.db.query("SELECT * FROM products")
        self.assertEqual(len(rows), 0)


# Run all tests in class
TestProducts.run_all()

# ── Assertion helpers ────────────────────────────────────
# assertEqual, assertNotEqual, assertTrue, assertFalse
# assertIn, assertNotIn, assertIsNone, assertIsNotNone
# assertGreater, assertLess, assertIsInstance, assertRaises
# assertStatus, assertOk, assertContains, assertRedirects, assertJsonEqual
```

### ORM Lookups & Transforms

17 built-in lookups and 12 transforms with custom registration support:

```python
# String matching
users = await User.objects.filter(name__icontains="ali").all()
users = await User.objects.filter(email__endswith="@example.com").all()

# Comparison
users = await User.objects.filter(age__gte=18, age__lte=65).all()
users = await User.objects.filter(age__range=(18, 65)).all()

# Membership, null checks, regex
users = await User.objects.filter(id__in=[1, 2, 3]).all()
users = await User.objects.filter(bio__isnull=False).all()
users = await User.objects.filter(name__regex=r"^[A-Z]").all()

# Date/time transforms
events = await Event.objects.filter(created__year=2024).all()
events = await Event.objects.filter(created__year__gte=2020).all()

# Text transforms (chainable)
users = await User.objects.filter(name__lower__contains="ali").all()
users = await User.objects.filter(name__trim__length__gte=3).all()

# Exclude (negate any lookup)
users = await User.objects.exclude(name__startswith="X").all()

# Custom lookups
from hyperdjango.lookups import Lookup, register_lookup


class NotEqualLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} != ${param_idx}", [value]


register_lookup("ne", NotEqualLookup())
users = await User.objects.filter(status__ne="deleted").all()
```

### Pagination

```python
from hyperdjango.paginator import Paginator

paginator = Paginator(User.objects.order_by("id"), per_page=25)
page = await paginator.page(1)

page.items  # list of model instances
page.has_next  # True if there's a next page
page.num_pages  # total pages
page.start_index  # 1-based index of first item
page.page_range  # range(1, num_pages + 1)

# Async iteration over all pages
async for page in paginator:
    for user in page:
        process(user)
```

### REST API Framework — Full DRF-Equivalent

```python
from hyperdjango.rest import (
    ModelSerializer,
    ModelViewSet,
    APIRouter,
    PageNumberPagination,
    CursorPagination,
    ServerCursorPagination,
    FieldFilter,
    SearchFilter,
    OrderingFilter,
    IsAuthenticated,
    AnonRateThrottle,
    action,
)


class PostSerializer(ModelSerializer):
    class Meta:
        model = Post
        fields = "__all__"
        read_only_fields = ["id"]


class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    model = Post
    permission_classes = (IsAuthenticated,)
    pagination_class = PageNumberPagination
    filter_backends = (FieldFilter, SearchFilter, OrderingFilter)
    filterset_fields = ("status",)
    search_fields = ("title", "content")
    ordering_fields = ("created_at", "title")
    throttle_classes = (AnonRateThrottle,)

    @action(methods=["POST"], detail=True, url_path="publish")
    async def publish(self, request, **kwargs):
        post = await self.get_object()
        post.status = "published"
        await post.save()
        return Response.json(self.get_serializer(obj=post).data)


router = APIRouter(prefix="/api/v1")
router.register("posts", PostViewSet)
router.mount(app.router)
```

Two cursor pagination systems:

- **CursorPagination** — stateless HMAC-signed keyset (`WHERE id < $1`), works across clusters
- **ServerCursorPagination** — real PostgreSQL `DECLARE CURSOR / FETCH` with pinned pool connections, user-bound, per-user limits, idle/lifetime timeouts, read-replica routing

Full feature set: ModelSerializer (auto-field generation, FK auto-detect), 13 typed fields, `perform_create/update/destroy` hooks, permission composition (`&`, `|`, `~`), RBAC integration, MeteringMixin, per-view throttling + authentication, content negotiation, `OPTIONS` metadata, `serializer.save(**kwargs)`, `many=True` deserialization, nested validation, dotted source traversal, SearchFilter with prefix operators + smart_split + ReDoS protection.

Feature summary:

- **4 pagination styles**: PageNumber, LimitOffset, keyset CursorPagination (HMAC-signed), real `DECLARE CURSOR` ServerCursorPagination
- **3+1 filter backends**: FieldFilter, SearchFilter (prefix operators, full-text search), OrderingFilter, plus RBAC-aware ObjectPermission filtering
- **Bulk operations**: bulk create, bulk update, bulk delete in single requests
- **Nested routers**: `router.register("posts", PostViewSet)` then `posts_router.register("comments", CommentViewSet)` for `/posts/{id}/comments/`
- **Renderers**: JSON, Browsable HTML, CSV export, Binary
- **ETag caching**: conditional GET with `If-None-Match` / `If-Modified-Since`
- **OpenAPI 3.1**: auto-generated schema at `/api/schema/`, Swagger UI at `/api/docs/`
- **`@api_view` decorator**: function-based views with the same features as ViewSets

A dedicated test suite covers the REST framework surface (`uv run hyper-test rest`).

### Settings System — 118 Settings Across 19 Categories

All HyperDjango settings live in a single validated registry with defaults, type checking, range validation, and environment variable loading.

```python
# Django mode: HYPERDJANGO_* prefix in settings.py
HYPERDJANGO_POOL_SIZE = 0  # auto-tune
HYPERDJANGO_PREPARED_STATEMENTS = True
HYPERDJANGO_CONNECT_TIMEOUT = 10000  # ms

# Standalone mode: HYPER_* environment variables
# HYPER_SECRET_KEY=mysecret HYPER_DEBUG=true uv run hyper run

# Or via .env file in project root:
# SECRET_KEY=mysecret
# DEBUG=true
# DATABASE_URL=postgres://localhost/myapp
```

Settings are validated at startup via `validate_settings()`. Invalid types, out-of-range values, and missing required settings (like `SECRET_KEY` in production) produce clear error messages.

**Categories**: Database, Security, CSRF, Cache, Auth, Email, Static Files, Media, Upload, Server, Logging, Proxy, URL, Messages, Internationalization, Features, Other.

See [docs/settings.md](docs/settings.md) for the full reference. The settings test suite covers validation and environment loading.

### RBAC -- Hierarchical Role-Based Access Control

The auth system includes a full RBAC engine with role inheritance, object-level permissions, conditional rules, and field-level access control.

**Role inheritance via recursive CTE** -- permissions cascade through group hierarchies:

```python
from hyperdjango.auth.permissions import RBACChecker

checker = RBACChecker(db)

# Create a role hierarchy: viewer -> editor -> admin
viewer = await checker.create_group("viewer")
editor = await checker.create_group("editor", parent_id=viewer.id)
admin = await checker.create_group("admin", parent_id=editor.id)

# Grant "view_post" to viewer -- editor and admin inherit it automatically
await checker.grant_group_perm(viewer.id, "view_post", "post")

# Check uses recursive CTE to walk the hierarchy
assert await checker.has_perm(editor_user, "view_post", "post")  # True (inherited)
```

**Object-level permissions** -- grant access to specific rows:

```python
await checker.grant_object_perm(
    user_id=5, codename="change_post", model_name="post", object_id="42"
)
if await checker.has_object_perm(user, "change_post", "post", "42"):
    ...
```

**5 conditional rule types** attached to any permission:

| Rule type     | Evaluates                           | Example                         |
| ------------- | ----------------------------------- | ------------------------------- |
| `is_owner`    | `row[owner_field] == user.id`       | Only edit your own posts        |
| `time_window` | Current time within start/end range | Edit only during business hours |
| `ip_range`    | Client IP within CIDR range         | Admin access from office only   |
| `field_match` | `row[field] == expected_value`      | Only publish "draft" posts      |
| `custom`      | Arbitrary async callable            | Any business logic              |

```python
await checker.add_rule(
    perm_id, "is_owner", {"owner_field": "user_id"}, group_id=editor_id
)
await checker.add_rule(perm_id, "time_window", {"start": "09:00", "end": "17:00"})
```

**Field-level access** -- control visibility per field per role (hidden/readonly/writable):

```python
await checker.set_field_access(
    "post", "internal_notes", group_id=editor_id, access="readonly"
)
await checker.set_field_access(
    "post", "internal_notes", group_id=viewer_id, access="hidden"
)
```

**Policy export/import** for audit and environment synchronization:

```python
policy = await checker.export_policy()  # JSON-serializable dict
await checker.import_policy(policy, clear_existing=True)  # restore from backup
```

Guards use the typed `SessionUser` interface: `user.in_group("admin")` and `user.has_perm("view_post")` are O(1) frozenset lookups. `build_session_data()` materializes groups and permissions at login; field-level access is cached in the session via `get_all_field_access()`. REST ViewSets with `field_permissions_model` auto-filter hidden/readonly fields per user.

Dedicated suites cover the RBAC hierarchy, policy export/import, the RBAC dashboard, and field permissions.

### Usage Metering

Multi-dimensional usage tracking for SaaS billing, API quotas, and resource accounting.

```python
from hyperdjango.metering import MeterEngine, DimensionSpec, set_meter_engine

engine = MeterEngine(db)
await engine.ensure_tables()
set_meter_engine(engine)

# Define a meter with multiple dimensions
await engine.define_meter(
    "llm_usage",
    [
        DimensionSpec("requests", "counter", "requests", "sum"),
        DimensionSpec("tokens_in", "counter", "tokens", "sum"),
        DimensionSpec("tokens_out", "counter", "tokens", "sum"),
        DimensionSpec("duration_ms", "gauge", "ms", "avg"),
    ],
)

# Record usage (idempotent, incrementally aggregated into time buckets)
await engine.record(
    "llm_usage",
    "acme_corp",
    {
        "requests": 1,
        "tokens_in": 1_000_000,
        "tokens_out": 2_000_000,
        "duration_ms": 4500,
    },
)

# Query aggregated usage
report = await engine.query_multi(
    "llm_usage",
    "acme_corp",
    ["requests", "tokens_in"],
    period="monthly",
    start=start,
    end=end,
)
```

**Quotas with enforcement** -- reject or throttle when limits are reached:

```python
await engine.set_quota(
    "llm_usage", "acme_corp", "requests", limit=10_000, action="reject"
)
# Subsequent record() calls check quota; QuotaExceeded raised on breach
```

The REST framework includes `MeteringMixin` for automatic per-request usage tracking. The metering suite covers hierarchy rollup and LLM usage scenarios.

### Contrib Modules

Self-contained modules that add common web application features. Each is independent with its own test suite.

**Redirects** -- URL redirect management with O(1) in-memory lookup:

```python
from hyperdjango.redirects import RedirectRegistry

registry = RedirectRegistry()
registry.add("/old-page", "/new-page", permanent=True)
registry.add("/blog/2023/", "/blog/", permanent=False)
# Prefix matching: /old-section/anything -> /new-section/anything
# Open-redirect protection built in (rejects external targets by default)
```

**Flatpages** -- simple CMS pages stored in the database:

```python
from hyperdjango.flatpages import FlatPageRegistry

pages = FlatPageRegistry(db)
await pages.add(
    "/about/", title="About Us", content="<h1>About</h1>...", auth_required=False
)
# Template rendering with full Zig template engine support
```

**Sitemaps** -- XML sitemap generation with automatic 50K-URL pagination:

```python
from hyperdjango.sitemaps import Sitemap, GenericSitemap


class PostSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    async def items(self):
        return await Post.objects.all()

    def location(self, obj):
        return f"/posts/{obj.id}/"


# ETag caching on sitemap responses
```

**Syndication** -- RSS and Atom feed generation:

```python
from hyperdjango.syndication import Feed


class PostFeed(Feed):
    title = "Latest Posts"
    link = "/posts/"

    async def items(self):
        return await Post.objects.order_by("-created_at").limit(20).all()

    def item_title(self, item):
        return item.title


# Supports enclosures for podcast feeds
```

**Humanize** -- human-readable formatting as template filters and Python functions:

```python
from hyperdjango.humanize import ordinal, intcomma, naturaltime, filesizeformat

ordinal(3)  # "3rd"
intcomma(1000000)  # "1,000,000"
naturaltime(delta)  # "2 hours ago"
filesizeformat(2048)  # "2.0 KB"
# All available as template filters: {{ value|intcomma }}, {{ date|naturaltime }}
```

**PostgreSQL extensions** -- native PostgreSQL types and full-text search:

```python
from hyperdjango.postgres import (
    ArrayField,
    SearchVector,
    SearchQuery,
    SearchRank,
    TrigramSimilarity,
)


class Article(Model):
    class Meta:
        table = "articles"

    tags: list[str] = ArrayField(base_field=Field(max_length=50))
    search_vector: str = Field(default="")  # tsvector column


# Full-text search with ranking
results = (
    await Article.objects.filter(
        search_vector__search=SearchQuery("django performance")
    )
    .annotate(rank=SearchRank("search_vector", SearchQuery("django performance")))
    .order_by("-rank")
    .all()
)

# Trigram similarity for fuzzy matching
results = await Article.objects.filter(title__trigram_similar="djang").all()
```

Also includes: aggregate functions (ArrayAgg, StringAgg, JSONBAgg), range fields (IntegerRange, DateRange), and GiST/GIN index operations.

**Fixtures** -- database seeding with FK dependency sorting:

```python
# CLI
uv run hyper dumpdata posts --indent 2 > posts.json
uv run hyper loaddata posts.json   # UPSERT with natural key support
```

**Custom Commands** -- extend the `hyper` CLI with your own commands:

```python
from hyperdjango.commands import command


@command("seed", help="Seed the database with sample data")
async def seed_command(count: int = 100):
    """Create sample records for development."""
    for i in range(count):
        await Post(title=f"Post {i}").save()
    print(f"Created {count} posts")


# uv run hyper seed --count 500
```

### Multi-Database Routing

```python
from hyperdjango.multi_db import ConnectionManager, PrimaryReplicaRouter

connections = ConnectionManager()
await connections.configure(
    {
        "default": "postgres://primary/myapp",
        "replica": "postgres://replica/myapp",
    }
)
connections.router = PrimaryReplicaRouter()

# Reads go to replica, writes to primary — automatic
users = await User.objects.all()  # reads from replica
await User.objects.create(name="Alice")  # writes to primary

# Explicit selection
users = await User.objects.using("replica").all()


# Per-model binding
class AnalyticsEvent(Model):
    class Meta:
        table = "events"
        database = "analytics"
```

---

## Vision: The Future of HyperDjango

HyperDjango's goal is to bring compiled-language performance to Django's 20+ years of ecosystem without replacing any of it. The approach:

1. **Accelerate Django's extension points** — database, templates, middleware, auth backends. Django developers change settings, not code.

2. **Replace third-party dependencies** — django-cors-headers, django-ratelimit, django-debug-toolbar functionality built in with zero external deps.

3. **Add capabilities Django lacks** — connection pipelining, DataLoader for N+1 prevention, SIMD JSON parsing, native WebSocket — all accessible through Django's standard patterns.

### Roadmap

**What exists today:**

**Django acceleration mode:**

- Native PostgreSQL backend (2-5x faster, full ORM compatibility, 819/819 migrations)
- Native dict query results (1.85x faster), native JSON query results (2.66x faster)
- Batch execute (`_db_exec_many`), COPY FROM/TO API
- Native Zig template engine (1.5x faster, Jinja2-compatible, {% break %}/{% continue %})
- Django middleware wrappers (CORS, security, rate-limit, timing, performance)
- OAuth2 auth backend (Google, GitHub, Auth0) with PKCE (RFC 7636)
- Connection pipelining + DataLoader
- Performance monitoring with N+1 detection
- Native HTTP server (`manage.py runziserver`)
- Admin performance overlay (auto-prefetch, query stats, ARIA accessibility)
- Composite primary key support (ORM, migrations, inspectdb, admin)
- QuerySet .only()/.defer() for column-specific SELECT
- Configurable thread pool (`HYPER_THREAD_POOL_SIZE`), response cache RwLock
- Build mode detection (`is_release_build`)

**Standalone HyperApp platform:**

- Full ORM: 17 lookups, 12 transforms, select_related/prefetch_related, annotate/aggregate, model inheritance, ManyToManyField, composite primary keys
- Multi-database routing with read replicas, per-model binding, QuerySet.using() (writes route to primary)
- HyperAdmin: HTMX CRUD, auth/RBAC with per-model permissions, inlines, audit log (old-vs-new diff), FK autocomplete, CSRF protection, system checks, split into admin/ package (fields.py, templates.py, utils.py submodules)
- Generic class-based views: ListView, DetailView, CreateView, UpdateView, DeleteView
- Forms + FormSet + ModelForm with 12 field types
- URL namespaces, includes, nested routing
- Paginator with orphans, async iteration
- Cache (LRU + PostgreSQL UNLOGGED), Rate limiting (multi-tenant, memory-bounded), Sessions (UNLOGGED)
- Migration framework: introspect, diff, migrate (advisory-locked), rollback, verify, snapshot, squash, quoted identifiers
- Auth: argon2id (timing-safe), permissions, groups, password reset (full HMAC), validators, API keys (SHA-256 hashed), session auth hash invalidation on password change (HMAC-SHA256, lazy+eager invalidation)
- Email, signals, storage, flash messages
- SIMD pattern validator in Zig (@Vector(16,u8) character class matching)
- REST API Framework: ModelSerializer, ModelViewSet, 4 pagination styles, 3+1 filter backends, bulk ops, nested routers, CSV/JSON/Browsable renderers, ETag caching, OpenAPI 3.1, @api_view
- Unified ID system: 4 modes (raw/encoded/signed/random), HMAC key rotation, time-windowed IDs, per-user signing
- RBAC: hierarchical role inheritance (CTE), object-level permissions, 5 conditional rule types, field-level access in REST serializers, policy export/import, typed SessionUser with frozenset groups/permissions, Require.role()/permission()/field_access() guards
- Dynamic seed credentials: `seed_password()` + `ensure_admin_user()` — zero hardcoded passwords, settings-driven resolution
- Usage metering: multi-dimensional tracking, quotas with enforcement, incremental aggregation
- Settings: validated settings across every subsystem, HYPER\_\* env vars, .env support
- Contrib: redirects, flatpages, sitemaps, syndication, humanize, PostgreSQL extensions, fixtures, custom commands

**Production performance & security (75+ security fixes, 6 audit passes):**

- Transparent query cache with version-based invalidation, FK dependency tracking, signal-driven auto-invalidation
- Model mixins: TimestampMixin, SoftDeleteMixin (auto-filtered), OwnershipMixin, VersionedMixin (row-locked append-only)
- Security: CRLF header injection prevention, CSRF double-submit with verification, path traversal prevention, XSS auto-escaping
- Connection pool: SlowQueryLog (UNLOGGED), QueryTimer, PoolHealthChecker, graceful drain, PostgreSQL BackendKeyData storage for cancel support
- Distributed cache: ConsistentHashRing (native Zig 3x), StampedeProtection (XFetch), TwoTierCache (user-aware), CacheMiddleware
- Logging: loguru-compatible API, JSON/Console/File sinks, ANSI colorizer, native Zig helpers (8.3x timestamp)
- Static files: StaticFilesMiddleware (ETag, gzip, LRU cache, native Zig file+hash), ManifestStaticFilesStorage, collectstatic, {{ static() }}
- WebSocket pub/sub: Channel/ChannelGroup/InMemoryChannelLayer/PgChannelLayer (LISTEN/NOTIFY), presence, history, auth
- CLI: hyper shell/dbshell/inspectdb (views, topological sort, 60+ PG types)/collectstatic/migrate/createsuperuser
- `hyper doctor`: environment checks across every category, 3 output modes (terminal/JSON/CI), extensible via decorator + entry points, <250ms
- Performance tuning guide ([Performance section above](#performance), plus `docs/profiling.md` and `docs/performance-guide.md`)

The philosophy: Django has solved application development. HyperDjango solves application performance — without asking developers to learn a new framework.

---

## Configuration Reference

118 configurable settings across 19 categories. All settings use the `HYPERDJANGO_` prefix in Django's `settings.py`:

```python
# Database
HYPERDJANGO_POOL_SIZE = 0  # 0 = auto (CPU cores * 2)
HYPERDJANGO_PREPARED_STATEMENTS = True
HYPERDJANGO_CONNECT_TIMEOUT = 10000  # ms
HYPERDJANGO_QUERY_TIMEOUT = 0  # 0 = no timeout

# CORS
HYPERDJANGO_CORS_ORIGINS = ["*"]
HYPERDJANGO_CORS_CREDENTIALS = False
HYPERDJANGO_CORS_MAX_AGE = 86400

# Security
HYPERDJANGO_FRAME_OPTIONS = "DENY"
HYPERDJANGO_HSTS = False
HYPERDJANGO_CSP = None

# Rate Limiting
HYPERDJANGO_RATE_LIMIT = 100  # requests per window
HYPERDJANGO_RATE_LIMIT_WINDOW = 60  # seconds

# Performance Monitoring
HYPERDJANGO_SLOW_QUERY_MS = 100  # threshold for slow query detection
HYPERDJANGO_PERF_DASHBOARD_PATH = "/debug/performance"

# OAuth2
HYPERDJANGO_OAUTH2_CREATE_USER = True  # auto-create Django User on first OAuth2 login
HYPERDJANGO_OAUTH2_UPDATE_USER = True  # update name fields on each login
```

---

## Documentation

92 documentation files covering every subsystem. Key references:

- [docs/tutorial.md](docs/tutorial.md) -- Getting started guide
- [docs/models.md](docs/models.md) -- Model definitions, fields, inheritance
- [docs/queries-guide.md](docs/queries-guide.md) -- QuerySet API, lookups, transforms
- [docs/views.md](docs/views.md) -- Function and class-based views
- [docs/rest.md](docs/rest.md) -- REST API framework reference
- [docs/auth.md](docs/auth.md) -- Authentication, sessions, API keys, OAuth2
- [docs/public-ids.md](docs/public-ids.md) -- Public ID system and anti-enumeration
- [docs/architecture.md](docs/architecture.md) -- System architecture, module map, request lifecycle
- [docs/settings.md](docs/settings.md) -- the full settings reference with descriptions
- [docs/deployment.md](docs/deployment.md) -- Production deployment guide
- [docs/security-guide.md](docs/security-guide.md) -- Security hardening
- [docs/database.md](docs/database.md) -- pg.zig driver, pooling, COPY, pipelines
- [docs/templates.md](docs/templates.md) -- Zig template engine reference
- [docs/admin.md](docs/admin.md) -- HyperAdmin configuration
- [docs/metering.md](docs/metering.md) -- Usage metering and quotas
- [docs/postgres-ext.md](docs/postgres-ext.md) -- ArrayField, FTS, trigrams, ranges
- [docs/migrations.md](docs/migrations.md) -- Schema migration system
- [docs/uploads.md](docs/uploads.md) -- Three-mode upload system
- [docs/versioning.md](docs/versioning.md) -- Asset versioning, cache busting, HTMX mismatch
- [docs/telemetry.md](docs/telemetry.md) -- Metrics, tracing, W3C traceparent
- [docs/tuning.md](docs/tuning.md) -- All tuning parameters reference
- [docs/auth.md](docs/auth.md) -- Auth, RBAC, sessions, OAuth2
- [docs/rest.md](docs/rest.md) -- REST API framework
- [docs/channels.md](docs/channels.md) -- WebSocket pub/sub channels
- [docs/formats.md](docs/formats.md) -- Locale-aware date/time/number formatting
- [docs/i18n.md](docs/i18n.md) -- Translation framework (gettext, PO files, plural rules)
- [docs/fields.md](docs/fields.md) -- Custom fields (14 built-in domain types)
- [docs/tasks.md](docs/tasks.md) -- Background task queue (priority, retry, scheduling, DLQ)

---

## Testing

```bash
uv run hyper-test                    # Run all tests
uv run hyper-test rest admin         # Pattern matching (run subsets)
uv run hyper-test --list             # List all available test files
uv run hyper doctor                  # 30 diagnostic checks across 7 categories
uv run hyper doctor --json           # JSON output for CI pipelines
uv run hyper benchmark               # EXPLAIN ANALYZE perf regression tests
```

Current state: **15,970+ tests across 362 files, 0 failures**. Includes unit tests, integration tests, E2E tests for all 22 services, and Hypothesis fuzz suites. Run `uv run hyper-test` for the live count.

---

## Requirements

- Python 3.14+ (free-threaded recommended for maximum performance)
- PostgreSQL 15+ (required for Django 5.x+)
- Zig 0.16+ (for building the native extension)
- Django 5.0+ (for Django integration mode)

---
