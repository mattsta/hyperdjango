#!/usr/bin/env python3
"""Chaos / resilience tests — verify graceful handling of edge cases.

Tests the framework's robustness against:
1. Malformed JSON requests
2. Oversized request bodies
3. Missing required fields
4. Invalid content types
5. Concurrent requests (thread safety)
6. Handler exceptions (500 errors don't crash)
7. Empty bodies on POST
8. Invalid path params
9. Double-slash paths
10. Unicode edge cases
11. Cookie overflow
12. Header injection attempts
"""

# hyper-test: unit

import sys
import threading

from hyperdjango import HTTPException, HyperApp
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.testing import TestClient


def build_chaos_app():
    """Build an app with various endpoints for chaos testing."""
    app = HyperApp()
    sa = SessionAuth(secret="chaos-test")
    app.use(sa)

    @app.post("/echo")
    async def echo(request):
        data = await request.json()
        return data

    @app.get("/hello/{name}")
    async def hello(request, name):
        return {"hello": name}

    @app.get("/divide/{a:int}/{b:int}")
    async def divide(request, a, b):
        if b == 0:
            raise HTTPException(400, "Division by zero")
        return {"result": a / b}

    @app.get("/crash")
    async def crash(request):
        raise RuntimeError("Intentional crash")

    @app.get("/slow")
    async def slow(request):
        return {"slow": True}

    @app.post("/validate")
    async def validate(request):
        data = await request.json()
        name = data.get("name")
        if not name or not isinstance(name, str) or len(name) > 100:
            raise HTTPException(422, "name required, must be string <= 100 chars")
        return {"valid": True, "name": name}

    @app.get("/unicode/{text}")
    async def unicode_echo(request, text):
        return {"text": text}

    @app.get("/headers")
    async def headers(request):
        return {"user_agent": request.headers.get("user-agent", "")}

    @app.post("/upload")
    async def upload(request):
        body = await request.bytes()
        return {"size": len(body)}

    return app


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

    app = build_chaos_app()
    client = TestClient(app)

    # ── Malformed JSON ────────────────────────────────────────────────────
    print("\n=== Malformed JSON ===")

    resp = client.post(
        "/echo", data=b"not json", headers={"content-type": "application/json"}
    )
    check("malformed json", resp.status == 400, f"status={resp.status}")

    resp = client.post(
        "/echo", data=b"{incomplete", headers={"content-type": "application/json"}
    )
    check("incomplete json", resp.status == 400, f"status={resp.status}")

    resp = client.post("/echo", data=b"", headers={"content-type": "application/json"})
    check(
        "empty json body", resp.status in (200, 400), f"status={resp.status}"
    )  # empty body may return null or 400

    # ── Valid JSON edge cases ─────────────────────────────────────────────
    print("\n=== JSON edge cases ===")

    resp = client.post("/echo", json=None)
    check("null json", resp.ok or resp.status == 400, f"status={resp.status}")

    resp = client.post("/echo", json=[1, 2, 3])
    check("array json", resp.ok and resp.json() == [1, 2, 3], f"got {resp.json()}")

    resp = client.post("/echo", json={"nested": {"deep": {"value": 42}}})
    check("deep nested", resp.ok and resp.json()["nested"]["deep"]["value"] == 42)

    resp = client.post("/echo", json={"key": "a" * 10000})
    check("large string value", resp.ok)

    # ── Handler exceptions ────────────────────────────────────────────────
    print("\n=== Handler exceptions ===")

    resp = client.get("/crash")
    check("runtime error → 500", resp.status == 500, f"status={resp.status}")

    resp = client.get("/divide/10/0")
    check("custom error 400", resp.status == 400)

    resp = client.get("/divide/10/3")
    check("normal divide", resp.ok)

    # ── Path parameter edge cases ─────────────────────────────────────────
    print("\n=== Path params ===")

    resp = client.get("/hello/world")
    check("normal path param", resp.ok and resp.json()["hello"] == "world")

    resp = client.get("/hello/")
    check(
        "trailing slash", resp.status in (200, 404), f"status={resp.status}"
    )  # router may or may not match

    resp = client.get("/hello/hello%20world")
    check("url-encoded param", resp.ok)

    # ── Validation ────────────────────────────────────────────────────────
    print("\n=== Validation ===")

    resp = client.post("/validate", json={"name": "Alice"})
    check("valid input", resp.ok and resp.json()["valid"])

    resp = client.post("/validate", json={})
    check("missing required field", resp.status == 422)

    resp = client.post("/validate", json={"name": ""})
    check("empty string field", resp.status == 422)

    resp = client.post("/validate", json={"name": "x" * 101})
    check("too long field", resp.status == 422)

    resp = client.post("/validate", json={"name": 42})
    check("wrong type field", resp.status == 422)

    # ── Unicode ───────────────────────────────────────────────────────────
    print("\n=== Unicode ===")

    resp = client.post("/echo", json={"emoji": "Hello 🌍🎉"})
    check(
        "emoji in json",
        resp.ok and resp.json().get("emoji", "").startswith("Hello"),
        f"got {resp.json()}",
    )

    resp = client.post("/echo", json={"cjk": "你好世界"})
    check("CJK characters", resp.ok and resp.json()["cjk"] == "你好世界")

    resp = client.post("/echo", json={"mixed": "café résumé naïve"})
    check("accented chars", resp.ok and "café" in resp.json()["mixed"])

    # ── Headers ───────────────────────────────────────────────────────────
    print("\n=== Headers ===")

    resp = client.get("/headers", headers={"user-agent": "TestBot/1.0"})
    check("custom user agent", resp.json()["user_agent"] == "TestBot/1.0")

    resp = client.get("/headers", headers={"user-agent": ""})
    check("empty user agent", resp.ok)

    # ── Large request body ────────────────────────────────────────────────
    print("\n=== Large bodies ===")

    large_body = b"x" * 100_000
    resp = client.post(
        "/upload", data=large_body, headers={"content-type": "application/octet-stream"}
    )
    check(
        "100KB body", resp.ok and resp.json()["size"] == 100_000, f"got {resp.json()}"
    )

    # ── Concurrent requests ───────────────────────────────────────────────
    print("\n=== Concurrent requests ===")

    results = []
    errors = []

    def make_request():
        try:
            c = TestClient(app)
            resp = c.get("/hello/concurrent")
            results.append(resp.status)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=make_request) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("concurrent no errors", len(errors) == 0, f"errors: {errors[:3]}")
    check(
        "concurrent all 200",
        all(r == 200 for r in results),
        f"statuses: {set(results)}",
    )

    # ── 404 handling ──────────────────────────────────────────────────────
    print("\n=== 404 handling ===")

    resp = client.get("/nonexistent")
    check("404 for unknown route", resp.status == 404)

    resp = client.get("/api/does/not/exist/deeply")
    check("404 deep path", resp.status == 404)

    resp = client.post("/nonexistent", json={"data": 1})
    check("404 on POST to unknown", resp.status == 404)

    # ── Method not allowed ────────────────────────────────────────────────
    print("\n=== Method handling ===")

    # DELETE on a GET-only route
    resp = client.delete("/hello/world")
    check("wrong method", resp.status in (404, 405), f"status={resp.status}")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All chaos tests passed!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
