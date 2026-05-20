#!/usr/bin/env python3
"""
Tests for URL namespaces and includes.

Tests: include() with sub-routers, include() with route lists,
namespace-aware reverse(), nested includes, prefix handling.

Usage:
    uv run hyper-test url_namespaces
"""

# hyper-test: unit

import sys
import traceback

from hyperdjango.router import Router

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        def wrapper():
            try:
                func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# Dummy handlers
def index(r):
    pass


def list_posts(r):
    pass


def post_detail(r):
    pass


def create_post(r):
    pass


def list_users(r):
    pass


def user_detail(r):
    pass


def list_comments(r):
    pass


def dashboard(r):
    pass


def settings_page(r):
    pass


# ═══════════════════════════════════════════════════════════════════════════
# INCLUDE WITH SUB-ROUTER
# ═══════════════════════════════════════════════════════════════════════════


@test("include sub-router: routes get prefixed")
def test_include_sub_router():
    main = Router()
    main.add("GET", "/", index, name="home")

    blog = Router()
    blog.add("GET", "/", list_posts, name="list")
    blog.add("GET", "/{id:int}", post_detail, name="detail")
    blog.add("POST", "/", create_post, name="create")

    main.include("/blog", blog, namespace="blog")

    # Check routes were added with prefix
    routes = main.routes()
    patterns = [r.pattern for r in routes]
    assert "/" in patterns
    assert "/blog/" in patterns
    assert "/blog/{id:int}" in patterns


@test("include sub-router: resolve works with prefix")
def test_include_resolve():
    main = Router()
    blog = Router()
    blog.add("GET", "/", list_posts, name="list")
    blog.add("GET", "/{id:int}", post_detail, name="detail")
    main.include("/blog", blog, namespace="blog")
    main.finalize()

    route, params = main.resolve("GET", "/blog/")
    assert route is not None
    assert route.handler is list_posts

    route, params = main.resolve("GET", "/blog/42")
    assert route is not None
    assert route.handler is post_detail
    assert params.get("id") == 42


@test("include sub-router: namespace in reverse()")
def test_include_reverse():
    main = Router()
    blog = Router()
    blog.add("GET", "/", list_posts, name="list")
    blog.add("GET", "/{id:int}", post_detail, name="detail")
    main.include("/blog", blog, namespace="blog")

    url = main.reverse("blog:list")
    assert url == "/blog/", f"Got: {url!r}"

    url = main.reverse("blog:detail", id=42)
    assert url == "/blog/42", f"Got: {url!r}"


@test("include sub-router: POST method preserved")
def test_include_method():
    main = Router()
    blog = Router()
    blog.add("POST", "/", create_post, name="create")
    main.include("/blog", blog, namespace="blog")
    main.finalize()

    route, _ = main.resolve("POST", "/blog/")
    assert route is not None
    assert route.handler is create_post

    route, _ = main.resolve("GET", "/blog/")
    assert route is None


# ═══════════════════════════════════════════════════════════════════════════
# INCLUDE WITH ROUTE LIST
# ═══════════════════════════════════════════════════════════════════════════


@test("include route list: 4-tuple format")
def test_include_list_4tuple():
    main = Router()
    main.include(
        "/api",
        [
            ("GET", "/users", list_users, "users"),
            ("GET", "/users/{id:int}", user_detail, "user-detail"),
        ],
        namespace="api",
    )

    route, _ = main.resolve("GET", "/api/users")
    assert route is not None
    assert route.handler is list_users

    route, params = main.resolve("GET", "/api/users/5")
    assert route is not None
    assert params.get("id") == 5

    url = main.reverse("api:users")
    assert url == "/api/users"

    url = main.reverse("api:user-detail", id=5)
    assert url == "/api/users/5"


@test("include route list: 3-tuple format (auto name)")
def test_include_list_3tuple():
    main = Router()
    main.include(
        "/api",
        [
            ("GET", "/comments", list_comments),
        ],
        namespace="api",
    )

    route, _ = main.resolve("GET", "/api/comments")
    assert route is not None

    url = main.reverse("api:list_comments")
    assert url == "/api/comments"


# ═══════════════════════════════════════════════════════════════════════════
# WITHOUT NAMESPACE
# ═══════════════════════════════════════════════════════════════════════════


@test("include without namespace: names not prefixed")
def test_include_no_namespace():
    main = Router()
    blog = Router()
    blog.add("GET", "/", list_posts, name="list_posts")
    main.include("/blog", blog)

    url = main.reverse("list_posts")
    assert url == "/blog/", f"Got: {url!r}"


# ═══════════════════════════════════════════════════════════════════════════
# MULTIPLE INCLUDES
# ═══════════════════════════════════════════════════════════════════════════


@test("multiple includes: different namespaces")
def test_multiple_includes():
    main = Router()
    main.add("GET", "/", index, name="home")

    blog = Router()
    blog.add("GET", "/", list_posts, name="list")

    api = Router()
    api.add("GET", "/users", list_users, name="users")

    main.include("/blog", blog, namespace="blog")
    main.include("/api", api, namespace="api")
    main.finalize()

    assert main.reverse("home") == "/"
    assert main.reverse("blog:list") == "/blog/"
    assert main.reverse("api:users") == "/api/users"

    route, _ = main.resolve("GET", "/blog/")
    assert route.handler is list_posts

    route, _ = main.resolve("GET", "/api/users")
    assert route.handler is list_users


@test("multiple includes: no namespace collision")
def test_namespace_collision():
    main = Router()

    blog = Router()
    blog.add("GET", "/", list_posts, name="list")

    users = Router()
    users.add("GET", "/", list_users, name="list")

    main.include("/blog", blog, namespace="blog")
    main.include("/users", users, namespace="users")

    # Both have "list" as base name, but namespaced differently
    assert main.reverse("blog:list") == "/blog/"
    assert main.reverse("users:list") == "/users/"


# ═══════════════════════════════════════════════════════════════════════════
# NESTED INCLUDES
# ═══════════════════════════════════════════════════════════════════════════


@test("nested includes: sub-sub-router")
def test_nested_includes():
    main = Router()

    v1 = Router()
    v1.add("GET", "/users", list_users, name="users")
    v1.add("GET", "/users/{id:int}", user_detail, name="user-detail")

    api = Router()
    api.include("/v1", v1, namespace="v1")

    main.include("/api", api, namespace="api")

    route, _ = main.resolve("GET", "/api/v1/users")
    assert route is not None
    assert route.handler is list_users

    route, params = main.resolve("GET", "/api/v1/users/42")
    assert route is not None
    assert params.get("id") == 42

    url = main.reverse("api:v1:users")
    assert url == "/api/v1/users"

    url = main.reverse("api:v1:user-detail", id=42)
    assert url == "/api/v1/users/42"


# ═══════════════════════════════════════════════════════════════════════════
# PREFIX NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════


@test("prefix normalization: no leading slash")
def test_prefix_no_leading_slash():
    main = Router()
    blog = Router()
    blog.add("GET", "/", list_posts, name="list")
    main.include("blog", blog, namespace="blog")
    main.finalize()

    route, _ = main.resolve("GET", "/blog/")
    assert route is not None


@test("prefix normalization: trailing slash stripped")
def test_prefix_trailing_slash():
    main = Router()
    blog = Router()
    blog.add("GET", "/posts", list_posts, name="posts")
    main.include("/blog/", blog, namespace="blog")

    route, _ = main.resolve("GET", "/blog/posts")
    assert route is not None


# ═══════════════════════════════════════════════════════════════════════════
# REVERSE ERRORS
# ═══════════════════════════════════════════════════════════════════════════


@test("reverse: raises ValueError for unknown name")
def test_reverse_unknown():
    main = Router()
    try:
        main.reverse("nonexistent")
        assert False, "Should raise ValueError"
    except ValueError as e:
        assert "nonexistent" in str(e)


@test("reverse: raises ValueError for wrong namespace")
def test_reverse_wrong_namespace():
    main = Router()
    blog = Router()
    blog.add("GET", "/", list_posts, name="list")
    main.include("/blog", blog, namespace="blog")

    try:
        main.reverse("wrong:list")
        assert False, "Should raise ValueError"
    except ValueError:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════


@test("include empty router: no routes added")
def test_include_empty():
    main = Router()
    empty = Router()
    main.include("/empty", empty, namespace="empty")
    assert len(main.routes()) == 0


@test("include preserves async handlers")
def test_include_async():
    async def async_handler(r):
        pass

    main = Router()
    sub = Router()
    sub.add("GET", "/async", async_handler, name="async")
    main.include("/sub", sub, namespace="sub")

    route, _ = main.resolve("GET", "/sub/async")
    assert route is not None
    assert route.is_async


@test("include with wildcard route")
def test_include_wildcard():
    main = Router()
    files = Router()

    def serve_file(r):
        pass

    files.add("GET", "/*path", serve_file, name="serve")
    main.include("/static", files, namespace="static")

    route, params = main.resolve("GET", "/static/css/app.css")
    assert route is not None
    assert params.get("path") == "css/app.css"


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


def main():
    tests = [
        obj
        for name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print("\n═══ URL Namespaces & Includes Tests ═══")
    for t in tests:
        t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
