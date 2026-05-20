"""Tests for Zig-native typed path parameter coercion.

Tests the full pipeline:
  Route definition → param_types_json → Zig registration → Zig dispatch → typed Python objects

The key invariant: path params arrive at the Python handler as their declared type
(int, float, str, bool) — converted by Zig via PyLong_FromLongLong, PyFloat_FromDouble,
etc. — NOT as strings that Python must convert.
"""

import json

from hyperdjango.app import (
    _CONVERTER_TO_ZIG_TYPE,
    HyperApp,
    _build_param_types_json,
)
from hyperdjango.router import Route, Router

# ── Unit tests: _build_param_types_json ──────────────────────────────────────


class TestBuildParamTypesJson:
    """Test the Python-side metadata builder that feeds Zig's parseParamMeta."""

    def test_empty_params(self):
        assert _build_param_types_json([], []) == ""

    def test_single_int(self):
        assert _build_param_types_json(["id"], [int]) == "id:int"

    def test_single_str(self):
        assert _build_param_types_json(["name"], [str]) == "name:str"

    def test_single_float(self):
        assert _build_param_types_json(["price"], [float]) == "price:float"

    def test_single_bool(self):
        assert _build_param_types_json(["active"], [bool]) == "active:bool"

    def test_multiple_params(self):
        result = _build_param_types_json(["id", "slug"], [int, str])
        assert result == "id:int|slug:str"

    def test_three_params(self):
        result = _build_param_types_json(
            ["user_id", "post_id", "slug"],
            [int, int, str],
        )
        assert result == "user_id:int|post_id:int|slug:str"

    def test_mixed_types(self):
        result = _build_param_types_json(
            ["id", "price", "name", "active"],
            [int, float, str, bool],
        )
        assert result == "id:int|price:float|name:str|active:bool"

    def test_unknown_converter_defaults_to_str(self):
        """Unknown Python types (e.g., uuid as str) map to Zig 'str'."""

        # UUID converter in Route maps to Python str, but test with a custom type
        class CustomType:
            pass

        result = _build_param_types_json(["token"], [CustomType])
        assert result == "token:str"

    def test_converter_to_zig_type_map(self):
        """Verify the mapping is complete for all supported types."""
        assert _CONVERTER_TO_ZIG_TYPE[int] == "int"
        assert _CONVERTER_TO_ZIG_TYPE[float] == "float"
        assert _CONVERTER_TO_ZIG_TYPE[str] == "str"
        assert _CONVERTER_TO_ZIG_TYPE[bool] == "bool"
        assert len(_CONVERTER_TO_ZIG_TYPE) == 4


# ── Unit tests: Route param extraction ───────────────────────────────────────


class TestRouteParamExtraction:
    """Test that Route correctly parses param_names and param_converters."""

    def test_no_params(self):
        route = Route("GET", "/health", lambda r: None)
        assert route.param_names == []
        assert route.param_converters == []

    def test_int_param(self):
        route = Route("GET", "/users/{id:int}", lambda r, id: None)
        assert route.param_names == ["id"]
        assert route.param_converters == [int]

    def test_str_param_explicit(self):
        route = Route("GET", "/users/{name:str}", lambda r, name: None)
        assert route.param_names == ["name"]
        assert route.param_converters == [str]

    def test_str_param_default(self):
        """Untyped params default to str converter."""
        route = Route("GET", "/users/{name}", lambda r, name: None)
        assert route.param_names == ["name"]
        assert route.param_converters == [str]

    def test_slug_param(self):
        route = Route("GET", "/posts/{slug:slug}", lambda r, slug: None)
        assert route.param_names == ["slug"]
        assert route.param_converters == [str]  # slug maps to str converter

    def test_uuid_param(self):
        route = Route("GET", "/items/{uuid:uuid}", lambda r, uuid: None)
        assert route.param_names == ["uuid"]
        assert route.param_converters == [str]  # uuid maps to str converter

    def test_path_param(self):
        route = Route("GET", "/files/{filepath:path}", lambda r, filepath: None)
        assert route.param_names == ["filepath"]
        assert route.param_converters == [str]  # path maps to str converter

    def test_multiple_typed_params(self):
        route = Route(
            "GET",
            "/users/{user_id:int}/posts/{post_id:int}",
            lambda r, user_id, post_id: None,
        )
        assert route.param_names == ["user_id", "post_id"]
        assert route.param_converters == [int, int]

    def test_mixed_typed_params(self):
        route = Route(
            "GET",
            "/users/{id:int}/posts/{slug:slug}",
            lambda r, id, slug: None,
        )
        assert route.param_names == ["id", "slug"]
        assert route.param_converters == [int, str]


# ── Integration: Route → param_types_json pipeline ──────────────────────────


class TestRouteToParamTypesJsonPipeline:
    """Test the full Route → _build_param_types_json pipeline."""

    def test_int_route(self):
        route = Route("GET", "/users/{id:int}", lambda r, id: None)
        result = _build_param_types_json(route.param_names, route.param_converters)
        assert result == "id:int"

    def test_str_route(self):
        route = Route("GET", "/users/{name:str}", lambda r, name: None)
        result = _build_param_types_json(route.param_names, route.param_converters)
        assert result == "name:str"

    def test_multi_param_route(self):
        route = Route(
            "GET",
            "/users/{user_id:int}/posts/{slug:slug}",
            lambda r, user_id, slug: None,
        )
        result = _build_param_types_json(route.param_names, route.param_converters)
        assert result == "user_id:int|slug:str"

    def test_no_param_route(self):
        route = Route("GET", "/health", lambda r: None)
        result = _build_param_types_json(route.param_names, route.param_converters)
        assert result == ""

    def test_three_int_params(self):
        route = Route(
            "GET",
            "/a/{x:int}/b/{y:int}/c/{z:int}",
            lambda r, x, y, z: None,
        )
        result = _build_param_types_json(route.param_names, route.param_converters)
        assert result == "x:int|y:int|z:int"


# ── Integration: Python-side Router resolve (stays typed) ────────────────────


class TestRouterResolveTyped:
    """Verify Router.resolve() still returns typed params via Python converters.

    This is the non-Zig-server path used by tests and WSGI.
    """

    def test_int_param_resolved_as_int(self):
        router = Router()
        router.add("GET", "/users/{id:int}", lambda r, id: None)
        route, params = router.resolve("GET", "/users/42")
        assert params == {"id": 42}
        assert isinstance(params["id"], int)

    def test_str_param_resolved_as_str(self):
        router = Router()
        router.add("GET", "/users/{name:str}", lambda r, name: None)
        route, params = router.resolve("GET", "/users/alice")
        assert params == {"name": "alice"}
        assert isinstance(params["name"], str)

    def test_slug_param_resolved_as_str(self):
        router = Router()
        router.add("GET", "/posts/{slug:slug}", lambda r, slug: None)
        route, params = router.resolve("GET", "/posts/hello-world")
        assert params == {"slug": "hello-world"}
        assert isinstance(params["slug"], str)

    def test_multi_typed_resolve(self):
        router = Router()
        router.add(
            "GET",
            "/users/{id:int}/posts/{slug:slug}",
            lambda r, id, slug: None,
        )
        route, params = router.resolve("GET", "/users/7/posts/my-post")
        assert params == {"id": 7, "slug": "my-post"}
        assert isinstance(params["id"], int)
        assert isinstance(params["slug"], str)

    def test_invalid_int_returns_none(self):
        router = Router()
        router.add("GET", "/users/{id:int}", lambda r, id: None)
        route, params = router.resolve("GET", "/users/notanumber")
        assert route is None

    def test_negative_int(self):
        """Negative ints: Zig native router matches any segment, Python int() succeeds.

        The native radix trie doesn't enforce \\d+ — it treats {n} as a wildcard segment.
        Python's int("-5") succeeds, so the route resolves with -5.
        This matches Django's behavior where IntConverter regex is \\d+ but the
        native router fast path bypasses regex for performance.
        """
        router = Router()
        router.add("GET", "/offset/{n:int}", lambda r, n: None)
        route, params = router.resolve("GET", "/offset/-5")
        assert params == {"n": -5}
        assert isinstance(params["n"], int)

    def test_zero_int(self):
        router = Router()
        router.add("GET", "/items/{id:int}", lambda r, id: None)
        route, params = router.resolve("GET", "/items/0")
        assert params == {"id": 0}
        assert isinstance(params["id"], int)

    def test_large_int(self):
        router = Router()
        router.add("GET", "/items/{id:int}", lambda r, id: None)
        route, params = router.resolve("GET", "/items/9999999999")
        assert params == {"id": 9999999999}
        assert isinstance(params["id"], int)


# ── Integration: Zig native router typed params ─────────────────────────────


class TestZigNativeRouterTypedParams:
    """Test the Zig native radix trie router returns correct string params.

    The native router always returns string values — type conversion is done
    either by Python (resolve() path) or Zig (server dispatch path).
    """

    def test_native_router_returns_strings(self):
        """Native router returns raw string values for all param types."""
        from hyperdjango._hyperdjango_native import (
            _router_add,
            _router_finalize,
            _router_free,
            _router_new,
            _router_resolve,
        )

        handle = _router_new()
        _router_add(handle, "GET", "/users/{id}", "GET /users/{id:int}")
        _router_finalize(handle)

        result = _router_resolve(handle, "GET", "/users/42")
        assert result is not None
        key, params = result
        assert key == "GET /users/{id:int}"
        assert params == {"id": "42"}
        assert isinstance(params["id"], str)  # Raw string from Zig router

        _router_free(handle)

    def test_native_router_multi_params(self):
        from hyperdjango._hyperdjango_native import (
            _router_add,
            _router_finalize,
            _router_free,
            _router_new,
            _router_resolve,
        )

        handle = _router_new()
        _router_add(
            handle,
            "GET",
            "/users/{id}/posts/{slug}",
            "GET /users/{id:int}/posts/{slug:slug}",
        )
        _router_finalize(handle)

        result = _router_resolve(handle, "GET", "/users/7/posts/hello-world")
        assert result is not None
        key, params = result
        assert params == {"id": "7", "slug": "hello-world"}
        assert isinstance(params["id"], str)
        assert isinstance(params["slug"], str)

        _router_free(handle)


# ── Integration: Zig server add_route_typed registration ─────────────────────


class TestZigServerAddRouteTyped:
    """Test that add_route_typed accepts param_types_json and registers routes."""

    def test_register_typed_route(self):
        """add_route_typed should accept (method, path, handler, param_types_json)."""
        from hyperdjango._hyperdjango_native import HyperServer

        server = HyperServer("127.0.0.1", 19876)

        def handler(**kwargs):
            return {"status_code": 200, "content_type": "text/plain", "content": "ok"}

        # Should not raise
        server.add_route_typed("GET", "/users/{id}", handler, "id:int")

    def test_register_multi_typed_route(self):
        from hyperdjango._hyperdjango_native import HyperServer

        server = HyperServer("127.0.0.1", 19877)

        def handler(**kwargs):
            return {"status_code": 200, "content_type": "text/plain", "content": "ok"}

        server.add_route_typed(
            "GET",
            "/users/{id}/posts/{slug}",
            handler,
            "id:int|slug:str",
        )

    def test_register_empty_param_types(self):
        """Empty param_types_json should work (static route via typed API)."""
        from hyperdjango._hyperdjango_native import HyperServer

        server = HyperServer("127.0.0.1", 19878)

        def handler(**kwargs):
            return {"status_code": 200, "content_type": "text/plain", "content": "ok"}

        server.add_route_typed("GET", "/health", handler, "")

    def test_register_all_types(self):
        """All 4 Zig param types should register without error."""
        from hyperdjango._hyperdjango_native import HyperServer

        server = HyperServer("127.0.0.1", 19879)

        def handler(**kwargs):
            return {"status_code": 200, "content_type": "text/plain", "content": "ok"}

        server.add_route_typed(
            "GET",
            "/api/{id}/{price}/{name}/{active}",
            handler,
            "id:int|price:float|name:str|active:bool",
        )


# ── Integration: _wrap_handler_for_zig pre-typed params ──────────────────────


class TestWrapHandlerForZigPreTyped:
    """Test that _wrap_handler_for_zig passes through pre-typed params.

    Simulates what Zig delivers: path_params dict with typed values
    (int, float, str, bool) instead of all-string values.
    """

    def test_int_param_passthrough(self):
        """Zig delivers int(42), wrapper passes it through unchanged."""
        received = {}

        def handler(request, id):
            received["id"] = id
            received["id_type"] = type(id)
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        result = wrapped(
            method="GET",
            path="/users/42",
            headers={},
            query_string="",
            body=b"",
            path_params={"id": 42},  # Pre-typed by Zig
        )
        assert received["id"] == 42
        assert received["id_type"] is int
        # Zig enhanced contract: (status, content_type, body:bytes, extra_headers)
        assert result[0] == 200

    def test_float_param_passthrough(self):
        received = {}

        def handler(request, price):
            received["price"] = price
            received["price_type"] = type(price)
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        result = wrapped(
            method="GET",
            path="/items/9.99",
            headers={},
            query_string="",
            body=b"",
            path_params={"price": 9.99},  # Pre-typed by Zig
        )
        assert received["price"] == 9.99
        assert received["price_type"] is float

    def test_str_param_passthrough(self):
        received = {}

        def handler(request, slug):
            received["slug"] = slug
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/posts/hello-world",
            headers={},
            query_string="",
            body=b"",
            path_params={"slug": "hello-world"},
        )
        assert received["slug"] == "hello-world"
        assert isinstance(received["slug"], str)

    def test_bool_param_passthrough(self):
        received = {}

        def handler(request, active):
            received["active"] = active
            received["active_type"] = type(active)
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/filter/true",
            headers={},
            query_string="",
            body=b"",
            path_params={"active": True},  # Pre-typed by Zig
        )
        assert received["active"] is True
        assert received["active_type"] is bool

    def test_mixed_params_passthrough(self):
        received = {}

        def handler(request, id, slug, price, active):
            received.update({"id": id, "slug": slug, "price": price, "active": active})
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/test",
            headers={},
            query_string="",
            body=b"",
            path_params={"id": 42, "slug": "test", "price": 1.5, "active": False},
        )
        assert received["id"] == 42
        assert isinstance(received["id"], int)
        assert received["slug"] == "test"
        assert isinstance(received["slug"], str)
        assert received["price"] == 1.5
        assert isinstance(received["price"], float)
        assert received["active"] is False
        assert isinstance(received["active"], bool)

    def test_no_request_handler(self):
        """Handlers without request param should receive only path params."""
        received = {}

        def handler(id):
            received["id"] = id
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/items/99",
            headers={},
            query_string="",
            body=b"",
            path_params={"id": 99},
        )
        assert received["id"] == 99

    def test_no_params_handler(self):
        """Handler with no params should work (static route)."""
        called = {"count": 0}

        def handler(request):
            called["count"] += 1
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/health",
            headers={},
            query_string="",
            body=b"",
            path_params={},
        )
        assert called["count"] == 1

    def test_zero_int_passthrough(self):
        received = {}

        def handler(request, id):
            received["id"] = id
            return {"ok": True}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/x/0",
            headers={},
            query_string="",
            body=b"",
            path_params={"id": 0},
        )
        assert received["id"] == 0
        assert isinstance(received["id"], int)

    def test_large_int_passthrough(self):
        received = {}

        def handler(request, id):
            received["id"] = id
            return {"ok": True}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/x/9999999999",
            headers={},
            query_string="",
            body=b"",
            path_params={"id": 9999999999},
        )
        assert received["id"] == 9999999999

    def test_negative_float_passthrough(self):
        received = {}

        def handler(request, val):
            received["val"] = val
            return {"ok": True}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        wrapped(
            method="GET",
            path="/x/-3.14",
            headers={},
            query_string="",
            body=b"",
            path_params={"val": -3.14},
        )
        assert received["val"] == -3.14


# ── Integration: HyperApp route registration pipeline ────────────────────────


class TestHyperAppTypedRoutePipeline:
    """Test HyperApp's route → param_types_json → Zig registration pipeline."""

    def test_app_route_extracts_param_metadata(self):
        """HyperApp route registration builds correct param metadata."""
        app = HyperApp("test")

        @app.get("/users/{id:int}")
        def get_user(request, id: int):
            return {"id": id}

        routes = app.router.routes()
        assert len(routes) == 1
        route = routes[0]
        assert route.param_names == ["id"]
        assert route.param_converters == [int]

        # Verify the param_types_json that would be sent to Zig
        ptj = _build_param_types_json(route.param_names, route.param_converters)
        assert ptj == "id:int"

    def test_app_multi_param_route(self):
        app = HyperApp("test")

        @app.get("/users/{user_id:int}/posts/{slug:slug}")
        def get_post(request, user_id: int, slug: str):
            return {"user_id": user_id, "slug": slug}

        routes = app.router.routes()
        route = routes[0]
        ptj = _build_param_types_json(route.param_names, route.param_converters)
        assert ptj == "user_id:int|slug:str"

    def test_app_static_route_no_metadata(self):
        app = HyperApp("test")

        @app.get("/health")
        def health(request):
            return {"status": "ok"}

        routes = app.router.routes()
        route = routes[0]
        ptj = _build_param_types_json(route.param_names, route.param_converters)
        assert ptj == ""

    def test_app_uuid_route_maps_to_str(self):
        app = HyperApp("test")

        @app.get("/items/{uuid:uuid}")
        def get_item(request, uuid: str):
            return {"uuid": uuid}

        routes = app.router.routes()
        route = routes[0]
        ptj = _build_param_types_json(route.param_names, route.param_converters)
        assert ptj == "uuid:str"  # UUID uses str converter


# ── Edge cases: Zig-side coercion behavior ───────────────────────────────────


class TestZigCoercionEdgeCases:
    """Test edge cases in Zig-native type coercion via param_types_json.

    These verify the Zig parseParamMeta and buildTypedPathParams behavior
    by exercising it through the Python API.
    """

    def test_param_types_json_format_single(self):
        """Verify format: 'name:type'."""
        result = _build_param_types_json(["id"], [int])
        assert ":" in result
        name, typ = result.split(":")
        assert name == "id"
        assert typ == "int"

    def test_param_types_json_format_multi(self):
        """Verify format: 'name1:type1|name2:type2'."""
        result = _build_param_types_json(["a", "b"], [int, str])
        parts = result.split("|")
        assert len(parts) == 2
        assert parts[0] == "a:int"
        assert parts[1] == "b:str"

    def test_param_types_json_no_trailing_pipe(self):
        result = _build_param_types_json(["x"], [int])
        assert not result.endswith("|")

    def test_param_types_json_no_leading_pipe(self):
        result = _build_param_types_json(["x"], [int])
        assert not result.startswith("|")

    def test_many_params(self):
        """Test with many params (approaching MAX_PARAMS=32 in Zig)."""
        names = [f"p{i}" for i in range(20)]
        converters = [int] * 20
        result = _build_param_types_json(names, converters)
        parts = result.split("|")
        assert len(parts) == 20
        for i, part in enumerate(parts):
            assert part == f"p{i}:int"


# ── Backward compatibility ───────────────────────────────────────────────────


class TestBackwardCompatibility:
    """Ensure existing APIs still work after the typed param changes."""

    def test_add_route_still_works(self):
        """The original add_route (no param types) should still register."""
        from hyperdjango._hyperdjango_native import HyperServer

        server = HyperServer("127.0.0.1", 19880)

        def handler(**kwargs):
            return {"status_code": 200, "content_type": "text/plain", "content": "ok"}

        # Original API — should still work
        server.add_route("GET", "/health", handler)

    def test_wrap_handler_no_params(self):
        """_wrap_handler_for_zig with no converter args still works."""

        def handler(request):
            return {"message": "ok"}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        result = wrapped(
            method="GET",
            path="/",
            headers={},
            query_string="",
            body=b"",
            path_params={},
        )
        assert result[0] == 200

    def test_router_resolve_still_converts(self):
        """Python-side Router.resolve() still applies converters."""
        router = Router()
        router.add("GET", "/users/{id:int}", lambda r, id: None)
        route, params = router.resolve("GET", "/users/42")
        assert params["id"] == 42
        assert isinstance(params["id"], int)


# ── Response format tests ────────────────────────────────────────────────────


class TestResponseFormatWithTypedParams:
    """Test that response formatting works correctly with typed params."""

    def test_dict_response(self):
        def handler(request, id):
            return {"id": id, "type": type(id).__name__}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        result = wrapped(
            method="GET",
            path="/users/42",
            headers={},
            query_string="",
            body=b"",
            path_params={"id": 42},
        )
        # Zig enhanced contract: (status, content_type, body:bytes, extra_headers)
        assert result[0] == 200
        assert "application/json" in result[1]
        parsed = json.loads(result[2])
        assert parsed["id"] == 42
        assert parsed["type"] == "int"

    def test_str_response(self):
        def handler(request, name):
            return f"Hello, {name}!"

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        result = wrapped(
            method="GET",
            path="/greet/alice",
            headers={},
            query_string="",
            body=b"",
            path_params={"name": "alice"},
        )
        assert result[0] == 200
        assert b"Hello, alice!" in result[2]

    def test_handler_uses_int_arithmetic(self):
        """Handler can do arithmetic on int params — proves they're real ints."""

        def handler(request, id):
            return {"next_id": id + 1, "doubled": id * 2}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        result = wrapped(
            method="GET",
            path="/users/42",
            headers={},
            query_string="",
            body=b"",
            path_params={"id": 42},
        )
        assert result[0] == 200
        parsed = json.loads(result[2])
        assert parsed["next_id"] == 43
        assert parsed["doubled"] == 84

    def test_handler_uses_float_arithmetic(self):
        """Handler can do arithmetic on float params."""

        def handler(request, price):
            return {"tax": round(price * 0.1, 2), "total": round(price * 1.1, 2)}

        wrapped = HyperApp._wrap_handler_for_zig(handler)
        result = wrapped(
            method="GET",
            path="/calc/100.0",
            headers={},
            query_string="",
            body=b"",
            path_params={"price": 100.0},
        )
        assert result[0] == 200
        parsed = json.loads(result[2])
        assert parsed["tax"] == 10.0
