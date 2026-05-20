#!/usr/bin/env python3
"""
Tests for URL reverse() and flash messages.

Usage:
    uv run hyper-test reverse_messages
"""

# hyper-test: unit

import sys

from hyperdjango.messages import (
    ERROR,
    INFO,
    SUCCESS,
    WARNING,
    MessageMiddleware,
    add_message,
    error,
    get_messages,
    info,
    success,
    warning,
)
from hyperdjango.router import Router

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("URL reverse() + Flash Messages Tests")
    print("=" * 60)

    test_reverse_basic()
    test_reverse_params()
    test_reverse_typed_params()
    test_reverse_not_found()
    test_reverse_multiple_params()
    test_flash_messages_basic()
    test_flash_messages_levels()
    test_flash_messages_clear()
    test_flash_messages_session()
    test_flash_messages_middleware()
    test_reverse_with_app()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


# ---------------------------------------------------------------------------
# URL reverse() tests
# ---------------------------------------------------------------------------


def test_reverse_basic():
    print("\n--- reverse() basic ---")

    router = Router()
    router.add("GET", "/", lambda r: None, name="home")
    router.add("GET", "/about/", lambda r: None, name="about")
    router.add("GET", "/products/", lambda r: None, name="product_list")

    check("reverse home", router.reverse("home") == "/")
    check("reverse about", router.reverse("about") == "/about/")
    check("reverse product_list", router.reverse("product_list") == "/products/")


def test_reverse_params():
    print("\n--- reverse() with params ---")

    router = Router()
    router.add("GET", "/products/{id}/", lambda r: None, name="product_detail")
    router.add(
        "GET", "/users/{user_id}/posts/{post_id}/", lambda r: None, name="user_post"
    )

    url = router.reverse("product_detail", id=42)
    check("reverse with single param", url == "/products/42/", f"got {url!r}")

    url2 = router.reverse("user_post", user_id=5, post_id=123)
    check(
        "reverse with multiple params", url2 == "/users/5/posts/123/", f"got {url2!r}"
    )


def test_reverse_typed_params():
    print("\n--- reverse() with typed params ---")

    router = Router()
    router.add("GET", "/items/{id:int}/", lambda r: None, name="item_detail")
    router.add("GET", "/pages/{slug:str}/", lambda r: None, name="page_detail")

    url = router.reverse("item_detail", id=99)
    check("reverse typed int param", url == "/items/99/", f"got {url!r}")

    url2 = router.reverse("page_detail", slug="hello-world")
    check("reverse typed str param", url2 == "/pages/hello-world/", f"got {url2!r}")


def test_reverse_not_found():
    print("\n--- reverse() not found ---")

    router = Router()
    router.add("GET", "/", lambda r: None, name="home")

    try:
        router.reverse("nonexistent")
        check("reverse raises on unknown name", False)
    except ValueError as e:
        check("reverse raises on unknown name", "No route named" in str(e))


def test_reverse_multiple_params():
    print("\n--- reverse() edge cases ---")

    router = Router()
    router.add("GET", "/api/v1/{resource}/{id}/", lambda r: None, name="api_detail")

    url = router.reverse("api_detail", resource="users", id=42)
    check("reverse with path + id params", url == "/api/v1/users/42/", f"got {url!r}")

    # Unnamed routes should not match
    router.add("GET", "/unnamed/", lambda r: None)
    try:
        router.reverse(None)
        check("reverse None name raises", False)
    except ValueError, TypeError:
        check("reverse None name raises", True)


# ---------------------------------------------------------------------------
# Flash messages tests
# ---------------------------------------------------------------------------


class MockRequest:
    """Mock request with session dict for testing."""

    def __init__(self):
        self.session = {}


def test_flash_messages_basic():
    print("\n--- Flash messages basic ---")

    req = MockRequest()
    add_message(req, SUCCESS, "Item created")
    msgs = get_messages(req)

    check("one message stored", len(msgs) == 1)
    check("message level correct", msgs[0]["level"] == SUCCESS)
    check("message text correct", msgs[0]["text"] == "Item created")

    # Messages cleared after get
    msgs2 = get_messages(req)
    check("messages cleared after get", len(msgs2) == 0)


def test_flash_messages_levels():
    print("\n--- Flash message levels ---")

    req = MockRequest()
    success(req, "Success!")
    error(req, "Error!")
    info(req, "Info!")
    warning(req, "Warning!")

    msgs = get_messages(req)
    check("four messages stored", len(msgs) == 4)
    check("success level", msgs[0]["level"] == SUCCESS)
    check("error level", msgs[1]["level"] == ERROR)
    check("info level", msgs[2]["level"] == INFO)
    check("warning level", msgs[3]["level"] == WARNING)


def test_flash_messages_clear():
    print("\n--- Flash messages clear behavior ---")

    req = MockRequest()
    success(req, "msg1")
    success(req, "msg2")

    # Get without clearing
    msgs = get_messages(req, clear=False)
    check("get without clear returns messages", len(msgs) == 2)

    msgs2 = get_messages(req, clear=False)
    check("messages persist when clear=False", len(msgs2) == 2)

    # Now clear
    msgs3 = get_messages(req, clear=True)
    check("get with clear returns messages", len(msgs3) == 2)

    msgs4 = get_messages(req)
    check("messages gone after clear", len(msgs4) == 0)


def test_flash_messages_session():
    print("\n--- Flash messages with session dict ---")

    req = MockRequest()
    success(req, "Session msg")

    # Verify stored in session
    check("stored in session dict", "_messages" in req.session)
    check("session has one message", len(req.session["_messages"]) == 1)


def test_flash_messages_middleware():
    print("\n--- Flash messages middleware ---")

    import asyncio

    middleware = MessageMiddleware()
    check("middleware is callable", callable(middleware))

    # Test middleware execution
    req = MockRequest()
    success(req, "Pre-redirect message")

    async def handler(request):
        return request._pending_messages

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(middleware(req, handler))
    check("middleware loads messages", len(result) == 1)
    check("middleware message text", result[0]["text"] == "Pre-redirect message")

    # Messages should be cleared after middleware
    msgs_after = get_messages(req)
    check("messages cleared after middleware", len(msgs_after) == 0)


def test_flash_messages_no_session():
    print("\n--- Flash messages without session (fallback) ---")

    class NoSessionRequest:
        pass

    req = NoSessionRequest()
    success(req, "Fallback msg")
    msgs = get_messages(req)
    check("fallback stores message", len(msgs) == 1)
    check("fallback message text", msgs[0]["text"] == "Fallback msg")


def test_reverse_with_app():
    print("\n--- reverse() integrated with HyperApp ---")

    from hyperdjango.app import HyperApp

    app = HyperApp(title="ReverseTest")

    @app.get("/products/", name="products")
    async def products(request):
        pass

    @app.get("/products/{id}/", name="product_detail")
    async def product_detail(request):
        pass

    url = app.router.reverse("products")
    check("app reverse static", url == "/products/", f"got {url!r}")

    url2 = app.router.reverse("product_detail", id=42)
    check("app reverse with param", url2 == "/products/42/", f"got {url2!r}")


if __name__ == "__main__":
    sys.exit(main())
