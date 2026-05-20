"""Tests for standalone HyperApp."""

from hyperdjango import HTTPException, HyperApp, Request, Response


class TestHyperApp:
    def test_create_app(self):
        app = HyperApp(title="Test")
        assert app.title == "Test"

    def test_register_route(self):
        app = HyperApp()

        @app.get("/health")
        async def health(request):
            return Response.json({"status": "ok"})

        routes = app.router.routes()
        assert len(routes) == 1
        assert routes[0].pattern == "/health"

    async def test_handle_request(self):
        app = HyperApp()

        @app.get("/hello")
        async def hello(request):
            return Response.json({"msg": "hi"})

        req = Request(method="GET", path="/hello")
        resp = await app.handle(req)
        assert resp.status == 200
        assert b"hi" in resp.body

    async def test_handle_404(self):
        app = HyperApp()
        req = Request(method="GET", path="/nonexistent")
        resp = await app.handle(req)
        assert resp.status == 404

    async def test_dict_auto_json(self):
        app = HyperApp()

        @app.get("/data")
        async def data(request):
            return {"key": "value"}

        req = Request(method="GET", path="/data")
        resp = await app.handle(req)
        assert resp.status == 200
        assert b"key" in resp.body

    async def test_string_auto_text(self):
        app = HyperApp()

        @app.get("/text")
        async def text(request):
            return "Hello World"

        req = Request(method="GET", path="/text")
        resp = await app.handle(req)
        assert resp.body == b"Hello World"

    async def test_path_params(self):
        app = HyperApp()

        @app.get("/users/{id:int}")
        async def get_user(request, id):
            return {"id": id}

        req = Request(method="GET", path="/users/42")
        resp = await app.handle(req)
        assert resp.status == 200
        assert b"42" in resp.body

    async def test_http_exception(self):
        app = HyperApp()

        @app.get("/fail")
        async def fail(request):
            raise HTTPException(403, "Forbidden")

        req = Request(method="GET", path="/fail")
        resp = await app.handle(req)
        assert resp.status == 403
        assert b"Forbidden" in resp.body

    async def test_sync_handler(self):
        app = HyperApp()

        @app.get("/sync")
        def sync_handler(request):
            return {"sync": True}

        req = Request(method="GET", path="/sync")
        resp = await app.handle(req)
        assert resp.status == 200

    async def test_multiple_methods(self):
        app = HyperApp()

        @app.get("/items")
        async def list_items(request):
            return {"items": []}

        @app.post("/items")
        async def create_item(request):
            return Response.json({"created": True}, status=201)

        get_resp = await app.handle(Request(method="GET", path="/items"))
        assert get_resp.status == 200

        post_resp = await app.handle(Request(method="POST", path="/items"))
        assert post_resp.status == 201


class TestExceptionHandlerPrecedence:
    """Regression: a catch-all Exception handler must NOT swallow an
    intentional HTTPException(4xx) into a 500.

    An HTTPException carries its own status/detail. Only a handler registered
    specifically for HTTPException (or a subclass) may override the built-in
    HTTPException mapping — a generic ``@app.exception_handler(Exception)``
    catch-all must not, even though HTTPException is a subclass of Exception.
    """

    async def test_catch_all_exception_does_not_swallow_http_exception(self):
        app = HyperApp()

        @app.exception_handler(Exception)
        async def catch_all(request, exc):
            return Response.json({"detail": "Internal server error"}, status=500)

        @app.get("/forbidden")
        async def forbidden(request):
            raise HTTPException(403, "Nope")

        resp = await app.handle(Request(method="GET", path="/forbidden"))
        assert resp.status == 403
        assert b"Nope" in resp.body
        assert b"Internal server error" not in resp.body

    async def test_catch_all_still_handles_non_http_exceptions(self):
        app = HyperApp()

        @app.exception_handler(Exception)
        async def catch_all(request, exc):
            return Response.json({"detail": "custom-500"}, status=500)

        @app.get("/boom")
        async def boom(request):
            raise ValueError("kaboom")

        resp = await app.handle(Request(method="GET", path="/boom"))
        assert resp.status == 500
        assert b"custom-500" in resp.body

    async def test_http_exception_specific_handler_still_wins(self):
        app = HyperApp()

        @app.exception_handler(Exception)
        async def catch_all(request, exc):
            return Response.json({"detail": "internal"}, status=500)

        @app.exception_handler(HTTPException)
        async def http_handler(request, exc):
            return Response.json({"custom_http": exc.detail}, status=exc.status_code)

        @app.get("/denied")
        async def denied(request):
            raise HTTPException(429, "slow down")

        resp = await app.handle(Request(method="GET", path="/denied"))
        assert resp.status == 429
        assert b"custom_http" in resp.body
        assert b"slow down" in resp.body

    async def test_http_exception_subclass_specific_handler_wins(self):
        app = HyperApp()

        class RateLimited(HTTPException):
            pass

        @app.exception_handler(Exception)
        async def catch_all(request, exc):
            return Response.json({"detail": "internal"}, status=500)

        @app.exception_handler(RateLimited)
        async def rl_handler(request, exc):
            return Response.json({"rate_limited": True}, status=exc.status_code)

        @app.get("/rl")
        async def rl(request):
            raise RateLimited(429, "too many")

        resp = await app.handle(Request(method="GET", path="/rl"))
        assert resp.status == 429
        assert b"rate_limited" in resp.body


class TestMiddleware:
    async def test_middleware_wraps_handler(self):
        app = HyperApp()

        @app.middleware
        async def add_header(request, call_next):
            response = await call_next(request)
            response.headers["x-custom"] = "added"
            return response

        @app.get("/test")
        async def test_view(request):
            return Response.text("ok")

        req = Request(method="GET", path="/test")
        resp = await app.handle(req)
        assert resp.headers["x-custom"] == "added"

    async def test_middleware_ordering(self):
        app = HyperApp()
        order = []

        @app.middleware
        async def mw1(request, call_next):
            order.append("mw1_before")
            resp = await call_next(request)
            order.append("mw1_after")
            return resp

        @app.middleware
        async def mw2(request, call_next):
            order.append("mw2_before")
            resp = await call_next(request)
            order.append("mw2_after")
            return resp

        @app.get("/test")
        async def test_view(request):
            order.append("handler")
            return Response.text("ok")

        await app.handle(Request(method="GET", path="/test"))
        assert order == [
            "mw1_before",
            "mw2_before",
            "handler",
            "mw2_after",
            "mw1_after",
        ]


class TestASGI:
    async def test_asgi_interface(self):
        app = HyperApp()

        @app.get("/asgi")
        async def asgi_view(request):
            return Response.json({"asgi": True})

        # Simulate ASGI
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/asgi",
            "query_string": b"",
            "headers": [],
        }

        received = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            received.append(message)

        await app(scope, receive, send)
        assert len(received) == 2  # response.start + response.body
        assert received[0]["status"] == 200
