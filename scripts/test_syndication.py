"""Tests for the syndication module (RSS/Atom feed generation)."""

# hyper-test: unit

import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from hyperdjango.request import Request
from hyperdjango.syndication import (
    Feed,
    _format_rfc822,
    _format_rfc3339,
    _render_atom,
    _render_rss,
    _xml_escape,
    feed_view,
)

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" -- {detail}"
        print(msg)
        failed += 1
        errors.append(name)


# ---------------------------------------------------------------------------
# Helper: minimal request
# ---------------------------------------------------------------------------


def _make_request(
    path: str = "/feed/", headers: dict[str, str] | None = None
) -> Request:
    return Request(
        method="GET",
        path=path,
        headers=headers or {},
    )


# ---------------------------------------------------------------------------
# Sample feed subclasses
# ---------------------------------------------------------------------------


@dataclass
class Article:
    title: str
    url: str
    summary: str
    published: datetime
    author: str = ""
    tags: list[str] | None = None


SAMPLE_ARTICLES = [
    Article(
        title="First Post",
        url="https://example.com/first",
        summary="This is the first post.",
        published=datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
        author="Alice",
        tags=["news", "tech"],
    ),
    Article(
        title="Second Post",
        url="https://example.com/second",
        summary="This is the second post.",
        published=datetime(2026, 3, 15, 8, 30, 0, tzinfo=UTC),
        author="Bob",
        tags=["science"],
    ),
]


@dataclass
class BlogFeed(Feed):
    language: str = "en"
    feed_type: str = "rss"

    def title(self) -> str:
        return "My Blog"

    def link(self) -> str:
        return "https://example.com/"

    def description(self) -> str:
        return "A sample blog feed"

    def feed_url(self) -> str:
        return "https://example.com/feed/"

    def items(self) -> list[Article]:
        return SAMPLE_ARTICLES

    def item_title(self, item: object) -> str:
        assert isinstance(item, Article)
        return item.title

    def item_description(self, item: object) -> str:
        assert isinstance(item, Article)
        return item.summary

    def item_link(self, item: object) -> str:
        assert isinstance(item, Article)
        return item.url

    def item_pubdate(self, item: object) -> datetime | None:
        assert isinstance(item, Article)
        return item.published

    def item_author_name(self, item: object) -> str | None:
        assert isinstance(item, Article)
        return item.author or None

    def item_categories(self, item: object) -> list[str] | None:
        assert isinstance(item, Article)
        return item.tags


@dataclass
class AtomBlogFeed(BlogFeed):
    feed_type: str = "atom"


@dataclass
class EmptyFeed(Feed):
    language: str = "en"
    feed_type: str = "rss"

    def title(self) -> str:
        return "Empty Feed"

    def link(self) -> str:
        return "https://example.com/empty"

    def description(self) -> str:
        return "Nothing here"

    def items(self) -> list[object]:
        return []


@dataclass
class PodcastItem:
    title: str
    url: str
    audio_url: str
    audio_length: int
    audio_type: str
    guid: str


PODCAST_ITEMS = [
    PodcastItem(
        title="Episode 1",
        url="https://podcast.example.com/ep1",
        audio_url="https://cdn.example.com/ep1.mp3",
        audio_length=12345678,
        audio_type="audio/mpeg",
        guid="podcast-ep-1",
    ),
]


@dataclass
class PodcastFeed(Feed):
    language: str = "en"
    feed_type: str = "rss"

    def title(self) -> str:
        return "My Podcast"

    def link(self) -> str:
        return "https://podcast.example.com/"

    def description(self) -> str:
        return "A podcast feed"

    def items(self) -> list[PodcastItem]:
        return PODCAST_ITEMS

    def item_title(self, item: object) -> str:
        assert isinstance(item, PodcastItem)
        return item.title

    def item_description(self, item: object) -> str:
        assert isinstance(item, PodcastItem)
        return f"Listen to {item.title}"

    def item_link(self, item: object) -> str:
        assert isinstance(item, PodcastItem)
        return item.url

    def item_enclosure_url(self, item: object) -> str | None:
        assert isinstance(item, PodcastItem)
        return item.audio_url

    def item_enclosure_length(self, item: object) -> int | None:
        assert isinstance(item, PodcastItem)
        return item.audio_length

    def item_enclosure_mime_type(self, item: object) -> str | None:
        assert isinstance(item, PodcastItem)
        return item.audio_type

    def item_guid(self, item: object) -> str | None:
        assert isinstance(item, PodcastItem)
        return item.guid

    def item_guid_is_permalink(self, item: object) -> bool:
        return False


@dataclass
class XssFeed(Feed):
    """Feed with XML-special characters in content."""

    language: str = "en"
    feed_type: str = "rss"

    def title(self) -> str:
        return "Feed <with> \"special\" & 'chars'"

    def link(self) -> str:
        return "https://example.com/"

    def description(self) -> str:
        return "A & B < C > D"

    def items(self) -> list[object]:
        return ["item with <html> & stuff"]

    def item_title(self, item: object) -> str:
        return str(item)

    def item_description(self, item: object) -> str:
        return str(item)

    def item_link(self, item: object) -> str:
        return "https://example.com/special?a=1&b=2"


@dataclass
class FrenchFeed(Feed):
    language: str = "fr"
    feed_type: str = "rss"

    def title(self) -> str:
        return "Mon Blog"

    def link(self) -> str:
        return "https://example.fr/"

    def description(self) -> str:
        return "Un blog en francais"

    def items(self) -> list[object]:
        return []


# =========================================================================
# Tests
# =========================================================================

print("=" * 60)
print("TEST: syndication (RSS/Atom feed generation)")
print("=" * 60)

# --- XML escaping ---

print("\n-- XML escaping --")
check("escape ampersand", _xml_escape("A & B") == "A &amp; B")
check("escape lt", _xml_escape("a < b") == "a &lt; b")
check("escape gt", _xml_escape("a > b") == "a &gt; b")
check("escape double quote", _xml_escape('say "hi"') == "say &quot;hi&quot;")
check("escape single quote", _xml_escape("it's") == "it&apos;s")
check(
    "escape combined",
    _xml_escape('<a href="x">&</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;",
)

# --- RFC 822 date formatting ---

print("\n-- RFC 822 date formatting --")
dt1 = datetime(2026, 3, 15, 14, 30, 0, tzinfo=UTC)
rfc822 = _format_rfc822(dt1)
check("rfc822 format", rfc822 == "Sun, 15 Mar 2026 14:30:00 +0000", f"got {rfc822!r}")

dt_naive = datetime(2026, 1, 1, 0, 0, 0)
rfc822_naive = _format_rfc822(dt_naive)
check(
    "rfc822 naive assumed UTC",
    "01 Jan 2026 00:00:00 +0000" in rfc822_naive,
    f"got {rfc822_naive!r}",
)

# --- RFC 3339 date formatting ---

print("\n-- RFC 3339 date formatting --")
rfc3339 = _format_rfc3339(dt1)
check("rfc3339 format", rfc3339 == "2026-03-15T14:30:00Z", f"got {rfc3339!r}")

rfc3339_naive = _format_rfc3339(dt_naive)
check(
    "rfc3339 naive assumed UTC",
    rfc3339_naive == "2026-01-01T00:00:00Z",
    f"got {rfc3339_naive!r}",
)

# --- RSS 2.0 rendering ---

print("\n-- RSS 2.0 rendering --")
rss_bytes = _render_rss(BlogFeed())
rss = rss_bytes.decode("utf-8")

check("rss xml declaration", rss.startswith('<?xml version="1.0" encoding="utf-8"?>'))
check("rss version 2.0", 'rss version="2.0"' in rss)
check("rss atom namespace", 'xmlns:atom="http://www.w3.org/2005/Atom"' in rss)
check("rss channel title", "<title>My Blog</title>" in rss)
check("rss channel link", "<link>https://example.com/</link>" in rss)
check("rss channel description", "<description>A sample blog feed</description>" in rss)
check("rss language", "<language>en</language>" in rss)
check("rss atom:link self", 'atom:link href="https://example.com/feed/"' in rss)
check("rss lastBuildDate present", "<lastBuildDate>" in rss)
check("rss item title", "<title>First Post</title>" in rss)
check("rss item link", "<link>https://example.com/first</link>" in rss)
check(
    "rss item description", "<description>This is the first post.</description>" in rss
)
check("rss item pubDate", "<pubDate>Sun, 01 Mar 2026 12:00:00 +0000</pubDate>" in rss)
check("rss item author", "<author>Alice</author>" in rss)
check("rss item category news", "<category>news</category>" in rss)
check("rss item category tech", "<category>tech</category>" in rss)
check("rss second item", "<title>Second Post</title>" in rss)
check("rss item guid permalink", 'isPermaLink="true"' in rss)

# --- Atom 1.0 rendering ---

print("\n-- Atom 1.0 rendering --")
atom_bytes = _render_atom(AtomBlogFeed())
atom = atom_bytes.decode("utf-8")

check("atom xml declaration", atom.startswith('<?xml version="1.0" encoding="utf-8"?>'))
check("atom namespace", 'xmlns="http://www.w3.org/2005/Atom"' in atom)
check("atom title", "<title>My Blog</title>" in atom)
check("atom alternate link", 'link href="https://example.com/" rel="alternate"' in atom)
check("atom subtitle", "<subtitle>A sample blog feed</subtitle>" in atom)
check("atom updated", "<updated>" in atom)
check("atom feed id", "<id>https://example.com/</id>" in atom)
check("atom entry title", "<title>First Post</title>" in atom)
check(
    "atom entry link", 'link href="https://example.com/first" rel="alternate"' in atom
)
check("atom entry summary", "<summary>This is the first post.</summary>" in atom)
check("atom entry published", "<published>2026-03-01T12:00:00Z</published>" in atom)
check("atom entry author name", "<name>Alice</name>" in atom)
check("atom entry category", 'category term="news"' in atom)
check("atom entry id", "<id>https://example.com/first</id>" in atom)

# --- Enclosure / podcast support ---

print("\n-- Enclosure (podcast) support --")
podcast_rss = _render_rss(PodcastFeed()).decode("utf-8")

check("enclosure url", 'url="https://cdn.example.com/ep1.mp3"' in podcast_rss)
check("enclosure length", 'length="12345678"' in podcast_rss)
check("enclosure type", 'type="audio/mpeg"' in podcast_rss)
check("podcast guid non-permalink", 'isPermaLink="false"' in podcast_rss)
check("podcast guid value", ">podcast-ep-1</guid>" in podcast_rss)

# --- XML escaping in content ---

print("\n-- XML escaping in feed content --")
xss_rss = _render_rss(XssFeed()).decode("utf-8")

check("escaped title ampersand", "&amp;" in xss_rss)
check("escaped title angle brackets", "&lt;with&gt;" in xss_rss)
check("escaped title quotes", "&quot;special&quot;" in xss_rss)
check("escaped description", "A &amp; B &lt; C &gt; D" in xss_rss)

# --- Empty feed ---

print("\n-- Empty feed --")
empty_rss = _render_rss(EmptyFeed()).decode("utf-8")

check("empty feed has channel", "<channel>" in empty_rss)
check("empty feed title", "<title>Empty Feed</title>" in empty_rss)
check("empty feed no items", "<item>" not in empty_rss)
check("empty feed lastBuildDate", "<lastBuildDate>" in empty_rss)

# --- Language attribute ---

print("\n-- Language attribute --")
french_rss = _render_rss(FrenchFeed()).decode("utf-8")
check("french language tag", "<language>fr</language>" in french_rss)

# --- feed_view returns correct content type ---

print("\n-- feed_view content type --")
req = _make_request("/feed/")
rss_response = feed_view(req, BlogFeed)
check(
    "rss content type",
    rss_response.headers.get("content-type", "").startswith("application/rss+xml"),
)
check("rss response status 200", rss_response.status == 200)
check("rss response body is bytes", isinstance(rss_response.body, bytes))

atom_response = feed_view(req, AtomBlogFeed)
check(
    "atom content type",
    atom_response.headers.get("content-type", "").startswith("application/atom+xml"),
)

# --- ETag caching ---

print("\n-- ETag caching --")
check("response has ETag", "ETag" in rss_response.headers)
etag_value = rss_response.headers["ETag"]
check("ETag is quoted", etag_value.startswith('"') and etag_value.endswith('"'))

# Conditional GET with matching ETag
req_cached = _make_request("/feed/", headers={"if-none-match": etag_value})
cached_response = feed_view(req_cached, BlogFeed)
check(
    "304 on matching ETag",
    cached_response.status == 304,
    f"got {cached_response.status}",
)
check("304 body is empty", cached_response.body == b"")

# Conditional GET with non-matching ETag
req_stale = _make_request("/feed/", headers={"if-none-match": '"stale"'})
stale_response = feed_view(req_stale, BlogFeed)
check("200 on stale ETag", stale_response.status == 200)

# --- feed_type="atom" switches format ---

print("\n-- feed_type switches format --")
atom_via_view = feed_view(req, AtomBlogFeed)
atom_body = atom_via_view.body.decode("utf-8")
check(
    "atom via view has atom namespace",
    'xmlns="http://www.w3.org/2005/Atom"' in atom_body,
)
check("atom via view no rss tag", "<rss " not in atom_body)

# --- Custom item methods override defaults ---

print("\n-- Custom item method overrides --")
# The default Feed.item_title just does str(item), but BlogFeed overrides it
default_feed = Feed()
check("default item_title uses str", default_feed.item_title("hello") == "hello")
check("default item_description uses str", default_feed.item_description(42) == "42")
check("default item_link is empty", default_feed.item_link("x") == "")
check("default item_pubdate is None", default_feed.item_pubdate("x") is None)
check("default item_author_name is None", default_feed.item_author_name("x") is None)
check("default item_categories is None", default_feed.item_categories("x") is None)
check("default item_guid is None", default_feed.item_guid("x") is None)
check(
    "default item_guid_is_permalink is True",
    default_feed.item_guid_is_permalink("x") is True,
)

# --- atom:link self from request.path when feed_url is empty ---

print("\n-- atom:link self from request.path --")


@dataclass
class NoFeedUrlFeed(Feed):
    language: str = "en"
    feed_type: str = "rss"

    def title(self) -> str:
        return "Test"

    def link(self) -> str:
        return "https://example.com/"

    def description(self) -> str:
        return "Test"

    def items(self) -> list[object]:
        return []


req_path = _make_request("/my/custom/feed/")
no_url_rss = _render_rss(NoFeedUrlFeed(), req_path).decode("utf-8")
check(
    "atom:link self from request path",
    'href="/my/custom/feed/"' in no_url_rss,
    f"got: {no_url_rss}",
)


# =========================================================================
# Summary
# =========================================================================
print("\n" + "=" * 60)
total = passed + failed
print(f"RESULTS: {passed}/{total} passed, {failed} failed")
if errors:
    print(f"FAILED: {', '.join(errors)}")
print("=" * 60)
sys.exit(1 if failed else 0)
