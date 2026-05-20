"""
Flatpages — serve simple, database-backed static pages.

Replaces django.contrib.flatpages with a native HyperDjango implementation.
Pages are stored in PostgreSQL and cached in an in-memory registry for O(1)
URL lookup. A middleware catches 404 responses and serves matching flatpages.

Usage:
    from hyperdjango.flatpages import registry, FlatPageMiddleware

    # Load all active pages into memory at startup
    await registry.load_all()

    # Add a page
    await registry.add("/about/", "About Us", "<h1>About</h1><p>Welcome.</p>")

    # Use middleware to auto-serve flatpages on 404
    app.add_middleware(FlatPageMiddleware)

    # Or use the standalone view
    from hyperdjango.flatpages import flatpage_view
    app.route("/pages/{url:path}")(flatpage_view)
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger("hyperdjango.flatpages")

from hyperdjango.database import get_db
from hyperdjango.response import Response

# --- SQL constants ---

_TABLE = "hyperdjango_flatpages"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    url VARCHAR(200) PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    template_name VARCHAR(200) NOT NULL DEFAULT 'flatpages/default.html',
    registration_required BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
)
"""

_CREATE_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_url ON {_TABLE} (url)
"""

_SELECT_ALL_ACTIVE_SQL = f"""
SELECT url, title, content, template_name, registration_required, is_active
FROM {_TABLE}
WHERE is_active = TRUE
"""

_SELECT_ONE_SQL = f"""
SELECT url, title, content, template_name, registration_required, is_active
FROM {_TABLE}
WHERE url = $1
"""

_UPSERT_SQL = f"""
INSERT INTO {_TABLE} (url, title, content, template_name, registration_required, is_active)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (url) DO UPDATE SET
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    template_name = EXCLUDED.template_name,
    registration_required = EXCLUDED.registration_required,
    is_active = EXCLUDED.is_active
"""

_DELETE_SQL = f"DELETE FROM {_TABLE} WHERE url = $1"

_SELECT_ALL_SQL = f"""
SELECT url, title, content, template_name, registration_required, is_active
FROM {_TABLE}
ORDER BY url
"""


# --- FlatPage dataclass ---


@dataclass(slots=True)
class FlatPage:
    """A single flat page stored in the database."""

    url: str
    title: str
    content: str = ""
    template_name: str = "flatpages/default.html"
    registration_required: bool = False
    is_active: bool = True

    def to_context(self) -> dict[str, str | bool]:
        """Return template context dict."""
        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "template_name": self.template_name,
            "registration_required": self.registration_required,
            "is_active": self.is_active,
        }


def _row_to_flatpage(row: dict[str, Any]) -> FlatPage:
    """Convert a database row dict to a FlatPage instance."""
    return FlatPage(
        url=row["url"],
        title=row["title"],
        content=row["content"],
        template_name=row["template_name"],
        registration_required=row["registration_required"],
        is_active=row["is_active"],
    )


# --- Normalize URL ---


def _normalize_url(url: str) -> str:
    """Ensure URL starts with / and ends with /."""
    if not url.startswith("/"):
        url = "/" + url
    if not url.endswith("/"):
        url = url + "/"
    return url


# --- FlatPageRegistry ---


@dataclass
class FlatPageRegistry:
    """Thread-safe in-memory registry of active flatpages.

    Stores url -> FlatPage mapping for O(1) lookup.
    Backed by PostgreSQL — load_all() populates from DB, add/remove
    write through to DB and update the in-memory cache.
    """

    _pages: dict[str, FlatPage] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def ensure_table(self) -> None:
        """Create the flatpages table if it doesn't exist."""
        db = get_db()
        await db.execute(_CREATE_TABLE_SQL)
        await db.execute(_CREATE_INDEX_SQL)

    async def load_all(self) -> None:
        """Load all active flatpages from the database into memory."""
        db = get_db()
        rows = await db.query(_SELECT_ALL_ACTIVE_SQL)
        with self._lock:
            self._pages.clear()
            for row in rows:
                page = _row_to_flatpage(row)
                self._pages[page.url] = page

    def lookup(self, url: str) -> FlatPage | None:
        """O(1) lookup of a flatpage by URL.

        Tries the exact URL first, then with trailing slash normalization.
        """
        with self._lock:
            page = self._pages.get(url)
            if page is not None:
                return page
            # Try normalized URL (add trailing slash)
            normalized = _normalize_url(url)
            if normalized != url:
                return self._pages.get(normalized)
            return None

    async def add(
        self,
        url: str,
        title: str,
        content: str = "",
        template_name: str = "flatpages/default.html",
        registration_required: bool = False,
        is_active: bool = True,
    ) -> FlatPage:
        """Add or update a flatpage in DB and in-memory cache."""
        url = _normalize_url(url)
        page = FlatPage(
            url=url,
            title=title,
            content=content,
            template_name=template_name,
            registration_required=registration_required,
            is_active=is_active,
        )
        db = get_db()
        await db.execute(
            _UPSERT_SQL,
            page.url,
            page.title,
            page.content,
            page.template_name,
            page.registration_required,
            page.is_active,
        )
        with self._lock:
            if is_active:
                self._pages[page.url] = page
            else:
                # Inactive pages should not be in the lookup cache
                self._pages.pop(page.url, None)
        return page

    async def remove(self, url: str) -> bool:
        """Remove a flatpage from DB and in-memory cache. Returns True if removed."""
        url = _normalize_url(url)
        db = get_db()
        # execute() returns the affected-row count: 1 if a row was deleted, 0 if not.
        removed = await db.execute(_DELETE_SQL, url) != 0
        with self._lock:
            self._pages.pop(url, None)
        return removed

    def get_all(self) -> list[FlatPage]:
        """Return all cached active flatpages, sorted by URL. For sitemap generation."""
        with self._lock:
            return sorted(self._pages.values(), key=lambda p: p.url)


# --- Module-level singleton ---

registry = FlatPageRegistry()


# --- Rendering ---


def _render_flatpage(page: FlatPage, template_engine: Any = None) -> str:
    """Render a flatpage to HTML string.

    Tries the template engine first. If no engine or template not found,
    returns the raw content as HTML.
    """
    if template_engine is not None:
        template_name = page.template_name or "flatpages/default.html"
        try:
            return template_engine.render(
                template_name, {"flatpage": page.to_context()}
            )
        # blind-except: optional template render; on any failure log and fall through to the built-in minimal HTML wrapper so the page still serves.
        except Exception as exc:
            _logger.warning("Template render failed for flatpage %s: %s", page.url, exc)
    # Fallback: wrap content in minimal HTML
    return f"<html><head><title>{page.title}</title></head><body>{page.content}</body></html>"


# --- Middleware ---


@dataclass
class FlatPageMiddleware:
    """Middleware that catches 404 responses and serves matching flatpages.

    Install after routing middleware. When a response has status 404,
    this middleware checks the registry for a matching flatpage URL
    and renders it if found.

    Attributes:
        template_engine: Optional template engine for rendering. If None,
                         raw HTML content is returned.
    """

    template_engine: Any = None

    async def __call__(self, request: Any, response: Response) -> Response:
        """Process response — intercept 404s and serve flatpages."""
        if response.status != 404:
            return response

        url = request.path if isinstance(request.path, str) else str(request.path)
        page = registry.lookup(url)

        if page is None:
            return response

        if not page.is_active:
            return response

        # Check registration requirement
        if page.registration_required:
            is_authenticated = False
            if request.user is not None:
                is_authenticated = request.user.is_authenticated
            if not is_authenticated:
                return response

        html = _render_flatpage(page, self.template_engine)
        return Response.html(html, status=200)


# --- Standalone view ---


async def flatpage_view(request: Any) -> Response:
    """Standalone view that renders a flatpage by URL.

    Looks up the request path in the registry and returns the rendered
    page, or a 404 response if not found.
    """
    url = request.path if isinstance(request.path, str) else str(request.path)
    page = registry.lookup(url)

    if page is None:
        return Response(status=404, body=b"Not Found")

    if not page.is_active:
        return Response(status=404, body=b"Not Found")

    if page.registration_required:
        is_authenticated = False
        if request.user is not None:
            is_authenticated = request.user.is_authenticated
        if not is_authenticated:
            return Response(status=404, body=b"Not Found")

    html = _render_flatpage(page)
    return Response.html(html, status=200)
