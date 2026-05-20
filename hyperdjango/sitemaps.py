"""
Sitemaps module -- native replacement for django.contrib.sitemaps.

Generates XML sitemaps and sitemap indexes per the sitemaps.org protocol.
Uses string-based XML building (no template engine needed for trivial XML).

Usage:
    from hyperdjango.sitemaps import Sitemap, GenericSitemap, sitemap_view

    class ArticleSitemap(Sitemap):
        changefreq = "daily"
        priority = 0.8

        def items(self):
            return Article.objects.all()

        def lastmod(self, item):
            return item.updated_at

    sitemaps = {"articles": ArticleSitemap()}
    # Wire into your router:
    # app.route("/sitemap.xml")(lambda req: sitemap_view(req, sitemaps))
"""

import hashlib
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from hyperdjango.paginator import Page
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# Valid changefreq values per sitemaps.org protocol
# ---------------------------------------------------------------------------

VALID_CHANGEFREQS = frozenset(
    {"always", "hourly", "daily", "weekly", "monthly", "yearly", "never"}
)


# ---------------------------------------------------------------------------
# XML escaping
# ---------------------------------------------------------------------------


def xml_escape(value: str) -> str:
    """Escape special XML characters in a string."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------


def _format_lastmod(dt: datetime | date) -> str:
    """Format a datetime or date as ISO 8601 for sitemaps.

    datetime with tzinfo -> full W3C datetime (YYYY-MM-DDThh:mm:ssTZD)
    datetime without tzinfo -> treated as UTC
    date -> YYYY-MM-DD
    """
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Plain date
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# SimplePaginator -- synchronous paginator for sitemap items (lists)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SimplePaginator:
    """Synchronous paginator over a plain list. Used by Sitemap.paginator.

    The async Paginator from hyperdjango.paginator is designed for QuerySets
    with async count/offset/limit. Sitemaps need synchronous pagination over
    the already-fetched items() list, so we use this lightweight wrapper that
    returns the same Page dataclass.
    """

    object_list: list[Any]
    per_page: int

    @property
    def count(self) -> int:
        return len(self.object_list)

    @property
    def num_pages(self) -> int:
        if self.count == 0:
            return 1
        return math.ceil(self.count / self.per_page)

    @property
    def page_range(self) -> range:
        return range(1, self.num_pages + 1)

    def page(self, number: int) -> Page:
        """Return a Page for the given 1-based page number."""
        number = int(number)
        if number < 1:
            raise ValueError(f"Page number must be >= 1, got {number}")
        if number > self.num_pages:
            raise ValueError(f"Page {number} out of range (num_pages={self.num_pages})")
        start = (number - 1) * self.per_page
        end = start + self.per_page
        items = self.object_list[start:end]
        return Page(
            items=items,
            number=number,
            num_pages=self.num_pages,
            count=self.count,
            per_page=self.per_page,
        )


# ---------------------------------------------------------------------------
# Sitemap base class
# ---------------------------------------------------------------------------


class Sitemap:
    """Base sitemap class. Subclass and override items(), location(), etc.

    Class attributes:
        limit: Max items per sitemap page (default 50000, Google's limit).
        protocol: URL protocol, default "https".
        changefreq: Default change frequency (str or None). Can also be a method.
        priority: Default priority (float or None). Can also be a method.
    """

    limit: int = 50000
    protocol: str = "https"
    changefreq: str | None = None
    priority: float | None = None

    def items(self) -> list[Any]:
        """Return the list of items to include in this sitemap."""
        return []

    def location(self, item: Any) -> str:
        """Return the URL path for an item. Default: item.get_absolute_url()."""
        return item.get_absolute_url()

    def lastmod(self, item: Any) -> datetime | date | None:
        """Return the last-modified datetime for an item, or None."""
        return None

    def get_changefreq(self, item: Any) -> str | None:
        """Return changefreq for an item. Checks if changefreq is callable."""
        cf = self.__class__.changefreq
        if callable(cf):
            return cf(self, item)
        return cf

    def get_priority(self, item: Any) -> float | None:
        """Return priority for an item. Checks if priority is callable."""
        p = self.__class__.priority
        if callable(p):
            return p(self, item)
        return p

    @property
    def paginator(self) -> SimplePaginator:
        """Return a SimplePaginator over items()."""
        items = self.items()
        if not isinstance(items, list):
            items = list(items)
        return SimplePaginator(object_list=items, per_page=self.limit)

    def get_latest_lastmod(self) -> datetime | date | None:
        """Return the most recent lastmod across all items, or None."""
        latest = None
        for item in self.items():
            lm = self.lastmod(item)
            if lm is not None:
                if latest is None or lm > latest:
                    latest = lm
        return latest


# ---------------------------------------------------------------------------
# GenericSitemap
# ---------------------------------------------------------------------------


class GenericSitemap(Sitemap):
    """A sitemap built from a queryset and optional date_field.

    Usage:
        GenericSitemap(queryset=Article.objects.all(), date_field="updated_at")
    """

    def __init__(
        self,
        queryset: Any,
        date_field: str | None = None,
        priority: float | None = None,
        changefreq: str | None = None,
        protocol: str | None = None,
    ):
        self.queryset = queryset
        self.date_field = date_field
        if priority is not None:
            self.priority = priority
        if changefreq is not None:
            self.changefreq = changefreq
        if protocol is not None:
            self.protocol = protocol

    def items(self) -> list[Any]:
        """Return queryset items as a list."""
        qs = self.queryset
        if isinstance(qs, list):
            return qs
        return list(qs)

    def lastmod(self, item: Any) -> datetime | date | None:
        """Return date_field value from item, or None.

        Uses object.__getattribute__ for direct attribute access because
        date_field is a dynamic string name provided at init time -- we cannot
        know the attribute name at code-writing time, so static .attr syntax
        is not possible here.
        """
        if self.date_field is not None:
            return object.__getattribute__(item, self.date_field)
        return None


# ---------------------------------------------------------------------------
# XML rendering
# ---------------------------------------------------------------------------


def _render_sitemap_xml(
    sitemap: Sitemap,
    page: int,
    request_host: str,
) -> bytes:
    """Render a single sitemap page as XML bytes.

    Args:
        sitemap: The Sitemap instance.
        page: 1-based page number.
        request_host: The host (domain) from the request, e.g. "example.com".
    """
    protocol = sitemap.protocol
    pag = sitemap.paginator
    page_obj = pag.page(page)

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for item in page_obj.items:
        loc_path = sitemap.location(item)
        loc = f"{protocol}://{xml_escape(request_host)}{xml_escape(loc_path)}"

        parts.append("  <url>")
        parts.append(f"    <loc>{loc}</loc>")

        lm = sitemap.lastmod(item)
        if lm is not None:
            parts.append(f"    <lastmod>{_format_lastmod(lm)}</lastmod>")

        cf = sitemap.get_changefreq(item)
        if cf is not None:
            parts.append(f"    <changefreq>{xml_escape(cf)}</changefreq>")

        pri = sitemap.get_priority(item)
        if pri is not None:
            parts.append(f"    <priority>{pri:.1f}</priority>")

        parts.append("  </url>")

    parts.append("</urlset>")
    return "\n".join(parts).encode("utf-8")


def _render_sitemap_index_xml(
    sitemaps: dict[str, Sitemap],
    request_host: str,
    protocol: str = "https",
) -> bytes:
    """Render a sitemap index XML linking to all section sitemaps.

    Args:
        sitemaps: Dict mapping section name to Sitemap instance.
        request_host: The host (domain) from the request.
        protocol: URL protocol (default "https").
    """
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for section, sm in sitemaps.items():
        pag = sm.paginator
        for page_num in pag.page_range:
            loc = f"{protocol}://{xml_escape(request_host)}/sitemap-{xml_escape(section)}.xml"
            if pag.num_pages > 1:
                loc += f"?p={page_num}"

            parts.append("  <sitemap>")
            parts.append(f"    <loc>{loc}</loc>")

            latest = sm.get_latest_lastmod()
            if latest is not None:
                parts.append(f"    <lastmod>{_format_lastmod(latest)}</lastmod>")

            parts.append("  </sitemap>")

    parts.append("</sitemapindex>")
    return "\n".join(parts).encode("utf-8")


# ---------------------------------------------------------------------------
# Sitemap view
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _MockHeaders:
    """Minimal request-like object for extracting host."""

    data: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)


def _get_host(request: Any) -> str:
    """Extract host from a request object."""
    # HyperDjango Request has .headers dict
    headers = request.headers
    if isinstance(headers, dict):
        return headers.get("host", "localhost")
    return headers.get("host", "localhost")


def sitemap_view(
    request: Any,
    sitemaps: dict[str, Sitemap],
    section: str | None = None,
    page: int | None = None,
) -> Response:
    """Serve a sitemap or sitemap index.

    Args:
        request: The incoming HTTP request.
        sitemaps: Dict mapping section names to Sitemap instances.
        section: If provided, render that section's sitemap. Otherwise, index.
        page: Page number for paginated sitemaps (1-based). Defaults to 1.

    Returns:
        Response with Content-Type application/xml, ETag, and Cache-Control.
    """
    host = _get_host(request)

    if section is not None:
        if section not in sitemaps:
            return Response(
                body=b"Sitemap section not found",
                status=404,
                content_type="text/plain; charset=utf-8",
            )
        sm = sitemaps[section]
        page_num = page if page is not None else 1
        xml_bytes = _render_sitemap_xml(sm, page_num, host)
    else:
        # Render sitemap index
        xml_bytes = _render_sitemap_index_xml(sitemaps, host)

    resp = Response(
        body=xml_bytes,
        status=200,
        content_type="application/xml; charset=utf-8",
    )
    resp.set_etag(hashlib.md5(xml_bytes).hexdigest())
    resp.cache_control(public=True, max_age=3600)
    return resp
