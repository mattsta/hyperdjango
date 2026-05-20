#!/usr/bin/env python3
"""
Tests for generic class-based views.

Tests View dispatch, ListView pagination, DetailView, CreateView,
UpdateView, DeleteView, and auth mixins.

Usage:
    uv run hyper-test views
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.auth.user import SessionUser
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.views import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    LoginRequiredMixin,
    PermissionRequiredMixin,
    UpdateView,
    View,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
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


# Mock request object
class MockRequest:
    def __init__(self, method="GET", json_data=None, query=None, user=None):
        self.method = method
        self.json = json_data
        self.GET = query or {}
        self.user = user
        self.path = "/"


# Test model
class ViewItem(Model):
    class Meta:
        table = "test_view_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)
    value: int = Field(default=0)


# ═══════════════════════════════════════════════════════════════════════════
# VIEW BASE CLASS
# ═══════════════════════════════════════════════════════════════════════════


@test("View: as_view returns async callable")
def test_view_as_view():
    class MyView(View):
        async def get(self, request):
            return {"status": "ok"}

    handler = MyView.as_view()
    assert callable(handler)
    assert inspect.iscoroutinefunction(handler)
    assert handler.view_class is MyView


@test("View: dispatches to correct method")
async def test_view_dispatch():
    class MyView(View):
        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"method": "GET"})

        async def post(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"method": "POST"})

    handler = MyView.as_view()

    resp = await handler(MockRequest("GET"))
    assert resp.status == 200

    resp = await handler(MockRequest("POST"))
    assert resp.status == 200


@test("View: 405 for unsupported method")
async def test_view_405():
    class MyView(View):
        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"ok": True})

    handler = MyView.as_view()
    resp = await handler(MockRequest("DELETE"))
    assert resp.status == 405


@test("View: init kwargs set as attributes")
async def test_view_initkwargs():
    class MyView(View):
        extra = None

        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"extra": self.extra})

    handler = MyView.as_view(extra="hello")
    resp = await handler(MockRequest("GET"))
    assert resp.status == 200


# ═══════════════════════════════════════════════════════════════════════════
# LIST VIEW
# ═══════════════════════════════════════════════════════════════════════════


@test("ListView: requires model or queryset")
async def test_listview_requires_model():
    class BadList(ListView):
        pass

    handler = BadList.as_view()
    try:
        await handler(MockRequest("GET"))
        assert False, "Should raise ValueError"
    except ValueError as e:
        assert "model" in str(e).lower()


@test("ListView: serializes model instances")
async def test_listview_serialize():
    items = await ViewItem.objects.order_by("id").all()
    assert len(items) >= 3  # Seeded in setup

    class ItemList(ListView):
        model = ViewItem
        per_page = 10
        ordering = "id"

    handler = ItemList.as_view()
    resp = await handler(MockRequest("GET"))
    assert resp.status == 200


@test("ListView: pagination metadata")
async def test_listview_pagination():
    class ItemList(ListView):
        model = ViewItem
        per_page = 5
        ordering = "id"

    handler = ItemList.as_view()
    resp = await handler(MockRequest("GET", query={"page": "1"}))
    assert resp.status == 200


@test("ListView: no pagination with per_page=0")
async def test_listview_no_pagination():
    class ItemList(ListView):
        model = ViewItem
        per_page = 0
        ordering = "id"

    handler = ItemList.as_view()
    resp = await handler(MockRequest("GET"))
    assert resp.status == 200


# ═══════════════════════════════════════════════════════════════════════════
# DETAIL VIEW
# ═══════════════════════════════════════════════════════════════════════════


@test("DetailView: returns object by PK")
async def test_detailview():
    class ItemDetail(DetailView):
        model = ViewItem

    handler = ItemDetail.as_view()
    resp = await handler(MockRequest("GET"), id=1)
    assert resp.status == 200


@test("DetailView: 404 for missing PK")
async def test_detailview_404():
    class ItemDetail(DetailView):
        model = ViewItem

    handler = ItemDetail.as_view()
    resp = await handler(MockRequest("GET"), id=99999)
    assert resp.status == 404


# ═══════════════════════════════════════════════════════════════════════════
# CREATE VIEW
# ═══════════════════════════════════════════════════════════════════════════


@test("CreateView: GET returns field list")
async def test_createview_get():
    class ItemCreate(CreateView):
        model = ViewItem
        fields = ["name", "value"]

    handler = ItemCreate.as_view()
    resp = await handler(MockRequest("GET"))
    assert resp.status == 200


@test("CreateView: POST creates object")
async def test_createview_post():
    class ItemCreate(CreateView):
        model = ViewItem
        fields = ["name", "value"]
        success_url = "/items"

    handler = ItemCreate.as_view()
    resp = await handler(
        MockRequest("POST", json_data={"name": "Created", "value": 99})
    )
    assert resp.status == 201


@test("CreateView: POST with validation error")
async def test_createview_validation():
    class ItemCreate(CreateView):
        model = ViewItem
        fields = ["name", "value"]

        def validate(self, data):
            errors = {}
            if not data.get("name"):
                errors["name"] = "Name is required"
            return errors

    handler = ItemCreate.as_view()
    resp = await handler(MockRequest("POST", json_data={"value": 1}))
    assert resp.status == 400


# ═══════════════════════════════════════════════════════════════════════════
# UPDATE VIEW
# ═══════════════════════════════════════════════════════════════════════════


@test("UpdateView: PUT updates object")
async def test_updateview_put():
    class ItemUpdate(UpdateView):
        model = ViewItem
        fields = ["name", "value"]

    handler = ItemUpdate.as_view()
    resp = await handler(MockRequest("PUT", json_data={"name": "Updated"}), id=1)
    assert resp.status == 200


@test("UpdateView: PATCH partial update")
async def test_updateview_patch():
    class ItemUpdate(UpdateView):
        model = ViewItem
        fields = ["name", "value"]

    handler = ItemUpdate.as_view()
    resp = await handler(MockRequest("PATCH", json_data={"value": 42}), id=1)
    assert resp.status == 200


@test("UpdateView: 404 for missing PK")
async def test_updateview_404():
    class ItemUpdate(UpdateView):
        model = ViewItem
        fields = ["name"]

    handler = ItemUpdate.as_view()
    resp = await handler(MockRequest("PUT", json_data={"name": "X"}), id=99999)
    assert resp.status == 404


# ═══════════════════════════════════════════════════════════════════════════
# DELETE VIEW
# ═══════════════════════════════════════════════════════════════════════════


@test("DeleteView: GET returns confirmation")
async def test_deleteview_get():
    class ItemDelete(DeleteView):
        model = ViewItem

    handler = ItemDelete.as_view()
    resp = await handler(MockRequest("GET"), id=1)
    assert resp.status == 200


@test("DeleteView: DELETE removes object")
async def test_deleteview_delete():
    # Create a throwaway item to delete
    item = await ViewItem.objects.create(name="ToDelete", value=0)

    class ItemDelete(DeleteView):
        model = ViewItem
        success_url = "/items"

    handler = ItemDelete.as_view()
    resp = await handler(MockRequest("DELETE"), id=item.id)
    assert resp.status == 200

    # Verify deleted
    try:
        await ViewItem.objects.get(id=item.id)
        assert False, "Should have raised DoesNotExist"
    except ViewItem.DoesNotExist:
        pass  # Correctly deleted


@test("DeleteView: 404 for missing PK")
async def test_deleteview_404():
    class ItemDelete(DeleteView):
        model = ViewItem

    handler = ItemDelete.as_view()
    resp = await handler(MockRequest("DELETE"), id=99999)
    assert resp.status == 404


# ═══════════════════════════════════════════════════════════════════════════
# AUTH MIXINS
# ═══════════════════════════════════════════════════════════════════════════


@test("LoginRequiredMixin: 302 redirect without user")
async def test_login_required_no_user():
    class Protected(LoginRequiredMixin, View):
        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"ok": True})

    handler = Protected.as_view()
    resp = await handler(MockRequest("GET"))
    assert resp.status == 302  # Redirects to LOGIN_URL


@test("LoginRequiredMixin: allows authenticated user")
async def test_login_required_with_user():
    class Protected(LoginRequiredMixin, View):
        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"ok": True})

    handler = Protected.as_view()
    # Dict user = session-based auth
    resp = await handler(
        MockRequest("GET", user=SessionUser({"id": 1, "name": "Alice"}))
    )
    assert resp.status == 200


@test("LoginRequiredMixin: allows user object with is_authenticated")
async def test_login_required_user_obj():
    class UserObj:
        is_authenticated = True

    class Protected(LoginRequiredMixin, View):
        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"ok": True})

    handler = Protected.as_view()
    resp = await handler(MockRequest("GET", user=UserObj()))
    assert resp.status == 200


@test("PermissionRequiredMixin: 403 without permission")
async def test_permission_denied():
    class UserObj:
        is_authenticated = True

        def has_perm(self, perm):
            return False

    class AdminOnly(PermissionRequiredMixin, View):
        permission_required = "admin_access"

        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"ok": True})

    handler = AdminOnly.as_view()
    resp = await handler(MockRequest("GET", user=UserObj()))
    assert resp.status == 403


@test("PermissionRequiredMixin: allows with permission")
async def test_permission_granted():
    class UserObj:
        is_authenticated = True

        def has_perm(self, perm):
            return True

    class AdminOnly(PermissionRequiredMixin, View):
        permission_required = "admin_access"

        async def get(self, request, **kwargs):
            from hyperdjango.response import Response

            return Response.json({"ok": True})

    handler = AdminOnly.as_view()
    resp = await handler(MockRequest("GET", user=UserObj()))
    assert resp.status == 200


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    tests = [
        obj
        for name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    unit_tests = [t for t in tests if "DB:" not in t.__name__]

    # Connect DB for integration tests
    db = Database(DB_URL)
    set_db(db)
    await db.connect()

    # Setup test table
    await db.execute("DROP TABLE IF EXISTS test_view_items CASCADE")
    await db.execute("""
        CREATE TABLE test_view_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            value INTEGER DEFAULT 0
        )
    """)
    for i in range(1, 16):
        await db.execute(
            "INSERT INTO test_view_items (name, value) VALUES ($1, $2)",
            f"item_{i}",
            i * 10,
        )

    print("\n═══ Generic Class-Based Views Tests ═══")
    for t in tests:
        await t()

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS test_view_items CASCADE")

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
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
