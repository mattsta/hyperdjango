"""Tests for standalone Router."""

from hyperdjango.router import Route, Router


class TestRoute:
    def test_static_route_match(self):
        route = Route("GET", "/users", lambda r: None)
        assert route.match("/users") == {}
        assert route.match("/other") is None

    def test_dynamic_route_match(self):
        route = Route("GET", "/users/{id}", lambda r: None)
        assert route.match("/users/42") == {"id": "42"}
        assert route.match("/users/") is None
        assert route.match("/other/42") is None

    def test_typed_param(self):
        route = Route("GET", "/users/{id:int}", lambda r: None)
        result = route.match("/users/42")
        assert result == {"id": 42}
        assert route.match("/users/abc") is None

    def test_multiple_params(self):
        route = Route("GET", "/users/{user_id:int}/posts/{post_id:int}", lambda r: None)
        result = route.match("/users/1/posts/99")
        assert result == {"user_id": 1, "post_id": 99}

    def test_slug_param(self):
        route = Route("GET", "/posts/{slug:slug}", lambda r: None)
        result = route.match("/posts/hello-world")
        assert result == {"slug": "hello-world"}

    def test_is_static(self):
        assert Route("GET", "/users", lambda r: None).is_static
        assert not Route("GET", "/users/{id}", lambda r: None).is_static


class TestRouter:
    def test_static_route(self):
        router = Router()
        handler = lambda r: "ok"
        router.add("GET", "/health", handler)

        route, params = router.resolve("GET", "/health")
        assert route is not None
        assert route.handler is handler
        assert params == {}

    def test_dynamic_route(self):
        router = Router()
        handler = lambda r, id: f"user {id}"
        router.add("GET", "/users/{id:int}", handler)

        route, params = router.resolve("GET", "/users/42")
        assert route is not None
        assert params == {"id": 42}

    def test_no_match(self):
        router = Router()
        route, params = router.resolve("GET", "/nonexistent")
        assert route is None

    def test_method_mismatch(self):
        router = Router()
        router.add("POST", "/users", lambda r: None)
        route, _ = router.resolve("GET", "/users")
        assert route is None

    def test_head_auto_registered(self):
        router = Router()
        router.add("GET", "/health", lambda r: None)
        route, _ = router.resolve("HEAD", "/health")
        assert route is not None

    def test_decorator_registration(self):
        router = Router()

        @router.get("/hello")
        def hello(request):
            return "hi"

        route, _ = router.resolve("GET", "/hello")
        assert route is not None
        assert route.handler is hello

    def test_multiple_methods(self):
        router = Router()

        @router.route("/items", methods=["GET", "POST"])
        def items(request):
            return "items"

        get_route, _ = router.resolve("GET", "/items")
        post_route, _ = router.resolve("POST", "/items")
        assert get_route is not None
        assert post_route is not None

    def test_routes_listing(self):
        router = Router()
        router.add("GET", "/a", lambda r: None)
        router.add("POST", "/b", lambda r: None)
        assert len(router.routes()) == 2

    def test_static_preferred_over_dynamic(self):
        router = Router()
        static_handler = lambda r: "static"
        dynamic_handler = lambda r, name: "dynamic"

        router.add("GET", "/users/me", static_handler)
        router.add("GET", "/users/{name}", dynamic_handler)

        route, params = router.resolve("GET", "/users/me")
        assert route.handler is static_handler
        assert params == {}
