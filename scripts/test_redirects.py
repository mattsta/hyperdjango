#!/usr/bin/env python3
"""Test redirects module — Redirect model, RedirectRegistry, RedirectMiddleware.

Tests:
1.  Redirect model creation with defaults
2.  Redirect model creation with custom values
3.  Redirect model __str__ representation
4.  Redirect model is_active default True
5.  Registry starts empty (count == 0)
6.  Registry add single redirect
7.  Registry add updates existing redirect
8.  Registry remove existing redirect
9.  Registry remove nonexistent returns False
10. Registry lookup exact match
11. Registry lookup miss returns None
12. Registry clear removes all
13. Registry count property
14. Registry load_all from redirect list
15. Registry load_all skips inactive redirects
16. Registry load_all clears previous data
17. Registry prefix matching
18. Registry longest prefix wins
19. Middleware passes through 200 response
20. Middleware passes through 500 response
21. Middleware intercepts 404 with matching redirect (301)
22. Middleware intercepts 404 with 302 status
23. Middleware passes through 404 with no match
24. Middleware preserves query string on redirect
25. Registry thread safety — concurrent adds
26. Registry thread safety — concurrent add and lookup
27. Empty registry lookup returns None
28. Redirect model with 302 status code
29. Registry all_redirects returns correct list
30. Middleware exact path match before prefix
31. Registry add prefix redirect
32. Registry remove prefix redirect
33. Middleware handles path with query string redirect
34. Singleton registry exists at module level

Run: uv run hyper-test redirects
"""

# hyper-test: unit

import asyncio
import threading

from hyperdjango.redirects import (
    Redirect,
    RedirectMatch,
    RedirectMiddleware,
    RedirectRegistry,
    registry,
)
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.testkit import check, finish, run_main

# ── Redirect model tests ──


def test_redirect_model():
    print("\n=== Redirect Model ===")

    # 1. Creation with defaults
    r = Redirect(old_path="/old/", new_path="/new/")
    check("default status_code is 301", r.status_code == 301, f"got {r.status_code}")
    check("default is_active is True", r.is_active is True, f"got {r.is_active}")

    # 2. Custom values
    r2 = Redirect(old_path="/a/", new_path="/b/", status_code=302, is_active=False)
    check("custom status_code 302", r2.status_code == 302)
    check("custom is_active False", r2.is_active is False)

    # 3. __str__
    r3 = Redirect(old_path="/from/", new_path="/to/")
    s = str(r3)
    check("__str__ format", s == "/from/ ---> /to/ (301)", f"got {s!r}")

    # 4. 302 model
    r4 = Redirect(old_path="/temp/", new_path="/dest/", status_code=302)
    check("302 status code stored", r4.status_code == 302)


# ── RedirectRegistry tests ──


def test_registry_basic():
    print("\n=== Registry Basic Operations ===")

    reg = RedirectRegistry()

    # 5. Starts empty
    check("empty registry count is 0", reg.count == 0, f"got {reg.count}")

    # 6. Add single
    r = reg.add("/old/", "/new/", 301)
    check("add returns Redirect", r.old_path == "/old/" and r.new_path == "/new/")
    check("count after add is 1", reg.count == 1, f"got {reg.count}")

    # 7. Add updates existing
    reg.add("/old/", "/newer/", 302)
    result = reg.lookup("/old/")
    check(
        "add updates existing redirect",
        result is not None and result[0] == "/newer/" and result[1] == 302,
        f"got {result}",
    )
    check("count still 1 after update", reg.count == 1, f"got {reg.count}")

    # 8. Remove existing
    removed = reg.remove("/old/")
    check("remove returns True for existing", removed is True)
    check("count after remove is 0", reg.count == 0, f"got {reg.count}")

    # 9. Remove nonexistent
    removed2 = reg.remove("/nonexistent/")
    check("remove returns False for nonexistent", removed2 is False)


def test_registry_lookup():
    print("\n=== Registry Lookup ===")

    reg = RedirectRegistry()
    reg.add("/old-page/", "/new-page/", 301)
    reg.add("/temp/", "/dest/", 302)

    # 10. Exact match
    result = reg.lookup("/old-page/")
    check(
        "exact match found",
        result is not None and result == ("/new-page/", 301),
        f"got {result}",
    )

    # 11. Miss
    result2 = reg.lookup("/nonexistent/")
    check("miss returns None", result2 is None, f"got {result2}")

    # 12. Clear
    reg.clear()
    check("clear removes all", reg.count == 0, f"got {reg.count}")
    result3 = reg.lookup("/old-page/")
    check("lookup after clear returns None", result3 is None)


def test_registry_count():
    print("\n=== Registry Count ===")

    reg = RedirectRegistry()

    # 13. Count property
    check("initial count 0", reg.count == 0)
    reg.add("/a/", "/b/")
    reg.add("/c/", "/d/")
    reg.add("/e/", "/f/")
    check("count after 3 adds is 3", reg.count == 3, f"got {reg.count}")
    reg.remove("/a/")
    check("count after remove is 2", reg.count == 2, f"got {reg.count}")


def test_registry_load_all():
    print("\n=== Registry load_all ===")

    reg = RedirectRegistry()

    redirects = [
        Redirect(old_path="/a/", new_path="/b/", status_code=301, is_active=True),
        Redirect(old_path="/c/", new_path="/d/", status_code=302, is_active=True),
        Redirect(
            old_path="/inactive/", new_path="/skip/", status_code=301, is_active=False
        ),
    ]

    async def _load():
        return await reg.load_all(redirects)

    loaded = asyncio.run(_load())

    # 14. load_all loads from list
    check("load_all returns count of loaded", loaded == 2, f"got {loaded}")
    check("count matches loaded", reg.count == 2, f"got {reg.count}")

    # 15. Skips inactive
    result = reg.lookup("/inactive/")
    check("inactive redirect not loaded", result is None, f"got {result}")

    # 16. Clears previous data
    reg.add("/extra/", "/other/")
    check("count before reload is 3", reg.count == 3, f"got {reg.count}")

    loaded2 = asyncio.run(_load())
    check(
        "load_all clears previous data",
        reg.count == 2,
        f"got count={reg.count}",
    )


def test_registry_prefix():
    print("\n=== Registry Prefix Matching ===")

    reg = RedirectRegistry()

    # 17. Prefix matching
    reg.add("/blog/*", "/articles/", 301)
    result = reg.lookup("/blog/post-1/")
    check(
        "prefix match works",
        result is not None and result == ("/articles/", 301),
        f"got {result}",
    )

    # 18. Longest prefix wins
    reg.add("/blog/2024/*", "/archive/2024/", 302)
    result2 = reg.lookup("/blog/2024/january/")
    check(
        "longest prefix wins",
        result2 is not None and result2 == ("/archive/2024/", 302),
        f"got {result2}",
    )

    # Shorter prefix still works for non-matching paths
    result3 = reg.lookup("/blog/other/")
    check(
        "shorter prefix matches when longer does not",
        result3 is not None and result3 == ("/articles/", 301),
        f"got {result3}",
    )


# ── Middleware tests ──


def test_middleware_passthrough():
    print("\n=== Middleware Passthrough ===")

    reg = RedirectRegistry()
    reg.add("/old/", "/new/", 301)
    mw = RedirectMiddleware(registry=reg)

    # 19. Passes through 200
    async def _test_200():
        req = Request(method="GET", path="/some-page/")

        async def call_next(r: Request) -> Response:
            return Response.html("<h1>OK</h1>", status=200)

        return await mw(req, call_next)

    resp = asyncio.run(_test_200())
    check("passes through 200", resp.status == 200, f"got {resp.status}")

    # 20. Passes through 500
    async def _test_500():
        req = Request(method="GET", path="/error/")

        async def call_next(r: Request) -> Response:
            return Response.text("error", status=500)

        return await mw(req, call_next)

    resp2 = asyncio.run(_test_500())
    check("passes through 500", resp2.status == 500, f"got {resp2.status}")


def test_middleware_redirect():
    print("\n=== Middleware Redirect ===")

    reg = RedirectRegistry()
    reg.add("/old-page/", "/new-page/", 301)
    reg.add("/temp-page/", "/dest-page/", 302)
    mw = RedirectMiddleware(registry=reg)

    # 21. Intercepts 404 with 301
    async def _test_301():
        req = Request(method="GET", path="/old-page/")

        async def call_next(r: Request) -> Response:
            return Response.text("Not found", status=404)

        return await mw(req, call_next)

    resp = asyncio.run(_test_301())
    check("intercepts 404 -> 301", resp.status == 301, f"got {resp.status}")
    check(
        "301 location header correct",
        resp.headers.get("location") == "/new-page/",
        f"got {resp.headers.get('location')}",
    )

    # 22. Intercepts 404 with 302
    async def _test_302():
        req = Request(method="GET", path="/temp-page/")

        async def call_next(r: Request) -> Response:
            return Response.text("Not found", status=404)

        return await mw(req, call_next)

    resp2 = asyncio.run(_test_302())
    check("intercepts 404 -> 302", resp2.status == 302, f"got {resp2.status}")
    check(
        "302 location header correct",
        resp2.headers.get("location") == "/dest-page/",
        f"got {resp2.headers.get('location')}",
    )

    # 23. 404 with no match passes through
    async def _test_no_match():
        req = Request(method="GET", path="/unknown/")

        async def call_next(r: Request) -> Response:
            return Response.text("Not found", status=404)

        return await mw(req, call_next)

    resp3 = asyncio.run(_test_no_match())
    check("404 no match passes through", resp3.status == 404, f"got {resp3.status}")


def test_middleware_query_string():
    print("\n=== Middleware Query String ===")

    reg = RedirectRegistry()
    reg.add("/search/", "/find/", 301)
    mw = RedirectMiddleware(registry=reg)

    # 24. Query string preserved
    async def _test_qs():
        req = Request(method="GET", path="/search/", query_string="q=hello&page=2")

        async def call_next(r: Request) -> Response:
            return Response.text("Not found", status=404)

        return await mw(req, call_next)

    resp = asyncio.run(_test_qs())
    check("redirect preserves query string", resp.status == 301, f"got {resp.status}")
    location = resp.headers.get("location", "")
    check(
        "query string in location",
        location == "/find/?q=hello&page=2",
        f"got {location!r}",
    )


# ── Thread safety tests ──


def test_thread_safety():
    print("\n=== Thread Safety ===")

    reg = RedirectRegistry()

    # 25. Concurrent adds
    errors: list[str] = []

    def add_batch(start: int, count: int) -> None:
        for i in range(start, start + count):
            reg.add(f"/path-{i}/", f"/dest-{i}/", 301)

    threads = [
        threading.Thread(target=add_batch, args=(i * 100, 100)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("concurrent adds all present", reg.count == 400, f"got {reg.count}")

    # 26. Concurrent add and lookup
    reg2 = RedirectRegistry()
    for i in range(100):
        reg2.add(f"/r-{i}/", f"/d-{i}/", 301)

    lookup_results: list[bool] = []

    def do_lookups() -> None:
        for i in range(100):
            result = reg2.lookup(f"/r-{i}/")
            if result is not None:
                lookup_results.append(True)

    def do_adds() -> None:
        for i in range(100, 200):
            reg2.add(f"/r-{i}/", f"/d-{i}/", 301)

    t1 = threading.Thread(target=do_lookups)
    t2 = threading.Thread(target=do_adds)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    check(
        "concurrent lookup during adds works",
        len(lookup_results) == 100,
        f"got {len(lookup_results)} successful lookups",
    )


# ── Edge case tests ──


def test_edge_cases():
    print("\n=== Edge Cases ===")

    # 27. Empty registry lookup
    reg = RedirectRegistry()
    result = reg.lookup("/anything/")
    check("empty registry lookup returns None", result is None, f"got {result}")

    # 28. all_redirects
    reg.add("/a/", "/b/", 301)
    reg.add("/c/", "/d/", 302)
    all_r = reg.all_redirects()
    check("all_redirects returns 2", len(all_r) == 2, f"got {len(all_r)}")
    paths = {r.old_path for r in all_r}
    check("all_redirects correct paths", paths == {"/a/", "/c/"}, f"got {paths}")

    # 29. Prefix add and remove
    reg2 = RedirectRegistry()
    reg2.add("/api/*", "/v2/api/", 301)
    check("prefix redirect added", reg2.count == 1)
    result2 = reg2.lookup("/api/users/")
    check(
        "prefix redirect matches",
        result2 is not None and result2[0] == "/v2/api/",
        f"got {result2}",
    )
    removed = reg2.remove("/api/*")
    check("prefix redirect removed", removed is True)
    check("count after prefix remove", reg2.count == 0, f"got {reg2.count}")

    # 30. Middleware exact match before prefix
    reg3 = RedirectRegistry()
    reg3.add("/docs/*", "/wiki/", 301)
    reg3.add("/docs/api/", "/api-docs/", 302)
    mw = RedirectMiddleware(registry=reg3)

    async def _test_exact_before_prefix():
        req = Request(method="GET", path="/docs/api/")

        async def call_next(r: Request) -> Response:
            return Response.text("Not found", status=404)

        return await mw(req, call_next)

    resp = asyncio.run(_test_exact_before_prefix())
    check(
        "exact match preferred over prefix",
        resp.status == 302,
        f"got {resp.status}",
    )
    check(
        "exact match location correct",
        resp.headers.get("location") == "/api-docs/",
        f"got {resp.headers.get('location')}",
    )


def test_open_redirect_rejection():
    print("\n=== Open Redirect Rejection ===")

    reg = RedirectRegistry()

    # Reject absolute URL by default
    try:
        reg.add("/old/", "https://evil.com/phish", 301)
        check("rejects absolute URL by default", False, "should have raised ValueError")
    except ValueError as exc:
        check("rejects absolute URL by default", "relative path" in str(exc))

    # Reject protocol-relative URL
    try:
        reg.add("/old/", "//evil.com/phish", 301)
        check("rejects protocol-relative URL", False, "should have raised ValueError")
    except ValueError as exc:
        check("rejects protocol-relative URL", "relative path" in str(exc))

    # Allow relative paths
    r = reg.add("/old/", "/new/", 301)
    check("allows relative path", r.new_path == "/new/")

    # Allow external with flag
    r2 = reg.add("/ext/", "https://example.com/page", 302, allow_external=True)
    check("allows external with flag", r2.new_path == "https://example.com/page")

    # Verify count includes both
    check("registry has 2 entries", reg.count == 2, f"got {reg.count}")


def test_middleware_query_string_redirect():
    print("\n=== Middleware Query String Redirect ===")

    # 33. Path with query string in redirect target
    reg = RedirectRegistry()
    reg.add("/legacy/", "https://example.com/new?ref=legacy", 301, allow_external=True)
    mw = RedirectMiddleware(registry=reg)

    async def _test():
        req = Request(method="GET", path="/legacy/", query_string="extra=1")

        async def call_next(r: Request) -> Response:
            return Response.text("Not found", status=404)

        return await mw(req, call_next)

    resp = asyncio.run(_test())
    location = resp.headers.get("location", "")
    check(
        "redirect with existing query string not doubled",
        location == "https://example.com/new?ref=legacy",
        f"got {location!r}",
    )


def test_singleton():
    print("\n=== Singleton Registry ===")

    # 34. Module-level registry exists
    check(
        "module-level registry is RedirectRegistry",
        isinstance(registry, RedirectRegistry),
    )
    check("module-level registry is usable", registry.count >= 0)


def test_redirect_match_dataclass():
    print("\n=== RedirectMatch Dataclass ===")

    m = RedirectMatch(new_path="/dest/", status_code=301)
    check("RedirectMatch new_path", m.new_path == "/dest/")
    check("RedirectMatch status_code", m.status_code == 301)


# ── Run all tests ──


def main() -> bool:
    test_redirect_model()
    test_registry_basic()
    test_registry_lookup()
    test_registry_count()
    test_registry_load_all()
    test_registry_prefix()
    test_middleware_passthrough()
    test_middleware_redirect()
    test_middleware_query_string()
    test_thread_safety()
    test_edge_cases()
    test_open_redirect_rejection()
    test_middleware_query_string_redirect()
    test_singleton()
    test_redirect_match_dataclass()

    print(f"\n{'=' * 50}")
    return finish()


if __name__ == "__main__":
    run_main(main)
