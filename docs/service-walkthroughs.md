# Service Walkthroughs

Annotated tours of HyperDjango's services — not just what the code does, but **why** each design choice was made. Every pattern shown here is a production best practice.

All 22 services are self-contained in `services/` with `app.py`, `seed.py`, and E2E tests in `scripts/test_e2e_*.py`.

---

## Which Example for My Use Case?

| I want to build...                    | Start with                      | Key patterns                                                     |
| ------------------------------------- | ------------------------------- | ---------------------------------------------------------------- |
| A REST API                            | [bookstore_api](#bookstore-api) | ViewSets, serializers, cursor pagination, ETag caching           |
| A full-stack web app                  | [full_stack](#full-stack)       | Templates, forms, admin panel, auth                              |
| A real-time chat/notification system  | `websocket_chat`                | Zig WebSocket, rooms, presence, pub/sub channels                 |
| A multi-tenant SaaS                   | [multi_tenant](#multi-tenant)   | Tenant isolation, scoped queries, RBAC, StatusTimeline           |
| An API with usage metering and quotas | [metering_api](#metering-api)   | MeterEngine, quota enforcement, IETF rate limit headers          |
| A content platform with search        | `hypernews`                     | Full-text search, voting, eigenvector ranking, keyset pagination |
| An AI/LLM-powered app                 | `hyperai`                       | SSE streaming, tiered rate limits, signed API keys               |
| A ticket/support system               | `hyperticket`                   | Multi-tenant, guard access control, 27+ models, StatusTimeline   |
| Something minimal to learn from       | [hello](#hello-world)           | The simplest possible HyperDjango app                            |

---

## Hello World

**Directory:** `services/hello/` | **Lines:** ~35 | **Concepts:** App lifecycle, routing, health checks

```python
from hyperdjango import HyperApp, Response

app = HyperApp(title="Hello")


@app.get("/")
async def index(request):
    return {"message": "Hello from HyperDjango!"}


@app.get("/greet/{name}")
async def greet(request, name):
    return {"greeting": f"Hello, {name}!"}


app.mount_health()  # GET /health → {"status": "ok"}
```

**Key patterns:**

- **Return a dict from a handler** and HyperDjango auto-serializes to JSON with `Content-Type: application/json`. No need for `Response.json()` for simple cases.
- **`{name}` in the route** captures a path parameter. Type hints work too: `{id:int}` validates the parameter is an integer before your handler runs.
- **`app.mount_health()`** adds a `/health` endpoint for load balancer and monitoring integration. Always include this in production apps.
- **No database required.** The hello app demonstrates that HyperDjango works without PostgreSQL for simple services.

**Run it:**

```bash
cd services/hello
uv run python app.py
# http://localhost:8000/ → {"message": "Hello from HyperDjango!"}
```

---

## Bookstore API

**Directory:** `services/bookstore_api/` | **Concepts:** REST framework, serializers, pagination, filtering, auth, rate limiting, OpenAPI, ETag caching, admin panel

This is the flagship REST API example — a complete bookstore with authors, books, reviews, and categories.

### Model Design

```python
class Book(TimestampMixin, IDMixin, Model):
    class Meta:
        table = "books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=300)
    author_id: int = Field()
    isbn: str = Field(max_length=13)
    published: bool = Field(default=False)
    price: str = Field(max_length=20, default="0.00")
```

**Why `TimestampMixin`:** Every model inherits `created_at` and `updated_at` fields. These are auto-managed by `save()` — no manual datetime handling needed. This is a project-wide convention: all models must use TimestampMixin.

**Why `IDMixin`:** Adds HMAC-signed opaque public IDs. Instead of exposing `id=42` in URLs (vulnerable to IDOR/enumeration), the API returns opaque tokens like `bk_7xKm9pQ`. The signing key is server-side — IDs can't be forged or predicted.

### REST API with ViewSets

```python
class BookSerializer(ModelSerializer):
    class Meta:
        model = Book
        fields = [
            "id",
            "title",
            "author_id",
            "isbn",
            "published",
            "price",
            "created_at",
        ]


class BookViewSet(ModelViewSet):
    serializer_class = BookSerializer
    model = Book
    permission_classes = (IsAuthenticatedOrReadOnly,)
    pagination_class = CursorPagination
    filter_backends = (FieldFilter, SearchFilter, OrderingFilter)
    filterset_fields = ("published", "author_id")
    search_fields = ("title",)
```

**Why `CursorPagination`:** Keyset pagination uses HMAC-signed cursors instead of page numbers. This is O(1) regardless of dataset size (no `OFFSET`), tamper-proof (can't skip pages by guessing), and stable under concurrent writes. Always use `CursorPagination` for APIs — avoid `PageNumberPagination` and `LimitOffsetPagination` in production.

**Why `FieldFilter` + `SearchFilter` + `OrderingFilter`:** These compose together. `FieldFilter` handles exact-match filters (`?published=true`), `SearchFilter` handles full-text search (`?search=django`), `OrderingFilter` handles sort (`?ordering=-created_at`). Each is a separate concern — add or remove backends without changing the ViewSet.

### Auth + Rate Limiting + OpenAPI

```python
# Middleware stack (outermost first)
app.use(SecurityHeadersMiddleware())
app.use(TimingMiddleware())
app.use(CORSMiddleware(origins=["http://localhost:3000"]))
app.use(RateLimitMiddleware(max_requests=120, window=60))
app.use(auth)

# OpenAPI auto-generation
app.mount_docs()  # /openapi.json + /docs (Swagger UI)
```

**Why this middleware order:** Security headers are outermost (always set, even on errors). Timing wraps everything inside. CORS must run before auth (preflight requests have no credentials). Rate limiting runs before auth (prevents brute-force). Auth is innermost (only runs on non-rejected requests).

**Run it:**

```bash
cd services/bookstore_api
uv run hyper setup --app app:app --drop --seed seed:run
uv run python app.py
# http://localhost:8000/docs → Swagger UI with all endpoints
```

---

## Full Stack

**Directory:** `services/full_stack/` | **Concepts:** Templates, forms, admin panel, session auth, static files

A traditional server-rendered web app with projects and tasks — demonstrates the same patterns you'd use in Django.

### Template Rendering

```python
@app.get("/projects/{id:int}")
@guard(Require.authenticated())
async def project_detail(request, id):
    project = await Project.objects.get(id=id)
    tasks = await Task.objects.filter(project_id=id).order_by("-created_at").all()
    return engine.render(
        "project_detail.html",
        {
            "project": project,
            "tasks": tasks,
            "user": request.user,
        },
    )
```

**Why native Zig templates:** The template engine compiles Jinja2-compatible syntax to native Zig node trees. First render compiles in 7.1μs (234x faster than Jinja2). Subsequent renders use the LRU cache at 36μs (1.7x faster). Templates support all Jinja2 features: extends, includes, macros, filters, expressions, for-loops with scoping.

### Forms with Validation

```python
class ProjectForm(Form):
    name = CharField(max_length=100, required=True)
    description = TextField(required=False)


@app.post("/projects/new")
@guard(Require.authenticated())
async def create_project(request):
    form = ProjectForm(await request.form())
    if not form.is_valid():
        return engine.render("project_form.html", {"form": form, "errors": form.errors})
    project = Project(name=form.cleaned_data["name"], owner_id=request.user.id)
    await project.save()
    return Response.redirect(f"/projects/{project.id}")
```

**Why Form objects:** Form validation runs through the native Zig validation engine (4x+ faster than Python). Forms also provide error rendering, CSRF token handling, and `cleaned_data` for safe access to validated input. Always validate through Forms or BaseModel — never trust raw `request.form()` data.

---

## Multi-Tenant

**Directory:** `services/multi_tenant/` | **Concepts:** Tenant isolation, scoped queries, RBAC groups, StatusTimeline, org suspension

A multi-tenant SaaS showing how to isolate data between organizations.

### Tenant-Scoped Models

```python
class Project(TenantMixin, TimestampMixin, Model):
    class Meta:
        table = "mt_projects"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    description: str = Field(default="")
```

**Why `TenantMixin`:** Adds a `tenant_id` field and auto-filters all queries to the current tenant. When you call `Project.objects.all()`, the QuerySet automatically appends `WHERE tenant_id = $current_tenant`. No manual filtering needed — the isolation is enforced at the ORM level.

### Setting the Tenant Context

```python
@app.middleware
async def tenant_middleware(request, call_next):
    tenant = await resolve_tenant(request)
    if tenant is None:
        return Response.json({"error": "Unknown tenant"}, status=404)
    with tenant_context(tenant.id):
        return await call_next(request)
```

**Why middleware, not per-view:** Tenant context is set once in middleware and applies to ALL queries in the request. This prevents the common bug of forgetting to filter by tenant in one view — the ORM enforces it automatically.

### Org Suspension with StatusTimeline

```python
class Org(StatusTimelineMixin, TenantMixin, TimestampMixin, Model): ...


# Suspend an org (temporal event with start/end time, not a boolean flag)
await org.set_status("suspended", actor_id=admin.id, reason="Payment overdue")

# Check if suspended (queries the timeline, not a boolean column)
if await org.has_active_status("suspended"):
    return Response.json({"error": "Organization suspended"}, status=403)
```

**Why StatusTimeline instead of `is_suspended: bool`:** Boolean flags lose history. StatusTimeline records who did what and when, with start/end times. You can see the full suspension history, when it happened, who did it, and automatically expire temporary suspensions.

---

## Metering API

**Directory:** `services/metering_api/` | **Concepts:** Usage metering, quota enforcement, IETF rate limit headers, tiered accounts

An API that tracks per-account usage (API calls, storage, compute) and enforces quotas.

### Recording Usage Events

```python
meter = MeterEngine(db)


@app.post("/api/compute")
@guard(Require.authenticated())
async def run_compute(request):
    # Record usage event (idempotent — deduped by idempotency_key)
    await meter.record(
        meter_name="api_calls",
        account_id=request.user.id,
        dimensions={"endpoint": "/api/compute", "tier": request.user.tier},
        quantity=1,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    # ... do the work ...
```

**Why MeterEngine:** Metering is separate from rate limiting. Rate limiting says "stop after N requests per minute." Metering says "track total usage for billing." MeterEngine provides multi-dimensional aggregation (by endpoint, tier, time bucket), idempotent recording (safe retries), and quota hooks (enforce monthly limits).

### IETF Rate Limit Headers

```python
app.use(
    RateLimitMiddleware(
        max_requests=60,
        window=60,
        policy_name="api-minute",
    )
)
```

All rate-limited responses include IETF-standard headers ([draft-ietf-httpapi-ratelimit-headers-10](https://www.ietf.org/archive/id/draft-ietf-httpapi-ratelimit-headers-10.txt)):

```
RateLimit-Policy: "api-minute";q=60;w=60
RateLimit: "api-minute";r=45;t=30
```

On 429 responses, the body is RFC 9457 Problem Details JSON:

```json
{
  "type": "urn:ietf:params:ratelimit:problem-type:quota-exceeded",
  "title": "Rate limit exceeded",
  "status": 429,
  "detail": "Quota exceeded for policy api-minute",
  "retry_after": 30
}
```

**Why IETF headers:** Standard headers let any HTTP client (including HyperDjango's own `RateLimitState` client) automatically respect rate limits with jittered backoff. No custom header parsing needed.

---

## Running Any Example

All services follow the same pattern:

```bash
cd services/<app_name>

# Create tables and seed data (--drop recreates from scratch)
uv run hyper setup --app app:app --drop --seed seed:run

# Run the app
uv run python app.py

# Run the E2E tests
uv run hyper-test e2e_<app_name>
```

Every service is a working, tested application — not a toy snippet. Copy one as a starting point for your own project.

---

## Further Reading

- [Examples Catalog](services.md) — full list of all 19 apps with feature descriptions
- [Production Scaling Guide](production-scaling.md) — caching, replicas, and deployment at scale
- [Patterns Reference](patterns.md) — coding patterns used across all services
- [REST Framework](rest.md) — complete ViewSet, serializer, and pagination API
- [Auth & RBAC](auth.md) — sessions, API keys, role hierarchy, field permissions
