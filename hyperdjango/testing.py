"""
Test utilities for HyperApp — TestClient, TestCase, assertion helpers.

Usage:
    from hyperdjango.testing import TestClient, TestCase

    # ── TestClient (low-level) ─────────────────────────────
    app = HyperApp()

    @app.get("/hello")
    async def hello(request):
        return {"msg": "hi"}

    client = TestClient(app)

    def test_hello():
        resp = client.get("/hello")
        assert resp.status == 200
        assert resp.json() == {"msg": "hi"}

    # ── TestCase (with DB rollback) ────────────────────────
    class TestUsers(TestCase):
        app = app
        db_url = "postgres://localhost/mydb_test"

        async def test_create_user(self):
            await self.db.execute(
                "INSERT INTO users (name) VALUES ($1)", "Alice"
            )
            rows = await self.db.query("SELECT name FROM users")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Alice")
            # DB is rolled back after each test — no cleanup needed
"""

import asyncio
import concurrent.futures
import contextlib
import threading
import traceback
import uuid
from typing import Any
from urllib.parse import urlencode

from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.logging import logger
from hyperdjango.native import fast_json_dumps, fast_json_loads
from hyperdjango.native._coro import get_thread_event_loop, run_coro_on_loop
from hyperdjango.request import Request
from hyperdjango.response import Response

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Shared offload workers for the rare sync-TestClient-inside-async-context
# case. Process-lifetime: each worker thread keeps its persistent event loop
# (via get_thread_event_loop), so repeated calls reuse loops instead of the
# old per-request ThreadPoolExecutor + asyncio.run churn.
_offload_executor: concurrent.futures.ThreadPoolExecutor | None = None
_offload_lock = threading.Lock()


def _offload_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _offload_executor
    if _offload_executor is None:
        with _offload_lock:
            if _offload_executor is None:
                _offload_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="testclient-offload"
                )
    return _offload_executor


def _offload_run(coro) -> Response:
    """Runs ON the offload worker thread: complete the handler coroutine on
    THIS thread's persistent loop (get_thread_event_loop is per-thread)."""
    return run_coro_on_loop(get_thread_event_loop(), coro)


class TestResponse:
    """Wrapper around Response with convenience methods for testing."""

    __test__ = False  # Not a pytest test class

    def __init__(self, response: Response):
        self._response = response
        self.status = response.status
        self.headers = response.headers
        self.body = response.body

    def json(self) -> Any:
        """Parse response body as JSON."""
        return fast_json_loads(self.body)

    def text(self) -> str:
        """Get response body as text."""
        return self.body.decode("utf-8")

    @property
    def ok(self) -> bool:
        """True if status is 2xx."""
        return 200 <= self.status < 300

    def __repr__(self):
        return f"TestResponse(status={self.status})"


class TestClient:
    __test__ = False  # Not a pytest test class
    """In-process test client for HyperApp.

    Makes requests directly to the app without network I/O.
    Synchronous API for easy use in pytest.
    """

    def __init__(self, app):
        self.app = app
        self._cookies = {}
        self._default_headers = {}

    def set_auth(self, token: str, scheme: str = "Bearer"):
        """Set authorization header for all subsequent requests.

        Usage:
            client.set_auth("my-token")
            client.set_auth("my-token", scheme="Token")
        """
        self._default_headers["authorization"] = f"{scheme} {token}"
        return self

    def set_api_key(self, key: str, header: str = "x-api-key"):
        """Set API key header for all subsequent requests."""
        self._default_headers[header] = key
        return self

    def clear_auth(self):
        """Remove all auth headers."""
        self._default_headers.pop("authorization", None)
        self._default_headers.pop("x-api-key", None)
        return self

    def login_oauth2(self, provider: str, user_data: dict):
        """Simulate OAuth2 login for testing (skip redirect flow).

        Creates a session directly with oauth2_provider set, as if the
        user completed the full OAuth2 authorization code flow.
        """
        user_data = {**user_data, "oauth2_provider": provider}

        # Find SessionAuth in middleware stack
        mw_list = self.app._middleware._middleware
        for mw in mw_list:
            if isinstance(mw, SessionAuth):
                response = Response.empty()
                session_id = mw.login(response, user_data)
                # Extract cookie from response
                for key, val in response.headers.items():
                    if key.lower() == "set-cookie":
                        parts = val.split(";")
                        cookie_parts = parts[0].split("=", 1)
                        if len(cookie_parts) == 2:
                            self._cookies[cookie_parts[0]] = cookie_parts[1]
                return self
        return self

    def get(self, path, headers=None, **kwargs) -> TestResponse:
        return self.request("GET", path, headers=headers, **kwargs)

    def post(self, path, json=None, data=None, headers=None, **kwargs) -> TestResponse:
        return self.request(
            "POST", path, json=json, data=data, headers=headers, **kwargs
        )

    def put(self, path, json=None, data=None, headers=None, **kwargs) -> TestResponse:
        return self.request(
            "PUT", path, json=json, data=data, headers=headers, **kwargs
        )

    def patch(self, path, json=None, data=None, headers=None, **kwargs) -> TestResponse:
        return self.request(
            "PATCH", path, json=json, data=data, headers=headers, **kwargs
        )

    def delete(self, path, headers=None, **kwargs) -> TestResponse:
        return self.request("DELETE", path, headers=headers, **kwargs)

    def request(
        self,
        method: str,
        path: str,
        json=None,
        data: dict[str, str] | bytes | str | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        headers: dict[str, str] | None = None,
        query_string: str = "",
    ) -> TestResponse:
        """Send a request to the app.

        Args:
            files: Dict of field_name → (filename, content_bytes, content_type).
                   When provided, sends as multipart/form-data.
        """
        hdrs = dict(self._default_headers)
        hdrs.update(headers or {})

        # Build body
        body = b""
        if json is not None:
            body = fast_json_dumps(json)
            hdrs.setdefault("content-type", "application/json")
        elif files is not None:
            body, content_type = self._build_multipart(data or {}, files)
            hdrs["content-type"] = content_type
        elif data is not None:
            if isinstance(data, bytes):
                body = data
            elif isinstance(data, str):
                body = data.encode("utf-8")
            elif isinstance(data, dict):
                body = urlencode(data).encode("utf-8")
                hdrs.setdefault("content-type", "application/x-www-form-urlencoded")

        # Add cookies
        if self._cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
            hdrs["cookie"] = cookie_str

        # Parse query string from path
        if "?" in path:
            path, query_string = path.split("?", 1)

        request = Request(
            method=method,
            path=path,
            headers=hdrs,
            query_string=query_string,
            body=body,
        )

        # Run async handler — handle both sync and async calling contexts.
        # Sync context (the overwhelmingly common case: every hyper-test
        # check, every cProfile harness) completes the handler on this
        # thread's PERSISTENT loop via the same eager-Task runner the native
        # server uses — never a new event loop per request. The previous
        # new_event_loop()/run_until_complete()/close() per call was the
        # per-request-loop anti-pattern (kqueue/epoll fd + socketpair churn,
        # 2 loop iterations + 4 kqueue syscalls per request measured) that
        # the Zig fallback path already eliminated.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            response = run_coro_on_loop(
                get_thread_event_loop(), self.app.handle(request)
            )
        else:
            # Called from inside a running loop (a sync client used in an
            # async test): blocking here would deadlock the caller's loop, so
            # complete the handler on the shared offload thread — a
            # process-lifetime worker with its own persistent loop, not a
            # per-request ThreadPoolExecutor + asyncio.run.
            response = (
                _offload_pool().submit(_offload_run, self.app.handle(request)).result()
            )

        # Extract set-cookie from response
        set_cookie = response.headers.get("set-cookie", "")
        if set_cookie:
            for part in set_cookie.split("\r\nset-cookie: "):
                if "=" in part:
                    cookie_part = part.split(";")[0]
                    k, v = cookie_part.split("=", 1)
                    if v and "Max-Age=0" not in part:
                        self._cookies[k.strip()] = v.strip()
                    elif "Max-Age=0" in part:
                        self._cookies.pop(k.strip(), None)

        return TestResponse(response)

    def reset_cookies(self):
        """Clear all cookies."""
        self._cookies.clear()

    @staticmethod
    def _build_multipart(
        fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
    ) -> tuple[bytes, str]:
        """Build a multipart/form-data body.

        Returns (body_bytes, content_type_with_boundary).
        """
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []

        for field_name, field_value in fields.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'
                f"{field_value}\r\n".encode()
            )

        for field_name, (filename, content, content_type) in files.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode()
                + content
                + b"\r\n"
            )

        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        return body, f"multipart/form-data; boundary={boundary}"


class TestWebSocket:
    """Mock WebSocket for testing @app.websocket handlers.

    Usage:
        async with client.websocket("/ws/chat") as ws:
            await ws.send_text("hello")
            msg = await ws.receive_text()
            assert msg == "echo: hello"
    """

    __test__ = False

    def __init__(self):
        self._send_queue: list[str | bytes] = []
        self._receive_queue: list[str | bytes] = []
        self._accepted = False
        self._closed = False

    async def accept(self):
        self._accepted = True

    async def send_text(self, data: str):
        self._send_queue.append(data)

    async def send_bytes(self, data: bytes):
        self._send_queue.append(data)

    async def receive_text(self) -> str:
        if not self._receive_queue:
            raise RuntimeError("No messages in receive queue — call feed() first")
        msg = self._receive_queue.pop(0)
        return msg if isinstance(msg, str) else msg.decode()

    async def receive_bytes(self) -> bytes:
        if not self._receive_queue:
            raise RuntimeError("No messages in receive queue — call feed() first")
        msg = self._receive_queue.pop(0)
        return msg if isinstance(msg, bytes) else msg.encode()

    async def close(self, code: int = 1000, reason: str = ""):
        self._closed = True

    def feed(self, *messages: str | bytes):
        """Pre-load messages that receive_text/receive_bytes will return."""
        self._receive_queue.extend(messages)

    @property
    def accepted(self) -> bool:
        return self._accepted

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def sent_messages(self) -> list[str | bytes]:
        return self._send_queue


# ─── TestCase ──────────────────────────────────────────────────────────────────


class TestCase:
    """Base test class with database transaction rollback.

    Each test method runs inside a SAVEPOINT that is rolled back after the test,
    so tests are fully isolated without manual cleanup. The outer transaction
    (BEGIN) is created once per test class and rolled back at teardown.

    Usage:
        class TestProducts(TestCase):
            app = my_app
            db_url = "postgres://localhost/mydb_test"

            async def asyncSetUp(self):
                await self.db.execute(
                    "CREATE TABLE IF NOT EXISTS products "
                    "(id SERIAL PRIMARY KEY, name TEXT)"
                )

            async def test_create(self):
                await self.db.execute("INSERT INTO products (name) VALUES ($1)", "Widget")
                rows = await self.db.query("SELECT * FROM products")
                self.assertEqual(len(rows), 1)

            async def test_empty(self):
                # Previous test's INSERT was rolled back
                rows = await self.db.query("SELECT * FROM products")
                self.assertEqual(len(rows), 0)

    Run with:
        TestProducts.run_all()
    """

    __test__ = False  # Not a pytest test class

    # Override in subclass
    app = None
    db_url: str = ""
    db = None
    client = None

    # ── Lifecycle ──────────────────────────────────────────

    async def asyncSetUp(self):
        """Override for per-test setup (runs inside savepoint)."""
        pass

    async def asyncTearDown(self):
        """Override for per-test teardown (runs inside savepoint, before rollback)."""
        pass

    @classmethod
    async def asyncSetUpClass(cls):
        """Override for one-time class setup (runs before all tests)."""
        pass

    @classmethod
    async def asyncTearDownClass(cls):
        """Override for one-time class teardown (runs after all tests)."""
        pass

    # ── Assertions ─────────────────────────────────────────

    def assertEqual(self, a, b, msg=""):
        if a != b:
            detail = msg or f"{a!r} != {b!r}"
            raise AssertionError(detail)

    def assertNotEqual(self, a, b, msg=""):
        if a == b:
            detail = msg or f"{a!r} == {b!r}"
            raise AssertionError(detail)

    def assertTrue(self, value, msg=""):
        if not value:
            raise AssertionError(msg or f"{value!r} is not truthy")

    def assertFalse(self, value, msg=""):
        if value:
            raise AssertionError(msg or f"{value!r} is not falsy")

    def assertIsNone(self, value, msg=""):
        if value is not None:
            raise AssertionError(msg or f"{value!r} is not None")

    def assertIsNotNone(self, value, msg=""):
        if value is None:
            raise AssertionError(msg or "value is None")

    def assertIn(self, member, container, msg=""):
        if member not in container:
            raise AssertionError(msg or f"{member!r} not in {container!r}")

    def assertNotIn(self, member, container, msg=""):
        if member in container:
            raise AssertionError(msg or f"{member!r} in {container!r}")

    def assertGreater(self, a, b, msg=""):
        if not (a > b):
            raise AssertionError(msg or f"{a!r} not > {b!r}")

    def assertGreaterEqual(self, a, b, msg=""):
        if not (a >= b):
            raise AssertionError(msg or f"{a!r} not >= {b!r}")

    def assertLess(self, a, b, msg=""):
        if not (a < b):
            raise AssertionError(msg or f"{a!r} not < {b!r}")

    def assertIsInstance(self, obj, cls, msg=""):
        if not isinstance(obj, cls):
            raise AssertionError(msg or f"{obj!r} is not instance of {cls}")

    def assertRaises(self, exc_type):
        """Context manager for asserting exceptions."""
        return _AssertRaisesContext(exc_type)

    # ── Response Assertions ────────────────────────────────

    def assertStatus(self, response, status, msg=""):
        """Assert response has expected status code."""
        if response.status != status:
            raise AssertionError(
                msg or f"Expected status {status}, got {response.status}"
            )

    def assertOk(self, response, msg=""):
        """Assert response is 2xx."""
        if not response.ok:
            raise AssertionError(msg or f"Expected 2xx, got {response.status}")

    def assertContains(self, response, text, msg=""):
        """Assert response body contains text."""
        body = response.text() if hasattr(response, "text") else str(response.body)
        if text not in body:
            raise AssertionError(msg or f"Response does not contain {text!r}")

    def assertNotContains(self, response, text, msg=""):
        """Assert response body does NOT contain text."""
        body = response.text() if hasattr(response, "text") else str(response.body)
        if text in body:
            raise AssertionError(msg or f"Response contains {text!r}")

    def assertRedirects(self, response, url, msg=""):
        """Assert response is a redirect to the given URL."""
        if response.status not in _REDIRECT_STATUSES:
            raise AssertionError(msg or f"Expected redirect, got {response.status}")
        location = response.headers.get("location", "")
        if location != url:
            raise AssertionError(
                msg or f"Expected redirect to {url!r}, got {location!r}"
            )

    def assertJsonEqual(self, response, expected, msg=""):
        """Assert response JSON equals expected dict."""
        actual = response.json()
        if actual != expected:
            raise AssertionError(
                msg
                or f"JSON mismatch:\n  expected: {expected!r}\n  actual:   {actual!r}"
            )

    # ── Runner ─────────────────────────────────────────────

    @classmethod
    def run_all(cls):
        """Discover and run all test methods in this class.

        Test methods are async methods starting with 'test_'.
        Each runs inside a savepoint for isolation.
        """
        return asyncio.run(cls._run_all_async())

    @classmethod
    async def _run_all_async(cls):
        from hyperdjango.conf import resolve_database_url
        from hyperdjango.database import Database, set_db

        # Route through the single connection-URL authority so a test harness
        # configured via DATABASE_URL, HYPER_DATABASE_URL, or the libpq PG* set
        # all resolve identically to what the server/CLI use.
        url = cls.db_url or resolve_database_url()

        # Connect to database
        if url:
            db = Database(url)
            await db.connect()
            set_db(db)
            cls.db = db
        else:
            db = None

        # Create client if app provided
        if cls.app:
            cls.client = TestClient(cls.app)

        passed = 0
        failed = 0
        errors = []

        try:
            await cls.asyncSetUpClass()

            # Start outer transaction (rolled back at end)
            if db:
                from hyperdjango._hyperdjango_native import _db_execute

                _db_execute(db._pool_handle, "BEGIN", [])

            # Discover test methods
            test_methods = sorted(
                name
                for name in dir(cls)
                # dynamic-attr: test discovery reflects over runtime-enumerated test_* method names on an arbitrary user test class
                if name.startswith("test_") and callable(getattr(cls, name))
            )

            for method_name in test_methods:
                instance = cls()
                instance.db = db
                if cls.app:
                    instance.client = TestClient(cls.app)

                # Create savepoint for isolation
                savepoint = f"sp_{method_name}"
                if db:
                    from hyperdjango._hyperdjango_native import (
                        _db_execute as _exec,
                    )

                    _exec(db._pool_handle, f"SAVEPOINT {savepoint}", [])

                try:
                    await instance.asyncSetUp()
                    # dynamic-attr: method_name is a runtime-discovered test method name; bound method is not statically knowable
                    method = getattr(instance, method_name)
                    await method()
                    logger.info("  PASS: {method_name}", method_name=method_name)
                    passed += 1
                except AssertionError as e:
                    logger.error(
                        "  FAIL: {method_name} — {error}",
                        method_name=method_name,
                        error=e,
                    )
                    failed += 1
                    errors.append((method_name, str(e)))
                # blind-except: any error from a test is recorded as ERROR and the run continues; the harness must not abort on one test.
                except Exception as e:
                    logger.error(
                        "  ERROR: {method_name} — {error}",
                        method_name=method_name,
                        error=e,
                    )
                    failed += 1
                    errors.append((method_name, traceback.format_exc()))
                finally:
                    with contextlib.suppress(Exception):
                        await instance.asyncTearDown()
                    # Rollback savepoint — undo all changes from this test
                    if db:
                        try:
                            from hyperdjango._hyperdjango_native import (
                                _db_execute as _exec2,
                            )

                            _exec2(
                                db._pool_handle,
                                f"ROLLBACK TO SAVEPOINT {savepoint}",
                                [],
                            )
                        # blind-except: best-effort per-test savepoint rollback in cleanup; a rollback failure must not mask the test's own result.
                        except Exception:
                            pass

            # Rollback outer transaction
            if db:
                try:
                    from hyperdjango._hyperdjango_native import (
                        _db_execute as _exec3,
                    )

                    _exec3(db._pool_handle, "ROLLBACK", [])
                # blind-except: best-effort teardown of the outer test transaction; a rollback failure must not mask results already collected.
                except Exception:
                    pass

        finally:
            with contextlib.suppress(Exception):
                await cls.asyncTearDownClass()
            if db:
                await db.disconnect()

        logger.info(
            "\nResults: {passed} passed, {failed} failed", passed=passed, failed=failed
        )
        return passed, failed, errors


class _AssertRaisesContext:
    """Context manager for assertRaises."""

    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.exception = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(
                f"Expected {self.exc_type.__name__} but no exception raised"
            )
        if not issubclass(exc_type, self.exc_type):
            return False  # Re-raise
        self.exception = exc_val
        return True  # Suppress
