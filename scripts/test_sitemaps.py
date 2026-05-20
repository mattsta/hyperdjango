#!/usr/bin/env python3
"""
Tests for the sitemaps module.

Unit tests only -- no database needed. Tests XML rendering, pagination,
escaping, caching headers, and the sitemap view.

Usage:
    uv run hyper-test sitemaps
"""

# hyper-test: unit

import asyncio
import hashlib
import inspect
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, date, datetime

from hyperdjango.sitemaps import (
    VALID_CHANGEFREQS,
    GenericSitemap,
    SimplePaginator,
    Sitemap,
    _format_lastmod,
    _render_sitemap_index_xml,
    _render_sitemap_xml,
    sitemap_view,
    xml_escape,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS: dict[str, int | list[tuple[str, str]]] = {
    "passed": 0,
    "failed": 0,
    "errors": [],
}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Mock objects
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MockItem:
    url: str
    updated: datetime | None = None
    freq: str | None = None
    prio: float | None = None

    def get_absolute_url(self) -> str:
        return self.url


@dataclass(slots=True)
class MockRequest:
    headers: dict[str, str]


# ---------------------------------------------------------------------------
# Custom sitemaps for testing
# ---------------------------------------------------------------------------


class ArticleSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.8

    def __init__(self, items_list: list[MockItem]):
        self._items = items_list

    def items(self) -> list[MockItem]:
        return self._items

    def lastmod(self, item: MockItem) -> datetime | None:
        return item.updated


class EmptySitemap(Sitemap):
    def items(self) -> list[MockItem]:
        return []


class CallableFreqPrioritySitemap(Sitemap):
    def __init__(self, items_list: list[MockItem]):
        self._items = items_list

    def items(self) -> list[MockItem]:
        return self._items

    def changefreq(self, item: MockItem) -> str | None:
        return item.freq

    def priority(self, item: MockItem) -> float | None:
        return item.prio


# ═══════════════════════════════════════════════════════════════════════════
# XML Escaping
# ═══════════════════════════════════════════════════════════════════════════


@test("xml_escape: ampersand")
def test_escape_amp():
    assert xml_escape("foo&bar") == "foo&amp;bar"


@test("xml_escape: angle brackets")
def test_escape_angles():
    assert xml_escape("<tag>") == "&lt;tag&gt;"


@test("xml_escape: double quotes")
def test_escape_quotes():
    assert xml_escape('a="b"') == "a=&quot;b&quot;"


@test("xml_escape: single quotes")
def test_escape_apos():
    assert xml_escape("it's") == "it&apos;s"


@test("xml_escape: combined special chars")
def test_escape_combined():
    result = xml_escape('a&b<c>d"e')
    assert "&amp;" in result
    assert "&lt;" in result
    assert "&gt;" in result
    assert "&quot;" in result


@test("xml_escape: no-op on clean string")
def test_escape_clean():
    assert xml_escape("hello-world/path") == "hello-world/path"


# ═══════════════════════════════════════════════════════════════════════════
# Date formatting
# ═══════════════════════════════════════════════════════════════════════════


@test("lastmod format: datetime with UTC timezone")
def test_format_datetime_utc():
    dt = datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC)
    result = _format_lastmod(dt)
    assert result == "2025-06-15T10:30:00+0000"


@test("lastmod format: naive datetime treated as UTC")
def test_format_naive_datetime():
    dt = datetime(2025, 1, 1, 0, 0, 0)
    result = _format_lastmod(dt)
    assert "+0000" in result
    assert "2025-01-01" in result


@test("lastmod format: plain date")
def test_format_date():
    d = date(2025, 3, 20)
    result = _format_lastmod(d)
    assert result == "2025-03-20"


# ═══════════════════════════════════════════════════════════════════════════
# SimplePaginator
# ═══════════════════════════════════════════════════════════════════════════


@test("SimplePaginator: single page")
def test_paginator_single():
    pag = SimplePaginator(object_list=list(range(10)), per_page=50)
    assert pag.num_pages == 1
    assert pag.count == 10
    page = pag.page(1)
    assert len(page.items) == 10


@test("SimplePaginator: multiple pages")
def test_paginator_multi():
    pag = SimplePaginator(object_list=list(range(25)), per_page=10)
    assert pag.num_pages == 3
    p1 = pag.page(1)
    assert len(p1.items) == 10
    p3 = pag.page(3)
    assert len(p3.items) == 5


@test("SimplePaginator: empty list")
def test_paginator_empty():
    pag = SimplePaginator(object_list=[], per_page=10)
    assert pag.num_pages == 1
    page = pag.page(1)
    assert len(page.items) == 0


@test("SimplePaginator: page out of range raises ValueError")
def test_paginator_out_of_range():
    pag = SimplePaginator(object_list=[1, 2, 3], per_page=10)
    try:
        pag.page(2)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


@test("SimplePaginator: page 0 raises ValueError")
def test_paginator_zero():
    pag = SimplePaginator(object_list=[1], per_page=10)
    try:
        pag.page(0)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Sitemap base class
# ═══════════════════════════════════════════════════════════════════════════


@test("Sitemap: default items is empty")
def test_sitemap_default_items():
    sm = Sitemap()
    assert sm.items() == []


@test("Sitemap: default limit is 50000")
def test_sitemap_default_limit():
    sm = Sitemap()
    assert sm.limit == 50000


@test("Sitemap: default protocol is https")
def test_sitemap_default_protocol():
    sm = Sitemap()
    assert sm.protocol == "https"


@test("Sitemap: custom items/location/lastmod")
def test_sitemap_custom():
    items = [
        MockItem(url="/page1/", updated=datetime(2025, 1, 1, tzinfo=UTC)),
        MockItem(url="/page2/", updated=datetime(2025, 6, 1, tzinfo=UTC)),
    ]
    sm = ArticleSitemap(items)
    assert sm.items() == items
    assert sm.location(items[0]) == "/page1/"
    assert sm.lastmod(items[0]) == items[0].updated


@test("Sitemap: get_changefreq returns class attr")
def test_sitemap_changefreq_attr():
    sm = ArticleSitemap([])
    assert sm.get_changefreq(None) == "daily"


@test("Sitemap: get_priority returns class attr")
def test_sitemap_priority_attr():
    sm = ArticleSitemap([])
    assert sm.get_priority(None) == 0.8


@test("Sitemap: callable changefreq and priority")
def test_sitemap_callable_freq_prio():
    item = MockItem(url="/x/", freq="weekly", prio=0.3)
    sm = CallableFreqPrioritySitemap([item])
    assert sm.get_changefreq(item) == "weekly"
    assert sm.get_priority(item) == 0.3


@test("Sitemap: paginator property returns SimplePaginator")
def test_sitemap_paginator():
    items = [MockItem(url=f"/{i}/") for i in range(10)]
    sm = ArticleSitemap(items)
    pag = sm.paginator
    assert isinstance(pag, SimplePaginator)
    assert pag.count == 10


@test("Sitemap: get_latest_lastmod returns max")
def test_sitemap_latest_lastmod():
    items = [
        MockItem(url="/a/", updated=datetime(2025, 1, 1, tzinfo=UTC)),
        MockItem(url="/b/", updated=datetime(2025, 6, 15, tzinfo=UTC)),
        MockItem(url="/c/", updated=datetime(2025, 3, 1, tzinfo=UTC)),
    ]
    sm = ArticleSitemap(items)
    latest = sm.get_latest_lastmod()
    assert latest == datetime(2025, 6, 15, tzinfo=UTC)


@test("Sitemap: get_latest_lastmod returns None for no items")
def test_sitemap_latest_lastmod_empty():
    sm = EmptySitemap()
    assert sm.get_latest_lastmod() is None


# ═══════════════════════════════════════════════════════════════════════════
# GenericSitemap
# ═══════════════════════════════════════════════════════════════════════════


@test("GenericSitemap: items returns queryset as list")
def test_generic_items():
    mock_qs = [MockItem(url="/1/"), MockItem(url="/2/")]
    sm = GenericSitemap(queryset=mock_qs)
    assert sm.items() == mock_qs


@test("GenericSitemap: lastmod with date_field")
def test_generic_lastmod():
    dt = datetime(2025, 5, 10, tzinfo=UTC)
    item = MockItem(url="/x/", updated=dt)
    sm = GenericSitemap(queryset=[item], date_field="updated")
    assert sm.lastmod(item) == dt


@test("GenericSitemap: lastmod without date_field returns None")
def test_generic_lastmod_none():
    item = MockItem(url="/x/")
    sm = GenericSitemap(queryset=[item])
    assert sm.lastmod(item) is None


@test("GenericSitemap: custom priority and changefreq")
def test_generic_custom_attrs():
    sm = GenericSitemap(queryset=[], priority=0.5, changefreq="monthly")
    assert sm.priority == 0.5
    assert sm.changefreq == "monthly"


@test("GenericSitemap: custom protocol")
def test_generic_protocol():
    sm = GenericSitemap(queryset=[], protocol="http")
    assert sm.protocol == "http"


# ═══════════════════════════════════════════════════════════════════════════
# XML rendering -- sitemap
# ═══════════════════════════════════════════════════════════════════════════


@test("render sitemap XML: root element")
def test_render_root():
    sm = EmptySitemap()
    xml = _render_sitemap_xml(sm, 1, "example.com")
    text = xml.decode("utf-8")
    assert '<?xml version="1.0" encoding="UTF-8"?>' in text
    assert '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' in text
    assert "</urlset>" in text


@test("render sitemap XML: url elements present")
def test_render_url_elements():
    items = [MockItem(url="/about/"), MockItem(url="/contact/")]
    sm = ArticleSitemap(items)
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<url>" in xml
    assert "<loc>https://example.com/about/</loc>" in xml
    assert "<loc>https://example.com/contact/</loc>" in xml


@test("render sitemap XML: lastmod included when present")
def test_render_lastmod():
    dt = datetime(2025, 3, 15, 12, 0, 0, tzinfo=UTC)
    items = [MockItem(url="/page/", updated=dt)]
    sm = ArticleSitemap(items)
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<lastmod>2025-03-15T12:00:00+0000</lastmod>" in xml


@test("render sitemap XML: changefreq and priority")
def test_render_freq_prio():
    items = [MockItem(url="/page/")]
    sm = ArticleSitemap(items)  # changefreq="daily", priority=0.8
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<changefreq>daily</changefreq>" in xml
    assert "<priority>0.8</priority>" in xml


@test("render sitemap XML: URL escaping in loc")
def test_render_url_escaping():
    items = [MockItem(url="/search?q=a&b=1")]
    sm = ArticleSitemap(items)
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "q=a&amp;b=1" in xml
    assert "q=a&b=1" not in xml  # raw ampersand must not appear


@test("render sitemap XML: empty sitemap has no url elements")
def test_render_empty():
    sm = EmptySitemap()
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<url>" not in xml
    assert "<urlset" in xml
    assert "</urlset>" in xml


@test("render sitemap XML: http protocol")
def test_render_http_protocol():
    items = [MockItem(url="/page/")]

    class HttpSitemap(Sitemap):
        protocol = "http"

        def items(self):
            return items

    sm = HttpSitemap()
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<loc>http://example.com/page/</loc>" in xml


@test("render sitemap XML: pagination at limit boundary")
def test_render_pagination_boundary():
    # 50001 items with limit=100 for test speed -> 501 pages
    items = [MockItem(url=f"/{i}/") for i in range(250)]

    class SmallLimitSitemap(Sitemap):
        limit = 100

        def items(self):
            return items

    sm = SmallLimitSitemap()
    pag = sm.paginator
    assert pag.num_pages == 3

    xml_p1 = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert xml_p1.count("<url>") == 100

    xml_p3 = _render_sitemap_xml(sm, 3, "example.com").decode("utf-8")
    assert xml_p3.count("<url>") == 50


@test("render sitemap XML: large pagination 50001 items")
def test_render_50001_items():
    # Verify 50001 items -> 2 pages at default limit
    items = [MockItem(url=f"/{i}/") for i in range(50001)]

    class LargeSitemap(Sitemap):
        def items(self):
            return items

    sm = LargeSitemap()
    pag = sm.paginator
    assert pag.num_pages == 2
    p1 = pag.page(1)
    p2 = pag.page(2)
    assert len(p1.items) == 50000
    assert len(p2.items) == 1


# ═══════════════════════════════════════════════════════════════════════════
# XML rendering -- sitemap index
# ═══════════════════════════════════════════════════════════════════════════


@test("render sitemap index: root element")
def test_index_root():
    sitemaps = {"articles": EmptySitemap()}
    xml = _render_sitemap_index_xml(sitemaps, "example.com").decode("utf-8")
    assert '<?xml version="1.0" encoding="UTF-8"?>' in xml
    assert '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' in xml
    assert "</sitemapindex>" in xml


@test("render sitemap index: multiple sections")
def test_index_sections():
    sitemaps = {
        "articles": ArticleSitemap([MockItem(url="/a/")]),
        "pages": ArticleSitemap([MockItem(url="/p/")]),
    }
    xml = _render_sitemap_index_xml(sitemaps, "example.com").decode("utf-8")
    assert "/sitemap-articles.xml" in xml
    assert "/sitemap-pages.xml" in xml
    assert xml.count("<sitemap>") == 2


@test("render sitemap index: lastmod from section")
def test_index_lastmod():
    dt = datetime(2025, 6, 1, tzinfo=UTC)
    sitemaps = {
        "news": ArticleSitemap([MockItem(url="/n/", updated=dt)]),
    }
    xml = _render_sitemap_index_xml(sitemaps, "example.com").decode("utf-8")
    assert "<lastmod>" in xml
    assert "2025-06-01" in xml


@test("render sitemap index: paginated section shows multiple entries")
def test_index_paginated():
    items = [MockItem(url=f"/{i}/") for i in range(250)]

    class SmallLimitSitemap(Sitemap):
        limit = 100

        def items(self):
            return items

    sitemaps = {"stuff": SmallLimitSitemap()}
    xml = _render_sitemap_index_xml(sitemaps, "example.com").decode("utf-8")
    # 250 items / 100 per page = 3 sitemap entries
    assert xml.count("<sitemap>") == 3
    assert "?p=1" in xml
    assert "?p=2" in xml
    assert "?p=3" in xml


# ═══════════════════════════════════════════════════════════════════════════
# sitemap_view
# ═══════════════════════════════════════════════════════════════════════════


@test("sitemap_view: returns XML content type")
def test_view_content_type():
    req = MockRequest(headers={"host": "example.com"})
    sitemaps = {"pages": ArticleSitemap([MockItem(url="/p/")])}
    resp = sitemap_view(req, sitemaps)
    assert resp.headers["content-type"] == "application/xml; charset=utf-8"


@test("sitemap_view: returns index when no section")
def test_view_index():
    req = MockRequest(headers={"host": "example.com"})
    sitemaps = {"pages": ArticleSitemap([MockItem(url="/p/")])}
    resp = sitemap_view(req, sitemaps)
    body = resp.body.decode("utf-8")
    assert "<sitemapindex" in body


@test("sitemap_view: returns section sitemap")
def test_view_section():
    req = MockRequest(headers={"host": "example.com"})
    sitemaps = {"pages": ArticleSitemap([MockItem(url="/p/")])}
    resp = sitemap_view(req, sitemaps, section="pages")
    body = resp.body.decode("utf-8")
    assert "<urlset" in body
    assert "<loc>https://example.com/p/</loc>" in body


@test("sitemap_view: 404 for unknown section")
def test_view_unknown_section():
    req = MockRequest(headers={"host": "example.com"})
    resp = sitemap_view(req, {}, section="nope")
    assert resp.status == 404


@test("sitemap_view: ETag header present")
def test_view_etag():
    req = MockRequest(headers={"host": "example.com"})
    sitemaps = {"pages": ArticleSitemap([MockItem(url="/p/")])}
    resp = sitemap_view(req, sitemaps)
    assert "etag" in resp.headers
    assert resp.headers["etag"].startswith('"')
    assert resp.headers["etag"].endswith('"')


@test("sitemap_view: ETag is md5 of body")
def test_view_etag_value():
    req = MockRequest(headers={"host": "example.com"})
    sitemaps = {"pages": ArticleSitemap([MockItem(url="/p/")])}
    resp = sitemap_view(req, sitemaps)
    expected_etag = '"' + hashlib.md5(resp.body).hexdigest() + '"'
    assert resp.headers["etag"] == expected_etag


@test("sitemap_view: Cache-Control header present")
def test_view_cache_control():
    req = MockRequest(headers={"host": "example.com"})
    sitemaps = {"pages": ArticleSitemap([MockItem(url="/p/")])}
    resp = sitemap_view(req, sitemaps)
    cc = resp.headers.get("cache-control", "")
    assert "public" in cc
    assert "max-age=3600" in cc


@test("sitemap_view: section with page param")
def test_view_section_page():
    items = [MockItem(url=f"/{i}/") for i in range(250)]

    class SmallLimitSitemap(Sitemap):
        limit = 100

        def items(self):
            return items

    req = MockRequest(headers={"host": "example.com"})
    sitemaps = {"stuff": SmallLimitSitemap()}
    resp = sitemap_view(req, sitemaps, section="stuff", page=2)
    body = resp.body.decode("utf-8")
    assert "<urlset" in body
    assert body.count("<url>") == 100


# ═══════════════════════════════════════════════════════════════════════════
# Changefreq validation
# ═══════════════════════════════════════════════════════════════════════════


@test("VALID_CHANGEFREQS contains all 7 values")
def test_valid_changefreqs():
    expected = {"always", "hourly", "daily", "weekly", "monthly", "yearly", "never"}
    assert expected == VALID_CHANGEFREQS


# ═══════════════════════════════════════════════════════════════════════════
# Priority range
# ═══════════════════════════════════════════════════════════════════════════


@test("priority 0.0 renders as 0.0")
def test_priority_zero():
    items = [MockItem(url="/z/")]

    class ZeroPriSitemap(Sitemap):
        priority = 0.0

        def items(self):
            return items

    sm = ZeroPriSitemap()
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<priority>0.0</priority>" in xml


@test("priority 1.0 renders as 1.0")
def test_priority_one():
    items = [MockItem(url="/z/")]

    class OnePriSitemap(Sitemap):
        priority = 1.0

        def items(self):
            return items

    sm = OnePriSitemap()
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<priority>1.0</priority>" in xml


@test("priority None omits element")
def test_priority_none():
    items = [MockItem(url="/z/")]

    class NoPriSitemap(Sitemap):
        priority = None

        def items(self):
            return items

    sm = NoPriSitemap()
    xml = _render_sitemap_xml(sm, 1, "example.com").decode("utf-8")
    assert "<priority>" not in xml


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════


@test("host extraction from request headers")
def test_host_extraction():
    req = MockRequest(headers={"host": "mysite.org"})
    sitemaps = {"p": ArticleSitemap([MockItem(url="/x/")])}
    resp = sitemap_view(req, sitemaps, section="p")
    body = resp.body.decode("utf-8")
    assert "mysite.org" in body


@test("host defaults to localhost when missing")
def test_host_default():
    req = MockRequest(headers={})
    sitemaps = {"p": ArticleSitemap([MockItem(url="/x/")])}
    resp = sitemap_view(req, sitemaps, section="p")
    body = resp.body.decode("utf-8")
    assert "localhost" in body


@test("XML output is valid UTF-8 bytes")
def test_xml_is_bytes():
    sm = ArticleSitemap([MockItem(url="/ok/")])
    xml = _render_sitemap_xml(sm, 1, "example.com")
    assert isinstance(xml, bytes)
    # Should decode without error
    xml.decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            tests.append(obj)

    print("\n\u2550\u2550\u2550 Unit Tests: Sitemaps \u2550\u2550\u2550")
    for t in tests:
        await t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
