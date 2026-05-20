"""Tests for standalone file-based routing."""

import tempfile
from pathlib import Path

from hyperdjango.routing.file_router import (
    _to_route_name,
    _to_url_segment,
    discover_routes,
)


class TestToUrlSegment:
    def test_literal(self):
        assert _to_url_segment("users") == "users"

    def test_bracket_param(self):
        assert _to_url_segment("[id]") == "{id}"

    def test_curly_param(self):
        assert _to_url_segment("{id}") == "{id}"


class TestToRouteName:
    def test_simple(self):
        assert _to_route_name(Path("about.py")) == "about"

    def test_nested(self):
        assert _to_route_name(Path("api/health.py")) == "api_health"

    def test_param_stripped(self):
        assert _to_route_name(Path("users/[id].py")) == "users_id"


class TestDiscoverRoutes:
    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            assert discover_routes(d) == []

    def test_simple_view(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "about.py").write_text("async def get(request): return 'about'\n")
            routes = discover_routes(d)
            assert len(routes) == 1
            method, pattern, handler, name = routes[0]
            assert method == "GET"
            assert pattern == "/about"

    def test_index_view(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "index.py").write_text("async def get(request): return 'home'\n")
            routes = discover_routes(d)
            assert len(routes) == 1
            assert routes[0][1] == "/"

    def test_nested_view(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "api").mkdir(parents=True)
            Path(d, "api", "health.py").write_text(
                "async def get(request): return 'ok'\n"
            )
            routes = discover_routes(d)
            assert len(routes) == 1
            assert routes[0][1] == "/api/health"

    def test_multiple_methods(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "users.py").write_text(
                "async def get(request): return 'list'\n"
                "async def post(request): return 'create'\n"
            )
            routes = discover_routes(d)
            methods = {r[0] for r in routes}
            assert "GET" in methods
            assert "POST" in methods

    def test_param_directory(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "users" / "[id]").mkdir(parents=True)
            Path(d, "users", "[id]", "index.py").write_text(
                "async def get(request): return 'detail'\n"
            )
            routes = discover_routes(d)
            assert len(routes) == 1
            assert "{id}" in routes[0][1]

    def test_nonexistent_dir(self):
        assert discover_routes("/nonexistent/path") == []
