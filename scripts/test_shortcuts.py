"""
Tests for view shortcuts: get_object_or_404, get_list_or_404, redirect, render.
"""

# hyper-test: unit

import asyncio
import sys

from hyperdjango.app import HTTPException
from hyperdjango.response import Response
from hyperdjango.shortcuts import (
    get_list_or_404,
    get_object_or_404,
    redirect,
    render,
)

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        errors.append(msg)
        print(msg)


# ── Mock model for testing ─────────────────────────────────────────────────


class MockDoesNotExist(Exception):
    pass


class MockMultipleReturned(Exception):
    pass


class MockQuerySet:
    def __init__(self, items: list[dict[str, object]]):
        self._items = items
        self._filters: dict[str, object] = {}

    def filter(self, **kwargs):
        filtered = []
        for item in self._items:
            match = all(item.get(k) == v for k, v in kwargs.items())
            if match:
                filtered.append(item)
        qs = MockQuerySet(filtered)
        qs._filters = kwargs
        return qs

    async def get(self, db=None):
        if len(self._items) == 0:
            raise MockModel.DoesNotExist("Not found")
        if len(self._items) > 1:
            raise MockModel.MultipleObjectsReturned("Multiple found")
        return self._items[0]

    async def all(self, db=None):
        return self._items


class MockModel:
    DoesNotExist = MockDoesNotExist
    MultipleObjectsReturned = MockMultipleReturned
    __name__ = "MockModel"

    objects = MockQuerySet(
        [
            {"id": 1, "name": "Alice", "active": True},
            {"id": 2, "name": "Bob", "active": True},
            {"id": 3, "name": "Charlie", "active": False},
        ]
    )


# ── get_object_or_404 ─────────────────────────────────────────────────────

print("=== get_object_or_404 ===")

loop = asyncio.new_event_loop()

# Found
result = loop.run_until_complete(get_object_or_404(MockModel, id=1))
check("get_found", result["name"] == "Alice")

# Not found → 404
try:
    loop.run_until_complete(get_object_or_404(MockModel, id=999))
    check("get_not_found", False, "should have raised")
except HTTPException as e:
    check("get_not_found", e.status_code == 404)
    check("get_not_found_msg", "MockModel" in str(e.detail))

# Multiple → 404
try:
    loop.run_until_complete(get_object_or_404(MockModel, active=True))
    check("get_multiple", False, "should have raised")
except HTTPException as e:
    check("get_multiple", e.status_code == 404)
    check("get_multiple_msg", "Multiple" in str(e.detail))

# With filter kwargs
result = loop.run_until_complete(get_object_or_404(MockModel, name="Bob"))
check("get_by_name", result["id"] == 2)

# ── get_list_or_404 ───────────────────────────────────────────────────────

print("\n=== get_list_or_404 ===")

# Found multiple
results = loop.run_until_complete(get_list_or_404(MockModel, active=True))
check("list_found", len(results) == 2)

# Found one
results = loop.run_until_complete(get_list_or_404(MockModel, name="Charlie"))
check("list_one", len(results) == 1)
check("list_one_name", results[0]["name"] == "Charlie")

# Empty → 404
try:
    loop.run_until_complete(get_list_or_404(MockModel, name="Nobody"))
    check("list_empty", False, "should have raised")
except HTTPException as e:
    check("list_empty", e.status_code == 404)
    check("list_empty_msg", "MockModel" in str(e.detail))

# ── redirect ──────────────────────────────────────────────────────────────

print("\n=== redirect ===")

# Temporary redirect (default)
r = redirect("/articles/")
check("redirect_302", r.status == 302)
check("redirect_location", r.headers.get("location") == "/articles/")

# Permanent redirect
r = redirect("/new/", permanent=True)
check("redirect_301", r.status == 301)

# Custom status
r = redirect("/api/", status=307)
check("redirect_307", r.status == 307)

# Status overrides permanent
r = redirect("/x/", permanent=True, status=308)
check("redirect_308_override", r.status == 308)

# Returns Response
check("redirect_is_response", isinstance(r, Response))

# ── render ─────────────────────────────────────────────────────────────────

print("\n=== render ===")

# render requires request.app with template engine
# We can test the basic flow with a mock


class MockTemplateEngine:
    def render(self, name, context):
        return f"<h1>{context.get('title', 'default')}</h1>"

    def add_global(self, name, fn):
        pass

    def add_filter(self, name, fn):
        pass


class MockApp:
    _template_engine = MockTemplateEngine()
    templates_dir = "templates"

    def render(self, template_name, context=None, status=200):
        html = self._template_engine.render(template_name, context or {})
        return Response.html(html, status=status)


class MockRequest:
    app = MockApp()


req = MockRequest()
r = render(req, "test.html", {"title": "Hello"})
check("render_status", r.status == 200)
check("render_body", b"Hello" in r.body)
check("render_is_response", isinstance(r, Response))

# Custom status
r2 = render(req, "test.html", {"title": "Error"}, status=400)
check("render_custom_status", r2.status == 400)

# None context
r3 = render(req, "test.html")
check("render_none_context", b"default" in r3.body)

# ── Summary ────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"Shortcuts tests: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'=' * 60}")

loop.close()
sys.exit(0 if failed == 0 else 1)
