"""
High-performance redirects module.

Replaces django.contrib.redirects with a native in-memory registry
backed by database persistence. O(1) dict lookup on every 404.

No Django dependency.

Usage:
    from hyperdjango.redirects import registry, RedirectMiddleware

    # Add redirect
    await registry.add("/old-page/", "/new-page/", 301)

    # Use as middleware
    @app.middleware
    async def redirect_mw(request, call_next):
        return await RedirectMiddleware(registry)(request, call_next)
"""

import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from hyperdjango.auth.sessions import is_safe_redirect_url
from hyperdjango.request import Request
from hyperdjango.response import Response


def _is_safe_relative_target(url: str) -> bool:
    """Return True only for safe same-origin relative redirect targets.

    Delegates to the ONE open-redirect authority, ``is_safe_redirect_url``, so
    redirect targets are validated identically everywhere and no second copy of
    the safety rules can drift.
    """
    return is_safe_redirect_url(url)


@dataclass(slots=True)
class Redirect:
    """A single redirect entry.

    Attributes:
        old_path: Source path to redirect from (e.g., "/old-page/").
        new_path: Destination path or URL to redirect to.
        status_code: HTTP status code (301 permanent, 302 temporary).
        is_active: Whether this redirect is currently active.
    """

    old_path: str
    new_path: str
    status_code: int = 301
    is_active: bool = True

    def __str__(self) -> str:
        return f"{self.old_path} ---> {self.new_path} ({self.status_code})"


@dataclass(slots=True)
class RedirectMatch:
    """Result of a redirect lookup.

    Attributes:
        new_path: Destination path or URL.
        status_code: HTTP status code for the redirect.
    """

    new_path: str
    status_code: int


@dataclass
class RedirectRegistry:
    """Thread-safe in-memory redirect registry with O(1) exact-match lookup.

    Maintains a dict mapping old_path -> (new_path, status_code) for all
    active redirects. All mutations are protected by a threading lock.

    The registry supports two lookup modes:
    1. Exact match: O(1) dict lookup on the full path.
    2. Prefix match: Linear scan of prefix redirects (paths ending with *).
    """

    _exact: dict[str, RedirectMatch] = field(default_factory=dict)
    _prefixes: dict[str, RedirectMatch] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _db_loader: Any = None  # Optional async callable to load from DB

    @property
    def count(self) -> int:
        """Number of active redirects in the registry."""
        with self._lock:
            return len(self._exact) + len(self._prefixes)

    async def load_all(self, redirects: list[Redirect] | None = None) -> int:
        """Load all active redirects into memory.

        If redirects list is provided, loads from that list.
        If a db_loader callable is set, calls it to fetch redirects.
        Returns the number of redirects loaded.
        """
        if redirects is None and self._db_loader is not None:
            redirects = await self._db_loader()

        if redirects is None:
            return 0

        with self._lock:
            self._exact.clear()
            self._prefixes.clear()
            loaded = 0
            for r in redirects:
                if not r.is_active:
                    continue
                # Open-redirect hardening: a target that starts with "/" but is
                # really protocol-relative ("//evil.com" or the backslash trick
                # "/\\evil.com") is never a legitimate same-origin path — it is
                # an open redirect disguised as a relative one. Skip such rows
                # even when loaded straight from the DB. Genuine external targets
                # (a real scheme like "https://…") are left untouched.
                np = r.new_path
                if np.startswith("/") and np[1:2] in ("/", "\\"):
                    continue
                match = RedirectMatch(new_path=r.new_path, status_code=r.status_code)
                if r.old_path.endswith("*"):
                    self._prefixes[r.old_path[:-1]] = match
                else:
                    self._exact[r.old_path] = match
                loaded += 1
            return loaded

    def add(
        self,
        old_path: str,
        new_path: str,
        status_code: int = 301,
        allow_external: bool = False,
    ) -> Redirect:
        """Add or update a redirect in the registry.

        Args:
            old_path: Source path to redirect from.
            new_path: Destination path or URL. Must start with "/" (relative)
                unless allow_external is True.
            status_code: HTTP status code (301 or 302).
            allow_external: If True, allow absolute URLs as redirect targets.

        Returns the Redirect object that was added.

        Raises:
            ValueError: If new_path is an absolute URL and allow_external is False.
        """
        if not allow_external and not _is_safe_relative_target(new_path):
            raise ValueError(
                f"Redirect target must be a safe relative path (starts with a "
                f"single /, no scheme/host, no backslash trick), got: {new_path}"
            )
        redirect = Redirect(
            old_path=old_path,
            new_path=new_path,
            status_code=status_code,
            is_active=True,
        )
        match = RedirectMatch(new_path=new_path, status_code=status_code)
        with self._lock:
            if old_path.endswith("*"):
                self._prefixes[old_path[:-1]] = match
            else:
                self._exact[old_path] = match
        return redirect

    def remove(self, old_path: str) -> bool:
        """Remove a redirect from the registry.

        Returns True if the redirect was found and removed, False otherwise.
        """
        with self._lock:
            if old_path.endswith("*"):
                prefix = old_path[:-1]
                if prefix in self._prefixes:
                    del self._prefixes[prefix]
                    return True
            else:
                if old_path in self._exact:
                    del self._exact[old_path]
                    return True
            return False

    def lookup(self, path: str) -> tuple[str, int] | None:
        """Look up a redirect for the given path.

        First checks exact match (O(1)), then prefix match (O(n) over prefixes).
        Returns (new_path, status_code) or None if no match.
        """
        with self._lock:
            # Exact match — O(1)
            exact = self._exact.get(path)
            if exact is not None:
                return (exact.new_path, exact.status_code)

            # Prefix match — check longest prefix first for specificity
            best_prefix = ""
            best_match: RedirectMatch | None = None
            for prefix, match in self._prefixes.items():
                if path.startswith(prefix) and len(prefix) > len(best_prefix):
                    best_prefix = prefix
                    best_match = match

            if best_match is not None:
                return (best_match.new_path, best_match.status_code)

        return None

    def clear(self) -> None:
        """Remove all redirects from the registry."""
        with self._lock:
            self._exact.clear()
            self._prefixes.clear()

    def all_redirects(self) -> list[Redirect]:
        """Return a list of all active redirects in the registry."""
        result: list[Redirect] = []
        with self._lock:
            for old_path, match in self._exact.items():
                result.append(
                    Redirect(
                        old_path=old_path,
                        new_path=match.new_path,
                        status_code=match.status_code,
                        is_active=True,
                    )
                )
            for prefix, match in self._prefixes.items():
                result.append(
                    Redirect(
                        old_path=prefix + "*",
                        new_path=match.new_path,
                        status_code=match.status_code,
                        is_active=True,
                    )
                )
        return result


# Module-level singleton registry
registry = RedirectRegistry()


@dataclass(slots=True)
class RedirectMiddleware:
    """Async middleware that intercepts 404 responses and checks for redirects.

    On every 404, performs an O(1) lookup against the redirect registry.
    If a match is found, returns the appropriate redirect response.
    Non-404 responses pass through unchanged.

    Usage:
        middleware = RedirectMiddleware(registry)

        # In middleware chain:
        async def redirect_mw(request, call_next):
            return await middleware(request, call_next)
    """

    registry: RedirectRegistry

    async def __call__(
        self,
        request: Request,
        call_next: Callable[[Request], Coroutine[Any, Any, Response]],
    ) -> Response:
        """Process the request — intercept 404s with matching redirects."""
        response = await call_next(request)

        if response.status != 404:
            return response

        # Check the full path (with query string) first, then bare path
        full_path = request.path
        if request.query_string:
            full_path = f"{request.path}?{request.query_string}"

        result = self.registry.lookup(full_path)
        if result is None:
            result = self.registry.lookup(request.path)

        if result is not None:
            new_path, status_code = result
            # Preserve query string on redirect if not already in new_path
            if request.query_string and "?" not in new_path:
                new_path = f"{new_path}?{request.query_string}"
            return Response.redirect(new_path, status=status_code)

        return response


async def redirect_view(request: Request) -> Response:
    """API view for managing redirects.

    GET: List all active redirects.
    POST: Add a new redirect (expects JSON body with old_path, new_path, status_code).
    DELETE: Remove a redirect (expects JSON body with old_path).
    """
    if request.method == "GET":
        redirects = registry.all_redirects()
        data = [
            {
                "old_path": r.old_path,
                "new_path": r.new_path,
                "status_code": r.status_code,
                "is_active": r.is_active,
            }
            for r in redirects
        ]
        return Response.json(data)

    if request.method == "POST":
        body = request.json
        old_path = body["old_path"]
        new_path = body["new_path"]
        status_code = body.get("status_code", 301)
        redirect = registry.add(old_path, new_path, status_code)
        return Response.json(
            {
                "old_path": redirect.old_path,
                "new_path": redirect.new_path,
                "status_code": redirect.status_code,
            },
            status=201,
        )

    if request.method == "DELETE":
        body = request.json
        old_path = body["old_path"]
        removed = registry.remove(old_path)
        if removed:
            return Response.json({"removed": old_path})
        return Response.error(404)

    return Response.error(405, headers={"Allow": "POST"})
