"""
File-based routing — auto-discover Django views from directory structure.

Inspired by merjs's file-based routing convention. Scans a directory
and generates standard Django URLpatterns.

Convention:
    views/index.py          -> path('', view, name='index')
    views/about.py          -> path('about/', view, name='about')
    views/users/index.py    -> path('users/', view, name='users-index')
    views/users/[id].py     -> path('users/<int:id>/', view, name='users-detail')
    views/users/[slug].py   -> path('users/<slug:slug>/', view, name='users-slug')
    views/api/health.py     -> path('api/health/', view, name='api-health')

Each file should export one of:
    - view: A function-based view or class-based view
    - get, post, put, patch, delete: Method-specific handlers
    - ViewClass: A class-based view (auto-detected)

Usage:
    # urls.py
    from hyperdjango.routing import discover_file_routes

    urlpatterns = [
        path('admin/', admin.site.urls),
        *discover_file_routes('views'),
    ]
"""

import importlib.util
import re
from pathlib import Path

from django.http import HttpResponseNotAllowed
from django.urls import path

from hyperdjango.logging import logger


def discover_file_routes(views_dir, prefix="", app_name=None):
    """Scan a directory and generate Django URL patterns.

    Args:
        views_dir: Directory to scan (relative to project root or absolute).
        prefix: URL prefix to prepend to all discovered routes.
        app_name: Optional app name for URL namespacing.

    Returns:
        List of Django URL patterns.
    """
    # Resolve the directory
    if not Path(views_dir).is_absolute():
        # Try relative to current working directory
        base_dir = Path.cwd() / views_dir
    else:
        base_dir = Path(views_dir)

    if not base_dir.exists():
        return []

    patterns = []
    _scan_directory(base_dir, base_dir, prefix, patterns)

    # Sort patterns: static routes before dynamic ones
    patterns.sort(
        key=lambda p: (
            "[" in str(p.pattern),  # Dynamic routes last
            str(p.pattern),
        )
    )

    return patterns


def _scan_directory(directory, base_dir, prefix, patterns):
    """Recursively scan a directory for view files."""
    for item in sorted(directory.iterdir()):
        if item.name.startswith(("_", ".")):
            continue

        if item.is_dir():
            # Recurse into subdirectories
            sub_prefix = _build_url_segment(item.name)
            _scan_directory(item, base_dir, f"{prefix}{sub_prefix}/", patterns)

        elif item.suffix == ".py" and item.name != "__init__.py":
            # Process Python file as a route
            pattern = _file_to_pattern(item, base_dir, prefix)
            if pattern is not None:
                patterns.append(pattern)


def _file_to_pattern(file_path, base_dir, prefix):
    """Convert a Python file to a Django URL pattern."""
    # Build the URL path
    stem = file_path.stem
    if stem == "index":
        url_path = prefix.rstrip("/") + "/" if prefix else ""
    else:
        segment = _build_url_segment(stem)
        url_path = f"{prefix}{segment}/"

    # Build the route name
    rel_path = file_path.relative_to(base_dir)
    name = _build_route_name(rel_path)

    # Import the module
    module = _import_view_module(file_path, base_dir)
    if module is None:
        return None

    # Find the view callable
    view_func = _extract_view(module)
    if view_func is None:
        return None

    return path(url_path, view_func, name=name)


def _build_url_segment(name):
    """Convert a filename/dirname to a URL segment.

    [id]    -> <int:id>
    [slug]  -> <slug:slug>
    [pk]    -> <int:pk>
    [uuid]  -> <uuid:uuid>
    [str]   -> <str:str>
    [name]  -> <str:name>  (default to str for unknown names)
    """
    match = re.match(r"^\[(\w+)\]$", name)
    if match:
        param = match.group(1)
        # Auto-detect type from parameter name
        type_map = {
            "id": "int",
            "pk": "int",
            "slug": "slug",
            "uuid": "uuid",
            "year": "int",
            "month": "int",
            "day": "int",
        }
        converter = type_map.get(param, "str")
        return f"<{converter}:{param}>"
    return name


def _build_route_name(rel_path):
    """Build a URL name from a relative file path.

    views/users/[id].py -> users-detail
    views/about.py -> about
    views/index.py -> index
    views/api/health.py -> api-health
    """
    parts = list(rel_path.parts)
    # Remove .py extension
    parts[-1] = Path(parts[-1]).stem

    # Replace index with parent name or 'index'
    clean_parts = []
    for part in parts:
        if re.match(r"^\[\w+\]$", part):
            clean_parts.append("detail")
        elif part != "index":
            clean_parts.append(part)
        else:
            if not clean_parts:
                clean_parts.append("index")

    return "-".join(clean_parts) if clean_parts else "index"


def _import_view_module(file_path, base_dir):
    """Import a view module from its file path."""
    rel = file_path.relative_to(base_dir)
    module_parts = list(rel.with_suffix("").parts)
    module_name = ".".join(module_parts)

    # Build a module spec from the file path
    try:
        spec = importlib.util.spec_from_file_location(
            f"hyperdjango_views.{module_name}", str(file_path)
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        # A view module that raises while executing is a real bug in the
        # route file (bad import, NameError, syntax-time failure). Swallowing
        # it here would make the route silently vanish (404) with no trace.
        # Surface it with context and let it propagate.
        logger.error("Failed to import view module: {path}", path=str(file_path))
        raise


_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _extract_view(module):
    """Extract the view callable from a module.

    Looks for: view, get/post/put/patch/delete, or a View class.
    """
    # Direct view export
    if hasattr(module, "view"):
        return module.view

    # Method-specific handlers -> combine into single view
    methods = {}
    for method in _HTTP_METHODS:
        if hasattr(module, method):
            # dynamic-attr: reading a user route module's handler by runtime HTTP-method name
            methods[method.upper()] = getattr(module, method)

    if methods:
        return _make_method_view(methods)

    # Look for a class-based view (class with as_view)
    for attr_name in dir(module):
        # dynamic-attr: enumerating a user route module's attributes to find a class-based view
        attr = getattr(module, attr_name)
        if isinstance(attr, type) and hasattr(attr, "as_view"):
            return attr.as_view()

    return None


def _make_method_view(methods):
    """Create a view function that dispatches by HTTP method."""

    def method_dispatch_view(request, *args, **kwargs):
        handler = methods.get(request.method)
        if handler is not None:
            return handler(request, *args, **kwargs)
        return HttpResponseNotAllowed(methods.keys())

    method_dispatch_view.__name__ = "method_dispatch_view"
    return method_dispatch_view
