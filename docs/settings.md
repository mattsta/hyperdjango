# Settings Reference

HyperDjango settings are configured via four sources, resolved in priority order (highest wins):

1. **`HYPERDJANGO_*` in Django `settings.py`** -- highest priority
2. **`HYPER_*` environment variables**
3. **`.env` file in project root** -- lowest priority
4. **Built-in defaults** -- fallback

All settings have sensible defaults. Only `SECRET_KEY` is required in production.

---

## Quick Start

```python
# settings.py (Django mode)
HYPERDJANGO_SECRET_KEY = "your-secret-key"
HYPERDJANGO_DEBUG = True
HYPERDJANGO_POOL_SIZE = 20
HYPERDJANGO_ALLOWED_HOSTS = ["example.com", "api.example.com"]
HYPERDJANGO_DATABASE_URL = "postgresql://user:pass@localhost/mydb"
```

```bash
# Or via environment variables
export HYPER_SECRET_KEY="your-secret-key"
export HYPER_DEBUG=true
export HYPER_POOL_SIZE=20
export HYPER_ALLOWED_HOSTS="example.com,api.example.com"
export HYPER_DATABASE_URL="postgresql://user:pass@localhost/mydb"
```

```bash
# Or via .env file in project root
SECRET_KEY=your-secret-key
DEBUG=true
POOL_SIZE=20
```

---

## Reading settings in code

`get_setting(name, default=None)` resolves a setting through the precedence
chain (Django override → `HYPER_<NAME>` env / `.env` → `DEFAULTS` → caller
default):

```python
from hyperdjango.conf import get_setting

pool_size = get_setting("POOL_SIZE")
```

### Requiring a setting (fail closed on defaults)

Security-critical settings — signing keys, credentials — must never run on an
auto-generated, empty, or too-weak default. `require_setting(name)` resolves
like `get_setting` but **raises `SettingNotConfigured`** unless the value is
safe to use — it rejects a value that was not explicitly provided (env /
`.env` / Django), one that is explicitly set but blank (`HYPER_X=""`), one
shorter than an optional `min_length`, or one a `validator` callback refuses.
An app that resolves it during import or startup then refuses to boot until it
is configured properly:

```python
from hyperdjango.conf import require_setting
from hyperdjango.signing import SigningKey


# Refuses to start unless HYPER_SESSION_SIGNING_KEY (or a Django/.env value)
# is set — never mints tokens with an ephemeral auto-generated key.
class TokenConfig:
    keys = [SigningKey(secret=require_setting("SESSION_SIGNING_KEY"))]
```

`is_explicitly_set(name)` is the underlying predicate — `True` only when the
value came from the environment or Django, not from the built-in default:

```python
from hyperdjango.conf import is_explicitly_set

if not is_explicitly_set("SESSION_SIGNING_KEY"):
    logger.warning("Signing key is ephemeral — set it before production")
```

This is the opt-in, per-setting complement to marking a `SettingDefinition`
`required=True` (enforced only by `validate_settings` / `hyper check`):
`require_setting` fails at the point of use, for any **registered** setting an
app depends on.

> **The name must be registered.** `is_explicitly_set` — and therefore
> `require_setting` — only sees settings present in `SETTING_DEFINITIONS` (and
> `DEFAULTS`): env loading iterates `SETTING_DEFINITIONS` and the Django scan
> iterates `DEFAULTS`, so an unregistered app-custom name is invisible to both.
> `require_setting("MY_APP_SECRET")` raises **even with `HYPER_MY_APP_SECRET`
> exported**. Register the setting first (add a `SettingDefinition` and a
> `DEFAULTS` entry) before requiring it.

---

## 1. Core

### SECRET_KEY

- **Type:** `str`
- **Default:** `""` (empty -- **required** in production)
- **Description:** Secret key used for session signing, CSRF tokens, and cryptographic signing. Must be set to a unique, unpredictable value in production. In debug mode, a random key is auto-generated with a warning.

```python
# settings.py
HYPERDJANGO_SECRET_KEY = "a-long-random-string-at-least-50-chars"
```

```bash
# Generate a secure key
python -c "import secrets; print(secrets.token_hex(32))"
```

- **Related:** `DEBUG`, `CSRF_COOKIE_*`, `SESSION_COOKIE_*`

---

### DEBUG

- **Type:** `bool`
- **Default:** `False`
- **Description:** Enable debug mode. When True, detailed error pages are shown, auto-generated SECRET_KEY is allowed, and extra logging is enabled. **Never enable in production.**

```python
# settings.py
HYPERDJANGO_DEBUG = True
```

```bash
# .env
HYPER_DEBUG=true
```

- **Related:** `SECRET_KEY`, `LOG_LEVEL`, `ALLOWED_HOSTS`

---

### ALLOWED_HOSTS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of hostnames/domains that the application is allowed to serve. When DEBUG is False and this list is empty, all requests will be rejected with a 400 error. Supports exact matches only (no wildcards).

```python
# settings.py
HYPERDJANGO_ALLOWED_HOSTS = ["example.com", "www.example.com", "api.example.com"]
```

```bash
# Environment (comma-separated)
HYPER_ALLOWED_HOSTS="example.com,www.example.com,api.example.com"
```

- **Related:** `DEBUG`, `SECURE_SSL_REDIRECT`

---

## 2. Database

### DATABASE_URL

- **Type:** `str`
- **Default:** `""`
- **Description:** PostgreSQL connection URL. HyperDjango uses pg.zig as its native PostgreSQL driver -- no other databases are supported.

A single resolver (`hyperdjango.conf.resolve_database_url()`) decides which database every component connects to, so you set **one** variable in whichever convention you prefer and the server, `get_db()`, the native driver, and every `hyper` CLI command (`setup`/`seed`/`migrate`/…) all agree. First non-empty wins:

1. `HyperApp(database=...)` constructor / Django `HYPERDJANGO_DATABASE_URL` — explicit override, beats every env var
2. `HYPER_DATABASE_URL` env var / `.env` — framework prefix
3. `DATABASE_URL` env var / `.env` — 12-factor / libpq URI
4. the libpq `PG*` set (`PGDATABASE` plus optional `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`)

`get_setting("DATABASE_URL")` returns this resolved value, so any one of the forms below is sufficient on its own — you never need to set several to the same value.

```python
# settings.py
HYPERDJANGO_DATABASE_URL = "postgresql://user:password@localhost:5432/mydb"
```

```bash
# .env or environment — any ONE of these
DATABASE_URL=postgresql://user:password@db.example.com:5432/mydb?sslmode=require
HYPER_DATABASE_URL=postgresql://user:password@localhost/mydb
PGDATABASE=mydb PGHOST=localhost PGUSER=myuser   # standard libpq PG* set
```

- **Related:** `POOL_SIZE`, `CONNECT_TIMEOUT`, `QUERY_TIMEOUT`

---

### POOL_SIZE

- **Type:** `int`
- **Default:** `0` (auto-tune: `THREAD_POOL_SIZE` + headroom + offload workers)
- **Min:** `0` | **Max:** `1024`
- **Description:** Connection pool size for pg.zig. Set to 0 for automatic sizing from THREAD_POOL_SIZE (the pool is never smaller than the worker-thread count, which pins connections). For most web applications, 10-30 connections is optimal. Never exceed your PostgreSQL `max_connections` setting.

```python
# settings.py -- typical web app
HYPERDJANGO_POOL_SIZE = 20

# High-throughput API server
HYPERDJANGO_POOL_SIZE = 50
```

- **Related:** `POOL_MAX_QUERIES`, `POOL_MAX_LIFETIME`, `DATABASE_URL`, `DB_OFFLOAD_WORKERS`

---

### DB_OFFLOAD_WORKERS

- **Type:** `int`
- **Default:** `0` (auto: `min(cpu_count, 8)`)
- **Min:** `0` | **Max:** `128`
- **Description:** Maximum number of blocking DB round-trips that can run concurrently _off_ a multiplexing event loop. When a query is awaited on a loop that drives many connections (the shared WebSocket pool, and later the HTTP reactor), the synchronous native round-trip is offloaded to this bounded executor so it can't stall the loop's other connections; on a thread-per-request loop (ordinary HTTP handlers, tests, scripts) queries run inline and never touch it. The workers are folded into the connection-pool budget, so raising this also raises the auto-tuned pool size — keep the total under PostgreSQL `max_connections`. A pure-HTTP app never offloads, so the executor is never created.

```python
# settings.py -- WebSocket-heavy app doing DB work in socket handlers
HYPERDJANGO_DB_OFFLOAD_WORKERS = 16
```

- **Related:** `POOL_SIZE`, `THREAD_POOL_SIZE`, `WEBSOCKET_CONCURRENCY`

---

### POOL_MAX_QUERIES

- **Type:** `int`
- **Default:** `10000`
- **Min:** `0`
- **Description:** Rotate connections after this many queries. Prevents long-lived connections from accumulating leaked resources (temp tables, prepared statements on the server, memory). Set to 0 to disable rotation.

```python
# settings.py
HYPERDJANGO_POOL_MAX_QUERIES = 50000  # longer-lived connections
```

- **Related:** `POOL_SIZE`, `POOL_MAX_LIFETIME`

---

### POOL_MAX_LIFETIME

- **Type:** `int`
- **Default:** `3600` (1 hour)
- **Min:** `0`
- **Description:** Maximum connection age in seconds. Connections older than this are closed and replaced. Prevents issues with stale connections after PostgreSQL restarts or network changes. Set to 0 to disable age-based rotation.

```python
# settings.py
HYPERDJANGO_POOL_MAX_LIFETIME = 1800  # 30 minutes for aggressive rotation
```

- **Related:** `POOL_SIZE`, `POOL_MAX_QUERIES`

---

### STATEMENT_CACHE_SIZE

- **Type:** `int`
- **Default:** `256`
- **Min:** `0` | **Max:** `65536`
- **Description:** **RESERVED — not currently honored.** Intended size of the
  per-connection prepared-statement cache, but the native pg pool ABI does not
  yet accept it, so setting this has no runtime effect. Kept for forward
  compatibility. See `PREPARED_STATEMENTS`.

```python
# settings.py
HYPERDJANGO_STATEMENT_CACHE_SIZE = 512  # larger cache for apps with many query shapes
```

- **Related:** `PREPARED_STATEMENTS`, `POOL_SIZE`

---

### CONNECT_TIMEOUT

- **Type:** `int`
- **Default:** `10000` (10 seconds)
- **Min:** `100` | **Max:** `300000`
- **Description:** TCP connect + authentication timeout in milliseconds. Uses non-blocking connect with poll for precise timeout control. Applies to initial connection establishment, not individual queries.

```python
# settings.py
HYPERDJANGO_CONNECT_TIMEOUT = 5000  # 5 seconds for local networks
HYPERDJANGO_CONNECT_TIMEOUT = 30000  # 30 seconds for cross-region connections
```

- **Related:** `QUERY_TIMEOUT`, `DATABASE_URL`

---

### QUERY_TIMEOUT

- **Type:** `int`
- **Default:** `0` (unlimited)
- **Min:** `0`
- **Description:** PostgreSQL `statement_timeout` in milliseconds, set via startup params. Queries exceeding this duration are cancelled by the server. Set to 0 to disable (no timeout). Useful for preventing runaway queries in production.

```python
# settings.py
HYPERDJANGO_QUERY_TIMEOUT = 30000  # 30 seconds -- kill long-running queries
```

- **Related:** `CONNECT_TIMEOUT`, `DATABASE_URL`

---

### PREPARED_STATEMENTS

- **Type:** `bool`
- **Default:** `True`
- **Description:** **RESERVED — not currently honored.** The native pg pool's
  configuration ABI does not yet accept a prepared-statement toggle, so setting
  this has no runtime effect. Kept for forward compatibility; a future release
  may wire it into the native pool.

```python
# settings.py
HYPERDJANGO_PREPARED_STATEMENTS = False  # disable for PgBouncer transaction mode
```

- **Related:** `STATEMENT_CACHE_SIZE`, `POOL_SIZE`

---

## 3. Security

### SECURE_SSL_REDIRECT

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, all HTTP requests are redirected to HTTPS. Enable in production when your site is served exclusively over HTTPS. Make sure to set `SECURE_REDIRECT_EXEMPT` for paths that must remain accessible over HTTP (e.g., health check endpoints behind a load balancer).

```python
# settings.py -- production
HYPERDJANGO_SECURE_SSL_REDIRECT = True
```

- **Related:** `SECURE_SSL_HOST`, `SECURE_REDIRECT_EXEMPT`, `SECURE_PROXY_SSL_HEADER`

---

### SECURE_HSTS_SECONDS

- **Type:** `int`
- **Default:** `0` (disabled)
- **Min:** `0`
- **Description:** Sets the `Strict-Transport-Security` max-age header in seconds. Once enabled, browsers will refuse non-HTTPS connections for the specified duration. Start with a small value (e.g., 3600) and increase to 31536000 (1 year) once verified.

```python
# settings.py -- production rollout
HYPERDJANGO_SECURE_HSTS_SECONDS = 3600  # 1 hour (testing)
HYPERDJANGO_SECURE_HSTS_SECONDS = 31536000  # 1 year (production)
```

- **Related:** `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD`, `SECURE_SSL_REDIRECT`

---

### SECURE_HSTS_INCLUDE_SUBDOMAINS

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, adds `includeSubDomains` to the HSTS header, enforcing HTTPS on all subdomains. Only enable when all subdomains support HTTPS.

```python
# settings.py
HYPERDJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS = True
```

- **Related:** `SECURE_HSTS_SECONDS`, `SECURE_HSTS_PRELOAD`

---

### SECURE_HSTS_PRELOAD

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, adds the `preload` directive to the HSTS header. This signals to browser vendors that the site should be included in the HSTS preload list (hardcoded HTTPS in browsers). Requires `SECURE_HSTS_SECONDS >= 31536000` and `SECURE_HSTS_INCLUDE_SUBDOMAINS = True`.

```python
# settings.py -- only after full HTTPS verification
HYPERDJANGO_SECURE_HSTS_PRELOAD = True
```

- **Related:** `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`

---

### SECURE_CONTENT_TYPE_NOSNIFF

- **Type:** `bool`
- **Default:** `True`
- **Description:** When True, sends the `X-Content-Type-Options: nosniff` header, preventing browsers from MIME-type sniffing. Should almost always be True.

```python
# settings.py
HYPERDJANGO_SECURE_CONTENT_TYPE_NOSNIFF = True  # default, keep enabled
```

- **Related:** `SECURE_REFERRER_POLICY`, `X_FRAME_OPTIONS`

---

### SECURE_REFERRER_POLICY

- **Type:** `str`
- **Default:** `"same-origin"`
- **Choices:** `no-referrer`, `no-referrer-when-downgrade`, `origin`, `origin-when-cross-origin`, `same-origin`, `strict-origin`, `strict-origin-when-cross-origin`, `unsafe-url`
- **Description:** Sets the `Referrer-Policy` header. Controls how much referrer information is included with requests. `same-origin` sends the full URL only for same-origin requests. `strict-origin-when-cross-origin` is a good alternative for sites with cross-origin links.

```python
# settings.py
HYPERDJANGO_SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
```

- **Related:** `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_CROSS_ORIGIN_OPENER_POLICY`

---

### SECURE_PROXY_SSL_HEADER

- **Type:** `str`
- **Default:** `""` (disabled)
- **Description:** Header name indicating HTTPS behind a reverse proxy. When set, the application trusts this header to determine if the request was made over HTTPS. Common value is `"X-Forwarded-Proto"`. Only set this if you control the proxy and it strips/sets this header.

```python
# settings.py -- behind nginx/ALB
HYPERDJANGO_SECURE_PROXY_SSL_HEADER = "X-Forwarded-Proto"
```

- **Related:** `SECURE_SSL_REDIRECT`, `USE_X_FORWARDED_HOST`, `USE_X_FORWARDED_PORT`

---

### SECURE_REDIRECT_EXEMPT

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of regex patterns for URL paths that are exempt from SSL redirect. Useful for health check endpoints that need to remain accessible over HTTP behind a load balancer.

```python
# settings.py
HYPERDJANGO_SECURE_REDIRECT_EXEMPT = [
    r"^healthz$",
    r"^readyz$",
    r"^\.well-known/",
]
```

- **Related:** `SECURE_SSL_REDIRECT`, `SECURE_SSL_HOST`

---

### SECURE_SSL_HOST

- **Type:** `str`
- **Default:** `""` (same host)
- **Description:** Host to redirect SSL requests to. When empty, redirects preserve the original host. Set this only if HTTPS is served on a different hostname than HTTP.

```python
# settings.py
HYPERDJANGO_SECURE_SSL_HOST = "secure.example.com"
```

- **Related:** `SECURE_SSL_REDIRECT`, `SECURE_REDIRECT_EXEMPT`

---

### SECURE_CROSS_ORIGIN_OPENER_POLICY

- **Type:** `str`
- **Default:** `"same-origin"`
- **Choices:** `same-origin`, `same-origin-allow-popups`, `unsafe-none`
- **Description:** Sets the `Cross-Origin-Opener-Policy` header. `same-origin` prevents cross-origin documents from opening/accessing this window. `same-origin-allow-popups` allows popups opened by this page. `unsafe-none` disables the protection.

```python
# settings.py -- if using OAuth popups
HYPERDJANGO_SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"
```

- **Related:** `SECURE_REFERRER_POLICY`, `SECURE_CSP`

---

### SECURE_CSP

- **Type:** `dict`
- **Default:** `{}` (no CSP header)
- **Description:** Content Security Policy directives as a dictionary. When non-empty, a `Content-Security-Policy` header is generated from these directives. Keys are CSP directive names, values are the directive values.

```python
# settings.py
HYPERDJANGO_SECURE_CSP = {
    "default-src": "'self'",
    "script-src": "'self' 'unsafe-inline' https://cdn.example.com",
    "style-src": "'self' 'unsafe-inline'",
    "img-src": "'self' data: https:",
    "font-src": "'self' https://fonts.gstatic.com",
    "connect-src": "'self' https://api.example.com",
}
```

- **Related:** `SECURE_CONTENT_TYPE_NOSNIFF`, `X_FRAME_OPTIONS`

---

### X_FRAME_OPTIONS

- **Type:** `str`
- **Default:** `"DENY"`
- **Choices:** `DENY`, `SAMEORIGIN`, `""`
- **Description:** Sets the `X-Frame-Options` header. `DENY` prevents all framing. `SAMEORIGIN` allows framing by the same origin. Empty string disables the header (use CSP `frame-ancestors` instead for modern browsers).

```python
# settings.py -- allow same-origin iframes
HYPERDJANGO_X_FRAME_OPTIONS = "SAMEORIGIN"
```

- **Related:** `SECURE_CSP`, `SECURE_CONTENT_TYPE_NOSNIFF`

---

## 4. CSRF

### CSRF_COOKIE_SECURE

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, the CSRF cookie is only sent over HTTPS. Enable in production when serving exclusively over HTTPS.

```python
# settings.py -- production
HYPERDJANGO_CSRF_COOKIE_SECURE = True
```

- **Related:** `CSRF_COOKIE_HTTPONLY`, `CSRF_COOKIE_SAMESITE`, `SESSION_COOKIE_SECURE`

---

### CSRF_COOKIE_HTTPONLY

- **Type:** `bool`
- **Default:** `True`
- **Description:** When True, the CSRF cookie is not accessible via JavaScript (`document.cookie`). The CSRF token is instead submitted via a hidden form field or the `X-CSRFToken` header.

```python
# settings.py
HYPERDJANGO_CSRF_COOKIE_HTTPONLY = True  # default, keep enabled
```

- **Related:** `CSRF_COOKIE_SECURE`, `CSRF_HEADER_NAME`

---

### CSRF_COOKIE_SAMESITE

- **Type:** `str`
- **Default:** `"Lax"`
- **Choices:** `Strict`, `Lax`, `None`
- **Description:** SameSite attribute for the CSRF cookie. `Lax` sends the cookie on top-level navigations and same-site requests. `Strict` only sends on same-site requests. `None` sends on all requests (requires `CSRF_COOKIE_SECURE = True`).

```python
# settings.py
HYPERDJANGO_CSRF_COOKIE_SAMESITE = "Strict"  # maximum protection
```

- **Related:** `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`

---

### CSRF_TRUSTED_ORIGINS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of trusted origins for CSRF validation. Requests from these origins are allowed to make cross-origin POST/PUT/PATCH/DELETE requests. Include the scheme (https://).

```python
# settings.py
HYPERDJANGO_CSRF_TRUSTED_ORIGINS = [
    "https://admin.example.com",
    "https://app.example.com",
]
```

- **Related:** `CSRF_COOKIE_SECURE`, `CSRF_HEADER_NAME`

---

### CSRF_COOKIE_DOMAIN

- **Type:** `str`
- **Default:** `""` (current domain only)
- **Description:** Domain for the CSRF cookie. When empty, the cookie is scoped to the current domain. Set to a parent domain (e.g., `.example.com`) to share the CSRF cookie across subdomains.

```python
# settings.py -- share across subdomains
HYPERDJANGO_CSRF_COOKIE_DOMAIN = ".example.com"
```

- **Related:** `CSRF_COOKIE_PATH`, `SESSION_COOKIE_DOMAIN`

---

### CSRF_COOKIE_NAME

- **Type:** `str`
- **Default:** `"csrftoken"`
- **Description:** Name of the CSRF cookie. Change if you need to avoid conflicts with other applications running on the same domain.

```python
# settings.py
HYPERDJANGO_CSRF_COOKIE_NAME = "myapp_csrf"
```

- **Related:** `CSRF_HEADER_NAME`, `CSRF_COOKIE_PATH`

---

### CSRF_COOKIE_PATH

- **Type:** `str`
- **Default:** `"/"`
- **Description:** URL path for the CSRF cookie. The cookie is only sent for requests matching this path prefix.

```python
# settings.py
HYPERDJANGO_CSRF_COOKIE_PATH = "/"  # default, available site-wide
```

- **Related:** `CSRF_COOKIE_DOMAIN`, `CSRF_COOKIE_NAME`

---

### CSRF_COOKIE_AGE

- **Type:** `int`
- **Default:** `31449600` (approximately 1 year)
- **Min:** `0`
- **Description:** Maximum age of the CSRF cookie in seconds. The cookie is renewed on each request that includes it.

```python
# settings.py
HYPERDJANGO_CSRF_COOKIE_AGE = 86400  # 1 day -- shorter for higher security
```

- **Related:** `CSRF_COOKIE_NAME`, `SESSION_COOKIE_AGE`

---

### CSRF_HEADER_NAME

- **Type:** `str`
- **Default:** `"X-CSRFToken"`
- **Description:** HTTP header name for submitting the CSRF token. JavaScript clients send the token via this header instead of a form field.

```python
# settings.py
HYPERDJANGO_CSRF_HEADER_NAME = "X-CSRFToken"  # default
```

```javascript
// JavaScript client
fetch("/api/data", {
  method: "POST",
  headers: {
    "X-CSRFToken": getCookie("csrftoken"),
    "Content-Type": "application/json",
  },
  body: JSON.stringify(data),
});
```

- **Related:** `CSRF_COOKIE_NAME`, `CSRF_COOKIE_HTTPONLY`

---

### CSRF_SECRET

- **Type:** `str`
- **Default:** a random per-process value (`secrets.token_urlsafe(32)`)
- **Description:** Signing secret used by CSRFMiddleware to generate and verify CSRF tokens. When left unset it is auto-generated as a fresh random value each time the process starts — usable for local development, but it does **not** persist across restarts and is not shared across processes, so every restart/instance invalidates existing tokens. **Must be set explicitly in production** via `HYPER_CSRF_SECRET` (or `HYPERDJANGO_CSRF_SECRET` in Django settings).

```python
# settings.py (production)
HYPERDJANGO_CSRF_SECRET = "a-long-random-secret-for-csrf"
```

```bash
# Environment (recommended for production)
export HYPER_CSRF_SECRET="a-long-random-secret-for-csrf"
```

- **Related:** `CSRF_COOKIE_NAME`, `SECRET_KEY`, `SESSION_SECRET`

---

## 5. Sessions

### SESSION_COOKIE_AGE

- **Type:** `int`
- **Default:** `1209600` (2 weeks)
- **Min:** `0`
- **Description:** Session cookie maximum age in seconds. After this duration, the session expires and the user must re-authenticate.

```python
# settings.py
HYPERDJANGO_SESSION_COOKIE_AGE = 86400  # 1 day
HYPERDJANGO_SESSION_COOKIE_AGE = 604800  # 1 week
HYPERDJANGO_SESSION_COOKIE_AGE = 2592000  # 30 days
```

- **Related:** `SESSION_EXPIRE_AT_BROWSER_CLOSE`, `SESSION_COOKIE_SECURE`

---

### SESSION_COOKIE_SECURE

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, the session cookie is only sent over HTTPS. Enable in production.

```python
# settings.py -- production
HYPERDJANGO_SESSION_COOKIE_SECURE = True
```

- **Related:** `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE`, `CSRF_COOKIE_SECURE`

---

### SESSION_COOKIE_HTTPONLY

- **Type:** `bool`
- **Default:** `True`
- **Description:** When True, the session cookie is not accessible via JavaScript. Should always be True to prevent XSS attacks from stealing session tokens.

```python
# settings.py
HYPERDJANGO_SESSION_COOKIE_HTTPONLY = True  # default, never disable
```

- **Related:** `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`

---

### SESSION_COOKIE_SAMESITE

- **Type:** `str`
- **Default:** `"Lax"`
- **Choices:** `Strict`, `Lax`, `None`
- **Description:** SameSite attribute for the session cookie. Same semantics as CSRF_COOKIE_SAMESITE.

```python
# settings.py
HYPERDJANGO_SESSION_COOKIE_SAMESITE = "Lax"  # default
```

- **Related:** `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SAMESITE`

---

### SESSION_COOKIE_NAME

- **Type:** `str`
- **Default:** `"sessionid"`
- **Description:** Name of the session cookie. Change to avoid conflicts with other applications on the same domain.

```python
# settings.py
HYPERDJANGO_SESSION_COOKIE_NAME = "myapp_session"
```

- **Related:** `SESSION_COOKIE_DOMAIN`, `SESSION_COOKIE_PATH`

---

### SESSION_COOKIE_DOMAIN

- **Type:** `str`
- **Default:** `""` (current domain only)
- **Description:** Domain for the session cookie. Set to a parent domain to share sessions across subdomains.

```python
# settings.py -- share sessions across subdomains
HYPERDJANGO_SESSION_COOKIE_DOMAIN = ".example.com"
```

- **Related:** `SESSION_COOKIE_NAME`, `CSRF_COOKIE_DOMAIN`

---

### SESSION_COOKIE_PATH

- **Type:** `str`
- **Default:** `"/"`
- **Description:** URL path for the session cookie.

```python
# settings.py
HYPERDJANGO_SESSION_COOKIE_PATH = "/"  # default
```

- **Related:** `SESSION_COOKIE_DOMAIN`, `SESSION_COOKIE_NAME`

---

### SESSION_EXPIRE_AT_BROWSER_CLOSE

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, the session cookie is a "session cookie" that expires when the browser is closed. Overrides `SESSION_COOKIE_AGE` behavior.

```python
# settings.py -- banking/sensitive applications
HYPERDJANGO_SESSION_EXPIRE_AT_BROWSER_CLOSE = True
```

- **Related:** `SESSION_COOKIE_AGE`, `SESSION_SAVE_EVERY_REQUEST`

---

### SESSION_SAVE_EVERY_REQUEST

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, the session is saved to the database on every request, not just when the session data has been modified. This keeps session expiry sliding forward with each request. Increases database writes but ensures active sessions stay alive.

```python
# settings.py
HYPERDJANGO_SESSION_SAVE_EVERY_REQUEST = True
```

- **Related:** `SESSION_COOKIE_AGE`, `SESSION_EXPIRE_AT_BROWSER_CLOSE`

---

### SESSION_SECRET

- **Type:** `str`
- **Default:** a random per-process value (`secrets.token_urlsafe(32)`)
- **Description:** Secret used by SessionAuth for session cookie signing. When left unset it is auto-generated as a fresh random value each time the process starts — fine for local development, but it does **not** persist across restarts and is not shared across processes, so every restart/instance invalidates existing sessions. **Must be set explicitly in production** via `HYPER_SESSION_SECRET` (or `HYPERDJANGO_SESSION_SECRET` in Django settings).

```bash
# Environment (recommended for production)
export HYPER_SESSION_SECRET="a-long-random-secret-for-sessions"
```

- **Related:** `SESSION_SIGNING_KEY`, `SESSION_COOKIE_AGE`, `SECRET_KEY`

---

### SESSION_SIGNING_KEY

- **Type:** `str`
- **Default:** a random per-process value (`secrets.token_urlsafe(32)`)
- **Description:** Key used by TokenEngine for signed session cookies with key rotation support. When left unset it is auto-generated as a fresh random value each time the process starts — fine for local development, but it does **not** persist across restarts and is not shared across processes. **Must be set explicitly in production** via `HYPER_SESSION_SIGNING_KEY` (or `HYPERDJANGO_SESSION_SIGNING_KEY` in Django settings).

```bash
# Environment (recommended for production)
export HYPER_SESSION_SIGNING_KEY="a-long-random-key-for-token-signing"
```

- **Related:** `SESSION_SECRET`, `SESSION_COOKIE_AGE`

---

## 6. Authentication

### LOGIN_URL

- **Type:** `str`
- **Default:** `"/login/"`
- **Description:** URL to redirect unauthenticated users to when they access a view that requires authentication.

```python
# settings.py
HYPERDJANGO_LOGIN_URL = "/accounts/login/"
```

- **Related:** `LOGIN_REDIRECT_URL`, `LOGOUT_REDIRECT_URL`

---

### LOGIN_REDIRECT_URL

- **Type:** `str`
- **Default:** `"/"`
- **Description:** URL to redirect to after a successful login, when no `next` parameter is provided.

```python
# settings.py
HYPERDJANGO_LOGIN_REDIRECT_URL = "/dashboard/"
```

- **Related:** `LOGIN_URL`, `LOGOUT_REDIRECT_URL`

---

### LOGOUT_REDIRECT_URL

- **Type:** `str`
- **Default:** `"/"`
- **Description:** URL to redirect to after logout.

```python
# settings.py
HYPERDJANGO_LOGOUT_REDIRECT_URL = "/goodbye/"
```

- **Related:** `LOGIN_URL`, `LOGIN_REDIRECT_URL`

---

### PASSWORD_RESET_TIMEOUT

- **Type:** `int`
- **Default:** `259200` (3 days)
- **Min:** `1`
- **Description:** Duration in seconds that a password reset token remains valid. After this time, the token expires and the user must request a new one.

```python
# settings.py
HYPERDJANGO_PASSWORD_RESET_TIMEOUT = 86400  # 1 day -- more secure
```

- **Related:** `PASSWORD_MIN_LENGTH`, `PASSWORD_HASHER`

---

### PASSWORD_MIN_LENGTH

- **Type:** `int`
- **Default:** `8`
- **Min:** `1` | **Max:** `128`
- **Description:** Minimum password length enforced during user registration and password changes.

```python
# settings.py
HYPERDJANGO_PASSWORD_MIN_LENGTH = 12  # stricter for enterprise
```

- **Related:** `PASSWORD_HASHER`, `AUTH_PASSWORD_VALIDATORS`

---

### PASSWORD_HASHER

- **Type:** `str`
- **Default:** `"argon2id"`
- **Choices:** `argon2id`
- **Description:** Password hashing algorithm. Only `argon2id` is supported -- it is the current best practice for password hashing (winner of the Password Hashing Competition). No weaker algorithms (bcrypt, pbkdf2, scrypt) are available.

```python
# settings.py
HYPERDJANGO_PASSWORD_HASHER = "argon2id"  # only valid option
```

- **Related:** `PASSWORD_MIN_LENGTH`, `AUTH_PASSWORD_VALIDATORS`

---

### AUTH_PASSWORD_VALIDATORS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of password validator class paths. Each validator is called during password changes and registration to enforce password strength rules.

```python
# settings.py
HYPERDJANGO_AUTH_PASSWORD_VALIDATORS = [
    "hyperdjango.auth.validators.MinLengthValidator",
    "hyperdjango.auth.validators.NumericPasswordValidator",
    "hyperdjango.auth.validators.CommonPasswordValidator",
    "hyperdjango.auth.validators.UserAttributeSimilarityValidator",
]
```

- **Related:** `PASSWORD_MIN_LENGTH`, `PASSWORD_HASHER`

---

### ADMIN_SECRET

- **Type:** `str`
- **Default:** a random per-process value (`secrets.token_urlsafe(32)`)
- **Description:** Secret used by HyperAdmin for the admin panel login and session management. When left unset it is auto-generated as a fresh random value each time the process starts — fine for local development, but it does **not** persist across restarts and is not shared across processes. **Must be set explicitly in production** via `HYPER_ADMIN_SECRET` (or `HYPERDJANGO_ADMIN_SECRET` in Django settings).

```bash
# Environment (recommended for production)
export HYPER_ADMIN_SECRET="a-long-random-secret-for-admin"
```

- **Related:** `SESSION_SECRET`, `SECRET_KEY`

---

### API_KEY

- **Type:** `str`
- **Default:** a random per-process value (`secrets.token_urlsafe(32)`)
- **Description:** Default API key for development, used as the seed API key in services. When left unset it is auto-generated as a fresh random value each time the process starts — fine for local development, but it does **not** persist across restarts and is not shared across processes. **Must be set explicitly in production** with a properly generated key via `HYPER_API_KEY` (or `HYPERDJANGO_API_KEY` in Django settings).

```bash
# Environment (recommended for production)
export HYPER_API_KEY="sk_live_your-production-api-key"
```

- **Related:** `ADMIN_SECRET`, `SECRET_KEY`

---

### SEED_PASSWORD

- **Type:** `str`
- **Default:** `""` (empty — generates random password and prints to stdout)
- **Description:** Global seed password for service seed files. When set, all calls to `seed_password(username)` return this value instead of generating a random password. Useful for CI/CD and E2E testing where you need predictable credentials.

```bash
# Environment — all seed users get this password
export HYPER_SEED_PASSWORD="my-test-password"
```

**Resolution order** (first non-empty wins):

1. `HYPER_SEED_PASSWORD_<USERNAME>` — per-user override (e.g., `HYPER_SEED_PASSWORD_DEMO`)
2. `HYPER_SEED_PASSWORD` — global override
3. Random — `secrets.token_urlsafe(16)`, printed to stdout

```python
from hyperdjango.auth.seed_utils import seed_password

password = seed_password("demo")  # resolves via settings, generates random if unset
```

- **Related:** `ADMIN_PASSWORD`

---

### ADMIN_PASSWORD

- **Type:** `str`
- **Default:** `""` (empty — generates random password and prints to stdout)
- **Description:** Password for the admin panel user created by `ensure_admin_user()`. When set, the admin user gets this password. When empty, a random password is generated and printed to stdout on first setup.

```bash
# Environment — admin panel login password
export HYPER_ADMIN_PASSWORD="my-admin-password"
```

```python
from hyperdjango.auth.permissions import PermissionChecker

checker = PermissionChecker(db)
admin = await checker.ensure_admin_user()  # resolves ADMIN_PASSWORD via get_setting()
```

The admin user is automatically assigned to the "staff" and "superuser" RBAC groups. This is idempotent — calling `ensure_admin_user()` multiple times is safe.

- **Related:** `SEED_PASSWORD`, `ADMIN_SECRET`

---

## 7. Email

### EMAIL_BACKEND

- **Type:** `str`
- **Default:** `"smtp"`
- **Choices:** `smtp`, `console`, `memory`
- **Description:** Email sending backend. `smtp` sends real emails via SMTP. `console` prints emails to stdout (development). `memory` stores emails in memory for testing.

```python
# settings.py -- development
HYPERDJANGO_EMAIL_BACKEND = "console"

# settings.py -- testing
HYPERDJANGO_EMAIL_BACKEND = "memory"

# settings.py -- production
HYPERDJANGO_EMAIL_BACKEND = "smtp"
```

- **Related:** `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`

---

### EMAIL_HOST

- **Type:** `str`
- **Default:** `"localhost"`
- **Description:** SMTP server hostname.

```python
# settings.py
HYPERDJANGO_EMAIL_HOST = "smtp.sendgrid.net"
HYPERDJANGO_EMAIL_HOST = "smtp.gmail.com"
HYPERDJANGO_EMAIL_HOST = "email-smtp.us-east-1.amazonaws.com"  # AWS SES
```

- **Related:** `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`

---

### EMAIL_PORT

- **Type:** `int`
- **Default:** `25`
- **Min:** `1` | **Max:** `65535`
- **Description:** SMTP server port. Common values: 25 (unencrypted), 465 (implicit TLS), 587 (STARTTLS).

```python
# settings.py
HYPERDJANGO_EMAIL_PORT = 587  # STARTTLS (most common for modern providers)
HYPERDJANGO_EMAIL_PORT = 465  # implicit TLS/SSL
```

- **Related:** `EMAIL_HOST`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL`

---

### EMAIL_HOST_USER

- **Type:** `str`
- **Default:** `""`
- **Description:** Username for SMTP authentication. Leave empty if your SMTP server does not require authentication.

```python
# settings.py
HYPERDJANGO_EMAIL_HOST_USER = "apikey"  # SendGrid
```

```bash
# .env -- keep credentials out of code
HYPER_EMAIL_HOST_USER=apikey
```

- **Related:** `EMAIL_HOST_PASSWORD`, `EMAIL_HOST`

---

### EMAIL_HOST_PASSWORD

- **Type:** `str`
- **Default:** `""`
- **Description:** Password for SMTP authentication. **Never commit this to version control.** Use environment variables or `.env` file.

```bash
# .env
HYPER_EMAIL_HOST_PASSWORD=SG.xxxxxxxxxxxxxxxxxxxxx
```

- **Related:** `EMAIL_HOST_USER`, `EMAIL_HOST`

---

### EMAIL_USE_TLS

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, use STARTTLS for the SMTP connection. The connection starts unencrypted and upgrades to TLS. Typically used with port 587. Mutually exclusive with `EMAIL_USE_SSL`.

```python
# settings.py
HYPERDJANGO_EMAIL_USE_TLS = True
HYPERDJANGO_EMAIL_PORT = 587
```

- **Related:** `EMAIL_USE_SSL`, `EMAIL_PORT`

---

### EMAIL_USE_SSL

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, use implicit TLS/SSL for the SMTP connection. The connection is encrypted from the start. Typically used with port 465. Mutually exclusive with `EMAIL_USE_TLS`.

```python
# settings.py
HYPERDJANGO_EMAIL_USE_SSL = True
HYPERDJANGO_EMAIL_PORT = 465
```

- **Related:** `EMAIL_USE_TLS`, `EMAIL_PORT`, `EMAIL_SSL_CERTFILE`

---

### EMAIL_SUBJECT_PREFIX

- **Type:** `str`
- **Default:** `"[HyperDjango] "`
- **Description:** String prepended to the subject of all outgoing emails sent by the framework (error notifications, etc.).

```python
# settings.py
HYPERDJANGO_EMAIL_SUBJECT_PREFIX = "[MyApp] "
```

- **Related:** `DEFAULT_FROM_EMAIL`, `SERVER_EMAIL`

---

### EMAIL_TIMEOUT

- **Type:** `int`
- **Default:** `30` (seconds)
- **Min:** `1` | **Max:** `300`
- **Description:** Timeout in seconds for SMTP connection establishment and operations.

```python
# settings.py
HYPERDJANGO_EMAIL_TIMEOUT = 10  # aggressive timeout for fast-failing
```

- **Related:** `EMAIL_HOST`, `EMAIL_BACKEND`

---

### EMAIL_SSL_CERTFILE

- **Type:** `str`
- **Default:** `""`
- **Description:** Path to a PEM-formatted SSL certificate file for client authentication with the SMTP server. Most SMTP providers do not require this.

```python
# settings.py
HYPERDJANGO_EMAIL_SSL_CERTFILE = "/etc/ssl/certs/smtp-client.pem"
```

- **Related:** `EMAIL_SSL_KEYFILE`, `EMAIL_USE_SSL`

---

### EMAIL_SSL_KEYFILE

- **Type:** `str`
- **Default:** `""`
- **Description:** Path to a PEM-formatted SSL private key file for client authentication with the SMTP server.

```python
# settings.py
HYPERDJANGO_EMAIL_SSL_KEYFILE = "/etc/ssl/private/smtp-client-key.pem"
```

- **Related:** `EMAIL_SSL_CERTFILE`, `EMAIL_USE_SSL`

---

### DEFAULT_FROM_EMAIL

- **Type:** `str`
- **Default:** `"webmaster@localhost"`
- **Description:** Default `From:` email address for outgoing emails sent to users (password resets, notifications, etc.).

```python
# settings.py
HYPERDJANGO_DEFAULT_FROM_EMAIL = "noreply@example.com"
HYPERDJANGO_DEFAULT_FROM_EMAIL = "My App <noreply@example.com>"
```

- **Related:** `SERVER_EMAIL`, `EMAIL_SUBJECT_PREFIX`

---

### SERVER_EMAIL

- **Type:** `str`
- **Default:** `"root@localhost"`
- **Description:** Email address used as the `From:` address for error notification emails sent to `ADMINS` and `MANAGERS`.

```python
# settings.py
HYPERDJANGO_SERVER_EMAIL = "errors@example.com"
```

- **Related:** `DEFAULT_FROM_EMAIL`, `ADMINS`, `MANAGERS`

---

## 8. Cache

### CACHE_BACKEND

- **Type:** `str`
- **Default:** `"memory"`
- **Choices:** `memory`, `database`
- **Description:** Cache backend type. `memory` uses an in-process LRU cache (fast, not shared across processes). `database` uses a PostgreSQL UNLOGGED table (shared across processes, survives restarts). PostgreSQL UNLOGGED tables provide fast shared caching for most workloads.

```python
# settings.py -- single process
HYPERDJANGO_CACHE_BACKEND = "memory"

# settings.py -- multi-process deployment
HYPERDJANGO_CACHE_BACKEND = "database"
```

- **Related:** `CACHE_TTL`, `CACHE_MAX_BYTES`, `CACHE_KEY_PREFIX`

---

### CACHE_TTL

- **Type:** `int`
- **Default:** `300` (5 minutes)
- **Min:** `0`
- **Description:** Default time-to-live for cache entries in seconds. Individual cache operations can override this value.

```python
# settings.py
HYPERDJANGO_CACHE_TTL = 60  # 1 minute for frequently changing data
HYPERDJANGO_CACHE_TTL = 3600  # 1 hour for relatively static data
```

- **Related:** `CACHE_BACKEND`, `CACHE_MAX_BYTES`

---

### CACHE_MAX_BYTES

- **Type:** `int`
- **Default:** `268435456` (256 MB)
- **Min:** `0`
- **Description:** Maximum total cache size in bytes. When the cache exceeds this size, least-recently-used entries are evicted.

```python
# settings.py
HYPERDJANGO_CACHE_MAX_BYTES = 512 * 1024 * 1024  # 512 MB
HYPERDJANGO_CACHE_MAX_BYTES = 64 * 1024 * 1024  # 64 MB for memory-constrained envs
```

- **Related:** `CACHE_BACKEND`, `CACHE_TTL`

---

### CACHE_KEY_PREFIX

- **Type:** `str`
- **Default:** `""`
- **Description:** Prefix prepended to all cache keys. Useful for namespacing when multiple applications share the same database cache table.

```python
# settings.py
HYPERDJANGO_CACHE_KEY_PREFIX = "myapp"
```

- **Related:** `CACHE_VERSION`, `CACHE_BACKEND`

---

### CACHE_VERSION

- **Type:** `int`
- **Default:** `1`
- **Min:** `1`
- **Description:** Default cache version number. Increment to invalidate all cache entries after a deployment that changes cached data structures.

```python
# settings.py
HYPERDJANGO_CACHE_VERSION = 2  # bump after schema change
```

- **Related:** `CACHE_KEY_PREFIX`, `CACHE_BACKEND`

---

## 9. Static Files and Media

### STATIC_URL

- **Type:** `str`
- **Default:** `"/static/"`
- **Description:** URL prefix for serving static files (CSS, JavaScript, images).

```python
# settings.py
HYPERDJANGO_STATIC_URL = "/static/"
HYPERDJANGO_STATIC_URL = "https://cdn.example.com/static/"  # CDN
```

- **Related:** `STATIC_ROOT`, `STATIC_MAX_AGE`, `STATICFILES_DIRS`

---

### STATIC_ROOT

- **Type:** `str`
- **Default:** `""`
- **Description:** Absolute filesystem path where `collectstatic` gathers all static files for deployment. Typically set to a directory served by nginx or a CDN.

```python
# settings.py
HYPERDJANGO_STATIC_ROOT = "/var/www/myapp/static/"
```

- **Related:** `STATIC_URL`, `STATICFILES_DIRS`

---

### STATIC_MAX_AGE

- **Type:** `int`
- **Default:** `3600` (1 hour)
- **Min:** `0`
- **Description:** `Cache-Control: max-age` value in seconds for static file responses. Hashed/immutable files can use much longer values.

```python
# settings.py
HYPERDJANGO_STATIC_MAX_AGE = 86400  # 1 day
HYPERDJANGO_STATIC_MAX_AGE = 31536000  # 1 year (if using content hashing)
```

- **Related:** `STATIC_URL`, `STATIC_CACHE`

---

### STATICFILES_DIRS

- **Type:** `list`
- **Default:** `[]`
- **Description:** Additional directories (beyond each app's `static/` subdirectory) for the static file finder to search.

```python
# settings.py
HYPERDJANGO_STATICFILES_DIRS = [
    "/var/www/myapp/assets/",
    "/opt/shared/static/",
]
```

- **Related:** `STATIC_URL`, `STATIC_ROOT`

---

### MEDIA_URL

- **Type:** `str`
- **Default:** `"/media/"`
- **Description:** URL prefix for serving user-uploaded media files.

```python
# settings.py
HYPERDJANGO_MEDIA_URL = "/media/"
HYPERDJANGO_MEDIA_URL = "https://media.example.com/"  # CDN
```

- **Related:** `MEDIA_ROOT`, `MAX_UPLOAD_SIZE`

---

### MEDIA_ROOT

- **Type:** `str`
- **Default:** `""`
- **Description:** Absolute filesystem path for storing user-uploaded files. This directory must be writable by the application process.

```python
# settings.py
HYPERDJANGO_MEDIA_ROOT = "/var/www/myapp/media/"
```

- **Related:** `MEDIA_URL`, `MAX_UPLOAD_SIZE`, `FILE_UPLOAD_PERMISSIONS`

---

## 10. File Upload

### MAX_UPLOAD_SIZE

- **Type:** `int`
- **Default:** `10485760` (10 MB)
- **Min:** `0`
- **Description:** Maximum allowed upload file size in bytes. Files exceeding this limit are rejected with a 413 error.

```python
# settings.py
HYPERDJANGO_MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
HYPERDJANGO_MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB for video uploads
```

- **Related:** `ALLOWED_UPLOAD_EXTENSIONS`, `MAX_BODY_SIZE`

---

### ALLOWED_UPLOAD_EXTENSIONS

- **Type:** `list`
- **Default:** `[]` (all extensions allowed)
- **Description:** List of allowed file extensions for uploads. When empty, all file types are accepted. Extensions should include the leading dot.

```python
# settings.py
HYPERDJANGO_ALLOWED_UPLOAD_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".pdf"]
```

- **Related:** `MAX_UPLOAD_SIZE`, `FILE_UPLOAD_PERMISSIONS`

---

### DATA_UPLOAD_MAX_NUMBER_FIELDS

- **Type:** `int`
- **Default:** `1000`
- **Min:** `1`
- **Description:** Maximum number of form fields (including file fields) that can be submitted in a single request. Protects against denial-of-service attacks via excessive form fields.

```python
# settings.py
HYPERDJANGO_DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000  # large forms
```

- **Related:** `DATA_UPLOAD_MAX_NUMBER_FILES`, `MAX_BODY_SIZE`

---

### DATA_UPLOAD_MAX_NUMBER_FILES

- **Type:** `int`
- **Default:** `100`
- **Min:** `1`
- **Description:** Maximum number of files that can be uploaded in a single request.

```python
# settings.py
HYPERDJANGO_DATA_UPLOAD_MAX_NUMBER_FILES = 20  # batch image upload
```

- **Related:** `DATA_UPLOAD_MAX_NUMBER_FIELDS`, `MAX_UPLOAD_SIZE`

---

### FILE_UPLOAD_TEMP_DIR

- **Type:** `str`
- **Default:** `""` (system temp directory)
- **Description:** Directory for temporary files during upload processing. When empty, uses the system default temp directory.

```python
# settings.py
HYPERDJANGO_FILE_UPLOAD_TEMP_DIR = "/var/tmp/myapp/uploads/"
```

- **Related:** `MAX_UPLOAD_SIZE`, `FILE_UPLOAD_PERMISSIONS`

---

### FILE_UPLOAD_PERMISSIONS

- **Type:** `int`
- **Default:** `0o644` (owner read/write, group/other read)
- **Min:** `0`
- **Description:** Unix file permissions applied to uploaded files (octal). Standard value is 0o644 for web-readable files.

```python
# settings.py
HYPERDJANGO_FILE_UPLOAD_PERMISSIONS = 0o644  # default
HYPERDJANGO_FILE_UPLOAD_PERMISSIONS = 0o640  # more restrictive
```

- **Related:** `FILE_UPLOAD_DIRECTORY_PERMISSIONS`, `MEDIA_ROOT`

---

### FILE_UPLOAD_DIRECTORY_PERMISSIONS

- **Type:** `int`
- **Default:** `0o755` (owner full, group/other read+execute)
- **Min:** `0`
- **Description:** Unix directory permissions applied to upload directories created by the application.

```python
# settings.py
HYPERDJANGO_FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o755  # default
HYPERDJANGO_FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o750  # more restrictive
```

- **Related:** `FILE_UPLOAD_PERMISSIONS`, `MEDIA_ROOT`

---

### FILE_UPLOAD_MAX_MEMORY_SIZE

- **Type:** `int`
- **Default:** `2621440` (2.5 MB)
- **Min:** `0`
- **Description:** Per-file size threshold (bytes): files smaller stay in memory, larger spill to disk.

```python
# settings.py
HYPERDJANGO_FILE_UPLOAD_MAX_MEMORY_SIZE = 2621440  # 2.5 MB (default)
HYPERDJANGO_FILE_UPLOAD_MAX_MEMORY_SIZE = (
    10485760  # 10 MB — keep larger files in memory
)
```

- **Related:** `FILE_UPLOAD_MAX_SIZE`, `FILE_UPLOAD_TEMP_DIR`

---

## 11. Server

### HTTP_SERVER

- **Type:** `str`
- **Default:** `"auto"`
- **Choices:** `auto`, `zig`, `asgi`
- **Description:** HTTP server backend. `auto` selects the native Zig server when available. `zig` forces the native Zig HTTP server (capacity-scaled worker pool, radix trie router, 2.1-2.3x faster than uvicorn). `asgi` uses the ASGI interface for compatibility with third-party ASGI servers.

```python
# settings.py
HYPERDJANGO_HTTP_SERVER = "zig"  # force native server
HYPERDJANGO_HTTP_SERVER = "asgi"  # use with external ASGI server
```

- **Related:** `THREAD_POOL_SIZE`, `MAX_BODY_SIZE`

---

### THREAD_POOL_SIZE

- **Type:** `int`
- **Default:** `24`
- **Min:** `1` | **Max:** `128`
- **Description:** Number of threads in the Zig HTTP server thread pool. Each thread handles requests concurrently. Set based on the number of CPU cores and expected request characteristics (CPU-bound vs I/O-bound).

```python
# settings.py
HYPERDJANGO_THREAD_POOL_SIZE = 8  # small VPS
HYPERDJANGO_THREAD_POOL_SIZE = 48  # high-core-count server
```

- **Related:** `HTTP_SERVER`, `MAX_BODY_SIZE`

---

### MAX_BODY_SIZE

- **Type:** `int`
- **Default:** `10485760` (10 MB)
- **Min:** `0`
- **Description:** Maximum allowed request body size in bytes. Requests with bodies exceeding this limit are rejected before processing. Applies to all request types (JSON, form data, file uploads).

```python
# settings.py
HYPERDJANGO_MAX_BODY_SIZE = 50 * 1024 * 1024  # 50 MB for file upload API
```

- **Related:** `MAX_UPLOAD_SIZE`, `HTTP_SERVER`

---

### PORT

- **Type:** `int`
- **Default:** `8000`
- **Description:** TCP port for the HTTP server to listen on.

```python
# settings.py
HYPERDJANGO_PORT = 8080
```

```bash
export HYPER_PORT=8080
```

- **Related:** `HOST`, `HTTP_SERVER`

---

### HOST

- **Type:** `str`
- **Default:** `"127.0.0.1"`
- **Description:** IP address for the HTTP server to bind to. Use `"0.0.0.0"` to listen on all interfaces in production.

```python
# settings.py (production)
HYPERDJANGO_HOST = "0.0.0.0"
```

```bash
export HYPER_HOST="0.0.0.0"
```

- **Related:** `PORT`, `HTTP_SERVER`, `ALLOWED_HOSTS`

---

### CORS_ORIGINS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of allowed origins for CORS. **Deny-by-default** — with the
  empty default, no cross-origin request is granted an
  `Access-Control-Allow-Origin` header. Set explicit origins to enable CORS; use
  `["*"]` only to deliberately allow all origins (development).

```python
# settings.py (production)
HYPERDJANGO_CORS_ORIGINS = ["https://example.com", "https://app.example.com"]
```

```bash
# Environment (comma-separated)
export HYPER_CORS_ORIGINS="https://example.com,https://app.example.com"
```

- **Related:** `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`

---

### RATE_LIMIT_REQUESTS

- **Type:** `int`
- **Default:** `120`
- **Min:** `1`
- **Description:** Maximum number of requests allowed per rate limit window. Applied per-client by RateLimitMiddleware. Set to a high value or use `LOAD_TEST=True` to disable during benchmarking.

```python
# settings.py
HYPERDJANGO_RATE_LIMIT_REQUESTS = 60  # stricter limit
```

- **Related:** `RATE_LIMIT_WINDOW`, `LOAD_TEST`

---

### RATE_LIMIT_WINDOW

- **Type:** `int`
- **Default:** `60` (1 minute)
- **Min:** `1`
- **Description:** Duration of the rate limit window in seconds. After the window expires, the request counter resets.

```python
# settings.py
HYPERDJANGO_RATE_LIMIT_WINDOW = 300  # 5-minute window
```

- **Related:** `RATE_LIMIT_REQUESTS`

---

## 12. URL Routing

### APPEND_SLASH

- **Type:** `bool`
- **Default:** `True`
- **Description:** When True, if a request URL does not match any route and does not end with a slash, the framework appends a slash and redirects. Provides clean URL behavior consistent with Django conventions.

```python
# settings.py
HYPERDJANGO_APPEND_SLASH = False  # disable for API-only servers
```

- **Related:** `PREPEND_WWW`

---

### PREPEND_WWW

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, requests to URLs without `www.` are redirected to `www.` versions. Useful for canonical URL enforcement.

```python
# settings.py
HYPERDJANGO_PREPEND_WWW = True  # redirect example.com -> www.example.com
```

- **Related:** `APPEND_SLASH`, `ALLOWED_HOSTS`

---

## 13. Proxy

### USE_X_FORWARDED_HOST

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, `request.get_host()` uses the `X-Forwarded-Host` header instead of the `Host` header. Enable when running behind a reverse proxy (nginx, ALB, CloudFront) that sets this header.

```python
# settings.py -- behind reverse proxy
HYPERDJANGO_USE_X_FORWARDED_HOST = True
```

- **Related:** `USE_X_FORWARDED_PORT`, `SECURE_PROXY_SSL_HEADER`

---

### USE_X_FORWARDED_PORT

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, the request port is determined from the `X-Forwarded-Port` header. Enable when running behind a reverse proxy that forwards on a different port.

```python
# settings.py -- behind reverse proxy
HYPERDJANGO_USE_X_FORWARDED_PORT = True
```

- **Related:** `USE_X_FORWARDED_HOST`, `SECURE_PROXY_SSL_HEADER`

---

### TRUSTED_PROXY_COUNT

- **Type:** `int`
- **Default:** `0`
- **Description:** Number of trusted reverse-proxy hops in front of the app. `0` (the default) means `X-Forwarded-For` / `X-Real-IP` are NOT trusted and `request.client_ip` comes from the socket peer — preventing IP spoofing (and the rate-limit-key spoofing / per-IP bucket memory exhaustion it enables). Set it to the number of proxies you actually run: the real client is then read from the correct hop of the forwarding chain (the entry just left of the last `TRUSTED_PROXY_COUNT` hops), clamped so a short or forged chain can never index past the real client.

```python
# settings.py -- one reverse proxy (e.g. nginx) in front
HYPERDJANGO_TRUSTED_PROXY_COUNT = 1
```

- **Related:** `TRUSTED_PROXIES`, `SECURE_PROXY_SSL_HEADER`

---

### TRUSTED_PROXIES

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of trusted proxy IP addresses. When the socket peer is one of these, `X-Forwarded-For` / `X-Real-IP` are honored regardless of `TRUSTED_PROXY_COUNT`, and the original client is taken from the left-most forwarded entry. Empty by default. Use this instead of a hop count when your proxies have known, stable addresses.

```python
# settings.py -- proxies with fixed addresses
HYPERDJANGO_TRUSTED_PROXIES = ["10.0.0.1", "10.0.0.2"]
```

**In-process mTLS terminator.** `MTLSTerminator.install(app, ...)` forwards over
loopback and injects `X-Real-IP` with the real client address, but that makes the
app's socket peer the loopback upstream — so `client_ip` would honor `X-Real-IP`
only if the loopback upstream is trusted. With `trust_upstream_ip=True` (the
default), `install` adds the loopback upstream address to this list automatically
(at the DEFAULTS layer), so `request.client_ip` reflects the injected `X-Real-IP`
and per-IP rate limiting keys on the real client instead of collapsing to
`127.0.0.1`. That automatic entry is **shadowed** if you set `TRUSTED_PROXIES`
explicitly here — when you configure the list by hand you own the policy, so
include the loopback upstream (`127.0.0.1`) yourself. See
[`docs/mtls.md`](mtls.md).

- **Related:** `TRUSTED_PROXY_COUNT`, `SECURE_PROXY_SSL_HEADER`

---

### MTLS_PROXY_SECRET

- **Type:** `str`
- **Default:** `""`
- **Description:** Shared attestation secret for the external-proxy mutual-TLS topology. When a TLS-terminating proxy in front of the app verifies client certificates, it must send this value in the `X-Hyper-MTLS-Attest` header alongside the verified-identity headers (`X-Hyper-MTLS-CN`, `X-Hyper-MTLS-Fingerprint`); the app honors those headers only when the attestation matches (constant-time comparison), so nothing that cannot present the secret can forge an identity. Empty (the default) disables the external-proxy path entirely — the in-process `MTLSTerminator` uses a per-process random secret and does not need this. See `docs/mtls.md` for a complete nginx configuration.

```python
# settings.py -- TLS-terminating proxy verifies client certs
HYPERDJANGO_MTLS_PROXY_SECRET = "a-long-random-shared-secret"
```

- **Related:** `USE_X_FORWARDED_HOST`, `SECURE_PROXY_SSL_HEADER`

---

## 14. Logging

### LOG_LEVEL

- **Type:** `str`
- **Default:** `"INFO"`
- **Choices:** `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- **Description:** Minimum logging level. Messages below this level are suppressed.

```python
# settings.py
HYPERDJANGO_LOG_LEVEL = "DEBUG"  # development -- verbose
HYPERDJANGO_LOG_LEVEL = "WARNING"  # production -- quiet
```

- **Related:** `LOG_FORMAT`, `DEBUG`

---

### LOG_FORMAT

- **Type:** `str`
- **Default:** `"text"`
- **Choices:** `text`, `json`
- **Description:** Log output format. `text` produces human-readable output with ANSI color support. `json` produces structured JSON lines for log aggregation services.

```python
# settings.py -- production
HYPERDJANGO_LOG_FORMAT = "json"
```

- **Related:** `LOG_LEVEL`

---

## 15. Messages

### MESSAGE_LEVEL

- **Type:** `int`
- **Default:** `20` (INFO)
- **Min:** `0`
- **Description:** Minimum message level to display. Messages below this level are silently discarded. Levels: 10=DEBUG, 20=INFO, 25=SUCCESS, 30=WARNING, 40=ERROR.

```python
# settings.py
HYPERDJANGO_MESSAGE_LEVEL = 10  # show all messages including debug
HYPERDJANGO_MESSAGE_LEVEL = 25  # only success, warning, error
```

- **Related:** `MESSAGE_TAGS`

---

### MESSAGE_TAGS

- **Type:** `dict`
- **Default:** `{}` (use built-in defaults)
- **Description:** Custom mapping of message level numbers to CSS class names. When empty, the built-in defaults are used. Override to match your frontend CSS framework.

```python
# settings.py -- Bootstrap 5 classes
HYPERDJANGO_MESSAGE_TAGS = {
    10: "alert-secondary",  # DEBUG
    20: "alert-info",  # INFO
    25: "alert-success",  # SUCCESS
    30: "alert-warning",  # WARNING
    40: "alert-danger",  # ERROR
}
```

- **Related:** `MESSAGE_LEVEL`

---

## 16. Internationalization

### LANGUAGE_CODE

- **Type:** `str`
- **Default:** `"en"`
- **Description:** Default language code (BCP 47 format). Used for locale-dependent formatting and translations.

```python
# settings.py
HYPERDJANGO_LANGUAGE_CODE = "en"
HYPERDJANGO_LANGUAGE_CODE = "fr"
HYPERDJANGO_LANGUAGE_CODE = "ja"
```

- **Related:** `TIME_ZONE`, `FIRST_DAY_OF_WEEK`

---

### TIME_ZONE

- **Type:** `str`
- **Default:** `"UTC"`
- **Description:** Default time zone for the application. Datetimes are stored as UTC in the database and converted to this time zone for display.

```python
# settings.py
HYPERDJANGO_TIME_ZONE = "UTC"  # default (recommended)
HYPERDJANGO_TIME_ZONE = "America/New_York"  # US Eastern
HYPERDJANGO_TIME_ZONE = "Europe/London"  # UK
HYPERDJANGO_TIME_ZONE = "Asia/Tokyo"  # Japan
```

- **Related:** `USE_TZ`, `DATETIME_FORMAT`

---

### USE_TZ

- **Type:** `bool`
- **Default:** `True`
- **Description:** When True, datetimes are stored as UTC in the database and converted to the configured time zone for display. Should always be True to avoid ambiguous datetime issues.

```python
# settings.py
HYPERDJANGO_USE_TZ = True  # default, always recommended
```

- **Related:** `TIME_ZONE`, `DATETIME_FORMAT`

---

### DATE_FORMAT

- **Type:** `str`
- **Default:** `"N j, Y"` (e.g., "March 28, 2026")
- **Description:** Default format for displaying dates. Uses Django/PHP-style format characters.

```python
# settings.py
HYPERDJANGO_DATE_FORMAT = "N j, Y"  # March 28, 2026
HYPERDJANGO_DATE_FORMAT = "Y-m-d"  # 2026-03-28 (ISO 8601)
HYPERDJANGO_DATE_FORMAT = "d/m/Y"  # 28/03/2026 (European)
```

- **Related:** `DATETIME_FORMAT`, `SHORT_DATE_FORMAT`, `DATE_INPUT_FORMATS`

---

### DATETIME_FORMAT

- **Type:** `str`
- **Default:** `"N j, Y, P"` (e.g., "March 28, 2026, 3:45 p.m.")
- **Description:** Default format for displaying datetimes.

```python
# settings.py
HYPERDJANGO_DATETIME_FORMAT = "Y-m-d H:i:s"  # 2026-03-28 15:45:00
```

- **Related:** `DATE_FORMAT`, `TIME_FORMAT`, `DATETIME_INPUT_FORMATS`

---

### TIME_FORMAT

- **Type:** `str`
- **Default:** `"P"` (e.g., "3:45 p.m.")
- **Description:** Default format for displaying time values.

```python
# settings.py
HYPERDJANGO_TIME_FORMAT = "H:i:s"  # 15:45:00 (24-hour)
HYPERDJANGO_TIME_FORMAT = "P"  # 3:45 p.m. (12-hour)
```

- **Related:** `DATETIME_FORMAT`, `DATE_FORMAT`

---

### SHORT_DATE_FORMAT

- **Type:** `str`
- **Default:** `"m/d/Y"` (e.g., "03/28/2026")
- **Description:** Short date display format used in compact contexts.

```python
# settings.py
HYPERDJANGO_SHORT_DATE_FORMAT = "Y-m-d"  # ISO style
HYPERDJANGO_SHORT_DATE_FORMAT = "d/m/Y"  # European style
```

- **Related:** `DATE_FORMAT`, `SHORT_DATETIME_FORMAT`

---

### SHORT_DATETIME_FORMAT

- **Type:** `str`
- **Default:** `"m/d/Y P"` (e.g., "03/28/2026 3:45 p.m.")
- **Description:** Short datetime display format used in compact contexts.

```python
# settings.py
HYPERDJANGO_SHORT_DATETIME_FORMAT = "Y-m-d H:i"  # 2026-03-28 15:45
```

- **Related:** `DATETIME_FORMAT`, `SHORT_DATE_FORMAT`

---

### DECIMAL_SEPARATOR

- **Type:** `str`
- **Default:** `"."`
- **Description:** Character used as the decimal separator in number formatting.

```python
# settings.py -- European locale
HYPERDJANGO_DECIMAL_SEPARATOR = ","
```

- **Related:** `THOUSAND_SEPARATOR`, `USE_THOUSAND_SEPARATOR`

---

### THOUSAND_SEPARATOR

- **Type:** `str`
- **Default:** `","`
- **Description:** Character used as the thousands grouping separator.

```python
# settings.py -- European locale
HYPERDJANGO_THOUSAND_SEPARATOR = "."

# settings.py -- space separator
HYPERDJANGO_THOUSAND_SEPARATOR = " "
```

- **Related:** `DECIMAL_SEPARATOR`, `USE_THOUSAND_SEPARATOR`, `NUMBER_GROUPING`

---

### USE_THOUSAND_SEPARATOR

- **Type:** `bool`
- **Default:** `False`
- **Description:** When True, numbers are formatted with thousand separators in templates and display functions.

```python
# settings.py
HYPERDJANGO_USE_THOUSAND_SEPARATOR = True  # 1,000,000 instead of 1000000
```

- **Related:** `THOUSAND_SEPARATOR`, `DECIMAL_SEPARATOR`, `NUMBER_GROUPING`

---

### NUMBER_GROUPING

- **Type:** `int`
- **Default:** `3`
- **Min:** `0` | **Max:** `10`
- **Description:** Number of digits per group when using thousands separators. Most locales use 3 (e.g., 1,000,000). Indian numbering uses 2 after the first group (not yet supported as variable grouping).

```python
# settings.py
HYPERDJANGO_NUMBER_GROUPING = 3  # default -- 1,000,000
```

- **Related:** `USE_THOUSAND_SEPARATOR`, `THOUSAND_SEPARATOR`

---

### FIRST_DAY_OF_WEEK

- **Type:** `int`
- **Default:** `0` (Sunday)
- **Min:** `0` | **Max:** `6`
- **Description:** First day of the week for calendar/date displays. 0=Sunday, 1=Monday, ..., 6=Saturday.

```python
# settings.py
HYPERDJANGO_FIRST_DAY_OF_WEEK = 0  # Sunday (US)
HYPERDJANGO_FIRST_DAY_OF_WEEK = 1  # Monday (Europe, ISO 8601)
```

- **Related:** `LANGUAGE_CODE`, `DATE_FORMAT`

---

### DATE_INPUT_FORMATS

- **Type:** `list`
- **Default:** `["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]`
- **Description:** Accepted date input formats for form parsing. Formats are tried in order; the first match wins. Uses Python `strptime` format codes.

```python
# settings.py
HYPERDJANGO_DATE_INPUT_FORMATS = [
    "%Y-%m-%d",  # 2026-03-28 (ISO 8601)
    "%d/%m/%Y",  # 28/03/2026 (European)
    "%d.%m.%Y",  # 28.03.2026 (German)
]
```

- **Related:** `DATETIME_INPUT_FORMATS`, `DATE_FORMAT`

---

### DATETIME_INPUT_FORMATS

- **Type:** `list`
- **Default:** `["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S"]`
- **Description:** Accepted datetime input formats for form parsing.

```python
# settings.py
HYPERDJANGO_DATETIME_INPUT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",  # 2026-03-28 15:45:00
    "%Y-%m-%dT%H:%M:%S",  # 2026-03-28T15:45:00 (ISO 8601)
    "%Y-%m-%d %H:%M",  # 2026-03-28 15:45
]
```

- **Related:** `DATE_INPUT_FORMATS`, `DATETIME_FORMAT`

---

### LOCALE_PATHS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of filesystem directories to scan for `.po` translation files. Each directory should contain subdirectories named by language code (e.g., `fr/LC_MESSAGES/django.po`). Used by `discover_translations()` and `load_translations()` in the i18n module. If empty and no explicit paths are passed, the i18n module falls back to `["locale"]`.

```python
# settings.py
HYPERDJANGO_LOCALE_PATHS = [
    "locale",
    "myapp/locale",
    "/opt/translations",
]
```

```bash
# Environment (comma-separated)
HYPER_LOCALE_PATHS="locale,myapp/locale"
```

- **Related:** `LANGUAGE_CODE`

---

## 17. Tasks

### TASK_WORKERS

- **Type:** `int`
- **Default:** `4`
- **Min:** `1` | **Max:** `64`
- **Description:** Number of background task worker threads. Each worker creates its own asyncio event loop and processes tasks from the shared priority queue. More workers allow higher task throughput at the cost of more threads and memory.

```python
# settings.py
HYPERDJANGO_TASK_WORKERS = 8  # 8 worker threads
```

```bash
HYPER_TASK_WORKERS=8
```

- **Related:** `TASK_MAX_QUEUE_SIZE`, `TASK_DLQ_MAX_SIZE`

---

### TASK_MAX_QUEUE_SIZE

- **Type:** `int`
- **Default:** `10000`
- **Min:** `1` | **Max:** `1000000`
- **Description:** Maximum number of pending tasks allowed in the queue. When the queue is full, new `.delay()` calls are dropped with a warning log and the task handle status is set to `FAILED` with error `"Queue full"`. Increase for high-throughput workloads.

```python
# settings.py
HYPERDJANGO_TASK_MAX_QUEUE_SIZE = 50000
```

```bash
HYPER_TASK_MAX_QUEUE_SIZE=50000
```

- **Related:** `TASK_WORKERS`, `TASK_DLQ_MAX_SIZE`

---

### TASK_DLQ_MAX_SIZE

- **Type:** `int`
- **Default:** `10000`
- **Min:** `1` | **Max:** `1000000`
- **Description:** Maximum number of entries retained in the dead letter queue. When this limit is reached, the oldest dead letters are evicted to make room. Tasks enter the DLQ when all retry attempts are exhausted.

```python
# settings.py
HYPERDJANGO_TASK_DLQ_MAX_SIZE = 50000
```

```bash
HYPER_TASK_DLQ_MAX_SIZE=50000
```

- **Related:** `TASK_WORKERS`, `TASK_MAX_QUEUE_SIZE`

---

## 18. Features

### FILE_ROUTING

- **Type:** `bool`
- **Default:** `False`
- **Description:** Enable file-based routing. When True, the framework discovers routes automatically from files in the `FILE_ROUTING_DIR` directory, similar to Next.js file-system routing.

```python
# settings.py
HYPERDJANGO_FILE_ROUTING = True
```

- **Related:** `FILE_ROUTING_DIR`

---

### FILE_ROUTING_DIR

- **Type:** `str`
- **Default:** `"views"`
- **Description:** Directory for file-based route discovery. Relative to the project root. Each Python file in this directory becomes a route.

```python
# settings.py
HYPERDJANGO_FILE_ROUTING_DIR = "pages"  # use pages/ instead of views/
```

- **Related:** `FILE_ROUTING`

---

### STATIC_CACHE

- **Type:** `bool`
- **Default:** `False`
- **Description:** Enable in-memory static file caching. When True, static files are loaded into memory on startup for faster serving. Useful for production deployments with a moderate number of static files.

```python
# settings.py -- production
HYPERDJANGO_STATIC_CACHE = True
```

- **Related:** `STATIC_URL`, `STATIC_MAX_AGE`

---

### HOT_RELOAD

- **Type:** `bool`
- **Default:** `False`
- **Description:** Enable hot reload on file changes. Uses native kqueue (macOS) or inotify (Linux) file watching -- no polling. Automatically reloads Python modules and pushes updates via SSE.

```python
# settings.py -- development
HYPERDJANGO_HOT_RELOAD = True
```

- **Related:** `DEBUG`, `FILE_ROUTING`

---

### PRE_VALIDATION

- **Type:** `bool`
- **Default:** `False`
- **Description:** Enable pre-GIL request validation. When True, incoming request bodies are validated in the native Zig extension before acquiring the GIL, allowing validation to run in parallel across all threads.

```python
# settings.py -- high-throughput API
HYPERDJANGO_PRE_VALIDATION = True
```

- **Related:** `VALIDATION_BACKEND`, `HTTP_SERVER`

---

### VALIDATION_BACKEND

- **Type:** `str`
- **Default:** `"dhi"`
- **Choices:** `dhi`, `django`
- **Description:** Validation engine backend. `dhi` uses the native SIMD validation engine (51M ints/sec, 13M models/sec). `django` uses Django's built-in validation for compatibility.

```python
# settings.py
HYPERDJANGO_VALIDATION_BACKEND = "dhi"  # native SIMD (default)
HYPERDJANGO_VALIDATION_BACKEND = "django"  # Django compatibility
```

- **Related:** `PRE_VALIDATION`

---

### LOAD_TEST

- **Type:** `bool`
- **Default:** `False`
- **Description:** Disable rate limiting for load testing. When True, RateLimitMiddleware is bypassed entirely. **Never enable in production.**

```bash
# For load testing
HYPER_LOAD_TEST=1 uv run hyper start --app app:app
```

- **Related:** `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW`

---

## 19. Embeddings

### EMBEDDINGS_API_URL

- **Type:** `str`
- **Default:** `""`
- **Description:** URL for an OpenAI-compatible embeddings API endpoint. Used by the semantic search service. Empty disables embedding generation.

```bash
export HYPER_EMBEDDINGS_API_URL="https://api.openai.com/v1/embeddings"
```

- **Related:** `EMBEDDINGS_API_KEY`, `EMBEDDINGS_MODEL`

---

### EMBEDDINGS_API_KEY

- **Type:** `str`
- **Default:** `""`
- **Description:** API key for the embeddings service. Required when `EMBEDDINGS_API_URL` is set.

```bash
export HYPER_EMBEDDINGS_API_KEY="sk-..."
```

- **Related:** `EMBEDDINGS_API_URL`, `EMBEDDINGS_MODEL`

---

### EMBEDDINGS_MODEL

- **Type:** `str`
- **Default:** `"text-embedding-3-small"`
- **Description:** Model name passed to the embeddings API. Must match your provider's available models.

```python
# settings.py
HYPERDJANGO_EMBEDDINGS_MODEL = "text-embedding-3-large"
```

- **Related:** `EMBEDDINGS_API_URL`, `EMBEDDINGS_VECTOR_DIM`

---

### EMBEDDINGS_VECTOR_DIM

- **Type:** `int`
- **Default:** `1536`
- **Min:** `1`
- **Description:** Dimensionality of embedding vectors. Must match the output dimension of `EMBEDDINGS_MODEL`. Used for VectorField column sizing and HNSW index parameters.

```python
# settings.py -- text-embedding-3-large
HYPERDJANGO_EMBEDDINGS_VECTOR_DIM = 3072
```

- **Related:** `EMBEDDINGS_MODEL`, `EMBEDDINGS_API_URL`

---

## 20. Other

### ADMINS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of (name, email) tuples of people who receive error notification emails when unhandled exceptions occur in production.

```python
# settings.py
HYPERDJANGO_ADMINS = [
    ("Alice", "alice@example.com"),
    ("Bob", "bob@example.com"),
]
```

- **Related:** `MANAGERS`, `SERVER_EMAIL`, `EMAIL_SUBJECT_PREFIX`

---

### MANAGERS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of (name, email) tuples of people who receive broken link notifications and other management alerts.

```python
# settings.py
HYPERDJANGO_MANAGERS = [
    ("Ops Team", "ops@example.com"),
]
```

- **Related:** `ADMINS`, `SERVER_EMAIL`

---

### DISALLOWED_USER_AGENTS

- **Type:** `list`
- **Default:** `[]`
- **Description:** List of user-agent regex strings. Requests with User-Agent headers matching any of these patterns are rejected with a 403 Forbidden response.

```python
# settings.py
HYPERDJANGO_DISALLOWED_USER_AGENTS = [
    r"BadBot",
    r"Scrapy",
    r"python-requests/\d",
]
```

- **Related:** `ALLOWED_HOSTS`

---

## 21. Asset Versioning

### APP_VERSION

- **Type:** `str` | **Default:** `""`
- **Description:** Explicit app version string. Empty = auto-compute from manifest hash.

### APP_VERSION_HEADER

- **Type:** `bool` | **Default:** `True`
- **Description:** Emit `X-App-Version` and `X-App-Version-Action` headers on every response.

### APP_VERSION_MISMATCH

- **Type:** `str` | **Default:** `"prompt"` | **Choices:** `"prompt"`, `"reload"`, `"warn"`, `"ignore"`
- **Description:** Operator policy advertised as `X-App-Version-Action` when a client's page version differs from the serving version. `"prompt"` shows a dismissible "new version available" banner with a Reload button (user-initiated, so no in-flight state is destroyed); `"reload"` reloads at the next navigation boundary, never mid-interaction, and degrades to `"prompt"` if one reload did not clear the skew; `"warn"` logs to console; `"ignore"` injects no mismatch script and, as a response header, tells already-instrumented older clients to stand down. An invalid value is a startup error.

### APP_VERSION_CLIENT_BROADCAST

- **Type:** `bool` | **Default:** `True`
- **Description:** Broadcast each page's own version back to the server so a load balancer can pin a client to a matching backend through a rolling deploy. Gates the whole feature: the injected script's `X-Client-Version` header and `hyper_client_version` cookie, the middleware's inbound parse onto `request.client_version`, and the `hyperdjango_version_skew_requests_total` metric.

### VERSION_ENDPOINT

- **Type:** `bool` | **Default:** `True`
- **Description:** Mount `/version` JSON endpoint and `/cache/bust` invalidation endpoint.

### STATIC_DEV_VERSION_QUERY

- **Type:** `bool` | **Default:** `True`
- **Description:** In dev mode, append `?v=<content_hash>` to static file URLs for cache busting.

---

## 22. Telemetry

### TELEMETRY_ENABLED

- **Type:** `bool` | **Default:** `False`
- **Description:** Master switch. When False, all telemetry is zero-cost (no-op spans, no metric emission).

### TELEMETRY_SERVICE_NAME

- **Type:** `str` | **Default:** `"hyperdjango"`
- **Description:** Service name attached to every root span and metric.

### TELEMETRY_SAMPLE_RATIO

- **Type:** `float` | **Default:** `1.0` | **Range:** `0.0` to `1.0`
- **Description:** Head sampling ratio. `1.0` = trace every request, `0.01` = 1%.

### TELEMETRY_DRAIN_INTERVAL

- **Type:** `float` | **Default:** `5.0`
- **Description:** Background drain worker interval in seconds. Controls how often buffered spans/metrics are flushed to sinks.

### TELEMETRY_EXTRACT_TRACEPARENT

- **Type:** `bool` | **Default:** `True`
- **Description:** Honor incoming W3C `traceparent` headers for distributed trace propagation.

### TELEMETRY_SINKS

- **Type:** `list[str]` | **Default:** `["prometheus"]`
- **Description:** Active telemetry sinks. Options: `"prometheus"`, `"stdout"`, `"memory"` (test assertions).

### TELEMETRY_SPAN_RING_CAPACITY

- **Type:** `int` | **Default:** `4096`
- **Description:** MPSC span ring buffer capacity. Must be a power of 2 in range [256, 16M].

### TELEMETRY_AUTO_LOG_CORRELATION

- **Type:** `bool` | **Default:** `True`
- **Description:** Auto-inject `trace_id`/`span_id` into log records inside active spans.

---

## 23. Tuning

### STATICFILES_GZIP_MIN_SIZE

- **Type:** `int` | **Default:** `1024`
- **Description:** Minimum file size (bytes) for gzip pre-compression during collectstatic.

### STATICFILES_HASH_LENGTH

- **Type:** `int` | **Default:** `12`
- **Description:** Content-hash suffix length in hashed filenames (e.g., `styles.a1b2c3d4e5f6.css`).

### STATICFILES_MAX_POST_PROCESS_PASSES

- **Type:** `int` | **Default:** `5`
- **Description:** Maximum CSS `url()` rewrite iterations during collectstatic post-processing.

### STATICFILES_DEV_HASH_CACHE_MAX

- **Type:** `int` | **Default:** `1000`
- **Description:** Dev-mode `?v=hash` URL cache size (LRU eviction).

### TASK_MAX_COMPLETED_RESULTS

- **Type:** `int` | **Default:** `1000`
- **Description:** Maximum completed task results retained before LRU eviction.

### TASK_CLEANUP_INTERVAL

- **Type:** `float` | **Default:** `60.0`
- **Description:** Interval (seconds) between completed-task eviction checks.

### TASK_SHUTDOWN_TIMEOUT

- **Type:** `float` | **Default:** `30.0`
- **Description:** Worker shutdown grace period (seconds) for in-flight tasks.

### PERFORMANCE_HISTORY_SIZE

- **Type:** `int` | **Default:** `1000`
- **Description:** Request history ring buffer size for the performance dashboard.

### PERFORMANCE_N_PLUS_ONE_THRESHOLD

- **Type:** `int` | **Default:** `5`
- **Description:** Repeated-query count that triggers N+1 detection warning.

### SLOW_QUERY_SQL_LENGTH

- **Type:** `int` | **Default:** `500`
- **Description:** SQL text truncation length in slow query logs.

### SLOW_QUERY_PARAMS_LENGTH

- **Type:** `int` | **Default:** `200`
- **Description:** Parameter text truncation length in slow query logs.

### SLOW_QUERY_RETENTION_DAYS

- **Type:** `int` | **Default:** `7`
- **Description:** How many days to retain slow query log entries.

### RATELIMIT_CLEANUP_RETENTION

- **Type:** `float` | **Default:** `3600.0`
- **Description:** How long (seconds) to retain expired rate-limit entries.

### HOT_RELOAD_POLL_INTERVAL

- **Type:** `float` | **Default:** `1.0`
- **Description:** File poll interval (seconds) for fallback polling mode (kqueue/inotify preferred).

### HOT_RELOAD_SSE_HEARTBEAT

- **Type:** `float` | **Default:** `30.0`
- **Description:** SSE keepalive ping interval (seconds) for hot-reload connections.

### FILE_UPLOAD_MAX_SIZE

- **Type:** `int` | **Default:** `0`
- **Description:** Maximum per-file size during multipart parsing. `0` = unlimited.

### STREAM_BODY_CHUNK_SIZE

- **Type:** `int` | **Default:** `262144` (256 KB)
- **Description:** Chunk size for `request.stream()` and `UploadedFile.chunks()`.

---

## Environment Variable Reference

Every setting can be configured via an environment variable with the `HYPER_` prefix:

| Setting                 | Environment Variable          | Example                                  |
| ----------------------- | ----------------------------- | ---------------------------------------- |
| `SECRET_KEY`            | `HYPER_SECRET_KEY`            | `HYPER_SECRET_KEY=abc123...`             |
| `DEBUG`                 | `HYPER_DEBUG`                 | `HYPER_DEBUG=true`                       |
| `ALLOWED_HOSTS`         | `HYPER_ALLOWED_HOSTS`         | `HYPER_ALLOWED_HOSTS=a.com,b.com`        |
| `DATABASE_URL`          | `HYPER_DATABASE_URL`          | `HYPER_DATABASE_URL=postgresql://...`    |
| `POOL_SIZE`             | `HYPER_POOL_SIZE`             | `HYPER_POOL_SIZE=20`                     |
| `POOL_MAX_QUERIES`      | `HYPER_POOL_MAX_QUERIES`      | `HYPER_POOL_MAX_QUERIES=50000`           |
| `POOL_MAX_LIFETIME`     | `HYPER_POOL_MAX_LIFETIME`     | `HYPER_POOL_MAX_LIFETIME=1800`           |
| `STATEMENT_CACHE_SIZE`  | `HYPER_STATEMENT_CACHE_SIZE`  | `HYPER_STATEMENT_CACHE_SIZE=512`         |
| `CONNECT_TIMEOUT`       | `HYPER_CONNECT_TIMEOUT`       | `HYPER_CONNECT_TIMEOUT=5000`             |
| `QUERY_TIMEOUT`         | `HYPER_QUERY_TIMEOUT`         | `HYPER_QUERY_TIMEOUT=30000`              |
| `PREPARED_STATEMENTS`   | `HYPER_PREPARED_STATEMENTS`   | `HYPER_PREPARED_STATEMENTS=false`        |
| `SECURE_SSL_REDIRECT`   | `HYPER_SECURE_SSL_REDIRECT`   | `HYPER_SECURE_SSL_REDIRECT=true`         |
| `SECURE_HSTS_SECONDS`   | `HYPER_SECURE_HSTS_SECONDS`   | `HYPER_SECURE_HSTS_SECONDS=31536000`     |
| `CACHE_BACKEND`         | `HYPER_CACHE_BACKEND`         | `HYPER_CACHE_BACKEND=database`           |
| `CACHE_TTL`             | `HYPER_CACHE_TTL`             | `HYPER_CACHE_TTL=600`                    |
| `EMAIL_BACKEND`         | `HYPER_EMAIL_BACKEND`         | `HYPER_EMAIL_BACKEND=console`            |
| `EMAIL_HOST`            | `HYPER_EMAIL_HOST`            | `HYPER_EMAIL_HOST=smtp.sendgrid.net`     |
| `EMAIL_PORT`            | `HYPER_EMAIL_PORT`            | `HYPER_EMAIL_PORT=587`                   |
| `EMAIL_HOST_USER`       | `HYPER_EMAIL_HOST_USER`       | `HYPER_EMAIL_HOST_USER=apikey`           |
| `EMAIL_HOST_PASSWORD`   | `HYPER_EMAIL_HOST_PASSWORD`   | `HYPER_EMAIL_HOST_PASSWORD=SG.xxx`       |
| `EMAIL_USE_TLS`         | `HYPER_EMAIL_USE_TLS`         | `HYPER_EMAIL_USE_TLS=true`               |
| `LOG_LEVEL`             | `HYPER_LOG_LEVEL`             | `HYPER_LOG_LEVEL=WARNING`                |
| `LOG_FORMAT`            | `HYPER_LOG_FORMAT`            | `HYPER_LOG_FORMAT=json`                  |
| `HTTP_SERVER`           | `HYPER_HTTP_SERVER`           | `HYPER_HTTP_SERVER=zig`                  |
| `THREAD_POOL_SIZE`      | `HYPER_THREAD_POOL_SIZE`      | `HYPER_THREAD_POOL_SIZE=48`              |
| `HOT_RELOAD`            | `HYPER_HOT_RELOAD`            | `HYPER_HOT_RELOAD=true`                  |
| `FILE_ROUTING`          | `HYPER_FILE_ROUTING`          | `HYPER_FILE_ROUTING=true`                |
| `PASSWORD_HASHER`       | `HYPER_PASSWORD_HASHER`       | `HYPER_PASSWORD_HASHER=argon2id`         |
| `SESSION_COOKIE_AGE`    | `HYPER_SESSION_COOKIE_AGE`    | `HYPER_SESSION_COOKIE_AGE=86400`         |
| `LANGUAGE_CODE`         | `HYPER_LANGUAGE_CODE`         | `HYPER_LANGUAGE_CODE=fr`                 |
| `TIME_ZONE`             | `HYPER_TIME_ZONE`             | `HYPER_TIME_ZONE=America/New_York`       |
| `LOCALE_PATHS`          | `HYPER_LOCALE_PATHS`          | `HYPER_LOCALE_PATHS=locale,myapp/locale` |
| `TASK_WORKERS`          | `HYPER_TASK_WORKERS`          | `HYPER_TASK_WORKERS=8`                   |
| `TASK_MAX_QUEUE_SIZE`   | `HYPER_TASK_MAX_QUEUE_SIZE`   | `HYPER_TASK_MAX_QUEUE_SIZE=50000`        |
| `TASK_DLQ_MAX_SIZE`     | `HYPER_TASK_DLQ_MAX_SIZE`     | `HYPER_TASK_DLQ_MAX_SIZE=50000`          |
| `CSRF_SECRET`           | `HYPER_CSRF_SECRET`           | `HYPER_CSRF_SECRET=your-csrf-secret`     |
| `SESSION_SECRET`        | `HYPER_SESSION_SECRET`        | `HYPER_SESSION_SECRET=your-session-key`  |
| `SESSION_SIGNING_KEY`   | `HYPER_SESSION_SIGNING_KEY`   | `HYPER_SESSION_SIGNING_KEY=your-key`     |
| `ADMIN_SECRET`          | `HYPER_ADMIN_SECRET`          | `HYPER_ADMIN_SECRET=your-admin-secret`   |
| `API_KEY`               | `HYPER_API_KEY`               | `HYPER_API_KEY=sk_live_...`              |
| `SEED_PASSWORD`         | `HYPER_SEED_PASSWORD`         | `HYPER_SEED_PASSWORD=my-test-pw`         |
| `ADMIN_PASSWORD`        | `HYPER_ADMIN_PASSWORD`        | `HYPER_ADMIN_PASSWORD=my-admin-pw`       |
| `PORT`                  | `HYPER_PORT`                  | `HYPER_PORT=8080`                        |
| `HOST`                  | `HYPER_HOST`                  | `HYPER_HOST=0.0.0.0`                     |
| `CORS_ORIGINS`          | `HYPER_CORS_ORIGINS`          | `HYPER_CORS_ORIGINS=https://example.com` |
| `RATE_LIMIT_REQUESTS`   | `HYPER_RATE_LIMIT_REQUESTS`   | `HYPER_RATE_LIMIT_REQUESTS=60`           |
| `RATE_LIMIT_WINDOW`     | `HYPER_RATE_LIMIT_WINDOW`     | `HYPER_RATE_LIMIT_WINDOW=300`            |
| `EMBEDDINGS_API_URL`    | `HYPER_EMBEDDINGS_API_URL`    | `HYPER_EMBEDDINGS_API_URL=https://...`   |
| `EMBEDDINGS_API_KEY`    | `HYPER_EMBEDDINGS_API_KEY`    | `HYPER_EMBEDDINGS_API_KEY=sk-...`        |
| `EMBEDDINGS_MODEL`      | `HYPER_EMBEDDINGS_MODEL`      | `HYPER_EMBEDDINGS_MODEL=text-embed-3-lg` |
| `EMBEDDINGS_VECTOR_DIM` | `HYPER_EMBEDDINGS_VECTOR_DIM` | `HYPER_EMBEDDINGS_VECTOR_DIM=3072`       |
| `LOAD_TEST`             | `HYPER_LOAD_TEST`             | `HYPER_LOAD_TEST=true`                   |

### Type Coercion

Environment variables are always strings. They are automatically coerced to the target type:

| Target Type | Coercion Rule                                                                       | Example                           |
| ----------- | ----------------------------------------------------------------------------------- | --------------------------------- |
| `bool`      | `"1"`, `"true"`, `"yes"`, `"on"` = True (case-insensitive); everything else = False | `HYPER_DEBUG=true`                |
| `int`       | `int(value)`                                                                        | `HYPER_POOL_SIZE=20`              |
| `float`     | `float(value)`                                                                      | `HYPER_RATE=0.5`                  |
| `list`      | Comma-separated, whitespace-trimmed                                                 | `HYPER_ALLOWED_HOSTS=a.com,b.com` |
| `str`       | Passed through unchanged                                                            | `HYPER_SECRET_KEY=abc`            |

---

## .env File Format

Place a `.env` file in your project root for local development:

```env
# .env -- development configuration
SECRET_KEY=dev-secret-key-not-for-production
DEBUG=true
DATABASE_URL=postgresql://localhost/mydb

# These are equivalent (HYPER_ prefix is optional in .env):
ALLOWED_HOSTS=localhost,127.0.0.1
HYPER_ALLOWED_HOSTS=localhost,127.0.0.1

# Email (console backend for development)
EMAIL_BACKEND=console

# Logging
LOG_LEVEL=DEBUG
LOG_FORMAT=text

# Features
HOT_RELOAD=true
FILE_ROUTING=true
```

### Format Rules

- One `KEY=VALUE` per line
- Lines starting with `#` are comments
- Blank lines are ignored
- Values can be quoted with single or double quotes (quotes are stripped)
- No variable interpolation

### Resolution Priority in .env

1. `HYPER_<NAME>` in real environment variables (highest priority)
2. `HYPER_<NAME>` in `.env` file
3. Bare `<NAME>` in `.env` file (lowest priority)

### Custom .env Path

```python
from hyperdjango.conf import load_env_settings
import pathlib

settings = load_env_settings(dotenv_path=pathlib.Path("/etc/myapp/.env"))
```

---

## Startup Validation

Validate all settings at application startup:

```python
from hyperdjango.conf import validate_settings

errors = validate_settings()
if errors:
    for error in errors:
        print(f"Configuration error: {error}")
    raise SystemExit(1)
```

### What validate_settings() Checks

- **Required settings:** `SECRET_KEY` must be non-empty in production (`DEBUG=False`). In debug mode, a random key is auto-generated with a warning.
- **Type checking:** Each setting must match its declared type (`int`, `str`, `bool`, `list`, `dict`).
- **Range checking:** Numeric settings are validated against `min_value` and `max_value` constraints (e.g., `POOL_SIZE` must be 0-1024, `EMAIL_PORT` must be 1-65535).
- **Choice checking:** String settings with restricted values are validated against the allowed set (e.g., `LOG_LEVEL` must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL).

### Validation Examples

```python
errors = validate_settings({"POOL_SIZE": -1})
# ["POOL_SIZE: value -1 is below minimum 0"]

errors = validate_settings({"LOG_LEVEL": "TRACE"})
# ["LOG_LEVEL: value 'TRACE' is not one of ['CRITICAL', 'DEBUG', 'ERROR', 'INFO', 'WARNING']"]

errors = validate_settings({"THREAD_POOL_SIZE": "fast"})
# ["THREAD_POOL_SIZE: expected type int, got str ('fast')"]

errors = validate_settings({"EMAIL_PORT": 99999})
# ["EMAIL_PORT: value 99999 is above maximum 65535"]

errors = validate_settings({"SECRET_KEY": "", "DEBUG": False})
# ["SECRET_KEY: required setting is not set"]
```

---

## Django Migration Guide

Mapping Django settings to their HyperDjango equivalents:

| Django Setting                      | HyperDjango Setting                                | Notes                                                      |
| ----------------------------------- | -------------------------------------------------- | ---------------------------------------------------------- |
| `SECRET_KEY`                        | `HYPERDJANGO_SECRET_KEY`                           | Same purpose                                               |
| `DEBUG`                             | `HYPERDJANGO_DEBUG`                                | Same purpose                                               |
| `ALLOWED_HOSTS`                     | `HYPERDJANGO_ALLOWED_HOSTS`                        | Same purpose                                               |
| `DATABASES`                         | `HYPERDJANGO_DATABASE_URL`                         | Single URL instead of dict (PostgreSQL only)               |
| `CACHES`                            | `HYPERDJANGO_CACHE_BACKEND`                        | `"memory"` or `"database"`                                 |
| `EMAIL_HOST`                        | `HYPERDJANGO_EMAIL_HOST`                           | Same purpose                                               |
| `EMAIL_PORT`                        | `HYPERDJANGO_EMAIL_PORT`                           | Same purpose                                               |
| `EMAIL_HOST_USER`                   | `HYPERDJANGO_EMAIL_HOST_USER`                      | Same purpose                                               |
| `EMAIL_HOST_PASSWORD`               | `HYPERDJANGO_EMAIL_HOST_PASSWORD`                  | Same purpose                                               |
| `EMAIL_USE_TLS`                     | `HYPERDJANGO_EMAIL_USE_TLS`                        | Same purpose                                               |
| `EMAIL_USE_SSL`                     | `HYPERDJANGO_EMAIL_USE_SSL`                        | Same purpose                                               |
| `EMAIL_BACKEND`                     | `HYPERDJANGO_EMAIL_BACKEND`                        | `"smtp"`, `"console"`, `"memory"` (not class paths)        |
| `DEFAULT_FROM_EMAIL`                | `HYPERDJANGO_DEFAULT_FROM_EMAIL`                   | Same purpose                                               |
| `SERVER_EMAIL`                      | `HYPERDJANGO_SERVER_EMAIL`                         | Same purpose                                               |
| `STATIC_URL`                        | `HYPERDJANGO_STATIC_URL`                           | Same purpose                                               |
| `STATIC_ROOT`                       | `HYPERDJANGO_STATIC_ROOT`                          | Same purpose                                               |
| `STATICFILES_DIRS`                  | `HYPERDJANGO_STATICFILES_DIRS`                     | Same purpose                                               |
| `MEDIA_URL`                         | `HYPERDJANGO_MEDIA_URL`                            | Same purpose                                               |
| `MEDIA_ROOT`                        | `HYPERDJANGO_MEDIA_ROOT`                           | Same purpose                                               |
| `SESSION_COOKIE_AGE`                | `HYPERDJANGO_SESSION_COOKIE_AGE`                   | Same purpose                                               |
| `SESSION_COOKIE_SECURE`             | `HYPERDJANGO_SESSION_COOKIE_SECURE`                | Same purpose                                               |
| `SESSION_COOKIE_HTTPONLY`           | `HYPERDJANGO_SESSION_COOKIE_HTTPONLY`              | Same purpose                                               |
| `SESSION_COOKIE_SAMESITE`           | `HYPERDJANGO_SESSION_COOKIE_SAMESITE`              | Same purpose                                               |
| `SESSION_COOKIE_NAME`               | `HYPERDJANGO_SESSION_COOKIE_NAME`                  | Same purpose                                               |
| `SESSION_COOKIE_DOMAIN`             | `HYPERDJANGO_SESSION_COOKIE_DOMAIN`                | Same purpose                                               |
| `SESSION_COOKIE_PATH`               | `HYPERDJANGO_SESSION_COOKIE_PATH`                  | Same purpose                                               |
| `SESSION_EXPIRE_AT_BROWSER_CLOSE`   | `HYPERDJANGO_SESSION_EXPIRE_AT_BROWSER_CLOSE`      | Same purpose                                               |
| `SESSION_SAVE_EVERY_REQUEST`        | `HYPERDJANGO_SESSION_SAVE_EVERY_REQUEST`           | Same purpose                                               |
| `CSRF_COOKIE_SECURE`                | `HYPERDJANGO_CSRF_COOKIE_SECURE`                   | Same purpose                                               |
| `CSRF_COOKIE_HTTPONLY`              | `HYPERDJANGO_CSRF_COOKIE_HTTPONLY`                 | Same purpose                                               |
| `CSRF_COOKIE_SAMESITE`              | `HYPERDJANGO_CSRF_COOKIE_SAMESITE`                 | Same purpose                                               |
| `CSRF_COOKIE_DOMAIN`                | `HYPERDJANGO_CSRF_COOKIE_DOMAIN`                   | Same purpose                                               |
| `CSRF_COOKIE_NAME`                  | `HYPERDJANGO_CSRF_COOKIE_NAME`                     | Same purpose                                               |
| `CSRF_COOKIE_AGE`                   | `HYPERDJANGO_CSRF_COOKIE_AGE`                      | Same purpose                                               |
| `CSRF_TRUSTED_ORIGINS`              | `HYPERDJANGO_CSRF_TRUSTED_ORIGINS`                 | Same purpose                                               |
| `SECURE_SSL_REDIRECT`               | `HYPERDJANGO_SECURE_SSL_REDIRECT`                  | Same purpose                                               |
| `SECURE_HSTS_SECONDS`               | `HYPERDJANGO_SECURE_HSTS_SECONDS`                  | Same purpose                                               |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS`    | `HYPERDJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS`       | Same purpose                                               |
| `SECURE_HSTS_PRELOAD`               | `HYPERDJANGO_SECURE_HSTS_PRELOAD`                  | Same purpose                                               |
| `SECURE_CONTENT_TYPE_NOSNIFF`       | `HYPERDJANGO_SECURE_CONTENT_TYPE_NOSNIFF`          | Same purpose                                               |
| `SECURE_REFERRER_POLICY`            | `HYPERDJANGO_SECURE_REFERRER_POLICY`               | Same purpose                                               |
| `SECURE_PROXY_SSL_HEADER`           | `HYPERDJANGO_SECURE_PROXY_SSL_HEADER`              | String header name instead of tuple                        |
| `SECURE_REDIRECT_EXEMPT`            | `HYPERDJANGO_SECURE_REDIRECT_EXEMPT`               | Same purpose                                               |
| `SECURE_SSL_HOST`                   | `HYPERDJANGO_SECURE_SSL_HOST`                      | Same purpose                                               |
| `SECURE_CROSS_ORIGIN_OPENER_POLICY` | `HYPERDJANGO_SECURE_CROSS_ORIGIN_OPENER_POLICY`    | Same purpose                                               |
| `X_FRAME_OPTIONS`                   | `HYPERDJANGO_X_FRAME_OPTIONS`                      | Same purpose                                               |
| `PASSWORD_HASHERS`                  | `HYPERDJANGO_PASSWORD_HASHER`                      | Single string, `argon2id` only (not a list of class paths) |
| `LOGIN_URL`                         | `HYPERDJANGO_LOGIN_URL`                            | Same purpose                                               |
| `LOGIN_REDIRECT_URL`                | `HYPERDJANGO_LOGIN_REDIRECT_URL`                   | Same purpose                                               |
| `LOGOUT_REDIRECT_URL`               | `HYPERDJANGO_LOGOUT_REDIRECT_URL`                  | Same purpose                                               |
| `PASSWORD_RESET_TIMEOUT`            | `HYPERDJANGO_PASSWORD_RESET_TIMEOUT`               | Same purpose                                               |
| `AUTH_PASSWORD_VALIDATORS`          | `HYPERDJANGO_AUTH_PASSWORD_VALIDATORS`             | Same purpose                                               |
| `LOGGING`                           | `HYPERDJANGO_LOG_LEVEL` + `HYPERDJANGO_LOG_FORMAT` | Simplified: level + format instead of dict config          |
| `APPEND_SLASH`                      | `HYPERDJANGO_APPEND_SLASH`                         | Same purpose                                               |
| `PREPEND_WWW`                       | `HYPERDJANGO_PREPEND_WWW`                          | Same purpose                                               |
| `USE_X_FORWARDED_HOST`              | `HYPERDJANGO_USE_X_FORWARDED_HOST`                 | Same purpose                                               |
| `USE_X_FORWARDED_PORT`              | `HYPERDJANGO_USE_X_FORWARDED_PORT`                 | Same purpose                                               |
| `ADMINS`                            | `HYPERDJANGO_ADMINS`                               | Same purpose                                               |
| `MANAGERS`                          | `HYPERDJANGO_MANAGERS`                             | Same purpose                                               |
| `DISALLOWED_USER_AGENTS`            | `HYPERDJANGO_DISALLOWED_USER_AGENTS`               | Same purpose                                               |
| `FILE_UPLOAD_MAX_MEMORY_SIZE`       | `HYPERDJANGO_FILE_UPLOAD_MAX_MEMORY_SIZE`          | Same purpose                                               |
| `DATA_UPLOAD_MAX_NUMBER_FIELDS`     | `HYPERDJANGO_DATA_UPLOAD_MAX_NUMBER_FIELDS`        | Same purpose                                               |
| `FILE_UPLOAD_TEMP_DIR`              | `HYPERDJANGO_FILE_UPLOAD_TEMP_DIR`                 | Same purpose                                               |
| `FILE_UPLOAD_PERMISSIONS`           | `HYPERDJANGO_FILE_UPLOAD_PERMISSIONS`              | Same purpose                                               |
| `FILE_UPLOAD_DIRECTORY_PERMISSIONS` | `HYPERDJANGO_FILE_UPLOAD_DIRECTORY_PERMISSIONS`    | Same purpose                                               |
| `LANGUAGE_CODE`                     | `HYPERDJANGO_LANGUAGE_CODE`                        | Same purpose                                               |
| `TIME_ZONE`                         | `HYPERDJANGO_TIME_ZONE`                            | Same purpose                                               |
| `USE_TZ`                            | `HYPERDJANGO_USE_TZ`                               | Same purpose                                               |
| `DATE_FORMAT`                       | `HYPERDJANGO_DATE_FORMAT`                          | Same purpose                                               |
| `DATETIME_FORMAT`                   | `HYPERDJANGO_DATETIME_FORMAT`                      | Same purpose                                               |
| `TIME_FORMAT`                       | `HYPERDJANGO_TIME_FORMAT`                          | Same purpose                                               |
| `DATE_INPUT_FORMATS`                | `HYPERDJANGO_DATE_INPUT_FORMATS`                   | Same purpose                                               |
| `DATETIME_INPUT_FORMATS`            | `HYPERDJANGO_DATETIME_INPUT_FORMATS`               | Same purpose                                               |
| `MESSAGE_LEVEL`                     | `HYPERDJANGO_MESSAGE_LEVEL`                        | Same purpose                                               |
| `MESSAGE_TAGS`                      | `HYPERDJANGO_MESSAGE_TAGS`                         | Same purpose                                               |

### Key Differences from Django

1. **Single database URL** instead of Django's `DATABASES` dict -- PostgreSQL only via pg.zig
2. **PostgreSQL UNLOGGED tables for caching** -- use `CACHE_BACKEND = "database"`
3. **No class paths for backends** -- use simple strings (`"smtp"`, `"console"`, `"memory"`)
4. **argon2id only** -- no weaker password hashers (bcrypt, pbkdf2, scrypt)
5. **Environment-first** -- all settings configurable via `HYPER_*` env vars and `.env` files
6. **Flat settings** -- no nested dicts (each setting is a single value with its own name)
7. **Built-in validation** -- `validate_settings()` checks types, ranges, and choices at startup
