#!/usr/bin/env python3
"""Test the ZigHandler Django WSGI bridge and _server_set_django_handler.

Tests:
1. ZigHandler construction and __call__ interface
2. Django request building (method, path, headers, body, query_string)
3. Response format (status, headers_str, body_bytes)
4. Header pre-formatting for Zig consumption
5. POST with form data
6. _server_set_django_handler registration
7. _db_pool_stats function
"""

# hyper-test: db_django

import os
import sys

# Set up minimal Django settings for testing
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django

django.setup()


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Test 1: ZigHandler construction ───────────────────────────────────
    print("\n=== Test 1: ZigHandler construction ===")
    from hyperdjango.serving.handler import ZigHandler

    handler = ZigHandler(server_name="localhost", server_port="8000")
    check("ZigHandler created", handler is not None)
    check("has _django_handler", hasattr(handler, "_django_handler"))
    check("callable", callable(handler))

    # ── Test 2: Handle a simple GET request ───────────────────────────────
    print("\n=== Test 2: Simple GET request ===")
    # This will hit Django's built-in URL resolver — should return 404 for unknown path
    result = handler("GET", "/nonexistent-path/", {}, b"", "")
    check("returns tuple", isinstance(result, tuple), f"got {type(result)}")
    check("tuple length == 3", len(result) == 3, f"got {len(result)}")
    status, headers_str, body = result
    check("status is int", isinstance(status, int), f"got {type(status)}")
    check("status 404 for unknown path", status == 404, f"got {status}")
    check("headers_str is str", isinstance(headers_str, str))
    check(
        "headers_str starts with \\r\\n",
        headers_str.startswith("\r\n") if headers_str else True,
        f"got {headers_str[:50]!r}",
    )
    check("body is bytes", isinstance(body, bytes))
    check(
        "Content-Type in headers",
        "Content-Type" in headers_str,
        f"headers: {headers_str[:100]!r}",
    )

    # ── Test 3: Request with headers ──────────────────────────────────────
    print("\n=== Test 3: Request with headers ===")
    result = handler(
        "GET",
        "/admin/",
        {
            "host": "localhost:8000",
            "accept": "text/html",
            "user-agent": "HyperDjango-Test",
        },
        b"",
        "",
    )
    status, headers_str, body = result
    # Django admin should redirect to login or return 302/200
    check("admin returns valid status", 200 <= status <= 404, f"got {status}")
    print(f"  Admin status: {status}")

    # ── Test 4: POST with form data ───────────────────────────────────────
    print("\n=== Test 4: POST with form data ===")
    result = handler(
        "POST",
        "/nonexistent/",
        {
            "content-type": "application/x-www-form-urlencoded",
            "content-length": "11",
        },
        b"key=value42",
        "",
    )
    status, headers_str, body = result
    check("POST returns valid status", 200 <= status <= 500, f"got {status}")

    # ── Test 5: Headers pre-formatting ────────────────────────────────────
    print("\n=== Test 5: Headers pre-formatting ===")
    result = handler("GET", "/nonexistent/", {}, b"", "")
    _, headers_str, _ = result
    # Check format: each header should be \r\nKey: Value
    lines = headers_str.split("\r\n")
    # First element is empty (leading \r\n)
    check("first split element is empty", lines[0] == "" if lines else True)
    for line in lines[1:]:
        if line:
            check(f"header has colon: {line[:30]}...", ": " in line, f"full: {line}")
            break  # Just check first one

    # ── Test 6: _server_set_django_handler registration ───────────────────
    print("\n=== Test 6: _server_set_django_handler ===")
    from hyperdjango._hyperdjango_native import _server_set_django_handler

    check("function exists", callable(_server_set_django_handler))
    # Register the handler (doesn't start server, just sets the callback)
    _server_set_django_handler(handler)
    check("handler registered without error", True)

    # ── Test 7: Query string handling ─────────────────────────────────────
    print("\n=== Test 7: Query string ===")
    result = handler("GET", "/nonexistent/", {}, b"", "foo=bar&baz=123")
    status, _, _ = result
    check("query string request works", 200 <= status <= 500, f"got {status}")

    # ── Test 8: Hit actual Django view ──────────────────────────────────
    print("\n=== Test 8: Django view dispatch ===")
    result = handler("GET", "/hello/", {"host": "localhost:8000"}, b"", "")
    status, headers_str, body = result
    check("hello view returns 200", status == 200, f"got {status}")
    check("hello body is correct", body == b"Hello from Django!", f"got {body!r}")
    check(
        "Content-Type text/html",
        "text/html" in headers_str,
        f"headers: {headers_str[:100]!r}",
    )

    # ── Test 9: Echo view with query params ───────────────────────────────
    print("\n=== Test 9: Echo view with params ===")
    result = handler(
        "GET",
        "/echo/",
        {
            "host": "localhost:8000",
            "x-custom": "test-value",
        },
        b"",
        "foo=bar&count=42",
    )
    status, headers_str, body = result
    check("echo returns 200", status == 200, f"got {status}")
    import json

    data = json.loads(body)
    check("echo method is GET", data["method"] == "GET")
    check("echo path is /echo/", data["path"] == "/echo/")
    check(
        "echo query has foo=bar",
        data["query"].get("foo") == "bar",
        f"got {data['query']}",
    )
    check("echo query has count=42", data["query"].get("count") == "42")
    check("Content-Type application/json", "application/json" in headers_str)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
    print("All runziserver tests passed!")


if __name__ == "__main__":
    main()
