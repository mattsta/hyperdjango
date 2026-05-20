# Sessions

Server-side session storage backed by PostgreSQL UNLOGGED tables for high performance. No JWT -- sessions are stored server-side with only a signed session ID in the cookie.

## Quick Start

```python
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore

# Create session store (production: PostgreSQL UNLOGGED table)
session_store = DatabaseSessionStore(db, max_age=86400)
await session_store.ensure_table()

# Add session middleware to app
app.use(SessionAuth(secret="your-secret-key", store=session_store))
```

The `SessionAuth` middleware reads the signed session cookie on every request, validates it, and attaches `request.user` and `request.session_id` to the request object.

## Session Stores

HyperDjango provides two session backends. Both share the same API surface.

### InMemorySessionStore (Development)

Fast, single-process storage. Sessions are lost on restart.

```python
from hyperdjango.auth.sessions import InMemorySessionStore

store = InMemorySessionStore(max_age=86400)  # 24 hours

# Uses SortedList for O(log n) expiry cleanup
# Uses user_id index for O(1) user session lookups
```

### DatabaseSessionStore (Production)

PostgreSQL UNLOGGED table for multi-server coordination.

```python
from hyperdjango.auth.db_sessions import DatabaseSessionStore

store = DatabaseSessionStore(db, max_age=86400)
await store.ensure_table()
```

**Constructor parameters:**

| Parameter | Type       | Default  | Description                                     |
| --------- | ---------- | -------- | ----------------------------------------------- |
| `db`      | `Database` | required | The database connection                         |
| `max_age` | `int`      | `86400`  | Session lifetime in seconds (default: 24 hours) |

## DatabaseSessionStore Full API

### create(data) -> str

Create a new session. Returns a cryptographically random session ID (via `secrets.token_urlsafe(32)`).

```python
session_id = await store.create(
    {
        "user_id": user.id,
        "username": user.name,
        "role": "admin",
    }
)
# Returns: "Xk9f2m_L7dA..." (43-character URL-safe token)
```

The `data` dict is stored as JSONB in PostgreSQL. pg.zig returns native Python dicts on read -- no `json.loads` overhead. The session automatically expires after `max_age` seconds from creation.

If `data` contains a `user_id` or `id` key, that value is extracted and stored in the indexed `user_id` column for efficient per-user lookups. If `data` contains a `_session_hash` key, it is stored in the `session_hash` column for auth hash verification.

### get(session_id) -> dict | None

Retrieve session data by ID. Returns `None` if the session has expired or does not exist.

```python
data = await store.get(session_id)
# Returns: {"user_id": 1, "username": "alice", "role": "admin"}
# Returns: None if expired or not found
```

The query filters `WHERE expires_at > NOW()` so expired sessions are never returned even if they have not been cleaned up yet.

### update(session_id, data)

Replace session data and extend the expiry timer. The expiry is reset to `NOW() + max_age`.

```python
await store.update(
    session_id,
    {
        "user_id": 1,
        "username": "alice",
        "role": "admin",
        "theme": "dark",
    },
)
```

### delete(session_id)

Delete a single session immediately.

```python
await store.delete(session_id)
```

### invalidate_for_user(user_id)

Delete all sessions for a specific user. Use this for "log out everywhere" functionality.

```python
await store.invalidate_for_user(user.id)
```

### invalidate_by_hash(user_id, valid_hash)

Delete all sessions for a user where the `session_hash` column does not match `valid_hash`. Used for selective invalidation on password change -- only sessions created before the password change are destroyed.

```python
from hyperdjango.auth.sessions import get_session_auth_hash

new_hash = get_session_auth_hash(user.password_hash, secret)
await store.invalidate_by_hash(user.id, new_hash)
```

### get_user_sessions(user_id) -> list[dict]

List all active (non-expired) sessions for a user, ordered by creation time descending.

```python
sessions = await store.get_user_sessions(user.id)
# [
#     {"session_id": "abc...", "data": {...}, "created_at": ..., "expires_at": ...},
#     {"session_id": "def...", "data": {...}, "created_at": ..., "expires_at": ...},
# ]
```

Use this to build a "manage your sessions" UI where users can view and revoke individual sessions.

### touch(session_id)

Extend the session expiry without changing the data. Call this on active requests to implement sliding expiration.

```python
await store.touch(session_id)
# Resets expires_at to NOW() + max_age
```

### count() -> int

Count active (non-expired) sessions.

```python
active = await store.count()
# 42
```

### cleanup()

Delete all expired sessions from the database. Call this periodically from a background task.

```python
# Run every hour
@app.task(schedule="0 * * * *")
async def cleanup_sessions():
    await store.cleanup()
```

### ensure_table()

Create the sessions UNLOGGED table and indexes if they do not exist. Call once at startup.

```python
await store.ensure_table()
```

This creates the table, the `idx_sessions_expires` index on `expires_at`, and the `idx_sessions_user` index on `user_id`.

## Session Data Storage (JSONB)

Session data is stored in a PostgreSQL `JSONB` column. This means:

- Arbitrary nested data structures (dicts, lists, strings, numbers, booleans, null)
- Native indexing with GIN indexes if needed for querying session contents
- pg.zig returns JSONB as native Python dicts -- zero deserialization overhead
- Data is serialized via `fast_json_dumps` (native Zig SIMD JSON) on write

```python
# Store complex data
session_id = await store.create(
    {
        "user_id": user.id,
        "permissions": ["read", "write", "admin"],
        "preferences": {
            "theme": "dark",
            "language": "en",
            "notifications": True,
        },
    }
)

# Retrieved as native Python dict
data = await store.get(session_id)
theme = data["preferences"]["theme"]  # "dark"
```

## UNLOGGED Table Schema

Sessions use a PostgreSQL **UNLOGGED** table. UNLOGGED tables skip Write-Ahead Log (WAL) writes, providing 2-3x faster writes than regular tables. Data survives normal operation and restarts but is lost on a PostgreSQL crash. This is acceptable for sessions since users simply re-authenticate.

```sql
CREATE UNLOGGED TABLE IF NOT EXISTS hyper_sessions (
    session_id  VARCHAR(64) PRIMARY KEY,
    data        JSONB NOT NULL DEFAULT '{}',
    user_id     INTEGER,
    session_hash VARCHAR(64) DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- Indexes for efficient cleanup and user lookups
CREATE INDEX idx_sessions_expires ON hyper_sessions (expires_at);
CREATE INDEX idx_sessions_user ON hyper_sessions (user_id);
```

**Benefits of UNLOGGED tables for sessions:**

- No WAL writes means 2-3x faster INSERT/UPDATE/DELETE
- No replication lag (sessions are local to the PostgreSQL instance)
- Reduced disk I/O and WAL archive size
- Multiple app servers coordinate via the shared table
- Sessions are ephemeral data -- crash loss is acceptable

## Cookie Configuration

The `SessionAuth` middleware sets a secure signed cookie. The cookie contains only the signed session ID -- all session data lives server-side.

### SessionAuth Constructor

```python
SessionAuth(
    secret="your-secret-key",  # HMAC signing key (required)
    cookie_name="session",  # Cookie name (default: "session")
    store=session_store,  # Session backend (default: InMemorySessionStore)
    secure_cookie=True,  # Set Secure flag (HTTPS only)
    cookie_httponly=None,  # HttpOnly flag; None → SESSION_COOKIE_HTTPONLY
    cookie_samesite=None,  # SameSite policy; None → SESSION_COOKIE_SAMESITE
    get_user=get_user_fn,  # Async callback for auth hash verification
    verify_auth_hash=True,  # Enable session auth hash checking
)
```

| Parameter          | Type           | Default                | Description                                                                     |
| ------------------ | -------------- | ---------------------- | ------------------------------------------------------------------------------- |
| `secret`           | `str`          | required               | HMAC-SHA256 key for signing session IDs                                         |
| `cookie_name`      | `str`          | `"session"`            | Name of the session cookie                                                      |
| `store`            | session store  | `InMemorySessionStore` | Backend for session data                                                        |
| `secure_cookie`    | `bool`         | `True`                 | Secure-by-default floor; pass `False` to defer to `SESSION_COOKIE_SECURE`       |
| `cookie_httponly`  | `bool \| None` | `None`                 | `HttpOnly` flag; `None` defers to `SESSION_COOKIE_HTTPONLY` (default `True`)    |
| `cookie_samesite`  | `str \| None`  | `None`                 | `SameSite` policy; `None` defers to `SESSION_COOKIE_SAMESITE` (default `"Lax"`) |
| `get_user`         | callable       | `None`                 | `async (user_id) -> user_dict` for hash verification                            |
| `verify_auth_hash` | `bool`         | `True`                 | Verify session hash on each request                                             |

### Cookie Attributes

When sessions are created via `login()` or `login_async()`, the following cookie attributes are set:

| Attribute  | Value                 | Purpose                                                 |
| ---------- | --------------------- | ------------------------------------------------------- |
| `HttpOnly` | `True`                | Cookie not accessible via JavaScript (XSS protection)   |
| `Secure`   | `True` (configurable) | Cookie only sent over HTTPS                             |
| `SameSite` | `Lax`                 | CSRF protection -- cookie sent on top-level navigations |
| `Max-Age`  | `store.max_age`       | Cookie expiry matches session expiry                    |
| `Path`     | `/`                   | Available on all paths                                  |

### Manual Cookie Configuration

If you need custom cookie settings instead of using the built-in `login()` method:

```python
cookie_parts = [
    f"session_id={signed_session_id}",
    "HttpOnly",  # Not accessible via JavaScript
    "Secure",  # HTTPS only
    "SameSite=Lax",  # CSRF protection
    f"Max-Age={86400}",  # 24 hours
    "Path=/",  # Available on all paths
    "Domain=.example.com",  # Share across subdomains
]
response.headers["Set-Cookie"] = "; ".join(cookie_parts)
```

## SessionAuth Middleware Setup

### Basic Setup

```python
from hyperdjango import HyperApp
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.auth.db_sessions import DatabaseSessionStore
from hyperdjango.database import Database

app = HyperApp()
db = Database("postgres://localhost/myapp")

session_store = DatabaseSessionStore(db, max_age=86400)
session_auth = SessionAuth(secret="your-secret-key", store=session_store)

app.use(session_auth)


@app.on("startup")
async def startup():
    await db.connect()
    await session_store.ensure_table()
```

### Setup with Auth Hash Verification

To automatically invalidate sessions when a user changes their password, provide a `get_user` callback:

```python
async def get_user_by_id(user_id):
    return await User.objects.get(id=user_id)


session_auth = SessionAuth(
    secret="your-secret-key",
    store=session_store,
    get_user=get_user_by_id,
    verify_auth_hash=True,
)
```

On every request, the middleware:

1. Reads the `session` cookie
2. Verifies the HMAC signature
3. Loads session data from the store
4. If `verify_auth_hash` is enabled and `_session_auth_hash` is in the session data, fetches the current user and verifies the hash matches
5. Sets `request.user` and `request.session_id`

## Login Flow

### Using login() / login_async()

The recommended approach uses the built-in `login()` or `login_async()` methods. These handle session fixation prevention, auth hash injection, and cookie setting in one call.

```python
@app.post("/login")
async def login(request):
    data = await request.json()
    user = await authenticate(data["username"], data["password"])
    if not user:
        raise HTTPException(401, "Invalid credentials")

    response = Response.json({"user": user.name})

    # Creates session, sets signed cookie, injects auth hash
    await session_auth.login_async(
        response,
        user_data={
            "user_id": user.id,
            "username": user.name,
            "password_hash": user.password_hash,
        },
        request=request,  # Pass request for session fixation prevention
    )
    return response
```

**Session fixation prevention:** When `request` is passed to `login_async()`, any pre-existing session is destroyed before creating a new one. This prevents an attacker from fixing a session ID before the user authenticates.

**Auth hash injection:** If `user_data` contains a `password_hash` field, `login_async()` automatically computes an HMAC-SHA256 hash and stores it as `_session_auth_hash` in the session data. This hash is verified on subsequent requests to detect password changes.

### Using login() (Sync Stores)

For `InMemorySessionStore` (sync), use the non-async `login()`:

```python
session_id = session_auth.login(response, user_data, request=request)
```

## Logout Flow

```python
@app.post("/logout")
async def logout(request):
    response = Response.json({"logged_out": True})
    if request.session_id:
        await session_auth.logout_async(response, request.session_id)
    return response
```

This deletes the session from the store and clears the cookie on the response.

## Reading Session Data in Views

After the `SessionAuth` middleware runs, every request has:

- `request.user` -- the session data dict (or `None` if not authenticated)
- `request.session_id` -- the raw session ID string (or `None`)

```python
@app.get("/profile")
async def profile(request):
    if not request.user:
        raise HTTPException(401, "Not authenticated")
    return {
        "user_id": request.user["user_id"],
        "username": request.user["username"],
    }
```

## Session Invalidation on Password Change

When a user changes their password, you should invalidate all their existing sessions. There are two strategies:

### Strategy 1: Delete All Sessions

Simple and immediate. All other devices are logged out instantly.

```python
@app.post("/change-password")
async def change_password(request):
    user = request.user
    new_password = (await request.json())["new_password"]

    # Update password in database
    hashed = hash_password(new_password)
    await User.objects.filter(id=user["user_id"]).update(password_hash=hashed)

    # Invalidate ALL sessions for this user
    await session_store.invalidate_for_user(user["user_id"])

    # Create a new session for the current request
    response = Response.json({"changed": True})
    await session_auth.login_async(
        response,
        {
            "user_id": user["user_id"],
            "username": user["username"],
            "password_hash": hashed,
        },
    )
    return response
```

### Strategy 2: Lazy Hash Verification

Sessions are not deleted immediately. Instead, the `SessionAuth` middleware checks the `_session_auth_hash` on each request. When the password changes, the hash no longer matches, and the old session is silently invalidated on next access.

This is automatic when `verify_auth_hash=True` and `get_user` is configured. No explicit invalidation call is needed.

### Strategy 3: Selective Hash Invalidation

Delete only sessions with the old hash, keeping the current session alive:

```python
from hyperdjango.auth.sessions import get_session_auth_hash

new_hash = get_session_auth_hash(new_password_hash, secret)
await session_store.invalidate_by_hash(user_id, new_hash)
```

## Session Auth Hash Security

The session auth hash is an HMAC-SHA256 computed from the user's password hash and the application secret:

```python
from hyperdjango.auth.sessions import get_session_auth_hash, verify_session_auth_hash

# Compute hash (stored in session on login)
session_hash = get_session_auth_hash(user.password_hash, app_secret)

# Verify hash (checked on each request by middleware)
is_valid = verify_session_auth_hash(stored_hash, user.password_hash, app_secret)
```

**How it works:**

1. On login, `HMAC-SHA256(password_hash, secret)` is computed and stored as `_session_auth_hash` in the session
2. On each request, the middleware recomputes the hash from the user's current `password_hash`
3. If the hashes do not match (constant-time comparison), the password has changed and the session is invalidated
4. The user must re-authenticate

This is inspired by Django's `get_session_auth_hash()` mechanism.

## Concurrent Session Management

### Viewing Active Sessions

Build a "manage sessions" page where users can see and revoke their sessions:

```python
@app.get("/settings/sessions")
async def list_sessions(request):
    sessions = await session_store.get_user_sessions(request.user["user_id"])
    return {
        "sessions": [
            {
                "id": s["session_id"][:8] + "...",  # Truncate for display
                "created": s["created_at"],
                "expires": s["expires_at"],
                "current": s["session_id"] == request.session_id,
            }
            for s in sessions
        ]
    }
```

### Revoking a Specific Session

```python
@app.post("/settings/sessions/{session_id}/revoke")
async def revoke_session(request, session_id: str):
    # Verify the session belongs to the current user
    sessions = await session_store.get_user_sessions(request.user["user_id"])
    session_ids = [s["session_id"] for s in sessions]
    if session_id not in session_ids:
        raise HTTPException(404, "Session not found")
    if session_id == request.session_id:
        raise HTTPException(400, "Cannot revoke current session")

    await session_store.delete(session_id)
    return {"revoked": True}
```

### Revoking All Other Sessions

```python
@app.post("/settings/sessions/revoke-all")
async def revoke_all_sessions(request):
    current_session_id = request.session_id
    sessions = await session_store.get_user_sessions(request.user["user_id"])
    for s in sessions:
        if s["session_id"] != current_session_id:
            await session_store.delete(s["session_id"])
    return {"revoked": len(sessions) - 1}
```

## Flash Messages via Sessions

Flash messages are one-time messages that survive a single redirect. They are stored in the session and cleared after retrieval.

```python
from hyperdjango.messages import (
    add_message,
    get_messages,
    success,
    error,
    info,
    warning,
)


@app.post("/items/create")
async def create_item(request):
    item = await Item.objects.create(name="New Item")
    success(request, "Item created!")
    return Response.redirect("/items/")


@app.get("/items/")
async def list_items(request):
    messages = get_messages(request)  # Retrieves AND clears
    items = await Item.objects.all()
    return render(
        request,
        "items/list.html",
        {
            "items": items,
            "messages": messages,
        },
    )
```

### Message Levels

| Function                            | Level       | Typical Use          |
| ----------------------------------- | ----------- | -------------------- |
| `success(request, text)`            | `"success"` | Operation completed  |
| `error(request, text)`              | `"error"`   | Operation failed     |
| `info(request, text)`               | `"info"`    | Informational notice |
| `warning(request, text)`            | `"warning"` | Non-critical warning |
| `add_message(request, level, text)` | custom      | Any custom level     |

### MessageMiddleware

For template-based applications, add the `MessageMiddleware` to automatically load messages into every request:

```python
from hyperdjango.messages import MessageMiddleware

app.add_middleware(MessageMiddleware)
```

Then in templates:

```html
{% for msg in messages %}
<div class="alert alert-{{ msg.level }}">{{ msg.text }}</div>
{% endfor %}
```

### Message Data Structure

Each message is a dict:

```python
{"level": "success", "text": "Item created!"}
```

Messages support custom levels -- pass any string as the level to `add_message()`.

## Periodic Session Cleanup

Expired sessions remain in the database until explicitly cleaned up. Schedule periodic cleanup to prevent table bloat:

```python
@app.task(schedule="0 * * * *")  # Every hour
async def cleanup_expired_sessions():
    await session_store.cleanup()
```

The `cleanup()` method runs `DELETE FROM hyper_sessions WHERE expires_at < NOW()`.

For high-traffic applications, you may also want to set `max_age` on the `DatabaseSessionStore` to a shorter duration and clean up more frequently:

```python
# 4-hour sessions, cleanup every 30 minutes
store = DatabaseSessionStore(db, max_age=14400)


@app.task(schedule="*/30 * * * *")
async def cleanup_sessions():
    await store.cleanup()
```

## Sliding Expiration

To extend session expiry on every active request (sliding window), use `touch()`:

```python
class SlidingSessionMiddleware:
    async def __call__(self, request, call_next):
        response = await call_next(request)
        if request.session_id:
            await session_store.touch(request.session_id)
        return response


app.use(SlidingSessionMiddleware())
```

This resets `expires_at` to `NOW() + max_age` on every request without rewriting the session data.

## Testing Sessions

Use `InMemorySessionStore` in tests for fast, isolated session testing:

```python
from hyperdjango.auth.sessions import SessionAuth, InMemorySessionStore
from hyperdjango.testing import TestClient

store = InMemorySessionStore(max_age=3600)
auth = SessionAuth(secret="test-secret", store=store, secure_cookie=False)
app.use(auth)

client = TestClient(app)

# Login
response = client.post("/login", json={"username": "alice", "password": "secret"})
assert response.status == 200

# Session cookie is automatically persisted by TestClient
response = client.get("/profile")
assert response.json()["username"] == "alice"

# Logout
response = client.post("/logout")
assert store.count() == 0
```
