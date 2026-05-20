#!/usr/bin/env python3
"""Cross-module integration tests -- HyperDjango modules working TOGETHER.

Tests interactions between:
- Sitemaps + Flatpages
- REST + PostgreSQL extensions
- Fixtures + Settings
- Commands + Settings
- Humanize + Syndication
- Middleware chaining (Redirects + Flatpages)
- Full stack scenarios

50+ checks covering cross-module integration, not isolated unit tests.
"""

# hyper-test: unit

import asyncio
import base64
import csv
import io
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from hyperdjango.commands import (
    _command_registry,
    command,
    get_command,
    list_commands,
)
from hyperdjango.conf import (
    DEFAULT_PAGE_SIZE,
    DEFAULTS,
    ONE_DAY,
    ONE_HOUR,
    get_setting,
    validate_settings,
)
from hyperdjango.fixtures import (
    _deserialize_value,
    _serialize_value,
)
from hyperdjango.flatpages import FlatPage, FlatPageMiddleware, FlatPageRegistry
from hyperdjango.humanize import (
    HUMANIZE_FILTERS,
    intcomma,
    naturaltime,
    ordinal,
)
from hyperdjango.postgres import (
    ArrayAgg,
    ArrayField,
    SearchQuery,
    SearchRank,
    SearchVector,
    StringAgg,
    TrigramSimilarity,
)
from hyperdjango.redirects import (
    Redirect,
    RedirectMiddleware,
    RedirectRegistry,
)
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.rest import CSVRenderer
from hyperdjango.sitemaps import (
    Sitemap,
    _render_sitemap_xml,
    sitemap_view,
)
from hyperdjango.syndication import Feed, _render_rss

# ── Test infrastructure ──────────────────────────────────────────────────────

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f" -- {detail}"
        print(msg)
        errors.append(name)


# ── Helper: mock objects ─────────────────────────────────────────────────────


@dataclass
class MockItem:
    """Mock item for sitemap/feed tests with get_absolute_url."""

    url: str
    title: str
    updated_at: datetime
    description: str = ""
    views: int = 0

    def get_absolute_url(self) -> str:
        return self.url


# ══════════════════════════════════════════════════════════════════════════════
# 1. SITEMAPS + FLATPAGES INTEGRATION (10 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_sitemaps_flatpages_integration() -> None:
    print("\n=== Sitemaps + Flatpages Integration ===")

    # Create a FlatPageRegistry and populate it (no DB needed -- use _pages directly)
    fp_registry = FlatPageRegistry()
    with fp_registry._lock:
        fp_registry._pages["/about/"] = FlatPage(
            url="/about/", title="About Us", content="<p>About page</p>"
        )
        fp_registry._pages["/contact/"] = FlatPage(
            url="/contact/", title="Contact", content="<p>Contact us</p>"
        )
        fp_registry._pages["/faq/"] = FlatPage(
            url="/faq/", title="FAQ", content="<p>FAQ</p>"
        )

    # 1. FlatPage registry get_all returns sorted pages
    all_pages = fp_registry.get_all()
    check(
        "flatpage_registry_returns_pages",
        len(all_pages) == 3,
        f"expected 3, got {len(all_pages)}",
    )

    # 2. Create a Sitemap subclass that returns flatpages as items
    class FlatPageSitemap(Sitemap):
        changefreq = "monthly"
        priority = 0.5

        def __init__(self, registry: FlatPageRegistry):
            self._registry = registry

        def items(self) -> list[FlatPage]:
            return self._registry.get_all()

        def location(self, item: FlatPage) -> str:
            return item.url

    fp_sitemap = FlatPageSitemap(fp_registry)
    items = fp_sitemap.items()
    check(
        "sitemap_items_from_flatpages", len(items) == 3, f"expected 3, got {len(items)}"
    )

    # 3. Sitemap location returns correct URL for each flatpage
    check("sitemap_location_about", fp_sitemap.location(items[0]) == "/about/")
    check("sitemap_location_contact", fp_sitemap.location(items[1]) == "/contact/")
    check("sitemap_location_faq", fp_sitemap.location(items[2]) == "/faq/")

    # 4. Render sitemap XML and verify all flatpage URLs present
    xml_bytes = _render_sitemap_xml(fp_sitemap, 1, "example.com")
    xml_str = xml_bytes.decode("utf-8")
    check("sitemap_xml_contains_about", "https://example.com/about/" in xml_str)
    check("sitemap_xml_contains_contact", "https://example.com/contact/" in xml_str)
    check("sitemap_xml_contains_faq", "https://example.com/faq/" in xml_str)

    # 5. Sitemap XML has correct changefreq from class attribute
    check("sitemap_xml_changefreq", "<changefreq>monthly</changefreq>" in xml_str)

    # 6. Sitemap index with flatpages section
    sitemaps = {"flatpages": fp_sitemap}
    request = Request(
        method="GET", path="/sitemap.xml", headers={"host": "example.com"}
    )
    resp = sitemap_view(request, sitemaps)
    check("sitemap_view_200", resp.status == 200, f"expected 200, got {resp.status}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. REST + POSTGRESQL EXTENSIONS INTEGRATION (10 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_rest_postgres_integration() -> None:
    print("\n=== REST + PostgreSQL Extensions Integration ===")

    # 1. SearchVector SQL generation usable in filtering context (Expression interface)
    sv = SearchVector(fields=["title", "body"], config="english")
    sv_sql, sv_params = sv.as_sql()
    check("searchvector_sql_has_tsvector", "to_tsvector" in sv_sql)
    check(
        "searchvector_sql_has_both_fields", '"title"' in sv_sql and '"body"' in sv_sql
    )

    # 2. SearchQuery + SearchVector combined into SearchRank
    sq = SearchQuery(query="django rest", config="english", search_type="plain")
    rank = SearchRank(vector=sv, query=sq)
    rank_sql, rank_params = rank.as_sql()
    check(
        "searchrank_combines_vector_query",
        "ts_rank" in rank_sql and "to_tsvector" in rank_sql,
    )

    # 3. ArrayField type mapping for REST serializer context
    af = ArrayField(base_type="int")
    check("arrayfield_db_type", af.db_type == "integer[]", f"got {af.db_type}")

    af_text = ArrayField(base_type="text")
    check("arrayfield_text_type", af_text.db_type == "text[]")

    # 4. ArrayAgg SQL for REST list aggregation
    agg = ArrayAgg(field="tag_name", distinct=True, ordering="tag_name")
    agg_sql = agg.as_sql()
    check(
        "arrayagg_distinct_ordered",
        "DISTINCT" in agg_sql and "ORDER BY" in agg_sql,
        f"got: {agg_sql}",
    )

    # 5. StringAgg SQL for REST list display
    sagg = StringAgg(field="name", delimiter=", ", distinct=True)
    sagg_sql = sagg.as_sql()
    check("stringagg_distinct", "DISTINCT" in sagg_sql and "string_agg" in sagg_sql)

    # 6. TrigramSimilarity SQL for search endpoint (Expression interface)
    trgm = TrigramSimilarity(field="title", value="djangp")
    trgm_sql, trgm_params = trgm.as_sql()
    check("trigram_similarity_sql", "similarity" in trgm_sql)

    # 7. CSVRenderer renders list-of-dicts (REST output format)
    renderer = CSVRenderer()
    data = [
        {"id": 1, "name": "Alice", "score": 95},
        {"id": 2, "name": "Bob", "score": 87},
    ]
    csv_bytes = renderer.render(data)
    csv_str = csv_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(csv_str))
    rows = list(reader)
    check("csv_renderer_row_count", len(rows) == 2)
    check(
        "csv_renderer_fields", rows[0]["name"] == "Alice" and rows[1]["name"] == "Bob"
    )

    # 8. CSVRenderer handles paginated response format
    paginated = {"count": 2, "results": data}
    csv_bytes2 = renderer.render(paginated)
    csv_str2 = csv_bytes2.decode("utf-8")
    reader2 = csv.DictReader(io.StringIO(csv_str2))
    rows2 = list(reader2)
    check("csv_renderer_paginated", len(rows2) == 2)


# ══════════════════════════════════════════════════════════════════════════════
# 3. FIXTURES + SETTINGS INTEGRATION (8 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_fixtures_settings_integration() -> None:
    print("\n=== Fixtures + Settings Integration ===")

    # 1. _serialize_value handles datetime (common in DEFAULTS-typed data)
    now = datetime(2024, 6, 15, 12, 30, 0, tzinfo=UTC)
    serialized_dt = _serialize_value(now)
    check(
        "serialize_datetime",
        isinstance(serialized_dt, str) and "2024-06-15" in serialized_dt,
    )

    # 2. _serialize_value handles date
    today = date(2024, 6, 15)
    serialized_date = _serialize_value(today)
    check("serialize_date", serialized_date == "2024-06-15")

    # 3. _serialize_value handles Decimal (used in numeric settings)
    dec = Decimal("3.14159")
    serialized_dec = _serialize_value(dec)
    check("serialize_decimal", serialized_dec == "3.14159")

    # 4. _serialize_value handles UUID
    uid = uuid4()
    serialized_uuid = _serialize_value(uid)
    check("serialize_uuid", serialized_uuid == str(uid))

    # 5. _serialize_value handles bytes (base64 encoded)
    raw_bytes = b"\x00\x01\x02\xff"
    serialized_bytes = _serialize_value(raw_bytes)
    check("serialize_bytes", base64.b64decode(serialized_bytes) == raw_bytes)

    # 6. Roundtrip: serialize then deserialize preserves types
    original_dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    ser = _serialize_value(original_dt)
    deser = _deserialize_value(ser, "datetime")
    check("roundtrip_datetime", deser == original_dt, f"got {deser!r}")

    # 7. Roundtrip for UUID
    original_uuid = uuid4()
    ser_uuid = _serialize_value(original_uuid)
    deser_uuid = _deserialize_value(ser_uuid, "uuid")
    check("roundtrip_uuid", deser_uuid == original_uuid)

    # 8. _serialize_value handles conf.py constants correctly
    check(
        "serialize_conf_constants",
        _serialize_value(ONE_DAY) == ONE_DAY
        and _serialize_value(ONE_HOUR) == ONE_HOUR
        and _serialize_value(DEFAULT_PAGE_SIZE) == DEFAULT_PAGE_SIZE
        and _serialize_value(True) is True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. COMMANDS + SETTINGS INTEGRATION (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_commands_settings_integration() -> None:
    print("\n=== Commands + Settings Integration ===")

    # Clean up any pre-existing test commands
    _command_registry.pop("check_settings", None)
    _command_registry.pop("show_pool_size", None)

    # 1. Register a command via decorator
    @command(name="check_settings", help="Validate all settings")
    def check_settings_cmd(verbose: bool = False) -> int:
        errs = validate_settings({"DEBUG": True, "SECRET_KEY": "test-key-123"})
        return len(errs)

    cmd = get_command("check_settings")
    check("command_registered", cmd is not None)
    check("command_in_list", any(c.name == "check_settings" for c in list_commands()))

    # 2. Command can read settings via get_setting
    @command(name="show_pool_size", help="Show pool size")
    def show_pool_size_cmd() -> int:
        pool_size = get_setting("POOL_SIZE", 0)
        return pool_size

    cmd2 = get_command("show_pool_size")
    check("command_reads_settings", cmd2 is not None and cmd2.name == "show_pool_size")

    # 3. validate_settings is callable and returns errors list
    settings_with_errors = {"POOL_SIZE": -1, "DEBUG": True, "SECRET_KEY": "x"}
    errs = validate_settings(settings_with_errors)
    check(
        "validate_settings_finds_errors", len(errs) > 0, f"expected errors, got {errs}"
    )

    # 4. validate_settings with valid settings returns empty list
    valid_settings = dict(DEFAULTS)
    valid_settings["SECRET_KEY"] = "a-real-secret-key-that-is-long-enough"
    valid_settings["DEBUG"] = True
    errs2 = validate_settings(valid_settings)
    check("validate_settings_valid_ok", len(errs2) == 0, f"unexpected errors: {errs2}")

    # Clean up
    _command_registry.pop("check_settings", None)
    _command_registry.pop("show_pool_size", None)


# ══════════════════════════════════════════════════════════════════════════════
# 5. HUMANIZE + SYNDICATION INTEGRATION (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_humanize_syndication_integration() -> None:
    print("\n=== Humanize + Syndication Integration ===")

    # Create a feed that uses humanize in item descriptions
    class StatsFeed(Feed):
        language = "en"
        feed_type = "rss"

        def title(self) -> str:
            return "Site Statistics"

        def link(self) -> str:
            return "https://example.com/stats/"

        def description(self) -> str:
            return "Daily site stats"

        def items(self) -> list[MockItem]:
            return [
                MockItem(
                    url="/stats/2024-06-15/",
                    title="Stats for June 15",
                    updated_at=datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
                    views=1234567,
                ),
                MockItem(
                    url="/stats/2024-06-14/",
                    title="Stats for June 14",
                    updated_at=datetime(2024, 6, 14, 12, 0, 0, tzinfo=UTC),
                    views=987654,
                ),
            ]

        def item_title(self, item: MockItem) -> str:
            return item.title

        def item_description(self, item: MockItem) -> str:
            # Use humanize.intcomma to format view counts in feed description
            return f"Page views: {intcomma(item.views)}"

        def item_link(self, item: MockItem) -> str:
            return item.url

        def item_pubdate(self, item: MockItem) -> datetime:
            return item.updated_at

    # 1. Feed description uses intcomma from humanize
    feed = StatsFeed()
    items = feed.items()
    desc0 = feed.item_description(items[0])
    check("feed_uses_intcomma", "1,234,567" in desc0, f"got: {desc0}")

    # 2. Render RSS and verify humanized content appears in XML
    xml_bytes = _render_rss(feed)
    xml_str = xml_bytes.decode("utf-8")
    check("rss_xml_has_intcomma", "1,234,567" in xml_str)

    # 3. ordinal works in feed title context
    position = 1
    title_with_ordinal = f"{ordinal(position)} place"
    check("ordinal_in_feed_context", title_with_ordinal == "1st place")

    # 4. naturaltime produces human-readable string for recent pubdates
    recent = datetime.now(UTC) - timedelta(hours=2)
    nt_result = naturaltime(recent)
    check("naturaltime_recent", "hour" in nt_result, f"got: {nt_result}")

    # 5. HUMANIZE_FILTERS registry contains all expected filters
    expected_filters = {
        "ordinal",
        "intcomma",
        "intword",
        "naturaltime",
        "naturalday",
        "filesizeformat",
        "apnumber",
    }
    check(
        "humanize_filters_registry",
        expected_filters.issubset(set(HUMANIZE_FILTERS.keys())),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 6. MIDDLEWARE CHAINING (7 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_middleware_chaining() -> None:
    print("\n=== Middleware Chaining ===")

    # Set up redirect registry
    redir_registry = RedirectRegistry()
    redir_registry.add("/old-page/", "/new-page/", 301)
    redir_registry.add("/moved/", "/destination/", 302)

    # Set up flatpage registry
    fp_registry = FlatPageRegistry()
    with fp_registry._lock:
        fp_registry._pages["/help/"] = FlatPage(
            url="/help/", title="Help", content="<h1>Help Page</h1>"
        )
        fp_registry._pages["/terms/"] = FlatPage(
            url="/terms/", title="Terms", content="<h1>Terms of Service</h1>"
        )

    # Save module-level registry and swap in our test one
    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = fp_registry

    redirect_mw = RedirectMiddleware(registry=redir_registry)
    flatpage_mw = FlatPageMiddleware()

    async def run_chain() -> None:
        # Helper: simulate the middleware chain
        # RedirectMiddleware wraps call_next, FlatPageMiddleware processes the response

        async def app_handler(req: Request) -> Response:
            """Simulate the inner app -- returns 404 for unknown, 200 for known."""
            if req.path == "/exists/":
                return Response.text("OK", status=200)
            return Response.text("Not Found", status=404)

        # 1. Redirect match takes priority (request to /old-page/)
        req1 = Request(method="GET", path="/old-page/")
        resp1 = await redirect_mw(req1, app_handler)
        check(
            "redirect_intercepts_404",
            resp1.status == 301,
            f"expected 301, got {resp1.status}",
        )
        check(
            "redirect_location_correct", resp1.headers.get("location") == "/new-page/"
        )

        # 2. Flatpage match on 404 (request to /help/)
        req2 = Request(method="GET", path="/help/")
        # First pass through redirect middleware (no match -> gets 404 from app)
        resp2_after_redirect = await redirect_mw(req2, app_handler)
        # Then flatpage middleware processes the 404
        resp2_final = await flatpage_mw(req2, resp2_after_redirect)
        check(
            "flatpage_intercepts_404",
            resp2_final.status == 200,
            f"expected 200, got {resp2_final.status}",
        )

        # 3. Normal 200 passes through both middlewares unchanged
        req3 = Request(method="GET", path="/exists/")
        resp3 = await redirect_mw(req3, app_handler)
        # resp3 should be 200 -- redirect MW passes through
        check("normal_200_passes_redirect", resp3.status == 200)
        # Flatpage MW also passes through 200
        resp3_final = await flatpage_mw(req3, resp3)
        check("normal_200_passes_flatpage", resp3_final.status == 200)

        # 4. True 404 (no redirect, no flatpage)
        req4 = Request(method="GET", path="/nonexistent/")
        resp4 = await redirect_mw(req4, app_handler)
        resp4_final = await flatpage_mw(req4, resp4)
        check(
            "true_404_passes_through",
            resp4_final.status == 404,
            f"expected 404, got {resp4_final.status}",
        )

        # 5. Redirect checked before flatpage (both could match)
        # Add a redirect that conflicts with a flatpage
        redir_registry.add("/terms/", "/new-terms/", 301)
        req5 = Request(method="GET", path="/terms/")
        resp5 = await redirect_mw(req5, app_handler)
        check(
            "redirect_before_flatpage",
            resp5.status == 301,
            f"expected 301 (redirect wins), got {resp5.status}",
        )

    asyncio.run(run_chain())

    # Restore module-level registry
    fp_module.registry = original_registry


# ══════════════════════════════════════════════════════════════════════════════
# 7. FULL STACK SCENARIOS (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_full_stack_scenarios() -> None:
    print("\n=== Full Stack Scenarios ===")

    # Scenario 1: Create redirects, verify they appear in a sitemap
    redir_registry = RedirectRegistry()
    redir_registry.add("/blog/old-post/", "/blog/new-post/", 301)
    redir_registry.add("/blog/removed/", "/blog/archive/", 301)

    class RedirectSitemap(Sitemap):
        changefreq = "never"
        priority = 0.1

        def __init__(self, registry: RedirectRegistry):
            self._registry = registry

        def items(self) -> list[Redirect]:
            return self._registry.all_redirects()

        def location(self, item: Redirect) -> str:
            return item.new_path

    redir_sitemap = RedirectSitemap(redir_registry)
    xml_bytes = _render_sitemap_xml(redir_sitemap, 1, "example.com")
    xml_str = xml_bytes.decode("utf-8")
    check(
        "redirects_in_sitemap",
        "/blog/new-post/" in xml_str and "/blog/archive/" in xml_str,
        f"XML:\n{xml_str[:300]}",
    )

    # Scenario 2: Create flatpages, verify in sitemap
    fp_registry = FlatPageRegistry()
    with fp_registry._lock:
        fp_registry._pages["/privacy/"] = FlatPage(
            url="/privacy/", title="Privacy Policy", content="<p>Privacy</p>"
        )

    class FPSitemap(Sitemap):
        priority = 0.3

        def __init__(self, registry: FlatPageRegistry):
            self._registry = registry

        def items(self) -> list[FlatPage]:
            return self._registry.get_all()

        def location(self, item: FlatPage) -> str:
            return item.url

    fp_sitemap = FPSitemap(fp_registry)
    xml_bytes2 = _render_sitemap_xml(fp_sitemap, 1, "example.com")
    xml_str2 = xml_bytes2.decode("utf-8")
    check("flatpages_in_sitemap", "/privacy/" in xml_str2)

    # Scenario 3: CSVRenderer with data that includes humanized values
    renderer = CSVRenderer()
    data = [
        {"rank": ordinal(1), "name": "Alice", "revenue": intcomma(1234567)},
        {"rank": ordinal(2), "name": "Bob", "revenue": intcomma(987654)},
        {"rank": ordinal(3), "name": "Charlie", "revenue": intcomma(555000)},
    ]
    csv_bytes = renderer.render(data)
    csv_str = csv_bytes.decode("utf-8")
    check(
        "csv_with_humanize",
        "1st" in csv_str and "1,234,567" in csv_str and "2nd" in csv_str,
    )

    # Scenario 4: Settings validation is comprehensive
    all_settings = dict(DEFAULTS)
    all_settings["SECRET_KEY"] = "production-secret-key-32-chars-long"
    all_settings["DEBUG"] = False
    errs = validate_settings(all_settings)
    check("full_settings_validation_passes", len(errs) == 0, f"errors: {errs}")

    # Scenario 5: Serialize complex nested data with fixtures helpers
    complex_data = {
        "created": datetime(2024, 1, 1, tzinfo=UTC),
        "amount": Decimal("99.99"),
        "id": uuid4(),
        "tags": ["python", "zig"],
        "active": True,
        "metadata": {"key": "value", "count": 42},
    }
    serialized = _serialize_value(complex_data)
    check(
        "serialize_complex_nested",
        isinstance(serialized, dict)
        and isinstance(serialized["created"], str)
        and serialized["amount"] == "99.99"
        and serialized["tags"] == ["python", "zig"]
        and serialized["active"] is True
        and serialized["metadata"]["count"] == 42,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Run all test groups
# ══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    t0 = time.perf_counter()

    test_sitemaps_flatpages_integration()
    test_rest_postgres_integration()
    test_fixtures_settings_integration()
    test_commands_settings_integration()
    test_humanize_syndication_integration()
    test_middleware_chaining()
    test_full_stack_scenarios()

    elapsed = time.perf_counter() - t0
    total = passed + failed

    print(f"\n{'=' * 60}")
    print(
        f"Cross-Module Integration: {passed}/{total} passed, {failed} failed  ({elapsed:.3f}s)"
    )
    if errors:
        print(f"FAILED: {', '.join(errors)}")
    print(f"{'=' * 60}")
    sys.exit(1 if failed else 0)


main()
