# Testing Guide

This guide covers writing and running tests for HyperDjango applications.
It walks through the TestClient, TestCase with database isolation, authentication
testing, fixtures, the built-in test runner, and pytest integration.

For the API reference, see [testing.md](testing.md).

---

## Table of Contents

- [TestClient](#testclient)
- [HTTP Methods and Request Options](#http-methods-and-request-options)
- [Authentication Helpers](#authentication-helpers)
- [Cookie Persistence](#cookie-persistence)
- [TestCase with DB Isolation](#testcase-with-db-isolation)
- [Assertion Helpers](#assertion-helpers)
- [OAuth2 Testing](#oauth2-testing)
- [File Upload Testing](#file-upload-testing)
- [WebSocket Testing](#websocket-testing)
- [Fixtures](#fixtures)
- [The hyper-test Runner](#the-hyper-test-runner)
- [Writing Test Scripts](#writing-test-scripts)
- [pytest Integration](#pytest-integration)
- [Migration from Django Tests](#migration-from-django-tests)

---

## TestClient

`TestClient` dispatches requests directly to your `HyperApp` instance in the
same process. No server is started, no sockets are opened. Cookies persist
across requests automatically.

```python
from hyperdjango import HyperApp, Response
from hyperdjango.testing import TestClient

app = HyperApp()


@app.get("/api/products")
async def list_products(request):
    products = await Product.objects.filter(active=True).all()
    return Response.json([{"id": p.id, "name": p.name} for p in products])


@app.post("/api/products")
async def create_product(request):
    data = await request.json()
    product = Product(**data)
    await product.save()
    return Response.json({"id": product.id}, status=201)


client = TestClient(app)


def test_list_products():
    resp = client.get("/api/products")
    assert resp.status == 200
    assert isinstance(resp.json(), list)


def test_create_product():
    resp = client.post(
        "/api/products",
        json={
            "name": "Widget",
            "price": 9.99,
            "stock": 100,
        },
    )
    assert resp.status == 201
    assert "id" in resp.json()
```

---

## HTTP Methods and Request Options

The client supports all standard HTTP methods:

```python
client = TestClient(app)

# GET with query string
resp = client.get("/search", query_string="q=widgets&page=2")

# POST with JSON body
resp = client.post("/api/users", json={"name": "Alice", "email": "alice@example.com"})

# POST with form data
resp = client.post("/login", data={"username": "alice", "password": "secret123"})

# PUT (full replacement)
resp = client.put(
    "/api/users/42", json={"name": "Alice Updated", "email": "alice@new.com"}
)

# PATCH (partial update)
resp = client.patch("/api/users/42", json={"name": "Alice Updated"})

# DELETE
resp = client.delete("/api/users/42")

# Custom headers
resp = client.get("/api/data", headers={"accept-language": "fr"})
```

### TestResponse

Every method returns a `TestResponse` with these attributes and methods:

```python
resp = client.get("/api/products")

resp.status  # int: HTTP status code (200, 404, etc.)
resp.headers  # dict: response headers
resp.body  # bytes: raw response body
resp.json()  # Parse body as JSON
resp.text()  # Decode body as UTF-8 string
resp.ok  # True if status is 2xx
```

---

## Authentication Helpers

### Bearer Token / API Key

```python
client = TestClient(app)

# Set Bearer token for all subsequent requests
client.set_auth("my-jwt-or-session-token")
# Sends: Authorization: Bearer my-jwt-or-session-token

# Use a custom scheme
client.set_auth("my-token", scheme="Token")
# Sends: Authorization: Token my-token

# Set API key header
client.set_api_key("ak_live_abc123")
# Sends: X-Api-Key: ak_live_abc123

# Custom header name
client.set_api_key("my-key", header="x-custom-auth")
# Sends: X-Custom-Auth: my-key

# Clear all auth
client.clear_auth()
```

### Session-Based Auth

Since cookies persist automatically, session login works naturally:

```python
def test_session_auth():
    client = TestClient(app)

    # Login via form post
    resp = client.post(
        "/login",
        data={
            "username": "alice",
            "password": "correct-password",
        },
    )
    assert resp.status == 302  # Redirect after login

    # Subsequent requests carry the session cookie
    resp = client.get("/dashboard")
    assert resp.status == 200
    assert "Welcome, Alice" in resp.text()
```

---

## Cookie Persistence

The TestClient stores cookies across requests, just like a browser:

```python
def test_cookie_flow():
    client = TestClient(app)

    # Server sets a cookie
    resp = client.get("/set-preference?theme=dark")
    assert resp.status == 200

    # Cookie is automatically sent on next request
    resp = client.get("/dashboard")
    assert "dark-theme" in resp.text()

    # Inspect stored cookies
    assert "theme" in client._cookies
```

---

## TestCase with DB Isolation

`TestCase` wraps each test in a database transaction that is rolled back
after the test completes. This gives you full database isolation with zero
cleanup overhead.

```python
from hyperdjango.testing import TestCase


class TestUserWorkflow(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"

    async def test_create_and_query_user(self):
        # INSERT happens inside a savepoint
        await self.db.execute(
            "INSERT INTO users (name, email) VALUES ($1, $2)",
            "Alice",
            "alice@example.com",
        )

        rows = await self.db.query(
            "SELECT name, email FROM users WHERE name = $1", "Alice"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Alice")
        self.assertEqual(rows[0]["email"], "alice@example.com")
        # After this test, the INSERT is rolled back automatically

    async def test_user_not_persisted(self):
        # This test sees a clean database -- Alice does not exist
        rows = await self.db.query("SELECT * FROM users WHERE name = $1", "Alice")
        self.assertEqual(len(rows), 0)

    async def test_update_workflow(self):
        await self.db.execute(
            "INSERT INTO users (name, email) VALUES ($1, $2)",
            "Bob",
            "bob@example.com",
        )
        await self.db.execute(
            "UPDATE users SET email = $1 WHERE name = $2",
            "bob@new.com",
            "Bob",
        )
        rows = await self.db.query("SELECT email FROM users WHERE name = $1", "Bob")
        self.assertEqual(rows[0]["email"], "bob@new.com")
```

### How Isolation Works

1. Before each test: `BEGIN` + `SAVEPOINT test_savepoint`
2. The test runs (all queries go through the same transaction)
3. After each test: `ROLLBACK TO SAVEPOINT test_savepoint` + `RELEASE`
4. After all tests: `ROLLBACK`

This is much faster than truncating tables or recreating the database
between tests.

### Using the TestClient Inside TestCase

TestCase provides a pre-configured `self.client`:

```python
class TestProductAPI(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"

    async def test_create_and_list(self):
        resp = self.client.post(
            "/api/products",
            json={
                "name": "Widget",
                "price": 9.99,
            },
        )
        self.assertEqual(resp.status, 201)

        resp = self.client.get("/api/products")
        self.assertEqual(resp.status, 200)
        products = resp.json()
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name"], "Widget")
```

---

## Assertion Helpers

TestCase includes assertion methods for common patterns:

```python
class TestAssertions(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"

    async def test_basic_assertions(self):
        self.assertEqual(1 + 1, 2)
        self.assertNotEqual("a", "b")
        self.assertTrue(True)
        self.assertFalse(False)
        self.assertIsNone(None)
        self.assertIsNotNone("something")
        self.assertIn("a", ["a", "b", "c"])
        self.assertNotIn("d", ["a", "b", "c"])
        self.assertGreater(5, 3)
        self.assertLess(3, 5)

    async def test_response_assertions(self):
        resp = self.client.get("/api/products")
        self.assertStatus(resp, 200)
        self.assertJsonEqual(resp, [])
        self.assertHeaderContains(resp, "content-type", "application/json")

    async def test_exception_assertion(self):
        with self.assertRaises(ValueError):
            int("not-a-number")
```

---

## OAuth2 Testing

Test OAuth2 login flows without real provider redirects using
`login_oauth2()`:

```python
def test_oauth2_google_login():
    client = TestClient(app)

    # Simulate completing the Google OAuth2 flow
    client.login_oauth2(
        "google",
        {
            "id": "google-12345",
            "email": "alice@gmail.com",
            "name": "Alice Smith",
            "picture": "https://lh3.googleusercontent.com/...",
        },
    )

    # The client now has a valid session cookie
    resp = client.get("/dashboard")
    assert resp.status == 200
    assert "Alice Smith" in resp.text()


def test_oauth2_github_login():
    client = TestClient(app)

    client.login_oauth2(
        "github",
        {
            "id": "gh-67890",
            "login": "alicesmith",
            "email": "alice@github.com",
            "name": "Alice Smith",
        },
    )

    resp = client.get("/api/me")
    assert resp.status == 200
    me = resp.json()
    assert me["oauth2_provider"] == "github"
```

This creates a session directly with the `oauth2_provider` set, bypassing
the full authorization code flow while still exercising your session
middleware and user resolution logic.

---

## File Upload Testing

Test multipart file uploads using the `files` parameter:

```python
def test_document_upload():
    client = TestClient(app)

    resp = client.post(
        "/api/documents",
        files={
            "file": ("report.pdf", b"%PDF-1.4 fake content", "application/pdf"),
        },
        data={
            "title": "Q4 Report",
            "category": "finance",
        },
    )
    assert resp.status == 201
    doc = resp.json()
    assert doc["filename"] == "report.pdf"
    assert doc["title"] == "Q4 Report"


def test_multiple_files():
    client = TestClient(app)

    resp = client.post(
        "/api/gallery/upload",
        files={
            "photo1": ("cat.jpg", b"\xff\xd8\xff\xe0...", "image/jpeg"),
            "photo2": ("dog.png", b"\x89PNG...", "image/png"),
        },
    )
    assert resp.status == 201
```

The `files` dict maps field names to `(filename, content_bytes, content_type)`
tuples. When `files` is provided, the request is automatically encoded as
`multipart/form-data`. The `data` dict provides additional form fields.

---

## WebSocket Testing

Test WebSocket handlers with `TestWebSocket`:

```python
from hyperdjango.testing import TestWebSocket


def test_websocket_echo():
    ws = TestWebSocket(app, "/ws/echo")
    ws.connect()
    ws.send_json({"type": "ping", "data": "hello"})
    msg = ws.receive_json()
    assert msg["type"] == "pong"
    assert msg["data"] == "hello"
    ws.close()
```

---

## Fixtures

Load test data from JSON fixture files using `loaddata` and `dumpdata`:

```python
# Dump current database state to a fixture file
# hyper dumpdata users products > fixtures/test_data.json

# Load fixture data in tests
class TestWithFixtures(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"
    fixtures = ["fixtures/test_data.json"]

    async def test_fixture_data_loaded(self):
        users = await self.db.query("SELECT * FROM users")
        self.assertGreater(len(users), 0)
```

Fixture files are JSON arrays of objects with `table` and `fields` keys:

```json
[
  {
    "table": "users",
    "fields": {
      "id": 1,
      "name": "Alice",
      "email": "alice@example.com",
      "is_active": true
    }
  },
  {
    "table": "users",
    "fields": {
      "id": 2,
      "name": "Bob",
      "email": "bob@example.com",
      "is_active": true
    }
  }
]
```

---

## The hyper-test Runner

HyperDjango includes a built-in test runner invoked via the `hyper-test`
entrypoint:

```bash
# Run all tests (177 test files)
uv run hyper-test

# Run tests matching patterns
uv run hyper-test rest              # All REST API tests
uv run hyper-test admin forms       # Admin + forms tests
uv run hyper-test template cache    # Multiple patterns

# List available test files without running
uv run hyper-test --list

# Run a specific test file
uv run hyper-test tests/test_forms.py
```

### Output Format

The runner prints results per test file with PASS/FAIL status:

```
tests/test_forms.py ........................... 90/90 PASS
tests/test_admin.py .......................... 101/101 PASS
tests/test_templates.py ...................... 770/770 PASS

====================================================
Total: 961 tests, 0 failures, 0 errors
Time: 2.34s
```

Failed tests show the assertion error, expected vs actual values, and the
test function name.

---

## Writing Test Scripts

For standalone test scripts (outside the hyper-test runner), follow the
PASS/FAIL pattern that the runner expects:

```python
"""Test product validation logic."""

from hyperdjango.forms import Form, CharField, IntegerField, DecimalField


class ProductForm(Form):
    name = CharField(min_length=3, max_length=200)
    price = DecimalField()
    stock = IntegerField(min_value=0)


passed = 0
failed = 0


def test(name, condition):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name}")


# --- Tests ---

form = ProductForm(data={"name": "Widget", "price": "9.99", "stock": "10"})
test("valid form passes", form.is_valid())
test("cleaned name is string", form.cleaned_data["name"] == "Widget")

form = ProductForm(data={"name": "AB", "price": "9.99", "stock": "10"})
test("short name fails", not form.is_valid())
test("error on name field", "name" in form.errors)

form = ProductForm(data={"name": "Widget", "price": "9.99", "stock": "-1"})
test("negative stock fails", not form.is_valid())

# --- Summary ---

total = passed + failed
if failed:
    print(f"FAIL: {passed}/{total} passed, {failed} failed")
else:
    print(f"PASS: {passed}/{total} passed")
```

Run with:

```bash
uv run hyper-test tests/test_product_validation.py
```

---

## pytest Integration

HyperDjango tests work with pytest. The TestClient is synchronous, so
standard pytest functions work directly:

```python
# tests/test_api.py
import pytest
from hyperdjango.testing import TestClient
from myapp import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status == 200
    assert resp.json()["status"] == "ok"


def test_create_product(client):
    resp = client.post(
        "/api/products",
        json={
            "name": "Widget",
            "price": 9.99,
        },
    )
    assert resp.status == 201


def test_auth_required(client):
    resp = client.get("/api/admin/users")
    assert resp.status == 401


def test_with_auth(client):
    client.set_api_key("test-key-12345")
    resp = client.get("/api/admin/users")
    assert resp.status == 200
```

For async tests with database isolation, use `pytest-asyncio`:

```python
import pytest
import pytest_asyncio
from hyperdjango.database import Database


@pytest_asyncio.fixture
async def db():
    database = Database("postgres://localhost/myapp_test")
    await database.connect()
    await database.execute("BEGIN")
    await database.execute("SAVEPOINT test_sp")
    yield database
    await database.execute("ROLLBACK TO SAVEPOINT test_sp")
    await database.execute("ROLLBACK")
    await database.disconnect()


@pytest.mark.asyncio
async def test_user_creation(db):
    await db.execute(
        "INSERT INTO users (name, email) VALUES ($1, $2)",
        "Alice",
        "alice@example.com",
    )
    rows = await db.query("SELECT * FROM users WHERE name = $1", "Alice")
    assert len(rows) == 1
```

Run with:

```bash
uv run pytest tests/ -q
```

---

## Migration from Django Tests

| Django                             | HyperDjango                                  |
| ---------------------------------- | -------------------------------------------- |
| `from django.test import Client`   | `from hyperdjango.testing import TestClient` |
| `from django.test import TestCase` | `from hyperdjango.testing import TestCase`   |
| `self.client.get()`                | `self.client.get()` (same API)               |
| `response.status_code`             | `resp.status`                                |
| `response.json()`                  | `resp.json()` (same)                         |
| `response.content`                 | `resp.body` (bytes)                          |
| `self.assertContains()`            | `assert "text" in resp.text()`               |
| `self.assertRedirects()`           | `assert resp.status in (301, 302)`           |
| `TransactionTestCase`              | `TestCase` (savepoint isolation by default)  |
| `@override_settings`               | Not needed (no global settings object)       |
| `LiveServerTestCase`               | Not needed (TestClient is in-process)        |

The main difference is that HyperDjango's `TestCase` uses savepoint rollback
for all tests (equivalent to Django's `TestCase`), so every test sees a clean
database without the overhead of table truncation.

---

## Advanced Testing

### Testing with Transactions and Atomic Blocks

HyperDjango's `TestCase` wraps each test in a savepoint. Nested transactions
(via `atomic()`) work correctly inside tests because they create additional
savepoints within the test's transaction.

```python
from hyperdjango.database import atomic
from hyperdjango.testing import TestCase


class TestOrderWorkflow(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"

    async def test_atomic_commit(self):
        """Atomic blocks that succeed are visible within the test transaction."""
        async with atomic(self.db):
            await self.db.execute(
                "INSERT INTO orders (customer_id, total) VALUES ($1, $2)",
                1,
                99.99,
            )
            await self.db.execute(
                "UPDATE inventory SET stock = stock - 1 WHERE product_id = $1",
                42,
            )

        rows = await self.db.query("SELECT total FROM orders WHERE customer_id = $1", 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total"], 99.99)

    async def test_atomic_rollback(self):
        """Atomic blocks that raise an exception roll back their changes."""
        try:
            async with atomic(self.db):
                await self.db.execute(
                    "INSERT INTO orders (customer_id, total) VALUES ($1, $2)",
                    1,
                    50.00,
                )
                raise ValueError("Payment declined")
        except ValueError:
            pass

        rows = await self.db.query("SELECT * FROM orders WHERE customer_id = $1", 1)
        self.assertEqual(len(rows), 0)

    async def test_nested_savepoints(self):
        """Triple-nested atomic blocks create nested savepoints."""
        async with atomic(self.db):
            await self.db.execute(
                "INSERT INTO accounts (name, balance) VALUES ($1, $2)",
                "Alice",
                1000,
            )
            try:
                async with atomic(self.db):
                    await self.db.execute(
                        "UPDATE accounts SET balance = balance - 500 WHERE name = $1",
                        "Alice",
                    )
                    async with atomic(self.db):
                        await self.db.execute(
                            "UPDATE accounts SET balance = balance - 600 WHERE name = $1",
                            "Alice",
                        )
                        raise ValueError("Insufficient funds")
            except ValueError:
                pass

            rows = await self.db.query(
                "SELECT balance FROM accounts WHERE name = $1", "Alice"
            )
            self.assertEqual(rows[0]["balance"], 1000)
```

### Testing Async Views

All HyperDjango views are async, so testing them is straightforward. The
`TestClient` handles the async-to-sync bridge internally -- test functions
themselves can be either sync or async.

```python
from hyperdjango.testing import TestCase, TestClient


class TestAsyncViews(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"

    async def test_concurrent_writes(self):
        """Test that concurrent database operations work correctly."""
        import asyncio

        async def create_user(name: str):
            await self.db.execute(
                "INSERT INTO users (name, email) VALUES ($1, $2)",
                name,
                f"{name.lower()}@example.com",
            )

        await asyncio.gather(
            create_user("Alice"),
            create_user("Bob"),
            create_user("Charlie"),
        )

        rows = await self.db.query("SELECT COUNT(*) as cnt FROM users")
        self.assertEqual(rows[0]["cnt"], 3)

    async def test_streaming_response(self):
        """Test an SSE endpoint returns the correct content type."""
        resp = self.client.get("/api/events/stream")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
```

### Testing Middleware

Test that middleware modifies requests and responses correctly by sending
requests through the full middleware stack via `TestClient`.

```python
from hyperdjango import HyperApp
from hyperdjango.testing import TestClient
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)

app = HyperApp()
app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=True))
app.use(CORSMiddleware(origins=["https://myapp.com"]))


@app.get("/api/data")
async def get_data(request):
    return {"value": 42}


client = TestClient(app)


def test_timing_header_present():
    resp = client.get("/api/data")
    assert resp.status == 200
    assert "x-response-time" in resp.headers or "X-Response-Time" in resp.headers


def test_security_headers():
    resp = client.get("/api/data")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "strict-transport-security" in {k.lower() for k in resp.headers}


def test_cors_allows_configured_origin():
    resp = client.get("/api/data", headers={"origin": "https://myapp.com"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://myapp.com"


def test_cors_rejects_unknown_origin():
    resp = client.get("/api/data", headers={"origin": "https://evil.com"})
    assert resp.headers.get("Access-Control-Allow-Origin") != "https://evil.com"
```

### Testing Signals

Test that signals fire correctly by connecting a listener before the test
and checking its side effects.

```python
from hyperdjango.signals import post_save, pre_delete
from hyperdjango.testing import TestCase


class TestSignals(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"

    async def test_post_save_fires(self):
        """Verify post_save signal fires after model creation."""
        signal_log = []

        async def on_save(sender, instance, created, **kwargs):
            signal_log.append(
                {
                    "model": sender.__name__,
                    "created": created,
                    "pk": instance.id,
                }
            )

        post_save.connect(on_save)
        try:
            product = await Product.objects.create(
                name="Widget", sku="WDG-001", price=9.99
            )
            self.assertEqual(len(signal_log), 1)
            self.assertTrue(signal_log[0]["created"])
            self.assertEqual(signal_log[0]["pk"], product.id)

            product.price = 14.99
            await product.save()
            self.assertEqual(len(signal_log), 2)
            self.assertFalse(signal_log[1]["created"])
        finally:
            post_save.disconnect(on_save)

    async def test_pre_delete_fires(self):
        """Verify pre_delete signal fires before model deletion."""
        deleted_ids = []

        async def on_delete(sender, instance, **kwargs):
            deleted_ids.append(instance.id)

        pre_delete.connect(on_delete)
        try:
            product = await Product.objects.create(
                name="Gadget", sku="GDG-001", price=19.99
            )
            await product.delete()
            self.assertEqual(deleted_ids, [product.id])
        finally:
            pre_delete.disconnect(on_delete)
```

### Using Fixtures in Tests

Load test data from JSON fixture files. Fixtures are loaded inside the test
transaction and rolled back after each test, so the database stays clean.

```python
from hyperdjango.testing import TestCase


class TestProductReporting(TestCase):
    app = app
    db_url = "postgres://localhost/myapp_test"
    fixtures = ["fixtures/products.json", "fixtures/categories.json"]

    async def test_fixture_data_available(self):
        """Fixture data is loaded before the test runs."""
        products = await self.db.query("SELECT * FROM products")
        self.assertGreater(len(products), 0)

    async def test_aggregate_queries(self):
        """Run aggregate queries against fixture data."""
        rows = await self.db.query(
            "SELECT category, COUNT(*) as cnt, AVG(price) as avg_price "
            "FROM products GROUP BY category ORDER BY cnt DESC"
        )
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertGreater(row["cnt"], 0)
            self.assertGreater(row["avg_price"], 0)

    async def test_fixture_isolation(self):
        """Modifications to fixture data do not leak between tests."""
        await self.db.execute("DELETE FROM products")
        rows = await self.db.query("SELECT COUNT(*) as cnt FROM products")
        self.assertEqual(rows[0]["cnt"], 0)
        # Next test will still see the original fixture data
```

Fixture files follow this format:

```json
[
  {
    "table": "products",
    "fields": {
      "id": 1,
      "name": "Widget",
      "sku": "WDG-001",
      "price": 9.99,
      "category": "hardware"
    }
  }
]
```

Use `uv run hyper dumpdata products categories > fixtures/test_data.json` to export real data as fixtures.

### Test Database Configuration

Configure the test database separately from your production database. The
`db_url` attribute on `TestCase` controls which database is used.

```python
import os
from hyperdjango.testing import TestCase


class TestWithCustomDB(TestCase):
    app = app
    db_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgres://localhost/myapp_test",
    )

    async def test_connection(self):
        rows = await self.db.query("SELECT 1 as ok")
        self.assertEqual(rows[0]["ok"], 1)
```

For CI environments, set `TEST_DATABASE_URL` as an environment variable:

```bash
export TEST_DATABASE_URL="postgres://ci_user:ci_pass@localhost:5432/ci_test_db"
uv run hyper-test
```

The test database must already exist and have the schema created (via
`uv run hyper migrate`). Tests do not create or destroy databases -- they
only use savepoint rollback for isolation within the existing schema.
