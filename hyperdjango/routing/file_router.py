"""
File-based route discovery — standalone version (no Django).

Scans a directory and returns route tuples for the HyperApp router.

Convention:
    views/index.py          -> GET /
    views/about.py          -> GET /about
    views/users/index.py    -> GET /users
    views/users/{id}.py     -> GET /users/{id}
    views/api/health.py     -> GET /api/health

Each file exports: get, post, put, patch, delete functions,
or a single `view` function for all methods.
"""

import importlib.util
import re
from pathlib import Path

from hyperdjango.logging import logger


def discover_routes(views_dir):
    """Scan a directory and return route tuples.

    Args:
        views_dir: Path to the views directory.

    Returns:
        List of (method, pattern, handler, name) tuples.
    """
    base_dir = Path(views_dir)
    if not base_dir.exists():
        return []

    routes = []
    _scan_directory(base_dir, base_dir, "", routes)

    # Sort: static before dynamic
    routes.sort(key=lambda r: ("{" in r[1], r[1]))
    return routes


def _scan_directory(directory, base_dir, prefix, routes):
    """Recursively scan for route files."""
    for item in sorted(directory.iterdir()):
        if item.name.startswith(("_", ".")):
            continue

        if item.is_dir():
            segment = _to_url_segment(item.name)
            _scan_directory(item, base_dir, f"{prefix}/{segment}", routes)

        elif item.suffix == ".py" and item.name != "__init__.py":
            _process_file(item, base_dir, prefix, routes)


def _process_file(file_path, base_dir, prefix, routes):
    """Process a single Python file into routes."""
    stem = file_path.stem

    if stem == "index":
        pattern = prefix or "/"
    else:
        segment = _to_url_segment(stem)
        pattern = f"{prefix}/{segment}"

    # Ensure pattern starts with /
    if not pattern.startswith("/"):
        pattern = "/" + pattern

    # Build route name
    name = _to_route_name(file_path.relative_to(base_dir))

    # Import the module
    module = _import_module(file_path)
    if module is None:
        return

    # Extract handlers
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    found = False

    for method in methods:
        # dynamic-attr: probing a user route module for optional per-HTTP-method handler functions named by convention
        handler = getattr(module, method.lower(), None)
        if handler is not None:
            routes.append((method, pattern, handler, f"{name}_{method.lower()}"))
            found = True

    # Fallback: 'view' function handles all methods
    if not found:
        # dynamic-attr: probing a user route module for an optional 'view' handler
        view = getattr(module, "view", None)
        if view is not None:
            routes.append(("GET", pattern, view, name))
            routes.append(("POST", pattern, view, f"{name}_post"))


def _to_url_segment(name):
    """Convert filename/dirname to URL segment.

    {id}    -> {id}       (already param syntax)
    [id]    -> {id}       (merjs-style to HyperApp style)
    users   -> users      (literal)
    """
    # Convert [param] to {param}
    match = re.match(r"^\[(\w+)\]$", name)
    if match:
        return "{" + match.group(1) + "}"

    # Already {param}
    if re.match(r"^\{\w+\}$", name):
        return name

    return name


def _to_route_name(rel_path):
    """Build a route name from relative path."""
    parts = list(rel_path.parts)
    parts[-1] = Path(parts[-1]).stem  # Remove .py

    clean = []
    for part in parts:
        part = re.sub(r"[\[\{\}\]]", "", part)  # Remove brackets
        if part and part != "index":
            clean.append(part)

    return "_".join(clean) if clean else "index"


def _import_module(file_path):
    """Import a module from file path."""
    try:
        spec = importlib.util.spec_from_file_location(
            f"hyper_views.{file_path.stem}", str(file_path)
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
        logger.error("Failed to import route module: {path}", path=str(file_path))
        raise
