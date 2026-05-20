"""
Tests for VersionMiddleware and VersionRouterMiddleware.

Tests X-App-Version header injection, HTML version mismatch script
injection, content-length updates, and version routing (blue/green,
canary, 409 for unknown versions).

# hyper-test: unit

Usage:
    uv run hyper-test version_middleware
"""

import asyncio
import inspect
import json
import sys
import traceback
from unittest.mock import patch

from hyperdjango.conf import DEFAULTS
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.standalone_middleware import (
    VersionMiddleware,
    VersionRouterMiddleware,
)
from hyperdjango.versioning import AppVersion, set_app_version

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS: dict[str, int | list[tuple[str, str]]] = {
    "passed": 0,
    "failed": 0,
    "errors": [],
}


def test(name: str):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  PASS: {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  FAIL: {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


def make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> Request:
    req = Request(
        method=method,
        path=path,
        headers=headers or {},
        query_string="",
        body=b"",
    )
    if cookies:
        req._cookies = cookies
    return req


async def html_handler(request: object) -> Response:
    """Handler that returns HTML."""
    html = "<html><head><title>Test</title></head><body><h1>Hello</h1></body></html>"
    return Response(
        body=html.encode("utf-8"),
        status=200,
        content_type="text/html; charset=utf-8",
    )


async def json_handler(request: object) -> Response:
    """Handler that returns JSON."""
    return Response.json({"status": "ok"})


async def empty_handler(request: object) -> Response:
    """Handler that returns empty body."""
    return Response(body=b"", status=204)


async def html_no_body_tag(request: object) -> Response:
    """HTML without </body> tag."""
    return Response(
        body=b"<html><head></head>No body tag",
        status=200,
        content_type="text/html",
    )


def _setup_version(version: str = "test123") -> AppVersion:
    """Set up a test AppVersion and return it."""
    av = AppVersion()
    av.set_explicit(version)
    set_app_version(av)
    return av


def _teardown_version() -> None:
    set_app_version(None)


# ---------------------------------------------------------------------------
# VersionMiddleware tests
# ---------------------------------------------------------------------------


@test("VersionMiddleware: adds X-App-Version header to JSON responses")
async def test_json_header():
    _setup_version("abc123def456")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "ignore"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), json_handler)
            assert resp.headers.get("x-app-version") == "abc123def456"
    finally:
        _teardown_version()


@test("VersionMiddleware: adds header to HTML responses")
async def test_html_header():
    _setup_version("v2")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "ignore"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), html_handler)
            assert resp.headers.get("x-app-version") == "v2"
    finally:
        _teardown_version()


@test("VersionMiddleware: header disabled when setting is False")
async def test_header_disabled():
    _setup_version("v1")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": False, "APP_VERSION_MISMATCH": "ignore"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), json_handler)
            assert "x-app-version" not in resp.headers
    finally:
        _teardown_version()


@test("VersionMiddleware: injects script into HTML with reload action")
async def test_html_script_injection_reload():
    _setup_version("inj123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "reload"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), html_handler)
            body = resp.body.decode("utf-8")
            assert 'window.__hyperAppVersion="inj123"' in body
            assert "htmx:afterRequest" in body
            assert "location.reload()" in body
            assert 'HYPER_ACTION="reload"' in body
            assert resp.headers.get("x-app-version-action") == "reload"
    finally:
        _teardown_version()


@test("VersionMiddleware: injects script with warn action")
async def test_html_script_injection_warn():
    _setup_version("warn123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "warn"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), html_handler)
            body = resp.body.decode("utf-8")
            assert 'window.__hyperAppVersion="warn123"' in body
            assert "console.warn" in body
            # The baked action is the FALLBACK only; the script still carries
            # every handler so a server-advertised action can override it.
            assert 'HYPER_ACTION="warn"' in body
            assert resp.headers.get("x-app-version-action") == "warn"
    finally:
        _teardown_version()


@test("VersionMiddleware: ignore action does not inject script")
async def test_ignore_no_script():
    _setup_version("ign123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "ignore"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), html_handler)
            body = resp.body.decode("utf-8")
            assert "htmx:afterRequest" not in body
            # Header still set
            assert resp.headers.get("x-app-version") == "ign123"
    finally:
        _teardown_version()


@test("VersionMiddleware: no script injection for JSON responses")
async def test_json_no_script():
    _setup_version("json123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "reload"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), json_handler)
            body = resp.body.decode("utf-8")
            assert "htmx:afterRequest" not in body
            assert "__hyperAppVersion" not in body
    finally:
        _teardown_version()


@test("VersionMiddleware: content-length updated after injection")
async def test_content_length_updated():
    _setup_version("len123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "reload"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), html_handler)
            declared_len = int(resp.headers.get("content-length", "0"))
            actual_len = len(resp.body)
            assert declared_len == actual_len, (
                f"content-length {declared_len} != body len {actual_len}"
            )
    finally:
        _teardown_version()


@test("VersionMiddleware: HTML without </body> tag gets header but no script")
async def test_no_body_tag():
    _setup_version("nobody123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "reload"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), html_no_body_tag)
            assert resp.headers.get("x-app-version") == "nobody123"
            body = resp.body.decode("utf-8")
            assert "htmx:afterRequest" not in body
    finally:
        _teardown_version()


@test("VersionMiddleware: empty body not modified")
async def test_empty_body():
    _setup_version("empty123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "reload"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), empty_handler)
            assert resp.headers.get("x-app-version") == "empty123"
            assert resp.body == b""
    finally:
        _teardown_version()


@test("VersionMiddleware: non-200 responses still get header")
async def test_non_200():
    async def error_handler(request):
        return Response(body=b"Error", status=500)

    _setup_version("err123")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "ignore"}
        ):
            mw = VersionMiddleware()
            resp = await mw(make_request(), error_handler)
            assert resp.headers.get("x-app-version") == "err123"
            assert resp.status == 500
    finally:
        _teardown_version()


@test("VersionMiddleware: version changes reflected immediately")
async def test_version_changes():
    av = _setup_version("v1")
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "ignore"}
        ):
            mw = VersionMiddleware()
            resp1 = await mw(make_request(), json_handler)
            assert resp1.headers["x-app-version"] == "v1"

            av.set_explicit("v2")
            resp2 = await mw(make_request(), json_handler)
            assert resp2.headers["x-app-version"] == "v2"
    finally:
        _teardown_version()


# ---------------------------------------------------------------------------
# VersionRouterMiddleware tests
# ---------------------------------------------------------------------------


@test("VersionRouter: no version requested passes through")
async def test_router_passthrough():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"v1": "backend-v1"},
            default_version="",
        )
        resp = await mw(make_request(), json_handler)
        assert resp.status == 200
        assert resp.headers.get("x-app-served-version") == "current"
        assert "x-backend-target" not in resp.headers
    finally:
        _teardown_version()


@test("VersionRouter: known version sets routing header")
async def test_router_known_version():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"v1": "backend-v1", "v2": "backend-v2"},
        )
        req = make_request(headers={"x-client-version": "v1"})
        resp = await mw(req, json_handler)
        assert resp.headers["x-backend-target"] == "backend-v1"
        assert resp.headers["x-app-served-version"] == "v1"
    finally:
        _teardown_version()


@test("VersionRouter: unknown version returns 409")
async def test_router_unknown_version():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"v1": "backend-v1"},
        )
        req = make_request(headers={"x-client-version": "v99"})
        resp = await mw(req, json_handler)
        assert resp.status == 409
        data = json.loads(resp.body)
        # Unified error contract {"detail","status"} — no bespoke error/available.
        assert data["status"] == 409
        assert "error" not in data
        assert "Unknown app version" in data["detail"]
        assert "v99" in data["detail"]  # requested version embedded
        assert "v1" in data["detail"]  # available versions embedded
    finally:
        _teardown_version()


@test("VersionRouter: default version used when empty request")
async def test_router_default_version():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"v1": "backend-v1", "v2": "backend-v2"},
            default_version="v2",
        )
        resp = await mw(make_request(), json_handler)
        assert resp.headers["x-backend-target"] == "backend-v2"
        assert resp.headers["x-app-served-version"] == "v2"
    finally:
        _teardown_version()


@test("VersionRouter: cookie fallback for version")
async def test_router_cookie_fallback():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"v1": "backend-v1"},
        )
        req = make_request()
        # Simulate cookie
        req._cookies = {"hyper_client_version": "v1"}
        resp = await mw(req, json_handler)
        assert resp.headers["x-backend-target"] == "backend-v1"
    finally:
        _teardown_version()


@test("VersionRouter: custom header names")
async def test_router_custom_headers():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"v1": "svc-v1"},
            request_header="x-custom-version",
            response_header="x-served",
            routing_header="x-route-to",
        )
        req = make_request(headers={"x-custom-version": "v1"})
        resp = await mw(req, json_handler)
        assert resp.headers["x-route-to"] == "svc-v1"
        assert resp.headers["x-served"] == "v1"
    finally:
        _teardown_version()


@test("VersionRouter: empty version_map passes through")
async def test_router_empty_map():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(version_map={})
        req = make_request(headers={"x-client-version": "v1"})
        resp = await mw(req, json_handler)
        assert resp.status == 200
        assert resp.headers.get("x-app-served-version") == "current"
    finally:
        _teardown_version()


@test("VersionRouter: header takes priority over cookie")
async def test_router_header_over_cookie():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"v1": "backend-v1", "v2": "backend-v2"},
        )
        req = make_request(headers={"x-client-version": "v2"})
        req._cookies = {"hyper_client_version": "v1"}
        resp = await mw(req, json_handler)
        # Header should win
        assert resp.headers["x-backend-target"] == "backend-v2"
    finally:
        _teardown_version()


@test("VersionRouter: 409 includes sorted available versions")
async def test_router_409_sorted():
    _setup_version("current")
    try:
        mw = VersionRouterMiddleware(
            version_map={"c": "c-be", "a": "a-be", "b": "b-be"},
        )
        req = make_request(headers={"x-client-version": "z"})
        resp = await mw(req, json_handler)
        data = json.loads(resp.body)
        # Sorted available versions are surfaced within the unified detail string.
        assert data["status"] == 409
        assert "a, b, c" in data["detail"]
    finally:
        _teardown_version()


@test("VersionRouter: served-version header on passthrough")
async def test_router_served_version_passthrough():
    _setup_version("v3.0")
    try:
        mw = VersionRouterMiddleware(version_map={}, default_version="")
        resp = await mw(make_request(), json_handler)
        assert resp.headers["x-app-served-version"] == "v3.0"
    finally:
        _teardown_version()


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for _name, obj in sorted(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nVersion Middleware Tests ({len(tests)} tests)")
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


def run_tests():
    exit_code = asyncio.run(main())
    return exit_code


if __name__ == "__main__":
    sys.exit(run_tests())
