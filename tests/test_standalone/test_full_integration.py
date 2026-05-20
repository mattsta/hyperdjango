"""Full integration tests — routes, middleware, TestClient, auth, error handling."""

import pytest

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.testing import TestClient

# ---- Basic route + TestClient integration ----


class TestBasicRouting:
    def test_get_json(self):
        app = HyperApp()

        @app.get("/api/status")
        async def status(request):
            return {"status": "ok"}

        client = TestClient(app)
        resp = client.get("/api/status")
        assert resp.ok
        assert resp.json() == {"status": "ok"}

    def test_post_json_echo(self):
        app = HyperApp()

        @app.post("/echo")
        async def echo(request):
            data = await request.json()
            return data

        client = TestClient(app)
        resp = client.post("/echo", json={"msg": "hello"})
        assert resp.ok
        assert resp.json()["msg"] == "hello"

    def test_put_method(self):
        app = HyperApp()

        @app.put("/items/{id:int}")
        async def update_item(request, id):
            data = await request.json()
            return {"id": id, "updated": data}

        client = TestClient(app)
        resp = client.put("/items/5", json={"name": "New"})
        assert resp.ok
        body = resp.json()
        assert body["id"] == 5
        assert body["updated"]["name"] == "New"

    def test_delete_method(self):
        app = HyperApp()

        @app.delete("/items/{id:int}")
        async def delete_item(request, id):
            return {"deleted": id}

        client = TestClient(app)
        resp = client.delete("/items/3")
        assert resp.ok
        assert resp.json()["deleted"] == 3

    def test_patch_method(self):
        app = HyperApp()

        @app.patch("/items/{id:int}")
        async def patch_item(request, id):
            return {"patched": id}

        client = TestClient(app)
        resp = client.patch("/items/7", json={"name": "Patched"})
        assert resp.ok
        assert resp.json()["patched"] == 7

    def test_string_return_becomes_text(self):
        app = HyperApp()

        @app.get("/text")
        async def text_view(request):
            return "plain text"

        client = TestClient(app)
        resp = client.get("/text")
        assert resp.ok
        assert resp.text() == "plain text"

    def test_response_object_passthrough(self):
        app = HyperApp()

        @app.get("/custom")
        async def custom(request):
            return Response.html("<h1>Hello</h1>", status=201)

        client = TestClient(app)
        resp = client.get("/custom")
        assert resp.status == 201
        assert b"<h1>Hello</h1>" in resp.body


# ---- Path params and query params ----


class TestParamsIntegration:
    def test_int_path_param(self):
        app = HyperApp()

        @app.get("/users/{id:int}")
        async def get_user(request, id):
            return {"id": id, "type": type(id).__name__}

        client = TestClient(app)
        resp = client.get("/users/42")
        body = resp.json()
        assert body["id"] == 42
        assert body["type"] == "int"

    def test_string_path_param(self):
        app = HyperApp()

        @app.get("/users/{name}")
        async def get_user(request, name):
            return {"name": name}

        client = TestClient(app)
        resp = client.get("/users/alice")
        assert resp.json()["name"] == "alice"

    def test_multiple_path_params(self):
        app = HyperApp()

        @app.get("/orgs/{org}/repos/{repo}")
        async def get_repo(request, org, repo):
            return {"org": org, "repo": repo}

        client = TestClient(app)
        resp = client.get("/orgs/acme/repos/widget")
        body = resp.json()
        assert body["org"] == "acme"
        assert body["repo"] == "widget"

    def test_query_params_from_url(self):
        app = HyperApp()

        @app.get("/search")
        async def search(request):
            q = request.query("q")
            page = request.query("page")
            return {"q": q, "page": page}

        client = TestClient(app)
        resp = client.get("/search?q=hello&page=2")
        body = resp.json()
        assert body["q"] == "hello"
        assert body["page"] == "2"


# ---- Error handling ----


class TestErrorHandling:
    def test_404_not_found(self):
        app = HyperApp()
        client = TestClient(app)
        resp = client.get("/nonexistent")
        assert resp.status == 404
        assert not resp.ok
        body = resp.json()
        assert "Not Found" in body.get("detail", "")

    def test_http_exception_custom_status(self):
        app = HyperApp()

        @app.get("/forbidden")
        async def forbidden(request):
            raise HTTPException(403, "Access denied")

        client = TestClient(app)
        resp = client.get("/forbidden")
        assert resp.status == 403
        assert resp.json()["detail"] == "Access denied"

    def test_http_exception_422(self):
        app = HyperApp()

        @app.post("/validate")
        async def validate(request):
            raise HTTPException(422, "Invalid input")

        client = TestClient(app)
        resp = client.post("/validate", json={})
        assert resp.status == 422

    def test_500_internal_error_non_debug(self):
        app = HyperApp(debug=False)

        @app.get("/crash")
        async def crash(request):
            raise ValueError("boom")

        client = TestClient(app)
        resp = client.get("/crash")
        assert resp.status == 500
        body = resp.json()
        assert "Internal Server Error" in body["detail"]
        # Should NOT leak the traceback
        assert "boom" not in body.get("detail", "")

    def test_500_debug_mode_shows_traceback(self):
        app = HyperApp(debug=True)

        @app.get("/crash")
        async def crash(request):
            raise ValueError("debug_boom")

        client = TestClient(app)
        resp = client.get("/crash")
        assert resp.status == 500
        text = resp.text()
        assert "debug_boom" in text

    def test_http_exception_with_headers(self):
        app = HyperApp()

        @app.get("/rate-limited")
        async def rate_limited(request):
            raise HTTPException(429, "Too many requests", headers={"retry-after": "60"})

        client = TestClient(app)
        resp = client.get("/rate-limited")
        assert resp.status == 429
        assert resp.headers.get("retry-after") == "60"


# ---- Middleware pipeline ----


class TestMiddlewarePipeline:
    def test_single_middleware(self):
        app = HyperApp()

        @app.middleware
        async def add_header(request, call_next):
            resp = await call_next(request)
            resp.headers["x-custom"] = "yes"
            return resp

        @app.get("/test")
        async def view(request):
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["x-custom"] == "yes"

    def test_middleware_ordering(self):
        """First-added middleware should be outermost."""
        app = HyperApp()
        order = []

        @app.middleware
        async def mw1(request, call_next):
            order.append("mw1-before")
            resp = await call_next(request)
            order.append("mw1-after")
            return resp

        @app.middleware
        async def mw2(request, call_next):
            order.append("mw2-before")
            resp = await call_next(request)
            order.append("mw2-after")
            return resp

        @app.get("/test")
        async def view(request):
            order.append("handler")
            return {"ok": True}

        client = TestClient(app)
        client.get("/test")
        assert order == [
            "mw1-before",
            "mw2-before",
            "handler",
            "mw2-after",
            "mw1-after",
        ]

    def test_middleware_can_short_circuit(self):
        app = HyperApp()

        @app.middleware
        async def block(request, call_next):
            if request.path == "/blocked":
                return Response.json({"error": "blocked"}, status=403)
            return await call_next(request)

        @app.get("/blocked")
        async def blocked(request):
            return {"should": "not reach"}

        @app.get("/allowed")
        async def allowed(request):
            return {"ok": True}

        client = TestClient(app)
        assert client.get("/blocked").status == 403
        assert client.get("/allowed").ok

    def test_use_class_middleware(self):
        app = HyperApp()

        class TimingMiddleware:
            async def __call__(self, request, call_next):
                resp = await call_next(request)
                resp.headers["x-timed"] = "true"
                return resp

        app.use(TimingMiddleware())

        @app.get("/test")
        async def view(request):
            return "ok"

        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["x-timed"] == "true"


# ---- Auth flow integration ----


class TestAuthFlowIntegration:
    def test_login_access_protected_logout(self):
        from hyperdjango.auth.sessions import SessionAuth

        app = HyperApp()
        auth = SessionAuth(secret="integration-test-secret")
        app.use(auth)

        @app.post("/login")
        async def login(request):
            data = await request.json()
            resp = Response.json({"logged_in": True})
            auth.login(resp, {"id": 1, "username": data["name"]})
            return resp

        @app.get("/me")
        async def me(request):
            user = request.user
            if user and user.is_authenticated:
                return {"user": {"id": user.id, "username": user.username}}
            return Response.json({"error": "unauthorized"}, status=401)

        @app.post("/logout")
        async def logout(request):
            resp = Response.json({"logged_out": True})
            if request.session_id:
                auth.logout(resp, request.session_id)
            return resp

        client = TestClient(app)

        # Step 1: Not logged in
        resp = client.get("/me")
        assert resp.status == 401

        # Step 2: Log in
        resp = client.post("/login", json={"name": "Alice"})
        assert resp.ok
        assert resp.json()["logged_in"] is True

        # Step 3: Access protected route
        resp = client.get("/me")
        assert resp.ok
        user = resp.json()["user"]
        assert user["username"] == "Alice"
        assert user["id"] == 1

        # Step 4: Log out
        resp = client.post("/logout")
        assert resp.ok

        # Step 5: No longer authenticated (cookie cleared)
        resp = client.get("/me")
        assert resp.status == 401

    def test_api_key_auth_flow(self):
        from hyperdjango.auth.api_keys import APIKeyAuth
        from hyperdjango.auth.decorators import require_api_key

        app = HyperApp()
        app.use(APIKeyAuth(valid_keys={"key-abc-123"}))

        @app.get("/api/data")
        @require_api_key
        async def api_data(request):
            return {"data": "secret"}

        client = TestClient(app)

        # No key
        resp = client.get("/api/data")
        assert resp.status == 401

        # Valid key
        resp = client.get("/api/data", headers={"x-api-key": "key-abc-123"})
        assert resp.ok
        assert resp.json()["data"] == "secret"

        # Wrong key
        resp = client.get("/api/data", headers={"x-api-key": "wrong"})
        assert resp.status == 401

    def test_require_auth_decorator(self):
        from hyperdjango.auth.decorators import require_auth

        app = HyperApp()

        @app.get("/protected")
        @require_auth()
        async def protected(request):
            return {"access": "granted"}

        client = TestClient(app)
        resp = client.get("/protected")
        assert resp.status in {401, 302}  # Redirects to LOGIN_URL or returns 401


# ---- TestClient features ----


class TestTestClientFeatures:
    def test_cookies_persist_across_requests(self):
        app = HyperApp()

        @app.get("/set")
        async def set_cookie(request):
            resp = Response.json({"ok": True})
            resp.set_cookie("token", "xyz")
            return resp

        @app.get("/get")
        async def get_cookie(request):
            return {"token": request.cookies.get("token")}

        client = TestClient(app)
        client.get("/set")
        resp = client.get("/get")
        assert resp.json()["token"] == "xyz"

    def test_reset_cookies(self):
        app = HyperApp()

        @app.get("/set")
        async def set_cookie(request):
            resp = Response.json({"ok": True})
            resp.set_cookie("token", "abc")
            return resp

        @app.get("/get")
        async def get_cookie(request):
            return {"token": request.cookies.get("token")}

        client = TestClient(app)
        client.get("/set")
        client.reset_cookies()
        resp = client.get("/get")
        assert resp.json()["token"] is None

    def test_post_form_data(self):
        app = HyperApp()

        @app.post("/form")
        async def form(request):
            data = await request.form()
            name_list = data.get("name", [])
            return {"name": name_list[0] if name_list else None}

        client = TestClient(app)
        resp = client.post("/form", data={"name": "Bob"})
        assert resp.ok
        assert resp.json()["name"] == "Bob"

    def test_post_raw_bytes(self):
        app = HyperApp()

        @app.post("/raw")
        async def raw(request):
            return {"length": len(request.body)}

        client = TestClient(app)
        resp = client.post("/raw", data=b"\x00\x01\x02")
        assert resp.ok
        assert resp.json()["length"] == 3

    def test_test_response_repr(self):
        app = HyperApp()

        @app.get("/test")
        async def view(request):
            return "ok"

        client = TestClient(app)
        resp = client.get("/test")
        r = repr(resp)
        assert "TestResponse" in r
        assert "200" in r

    def test_test_response_ok_false_for_4xx(self):
        app = HyperApp()
        client = TestClient(app)
        resp = client.get("/missing")
        assert resp.ok is False

    def test_multiple_routes(self):
        app = HyperApp()

        @app.get("/a")
        async def a(request):
            return {"route": "a"}

        @app.get("/b")
        async def b(request):
            return {"route": "b"}

        @app.post("/c")
        async def c(request):
            return {"route": "c"}

        client = TestClient(app)
        assert client.get("/a").json()["route"] == "a"
        assert client.get("/b").json()["route"] == "b"
        assert client.post("/c").json()["route"] == "c"


# ---- File-based route discovery + TestClient ----


class TestFileBasedRoutesIntegration:
    def test_discover_and_serve(self, tmp_path):
        """Create a views directory, discover routes, and test with TestClient."""
        # Create views/index.py
        views_dir = tmp_path / "views"
        views_dir.mkdir()
        (views_dir / "index.py").write_text(
            "async def get(request):\n    return {'page': 'index'}\n"
        )

        # Create views/about.py
        (views_dir / "about.py").write_text(
            "async def get(request):\n    return {'page': 'about'}\n"
        )

        app = HyperApp()
        app.discover_routes(str(views_dir))

        client = TestClient(app)
        resp = client.get("/")
        assert resp.ok
        assert resp.json()["page"] == "index"

        resp = client.get("/about")
        assert resp.ok
        assert resp.json()["page"] == "about"

    def test_discover_nested_routes(self, tmp_path):
        views_dir = tmp_path / "views"
        api_dir = views_dir / "api"
        api_dir.mkdir(parents=True)

        (api_dir / "health.py").write_text(
            "async def get(request):\n    return {'status': 'healthy'}\n"
        )

        app = HyperApp()
        app.discover_routes(str(views_dir))

        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.ok
        assert resp.json()["status"] == "healthy"

    def test_discover_empty_dir(self, tmp_path):
        views_dir = tmp_path / "empty_views"
        views_dir.mkdir()

        app = HyperApp()
        app.discover_routes(str(views_dir))
        assert len(app.router.routes()) == 0

    def test_discover_nonexistent_dir(self, tmp_path):
        app = HyperApp()
        app.discover_routes(str(tmp_path / "does_not_exist"))
        assert len(app.router.routes()) == 0


# ---- HyperApp lifecycle and config ----


class TestHyperAppConfig:
    def test_default_title(self):
        app = HyperApp()
        assert app.title == "HyperDjango"

    def test_debug_default_false(self):
        import os

        old = os.environ.pop("HYPER_DEBUG", None)
        try:
            from hyperdjango.conf import _ENV_OVERRIDES

            _ENV_OVERRIDES.pop("DEBUG", None)
            app = HyperApp()
            assert app.debug is False
        finally:
            if old is not None:
                os.environ["HYPER_DEBUG"] = old

    def test_debug_explicit_true(self):
        app = HyperApp(debug=True)
        assert app.debug is True

    def test_database_access_raises_without_url(self):
        app = HyperApp()
        with pytest.raises(RuntimeError, match="No database configured"):
            _ = app.db

    def test_database_lazy_init(self):
        app = HyperApp(database="postgres://localhost/test")
        db = app.db
        assert db.url == "postgres://localhost/test"

    def test_websocket_handler_registration(self):
        app = HyperApp()

        @app.websocket("/ws/chat")
        async def chat(ws):
            pass

        assert "/ws/chat" in app._ws_handlers

    def test_on_startup_hook(self):
        app = HyperApp()
        called = []

        @app.on_startup
        def startup():
            called.append("startup")

        assert len(app._on_startup) == 1

    def test_on_shutdown_hook(self):
        app = HyperApp()
        called = []

        @app.on_shutdown
        def shutdown():
            called.append("shutdown")

        assert len(app._on_shutdown) == 1

    def test_route_decorator_all_methods(self):
        app = HyperApp()

        @app.route("/all", methods=["GET", "POST"])
        async def all_methods(request):
            return {"method": request.method}

        client = TestClient(app)
        assert client.get("/all").ok
        assert client.post("/all").ok


# ---- Sync handler support ----


class TestSyncHandlers:
    def test_sync_handler(self):
        app = HyperApp()

        @app.get("/sync")
        def sync_view(request):
            return {"sync": True}

        client = TestClient(app)
        resp = client.get("/sync")
        assert resp.ok
        assert resp.json()["sync"] is True


# ---- HTTPException ----


class TestHTTPException:
    def test_exception_attributes(self):
        exc = HTTPException(404, "Not Found")
        assert exc.status_code == 404
        assert exc.detail == "Not Found"
        assert exc.headers == {}

    def test_exception_with_headers(self):
        exc = HTTPException(429, "Rate limited", headers={"retry-after": "30"})
        assert exc.headers["retry-after"] == "30"

    def test_exception_str(self):
        exc = HTTPException(500, "Server Error")
        assert str(exc) == "Server Error"

    def test_is_exception(self):
        assert issubclass(HTTPException, Exception)
