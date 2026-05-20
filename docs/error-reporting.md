# Error Reporting

Handle errors gracefully in production and get notified when things break. HyperDjango provides custom exception handlers, structured logging, error notifications, sensitive data filtering, health checks, and security event tracking.

## Debug Mode vs Production

### Development (Debug Mode)

In development, unhandled exceptions show a detailed error page with:

- Full stack trace with syntax-highlighted source code
- Request information (method, path, headers, body)
- Local variables at each frame
- SQL queries executed before the error

```python
app = HyperApp("myapp")
# debug mode is auto-detected from HYPER_DEBUG=1 environment variable
```

### Production Mode

In production (`app.run(prod=True)`), errors return a generic JSON response with no internal details:

```json
{ "detail": "Internal Server Error", "status": 500 }
```

This prevents leaking stack traces, file paths, database queries, and other sensitive information to end users.

```python
app.run(host="0.0.0.0", port=8000, prod=True)
```

## The unified error contract

Every error response the framework produces has the **same shape** — a JSON body
`{"detail": <message>, "status": <code>}` (plus an optional `"errors"` object for
field validation). Produce one with `Response.error(status, detail)` (to return)
or `raise HTTPException(status, detail)` (to signal from anywhere):

```python
from hyperdjango.exceptions import HTTPException
from hyperdjango.response import Response

# Raise it from any handler/middleware/serializer — it maps to {"detail","status"}:
raise HTTPException(404, "Order not found")

# Or return it directly where a Response is required:
return Response.error(403, "Forbidden")
```

### Automatic status mapping

You usually do **not** need a custom handler for common failures. A plain handler
that lets one of these escape is mapped to the correct 4xx automatically (not a
generic 500):

| Exception                  | Status    |
| -------------------------- | --------- |
| `PermissionError`          | 403       |
| `Model.DoesNotExist`       | 404       |
| `SuspiciousFileOperation`  | 400       |
| `InvalidPage` (pagination) | 404       |
| validation errors          | 400 / 422 |

Any exception can declare its own status via an `http_status` int attribute, or be
registered with `register_exception_status(ExcType, status)`. An unmapped exception
is a generic 500 and its message is **never** leaked to the client.

### Database errors

Every database failure — direct-SQL or ORM — raises one typed hierarchy
(`IntegrityError`, `ProgrammingError`, `DataError`, `OperationalError`,
`DatabaseError`, …) classified at the native boundary, so the same PostgreSQL
error is the same class on either path. Register a handler (or map a status) for
whichever class a route should surface distinctly — for example a
`409 Conflict` for a unique violation:

```python
from hyperdjango.db import IntegrityError, is_unique_violation


@app.exception_handler(IntegrityError)
async def on_integrity_error(request, exc):
    if is_unique_violation(exc):
        return Response.error(409, "Already exists")
    return Response.error(400, "Constraint violation")
```

See [Database → typed exception hierarchy](database.md#typed-exception-hierarchy-one-contract-for-both-paths)
for the full class→cause mapping.

## Custom Exception Handlers

Register handlers for specific exception types using the `@app.exception_handler`
decorator. Return the unified body via `Response.error(...)`:

```python
from hyperdjango.response import Response


@app.exception_handler(ValueError)
async def handle_value_error(request, exc):
    return Response.error(400, str(exc))


@app.exception_handler(KeyError)
async def handle_key_error(request, exc):
    return Response.error(400, f"Missing field: {exc}")
```

### MRO-Based Resolution

Exception handlers use Method Resolution Order (MRO) for matching. A handler for `Exception` catches anything not caught by a more specific handler:

```python
# Specific handlers take priority
@app.exception_handler(ValueError)
async def handle_value_error(request, exc):
    return Response.error(400, str(exc))


# Catch-all for unhandled exceptions
@app.exception_handler(Exception)
async def handle_all(request, exc):
    logger.error(f"Unhandled error: {type(exc).__name__}: {exc}")
    return Response.error(500, "Internal server error")
```

The resolution order:

1. Check for an exact match on the exception type
2. Walk the exception's MRO (parent classes)
3. If no handler matches, use the default behavior (debug page or generic 500)

### Custom Exception Classes

Define application-specific exceptions with their own handlers:

```python
class PaymentError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        super().__init__("Rate limit exceeded")
        self.retry_after = retry_after


@app.exception_handler(PaymentError)
async def handle_payment_error(request, exc):
    return Response.error(402, str(exc))


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit(request, exc):
    response = Response.error(429, "Too many requests")
    response.headers["Retry-After"] = str(exc.retry_after)
    return response
```

## Error Logging

### Structured Logging

Use the HyperDjango logger for structured error logging with full context:

```python
from hyperdjango.logging import logger


@app.exception_handler(Exception)
async def log_and_respond(request, exc):
    logger.error(
        "Unhandled {exc_type}: {exc} | {method} {path}",
        exc_type=type(exc).__name__,
        exc=exc,
        method=request.method,
        path=request.path,
    )
    return Response.error(500, "Internal server error")
```

### File Logging with Rotation

```python
# Log errors to a file with rotation
logger.add("errors.log", level="ERROR", rotation="100 MB", retention="30 days")

# JSON logging for log aggregators
logger.add("errors.json", format="json", level="ERROR")
```

### Logging Request Context

Include request details in error logs for debugging:

```python
@app.exception_handler(Exception)
async def detailed_error_handler(request, exc):
    logger.error(
        "Error processing {method} {path}\n"
        "  Client: {ip}\n"
        "  User-Agent: {ua}\n"
        "  Exception: {exc_type}: {exc}",
        method=request.method,
        path=request.path,
        ip=request.client_ip,
        ua=request.headers.get("User-Agent", "unknown"),
        exc_type=type(exc).__name__,
        exc=exc,
    )
    return Response.error(500, "Internal server error")
```

## Error Notifications

### Email on 500 Errors

Send email alerts to administrators when unhandled errors occur:

```python
from hyperdjango.mail import send_mail


@app.exception_handler(Exception)
async def notify_on_error(request, exc):
    # Send to admins
    await send_mail(
        subject=f"[500] {type(exc).__name__}: {request.method} {request.path}",
        message=(
            f"Error: {exc}\n\n"
            f"Path: {request.path}\n"
            f"Method: {request.method}\n"
            f"Client IP: {request.client_ip}\n"
            f"User-Agent: {request.headers.get('User-Agent', 'unknown')}\n"
        ),
        from_email="errors@myapp.com",
        to=["admin@myapp.com"],
    )
    return Response.error(500, "Internal server error")
```

### Rate-Limited Notifications

Avoid email storms during cascading failures:

```python
import time

_last_error_email = 0
_ERROR_EMAIL_COOLDOWN = 60  # seconds


@app.exception_handler(Exception)
async def throttled_notify(request, exc):
    global _last_error_email
    now = time.time()

    logger.error("Unhandled {exc_type}: {exc}", exc_type=type(exc).__name__, exc=exc)

    if now - _last_error_email > _ERROR_EMAIL_COOLDOWN:
        _last_error_email = now
        await send_mail(
            subject=f"[500] {type(exc).__name__}",
            message=f"Error: {exc}\nPath: {request.path}",
            from_email="errors@myapp.com",
            to=["admin@myapp.com"],
        )

    return Response.error(500, "Internal server error")
```

## Sensitive Data Filtering

Never log passwords, tokens, API keys, or other secrets. Use the `SENSITIVE_FIELDS` pattern to redact sensitive values:

```python
SENSITIVE_FIELDS = {
    "password",
    "token",
    "secret",
    "api_key",
    "authorization",
    "credit_card",
    "ssn",
    "session_id",
    "csrf_token",
}


def filter_sensitive(data: dict[str, object]) -> dict[str, object]:
    """Replace sensitive values with '***'."""
    return {k: "***" if k.lower() in SENSITIVE_FIELDS else v for k, v in data.items()}
```

### Filtering Headers

```python
@app.exception_handler(Exception)
async def safe_error_handler(request, exc):
    # Log request info with sensitive data filtered
    logger.error(
        "Error: {exc} | Headers: {headers}",
        exc=exc,
        headers=filter_sensitive(dict(request.headers)),
    )
    return Response.error(500, "Internal server error")
```

### Filtering Request Body

```python
@app.exception_handler(Exception)
async def safe_body_handler(request, exc):
    body = request.json if request.json else {}
    filtered_body = filter_sensitive(body) if isinstance(body, dict) else body
    logger.error(
        "Error: {exc} | Body: {body}",
        exc=exc,
        body=filtered_body,
    )
    return Response.error(500, "Internal server error")
```

### Nested Data Filtering

For deeply nested structures:

```python
def deep_filter_sensitive(data: object) -> object:
    """Recursively filter sensitive fields from nested structures."""
    if isinstance(data, dict):
        return {
            k: "***" if k.lower() in SENSITIVE_FIELDS else deep_filter_sensitive(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [deep_filter_sensitive(item) for item in data]
    return data
```

## 404 Handling

### Custom 404 Template

```python
@app.exception_handler(HTTPException)
async def handle_http_error(request, exc):
    if exc.status_code == 404:
        return render(request, "errors/404.html", {"path": request.path}, status=404)
    return Response.json({"detail": exc.detail}, status=exc.status_code)
```

### JSON 404 for APIs

```python
@app.exception_handler(HTTPException)
async def handle_http_error(request, exc):
    if exc.status_code == 404:
        # Check if client wants JSON
        accept = request.headers.get("Accept", "")
        if "application/json" in accept:
            return Response.error(404, "Not found", "path": request.path)
        return render(request, "errors/404.html", {"path": request.path}, status=404)
    return Response.json({"detail": exc.detail}, status=exc.status_code)
```

## 500 Handling

### Catch-All Handler

```python
@app.exception_handler(Exception)
async def handle_500(request, exc):
    logger.error(
        "Internal error: {exc_type}: {exc}",
        exc_type=type(exc).__name__,
        exc=exc,
    )

    accept = request.headers.get("Accept", "")
    if "application/json" in accept:
        return Response.error(500, "Internal server error")
    return render(request, "errors/500.html", status=500)
```

### Different Responses by Content Type

```python
@app.exception_handler(Exception)
async def content_negotiated_error(request, exc):
    logger.error("Error: {exc}", exc=exc)

    accept = request.headers.get("Accept", "text/html")

    if "application/json" in accept:
        return Response.json(
            {
                "error": "Internal server error",
                "type": type(exc).__name__,
            },
            status=500,
        )

    if "text/plain" in accept:
        return Response.text("Internal Server Error", status=500)

    return render(request, "errors/500.html", status=500)
```

## Health Check Monitoring

### Basic Health Check

Mount health check endpoints to detect errors before users do:

```python
app.mount_health(
    checks={
        "database": check_db,
        "cache": check_cache,
    }
)
# GET /health  -> 200 (liveness)
# GET /ready   -> 200 or 503 (readiness)
```

### Liveness vs Readiness

- **Liveness** (`/health`) -- is the process running? Always returns 200 unless the process is deadlocked.
- **Readiness** (`/ready`) -- can the process handle requests? Returns 503 if any check fails (database down, cache unreachable, etc.).

### Custom Health Checks

```python
async def check_db():
    """Returns True if database is reachable."""
    try:
        await db.query("SELECT 1")
        return True
    except Exception:
        return False


async def check_cache():
    """Returns True if cache is working."""
    try:
        cache.set("_health", "ok", ttl=10)
        return cache.get("_health") == "ok"
    except Exception:
        return False


async def check_storage():
    """Returns True if file storage is writable."""
    try:
        await storage.save("_health_check", b"ok")
        await storage.delete("_health_check")
        return True
    except Exception:
        return False


app.mount_health(
    checks={
        "database": check_db,
        "cache": check_cache,
        "storage": check_storage,
    }
)
```

### Health Check Response

```json
// GET /ready (all checks pass)
// HTTP 200
{
    "status": "healthy",
    "checks": {
        "database": true,
        "cache": true,
        "storage": true
    }
}

// GET /ready (database down)
// HTTP 503
{
    "status": "unhealthy",
    "checks": {
        "database": false,
        "cache": true,
        "storage": true
    }
}
```

## Security Event Logging

Track security-relevant events separately from application errors:

```python
from hyperdjango.security import SecurityLog, SecurityEvent

log = SecurityLog(db)
await log.ensure_table()
```

### Event Types

| Event                      | Description                                  |
| -------------------------- | -------------------------------------------- |
| `LOGIN_SUCCESS`            | User logged in successfully                  |
| `LOGIN_FAILED`             | Failed login attempt                         |
| `LOGOUT`                   | User logged out                              |
| `PASSWORD_CHANGED`         | User changed password                        |
| `PASSWORD_RESET_REQUESTED` | Password reset requested                     |
| `PASSWORD_RESET_COMPLETED` | Password reset completed                     |
| `PERMISSION_DENIED`        | Authorization failure                        |
| `CSRF_VIOLATION`           | CSRF token mismatch                          |
| `AUTH_REQUIRED`            | Unauthenticated access to protected resource |
| `RATE_LIMIT_HIT`           | Rate limit exceeded                          |
| `SESSION_CREATED`          | New session started                          |
| `SESSION_DESTROYED`        | Session invalidated                          |
| `SESSION_EXPIRED`          | Session expired                              |
| `SESSION_FIXATION_ATTEMPT` | Possible session fixation attack             |

### Logging Events

```python
# Log successful login
await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=42, ip="1.2.3.4")

# Log failed login
await log.log(SecurityEvent.LOGIN_FAILED, ip="1.2.3.4", detail="invalid password")

# Log permission denied
await log.log(
    SecurityEvent.PERMISSION_DENIED,
    user_id=42,
    detail="missing edit_product permission",
)

# Log rate limit hit
await log.log(SecurityEvent.RATE_LIMIT_HIT, ip="1.2.3.4", detail="100/min exceeded")
```

### Querying Security Events

```python
# Recent events
events = await log.get_recent(limit=50)

# Events for a specific user
user_events = await log.get_for_user(42)

# Events from a specific IP
ip_events = await log.get_for_ip("1.2.3.4")

# Failed logins in the last hour
failed_logins = await log.get_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)
```

### Security Alerting

Combine security events with notifications:

```python
@app.exception_handler(PermissionError)
async def handle_permission_denied(request, exc):
    await log.log(
        SecurityEvent.PERMISSION_DENIED,
        user_id=request.user.id if request.user else None,
        ip=request.client_ip,
        detail=str(exc),
    )

    # Alert on repeated permission denials from same IP
    recent = await log.get_for_ip(request.client_ip)
    denied_count = sum(1 for e in recent if e["event_type"] == "permission_denied")
    if denied_count > 10:
        logger.warning(
            "Possible attack from {ip}: {count} permission denials",
            ip=request.client_ip,
            count=denied_count,
        )

    return Response.error(403, "Forbidden")
```

## Structured Error Responses

### Consistent JSON Error Format

Standardize error responses across your API:

```python
def error_response(
    status: int,
    message: str,
    code: str | None = None,
    details: dict | None = None,
) -> Response:
    body = {"error": message}
    if code:
        body["code"] = code
    if details:
        body["details"] = details
    return Response.json(body, status=status)


@app.exception_handler(ValueError)
async def handle_validation(request, exc):
    return error_response(400, str(exc), code="validation_error")


@app.exception_handler(PermissionError)
async def handle_forbidden(request, exc):
    return error_response(403, "Forbidden", code="permission_denied")


@app.exception_handler(Exception)
async def handle_server_error(request, exc):
    logger.error("Unhandled: {exc}", exc=exc)
    return error_response(500, "Internal server error", code="server_error")
```

### Error Response with Validation Details

```python
@app.exception_handler(ValidationError)
async def handle_form_errors(request, exc):
    return error_response(
        422,
        "Validation failed",
        code="validation_error",
        details=exc.errors,  # {"field": ["error message", ...]}
    )
```

## Error Tracking Integration

### External SDK (generic pattern)

Any third-party error tracker that exposes a Python SDK can be wired through the
`@app.exception_handler` decorator. Initialize the SDK at startup, then forward
the exception in the handler:

```python
@app.exception_handler(Exception)
async def external_tracker_handler(request, exc):
    external_sdk.capture_exception(exc)
    return Response.error(500, "Internal server error")
```

### Custom Error Tracker

```python
class ErrorTracker:
    def __init__(self, db):
        self.db = db

    async def capture(self, exc, request):
        await self.db.execute(
            "INSERT INTO error_log (exc_type, message, path, timestamp) "
            "VALUES ($1, $2, $3, NOW())",
            [type(exc).__name__, str(exc), request.path],
        )


tracker = ErrorTracker(db)


@app.exception_handler(Exception)
async def track_error(request, exc):
    await tracker.capture(exc, request)
    return Response.error(500, "Internal server error")
```
