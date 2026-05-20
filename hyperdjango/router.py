"""
Standalone router — maps URL patterns to handlers.

Supports:
- Decorator-based registration (@app.get("/path"))
- File-based discovery (views/ directory)
- Path parameters: /users/{id} with type conversion
- Method-specific handlers (GET, POST, PUT, PATCH, DELETE)
- URL namespaces and includes for modular route organization
- Namespaced reverse() resolution (e.g., reverse("blog:post-detail", id=1))

No Django dependency.
"""

import contextlib
import functools
import inspect
import re
from dataclasses import dataclass, field
from typing import Any

from hyperdjango.conf import get_setting


@dataclass(slots=True)
class Route:
    """A single route mapping pattern → handler."""

    PARAM_PATTERN = re.compile(r"\{(\w+)(?::(\w+))?\}")

    CONVERTERS = {
        "int": (int, r"(\d+)"),
        "str": (str, r"([^/]+)"),
        "slug": (str, r"([-\w]+)"),
        "uuid": (
            str,
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        ),
        "path": (str, r"(.+)"),
    }

    method: str
    pattern: str
    handler: Any
    name: str | None = None
    param_names: list[str] = field(default_factory=list, init=False, repr=False)
    param_converters: list[type] = field(default_factory=list, init=False, repr=False)
    _regex: Any = field(default=None, init=False, repr=False)
    is_async: bool = field(default=False, init=False)

    def __post_init__(self):
        self.method = self.method.upper()
        if self.name is None:
            self.name = self.handler.__name__
        self.is_async = inspect.iscoroutinefunction(self.handler)

        # Parse path parameters
        regex_pattern = "^"
        last_end = 0
        for match in self.PARAM_PATTERN.finditer(self.pattern):
            regex_pattern += re.escape(self.pattern[last_end : match.start()])

            param_name = match.group(1)
            param_type = match.group(2) or "str"
            converter, regex_part = self.CONVERTERS.get(param_type, (str, r"([^/]+)"))

            self.param_names.append(param_name)
            self.param_converters.append(converter)
            regex_pattern += regex_part
            last_end = match.end()

        regex_pattern += re.escape(self.pattern[last_end:]) + "$"
        self._regex = re.compile(regex_pattern)

    def match(self, path):
        """Try to match a path. Returns dict of params or None."""
        m = self._regex.match(path)
        if m is None:
            return None

        params = {}
        for i, name in enumerate(self.param_names):
            raw = m.group(i + 1)
            try:
                params[name] = self.param_converters[i](raw)
            except ValueError, TypeError:
                return None
        return params

    @property
    def is_static(self):
        """True if this route has no path parameters."""
        return len(self.param_names) == 0

    @property
    def view_path(self) -> str:
        """Dotted path to the view callable: module + qualified name.

        Resolves the underlying callable for functools.partial wrappers and
        bound methods, then returns ``handler.__module__ + "." + handler.__qualname__``.
        Falls back gracefully (e.g. ``"<lambda>"``, ``repr``) when the handler
        exposes neither attribute (callable instances, builtins).
        """
        target = self.handler

        # Unwrap functools.partial chains to the underlying callable.
        while isinstance(target, functools.partial):
            target = target.func

        # Bound/unbound methods expose the function via __func__.
        func = inspect.unwrap(target)
        # dynamic-attr: handler is an arbitrary user callable (function, method, lambda, callable instance); only methods carry __func__
        wrapped = getattr(func, "__func__", None)
        if wrapped is not None:
            func = wrapped

        # dynamic-attr: callable instances and some builtins lack __module__/__qualname__; not statically guaranteed on an arbitrary handler
        module = getattr(func, "__module__", None)
        # dynamic-attr: callable instances lack __qualname__; not statically guaranteed on an arbitrary handler
        qualname = getattr(func, "__qualname__", None)
        if qualname is None:
            # Callable instance (no __qualname__) — use its class.
            cls = type(target)
            module = cls.__module__
            qualname = cls.__qualname__

        if module:
            return f"{module}.{qualname}"
        return qualname


# Sentinel route returned when APPEND_SLASH triggers a redirect
_APPEND_SLASH_REDIRECT = Route(
    method="GET",
    pattern="/",
    handler=lambda request: None,
    name="_append_slash_redirect",
)


class Router:
    """URL router with native Zig radix trie.

    Uses a compiled radix trie for O(log n) parameterized routing.
    Native Zig extension is required — no fallback.
    """

    def __init__(self):
        # All routes for listing and handler lookup
        self._all_routes: list[Route] = []
        # handler_key → Route for native router lookups
        self._route_map: dict[str, Route] = {}

        # Native radix trie router (Zig) — always available
        from hyperdjango._hyperdjango_native import (
            _router_add,
            _router_finalize,
            _router_free,
            _router_new,
            _router_resolve,
        )

        self._native_handle = _router_new()
        self._native_add = _router_add
        self._native_resolve = _router_resolve
        self._native_free = _router_free
        self._native_finalize = _router_finalize

    def __del__(self):
        with contextlib.suppress(Exception):
            self._native_free(self._native_handle)

    def add(self, method, pattern, handler, name=None):
        """Register a route."""
        route = Route(method, pattern, handler, name)
        self._all_routes.append(route)

        # Unique key for this route (used by native router)
        key = f"{route.method} {pattern}"
        self._route_map[key] = route

        # Register in native radix trie
        # Strip type annotations from pattern: {id:int} → {id}
        # Zig router only needs param names, Python handles type conversion
        native_pattern = re.sub(r"\{(\w+):\w+\}", r"{\1}", pattern)
        self._native_add(self._native_handle, route.method, native_pattern, key)
        if route.method == "GET":
            head_key = f"HEAD {pattern}"
            self._route_map[head_key] = route
            self._native_add(self._native_handle, "HEAD", native_pattern, head_key)

    def finalize(self):
        """Optimize the native router: compress paths, sort children.

        Call after all routes are registered, before serving requests.
        Called automatically by HyperApp.listen().
        """
        self._native_finalize(self._native_handle)

    def resolve(self, method, path):
        """Find a matching route for a method and path.

        Uses native Zig radix trie for O(log n) resolution.
        If APPEND_SLASH is True and path lacks a trailing slash, tries
        path + "/" and returns a redirect sentinel if that matches.

        Returns:
            (route, path_params) tuple, or (None, {}) if no match.
        """
        method = method.upper()

        result = self._native_resolve(self._native_handle, method, path)

        if result is not None:
            key, params = result
            route = self._route_map.get(key)
            if route is not None:
                # APPEND_SLASH: if the path lacks a trailing slash but the
                # matched route's pattern has one, signal a redirect instead
                # of serving the content directly.
                if (
                    get_setting("APPEND_SLASH")
                    and not path.endswith("/")
                    and route.pattern.endswith("/")
                ):
                    return _APPEND_SLASH_REDIRECT, {"redirect_to": path + "/"}

                # Check if the native trie's param names match the route's expected
                # param names. The native trie shares param nodes across routes at the
                # same path position, so param names from the trie may not match
                # (e.g., {id} vs {post_id} when /posts/{id} and
                # /posts/{post_id}/comments coexist). When names mismatch, fall back
                # to the route's own regex for correct param extraction.
                names_match = all(name in params for name in route.param_names)
                if names_match:
                    # Fast path: native param names match, just apply type converters
                    converted = {}
                    for i, name in enumerate(route.param_names):
                        try:
                            converted[name] = route.param_converters[i](params[name])
                        except ValueError, TypeError:
                            return None, {}
                    # Pass through wildcard params (not in param_names)
                    for name, value in params.items():
                        if name not in converted:
                            converted[name] = value
                    return route, converted

                # Slow path: re-extract params using route's regex
                converted = route.match(path)
                if converted is not None:
                    return route, converted

                # Regex failed — fall back to positional remapping
                param_values = list(params.values())
                converted = {}
                for i, name in enumerate(route.param_names):
                    if i < len(param_values):
                        try:
                            converted[name] = route.param_converters[i](param_values[i])
                        except ValueError, TypeError:
                            return None, {}
                return route, converted
        return None, {}

    def include(self, prefix: str, routes, namespace: str | None = None):
        """Mount a sub-router or route list at a prefix.

        Args:
            prefix: URL prefix (e.g., "/api/v1" or "/blog")
            routes: A Router instance, or a list of (method, pattern, handler, name) tuples
            namespace: Optional namespace for reverse() resolution

        Usage:
            # Mount a sub-router
            blog_router = Router()
            blog_router.get("/", list_posts, name="list")
            blog_router.get("/{id:int}", post_detail, name="detail")

            app.router.include("/blog", blog_router, namespace="blog")
            # Creates: /blog/ and /blog/{id:int}
            # Reverse: app.router.reverse("blog:detail", id=42) → "/blog/42"

            # Mount a list of route tuples
            app.router.include("/api", [
                ("GET", "/users", list_users, "users"),
                ("GET", "/users/{id}", get_user, "user-detail"),
            ], namespace="api")
        """
        # Normalize prefix: ensure starts with /, no trailing /
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        prefix = prefix.rstrip("/")

        if isinstance(routes, Router):
            for route in routes._all_routes:
                full_pattern = prefix + route.pattern
                full_name = f"{namespace}:{route.name}" if namespace else route.name
                self.add(route.method, full_pattern, route.handler, name=full_name)
        else:
            # List of (method, pattern, handler, name) tuples
            for entry in routes:
                if len(entry) == 4:
                    method, pattern, handler, name = entry
                elif len(entry) == 3:
                    method, pattern, handler = entry
                    name = handler.__name__
                else:
                    raise ValueError(
                        f"Route entry must be (method, pattern, handler[, name]), got {len(entry)} items"
                    )
                full_pattern = prefix + pattern
                full_name = f"{namespace}:{name}" if namespace else name
                self.add(method, full_pattern, handler, name=full_name)

    def routes(self):
        """List all registered routes."""
        return list(self._all_routes)

    def reverse(self, name: str, **kwargs) -> str:
        """Resolve a route name to a URL path.

        Supports namespaced names: "namespace:name" resolves within the namespace.

        Usage:
            url = router.reverse("product_detail", id=42)
            # Returns "/products/42"

            url = router.reverse("blog:detail", id=7)
            # Returns "/blog/7"

        Raises ValueError if the route name is not found.
        """
        for route in self._all_routes:
            if route.name == name:
                path = route.pattern
                for param_name, value in kwargs.items():
                    path = path.replace(f"{{{param_name}}}", str(value))
                    path = re.sub(rf"\{{{param_name}:\w+\}}", str(value), path)
                return path
        raise ValueError(f"No route named '{name}'")

    # --- Decorator shortcuts ---

    def get(self, pattern, name=None):
        def decorator(func):
            self.add("GET", pattern, func, name)
            return func

        return decorator

    def post(self, pattern, name=None):
        def decorator(func):
            self.add("POST", pattern, func, name)
            return func

        return decorator

    def put(self, pattern, name=None):
        def decorator(func):
            self.add("PUT", pattern, func, name)
            return func

        return decorator

    def patch(self, pattern, name=None):
        def decorator(func):
            self.add("PATCH", pattern, func, name)
            return func

        return decorator

    def delete(self, pattern, name=None):
        def decorator(func):
            self.add("DELETE", pattern, func, name)
            return func

        return decorator

    def route(self, pattern, methods=None, name=None):
        """Register a handler for multiple methods."""
        methods = methods or ["GET"]

        def decorator(func):
            for method in methods:
                self.add(method, pattern, func, name)
            return func

        return decorator
