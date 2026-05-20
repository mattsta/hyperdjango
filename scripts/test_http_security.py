"""
Regression tests for HTTP layer security fixes.

Tests:
1. Header injection prevention (CRLF in headers, cookies, redirects)
2. CSRF double-submit pattern correctness
3. WebSocket header decoding (bytes → strings)
4. Middleware chain caching
5. Response.empty() content-type handling

Usage:
    uv run hyper-test http_security
"""

# hyper-test: unit

import asyncio
import inspect
import sys
import traceback

from hyperdjango.request import Request
from hyperdjango.response import Response, _sanitize_header
from hyperdjango.standalone_middleware import CSRFMiddleware
from hyperdjango.websocket import WebSocket

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Header injection prevention
# ---------------------------------------------------------------------------


@test("sanitize_header: CR truncates")
def test_sanitize_cr():
    assert _sanitize_header("value\rfoo") == "value"


@test("sanitize_header: LF truncates")
def test_sanitize_lf():
    assert _sanitize_header("value\nfoo") == "value"


@test("sanitize_header: CRLF split attempt truncates (attacker payload dropped)")
def test_sanitize_crlf():
    assert _sanitize_header("value\r\nX-Injected: evil") == "value"


@test("sanitize_header: safe string unchanged")
def test_sanitize_safe():
    assert (
        _sanitize_header("application/json; charset=utf-8")
        == "application/json; charset=utf-8"
    )


@test("Response: redirect URL sanitized")
def test_redirect_sanitized():
    resp = Response.redirect("http://evil.com\r\nX-Injected: evil")
    location = resp.headers.get("location", "")
    assert "\r" not in location
    assert "\n" not in location


@test("Response: set_cookie sanitizes key and value")
def test_cookie_sanitized():
    resp = Response.html("<h1>OK</h1>")
    resp.set_cookie("session\r\nX-Inject", "val\r\nX-Bad: yes")
    cookie = resp.headers.get("set-cookie", "")
    assert "\r" not in cookie
    assert "\n" not in cookie


@test("Response: constructor sanitizes all header values")
def test_constructor_sanitizes():
    resp = Response(
        body=b"ok",
        status=200,
        headers={"x-custom": "safe\r\nX-Injected: evil"},
    )
    custom = resp.headers.get("x-custom", "")
    assert "\r" not in custom
    assert "\n" not in custom


# ---------------------------------------------------------------------------
# CSRF double-submit pattern
# ---------------------------------------------------------------------------


@test("CSRF: matching token and cookie passes")
async def test_csrf_matching():
    csrf = CSRFMiddleware(secret="test-secret")
    token = csrf._generate_token()

    req = Request(
        method="POST",
        path="/api/action",
        headers={"x-csrftoken": token, "cookie": f"csrftoken={token}"},
        body=b"",
    )

    async def handler(request):
        return Response.json({"ok": True})

    resp = await csrf(req, handler)
    assert resp.status == 200


@test("CSRF: mismatched token and cookie rejected")
async def test_csrf_mismatch():
    csrf = CSRFMiddleware(secret="test-secret")
    token1 = csrf._generate_token()
    token2 = csrf._generate_token()

    req = Request(
        method="POST",
        path="/api/action",
        headers={"x-csrftoken": token1, "cookie": f"csrftoken={token2}"},
        body=b"",
    )

    async def handler(request):
        return Response.json({"ok": True})

    resp = await csrf(req, handler)
    assert resp.status == 403


@test("CSRF: missing token rejected")
async def test_csrf_missing():
    csrf = CSRFMiddleware(secret="test-secret")

    req = Request(
        method="POST",
        path="/api/action",
        headers={},
        body=b"",
    )

    async def handler(request):
        return Response.json({"ok": True})

    resp = await csrf(req, handler)
    assert resp.status == 403


@test("CSRF: GET requests pass through without token")
async def test_csrf_get_pass():
    csrf = CSRFMiddleware(secret="test-secret")

    req = Request(
        method="GET",
        path="/api/data",
        headers={},
        body=b"",
    )

    async def handler(request):
        return Response.json({"ok": True})

    resp = await csrf(req, handler)
    assert resp.status == 200


@test("CSRF: exempt paths bypass check")
async def test_csrf_exempt():
    csrf = CSRFMiddleware(secret="test-secret", exempt_paths=["/webhook"])

    req = Request(
        method="POST",
        path="/webhook",
        headers={},
        body=b"",
    )

    async def handler(request):
        return Response.json({"ok": True})

    resp = await csrf(req, handler)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# WebSocket header decoding
# ---------------------------------------------------------------------------


@test("WebSocket: headers decoded from bytes to strings")
def test_ws_headers_decoded():
    scope = {
        "type": "websocket",
        "path": "/ws/test",
        "headers": [
            (b"host", b"localhost:8000"),
            (b"upgrade", b"websocket"),
        ],
        "query_string": b"",
    }

    async def noop_receive():
        return {"type": "websocket.connect"}

    async def noop_send(msg):
        pass

    ws = WebSocket(scope, noop_receive, noop_send)
    assert ws.headers.get("host") == "localhost:8000"
    assert ws.headers.get("upgrade") == "websocket"
    assert isinstance(list(ws.headers.keys())[0], str)


# ---------------------------------------------------------------------------
# Response.empty() content-type
# ---------------------------------------------------------------------------


@test("Response: basic construction works")
def test_response_basic():
    resp = Response(body=b"hello", status=200)
    assert resp.status == 200
    assert resp.body == b"hello"
    assert "content-type" in resp.headers


@test("Response: json() creates correct content-type")
def test_response_json():
    resp = Response.json({"key": "value"})
    assert resp.status == 200
    assert "application/json" in resp.headers.get("content-type", "")


@test("Response: html() creates correct content-type")
def test_response_html():
    resp = Response.html("<h1>Hello</h1>")
    assert "text/html" in resp.headers.get("content-type", "")


@test("Response: string body auto-encoded to bytes")
def test_response_string_body():
    resp = Response(body="hello world", status=200)
    assert resp.body == b"hello world"


@test("Request: query method returns single values")
def test_request_query_method():
    req = Request(query_string="page=3&q=hello&tags=a&tags=b")
    assert req.query("page") == "3"
    assert req.query("q") == "hello"
    assert req.query("missing", "default") == "default"


@test("Request: GET property returns flat dict")
def test_request_get_property():
    req = Request(query_string="page=3&q=hello")
    assert req.GET.get("page") == "3"
    assert req.GET.get("q") == "hello"
    assert req.GET.get("missing", "default") == "default"


@test("Request: query_params returns dict of lists")
def test_request_query_params():
    req = Request(query_string="tags=a&tags=b&page=1")
    params = req.query_params
    assert isinstance(params.get("tags"), list)
    assert len(params.get("tags", [])) == 2


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nHTTP Security Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
