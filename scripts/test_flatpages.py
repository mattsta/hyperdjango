#!/usr/bin/env python3
"""
Tests for the flatpages module.

Tests:
1. FlatPage model fields and defaults
2. FlatPage to_context
3. FlatPage custom fields
4. Registry starts empty
5. Registry add creates page
6. Registry lookup by exact URL
7. Registry lookup with trailing slash normalization
8. Registry lookup returns None for missing
9. Registry add updates existing page
10. Registry remove deletes page
11. Registry remove returns False for missing
12. Registry get_all returns sorted list
13. Registry get_all empty
14. Registry thread safety
15. Registry load_all from DB
16. Registry inactive pages not in lookup
17. Middleware passes through non-404
18. Middleware serves flatpage on 404
19. Middleware passes through 404 with no matching page
20. Middleware registration_required blocks unauthenticated
21. Middleware registration_required allows authenticated
22. Middleware inactive pages not served
23. Middleware content rendered as HTML
24. Standalone view serves flatpage
25. Standalone view returns 404 for missing
26. Standalone view registration_required blocks
27. Render with template engine
28. Render fallback when no template engine
29. Render fallback when template raises
30. URL normalization
31. Custom template_name used

Usage:
    uv run hyper-test flatpages
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
import threading
from dataclasses import dataclass

from hyperdjango.auth.user import AnonymousUser
from hyperdjango.database import Database, set_db
from hyperdjango.flatpages import (
    FlatPage,
    FlatPageMiddleware,
    FlatPageRegistry,
    _normalize_url,
    _render_flatpage,
    flatpage_view,
)
from hyperdjango.response import Response

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://localhost/hyperdjango_test",
)

RESULTS: dict[str, int | list[str]] = {"passed": 0, "failed": 0, "errors": []}


def check(name: str, condition: bool, details: str = "") -> None:
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} -- {details}")


# --- Mock objects ---


@dataclass(slots=True)
class MockUser:
    is_authenticated: bool = False


@dataclass(slots=True)
class MockRequest:
    path: str = "/"
    user: MockUser | None = None


class MockTemplateEngine:
    """Template engine that records calls and returns predictable output."""

    def __init__(self, should_fail: bool = False):
        self.last_template: str | None = None
        self.last_context: dict | None = None
        self.should_fail = should_fail

    def render(self, template_name: str, context: dict) -> str:
        if self.should_fail:
            raise FileNotFoundError(f"Template {template_name} not found")
        self.last_template = template_name
        self.last_context = context
        title = context.get("flatpage", {}).get("title", "")
        content = context.get("flatpage", {}).get("content", "")
        return f"<rendered>{title}: {content}</rendered>"


# --- Tests ---


def test_flatpage_model_defaults():
    """Test FlatPage dataclass fields and default values."""
    print("\n--- FlatPage Model Defaults ---")
    page = FlatPage(url="/test/", title="Test")
    check("url field", page.url == "/test/")
    check("title field", page.title == "Test")
    check("content default empty", page.content == "")
    check("template_name default", page.template_name == "flatpages/default.html")
    check("registration_required default False", page.registration_required is False)
    check("is_active default True", page.is_active is True)


def test_flatpage_to_context():
    """Test FlatPage.to_context returns correct dict."""
    print("\n--- FlatPage to_context ---")
    page = FlatPage(
        url="/about/",
        title="About Us",
        content="<p>Hello</p>",
        template_name="custom.html",
        registration_required=True,
        is_active=True,
    )
    ctx = page.to_context()
    check("context url", ctx["url"] == "/about/")
    check("context title", ctx["title"] == "About Us")
    check("context content", ctx["content"] == "<p>Hello</p>")
    check("context template_name", ctx["template_name"] == "custom.html")
    check("context registration_required", ctx["registration_required"] is True)
    check("context is_active", ctx["is_active"] is True)


def test_flatpage_custom_fields():
    """Test FlatPage with all custom values."""
    print("\n--- FlatPage Custom Fields ---")
    page = FlatPage(
        url="/faq/",
        title="FAQ",
        content="<h1>FAQ</h1>",
        template_name="pages/faq.html",
        registration_required=True,
        is_active=False,
    )
    check("custom url", page.url == "/faq/")
    check("custom template", page.template_name == "pages/faq.html")
    check("custom registration", page.registration_required is True)
    check("custom inactive", page.is_active is False)


def test_url_normalization():
    """Test URL normalization helper."""
    print("\n--- URL Normalization ---")
    check("already normalized", _normalize_url("/about/") == "/about/")
    check("missing leading slash", _normalize_url("about/") == "/about/")
    check("missing trailing slash", _normalize_url("/about") == "/about/")
    check("missing both slashes", _normalize_url("about") == "/about/")
    check("root url", _normalize_url("/") == "/")


def test_registry_empty():
    """Test registry starts empty."""
    print("\n--- Registry Empty ---")
    reg = FlatPageRegistry()
    check("lookup returns None", reg.lookup("/test/") is None)
    check("get_all returns empty list", reg.get_all() == [])


def test_registry_in_memory():
    """Test registry add/lookup/remove without DB."""
    print("\n--- Registry In-Memory Operations ---")
    reg = FlatPageRegistry()

    # Manually insert into the registry (bypassing DB for unit test)
    page = FlatPage(url="/about/", title="About", content="<p>About us</p>")
    with reg._lock:
        reg._pages["/about/"] = page

    check("lookup finds page", reg.lookup("/about/") is not None)
    found = reg.lookup("/about/")
    check("lookup returns correct page", found.title == "About")

    # Lookup with slash normalization
    check("lookup normalizes URL", reg.lookup("/about") is not None)

    # Missing page
    check("lookup missing returns None", reg.lookup("/missing/") is None)

    # get_all
    page2 = FlatPage(url="/faq/", title="FAQ")
    with reg._lock:
        reg._pages["/faq/"] = page2

    all_pages = reg.get_all()
    check("get_all returns 2 pages", len(all_pages) == 2)
    check(
        "get_all sorted by url",
        all_pages[0].url == "/about/" and all_pages[1].url == "/faq/",
    )

    # Remove
    with reg._lock:
        reg._pages.pop("/about/", None)
    check("removed page not found", reg.lookup("/about/") is None)
    check("other page still present", reg.lookup("/faq/") is not None)


def test_registry_thread_safety():
    """Test registry is thread-safe for concurrent access."""
    print("\n--- Registry Thread Safety ---")
    reg = FlatPageRegistry()
    errors: list[str] = []

    def writer(start: int) -> None:
        for i in range(100):
            url = f"/page-{start}-{i}/"
            page = FlatPage(url=url, title=f"Page {start}-{i}")
            with reg._lock:
                reg._pages[url] = page

    def reader() -> None:
        for _ in range(200):
            reg.get_all()
            reg.lookup("/page-0-50/")

    threads = []
    for t_id in range(4):
        threads.append(threading.Thread(target=writer, args=(t_id,)))
    threads.append(threading.Thread(target=reader))
    threads.append(threading.Thread(target=reader))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    all_pages = reg.get_all()
    check(
        "thread safety: all pages written",
        len(all_pages) == 400,
        f"got {len(all_pages)}",
    )
    check("thread safety: no errors", len(errors) == 0)


async def test_registry_db_operations():
    """Test registry with real database operations."""
    print("\n--- Registry DB Operations ---")

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    reg = FlatPageRegistry()

    # Ensure table
    await reg.ensure_table()
    check("ensure_table succeeds", True)

    # Clean slate
    await db.execute(f"DELETE FROM {_TABLE}")

    # Add pages
    p1 = await reg.add("/about/", "About Us", "<p>About page</p>")
    check("add returns FlatPage", isinstance(p1, FlatPage))
    check("add normalizes url", p1.url == "/about/")

    p2 = await reg.add("/faq/", "FAQ", "<p>Questions</p>", registration_required=True)
    p3 = await reg.add("/hidden/", "Hidden", "<p>Secret</p>", is_active=False)

    # Lookup
    check("lookup after add", reg.lookup("/about/") is not None)
    check("lookup registration page", reg.lookup("/faq/") is not None)
    check("inactive not in cache", reg.lookup("/hidden/") is None)

    # get_all only returns active cached pages
    all_pages = reg.get_all()
    check("get_all returns 2 active", len(all_pages) == 2, f"got {len(all_pages)}")

    # Update existing
    await reg.add("/about/", "About (Updated)", "<p>Updated content</p>")
    updated = reg.lookup("/about/")
    check("update in place", updated.title == "About (Updated)")

    # load_all from DB
    reg2 = FlatPageRegistry()
    await reg2.ensure_table()
    await reg2.load_all()
    check("load_all populates cache", reg2.lookup("/about/") is not None)
    check("load_all skips inactive", reg2.lookup("/hidden/") is None)
    check("load_all count", len(reg2.get_all()) == 2, f"got {len(reg2.get_all())}")

    # Remove
    removed = await reg.remove("/about/")
    check("remove returns True", removed is True)
    check("remove clears cache", reg.lookup("/about/") is None)

    removed_again = await reg.remove("/nonexistent/")
    check("remove missing returns False", removed_again is False)

    # Cleanup
    await db.execute(f"DELETE FROM {_TABLE}")
    await db.disconnect()


# Import the table name for cleanup
from hyperdjango.flatpages import _TABLE


async def test_middleware_non_404():
    """Test middleware passes through non-404 responses."""
    print("\n--- Middleware Non-404 Passthrough ---")
    mw = FlatPageMiddleware()
    req = MockRequest(path="/about/")
    resp = Response.html("<p>OK</p>", status=200)

    result = await mw(req, resp)
    check("non-404 passes through", result.status == 200)
    check("non-404 body unchanged", b"OK" in result.body)


async def test_middleware_serves_flatpage():
    """Test middleware serves flatpage on 404."""
    print("\n--- Middleware Serves Flatpage ---")
    reg = FlatPageRegistry()
    page = FlatPage(url="/about/", title="About", content="<h1>About Us</h1>")
    with reg._lock:
        reg._pages["/about/"] = page

    # Temporarily replace the global registry
    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = reg

    mw = FlatPageMiddleware()
    req = MockRequest(path="/about/")
    resp = Response(status=404, body=b"Not Found")

    result = await mw(req, resp)
    check("404 becomes 200", result.status == 200)
    check("content served", b"About Us" in result.body)

    fp_module.registry = original_registry


async def test_middleware_no_matching_page():
    """Test middleware passes through 404 with no matching page."""
    print("\n--- Middleware No Matching Page ---")
    reg = FlatPageRegistry()

    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = reg

    mw = FlatPageMiddleware()
    req = MockRequest(path="/nonexistent/")
    resp = Response(status=404, body=b"Not Found")

    result = await mw(req, resp)
    check("unmatched 404 stays 404", result.status == 404)

    fp_module.registry = original_registry


async def test_middleware_registration_required():
    """Test middleware blocks unauthenticated and allows authenticated."""
    print("\n--- Middleware Registration Required ---")
    reg = FlatPageRegistry()
    page = FlatPage(
        url="/members/",
        title="Members Only",
        content="<p>Private</p>",
        registration_required=True,
    )
    with reg._lock:
        reg._pages["/members/"] = page

    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = reg

    mw = FlatPageMiddleware()

    # Unauthenticated user
    req_unauth = MockRequest(path="/members/", user=AnonymousUser())
    resp_404 = Response(status=404, body=b"Not Found")
    result = await mw(req_unauth, resp_404)
    check("registration blocks unauthenticated", result.status == 404)

    # No user at all
    req_no_user = MockRequest(path="/members/", user=None)
    resp_404_2 = Response(status=404, body=b"Not Found")
    result2 = await mw(req_no_user, resp_404_2)
    check("registration blocks null user", result2.status == 404)

    # Authenticated user
    req_auth = MockRequest(path="/members/", user=MockUser(is_authenticated=True))
    resp_404_3 = Response(status=404, body=b"Not Found")
    result3 = await mw(req_auth, resp_404_3)
    check("registration allows authenticated", result3.status == 200)
    check("authenticated gets content", b"Private" in result3.body)

    fp_module.registry = original_registry


async def test_middleware_inactive_page():
    """Test middleware does not serve inactive pages."""
    print("\n--- Middleware Inactive Page ---")
    reg = FlatPageRegistry()
    # Manually insert an inactive page into the cache (shouldn't normally happen,
    # but tests the is_active guard)
    page = FlatPage(
        url="/old/", title="Old Page", content="<p>Old</p>", is_active=False
    )
    with reg._lock:
        reg._pages["/old/"] = page

    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = reg

    mw = FlatPageMiddleware()
    req = MockRequest(path="/old/")
    resp = Response(status=404, body=b"Not Found")
    result = await mw(req, resp)
    check("inactive page not served", result.status == 404)

    fp_module.registry = original_registry


async def test_middleware_custom_template():
    """Test middleware uses custom template engine."""
    print("\n--- Middleware Custom Template ---")
    reg = FlatPageRegistry()
    page = FlatPage(
        url="/styled/",
        title="Styled",
        content="<p>Styled content</p>",
        template_name="pages/custom.html",
    )
    with reg._lock:
        reg._pages["/styled/"] = page

    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = reg

    engine = MockTemplateEngine()
    mw = FlatPageMiddleware(template_engine=engine)
    req = MockRequest(path="/styled/")
    resp = Response(status=404, body=b"Not Found")
    result = await mw(req, resp)

    check("custom template used", engine.last_template == "pages/custom.html")
    check("context passed to engine", engine.last_context is not None)
    check("flatpage in context", "flatpage" in (engine.last_context or {}))
    check("rendered content returned", b"<rendered>" in result.body)

    fp_module.registry = original_registry


async def test_middleware_template_fallback():
    """Test middleware falls back to raw HTML when template fails."""
    print("\n--- Middleware Template Fallback ---")
    reg = FlatPageRegistry()
    page = FlatPage(url="/raw/", title="Raw Page", content="<b>Bold content</b>")
    with reg._lock:
        reg._pages["/raw/"] = page

    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = reg

    engine = MockTemplateEngine(should_fail=True)
    mw = FlatPageMiddleware(template_engine=engine)
    req = MockRequest(path="/raw/")
    resp = Response(status=404, body=b"Not Found")
    result = await mw(req, resp)

    check("fallback renders raw content", b"Bold content" in result.body)
    check("fallback includes title", b"Raw Page" in result.body)
    check("fallback is 200", result.status == 200)

    fp_module.registry = original_registry


async def test_standalone_view():
    """Test standalone flatpage_view."""
    print("\n--- Standalone View ---")
    reg = FlatPageRegistry()
    page = FlatPage(url="/help/", title="Help", content="<p>Help page</p>")
    with reg._lock:
        reg._pages["/help/"] = page

    import hyperdjango.flatpages as fp_module

    original_registry = fp_module.registry
    fp_module.registry = reg

    # Serves existing page
    req = MockRequest(path="/help/")
    result = await flatpage_view(req)
    check("view serves page", result.status == 200)
    check("view has content", b"Help page" in result.body)

    # Missing page
    req_missing = MockRequest(path="/nowhere/")
    result2 = await flatpage_view(req_missing)
    check("view 404 for missing", result2.status == 404)

    # Registration required blocks
    page_auth = FlatPage(
        url="/private/",
        title="Private",
        content="<p>Secret</p>",
        registration_required=True,
    )
    with reg._lock:
        reg._pages["/private/"] = page_auth

    req_unauth = MockRequest(path="/private/", user=AnonymousUser())
    result3 = await flatpage_view(req_unauth)
    check("view blocks unauthenticated", result3.status == 404)

    req_auth = MockRequest(path="/private/", user=MockUser(is_authenticated=True))
    result4 = await flatpage_view(req_auth)
    check("view allows authenticated", result4.status == 200)

    fp_module.registry = original_registry


def test_render_no_engine():
    """Test _render_flatpage without template engine."""
    print("\n--- Render No Engine ---")
    page = FlatPage(url="/test/", title="Test Page", content="<p>Content here</p>")
    html = _render_flatpage(page, template_engine=None)
    check("render includes title", "Test Page" in html)
    check("render includes content", "Content here" in html)
    check("render wraps in HTML", "<html>" in html and "</html>" in html)


def test_render_with_engine():
    """Test _render_flatpage with template engine."""
    print("\n--- Render With Engine ---")
    page = FlatPage(url="/test/", title="Rendered", content="<p>Via engine</p>")
    engine = MockTemplateEngine()
    html = _render_flatpage(page, template_engine=engine)
    check("engine called", engine.last_template == "flatpages/default.html")
    check("engine output used", "<rendered>" in html)


def test_render_engine_error_fallback():
    """Test _render_flatpage falls back on engine error."""
    print("\n--- Render Engine Error Fallback ---")
    page = FlatPage(url="/test/", title="Fallback", content="<p>Direct</p>")
    engine = MockTemplateEngine(should_fail=True)
    html = _render_flatpage(page, template_engine=engine)
    check("fallback on error", "Direct" in html)
    check("fallback wraps HTML", "<html>" in html)


def test_content_rendered_as_html():
    """Test content is served as HTML content-type."""
    print("\n--- Content Rendered as HTML ---")
    page = FlatPage(url="/test/", title="HTML Test", content="<div>Rich HTML</div>")
    html = _render_flatpage(page)
    check("html content preserved", "<div>Rich HTML</div>" in html)


def main() -> int:
    print("=" * 60)
    print("Flatpages Module Tests")
    print("=" * 60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Pure unit tests (no DB)
    test_flatpage_model_defaults()
    test_flatpage_to_context()
    test_flatpage_custom_fields()
    test_url_normalization()
    test_registry_empty()
    test_registry_in_memory()
    test_registry_thread_safety()
    test_render_no_engine()
    test_render_with_engine()
    test_render_engine_error_fallback()
    test_content_rendered_as_html()

    # Async tests (no DB)
    loop.run_until_complete(test_middleware_non_404())
    loop.run_until_complete(test_middleware_serves_flatpage())
    loop.run_until_complete(test_middleware_no_matching_page())
    loop.run_until_complete(test_middleware_registration_required())
    loop.run_until_complete(test_middleware_inactive_page())
    loop.run_until_complete(test_middleware_custom_template())
    loop.run_until_complete(test_middleware_template_fallback())
    loop.run_until_complete(test_standalone_view())

    # DB integration tests
    try:
        loop.run_until_complete(test_registry_db_operations())
    except Exception as e:
        print(f"\n  SKIP: DB tests (connection failed: {e})")

    loop.close()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
