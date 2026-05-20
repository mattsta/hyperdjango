"""
Tests for the enhanced `hyper routes` command.

Covers:
- Route.view_path resolution (plain function, bound method, functools.partial,
  callable instance, lambda).
- collect_route_info(): prefix filtering, sorted vs registration order.
- cmd_routes() end-to-end output for tabular / stacked / json formats,
  including the view path in each shape, captured via a callable logger sink.

Usage:
    uv run hyper-test cli_routes
"""

# hyper-test: unit

import argparse
import asyncio
import functools
import inspect
import json
import sys
import traceback

from hyperdjango.app import HyperApp
from hyperdjango.cli import RouteInfo, cmd_routes, collect_route_info
from hyperdjango.logging import logger

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        def wrapper():
            try:
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
# Sample handlers (module-level so view paths are stable & resolvable)
# ---------------------------------------------------------------------------


def index_view(request):
    return None


def user_detail(request, id):
    return None


class _Views:
    def list_items(self, request):
        return None


class _CallableView:
    def __call__(self, request):
        return None


_views = _Views()
_callable_view = _CallableView()


def _partial_view(request, mode):
    return None


_bound_partial = functools.partial(_partial_view, mode="ro")


def _make_app():
    """Build a small HyperApp with a handful of routes (no views dir)."""
    app = HyperApp(title="RoutesTestApp")
    app.get("/")(index_view)
    app.get("/users/{id:int}", name="user-detail")(user_detail)
    app.get("/items")(_views.list_items)
    # Callable instances and functools.partial have no __name__, so the route
    # name must be supplied explicitly (Route.__post_init__ derives from __name__).
    app.get("/admin/dashboard", name="admin-dashboard")(_callable_view)
    app.get("/reports", name="reports")(_bound_partial)
    return app


# ---------------------------------------------------------------------------
# Logger capture helper
# ---------------------------------------------------------------------------


def _run_routes(**overrides) -> str:
    """Invoke cmd_routes with the given arg overrides, capture raw output."""
    captured: list[str] = []

    def _sink(record, message):
        # CallableSink invokes func(record, message); keep the formatted text.
        captured.append(str(message))

    # Stop the background writer so log dispatch emits synchronously — otherwise
    # records are queued and our capture list is empty when cmd_routes returns.
    logger._core.stop_writer()
    handler_id = logger.add(_sink, enqueue=False, colorize=False, format="{message}")
    try:
        args = argparse.Namespace(
            app="_unused_",
            format=overrides.get("format", "tabular"),
            prefix=overrides.get("prefix"),
            unsorted=overrides.get("unsorted", False),
        )
        # cmd_routes calls _load_app(args.app); swap in our prebuilt app instead.
        app = _make_app()
        _patch_load_app(app)
        try:
            cmd_routes(args)
        finally:
            _unpatch_load_app()
    finally:
        logger.remove(handler_id)
    return "".join(captured)


# Patch _load_app so cmd_routes uses our in-memory app rather than importing.
import hyperdjango.cli as _cli_module  # noqa: E402

_ORIGINAL_LOAD_APP = _cli_module._load_app


def _patch_load_app(app):
    _cli_module._load_app = lambda _path: app


def _unpatch_load_app():
    _cli_module._load_app = _ORIGINAL_LOAD_APP


# ---------------------------------------------------------------------------
# Route.view_path resolution
# ---------------------------------------------------------------------------


@test("view_path resolves plain function")
def test_view_path_function():
    app = _make_app()
    route = next(r for r in app.router.routes() if r.pattern == "/")
    assert route.view_path == f"{index_view.__module__}.index_view", route.view_path


@test("view_path resolves bound method")
def test_view_path_bound_method():
    app = _make_app()
    route = next(r for r in app.router.routes() if r.pattern == "/items")
    assert route.view_path == f"{__name__}._Views.list_items", route.view_path


@test("view_path resolves functools.partial")
def test_view_path_partial():
    app = _make_app()
    route = next(r for r in app.router.routes() if r.pattern == "/reports")
    assert route.view_path == f"{__name__}._partial_view", route.view_path


@test("view_path resolves callable instance via its class")
def test_view_path_callable_instance():
    app = _make_app()
    route = next(r for r in app.router.routes() if r.pattern == "/admin/dashboard")
    assert route.view_path == f"{__name__}._CallableView", route.view_path


@test("view_path resolves lambda")
def test_view_path_lambda():
    app = HyperApp()
    app.get("/ping")(lambda request: None)
    route = next(r for r in app.router.routes() if r.pattern == "/ping")
    # Lambdas have qualname '<lambda>' (possibly with enclosing qualname).
    assert route.view_path.endswith("<lambda>"), route.view_path
    assert route.view_path.startswith(__name__), route.view_path


# ---------------------------------------------------------------------------
# collect_route_info
# ---------------------------------------------------------------------------


@test("collect_route_info returns RouteInfo objects with view path")
def test_collect_basic():
    app = _make_app()
    infos = collect_route_info(app)
    assert all(isinstance(i, RouteInfo) for i in infos)
    by_pattern = {i.pattern: i for i in infos}
    assert by_pattern["/users/{id:int}"].name == "user-detail"
    assert by_pattern["/users/{id:int}"].view == f"{__name__}.user_detail"
    assert by_pattern["/"].view == f"{index_view.__module__}.index_view"


@test("collect_route_info is sorted by pattern by default")
def test_collect_sorted():
    app = _make_app()
    patterns = [i.pattern for i in collect_route_info(app)]
    assert patterns == sorted(patterns), patterns


@test("collect_route_info --unsorted preserves registration order")
def test_collect_unsorted():
    app = _make_app()
    patterns = [i.pattern for i in collect_route_info(app, unsorted=True)]
    # Registration order from _make_app().
    expected = ["/", "/users/{id:int}", "/items", "/admin/dashboard", "/reports"]
    assert patterns == expected, patterns


@test("collect_route_info --prefix filters by pattern startswith")
def test_collect_prefix():
    app = _make_app()
    infos = collect_route_info(app, prefix="/users")
    assert [i.pattern for i in infos] == ["/users/{id:int}"], infos
    # A prefix matching nothing yields an empty list.
    assert collect_route_info(app, prefix="/nonexistent") == []
    # /admin prefix matches the dashboard route.
    admin = collect_route_info(app, prefix="/admin")
    assert [i.pattern for i in admin] == ["/admin/dashboard"], admin


@test("RouteInfo.to_dict yields the four expected keys")
def test_routeinfo_to_dict():
    info = RouteInfo(method="GET", pattern="/x", name="x", view="mod.fn")
    d = info.to_dict()
    assert d == {"method": "GET", "pattern": "/x", "name": "x", "view": "mod.fn"}, d


# ---------------------------------------------------------------------------
# cmd_routes end-to-end output
# ---------------------------------------------------------------------------


@test("cmd_routes json format parses to expected dicts incl. view path")
def test_cmd_json():
    out = _run_routes(format="json")
    payload = json.loads(out)
    assert isinstance(payload, list), type(payload)
    by_pattern = {row["pattern"]: row for row in payload}
    assert set(by_pattern["/"].keys()) == {"method", "pattern", "name", "view"}
    assert by_pattern["/"]["view"] == f"{index_view.__module__}.index_view"
    assert by_pattern["/users/{id:int}"]["name"] == "user-detail"
    assert by_pattern["/users/{id:int}"]["view"] == f"{__name__}.user_detail"
    assert by_pattern["/reports"]["view"] == f"{__name__}._partial_view"
    # Sorted by pattern by default.
    patterns = [row["pattern"] for row in payload]
    assert patterns == sorted(patterns), patterns


@test("cmd_routes json format honors --prefix")
def test_cmd_json_prefix():
    out = _run_routes(format="json", prefix="/users")
    payload = json.loads(out)
    assert [row["pattern"] for row in payload] == ["/users/{id:int}"], payload


@test("cmd_routes json format honors --unsorted")
def test_cmd_json_unsorted():
    out = _run_routes(format="json", unsorted=True)
    payload = json.loads(out)
    patterns = [row["pattern"] for row in payload]
    assert patterns == [
        "/",
        "/users/{id:int}",
        "/items",
        "/admin/dashboard",
        "/reports",
    ], patterns


@test("cmd_routes stacked format includes view path")
def test_cmd_stacked():
    out = _run_routes(format="stacked")
    assert "view:" in out, out
    assert f"view: {index_view.__module__}.index_view" in out, out
    assert f"view: {__name__}.user_detail" in out, out
    assert "GET /users/{id:int}" in out, out
    assert "name: user-detail" in out, out


@test("cmd_routes tabular (default) includes a VIEW column with view path")
def test_cmd_tabular():
    out = _run_routes()
    assert "VIEW" in out, out
    assert "METHOD" in out and "PATTERN" in out and "NAME" in out, out
    assert f"{index_view.__module__}.index_view" in out, out
    assert f"{__name__}.user_detail" in out, out


@test("cmd_routes tabular honors --prefix")
def test_cmd_tabular_prefix():
    out = _run_routes(prefix="/admin")
    assert "/admin/dashboard" in out, out
    assert "/users/{id:int}" not in out, out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for _name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nCLI routes command tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        if inspect.iscoroutinefunction(t):
            await t()
        else:
            t()

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
