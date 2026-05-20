"""Tests for file-based routing discovery."""

import tempfile
from pathlib import Path

from hyperdjango.routing.discovery import (
    _build_route_name,
    _build_url_segment,
    discover_file_routes,
)


class TestBuildUrlSegment:
    def test_static_segment(self):
        assert _build_url_segment("about") == "about"

    def test_id_param(self):
        assert _build_url_segment("[id]") == "<int:id>"

    def test_slug_param(self):
        assert _build_url_segment("[slug]") == "<slug:slug>"

    def test_uuid_param(self):
        assert _build_url_segment("[uuid]") == "<uuid:uuid>"

    def test_pk_param(self):
        assert _build_url_segment("[pk]") == "<int:pk>"

    def test_unknown_param_defaults_to_str(self):
        assert _build_url_segment("[username]") == "<str:username>"


class TestBuildRouteName:
    def test_simple_file(self):
        assert _build_route_name(Path("about.py")) == "about"

    def test_index_file(self):
        assert _build_route_name(Path("index.py")) == "index"

    def test_nested_file(self):
        assert _build_route_name(Path("api/health.py")) == "api-health"

    def test_dynamic_segment(self):
        assert _build_route_name(Path("users/[id].py")) == "users-detail"


class TestDiscoverFileRoutes:
    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            patterns = discover_file_routes(tmpdir)
            assert patterns == []

    def test_nonexistent_directory(self):
        patterns = discover_file_routes("/nonexistent/path")
        assert patterns == []

    def test_simple_view_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a view file
            view_file = Path(tmpdir) / "about.py"
            view_file.write_text(
                "from django.http import HttpResponse\n"
                "def view(request):\n"
                "    return HttpResponse('About')\n"
            )

            patterns = discover_file_routes(tmpdir)
            assert len(patterns) == 1
            assert str(patterns[0].pattern) == "about/"

    def test_index_view(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            view_file = Path(tmpdir) / "index.py"
            view_file.write_text(
                "from django.http import HttpResponse\n"
                "def view(request):\n"
                "    return HttpResponse('Home')\n"
            )

            patterns = discover_file_routes(tmpdir)
            assert len(patterns) == 1
            assert str(patterns[0].pattern) == ""

    def test_nested_views(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api_dir = Path(tmpdir) / "api"
            api_dir.mkdir(parents=True, exist_ok=True)
            view_file = api_dir / "health.py"
            view_file.write_text(
                "from django.http import JsonResponse\n"
                "def view(request):\n"
                "    return JsonResponse({'status': 'ok'})\n"
            )

            patterns = discover_file_routes(tmpdir)
            assert len(patterns) == 1
            assert str(patterns[0].pattern) == "api/health/"

    def test_skips_dunder_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # __init__.py should be skipped
            (Path(tmpdir) / "__init__.py").write_text("")

            view_file = Path(tmpdir) / "about.py"
            view_file.write_text(
                "from django.http import HttpResponse\n"
                "def view(request):\n"
                "    return HttpResponse('About')\n"
            )

            patterns = discover_file_routes(tmpdir)
            assert len(patterns) == 1

    def test_method_specific_handlers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            view_file = Path(tmpdir) / "users.py"
            view_file.write_text(
                "from django.http import HttpResponse, JsonResponse\n"
                "def get(request):\n"
                "    return JsonResponse({'users': []})\n"
                "def post(request):\n"
                "    return HttpResponse(status=201)\n"
            )

            patterns = discover_file_routes(tmpdir)
            assert len(patterns) == 1
