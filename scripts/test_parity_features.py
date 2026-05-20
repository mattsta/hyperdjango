"""
Tests for features implemented from Django parity audit:
- QuerySet.get_or_create
- QuerySet.update_or_create
- QuerySet.bulk_update
- QuerySet.in_bulk
- QuerySet.aiterator
- Database.on_commit
- require_http_methods / require_GET / require_POST / require_safe
"""

# hyper-test: unit

import asyncio
import sys

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


# ── Mock model for QuerySet tests ──────────────────────────────────────────

from _test_meta import make_table_meta

from hyperdjango.query import QuerySet


class MockModel:
    # Real TableMeta (scripts/_test_meta.py): column_names/writable_columns/
    # pk_fields/pk_where_clause/get_fk_fields are the genuine derived contract.
    _meta = make_table_meta("test_items", ["id", "name", "status"])
    _loaded_from_db = False

    class DoesNotExist(Exception):
        pass

    class MultipleObjectsReturned(Exception):
        pass

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        if "id" not in kwargs:
            self.id = None
        if "name" not in kwargs:
            self.name = ""
        if "status" not in kwargs:
            self.status = "active"

    @classmethod
    def from_record(cls, record):
        if isinstance(record, dict):
            return cls(**record)
        return record

    async def save(self, db=None):
        self._loaded_from_db = True
        if self.id is None:
            self.id = id(self) % 10000
        return self


# ── require_http_methods tests ─────────────────────────────────────────────

print("=== HTTP Method Decorators ===")

from hyperdjango.response import Response
from hyperdjango.shortcuts import (
    require_GET,
    require_http_methods,
    require_POST,
    require_safe,
)


class FakeRequest:
    def __init__(self, method="GET"):
        self.method = method


loop = asyncio.new_event_loop()


@require_http_methods(["GET", "POST"])
async def get_post_view(request):
    return Response.json({"ok": True})


# Allowed methods
r = loop.run_until_complete(get_post_view(FakeRequest("GET")))
check("require_methods_get_allowed", r.status == 200)

r = loop.run_until_complete(get_post_view(FakeRequest("POST")))
check("require_methods_post_allowed", r.status == 200)

# Disallowed method
r = loop.run_until_complete(get_post_view(FakeRequest("DELETE")))
check("require_methods_delete_blocked", r.status == 405)
check("require_methods_allow_header", "Allow" in r.headers)


@require_GET
async def get_only_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(get_only_view(FakeRequest("GET")))
check("require_get_allowed", r.status == 200)

r = loop.run_until_complete(get_only_view(FakeRequest("POST")))
check("require_get_post_blocked", r.status == 405)

r = loop.run_until_complete(get_only_view(FakeRequest("HEAD")))
check("require_get_head_allowed", r.status == 200)


@require_POST
async def post_only_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(post_only_view(FakeRequest("POST")))
check("require_post_allowed", r.status == 200)

r = loop.run_until_complete(post_only_view(FakeRequest("GET")))
check("require_post_get_blocked", r.status == 405)


@require_safe
async def safe_view(request):
    return Response.json({"ok": True})


r = loop.run_until_complete(safe_view(FakeRequest("GET")))
check("require_safe_get", r.status == 200)

r = loop.run_until_complete(safe_view(FakeRequest("HEAD")))
check("require_safe_head", r.status == 200)

r = loop.run_until_complete(safe_view(FakeRequest("POST")))
check("require_safe_post_blocked", r.status == 405)

r = loop.run_until_complete(safe_view(FakeRequest("PUT")))
check("require_safe_put_blocked", r.status == 405)

# ── QuerySet method existence tests ────────────────────────────────────────

print("\n=== QuerySet Method Existence ===")

qs = QuerySet(MockModel)

# Check all new methods exist
check("has_get_or_create", callable(qs.get_or_create))
check("has_update_or_create", callable(qs.update_or_create))
check("has_bulk_update", callable(qs.bulk_update))
check("has_in_bulk", callable(qs.in_bulk))
check("has_aiterator", callable(qs.aiterator))
check("has_latest", callable(qs.latest))
check("has_earliest", callable(qs.earliest))
check("has_explain", callable(qs.explain))
check("has_select_for_update", callable(qs.select_for_update))

# ── on_commit existence test ───────────────────────────────────────────────

print("\n=== on_commit Existence ===")

from hyperdjango.database import Database

# Built via __new__ to skip pool setup: on_commit state now lives on the
# per-instance thread-local `_tx_depth`, which is lazily created on first
# access, so it works even without __init__.
db = Database.__new__(Database)
check("on_commit_method_exists", callable(db.on_commit))

# Test callback registration
callback_ran = False


def my_callback():
    global callback_ran
    callback_ran = True


db.on_commit(my_callback)
# Callbacks are stored on the thread-local transaction state, not a plain
# instance attribute — read them via the same accessor on_commit() uses.
check("on_commit_registered", len(db._get_on_commit_callbacks()) == 1)
check("on_commit_is_our_callback", db._get_on_commit_callbacks()[0] is my_callback)


# Test decorator usage
@db.on_commit
def another_callback():
    pass


check("on_commit_decorator", len(db._get_on_commit_callbacks()) == 2)

# ── Summary ────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"Parity features tests: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'=' * 60}")

loop.close()
sys.exit(0 if failed == 0 else 1)
