"""
Regression tests for the ws8 full-stack middleware/auth/security fixes.

Covers:
1. Exception responses still pass through response middleware (security headers
   present on raised RuntimeError→500 and raised HTTPException→403).
2. CSRF signature is verified (planted/forged/tampered tokens rejected; a
   legitimately minted token round-trips).
3. Flash messages survive a Response.redirect round-trip in the standalone
   stack (backed by the SessionAuth session bridge).
8. The Zig enhanced-response wrapper drops Content-Length from extra headers,
   so a body-rewriting middleware (VersionMiddleware/CompressionMiddleware)
   can't produce a duplicate Content-Length.
9. The fallback static server caches file bytes (no re-read on an unchanged
   second request) and honors If-None-Match with a 304.

Usage:
    uv run hyper-test ws8_middleware_fixes
"""

# hyper-test: unit

import asyncio
import inspect
import pathlib
import sys
import tempfile
import traceback

from hyperdjango.app import HyperApp
from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth
from hyperdjango.exceptions import HTTPException
from hyperdjango.messages import get_messages, success
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    MiddlewareStack,
    SecurityHeadersMiddleware,
)
from hyperdjango.testing import TestClient

RESULTS = {"passed": 0, "failed": 0, "errors": []}

_SEC_HEADER_NAMES = (
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "cross-origin-opener-policy",
)


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Item 1: security headers must reach error responses too
# ---------------------------------------------------------------------------


def _security_app():
    app = HyperApp(debug=False)
    app.use(SecurityHeadersMiddleware())

    @app.get("/ok")
    async def ok(request):
        return Response.json({"ok": True})

    @app.get("/boom")
    async def boom(request):
        raise RuntimeError("kaboom")

    @app.get("/forbidden")
    async def forbidden(request):
        raise HTTPException(403, "nope")

    return TestClient(app)


@test("item1: security headers present on a 200 baseline")
def test_item1_baseline():
    client = _security_app()
    r = client.get("/ok")
    present = [h for h in _SEC_HEADER_NAMES if h in r.headers]
    assert present, f"expected some security headers on 200, got {dict(r.headers)}"


@test("item1: raised RuntimeError -> 500 still carries security headers")
def test_item1_runtime_error_500():
    client = _security_app()
    baseline = {h for h in _SEC_HEADER_NAMES if h in client.get("/ok").headers}
    r = client.get("/boom")
    assert r.status == 500, f"expected 500, got {r.status}"
    missing = [h for h in baseline if h not in r.headers]
    assert not missing, f"500 response missing security headers: {missing}"


@test("item1: raised HTTPException(403) still carries security headers")
def test_item1_http_exception_403():
    client = _security_app()
    baseline = {h for h in _SEC_HEADER_NAMES if h in client.get("/ok").headers}
    r = client.get("/forbidden")
    assert r.status == 403, f"expected 403, got {r.status}"
    missing = [h for h in baseline if h not in r.headers]
    assert not missing, f"403 response missing security headers: {missing}"


@test("item1(zig): raised HTTPException in the Zig wrapper carries security headers")
async def test_item1_zig_wrapper_path():
    # Exercise the actual Zig enhanced-response wrapper (the audit's target):
    # a raised exception must be normalized inside the chain so response
    # middleware still decorates it. The security headers land in extra_headers.
    stack = MiddlewareStack()
    stack.add(SecurityHeadersMiddleware())

    async def handler(request):
        raise HTTPException(403, "nope")

    wrapped = HyperApp._wrap_handler_for_zig(handler, stack)

    def call():
        return wrapped(
            method="GET",
            path="/",
            headers={},
            query_string="",
            body=b"",
            path_params={},
        )

    status, ct, body, extra = await asyncio.to_thread(call)
    assert status == 403, f"expected 403, got {status}"
    extra_l = (extra or "").lower()
    assert "x-content-type-options" in extra_l or "x-frame-options" in extra_l, (
        f"security headers missing from Zig error response: {extra!r}"
    )


# ---------------------------------------------------------------------------
# Item 2: CSRF signature verification
# ---------------------------------------------------------------------------


async def _run_csrf(csrf, req):
    async def handler(request):
        return Response.json({"ok": True}, status=200)

    return await csrf(req, handler)


@test("item2: legitimately minted CSRF token round-trips (200)")
async def test_item2_roundtrip():
    csrf = CSRFMiddleware(secret="csrf-secret")
    token = csrf._generate_token()
    post = Request(
        method="POST",
        path="/x",
        headers={"x-csrftoken": token, "cookie": f"csrftoken={token}"},
    )
    resp = await _run_csrf(csrf, post)
    assert resp.status == 200, f"legit token should pass, got {resp.status}"


@test("item2: planted unsigned token rejected (403)")
async def test_item2_planted_unsigned():
    csrf = CSRFMiddleware(secret="csrf-secret")
    # Attacker plants an identical cookie + header with NO valid signature.
    planted = "forgedtoken.0000000000000000"
    post = Request(
        method="POST",
        path="/x",
        headers={"x-csrftoken": planted, "cookie": f"csrftoken={planted}"},
    )
    resp = await _run_csrf(csrf, post)
    assert resp.status == 403, f"planted token must be rejected, got {resp.status}"


@test("item2: no-dot token rejected (403)")
async def test_item2_no_signature():
    csrf = CSRFMiddleware(secret="csrf-secret")
    planted = "attacker-planted-value-without-any-signature"
    post = Request(
        method="POST",
        path="/x",
        headers={"x-csrftoken": planted, "cookie": f"csrftoken={planted}"},
    )
    resp = await _run_csrf(csrf, post)
    assert resp.status == 403, f"unsigned token must be rejected, got {resp.status}"


@test("item2: tampered signature rejected (403)")
async def test_item2_tampered_sig():
    csrf = CSRFMiddleware(secret="csrf-secret")
    token = csrf._generate_token()
    # Flip the last character of the signature. Cookie+header still match each
    # other (equality passes) but the HMAC no longer verifies.
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    post = Request(
        method="POST",
        path="/x",
        headers={"x-csrftoken": tampered, "cookie": f"csrftoken={tampered}"},
    )
    resp = await _run_csrf(csrf, post)
    assert resp.status == 403, f"tampered sig must be rejected, got {resp.status}"


@test("item2: GET issues a signed cookie that a later POST accepts")
async def test_item2_get_issues_valid_cookie():
    csrf = CSRFMiddleware(secret="csrf-secret")
    get = Request(method="GET", path="/form", headers={})
    get_resp = await _run_csrf(csrf, get)
    # Extract the issued token from Set-Cookie
    set_cookie = get_resp.headers.get("set-cookie", "")
    assert "csrftoken=" in set_cookie, "GET should issue a csrftoken cookie"
    token = set_cookie.split("csrftoken=", 1)[1].split(";", 1)[0]
    assert csrf._validate_token(token), "issued token must be validly signed"
    post = Request(
        method="POST",
        path="/x",
        headers={"x-csrftoken": token, "cookie": f"csrftoken={token}"},
    )
    resp = await _run_csrf(csrf, post)
    assert resp.status == 200, f"issued token should pass on POST, got {resp.status}"


# ---------------------------------------------------------------------------
# Item 3: flash messages survive a redirect round-trip (standalone stack)
# ---------------------------------------------------------------------------


@test("item3: flash message survives a redirect round-trip via session bridge")
def test_item3_flash_redirect_roundtrip():
    app = HyperApp(debug=False)
    app.use(
        SessionAuth(
            secret="sess-secret", store=InMemorySessionStore(), secure_cookie=False
        )
    )

    @app.post("/set-flash")
    async def set_flash(request):
        success(request, "saved!")
        return Response.redirect("/read")

    @app.get("/read")
    async def read(request):
        msgs = get_messages(request)
        return Response.json({"messages": [m["text"] for m in msgs]})

    client = TestClient(app)
    r1 = client.post("/set-flash")
    assert r1.status in (302, 303), f"expected redirect, got {r1.status}"
    # The session cookie carrying the flash message must have been issued.
    assert client._cookies, "expected a session cookie to be set on the redirect"

    r2 = client.get("/read")
    assert r2.status == 200
    msgs = r2.json()["messages"]
    assert msgs == ["saved!"], f"flash message lost across redirect: {msgs}"

    # Messages are one-shot: a second read is empty (cleared + persisted).
    r3 = client.get("/read")
    assert r3.json()["messages"] == [], "messages should be cleared after reading"


# ---------------------------------------------------------------------------
# Item 8: no duplicate Content-Length from the Zig enhanced-response wrapper
# ---------------------------------------------------------------------------


@test("item8: Zig wrapper drops Content-Length from extra headers")
async def test_item8_no_duplicate_content_length():
    async def handler(request):
        # Simulate VersionMiddleware/CompressionMiddleware: rewrite the body and
        # set a Content-Length header. Zig frames its own Content-Length from the
        # body bytes, so this must NOT be forwarded (else the client sees two).
        resp = Response.html("<html><body>hi</body></html>")
        resp.headers["content-length"] = "999"
        resp.headers["x-custom"] = "kept"
        return resp

    wrapped = HyperApp._wrap_handler_for_zig(handler, None)

    def call():
        # Run off the event loop: the wrapper drives its own thread-local loop.
        return wrapped(
            method="GET",
            path="/",
            headers={},
            query_string="",
            body=b"",
            path_params={},
        )

    status, ct, body, extra = await asyncio.to_thread(call)
    assert status == 200
    assert b"<body>hi</body>" in body
    extra_l = (extra or "").lower()
    assert "content-length" not in extra_l, (
        f"duplicate Content-Length leaked: {extra!r}"
    )
    # Non-framing headers are still forwarded.
    assert extra is not None and "x-custom" in extra


# ---------------------------------------------------------------------------
# Item 9: fallback static server caches bytes + supports ETag / 304
# ---------------------------------------------------------------------------


@test("item9: static file cached (no re-read) + If-None-Match -> 304")
def test_item9_static_cache_and_304():
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "asset.txt"
        f.write_bytes(b"hello static world")

        app = HyperApp(static=tmp, debug=False)
        client = TestClient(app)

        reads = {"n": 0}
        orig_read_bytes = pathlib.Path.read_bytes

        def counting_read_bytes(self):
            reads["n"] += 1
            return orig_read_bytes(self)

        pathlib.Path.read_bytes = counting_read_bytes
        try:
            n0 = reads["n"]
            r1 = client.get("/asset.txt")
            first_reads = reads["n"] - n0
            assert r1.status == 200, f"expected 200, got {r1.status}"
            assert r1.body == b"hello static world"
            etag = r1.headers.get("etag")
            assert etag, "static response must carry an ETag"
            assert first_reads == 1, (
                f"first request should read once, got {first_reads}"
            )

            # Second request, unchanged file: served from cache, no re-read.
            n1 = reads["n"]
            r2 = client.get("/asset.txt")
            second_reads = reads["n"] - n1
            assert r2.status == 200
            assert second_reads == 0, (
                f"cached request must not re-read the file, got {second_reads}"
            )

            # Conditional request: If-None-Match -> 304, empty body, no re-read.
            n2 = reads["n"]
            r3 = client.get("/asset.txt", headers={"if-none-match": etag})
            cond_reads = reads["n"] - n2
            assert r3.status == 304, f"expected 304, got {r3.status}"
            assert r3.body == b""
            assert cond_reads == 0, f"304 path must not re-read, got {cond_reads}"
        finally:
            pathlib.Path.read_bytes = orig_read_bytes


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for _name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nws8 middleware/auth/security fixes ({len(tests)} tests)")
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
    sys.exit(asyncio.run(main()))
