# Security Guide

Comprehensive guide to securing HyperDjango applications: CSRF protection, CORS, security headers, rate limiting, input validation, SQL injection prevention, XSS prevention, and password security.

---

## Table of Contents

- [Security Middleware Stack](#security-middleware-stack)
- [CSRF Protection](#csrf-protection)
- [CORS Configuration](#cors-configuration)
- [Security Headers](#security-headers)
- [Rate Limiting](#rate-limiting)
- [Input Validation](#input-validation)
- [SQL Injection Prevention](#sql-injection-prevention)
- [XSS Prevention](#xss-prevention)
- [Password Security](#password-security)
- [Session Security](#session-security)
- [Security Audit Logging](#security-audit-logging)
- [Configuration Reference](#configuration-reference)
- [Migration Notes for Django Users](#migration-notes-for-django-users)

---

## Security Middleware Stack

A production application should enable the full security middleware stack:

```python
from hyperdjango import HyperApp
from hyperdjango.auth import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore
from hyperdjango import RateLimitMiddleware
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
)

app = HyperApp("myapp", database="postgres://localhost/mydb")

# Order matters: outermost middleware runs first
# 1. Security headers on ALL responses
app.use(
    SecurityHeadersMiddleware(
        hsts=True,
        hsts_max_age=31536000,
        csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'",
        x_frame_options="DENY",
        x_content_type_options="nosniff",
        referrer_policy="strict-origin-when-cross-origin",
    )
)

# 2. CORS for API endpoints
app.use(
    CORSMiddleware(
        origins=["https://myapp.com", "https://admin.myapp.com"],
        methods=["GET", "POST", "PUT", "DELETE"],
        headers=["Content-Type", "Authorization", "X-CSRF-Token"],
        allow_credentials=True,
        max_age=86400,
    )
)

# 3. Rate limiting
app.use(RateLimitMiddleware(max_requests=100, window=60))

# 4. CSRF protection (requires session middleware)
store = DatabaseSessionStore(app.db, max_age=86400)
app.use(SessionAuth(secret="your-secret-key", store=store))
app.use(CSRFMiddleware(secret="csrf-signing-secret"))
```

---

## CSRF Protection

CSRF protection prevents cross-site request forgery by requiring a signed token on state-changing requests (POST, PUT, PATCH, DELETE).

### How It Works

1. `CSRFMiddleware` generates a signed CSRF token and stores it in the session
2. The token must be included in state-changing requests via header or form field
3. The middleware validates the token before allowing the request to proceed

### Configuration

```python
from hyperdjango.standalone_middleware import CSRFMiddleware

app.use(
    CSRFMiddleware(
        secret="csrf-signing-secret-256-bits",
        cookie_name="csrf_token",
        header_name="X-CSRF-Token",
        safe_methods=("GET", "HEAD", "OPTIONS"),
    )
)
```

### Sending the CSRF Token

For API clients, read the CSRF token from the cookie and include it in the request header:

```javascript
// JavaScript frontend
const csrfToken = document.cookie
  .split("; ")
  .find((row) => row.startsWith("csrf_token="))
  ?.split("=")[1];

fetch("/api/products", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
  },
  body: JSON.stringify({ name: "Widget", price: 29.99 }),
  credentials: "include",
});
```

For HTML forms:

```html
<form method="POST" action="/products/create">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  <input type="text" name="name" />
  <button type="submit">Create</button>
</form>
```

### Exempt Endpoints

API endpoints that use API key authentication (not cookies) typically do not need CSRF protection. The middleware skips requests that do not have a session cookie.

---

## CORS Configuration

Control which origins can make cross-origin requests:

```python
from hyperdjango.standalone_middleware import CORSMiddleware

# Strict: specific origins only
app.use(
    CORSMiddleware(
        origins=["https://myapp.com", "https://admin.myapp.com"],
        methods=["GET", "POST", "PUT", "DELETE"],
        headers=["Content-Type", "Authorization", "X-CSRF-Token"],
        allow_credentials=True,
        max_age=86400,  # Preflight cache time in seconds
    )
)

# Development: allow all origins (never use in production)
app.use(CORSMiddleware(origins=["*"]))
```

### CORS Parameters

| Parameter           | Type        | Default                                                | Description                         |
| ------------------- | ----------- | ------------------------------------------------------ | ----------------------------------- |
| `origins`           | `list[str]` | `["*"]`                                                | Allowed origin URLs                 |
| `methods`           | `list[str]` | `["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]` | Allowed HTTP methods                |
| `headers`           | `list[str]` | `["*"]`                                                | Allowed request headers             |
| `allow_credentials` | `bool`      | `False`                                                | Allow cookies/auth headers          |
| `max_age`           | `int`       | `86400`                                                | Preflight cache duration in seconds |

### Common Mistakes

1. Setting `origins=["*"]` with `allow_credentials=True` -- browsers reject this combination.
2. Forgetting to include `X-CSRF-Token` in `headers` when using CSRF protection.
3. Not including `Content-Type` in `headers` for JSON APIs.

---

## Security Headers

The `SecurityHeadersMiddleware` adds protective HTTP headers to every response:

```python
from hyperdjango.standalone_middleware import SecurityHeadersMiddleware

app.use(
    SecurityHeadersMiddleware(
        hsts=True,
        hsts_max_age=31536000,  # 1 year
        hsts_include_subdomains=True,
        hsts_preload=True,
        csp="default-src 'self'; script-src 'self'; img-src 'self' data:",
        x_frame_options="DENY",
        x_content_type_options="nosniff",
        referrer_policy="strict-origin-when-cross-origin",
        permissions_policy="camera=(), microphone=(), geolocation=()",
    )
)
```

### Headers Set

| Header                      | Value                                 | Purpose                  |
| --------------------------- | ------------------------------------- | ------------------------ |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | Force HTTPS              |
| `Content-Security-Policy`   | `default-src 'self'`                  | Prevent XSS, injection   |
| `X-Frame-Options`           | `DENY`                                | Prevent clickjacking     |
| `X-Content-Type-Options`    | `nosniff`                             | Prevent MIME sniffing    |
| `Referrer-Policy`           | `strict-origin-when-cross-origin`     | Control referrer leakage |
| `Permissions-Policy`        | `camera=(), microphone=()`            | Disable browser features |

### Content Security Policy Examples

```python
# Strict (no inline scripts, no external resources)
csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'"

# Allow inline styles (common for CSS frameworks)
csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"

# Allow CDN resources
csp = "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' https://cdn.jsdelivr.net"

# Allow images from anywhere
csp = "default-src 'self'; img-src *"

# Report violations (useful during rollout)
csp = "default-src 'self'; report-uri /api/csp-report"
```

### Per-View Overrides

```python
from hyperdjango.shortcuts import xframe_options_deny


@app.get("/embed")
@xframe_options_deny
async def no_embed(request):
    """Override X-Frame-Options for this specific view."""
    return Response.html("<h1>Cannot be embedded</h1>")
```

---

## Rate Limiting

### In-Memory Rate Limiting (Single Server)

```python
from hyperdjango import RateLimitMiddleware

# 100 requests per minute per IP
app.use(RateLimitMiddleware(max_requests=100, window=60))
```

### Database-Backed Rate Limiting (Multi-Server)

```python
from hyperdjango.ratelimit import DatabaseRateLimitBackend, RateLimitMiddleware

backend = DatabaseRateLimitBackend(app.db)
await backend.ensure_table()  # Creates PostgreSQL UNLOGGED table

app.use(
    RateLimitMiddleware(
        max_requests=100,
        window=60,
        backend=backend,
    )
)
```

### Hierarchical Rate Limits

Apply different limits at different scopes:

```python
from hyperdjango.ratelimit import ip_key, org_key, user_key

# Layer 1: 10 requests/second per IP (burst protection)
app.use(RateLimitMiddleware(max_requests=10, window=1, key_func=ip_key))

# Layer 2: 100 requests/minute per user
app.use(RateLimitMiddleware(max_requests=100, window=60, key_func=user_key))

# Layer 3: 5000 requests/hour per organization
app.use(RateLimitMiddleware(max_requests=5000, window=3600, key_func=org_key))
```

### Tiered Rate Limiting (Per-Plan Limits)

```python
from hyperdjango.ratelimit import TieredRateLimitMiddleware

tiers = {
    "free": {"max_requests": 100, "window": 60},
    "pro": {"max_requests": 1000, "window": 60},
    "enterprise": {"max_requests": 10000, "window": 60},
}

app.use(
    TieredRateLimitMiddleware(
        tiers=tiers,
        default_tier="free",
        db=app.db,
    )
)
```

### Rule-Based Rate Limiting

Different limits for different endpoints:

```python
from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware

rules = [
    {"path": "/api/auth/login", "method": "POST", "max_requests": 5, "window": 60},
    {"path": "/api/auth/register", "method": "POST", "max_requests": 3, "window": 300},
    {"path": "/api/search", "method": "GET", "max_requests": 30, "window": 60},
    {"path": "/api/*", "method": "*", "max_requests": 100, "window": 60},
]

app.use(RuleBasedRateLimitMiddleware(rules=rules, db=app.db))
```

### Rate Limit Response

When a client exceeds the limit, a 429 response is returned:

```json
{ "error": "Rate limit exceeded", "retry_after": 42 }
```

The `Retry-After` header indicates when the client can retry.

---

## Input Validation

### Model-Level Validation (Native Zig SIMD)

Model fields with validation constraints are checked at the native level using SIMD-accelerated validation:

```python
from hyperdjango.models import Field, Model


class UserRegistration(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(min_length=3, max_length=30, regex=r"^[a-zA-Z0-9_]+$")
    email: str = Field()  # Email validation via native SIMD (77ns per email)
    age: int = Field(ge=13, le=150)
    password: str = Field(min_length=8)
```

Validation performance:

- 1.6M model validations/second (full model with all fields)
- 6.7M individual field validations/second
- 51.5M integer range checks/second (SIMD 4-wide)
- 77ns per email validation (SIMD)

### Form Validation

```python
from hyperdjango.forms import CharField, Form, IntegerField


class ContactForm(Form):
    name = CharField(max_length=100, required=True)
    email = CharField(max_length=200, required=True)
    message = CharField(widget="textarea", required=True)
    age = IntegerField(min_value=0, max_value=150, required=False)

    def clean(self, data):
        """Cross-field validation."""
        errors = {}
        if data.get("message") and len(data["message"]) < 10:
            errors["message"] = ["Message must be at least 10 characters"]
        return errors


form = ContactForm(data=request.json)
if form.is_valid():
    cleaned = form.cleaned_data
else:
    errors = form.errors  # {"name": ["This field is required"], ...}
```

### Request Body Size Limit

```python
app = HyperApp(
    "myapp",
    max_body_size=10 * 1024 * 1024,  # 10MB (default)
)
```

Requests exceeding the body size limit are rejected before the body is read.

---

## SQL Injection Prevention

All database queries use parameterized placeholders. User input is NEVER interpolated into SQL strings.

### Parameterized Queries (Safe)

```python
# ORM queries are always parameterized
users = await User.objects.filter(username=user_input).all()
# Generates: SELECT * FROM users WHERE username = $1
# With params: [user_input]

# Raw queries use $N placeholders
rows = await db.query(
    "SELECT * FROM products WHERE name ILIKE $1 AND price < $2",
    f"%{search_term}%",
    max_price,
)

# where_raw uses {idx} placeholders (converted to $N internally)
results = await Product.objects.where_raw(
    "(name ILIKE {idx} OR description ILIKE {idx})",
    f"%{search_term}%",
).all()
```

### What NOT to Do

```python
# DANGEROUS: string interpolation (NEVER do this)
# rows = await db.query(f"SELECT * FROM users WHERE name = '{name}'")

# DANGEROUS: format strings in SQL
# rows = await db.query("SELECT * FROM users WHERE name = '%s'" % name)
```

The pg.zig driver sends parameters separately from the SQL statement using the PostgreSQL binary protocol. Parameters are never parsed as SQL.

---

## XSS Prevention

### Template Auto-Escaping

The native Zig template engine auto-escapes all output by default:

```html
{# User input is automatically HTML-escaped #}
<p>{{ user.name }}</p>
{# If user.name = '
<script>
  alert("xss");
</script>
' #} {# Output:
<p>&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;</p>
#}
```

To disable escaping for trusted content:

```html
{# Only use for content you trust completely #} {% autoescape false %} {{
trusted_html_content }} {% endautoescape %} {# Or per-variable #} {{
trusted_content | safe }}
```

### JSON Response Escaping

`Response.json()` serializes data via the native SIMD JSON serializer, which properly escapes all string values:

```python
# Special characters are escaped in JSON output
return Response.json({"message": user_input})
# Produces valid JSON with escaped quotes, backslashes, etc.
```

### SIMD HTML Escaping

The native Zig `html_escape` function processes strings 1.7-3.4x faster than Python:

```python
from hyperdjango.native._strings import html_escape

safe_html = html_escape(user_input)
# Escapes: & < > " '
```

---

## Password Security

### argon2id Hashing

All passwords are hashed with argon2id, the recommended algorithm for password hashing (RFC 9106). It is:

- Memory-hard: resistant to GPU/ASIC attacks
- Time-hard: configurable iteration count
- Side-channel resistant: the "id" variant

```python
from hyperdjango.auth import hash_password, needs_rehash, verify_password

# Hash (includes random salt)
hashed = hash_password("user-password")

# Verify (constant-time comparison)
is_valid = verify_password("user-password", hashed)

# Automatic rehashing when parameters are upgraded
if needs_rehash(hashed):
    new_hash = hash_password("user-password")
```

### Password Strength Validation

```python
from hyperdjango.auth.password_validation import validate_password

errors = validate_password("short")
# ["Password must be at least 8 characters"]

errors = validate_password("12345678")
# ["Password is entirely numeric"]

errors = validate_password("password123")
# ["Password is too common"]

errors = validate_password("Xk9#mP2$vL7!", user=user)
# [] (valid)
```

Built-in validators:

- Minimum length (default: 8 characters)
- Maximum length
- Numeric-only check
- Common password list (20,000+ passwords)
- User attribute similarity (username, email)

---

## Session Security

### Secure Session Configuration

```python
from hyperdjango.auth import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore

store = DatabaseSessionStore(app.db, max_age=86400)  # 24 hours
await store.ensure_table()

app.use(
    SessionAuth(
        secret="256-bit-secret-key",
        store=store,
        cookie_name="session",
        cookie_httponly=True,  # Not accessible via JavaScript
        cookie_secure=True,  # HTTPS only (set True in production)
        cookie_samesite="Lax",  # Prevents CSRF on most cross-site requests
    )
)
```

### Session Invalidation

Sessions are automatically invalidated when a user changes their password (via the session auth hash mechanism):

```python
# When password changes, all existing sessions for that user become invalid
user.set_password("new-password")
await user.save()
# All other browser sessions will require re-login on their next request
```

### Manual Session Invalidation

```python
# Invalidate all sessions for a specific user
await store.delete_user_sessions(user_id=user.id)
```

---

## Security Audit Logging

Track security-relevant events:

```python
from hyperdjango.security import SecurityEvent, SecurityLog

security_log = SecurityLog(app.db)
await security_log.ensure_table()

# Log events
await security_log.log(
    SecurityEvent(
        event_type="login_success",
        user_id=user.id,
        ip_address=request.client_ip,
        details={"username": user.username},
    )
)

await security_log.log(
    SecurityEvent(
        event_type="login_failed",
        ip_address=request.client_ip,
        details={"username": attempted_username},
    )
)

# Query events
events = await security_log.get_events(
    event_type="login_failed",
    ip_address="192.168.1.100",
    since=datetime.now(UTC) - timedelta(hours=1),
)

# User activity
activity = await security_log.get_user_events(user_id=user.id, limit=50)

# Cleanup old events
await security_log.cleanup(older_than=timedelta(days=90))
```

### Event Types

| Event Type          | When Logged                   |
| ------------------- | ----------------------------- |
| `login_success`     | Successful authentication     |
| `login_failed`      | Failed authentication attempt |
| `logout`            | User logout                   |
| `password_changed`  | Password update               |
| `permission_denied` | Authorization failure         |
| `rate_limited`      | Rate limit exceeded           |
| `csrf_failed`       | CSRF token validation failure |

---

## Configuration Reference

### SecurityHeadersMiddleware

| Parameter                 | Type   | Default                             | Description                   |
| ------------------------- | ------ | ----------------------------------- | ----------------------------- |
| `hsts`                    | `bool` | `False`                             | Enable HSTS header            |
| `hsts_max_age`            | `int`  | `31536000`                          | HSTS max-age in seconds       |
| `hsts_include_subdomains` | `bool` | `True`                              | Include subdomains in HSTS    |
| `hsts_preload`            | `bool` | `False`                             | HSTS preload flag             |
| `csp`                     | `str`  | `None`                              | Content-Security-Policy value |
| `x_frame_options`         | `str`  | `"DENY"`                            | X-Frame-Options value         |
| `x_content_type_options`  | `str`  | `"nosniff"`                         | X-Content-Type-Options value  |
| `referrer_policy`         | `str`  | `"strict-origin-when-cross-origin"` | Referrer-Policy value         |
| `permissions_policy`      | `str`  | `None`                              | Permissions-Policy value      |

### CSRFMiddleware

| Parameter      | Type         | Default                      | Description              |
| -------------- | ------------ | ---------------------------- | ------------------------ |
| `secret`       | `str`        | required                     | Token signing key        |
| `cookie_name`  | `str`        | `"csrf_token"`               | CSRF cookie name         |
| `header_name`  | `str`        | `"X-CSRF-Token"`             | CSRF header name         |
| `safe_methods` | `tuple[str]` | `("GET", "HEAD", "OPTIONS")` | Methods exempt from CSRF |

### RateLimitMiddleware

| Parameter      | Type       | Default           | Description                        |
| -------------- | ---------- | ----------------- | ---------------------------------- |
| `max_requests` | `int`      | `100`             | Max requests per window            |
| `window`       | `int`      | `60`              | Window size in seconds             |
| `key_func`     | `callable` | `ip_key`          | Function to extract rate limit key |
| `backend`      | backend    | `InMemoryBackend` | Storage backend                    |

---

## Migration Notes for Django Users

### Key Differences

| Django                                      | HyperDjango                                             |
| ------------------------------------------- | ------------------------------------------------------- |
| `{% csrf_token %}` template tag             | `X-CSRF-Token` header or hidden field                   |
| `django-cors-headers` package               | Built-in `CORSMiddleware`                               |
| `SECURE_HSTS_SECONDS` setting               | `SecurityHeadersMiddleware(hsts_max_age=...)`           |
| `SECURE_CONTENT_TYPE_NOSNIFF` setting       | `SecurityHeadersMiddleware(x_content_type_options=...)` |
| `X_FRAME_OPTIONS` setting                   | `SecurityHeadersMiddleware(x_frame_options=...)`        |
| `django-ratelimit` package                  | Built-in `RateLimitMiddleware`                          |
| PostgreSQL UNLOGGED table for rate limiting | Built-in                                                |
| `django.contrib.auth.hashers`               | `hyperdjango.auth.passwords` (argon2id only)            |
| Multiple password hashers                   | Single hasher (argon2id)                                |
| `settings.py` security config               | Middleware constructor parameters                       |

### What Stayed the Same

- CSRF protection concept (token verification on state-changing requests)
- Session-based authentication (server-side sessions, signed cookies)
- Parameterized queries for SQL injection prevention
- Auto-escaping in templates for XSS prevention
- Password hashing with automatic upgrade support
