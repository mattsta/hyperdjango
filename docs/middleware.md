# Middleware

Middleware processes requests and responses globally. Each middleware wraps the next, forming a chain: request flows top-down, response flows bottom-up.

## Using Middleware

```python
from hyperdjango import HyperApp
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
    CompressionMiddleware,
    CSRFMiddleware,
)
from hyperdjango.logging import AccessLogMiddleware
from hyperdjango.ratelimit import RateLimitMiddleware

app = HyperApp("myapp")

# Add middleware (executed top-down on request, bottom-up on response)
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware(origins=["https://myapp.com"]))
app.use(TimingMiddleware())
app.use(CompressionMiddleware(min_size=1024))
app.use(AccessLogMiddleware())
```

## Built-in Middleware

### SecurityHeadersMiddleware

Adds security headers to every response:

```python
app.use(
    SecurityHeadersMiddleware(
        hsts=True,  # Strict-Transport-Security
        hsts_max_age=31536000,  # 1 year
        content_type_nosniff=True,  # X-Content-Type-Options: nosniff
        frame_options="DENY",  # X-Frame-Options
        referrer_policy="strict-origin",  # Referrer-Policy
        coop="same-origin",  # Cross-Origin-Opener-Policy
    )
)
```

### CORSMiddleware

Cross-Origin Resource Sharing:

```python
app.use(
    CORSMiddleware(
        origins=["https://myapp.com", "https://admin.myapp.com"],
        methods=["GET", "POST", "PUT", "DELETE"],
        headers=["Authorization", "Content-Type"],
        expose_headers=["X-Total-Count", "X-Page"],  # readable by browser JS
        allow_credentials=True,
        max_age=3600,
    )
)
```

`expose_headers` lists response headers the browser is allowed to expose to
client-side JavaScript; it emits `Access-Control-Expose-Headers` on actual
(non-preflight) responses.

### TimingMiddleware

Adds `X-Response-Time` header:

```python
app.use(TimingMiddleware())
# Response includes: X-Response-Time: 3.2ms
```

### CompressionMiddleware

Gzip compression for responses above a size threshold:

```python
app.use(CompressionMiddleware(min_size=1024))  # Compress responses > 1KB
```

### CSRFMiddleware

CSRF protection for POST/PUT/PATCH/DELETE:

```python
app.use(CSRFMiddleware(secret="your-csrf-secret"))
```

In templates:

```html
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  ...
</form>
```

### RateLimitMiddleware

Rate limiting per IP, user, or custom key:

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

See [Rate Limiting](ratelimit.md) for tiered and rule-based limiting.

### AccessLogMiddleware

Structured access logging:

```python
from hyperdjango.logging import AccessLogMiddleware

app.use(AccessLogMiddleware())
# Logs: 200 GET /api/users 3.2ms
```

### MetricsMiddleware

Prometheus-compatible metrics export. Tracks HTTP request counts, latency histograms, and in-flight requests. Serves a `/metrics` scrape endpoint:

```python
from hyperdjango.metrics import MetricsMiddleware

app.use(MetricsMiddleware())
# GET /metrics → Prometheus text format

# Custom path
app.use(MetricsMiddleware(metrics_path="/prom/metrics"))
```

Also collects database pool stats, prepared statement cache stats, and query performance (when `PerformanceMiddleware` is active). See [Metrics](metrics.md) for full reference.

## Writing Custom Middleware

### Function-Based

```python
async def timing_middleware(request, call_next):
    """Measure request processing time."""
    import time

    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time"] = f"{elapsed:.1f}ms"
    return response


app.use(timing_middleware)
```

### Class-Based

```python
class BearerAuthMiddleware:
    """Check authentication on every request."""

    def __init__(self, exclude_paths: list[str] | None = None):
        self.exclude_paths = exclude_paths or ["/health", "/login"]

    async def __call__(self, request, call_next):
        if request.path in self.exclude_paths:
            return await call_next(request)

        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer "):
            return Response.json({"error": "Unauthorized"}, status=401)

        # Validate token, set request.user
        request.user = await validate_token(token[7:])
        return await call_next(request)


app.use(BearerAuthMiddleware(exclude_paths=["/health", "/login", "/docs"]))
```

### Short-Circuit (Early Return)

Middleware can return a response without calling `call_next`:

```python
async def maintenance_middleware(request, call_next):
    if MAINTENANCE_MODE:
        return Response.json({"error": "Service under maintenance"}, status=503)
    return await call_next(request)
```

### Modify Request

```python
async def request_id_middleware(request, call_next):
    import uuid

    request.headers["X-Request-ID"] = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.headers["X-Request-ID"]
    return response
```

### Modify Response

```python
async def cache_control_middleware(request, call_next):
    response = await call_next(request)
    if request.method == "GET" and response.status == 200:
        response.headers["Cache-Control"] = "public, max-age=300"
    return response
```

## Middleware Ordering

Middleware executes in the order you add it. Request flows top-down, response flows bottom-up:

```python
app.use(SecurityHeadersMiddleware())  # 1st on request, last on response
app.use(CORSMiddleware(...))  # 2nd on request, 2nd-to-last on response
app.use(TimingMiddleware())  # 3rd on request, 3rd-to-last on response
app.use(BearerAuthMiddleware())  # 4th on request, 4th-to-last on response
```

**Recommended order:**

1. Security headers (outermost — always applied)
2. CORS (must run before auth to handle preflight)
3. Compression (wrap response after all modifications)
4. Version headers / cohort broadcast (must be **after** compression — see below)
5. Timing (measure after security, before auth)
6. Rate limiting (before expensive auth checks)
7. Authentication (before route handlers)
8. Access logging (innermost — log with full context)

`VersionMiddleware` injects `<script>` tags into HTML bodies, so it must see
uncompressed bytes. Because responses unwind innermost-first, the middleware
registered **last** touches the body **first** — so `CompressionMiddleware` has
to be registered before it. The wrong order raises at startup. See
[versioning.md](versioning.md).

## Exception Handling in Middleware

Exceptions propagate up through the middleware chain. Use try/except to handle them:

```python
async def error_handler_middleware(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logger.error(f"Unhandled error: {e}")
        return Response.json({"error": "Internal server error"}, status=500)


# Or use the built-in exception handler system:
@app.exception_handler(ValueError)
async def handle_value_error(request, exc):
    return Response.json({"error": str(exc)}, status=400)
```

## Session Middleware

Set up session-based authentication:

```python
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore

session_store = DatabaseSessionStore(app.db)
await session_store.create_table()

app.use(SessionAuth(session_store))
# Now request.user is populated for authenticated requests
```

## Authentication Middleware

The `SessionAuth` middleware:

1. Reads the `session_id` cookie from the request
2. Looks up the session in the database
3. Sets `request.user` to the authenticated user (or None)
4. Sets `request.session_id` to the session key

```python
@app.get("/profile")
async def profile(request):
    if not request.user:
        return redirect("/login/")
    return render(request, "profile.html", {"user": request.user})
```
