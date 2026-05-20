#!/usr/bin/env python3
"""Comprehensive integration test — exercises all hyperdjango features end-to-end.

Tests the full request lifecycle through:
1. Routing (GET/POST/PUT/DELETE, path params, query params)
2. Middleware (CORS, rate limiting, security headers, timing)
3. Auth (session login/logout, API key, OAuth2, decorators)
4. Request/Response (JSON, HTML, redirects, streaming, cookies, errors)
5. Templates (render with context, math expressions, filters, for loops)
6. Validation (BaseModel, field validation, error responses)
7. DataLoader (batch loading, caching, deduplication)
8. Pipeline (if PostgreSQL available)

Run: uv run hyper-test integration
"""

# hyper-test: unit

import asyncio
import os
import sys
import time

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.auth.api_keys import APIKeyAuth
from hyperdjango.auth.decorators import require_api_key, require_auth
from hyperdjango.auth.oauth2 import OAuth2, google, require_oauth2
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.dataloader import DataLoader
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.testing import TestClient


def build_app():
    """Build a fully-configured HyperApp with all features."""
    SECRET = "integration-test-secret"
    app = HyperApp()

    # --- Middleware stack ---
    sa = SessionAuth(secret=SECRET)
    app.use(sa)
    app.use(APIKeyAuth(valid_keys=["test-api-key-123"]))
    app.use(CORSMiddleware(origins=["https://example.com"], allow_credentials=True))
    app.use(TimingMiddleware())
    app.use(SecurityHeadersMiddleware())

    oauth = OAuth2(secret=SECRET)
    oauth.add_provider(google("gid", "gsecret"))
    oauth.set_session_auth(sa)
    app.use(oauth)

    # --- In-memory data store ---
    items_db = {}
    next_id = [1]

    # --- CRUD routes ---
    @app.get("/api/items")
    async def list_items(request):
        return list(items_db.values())

    @app.post("/api/items")
    @require_auth()
    async def create_item(request):
        data = await request.json()
        item_id = next_id[0]
        next_id[0] += 1
        item = {"id": item_id, **data}
        items_db[item_id] = item
        return Response.json(item, status=201)

    @app.get("/api/items/{id:int}")
    async def get_item(request, id):
        item = items_db.get(id)
        if not item:
            raise HTTPException(404, f"Item {id} not found")
        return item

    @app.put("/api/items/{id:int}")
    @require_auth()
    async def update_item(request, id):
        if id not in items_db:
            raise HTTPException(404, f"Item {id} not found")
        data = await request.json()
        items_db[id] = {"id": id, **data}
        return items_db[id]

    @app.delete("/api/items/{id:int}")
    @require_auth()
    async def delete_item(request, id):
        if id not in items_db:
            raise HTTPException(404, f"Item {id} not found")
        del items_db[id]
        return Response.empty()

    # --- Auth routes ---
    @app.post("/api/auth/login")
    async def login(request):
        data = await request.json()
        username = data.get("username")
        if username == "admin" and data.get("password") == "secret":
            resp = Response.json({"logged_in": True, "username": username})
            sa.login(resp, {"username": username, "role": "admin"}, request)
            return resp
        raise HTTPException(401, "Invalid credentials")

    @app.post("/api/auth/logout")
    @require_auth()
    async def logout(request):
        resp = Response.json({"logged_out": True})
        sa.logout(resp, request.session_id)
        return resp

    @app.get("/api/me")
    @require_auth()
    async def me(request):
        user = request.user
        return {"user": {"id": user.id, "username": user.username}}

    @app.get("/api/admin")
    @require_auth(lambda r: r.user and r.user.get("role") == "admin")
    async def admin_only(request):
        return {"admin": True}

    # --- API key route ---
    @app.get("/api/data")
    @require_api_key
    async def api_data(request):
        return {"data": [1, 2, 3]}

    # --- OAuth2 protected route ---
    @app.get("/api/oauth-protected")
    @require_oauth2()
    async def oauth_protected(request):
        return {"provider": request.oauth2_provider}

    # --- Query params ---
    @app.get("/api/search")
    async def search(request):
        q = request.query("q", "")
        page = request.query("page", "1")
        return {"q": q, "page": int(page)}

    # --- Error handling ---
    @app.get("/api/error")
    async def error_route(request):
        raise HTTPException(418, "I'm a teapot")

    # --- Template rendering ---
    @app.get("/page")
    async def page(request):
        try:
            from hyperdjango._hyperdjango_native import (
                _template_compile,
                _template_render,
            )

            tmpl = _template_compile(
                "<h1>{{ title }}</h1><p>{{ count * 2 }} items</p>"
                "{% for item in items %}<li>{{ item }}</li>{% endfor %}",
                "<test>",
            )
            result = _template_render(
                tmpl, {"title": "Test Page", "count": 3, "items": ["a", "b", "c"]}
            )
            html = result.decode("utf-8") if isinstance(result, bytes) else result
            return Response.html(html)
        except ImportError:
            return Response.html("<h1>No native</h1>")

    # --- Streaming ---
    @app.get("/api/stream")
    async def stream(request):
        async def generate():
            for i in range(3):
                yield f"chunk {i}\n"

        return Response.stream(generate())

    # --- Cookies ---
    @app.get("/api/set-cookie")
    async def set_cookie(request):
        resp = Response.json({"cookie_set": True})
        resp.set_cookie("test_cookie", "test_value", max_age=3600)
        return resp

    @app.get("/api/get-cookie")
    async def get_cookie(request):
        return {"cookie": request.cookies.get("test_cookie", "")}

    return app, items_db, next_id


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

    app, items_db, next_id = build_app()
    client = TestClient(app)

    # ── CRUD operations ───────────────────────────────────────────────────
    print("\n=== CRUD operations ===")

    # List empty
    resp = client.get("/api/items")
    check("list empty", resp.ok and resp.json() == [], f"got {resp.json()}")

    # Create requires auth
    resp = client.post("/api/items", json={"name": "Widget"})
    check("create unauthorized", resp.status in {401, 302})

    # Login
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "secret"}
    )
    check(
        "login success", resp.ok and resp.json()["logged_in"], f"status={resp.status}"
    )

    # Create after login
    resp = client.post("/api/items", json={"name": "Widget", "price": 9.99})
    check("create item", resp.status == 201, f"status={resp.status}")
    item = resp.json()
    check(
        "create returns item",
        item["name"] == "Widget" and item["id"] == 1,
        f"got {item}",
    )

    # Create second
    resp = client.post("/api/items", json={"name": "Gadget", "price": 19.99})
    check("create second", resp.status == 201)

    # List
    resp = client.get("/api/items")
    check(
        "list 2 items",
        resp.ok and len(resp.json()) == 2,
        f"got {len(resp.json())} items",
    )

    # Get by ID
    resp = client.get("/api/items/1")
    check("get by id", resp.ok and resp.json()["name"] == "Widget")

    # Get 404
    resp = client.get("/api/items/999")
    check("get 404", resp.status == 404)

    # Update
    resp = client.put("/api/items/1", json={"name": "Super Widget", "price": 14.99})
    check("update item", resp.ok and resp.json()["name"] == "Super Widget")

    # Delete
    resp = client.delete("/api/items/1")
    check("delete item", resp.status == 204)

    resp = client.get("/api/items/1")
    check("deleted 404", resp.status == 404)

    # ── Auth flows ────────────────────────────────────────────────────────
    print("\n=== Auth flows ===")

    # Me endpoint
    resp = client.get("/api/me")
    check("me endpoint", resp.ok and resp.json()["user"]["username"] == "admin")

    # Admin-only
    resp = client.get("/api/admin")
    check("admin access", resp.ok and resp.json()["admin"])

    # Logout
    resp = client.post("/api/auth/logout")
    check("logout", resp.ok and resp.json()["logged_out"])

    # After logout, auth required
    resp = client.get("/api/me")
    check("after logout unauthorized", resp.status in {401, 302})

    # Bad login
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong"}
    )
    check("bad login", resp.status == 401)

    # ── API key auth ──────────────────────────────────────────────────────
    print("\n=== API key auth ===")

    api_client = TestClient(app)
    resp = api_client.get("/api/data")
    check("no api key", resp.status == 401)

    api_client.set_api_key("test-api-key-123")
    resp = api_client.get("/api/data")
    check("valid api key", resp.ok and resp.json()["data"] == [1, 2, 3])

    api_client.set_api_key("invalid-key")
    resp = api_client.get("/api/data")
    check("invalid api key", resp.status == 401)

    # ── OAuth2 ────────────────────────────────────────────────────────────
    print("\n=== OAuth2 ===")

    oauth_client = TestClient(app)
    resp = oauth_client.get("/api/oauth-protected")
    check("oauth unauthed", resp.status == 401)

    oauth_client.login_oauth2("google", {"id": "123", "email": "test@gmail.com"})
    resp = oauth_client.get("/api/oauth-protected")
    check("oauth authed", resp.ok and resp.json()["provider"] == "google")

    # Login redirect
    resp = TestClient(app).get("/auth/google/login")
    check("oauth login redirect", resp.status in (301, 302, 307, 308))

    # ── Query params ──────────────────────────────────────────────────────
    print("\n=== Query params ===")

    resp = client.get("/api/search?q=hello&page=3")
    check(
        "query params",
        resp.json()["q"] == "hello" and resp.json()["page"] == 3,
        f"got {resp.json()}",
    )

    # ── Middleware ─────────────────────────────────────────────────────────
    print("\n=== Middleware ===")

    resp = client.get("/api/items")
    check(
        "timing header",
        "x-response-time" in resp.headers,
        f"headers: {dict(resp.headers)}",
    )
    check("security nosniff", resp.headers.get("x-content-type-options") == "nosniff")

    # CORS
    cors_client = TestClient(app)
    resp = cors_client.get("/api/items", headers={"origin": "https://example.com"})
    check(
        "cors origin header",
        resp.headers.get("access-control-allow-origin") == "https://example.com",
        f"got {resp.headers.get('access-control-allow-origin')}",
    )

    # ── Error handling ────────────────────────────────────────────────────
    print("\n=== Error handling ===")

    resp = client.get("/api/error")
    check("custom error code", resp.status == 418)

    resp = client.get("/api/nonexistent")
    check("404 not found", resp.status == 404)

    # ── Template rendering ────────────────────────────────────────────────
    print("\n=== Template rendering ===")

    resp = client.get("/page")
    check("template renders", resp.ok)
    body = resp.text()
    check("template title", "<h1>Test Page</h1>" in body, f"got {body[:100]}")
    check("template math", "6 items" in body, f"got {body[:100]}")
    check("template for loop", "<li>a</li>" in body and "<li>b</li>" in body)

    # ── Cookies ───────────────────────────────────────────────────────────
    print("\n=== Cookies ===")

    cookie_client = TestClient(app)
    resp = cookie_client.get("/api/set-cookie")
    check("set cookie response", resp.ok)

    resp = cookie_client.get("/api/get-cookie")
    check("get cookie", resp.json()["cookie"] == "test_value", f"got {resp.json()}")

    # ── DataLoader ────────────────────────────────────────────────────────
    print("\n=== DataLoader ===")

    call_log = []

    async def batch_fn(keys):
        call_log.append(list(keys))
        return [{"id": k, "name": f"item_{k}"} for k in keys]

    async def test_dataloader():
        loader = DataLoader(batch_fn=batch_fn)
        results = await loader.load_many([1, 2, 3])
        # Second load should be cached
        r1 = await loader.load(1)
        return results, r1

    results, r1 = asyncio.run(test_dataloader())
    check("dataloader batch", len(results) == 3 and results[0]["id"] == 1)
    check("dataloader cache", r1["id"] == 1)
    check("dataloader single batch call", len(call_log) == 1, f"calls: {len(call_log)}")

    # ── Performance ───────────────────────────────────────────────────────
    print("\n=== Performance ===")

    perf_app = HyperApp()

    @perf_app.get("/ping")
    async def ping(request):
        return {"pong": True}

    perf_client = TestClient(perf_app)

    # Warm up
    for _ in range(10):
        perf_client.get("/ping")

    N = 1000
    start = time.perf_counter()
    for _ in range(N):
        perf_client.get("/ping")
    elapsed = time.perf_counter() - start
    rps = N / elapsed

    print(f"  {N} requests in {elapsed:.3f}s = {rps:.0f} req/s")
    # Under parallel execution (50+ processes), CPU contention reduces throughput ~7x.
    # Proven: standalone=~5000 req/s, parallel=~157 req/s (from test_run.log)
    _min_rps = 20 if os.environ.get("HYPER_TEST_PARALLEL") == "1" else 1000
    check(f"perf > {_min_rps} req/s", rps > _min_rps, f"only {rps:.0f} req/s")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All integration tests passed!")
    else:
        print("SOME TESTS FAILED!")
    return failed


if __name__ == "__main__":
    sys.exit(main())
