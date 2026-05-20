"""
RSS 2.0 and Atom 1.0 feed generation.

Replaces django.contrib.syndication with a native implementation.
Supports RSS 2.0, Atom 1.0, enclosures (podcasts), ETag caching,
and all standard feed/item metadata.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.sax.saxutils import escape as _sax_escape

from hyperdjango.request import Request
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# XML / date helpers
# ---------------------------------------------------------------------------


def _xml_escape(text: str) -> str:
    """Escape XML special characters: & < > " '."""
    return _sax_escape(text, {"'": "&apos;", '"': "&quot;"})


_RFC822_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_RFC822_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _format_rfc822(dt: datetime) -> str:
    """Format *dt* as an RFC 822 date string (used in RSS 2.0).

    If *dt* is naive it is assumed to be UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    return (
        f"{_RFC822_DAYS[utc.weekday()]}, "
        f"{utc.day:02d} {_RFC822_MONTHS[utc.month - 1]} {utc.year} "
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d} +0000"
    )


def _format_rfc3339(dt: datetime) -> str:
    """Format *dt* as an RFC 3339 date string (used in Atom 1.0).

    If *dt* is naive it is assumed to be UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Feed base class
# ---------------------------------------------------------------------------


@dataclass
class Feed:
    """Base class for syndication feeds.

    Subclass and override the ``title``, ``link``, ``description``,
    ``items``, and ``item_*`` methods to produce a feed.
    """

    language: str = "en"
    feed_type: str = "rss"  # "rss" or "atom"

    # -- Feed-level metadata --------------------------------------------------

    def title(self) -> str:  # pragma: no cover — meant to be overridden
        return ""

    def link(self) -> str:  # pragma: no cover
        return ""

    def description(self) -> str:  # pragma: no cover
        return ""

    def feed_url(self) -> str:
        """Return the URL of the feed itself (for atom:link rel=self)."""
        return ""

    # -- Item enumeration -----------------------------------------------------

    def items(self) -> list[object]:  # pragma: no cover
        return []

    # -- Per-item metadata ----------------------------------------------------

    def item_title(self, item: object) -> str:
        return str(item)

    def item_description(self, item: object) -> str:
        return str(item)

    def item_link(self, item: object) -> str:
        return ""

    def item_pubdate(self, item: object) -> datetime | None:
        return None

    def item_author_name(self, item: object) -> str | None:
        return None

    def item_categories(self, item: object) -> list[str] | None:
        return None

    def item_enclosure_url(self, item: object) -> str | None:
        return None

    def item_enclosure_length(self, item: object) -> int | None:
        return None

    def item_enclosure_mime_type(self, item: object) -> str | None:
        return None

    def item_guid(self, item: object) -> str | None:
        return None

    def item_guid_is_permalink(self, item: object) -> bool:
        return True


# ---------------------------------------------------------------------------
# RSS 2.0 renderer
# ---------------------------------------------------------------------------


def _render_rss(feed_instance: Feed, request: Request | None = None) -> bytes:
    """Render *feed_instance* as RSS 2.0 XML bytes."""
    parts: list[str] = []
    _a = parts.append

    _a('<?xml version="1.0" encoding="utf-8"?>')
    _a('<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">')
    _a("<channel>")

    _a(f"<title>{_xml_escape(feed_instance.title())}</title>")
    _a(f"<link>{_xml_escape(feed_instance.link())}</link>")
    _a(f"<description>{_xml_escape(feed_instance.description())}</description>")
    _a(f"<language>{_xml_escape(feed_instance.language)}</language>")

    # atom:link self
    feed_url = feed_instance.feed_url()
    if not feed_url and request is not None:
        feed_url = request.path
    if feed_url:
        _a(
            f'<atom:link href="{_xml_escape(feed_url)}" '
            f'rel="self" type="application/rss+xml"/>'
        )

    # lastBuildDate — use most recent item pubdate or now
    items = list(feed_instance.items())
    now_str = _format_rfc822(datetime.now(UTC))
    latest_date: datetime | None = None
    for item in items:
        pd = feed_instance.item_pubdate(item)
        if pd is not None:
            if latest_date is None or pd > latest_date:
                latest_date = pd
    _a(
        f"<lastBuildDate>{_format_rfc822(latest_date) if latest_date else now_str}</lastBuildDate>"
    )

    # Items
    for item in items:
        _a("<item>")
        _a(f"<title>{_xml_escape(feed_instance.item_title(item))}</title>")

        item_link = feed_instance.item_link(item)
        _a(f"<link>{_xml_escape(item_link)}</link>")
        _a(
            f"<description>{_xml_escape(feed_instance.item_description(item))}</description>"
        )

        pubdate = feed_instance.item_pubdate(item)
        if pubdate is not None:
            _a(f"<pubDate>{_format_rfc822(pubdate)}</pubDate>")

        # GUID
        guid = feed_instance.item_guid(item)
        is_permalink = feed_instance.item_guid_is_permalink(item)
        if guid is not None:
            permalink_attr = "true" if is_permalink else "false"
            _a(f'<guid isPermaLink="{permalink_attr}">{_xml_escape(guid)}</guid>')
        elif item_link:
            _a(f'<guid isPermaLink="true">{_xml_escape(item_link)}</guid>')

        author = feed_instance.item_author_name(item)
        if author is not None:
            _a(f"<author>{_xml_escape(author)}</author>")

        categories = feed_instance.item_categories(item)
        if categories is not None:
            for cat in categories:
                _a(f"<category>{_xml_escape(cat)}</category>")

        # Enclosure
        enc_url = feed_instance.item_enclosure_url(item)
        if enc_url is not None:
            enc_length = feed_instance.item_enclosure_length(item) or 0
            enc_type = (
                feed_instance.item_enclosure_mime_type(item)
                or "application/octet-stream"
            )
            _a(
                f'<enclosure url="{_xml_escape(enc_url)}" '
                f'length="{enc_length}" '
                f'type="{_xml_escape(enc_type)}"/>'
            )

        _a("</item>")

    _a("</channel>")
    _a("</rss>")

    return "\n".join(parts).encode("utf-8")


# ---------------------------------------------------------------------------
# Atom 1.0 renderer
# ---------------------------------------------------------------------------


def _render_atom(feed_instance: Feed, request: Request | None = None) -> bytes:
    """Render *feed_instance* as Atom 1.0 XML bytes."""
    parts: list[str] = []
    _a = parts.append

    _a('<?xml version="1.0" encoding="utf-8"?>')
    _a('<feed xmlns="http://www.w3.org/2005/Atom">')

    _a(f"<title>{_xml_escape(feed_instance.title())}</title>")
    _a(f'<link href="{_xml_escape(feed_instance.link())}" rel="alternate"/>')
    _a(f"<subtitle>{_xml_escape(feed_instance.description())}</subtitle>")

    # Feed ID — use link as the ID
    feed_link = feed_instance.link()
    _a(f"<id>{_xml_escape(feed_link)}</id>")

    # Self link
    feed_url = feed_instance.feed_url()
    if not feed_url and request is not None:
        feed_url = request.path
    if feed_url:
        _a(f'<link href="{_xml_escape(feed_url)}" rel="self"/>')

    # Updated — use most recent item pubdate or now
    items = list(feed_instance.items())
    latest_date: datetime | None = None
    for item in items:
        pd = feed_instance.item_pubdate(item)
        if pd is not None:
            if latest_date is None or pd > latest_date:
                latest_date = pd
    updated = latest_date or datetime.now(UTC)
    _a(f"<updated>{_format_rfc3339(updated)}</updated>")

    # Entries
    for item in items:
        _a("<entry>")
        _a(f"<title>{_xml_escape(feed_instance.item_title(item))}</title>")

        item_link = feed_instance.item_link(item)
        _a(f'<link href="{_xml_escape(item_link)}" rel="alternate"/>')
        _a(f"<summary>{_xml_escape(feed_instance.item_description(item))}</summary>")

        pubdate = feed_instance.item_pubdate(item)
        if pubdate is not None:
            _a(f"<published>{_format_rfc3339(pubdate)}</published>")
            _a(f"<updated>{_format_rfc3339(pubdate)}</updated>")

        # ID — use guid or link
        guid = feed_instance.item_guid(item)
        entry_id = guid if guid is not None else item_link
        _a(f"<id>{_xml_escape(entry_id)}</id>")

        author = feed_instance.item_author_name(item)
        if author is not None:
            _a("<author>")
            _a(f"<name>{_xml_escape(author)}</name>")
            _a("</author>")

        categories = feed_instance.item_categories(item)
        if categories is not None:
            for cat in categories:
                _a(f'<category term="{_xml_escape(cat)}"/>')

        _a("</entry>")

    _a("</feed>")

    return "\n".join(parts).encode("utf-8")


# ---------------------------------------------------------------------------
# View function
# ---------------------------------------------------------------------------


def feed_view(request: Request, feed_class: type[Feed]) -> Response:
    """Instantiate *feed_class*, render, and return a ``Response``.

    Supports ETag-based conditional GET: if the client sends an
    ``If-None-Match`` header matching the feed's ETag, a 304 is returned.
    """
    instance = feed_class()

    if instance.feed_type == "atom":
        xml_bytes = _render_atom(instance, request)
        content_type = "application/atom+xml; charset=utf-8"
    else:
        xml_bytes = _render_rss(instance, request)
        content_type = "application/rss+xml; charset=utf-8"

    # ETag
    etag = '"' + hashlib.md5(xml_bytes).hexdigest() + '"'

    # Check If-None-Match
    if_none_match = _get_header(request, "if-none-match")
    if if_none_match is not None and if_none_match == etag:
        return Response(body=b"", status=304, headers={"ETag": etag})

    return Response(
        body=xml_bytes,
        status=200,
        content_type=content_type,
        headers={"ETag": etag},
    )


def _get_header(request: Request, name: str) -> str | None:
    """Read a header from the request (case-insensitive).

    Uses Request.headers dict directly — keys are lowercase.
    """
    return request.headers.get(name)
