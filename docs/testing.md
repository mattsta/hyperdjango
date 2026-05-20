# Testing

HyperDjango provides a complete testing toolkit: an in-process HTTP client, a
base test class with database transaction isolation, WebSocket testing utilities,
and a rich set of assertion helpers. No server is started, no sockets are opened
-- every request is dispatched directly to your application in the same process.

```python
from hyperdjango.testing import TestClient, TestCase, TestWebSocket
```

---

## TestClient

`TestClient` is a synchronous HTTP client that dispatches requests directly to a
`HyperApp` instance. It handles cookie persistence, authentication headers,
JSON serialization, form encoding, and multipart file uploads automatically.

### Creating a Client

```python
from hyperdjango.testing import TestClient
from myapp import app

client = TestClient(app)
```

The `app` argument is a `HyperApp` instance. The client stores cookies across
requests (just like a browser), so session-based authentication works without
any extra configuration.

### Constructor

```python
TestClient(app)
```

| Parameter | Type       | Description                               |
| --------- | ---------- | ----------------------------------------- |
| `app`     | `HyperApp` | The application instance to test against. |

The client initializes with an empty cookie jar and no default headers.

---

## HTTP Methods

Every method returns a `TestResponse` object (documented below). All methods
are synchronous -- the client handles the async-to-sync bridge internally.

### `get(path, headers=None, **kwargs)`

Send a GET request.

```python
def get(
    self,
    path: str,
    headers: dict[str, str] | None = None,
    **kwargs,
) -> TestResponse
```

| Parameter | Type                     | Default  | Description                                  |
| --------- | ------------------------ | -------- | -------------------------------------------- |
| `path`    | `str`                    | required | URL path, optionally including query string. |
| `headers` | `dict[str, str] \| None` | `None`   | Additional request headers.                  |

Query parameters can be included directly in the path:

```python
# Query string in the path
resp = client.get("/api/users?page=2&sort=name")
assert resp.ok

# Path without query
resp = client.get("/api/users")
```

**Example -- basic GET:**

```python
@app.get("/hello")
async def hello(request):
    return {"message": "Hello, World!"}


resp = client.get("/hello")
assert resp.status == 200
assert resp.json() == {"message": "Hello, World!"}
```

**Example -- GET with custom headers:**

```python
resp = client.get(
    "/api/data",
    headers={
        "Accept": "application/json",
        "X-Request-ID": "test-123",
    },
)
assert resp.ok
```

---

### `post(path, json=None, data=None, headers=None, **kwargs)`

Send a POST request. Supports JSON bodies, form-encoded data, raw bytes, and
multipart file uploads.

```python
def post(
    self,
    path: str,
    json: Any | None = None,
    data: dict[str, str] | bytes | str | None = None,
    headers: dict[str, str] | None = None,
    **kwargs,
) -> TestResponse
```

| Parameter | Type                           | Default  | Description                                                     |
| --------- | ------------------------------ | -------- | --------------------------------------------------------------- |
| `path`    | `str`                          | required | URL path.                                                       |
| `json`    | `Any \| None`                  | `None`   | Body serialized as JSON. Sets `Content-Type: application/json`. |
| `data`    | `dict \| bytes \| str \| None` | `None`   | Form data (dict), raw bytes, or raw string body.                |
| `headers` | `dict[str, str] \| None`       | `None`   | Additional request headers.                                     |
| `files`   | `dict[str, tuple] \| None`     | `None`   | Multipart file uploads (passed via `**kwargs`).                 |

When `json` is provided, it takes precedence over `data`. When `data` is a dict,
the body is form-encoded with `Content-Type: application/x-www-form-urlencoded`.

**Example -- POST with JSON:**

```python
@app.post("/api/users")
async def create_user(request):
    body = await request.json()
    return Response.json({"id": 1, "name": body["name"]}, status=201)


resp = client.post("/api/users", json={"name": "Alice", "email": "alice@example.com"})
assert resp.status == 201
assert resp.json()["name"] == "Alice"
```

**Example -- POST with form data:**

```python
resp = client.post(
    "/login",
    data={
        "username": "admin",
        "password": "secret",
    },
)
assert resp.status == 200
```

**Example -- POST with raw bytes:**

```python
resp = client.post(
    "/api/raw",
    data=b"\x00\x01\x02",
    headers={
        "Content-Type": "application/octet-stream",
    },
)
```

---

### `put(path, json=None, data=None, headers=None, **kwargs)`

Send a PUT request. Parameters are identical to `post()`.

```python
def put(
    self,
    path: str,
    json: Any | None = None,
    data: dict[str, str] | bytes | str | None = None,
    headers: dict[str, str] | None = None,
    **kwargs,
) -> TestResponse
```

| Parameter | Type                           | Default  | Description                 |
| --------- | ------------------------------ | -------- | --------------------------- |
| `path`    | `str`                          | required | URL path.                   |
| `json`    | `Any \| None`                  | `None`   | Body serialized as JSON.    |
| `data`    | `dict \| bytes \| str \| None` | `None`   | Form data or raw body.      |
| `headers` | `dict[str, str] \| None`       | `None`   | Additional request headers. |

**Example:**

```python
resp = client.put(
    "/api/users/1", json={"name": "Alice Updated", "email": "new@example.com"}
)
assert resp.ok
assert resp.json()["name"] == "Alice Updated"
```

---

### `patch(path, json=None, data=None, headers=None, **kwargs)`

Send a PATCH request for partial updates. Parameters are identical to `post()`.

```python
def patch(
    self,
    path: str,
    json: Any | None = None,
    data: dict[str, str] | bytes | str | None = None,
    headers: dict[str, str] | None = None,
    **kwargs,
) -> TestResponse
```

| Parameter | Type                           | Default  | Description                 |
| --------- | ------------------------------ | -------- | --------------------------- |
| `path`    | `str`                          | required | URL path.                   |
| `json`    | `Any \| None`                  | `None`   | Body serialized as JSON.    |
| `data`    | `dict \| bytes \| str \| None` | `None`   | Form data or raw body.      |
| `headers` | `dict[str, str] \| None`       | `None`   | Additional request headers. |

**Example:**

```python
resp = client.patch("/api/users/1", json={"email": "updated@example.com"})
assert resp.ok
```

---

### `delete(path, headers=None, **kwargs)`

Send a DELETE request.

```python
def delete(
    self,
    path: str,
    headers: dict[str, str] | None = None,
    **kwargs,
) -> TestResponse
```

| Parameter | Type                     | Default  | Description                 |
| --------- | ------------------------ | -------- | --------------------------- |
| `path`    | `str`                    | required | URL path.                   |
| `headers` | `dict[str, str] \| None` | `None`   | Additional request headers. |

**Example:**

```python
resp = client.delete("/api/users/1")
assert resp.status == 204
```

---

### `request(method, path, **kwargs)`

The low-level method that all HTTP method helpers delegate to. Use this for
custom HTTP methods or when you need full control.

```python
def request(
    self,
    method: str,
    path: str,
    json: Any | None = None,
    data: dict[str, str] | bytes | str | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    headers: dict[str, str] | None = None,
    query_string: str = "",
) -> TestResponse
```

| Parameter      | Type                           | Default  | Description                                             |
| -------------- | ------------------------------ | -------- | ------------------------------------------------------- |
| `method`       | `str`                          | required | HTTP method (`"GET"`, `"POST"`, `"OPTIONS"`, etc).      |
| `path`         | `str`                          | required | URL path. May include `?query_string`.                  |
| `json`         | `Any \| None`                  | `None`   | JSON-serializable body.                                 |
| `data`         | `dict \| bytes \| str \| None` | `None`   | Form data or raw body.                                  |
| `files`        | `dict[str, tuple] \| None`     | `None`   | Multipart file uploads.                                 |
| `headers`      | `dict[str, str] \| None`       | `None`   | Request headers (merged with defaults).                 |
| `query_string` | `str`                          | `""`     | Explicit query string (overridden if `?` is in `path`). |

**Body encoding priority:**

1. If `json` is provided, the body is `json.dumps(json).encode()` with `Content-Type: application/json`.
2. If `files` is provided, a multipart body is built containing both `data` fields and `files`.
3. If `data` is a `dict`, it is form-encoded with `Content-Type: application/x-www-form-urlencoded`.
4. If `data` is `bytes` or `str`, it is sent as-is.

**Example -- custom HTTP method:**

```python
resp = client.request("OPTIONS", "/api/users")
assert "Allow" in resp.headers
```

---

## TestResponse

Every client method returns a `TestResponse` object that wraps the raw
`Response` with convenience accessors.

### Attribute Reference

| Attribute / Method | Type             | Description                                        |
| ------------------ | ---------------- | -------------------------------------------------- |
| `status`           | `int`            | HTTP status code (e.g. `200`, `404`, `500`).       |
| `ok`               | `bool`           | `True` if status is in the `2xx` range.            |
| `body`             | `bytes`          | Raw response body as bytes.                        |
| `text()`           | `str`            | Body decoded as UTF-8 string.                      |
| `json()`           | `Any`            | Body parsed as JSON (returns `dict`, `list`, etc). |
| `headers`          | `dict[str, str]` | Response headers.                                  |

### Checking Status

```python
resp = client.get("/api/users")

# Boolean check
assert resp.ok  # True for any 2xx

# Exact status
assert resp.status == 200

# Check for specific error codes
resp = client.get("/nonexistent")
assert resp.status == 404
assert not resp.ok
```

### Reading the Body

```python
# As JSON
data = resp.json()
assert data["users"][0]["name"] == "Alice"

# As text
html = resp.text()
assert "<h1>Welcome</h1>" in html

# As raw bytes
raw = resp.body
assert raw.startswith(b"\x89PNG")
```

### Inspecting Headers

```python
resp = client.get("/api/data")
assert resp.headers["content-type"] == "application/json"
assert "x-request-id" in resp.headers
```

---

## Authentication Helpers

The `TestClient` provides multiple authentication strategies. Cookies persist
across requests automatically, so session-based auth works out of the box.

### Session Authentication (Cookie-Based)

Post to your login endpoint directly. Cookies are stored and sent on all
subsequent requests:

```python
# Login via your app's login endpoint
client.post("/login", json={"username": "admin", "password": "secret"})

# All subsequent requests include the session cookie
resp = client.get("/dashboard")
assert resp.ok

# Verify protected content is accessible
resp = client.get("/api/me")
assert resp.json()["username"] == "admin"
```

### `set_auth(token, scheme="Bearer")`

Set an `Authorization` header for all subsequent requests. Returns `self` for
chaining.

```python
def set_auth(self, token: str, scheme: str = "Bearer") -> TestClient
```

| Parameter | Type  | Default    | Description                  |
| --------- | ----- | ---------- | ---------------------------- |
| `token`   | `str` | required   | The token value.             |
| `scheme`  | `str` | `"Bearer"` | Authorization scheme prefix. |

**Example:**

```python
# Bearer token (default)
client.set_auth("eyJhbGciOi...")
resp = client.get("/api/protected")
# Sends: Authorization: Bearer eyJhbGciOi...

# Custom scheme
client.set_auth("my-token", scheme="Token")
resp = client.get("/api/protected")
# Sends: Authorization: Token my-token
```

### `set_api_key(key, header="x-api-key")`

Set an API key header for all subsequent requests. Returns `self` for chaining.

```python
def set_api_key(self, key: str, header: str = "x-api-key") -> TestClient
```

| Parameter | Type  | Default       | Description         |
| --------- | ----- | ------------- | ------------------- |
| `key`     | `str` | required      | The API key value.  |
| `header`  | `str` | `"x-api-key"` | Header name to use. |

**Example:**

```python
client.set_api_key("sk_live_abc123")
resp = client.get("/api/data")
# Sends: x-api-key: sk_live_abc123

# Custom header name
client.set_api_key("my-key", header="X-Custom-Auth")
# Sends: X-Custom-Auth: my-key
```

### `clear_auth()`

Remove all authentication headers (`authorization` and `x-api-key`). Returns
`self` for chaining.

```python
client.set_auth("token123")
client.get("/api/data")  # authenticated

client.clear_auth()
resp = client.get("/api/data")  # unauthenticated
assert resp.status == 401
```

### `login_oauth2(provider, user_data)`

Simulate a complete OAuth2 login without going through the redirect flow. This
creates a session directly with the given provider and user data, as if the user
completed the full OAuth2 authorization code exchange.

```python
def login_oauth2(self, provider: str, user_data: dict) -> TestClient
```

| Parameter   | Type   | Description                                         |
| ----------- | ------ | --------------------------------------------------- |
| `provider`  | `str`  | OAuth2 provider name (e.g. `"google"`, `"github"`). |
| `user_data` | `dict` | User profile data from the provider.                |

Requires `SessionAuth` middleware to be registered on the app.

**Example:**

```python
client.login_oauth2(
    "google",
    {
        "email": "alice@gmail.com",
        "name": "Alice",
        "sub": "google-id-123",
    },
)

# Session cookie is set -- subsequent requests are authenticated
resp = client.get("/dashboard")
assert resp.ok
assert resp.json()["email"] == "alice@gmail.com"
```

### `reset_cookies()`

Clear all stored cookies. This effectively logs the user out of session-based
authentication.

```python
client.reset_cookies()
resp = client.get("/dashboard")
assert resp.status == 401  # no longer authenticated
```

### Setting Custom Headers Per-Request

Any request method accepts a `headers` parameter that is merged with default
headers for that single request:

```python
# One-off custom header
resp = client.get(
    "/api/data",
    headers={
        "Authorization": "Bearer one-time-token",
        "Accept-Language": "fr",
    },
)

# Default headers still apply to other requests
resp = client.get("/api/other")
```

---

## File Upload Testing

Multipart file uploads use the `files` parameter on `post()` (or `request()`).
Each entry is a tuple of `(filename, content_bytes, content_type)`.

### Single File Upload

```python
resp = client.post(
    "/upload",
    files={
        "document": ("report.pdf", b"%PDF-1.4 content here", "application/pdf"),
    },
)
assert resp.status == 201
assert resp.json()["filename"] == "report.pdf"
```

### Multiple File Uploads

```python
resp = client.post(
    "/upload-many",
    files={
        "photo": ("sunset.jpg", b"\xff\xd8\xff\xe0...", "image/jpeg"),
        "document": ("notes.txt", b"Meeting notes...", "text/plain"),
    },
)
assert resp.ok
```

### Files with Form Fields

When `files` is provided alongside `data`, both are included in the multipart
body:

```python
resp = client.post(
    "/upload",
    data={"title": "Q4 Report", "category": "finance"},
    files={
        "attachment": (
            "q4.xlsx",
            spreadsheet_bytes,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    },
)
assert resp.status == 201
```

### File Upload Format Reference

The `files` parameter is a dict where:

| Key        | Type                     | Description                               |
| ---------- | ------------------------ | ----------------------------------------- |
| field name | `str`                    | The form field name for the file input.   |
| value      | `tuple[str, bytes, str]` | `(filename, content_bytes, content_type)` |

The client builds a proper `multipart/form-data` body with a random boundary,
so your handler receives the file exactly as it would from a real browser upload.

---

## WebSocket Testing

`TestWebSocket` is a mock WebSocket connection for testing `@app.websocket`
handlers without a real server or network connection.

### Creating a TestWebSocket

```python
from hyperdjango.testing import TestWebSocket

ws = TestWebSocket()
```

### TestWebSocket API

| Method / Property                   | Type                 | Description                                           |
| ----------------------------------- | -------------------- | ----------------------------------------------------- |
| `await accept()`                    | `None`               | Mark the WebSocket as accepted.                       |
| `await send_text(data)`             | `None`               | Send a text message (queued for inspection).          |
| `await send_bytes(data)`            | `None`               | Send a binary message (queued for inspection).        |
| `await receive_text()`              | `str`                | Pop the next message from the receive queue as text.  |
| `await receive_bytes()`             | `bytes`              | Pop the next message from the receive queue as bytes. |
| `await close(code=1000, reason="")` | `None`               | Mark the WebSocket as closed.                         |
| `feed(*messages)`                   | `None`               | Pre-load messages into the receive queue.             |
| `accepted`                          | `bool`               | `True` if `accept()` was called.                      |
| `closed`                            | `bool`               | `True` if `close()` was called.                       |
| `sent_messages`                     | `list[str \| bytes]` | All messages sent via `send_text`/`send_bytes`.       |

### `accept()`

Mark the connection as accepted. Your handler should call this before sending
or receiving messages.

```python
ws = TestWebSocket()
assert not ws.accepted

await ws.accept()
assert ws.accepted
```

### `feed(*messages)`

Pre-load messages into the receive queue. This is the primary way to simulate
incoming client messages. Call `feed()` before calling `receive_text()` or
`receive_bytes()`.

```python
ws = TestWebSocket()
ws.feed("Hello", "World")

msg1 = await ws.receive_text()  # "Hello"
msg2 = await ws.receive_text()  # "World"
```

Messages are consumed in FIFO order. Calling `receive_text()` or
`receive_bytes()` on an empty queue raises `RuntimeError`.

### `send_text(data)` / `send_bytes(data)`

Queue an outgoing message. After your handler runs, inspect `sent_messages` to
verify what was sent:

```python
ws = TestWebSocket()
await ws.accept()
await ws.send_text("echo: Hello")
await ws.send_bytes(b"\x00\x01")

assert ws.sent_messages == ["echo: Hello", b"\x00\x01"]
```

### `close(code=1000, reason="")`

Mark the WebSocket as closed with an optional close code and reason.

```python
await ws.close(code=1001, reason="going away")
assert ws.closed
```

### Full WebSocket Test Example

```python
from hyperdjango.testing import TestWebSocket


async def test_echo_handler():
    """Test a WebSocket echo handler."""
    ws = TestWebSocket()

    # Pre-load messages the handler will receive
    ws.feed("Hello", "World")

    # Simulate handler logic
    await ws.accept()
    msg1 = await ws.receive_text()
    await ws.send_text(f"echo: {msg1}")
    msg2 = await ws.receive_text()
    await ws.send_text(f"echo: {msg2}")
    await ws.close()

    # Verify
    assert ws.accepted
    assert ws.closed
    assert ws.sent_messages == ["echo: Hello", "echo: World"]
```

### Testing with Channels

When testing pub/sub with the channel layer, use `InMemoryChannelLayer` for
isolation:

```python
from hyperdjango.channels import Channel, ChannelGroup, InMemoryChannelLayer


async def test_channel_broadcast():
    layer = InMemoryChannelLayer()

    # Create channels
    ch1 = Channel("user-1", layer)
    ch2 = Channel("user-2", layer)

    # Create a group and subscribe both
    group = ChannelGroup("chat-room", layer)
    await group.subscribe(ch1)
    await group.subscribe(ch2)

    # Publish to the group
    await group.publish({"type": "message", "text": "Hello everyone"})

    # Both channels receive the message
    msg1 = await ch1.receive()
    msg2 = await ch2.receive()
    assert msg1["text"] == "Hello everyone"
    assert msg2["text"] == "Hello everyone"
```

---

## TestCase

`TestCase` is a base class that provides database transaction isolation and
assertion helpers. Each test method runs inside a PostgreSQL `SAVEPOINT` that is
rolled back when the test finishes -- full isolation without manual cleanup.

### Basic Usage

```python
from hyperdjango.testing import TestCase
from myapp import app


class TestUsers(TestCase):
    db_url = "postgres://localhost/mydb_test"
    app = app  # optional: creates self.client

    async def asyncSetUp(self):
        """Runs before each test (inside savepoint)."""
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(id SERIAL PRIMARY KEY, name TEXT, email TEXT)"
        )

    async def test_create(self):
        await self.db.execute(
            "INSERT INTO users (name, email) VALUES ($1, $2)",
            "Alice",
            "alice@example.com",
        )
        rows = await self.db.query("SELECT * FROM users")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Alice")

    async def test_isolation(self):
        """Previous test's INSERT was rolled back automatically."""
        rows = await self.db.query("SELECT * FROM users")
        self.assertEqual(len(rows), 0)


# Run all tests
TestUsers.run_all()
```

### Class Attributes

| Attribute | Type                 | Description                                                               |
| --------- | -------------------- | ------------------------------------------------------------------------- |
| `app`     | `HyperApp \| None`   | If set, `self.client` is created as a `TestClient(app)` for each test.    |
| `db_url`  | `str`                | PostgreSQL connection URL. Also reads `DATABASE_URL` env var as fallback. |
| `db`      | `Database \| None`   | Database connection, set automatically during test execution.             |
| `client`  | `TestClient \| None` | Test client, set automatically if `app` is provided.                      |

### Lifecycle Methods

Tests are discovered and executed in alphabetical order. The lifecycle is:

```
asyncSetUpClass()           -- once per class
    BEGIN                   -- outer transaction
    for each test method:
        SAVEPOINT sp_xxx    -- isolation boundary
        asyncSetUp()        -- per-test setup
        test_xxx()          -- the test itself
        asyncTearDown()     -- per-test cleanup
        ROLLBACK TO sp_xxx  -- undo all changes
    ROLLBACK                -- undo outer transaction
asyncTearDownClass()        -- once per class
```

#### `asyncSetUp(self)`

Runs before each test method, inside the savepoint. Use this to create tables,
insert seed data, or configure per-test state.

```python
async def asyncSetUp(self):
    await self.db.execute(
        "CREATE TABLE IF NOT EXISTS products "
        "(id SERIAL PRIMARY KEY, name TEXT, price NUMERIC)"
    )
    await self.db.execute(
        "INSERT INTO products (name, price) VALUES ($1, $2)",
        "Widget",
        9.99,
    )
```

#### `asyncTearDown(self)`

Runs after each test method, still inside the savepoint (before rollback). Use
this for any cleanup that needs to happen before the savepoint rollback.

```python
async def asyncTearDown(self):
    # Cleanup temporary files, close connections, etc.
    pass
```

#### `asyncSetUpClass(cls)`

Class method. Runs once before all tests in the class, before the outer
transaction begins.

```python
@classmethod
async def asyncSetUpClass(cls):
    # One-time expensive setup
    pass
```

#### `asyncTearDownClass(cls)`

Class method. Runs once after all tests in the class, after the outer
transaction is rolled back.

```python
@classmethod
async def asyncTearDownClass(cls):
    # One-time cleanup
    pass
```

### Running Tests

```python
# Run all test_ methods in the class
TestUsers.run_all()
```

`run_all()` returns a tuple of `(passed, failed, errors)`:

```python
passed, failed, errors = TestUsers.run_all()
assert failed == 0
```

Output:

```
  PASS: test_create
  PASS: test_isolation

Results: 2 passed, 0 failed
```

---

## Assertions

### Value Assertions

All assertion methods accept an optional `msg` parameter for custom error
messages.

| Method                           | Description               |
| -------------------------------- | ------------------------- |
| `assertEqual(a, b)`              | `a == b`                  |
| `assertNotEqual(a, b)`           | `a != b`                  |
| `assertTrue(val)`                | `val` is truthy           |
| `assertFalse(val)`               | `val` is falsy            |
| `assertIsNone(val)`              | `val is None`             |
| `assertIsNotNone(val)`           | `val is not None`         |
| `assertIn(member, container)`    | `member in container`     |
| `assertNotIn(member, container)` | `member not in container` |
| `assertGreater(a, b)`            | `a > b`                   |
| `assertGreaterEqual(a, b)`       | `a >= b`                  |
| `assertLess(a, b)`               | `a < b`                   |
| `assertIsInstance(obj, cls)`     | `isinstance(obj, cls)`    |

**Examples:**

```python
self.assertEqual(user["name"], "Alice")
self.assertNotEqual(resp.status, 500)
self.assertTrue(resp.ok)
self.assertFalse(resp.json().get("is_admin"))
self.assertIsNone(resp.json().get("deleted_at"))
self.assertIsNotNone(resp.json().get("id"))
self.assertIn("alice", [u["name"] for u in users])
self.assertNotIn("admin", public_roles)
self.assertGreater(len(rows), 0)
self.assertGreaterEqual(resp.json()["count"], 10)
self.assertLess(resp.json()["latency_ms"], 100)
self.assertIsInstance(resp.json(), dict)
```

### Exception Assertion

Use `assertRaises` as a context manager to verify that specific exceptions
are raised:

```python
with self.assertRaises(ValueError):
    int("not_a_number")

with self.assertRaises(KeyError):
    d = {}
    _ = d["missing"]

# Access the exception after the block
ctx = self.assertRaises(ValueError)
with ctx:
    int("bad")
assert "invalid literal" in str(ctx.exception)
```

### Response Assertions

Purpose-built assertions for testing HTTP responses. These provide clear error
messages that include the actual status code and body content.

| Method                            | Description                                          |
| --------------------------------- | ---------------------------------------------------- |
| `assertOk(resp)`                  | Status is `2xx`.                                     |
| `assertStatus(resp, code)`        | Exact status code match.                             |
| `assertContains(resp, text)`      | Response body contains the given text.               |
| `assertNotContains(resp, text)`   | Response body does not contain the given text.       |
| `assertRedirects(resp, url)`      | Status is `3xx` and `Location` header matches `url`. |
| `assertJsonEqual(resp, expected)` | JSON body equals the expected dict/list.             |

**Examples:**

```python
resp = client.get("/api/users")
self.assertOk(resp)
self.assertStatus(resp, 200)

resp = client.get("/nonexistent")
self.assertStatus(resp, 404)
self.assertContains(resp, "not found")
self.assertNotContains(resp, "server error")

resp = client.get("/old-page")
self.assertRedirects(resp, "/new-page")

resp = client.get("/api/user/1")
self.assertJsonEqual(resp, {"id": 1, "name": "Alice"})
```

---

## Database Test Patterns

### Savepoint-Based Isolation

Every test method in a `TestCase` subclass runs inside a PostgreSQL `SAVEPOINT`.
When the test finishes (whether it passes or fails), the savepoint is rolled
back. This means:

- Each test starts with a clean database state.
- Tests cannot interfere with each other.
- No manual cleanup (`DELETE FROM ...`) is needed.
- Schema changes (`CREATE TABLE`, `ALTER TABLE`) inside tests are also rolled back.

```python
class TestOrders(TestCase):
    db_url = "postgres://localhost/mydb_test"

    async def asyncSetUp(self):
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS orders "
            "(id SERIAL PRIMARY KEY, total NUMERIC, status TEXT)"
        )

    async def test_create_order(self):
        await self.db.execute(
            "INSERT INTO orders (total, status) VALUES ($1, $2)", 99.99, "pending"
        )
        rows = await self.db.query("SELECT * FROM orders")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")

    async def test_no_leakage(self):
        # The INSERT from test_create_order was rolled back
        rows = await self.db.query("SELECT * FROM orders")
        self.assertEqual(len(rows), 0)
```

### Testing with Real Data

HyperDjango tests use a real PostgreSQL database -- never mocks. This ensures
your SQL, constraints, indexes, and types are validated at test time.

```python
class TestConstraints(TestCase):
    db_url = "postgres://localhost/mydb_test"

    async def asyncSetUp(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                balance NUMERIC CHECK (balance >= 0)
            )
        """)

    async def test_unique_constraint(self):
        await self.db.execute(
            "INSERT INTO accounts (email, balance) VALUES ($1, $2)",
            "alice@example.com",
            100,
        )
        # Duplicate email raises an error
        with self.assertRaises(Exception):
            await self.db.execute(
                "INSERT INTO accounts (email, balance) VALUES ($1, $2)",
                "alice@example.com",
                200,
            )

    async def test_check_constraint(self):
        # Negative balance violates CHECK constraint
        with self.assertRaises(Exception):
            await self.db.execute(
                "INSERT INTO accounts (email, balance) VALUES ($1, $2)",
                "bob@example.com",
                -50,
            )
```

### Table Creation in Tests

Since savepoints roll back schema changes, you can create tables in
`asyncSetUp` and they will be cleaned up automatically:

```python
async def asyncSetUp(self):
    await self.db.execute("""
        CREATE TABLE IF NOT EXISTS temp_data (
            id SERIAL PRIMARY KEY,
            key TEXT NOT NULL,
            value JSONB
        )
    """)
```

Use `CREATE TABLE IF NOT EXISTS` to handle the case where the table already
exists from the class-level outer transaction.

### Transaction Testing

The test savepoint does not prevent you from testing your own transaction logic.
Nested `atomic()` blocks create additional savepoints within the test savepoint:

```python
class TestTransactions(TestCase):
    db_url = "postgres://localhost/mydb_test"

    async def asyncSetUp(self):
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS ledger (id SERIAL PRIMARY KEY, amount NUMERIC)"
        )

    async def test_atomic_rollback(self):
        """Test that a failed atomic block rolls back its changes."""
        await self.db.execute("INSERT INTO ledger (amount) VALUES ($1)", 100)

        try:
            async with self.db.atomic():
                await self.db.execute("INSERT INTO ledger (amount) VALUES ($1)", 200)
                raise ValueError("Simulated failure")
        except ValueError:
            pass

        # The 200 insert was rolled back, but the 100 insert remains
        rows = await self.db.query("SELECT * FROM ledger")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 100)
```

**Key rules:**

- Do not call `BEGIN` or `COMMIT` in tests -- transactions are managed automatically.
- Nested `atomic()` blocks create additional savepoints within the test savepoint.
- Schema changes (`CREATE TABLE`, `ALTER TABLE`) inside tests are also rolled back.

---

## Fixtures and Test Data

### Creating Test Data in asyncSetUp

The simplest approach is to insert data directly in `asyncSetUp`. Since each
test gets a fresh savepoint, the data is always clean:

```python
class TestUserAPI(TestCase):
    db_url = "postgres://localhost/mydb_test"
    app = app

    async def asyncSetUp(self):
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(id SERIAL PRIMARY KEY, name TEXT, email TEXT, role TEXT)"
        )
        # Seed data for every test
        await self.db.execute(
            "INSERT INTO users (name, email, role) VALUES ($1, $2, $3)",
            "Alice",
            "alice@example.com",
            "admin",
        )
        await self.db.execute(
            "INSERT INTO users (name, email, role) VALUES ($1, $2, $3)",
            "Bob",
            "bob@example.com",
            "user",
        )

    async def test_list_users(self):
        resp = self.client.get("/api/users")
        self.assertOk(resp)
        self.assertEqual(len(resp.json()), 2)

    async def test_filter_admins(self):
        resp = self.client.get("/api/users?role=admin")
        self.assertOk(resp)
        users = resp.json()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["name"], "Alice")
```

### Factory Functions

For complex test data, use factory functions that create objects with sensible
defaults:

```python
async def create_user(db, name="Test User", email=None, role="user"):
    """Create a test user with defaults."""
    email = email or f"{name.lower().replace(' ', '.')}@example.com"
    await db.execute(
        "INSERT INTO users (name, email, role) VALUES ($1, $2, $3)",
        name,
        email,
        role,
    )
    rows = await db.query("SELECT * FROM users WHERE email = $1", email)
    return rows[0]


async def create_order(db, user_id, total=99.99, status="pending"):
    """Create a test order with defaults."""
    await db.execute(
        "INSERT INTO orders (user_id, total, status) VALUES ($1, $2, $3)",
        user_id,
        total,
        status,
    )
    rows = await db.query(
        "SELECT * FROM orders WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
        user_id,
    )
    return rows[0]


class TestOrderWorkflow(TestCase):
    db_url = "postgres://localhost/mydb_test"

    async def asyncSetUp(self):
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(id SERIAL PRIMARY KEY, name TEXT, email TEXT, role TEXT)"
        )
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS orders "
            "(id SERIAL PRIMARY KEY, user_id INT, total NUMERIC, status TEXT)"
        )

    async def test_user_places_order(self):
        user = await create_user(self.db, name="Alice")
        order = await create_order(self.db, user["id"], total=150.00)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["total"], 150.00)
```

### Shared Setup with asyncSetUpClass

For expensive setup that should only happen once per test class (not per test),
use `asyncSetUpClass`:

```python
class TestReporting(TestCase):
    db_url = "postgres://localhost/mydb_test"

    @classmethod
    async def asyncSetUpClass(cls):
        """Create schema once for all tests."""
        # This runs before the outer BEGIN transaction
        pass

    async def asyncSetUp(self):
        """Seed per-test data (rolled back after each test)."""
        for i in range(100):
            await self.db.execute(
                "INSERT INTO events (name, timestamp) VALUES ($1, NOW())",
                f"event-{i}",
            )
```

---

## Testing with TestClient and TestCase Together

When `app` is set on a `TestCase` subclass, a fresh `TestClient` is created for
each test method, providing both HTTP testing and database access:

```python
class TestFullStack(TestCase):
    db_url = "postgres://localhost/mydb_test"
    app = app

    async def asyncSetUp(self):
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS items "
            "(id SERIAL PRIMARY KEY, name TEXT, price NUMERIC)"
        )

    async def test_create_and_read(self):
        # Create via API
        resp = self.client.post("/api/items", json={"name": "Widget", "price": 9.99})
        self.assertStatus(resp, 201)
        item_id = resp.json()["id"]

        # Verify in database
        rows = await self.db.query("SELECT * FROM items WHERE id = $1", item_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Widget")

        # Read back via API
        resp = self.client.get(f"/api/items/{item_id}")
        self.assertOk(resp)
        self.assertJsonEqual(resp, {"id": item_id, "name": "Widget", "price": 9.99})

    async def test_delete(self):
        # Seed directly in DB
        await self.db.execute(
            "INSERT INTO items (id, name, price) VALUES ($1, $2, $3)",
            1,
            "Gadget",
            19.99,
        )

        # Delete via API
        resp = self.client.delete("/api/items/1")
        self.assertStatus(resp, 204)

        # Verify gone
        rows = await self.db.query("SELECT * FROM items WHERE id = $1", 1)
        self.assertEqual(len(rows), 0)
```

---

## Cookie Management

The `TestClient` persists cookies across requests, just like a browser. Cookies
set via `Set-Cookie` response headers are automatically stored and sent on
subsequent requests.

```python
# Login sets a session cookie
resp = client.post("/login", json={"username": "admin", "password": "secret"})
# Session cookie is now stored

# Subsequent requests include the cookie
resp = client.get("/dashboard")
assert resp.ok

# Inspect cookies directly
print(client._cookies)

# Clear all cookies (effectively logging out)
client.reset_cookies()
resp = client.get("/dashboard")
assert resp.status == 401
```

### Cookie Expiration

When a response sets a cookie with `Max-Age=0`, the client removes it:

```python
# Logout endpoint sets Max-Age=0
resp = client.post("/logout")
# Session cookie is now removed

resp = client.get("/dashboard")
assert resp.status == 401
```

---

## Running Tests with pytest

While `TestCase.run_all()` provides a standalone runner, you can also use
pytest with standard `assert` statements and the `TestClient`:

```python
import pytest
from myapp import app
from hyperdjango.testing import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def test_homepage(client):
    resp = client.get("/")
    assert resp.ok
    assert "Welcome" in resp.text()


def test_api_create_user(client):
    resp = client.post("/api/users", json={"name": "Alice"})
    assert resp.status == 201
    assert resp.json()["name"] == "Alice"


def test_not_found(client):
    resp = client.get("/nonexistent")
    assert resp.status == 404


def test_auth_required(client):
    resp = client.get("/api/protected")
    assert resp.status == 401

    client.set_auth("valid-token")
    resp = client.get("/api/protected")
    assert resp.ok
```

Run with:

```bash
uv run pytest tests/test_api.py -v
```

---

## Test Runner

HyperDjango includes a parallel test runner (`hyper-test`) that classifies tests by their declared marker and runs them with maximum concurrency.

### Usage

```bash
uv run hyper-test                    # Run all tests (parallel)
uv run hyper-test rest admin         # Filter by pattern
uv run hyper-test --list             # Show all tests with classification
uv run hyper-test --serial           # Force sequential execution
uv run hyper-test --scripts-only     # Only scripts/test_*.py
uv run hyper-test --pytest-only      # Only tests/ pytest suites
```

### Classification

Every `scripts/test_*.py` declares its resource kind with a `# hyper-test: <kind>`
marker; the marker is the sole source of the kind, and a file without one is a
loud classification error (enforced by `scripts/check_test_markers.py`):

| Kind            | Description                        | Concurrency                               |
| --------------- | ---------------------------------- | ----------------------------------------- |
| **unit**        | No DB, no server                   | 50 workers                                |
| **db_isolated** | Reads `DATABASE_URL` env var       | 20 workers, own database                  |
| **db_django**   | Uses `DJANGO_SETTINGS_MODULE`      | 20 workers, own database via `PGDATABASE` |
| **db_shared**   | Hardcodes database name            | Sequential                                |
| **e2e**         | Starts HTTP server via `AppRunner` | 13 workers, serialized by app DB          |

Orthogonal per-file markers tune scheduling without changing the kind:
`# hyper-test-timeout: <seconds>` (budget override), `# hyper-test-concurrency: low`
(dedicated width-2 lane for starvation-sensitive files), and
`# hyper-test-flaky: <reason>` (quarantine: one retry on failure, tallied in
the summary; the reason text is mandatory). See [Test Harness](test-harness.md)
for the full taxonomy, the shared `testkit` assertion/e2e API, and the test
environment contract.

Each `db_isolated` and `db_django` test gets its own empty PostgreSQL database created at startup (`createdb hd_tN_PID`) and dropped after the test completes. This eliminates all cross-test data conflicts.

E2E tests that share an app database (e.g., multiple HyperNews tests) are serialized with each other to avoid DDL race conditions, but run concurrently with tests using other databases.

Every run also writes a machine-readable summary (`logs/test_runs/<run>.json`)
recording each file's kind, duration, retries, and effective timeout — the
artifact CI trends and the first place to look when a run is slow or flaky.

### Performance

The parallel runner executes 18,800+ tests across 455 files in under two minutes. All tests run in parallel — zero sequential. The speedup comes from:

- asyncio subprocess management (no ProcessPoolExecutor overhead)
- Per-test database isolation (all DB tests can run in parallel)
- Semaphore-based concurrency control per test category
- `HYPER_TEST_PARALLEL=1` env var for performance threshold awareness

### Process Cleanup

- On Ctrl-C: all child processes are killed immediately
- `AppRunner` kills stale processes on the test port before starting
- Server subprocesses use `start_new_session=True` for process group cleanup
- `SIGTERM` first, `SIGKILL` fallback after 5 seconds

---

## Quick Reference

### TestClient Methods

| Method                                            | Description                        |
| ------------------------------------------------- | ---------------------------------- |
| `get(path, headers=None)`                         | Send GET request.                  |
| `post(path, json=None, data=None, headers=None)`  | Send POST request.                 |
| `put(path, json=None, data=None, headers=None)`   | Send PUT request.                  |
| `patch(path, json=None, data=None, headers=None)` | Send PATCH request.                |
| `delete(path, headers=None)`                      | Send DELETE request.               |
| `request(method, path, **kwargs)`                 | Send request with any HTTP method. |
| `set_auth(token, scheme="Bearer")`                | Set Authorization header.          |
| `set_api_key(key, header="x-api-key")`            | Set API key header.                |
| `clear_auth()`                                    | Remove auth headers.               |
| `login_oauth2(provider, user_data)`               | Simulate OAuth2 login.             |
| `reset_cookies()`                                 | Clear all cookies.                 |

### TestResponse Attributes

| Attribute | Type    | Description         |
| --------- | ------- | ------------------- |
| `status`  | `int`   | HTTP status code.   |
| `ok`      | `bool`  | `True` if `2xx`.    |
| `body`    | `bytes` | Raw body.           |
| `text()`  | `str`   | UTF-8 decoded body. |
| `json()`  | `Any`   | Parsed JSON body.   |
| `headers` | `dict`  | Response headers.   |

### TestCase Lifecycle

| Method                 | Scope | Description                               |
| ---------------------- | ----- | ----------------------------------------- |
| `asyncSetUpClass()`    | Class | Runs once before all tests.               |
| `asyncSetUp()`         | Test  | Runs before each test (inside savepoint). |
| `asyncTearDown()`      | Test  | Runs after each test (before rollback).   |
| `asyncTearDownClass()` | Class | Runs once after all tests.                |

### TestWebSocket Methods

| Method                      | Description               |
| --------------------------- | ------------------------- |
| `await accept()`            | Accept the connection.    |
| `await send_text(data)`     | Send text message.        |
| `await send_bytes(data)`    | Send binary message.      |
| `await receive_text()`      | Receive text from queue.  |
| `await receive_bytes()`     | Receive bytes from queue. |
| `await close(code, reason)` | Close the connection.     |
| `feed(*messages)`           | Pre-load receive queue.   |
| `accepted`                  | Connection accepted?      |
| `closed`                    | Connection closed?        |
| `sent_messages`             | List of sent messages.    |

---

## Hypothesis Property-Based Testing

HyperDjango uses [Hypothesis](https://hypothesis.readthedocs.io/) for property-based fuzz testing across all critical subsystems. Unlike hand-written tests that check specific inputs, hypothesis generates thousands of random inputs and verifies invariant properties hold for ALL of them.

### Running Fuzz Tests

```bash
# Run all fuzz suites (CI mode — ~30s total)
uv run hyper-test fuzz

# Run a specific fuzz suite
uv run python scripts/test_signing_fuzz.py
uv run python scripts/test_json_fuzz.py

# Run with full iterations (benchmarking mode)
HYPER_BENCH_FULL=1 uv run python scripts/test_where_node_benchmark.py
```

### Fuzz Test Suites (20 suites, 163 tests)

#### Tier 1 — Security Boundaries

| Suite                 | File                      | Tests | What It Proves                                                                                           |
| --------------------- | ------------------------- | ----- | -------------------------------------------------------------------------------------------------------- |
| **Token signing**     | `test_signing_fuzz.py`    | 13    | encode/decode roundtrip for ANY data; ANY bit-flip → reject; key rotation; wrong key → reject            |
| **JSON parser**       | `test_json_fuzz.py`       | 8     | Zig SIMD json_loads/json_dumps roundtrip for ANY Python object; stdlib parity; unicode; deep nesting     |
| **Template engine**   | `test_template_fuzz.py`   | 10    | render_string never crashes; filter chains produce strings; autoescape prevents XSS; if/else/for correct |
| **Serializer fields** | `test_serializer_fuzz.py` | 13    | DateTime/Date/Time/UUID/Decimal/Email/Choice/IP roundtrip; invalid input → ValueError                    |
| **HMAC cursor**       | `test_cursor_fuzz.py`     | 8     | Cursor sign/verify roundtrip for all types; ANY payload byte tamper → reject; garbage → reject           |

#### Tier 2 — Core Platform Correctness

| Suite                 | File                           | Tests | What It Proves                                                                                             |
| --------------------- | ------------------------------ | ----- | ---------------------------------------------------------------------------------------------------------- |
| **WhereNode cache**   | `test_where_node_fuzz.py`      | 6     | Cache hit == cache miss for ANY filters; compile $N count == values; Q structural key; fast params == tree |
| **ORM lookups**       | `test_lookups_fuzz.py`         | 7     | resolve_lookup valid SQL; node matches lookup; bind_params count; transform chains; LIKE values            |
| **SIMD batch**        | `test_simd_validation_fuzz.py` | 7     | batch int/string validation matches per-element scalar for ANY inputs                                      |
| **URL router**        | `test_router_fuzz.py`          | 5     | register → resolve roundtrip; param extraction; no collision; unregistered → None                          |
| **RBAC hierarchy**    | `test_rbac_fuzz.py`            | 6     | Parent→child inheritance; direct perms; revoke; multi-group union; deep hierarchy; no default perms        |
| **Mixin composition** | `test_mixin_fuzz.py`           | 7     | SoftDelete/Versioned cache key differs; fast params == tree; cache hit == miss; SQL correctness            |
| **Form validation**   | `test_forms_fuzz.py`           | 6     | Valid data accepted; invalid rejected; choice validation; missing required field                           |

#### Tier 3 — Adversarial Web Security

| Suite                 | File                         | Tests | What It Proves                                                                                             |
| --------------------- | ---------------------------- | ----- | ---------------------------------------------------------------------------------------------------------- |
| **HTTP input**        | `test_http_input_fuzz.py`    | 16    | Query string/cookie/path/header with ANY input → no crash; null bytes; path traversal; html_escape         |
| **Template sandbox**  | `test_sandbox_fuzz.py`       | 6     | 17 known Jinja2 CVE escape patterns blocked; dunder access blocked; multi-step chains blocked              |
| **SQL injection**     | `test_sql_injection_fuzz.py` | 7     | ALL SQL injection payloads parameterized in filter/Q/where_raw; never interpolated into SQL                |
| **CSRF tokens**       | `test_csrf_fuzz.py`          | 8     | Token roundtrip; ANY mutation → reject; truncated → reject; garbage → reject; wrong secret → reject        |
| **Redirects/headers** | `test_redirect_fuzz.py`      | 8     | Redirect with ANY URL → no crash; evil URLs handled; set_cookie no crash; JSON response no crash           |
| **Multipart**         | `test_multipart_fuzz.py`     | 7     | Form field roundtrip; file upload roundtrip; boundary in content; empty body; multiple fields; large files |
| **Password hashing**  | `test_password_fuzz.py`      | 4     | hash/verify roundtrip for ANY password (including unicode); wrong password → reject; salt uniqueness       |
| **String ops**        | `test_string_ops_fuzz.py`    | 7     | html_escape == stdlib; url_encode == stdlib; url_decode roundtrip; xor_bytes roundtrip                     |

#### Infrastructure

| Suite              | File                            | Tests | What It Proves                                                                                                                |
| ------------------ | ------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Zig s# audit**   | `test_zig_s_hash_audit.py`      | 7     | ALL Zig native functions handle non-UTF-8 input from ASGI latin-1 decode                                                      |
| **Free-threading** | `test_free_threading_stress.py` | 10    | 24 threads × 1000 ops: compiled cache, lookups, LocMemCache, WhereNode, model registry, signals, QuerySet, templates, signing |

### How to Write a New Fuzz Test

Each fuzz test proves a **property** — an invariant that must hold for ALL inputs:

```python
from hypothesis import given, settings
from hypothesis import strategies as st


@given(value=st.text(max_size=100))
@settings(max_examples=500, deadline=2000)
def test_roundtrip(value):
    """encode(x) → decode() == x for ANY string."""
    encoded = my_encode(value)
    decoded = my_decode(encoded)
    assert decoded == value
```

**Key patterns:**

| Pattern              | Use For                          | Example                                   |
| -------------------- | -------------------------------- | ----------------------------------------- |
| **Roundtrip**        | Serialization, encoding, hashing | `decode(encode(x)) == x`                  |
| **Tamper rejection** | Crypto, signing, HMAC            | Flip any byte → decode returns None       |
| **Crash resilience** | Parsers, input handling          | `parse(any_garbage)` → no crash           |
| **Parity**           | Native vs stdlib                 | `zig_func(x) == python_func(x)`           |
| **Parameterization** | SQL safety                       | Injection payload in params, never in SQL |

**Strategies for common types:**

```python
# SQL injection payloads
st.sampled_from(["' OR '1'='1", "'; DROP TABLE users; --", ...])

# Adversarial strings (null bytes, CRLF, path traversal)
st.one_of(st.text(), st.sampled_from(["\x00", "\r\n", "../../etc/passwd"]))

# JSON-compatible objects (recursive)
st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text()),
    lambda c: st.one_of(st.lists(c), st.dictionaries(st.text(), c)),
)

# Valid email addresses
st.builds(
    lambda l, d, t: f"{l}@{d}.{t}",
    st.text(min_size=1, max_size=20, alphabet="abcdef"),
    st.text(min_size=1, max_size=10, alphabet="abcdef"),
    st.sampled_from(["com", "org", "net"]),
)
```

### When Hypothesis Finds a Bug

1. **Read the full traceback** — identify exactly which line crashes
2. **Fix the system** — never hack the test to skip the failing input
3. **Add a hardcoded regression test** for the exact failing input
4. **Rerun the fuzz suite** — verify the fix works for all random inputs
5. **Run full test suite** — verify the fix doesn't break anything else
