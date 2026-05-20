# Security

Security event audit logging, HTTP security headers, CSRF protection, CORS, rate limiting, and compression middleware.

## Quick Start

```python
from hyperdjango import RateLimitMiddleware
from hyperdjango.standalone_middleware import (
    SecurityHeadersMiddleware,
    CSRFMiddleware,
    CORSMiddleware,
    CompressionMiddleware,
)
from hyperdjango.security import SecurityLog, SecurityEvent

app = HyperApp()

# Security headers on all responses
app.use(SecurityHeadersMiddleware(hsts=True, csp="default-src 'self'"))

# CSRF protection
app.use(CSRFMiddleware(secret="your-csrf-secret"))

# CORS
app.use(CORSMiddleware(origins=["https://example.com"]))

# Rate limiting (100 requests per minute per IP)
app.use(RateLimitMiddleware(max_requests=100, window=60))
```

## SecurityLog

Tracks security-relevant events in a PostgreSQL UNLOGGED table for fast writes with multi-server visibility.

```python
from hyperdjango.security import SecurityLog, SecurityEvent

log = SecurityLog(db)
await log.ensure_table()
```

### ensure_table()

Creates the `hyper_security_log` table and indexes. Uses UNLOGGED for performance (no WAL writes). Falls back to a regular table if UNLOGGED is not supported. Safe to call multiple times.

```python
await log.ensure_table()
```

### Table Schema

```sql
CREATE UNLOGGED TABLE hyper_security_log (
    id SERIAL PRIMARY KEY,
    event VARCHAR(50) NOT NULL,
    user_id INTEGER,
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),
    path VARCHAR(2000),
    detail TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Indexes on event, user_id, ip_address, timestamp DESC
```

### log()

Log a security event manually:

```python
await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=42, ip="1.2.3.4")
await log.log(SecurityEvent.LOGIN_FAILED, ip="1.2.3.4", detail="invalid password")
await log.log(
    SecurityEvent.PERMISSION_DENIED, user_id=42, detail="missing edit_product"
)
await log.log(SecurityEvent.RATE_LIMIT_HIT, ip="1.2.3.4", detail="100/min exceeded")
```

| Parameter    | Type            | Default  | Description                                |
| ------------ | --------------- | -------- | ------------------------------------------ |
| `event`      | `SecurityEvent` | required | Event type enum value                      |
| `user_id`    | `int \| None`   | `None`   | User ID if known                           |
| `ip`         | `str \| None`   | `None`   | Client IP address                          |
| `user_agent` | `str \| None`   | `None`   | User-Agent header (truncated to 500 chars) |
| `path`       | `str \| None`   | `None`   | Request path                               |
| `detail`     | `str \| None`   | `None`   | Free-text detail about the event           |

### log_from_request()

Log an event with IP, user-agent, path, and user auto-extracted from a Request object:

```python
await log.log_from_request(
    SecurityEvent.CSRF_VIOLATION, request, detail="token mismatch"
)
```

Auto-extracts:

- `ip` from `request.client_ip`
- `user_agent` from `request.headers["user-agent"]` (truncated to 500 chars)
- `path` from `request.path`
- `user_id` from `request.user.id` or `request.user.pk` (if `user_id` not provided)

### Event Types

#### Authentication Events

| Event                      | Description                          |
| -------------------------- | ------------------------------------ |
| `LOGIN_SUCCESS`            | User logged in successfully          |
| `LOGIN_FAILED`             | Login attempt with wrong credentials |
| `LOGOUT`                   | User logged out                      |
| `PASSWORD_CHANGED`         | User changed their password          |
| `PASSWORD_RESET_REQUESTED` | Password reset email sent            |
| `PASSWORD_RESET_COMPLETED` | Password reset completed             |

#### Authorization Events

| Event               | Description                                          |
| ------------------- | ---------------------------------------------------- |
| `PERMISSION_DENIED` | User lacks required permission                       |
| `CSRF_VIOLATION`    | CSRF token missing or invalid                        |
| `AUTH_REQUIRED`     | Request to protected resource without authentication |

#### Rate Limiting Events

| Event            | Description                |
| ---------------- | -------------------------- |
| `RATE_LIMIT_HIT` | Client exceeded rate limit |

#### Session Events

| Event                      | Description                              |
| -------------------------- | ---------------------------------------- |
| `SESSION_CREATED`          | New session created                      |
| `SESSION_DESTROYED`        | Session explicitly destroyed (logout)    |
| `SESSION_EXPIRED`          | Session expired due to timeout           |
| `SESSION_FIXATION_ATTEMPT` | Detected session fixation attack attempt |

#### Suspicious Activity Events

| Event                    | Description                                         |
| ------------------------ | --------------------------------------------------- |
| `SUSPICIOUS_INPUT`       | Input matching known attack patterns                |
| `PATH_TRAVERSAL_ATTEMPT` | Attempt to access files outside allowed directories |
| `INVALID_TOKEN`          | Invalid or expired authentication token             |

### Querying Events

#### get_recent()

Get the most recent security events:

```python
events = await log.get_recent(limit=50)
# Returns list of dicts with: id, event, user_id, ip_address, user_agent, path, detail, timestamp
```

#### get_for_user()

Get events for a specific user:

```python
user_events = await log.get_for_user(42)
user_events = await log.get_for_user(42, limit=20)
```

#### get_for_ip()

Get events from a specific IP address:

```python
ip_events = await log.get_for_ip("1.2.3.4")
ip_events = await log.get_for_ip("1.2.3.4", limit=20)
```

#### get_by_event()

Get events of a specific type within a time window:

```python
failed_logins = await log.get_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)
csrf_violations = await log.get_by_event(SecurityEvent.CSRF_VIOLATION, since_hours=24)
```

#### count_by_event()

Count events of a specific type within a time window:

```python
count = await log.count_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)
if count > 100:
    # Possible brute-force attack
    await alert_security_team()
```

#### count_by_ip()

Count events from a specific IP within a time window:

```python
ip_count = await log.count_by_ip("1.2.3.4", SecurityEvent.LOGIN_FAILED, since_hours=1)
if ip_count > 10:
    # Lock out this IP
    await block_ip("1.2.3.4")
```

### Brute-Force Detection Pattern

```python
@app.post("/login")
async def login(request):
    data = await request.json()
    ip = request.client_ip

    # Check if IP is already rate-limited
    recent_failures = await log.count_by_ip(
        ip, SecurityEvent.LOGIN_FAILED, since_hours=1
    )
    if recent_failures > 10:
        await log.log(SecurityEvent.RATE_LIMIT_HIT, ip=ip, detail="login lockout")
        return Response.json({"error": "Too many attempts"}, status=429)

    user = await authenticate(data["email"], data["password"])
    if user is None:
        await log.log(
            SecurityEvent.LOGIN_FAILED, ip=ip, detail=f"email={data['email']}"
        )
        return Response.json({"error": "Invalid credentials"}, status=401)

    await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=user.id, ip=ip)
    return Response.json({"token": create_session(user)})
```

### cleanup()

Delete entries older than N days:

```python
await log.cleanup(days=90)
```

### Global Singleton

```python
from hyperdjango.security import get_security_log, set_security_log

# Set during app startup
set_security_log(SecurityLog(db))

# Access from anywhere
log = get_security_log()
```

## SecurityHeadersMiddleware

Adds security headers to all responses. Headers are pre-computed at init time for zero per-request overhead.

```python
from hyperdjango.standalone_middleware import SecurityHeadersMiddleware

app.use(
    SecurityHeadersMiddleware(
        content_type_nosniff=True,
        frame_options="DENY",
        hsts=False,
        hsts_max_age=31536000,
        csp="default-src 'self'",
        referrer_policy="strict-origin-when-cross-origin",
        permissions_policy="camera=(), microphone=()",
        cross_origin_opener_policy="same-origin",
    )
)
```

### Headers Explained

#### X-Content-Type-Options: nosniff

**Default: enabled.** Prevents the browser from MIME-type sniffing. Without this, a browser might interpret a text file as JavaScript if it contains valid JS, enabling XSS attacks.

```python
SecurityHeadersMiddleware(content_type_nosniff=True)
# X-Content-Type-Options: nosniff
```

#### X-Frame-Options

**Default: DENY.** Controls whether the page can be embedded in `<iframe>`, `<frame>`, or `<object>`. Prevents clickjacking attacks where an attacker overlays your site with invisible frames.

| Value          | Description                                         |
| -------------- | --------------------------------------------------- |
| `"DENY"`       | Page cannot be framed by anyone                     |
| `"SAMEORIGIN"` | Page can only be framed by pages on the same origin |

```python
SecurityHeadersMiddleware(frame_options="DENY")
# X-Frame-Options: DENY
```

#### Strict-Transport-Security (HSTS)

**Default: disabled.** Tells the browser to always use HTTPS for this domain. Once set, the browser will not make insecure HTTP requests to this domain for `max-age` seconds.

```python
SecurityHeadersMiddleware(hsts=True, hsts_max_age=31536000)
# Strict-Transport-Security: max-age=31536000
```

Only enable HSTS when you are certain HTTPS is fully configured. HSTS is difficult to undo -- if you set `max-age=31536000` (1 year), browsers will refuse HTTP connections to your domain for a year.

#### Content-Security-Policy (CSP)

**Default: not set** (a default would break most apps' inline scripts/CDNs). CSP
controls which resources the browser may load and is the most effective
defense-in-depth against XSS. Set it via `SECURE_CSP` (applied identically on the
ASGI and Django paths) or the middleware `csp=` argument.

**Recommended starting policy for a user-content app (social/CMS).** Blocks
inline scripts (defeats most reflected/stored XSS), plugins, base-tag injection,
and framing, while allowing user avatars/images over HTTPS:

```python
SECURE_CSP = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; "
    "img-src 'self' data: https:; style-src 'self'"
)
```

Tighten `img-src`/`style-src` and add explicit `script-src`/`connect-src` hosts
for your CDNs and APIs. Roll out with `Content-Security-Policy-Report-Only` first
to find violations before enforcing.

```python
# Restrictive: only load resources from same origin
SecurityHeadersMiddleware(csp="default-src 'self'")

# Allow inline styles and scripts from a CDN
SecurityHeadersMiddleware(
    csp="default-src 'self'; script-src 'self' https://cdn.example.com; style-src 'self' 'unsafe-inline'"
)

# Report violations without blocking
SecurityHeadersMiddleware(csp="default-src 'self'; report-uri /csp-report")
```

Common CSP directives:

| Directive     | Controls                        |
| ------------- | ------------------------------- |
| `default-src` | Fallback for all resource types |
| `script-src`  | JavaScript sources              |
| `style-src`   | CSS sources                     |
| `img-src`     | Image sources                   |
| `font-src`    | Font sources                    |
| `connect-src` | Fetch/XHR/WebSocket targets     |
| `frame-src`   | iframe sources                  |
| `object-src`  | Plugin sources (Flash, etc.)    |

#### Referrer-Policy

**Default: strict-origin-when-cross-origin.** Controls how much referrer information is sent with requests.

| Value                               | Description                                                           |
| ----------------------------------- | --------------------------------------------------------------------- |
| `"no-referrer"`                     | Never send the Referer header                                         |
| `"same-origin"`                     | Only send for same-origin requests                                    |
| `"strict-origin"`                   | Send origin only (no path) for cross-origin, full URL for same-origin |
| `"strict-origin-when-cross-origin"` | Default. Origin for cross-origin, full URL for same-origin            |

```python
SecurityHeadersMiddleware(referrer_policy="strict-origin-when-cross-origin")
# Referrer-Policy: strict-origin-when-cross-origin
```

#### Cross-Origin-Opener-Policy (COOP)

**Default: same-origin.** Isolates the browsing context from cross-origin popup windows. Prevents cross-origin attacks where a popup could access `window.opener` of your page.

```python
SecurityHeadersMiddleware(cross_origin_opener_policy="same-origin")
# Cross-Origin-Opener-Policy: same-origin
```

#### Permissions-Policy

Controls which browser features (camera, microphone, geolocation, etc.) the page can use:

```python
SecurityHeadersMiddleware(
    permissions_policy="camera=(), microphone=(), geolocation=(self)"
)
# Permissions-Policy: camera=(), microphone=(), geolocation=(self)
```

`()` disables the feature entirely. `(self)` allows it only on same-origin pages.

### Default Headers

The following headers are set with no configuration needed:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Cross-Origin-Opener-Policy: same-origin
```

## CSRFMiddleware

Double-submit cookie pattern for CSRF protection. Safe methods (GET, HEAD, OPTIONS) pass through. Unsafe methods (POST, PUT, PATCH, DELETE) require a valid CSRF token.

```python
from hyperdjango.standalone_middleware import CSRFMiddleware

app.use(
    CSRFMiddleware(
        secret="your-csrf-secret",
        cookie_name="csrf_token",
        header_name="x-csrf-token",
        field_name="_csrf_token",
        exempt_paths={"/webhooks/", "/api/public/"},
    )
)
```

| Parameter      | Type       | Default          | Description                            |
| -------------- | ---------- | ---------------- | -------------------------------------- |
| `secret`       | `str`      | required         | HMAC signing key for token generation  |
| `cookie_name`  | `str`      | `"csrf_token"`   | Name of the CSRF cookie                |
| `header_name`  | `str`      | `"x-csrf-token"` | Header to check for the token          |
| `field_name`   | `str`      | `"_csrf_token"`  | Form field name to check for the token |
| `exempt_paths` | `set[str]` | `set()`          | Paths that skip CSRF validation        |

### How It Works

1. **Token generation**: On safe requests (GET), the middleware generates a token with HMAC signature: `{random}.{hmac_signature}` and sets it as a cookie.
2. **Token validation**: On unsafe requests (POST, PUT, etc.), the middleware checks for the token in either the `X-CSRF-Token` header or the `_csrf_token` form field.
3. **Double-submit verification**: The submitted token must match the cookie value (double-submit cookie pattern).
4. **API key bypass**: Requests authenticated via API key skip CSRF validation (API keys are not vulnerable to CSRF).

### In Templates

Include the CSRF token in forms:

```html
<form method="POST" action="/submit">
  <input type="hidden" name="_csrf_token" value="{{ csrf_token }}" />
  <button type="submit">Submit</button>
</form>
```

### In JavaScript

Read the token from the cookie and send it as a header:

```javascript
const csrfToken = document.cookie
  .split("; ")
  .find((row) => row.startsWith("csrf_token="))
  ?.split("=")[1];

fetch("/api/data", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
  },
  body: JSON.stringify(data),
});
```

## SQL Injection Prevention

HyperDjango prevents SQL injection through parameterized queries at every level:

### ORM Queries (Always Safe)

The ORM constructs all queries with parameterization. User input is never interpolated into SQL:

```python
# Safe: values are parameterized
users = await User.objects.filter(name=user_input).all()
# Generates: SELECT * FROM users WHERE name = $1 (with user_input as parameter)
```

### Raw Queries (Use Parameters)

When writing raw SQL, always use parameterized queries:

```python
# SAFE: parameterized query
rows = await db.query(
    "SELECT * FROM users WHERE email = $1 AND active = $2",
    email,
    True,
)

# DANGEROUS: never do this
rows = await db.query(f"SELECT * FROM users WHERE email = '{email}'")  # SQL INJECTION!
```

The native pg.zig driver uses PostgreSQL's extended query protocol, which separates SQL text from parameter values at the wire level -- parameters cannot break out of their value context.

### Prepared Statements

Frequently executed queries use prepared statements, which are parsed once and executed with different parameters. The parse phase rejects malformed SQL before any parameters are bound.

## XSS Prevention

### Auto-Escaping in Templates

The Zig template engine auto-escapes all variable output by default:

```html
<!-- Template: {{ user_input }} -->
<!-- If user_input = "<script>alert('xss')</script>" -->
<!-- Renders: &lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt; -->
```

The native Zig `html_escape` function (1.7-3.4x faster than Python) escapes `<`, `>`, `&`, `"`, and `'`.

### Marking Content as Safe

To output raw HTML, use the `safe` filter explicitly:

```html
{{ trusted_html|safe }}
```

Only use `safe` with content you fully control. Never apply it to user input.

### JSON in HTML

When embedding data in `<script>` tags, use JSON serialization:

```html
<script>
  const data = {{ data|tojson }};
</script>
```

The `tojson` filter serializes to JSON and escapes `</script>` sequences.

## Clickjacking Protection

The `X-Frame-Options: DENY` header (set by default via `SecurityHeadersMiddleware`) prevents your pages from being embedded in frames on other sites. This blocks clickjacking attacks where an attacker overlays your UI with invisible frames to trick users into clicking.

For sites that need framing from same-origin pages:

```python
app.use(SecurityHeadersMiddleware(frame_options="SAMEORIGIN"))
```

For more granular control, use CSP's `frame-ancestors` directive instead:

```python
app.use(
    SecurityHeadersMiddleware(
        csp="default-src 'self'; frame-ancestors 'self' https://trusted.example.com"
    )
)
```

## SSL/HTTPS

### Enforcing HTTPS

Configure HSTS to ensure browsers always use HTTPS:

```python
app.use(SecurityHeadersMiddleware(hsts=True, hsts_max_age=31536000))
```

### Secure Cookies

Always set `secure=True` on session cookies in production:

```python
response.set_cookie(
    "session_id",
    value=session_id,
    httponly=True,
    secure=True,  # HTTPS only
    samesite="Lax",
)
```

### Detecting HTTPS

Behind a reverse proxy, `request.is_secure` checks `X-Forwarded-Proto`:

```python
if not request.is_secure:
    return Response.redirect(
        f"https://{request.headers['host']}{request.path}", status=301
    )
```

## Host Header Validation

Fake `Host` headers can be used for cache poisoning, password reset poisoning, and CSRF attacks. Validate the Host header against an allowlist:

```python
ALLOWED_HOSTS = {"example.com", "www.example.com", "api.example.com"}


@app.middleware
async def validate_host(request, call_next):
    host = request.headers.get("host", "").split(":")[0]  # Strip port
    if host not in ALLOWED_HOSTS:
        return Response.json({"error": "Invalid host"}, status=400)
    return await call_next(request)
```

In production behind a reverse proxy, also validate `X-Forwarded-Host` if your proxy sets it.

## Sensitive Data Filtering

### In Logs

Filter sensitive fields from request logging:

```python
SENSITIVE_FIELDS = {"password", "token", "secret", "api_key", "credit_card"}


def filter_sensitive(data):
    if isinstance(data, dict):
        return {
            k: "***" if k.lower() in SENSITIVE_FIELDS else filter_sensitive(v)
            for k, v in data.items()
        }
    return data
```

### In Error Reports

The `detail` field in `SecurityLog` events should never contain passwords or tokens. Log only identifiers:

```python
# Good: log the email, not the password
await log.log(SecurityEvent.LOGIN_FAILED, ip=ip, detail=f"email={email}")

# Bad: never log credentials
await log.log(SecurityEvent.LOGIN_FAILED, detail=f"email={email} password={password}")
```

## CORSMiddleware

Cross-Origin Resource Sharing with preflight handling.

```python
from hyperdjango.standalone_middleware import CORSMiddleware

app.use(
    CORSMiddleware(
        origins=["https://example.com", "https://app.example.com"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        headers=["*"],
        allow_credentials=False,
        max_age=86400,
    )
)
```

| Parameter           | Type        | Default                | Description                                 |
| ------------------- | ----------- | ---------------------- | ------------------------------------------- |
| `origins`           | `list[str]` | required               | Allowed origins (`["*"]` for all)           |
| `methods`           | `list[str]` | `["GET", "POST", ...]` | Allowed HTTP methods                        |
| `headers`           | `list[str]` | `["*"]`                | Allowed request headers                     |
| `allow_credentials` | `bool`      | `False`                | Allow cookies/auth in cross-origin requests |
| `max_age`           | `int`       | `86400`                | Preflight cache duration in seconds         |

OPTIONS preflight requests return 204 with the appropriate CORS headers.

## RateLimitMiddleware

Windowed token-bucket rate limiter with no external dependencies. Admits at most `max_requests` per fixed `window`-second window (O(1) memory per key, sharded locks).

```python
from hyperdjango import RateLimitMiddleware

app.use(
    RateLimitMiddleware(
        max_requests=100,
        window=60,
        key_func=lambda r: r.client_ip,
    )
)
```

| Parameter      | Type     | Default                 | Description                        |
| -------------- | -------- | ----------------------- | ---------------------------------- |
| `max_requests` | `int`    | required                | Maximum requests per window        |
| `window`       | `int`    | required                | Window size in seconds             |
| `key_func`     | callable | `lambda r: r.client_ip` | Function to extract rate limit key |

Response headers on every request:

- `X-RateLimit-Limit`: Maximum requests allowed
- `X-RateLimit-Remaining`: Requests remaining in window
- `X-RateLimit-Reset`: Seconds until window resets

Returns 429 with `Retry-After` header when limit exceeded. Periodic stale key cleanup prevents unbounded memory growth.

## CompressionMiddleware

Gzip compression for responses above a minimum size.

```python
from hyperdjango.standalone_middleware import CompressionMiddleware

app.use(
    CompressionMiddleware(
        min_size=500,
        level=6,
    )
)
```

| Parameter  | Type  | Default | Description                                |
| ---------- | ----- | ------- | ------------------------------------------ |
| `min_size` | `int` | `500`   | Minimum response size in bytes to compress |
| `level`    | `int` | `6`     | Gzip compression level (1=fast, 9=best)    |

Skips: streaming responses, already-compressed content, images/video/audio, responses with existing `Content-Encoding`.

## Middleware Stack

All middleware follows the async `(request, call_next)` protocol. The stack manages ordering:

```python
from hyperdjango.standalone_middleware import MiddlewareStack

stack = MiddlewareStack()
stack.add(SecurityHeadersMiddleware())
stack.add(CORSMiddleware(origins=["*"]))
stack.add(RateLimitMiddleware(max_requests=100, window=60))

handler = stack.wrap(my_handler)
```

Or use `app.use()` which manages the stack automatically:

```python
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware(origins=["*"]))
```

### Recommended Middleware Order

```python
# 1. Security headers (outermost -- applies to all responses including errors)
app.use(SecurityHeadersMiddleware(hsts=True))

# 2. CORS (must run before CSRF to handle preflight)
app.use(CORSMiddleware(origins=["https://app.example.com"]))

# 3. Rate limiting (reject abusive traffic early)
app.use(RateLimitMiddleware(max_requests=100, window=60))

# 4. CSRF (after rate limiting, before business logic)
app.use(CSRFMiddleware(secret="..."))

# 5. Compression (innermost -- compress the final response)
app.use(CompressionMiddleware())
```

## Security Checklist

A production security checklist for HyperDjango applications:

- [ ] `SecurityHeadersMiddleware` enabled with HSTS
- [ ] CSP configured to restrict script/style sources
- [ ] `CSRFMiddleware` enabled with a strong secret
- [ ] Session cookies set with `httponly=True`, `secure=True`, `samesite="Lax"`
- [ ] Rate limiting configured on login and sensitive endpoints
- [ ] `SecurityLog` recording authentication and authorization events
- [ ] All database queries use parameterized values (no string interpolation)
- [ ] Template auto-escaping is never globally disabled
- [ ] Host header validation in place
- [ ] HTTPS enforced (redirect HTTP to HTTPS)
- [ ] Sensitive data filtered from logs and error reports
- [ ] Security log cleanup scheduled (e.g., `cleanup(days=90)` via background task)
- [ ] Brute-force detection on login endpoint
- [ ] File uploads validated for type and size
- [ ] CORS restricted to specific origins (not `*` in production)

## SSRF-safe outbound fetches

Social/CMS features often fetch a **user-supplied** URL (link previews, avatar
import, webhooks). Do not fetch such URLs directly — a value like
`http://169.254.169.254/latest/meta-data/` (cloud metadata) or
`http://127.0.0.1:6379` reaches internal services. Use `hyperdjango.net`:

```python
from hyperdjango.net import safe_get, validate_public_url, UnsafeURLError

try:
    resp = await safe_get(
        user_supplied_url
    )  # validates, then GETs (no redirects, size-capped)
except UnsafeURLError:
    return Response.error(400, "URL not allowed")

# Or validate only, before your own fetch:
validate_public_url(
    url
)  # raises UnsafeURLError for internal/loopback/link-local/private targets
```

`validate_public_url` resolves the host and refuses if **any** resolved address
is loopback / private / link-local (incl. `169.254.169.254`) / reserved /
multicast / unspecified. `safe_get` additionally disables redirects (each hop is
a fresh SSRF surface) and caps the response size. For untrusted URLs at scale,
also pin the connection to the validated IP to defeat DNS rebinding.

## Data privacy

- **Logs never contain secrets.** The access log records only method, path,
  status, duration, `client_ip`, and `user_id` — never headers, cookies, request
  bodies, or tokens. No auth flow logs a password/token value. (Keep `DEBUG` off
  in production; the debug error page renders tracebacks — `hyper doctor` warns.)
- **Right to erasure.** Model your user-owned data with `Field(foreign_key=...,
on_delete="CASCADE")` so deleting a user cascades to their rows.
- **Uploaded-image EXIF/GPS.** User photos carry EXIF metadata (GPS location,
  device) that can deanonymize the uploader. Strip it before storing user images
  (a dedicated helper is planned; until then use Pillow to re-encode without
  metadata).
