# hyper-test: unit
"""
Regression tests for round-12 fix-wave findings #18 and #20.

  #18  Unified error-body contract. Several class-based views, the versioning
       cache-bust endpoint, and the VersionRouterMiddleware emitted a bespoke
       ``{"error": ...}`` body with NO ``status`` field, diverging from the
       framework's single error shape ``{"detail", "status"}`` (produced by
       exception_to_response / ratelimit / admin / CSRF). Each is converted to
       the unified shape (keeping status codes and any Allow header).

  #20  Query-cache invalidation vs. transactions. post_save/post_delete bumped
       the table version INLINE. Inside ``async with db.transaction():`` that
       bump happens BEFORE COMMIT, so a concurrent reader between the bump and
       the commit re-populates the NEW version key with the OLD committed rows
       -> stale for the whole TTL. Fix: when a transaction is active, DEFER the
       invalidation via ``db.on_commit(...)`` (fires on commit, discarded on
       rollback); keep the inline path for autocommit.

Usage:
    uv run hyper-test contract_and_commitcache_r12
"""

import asyncio
import inspect
import json
import sys
import traceback
from unittest.mock import patch

import hyperdjango.query_cache as qc_module
import hyperdjango.versioning as versioning_module
from hyperdjango.query_cache import set_query_cache
from hyperdjango.signals import post_delete, post_save
from hyperdjango.standalone_middleware import VersionRouterMiddleware
from hyperdjango.versioning import _cache_bust_handler
from hyperdjango.views import DetailView, PermissionRequiredMixin, View

RESULTS = {"passed": 0, "failed": 0, "errors": []}


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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, headers=None, cookies=None, user=None, method="GET"):
        self.method = method
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.user = user


class FakeUser:
    def __init__(self, authenticated=True, perms=()):
        self.is_authenticated = authenticated
        self._perms = set(perms)

    def has_perm(self, perm):
        return perm in self._perms


def _body(resp):
    return json.loads(resp.body)


def _assert_unified(resp, expected_status):
    body = _body(resp)
    assert "error" not in body, f"legacy 'error' key still present: {body}"
    assert "detail" in body, f"missing 'detail': {body}"
    assert "status" in body, f"missing 'status': {body}"
    assert body["status"] == expected_status, f"status mismatch: {body}"
    assert resp.status == expected_status, f"resp.status mismatch: {resp.status}"


# ---------------------------------------------------------------------------
# #18 — unified error body across the outliers
# ---------------------------------------------------------------------------


@test("#18 View.http_method_not_allowed -> unified body + Allow header")
def t_method_not_allowed():
    v = View()
    resp = v.http_method_not_allowed(FakeRequest(method="PATCH"))
    _assert_unified(resp, 405)
    assert "Allow" in resp.headers, f"Allow header dropped: {resp.headers}"


@test("#18 DetailView 404 (object missing) -> unified body")
async def t_detail_not_found():
    class NoObjDetail(DetailView):
        async def get_object(self, **kwargs):
            return None

    v = NoObjDetail()
    resp = await v.get(FakeRequest())
    _assert_unified(resp, 404)


@test("#18 PermissionRequiredMixin 401 (unauthenticated) -> unified body")
async def t_perm_401():
    class PV(PermissionRequiredMixin, View):
        permission_required = "edit"

    v = PV()
    resp = await v.dispatch(FakeRequest(user=None))
    _assert_unified(resp, 401)


@test("#18 PermissionRequiredMixin 403 (missing perm) -> unified body")
async def t_perm_403():
    class PV(PermissionRequiredMixin, View):
        permission_required = "edit"

    v = PV()
    resp = await v.dispatch(FakeRequest(user=FakeUser(authenticated=True, perms=())))
    _assert_unified(resp, 403)


@test("#18 cache-bust 401 (no Bearer token) -> unified body")
def t_cachebust_401():
    resp = _cache_bust_handler(FakeRequest(headers={}))
    _assert_unified(resp, 401)


@test("#18 cache-bust 403 (invalid token) -> unified body")
def t_cachebust_403():
    def fake_get_setting(name, default=""):
        return "topsecret" if name == "SECRET_KEY" else default

    with patch.object(versioning_module, "get_setting", fake_get_setting):
        resp = _cache_bust_handler(
            FakeRequest(headers={"authorization": "Bearer not-the-right-token"})
        )
    _assert_unified(resp, 403)


@test("#18 VersionRouterMiddleware 409 (unknown version) -> unified body")
async def t_version_409():
    mw = VersionRouterMiddleware(version_map={"v1": "backend-v1"}, default_version="v1")

    async def call_next(req):
        raise AssertionError("call_next must not run for an unknown version")

    resp = await mw(FakeRequest(headers={"x-client-version": "v999"}), call_next)
    _assert_unified(resp, 409)


# ---------------------------------------------------------------------------
# #20 — invalidation deferred to on_commit when a transaction is active
# ---------------------------------------------------------------------------


class FakeTxDepth:
    def __init__(self, depth):
        self.depth = depth


class FakeDB:
    """Duck-types the bits of Database that _active_transaction_db() reads,
    plus on_commit and a commit/rollback driver."""

    def __init__(self, in_transaction):
        # Single-flow-loop path: depth > 0 means a transaction is active.
        self._tx_depth = FakeTxDepth(1 if in_transaction else 0)
        self._pending = []

    def _task_tx(self):
        # Multiplexing path unused in this test.
        return None

    def in_transaction(self):
        # Public authority the query-cache invalidation path now consults
        # (query_cache.py -> db.in_transaction()). Mirror the real
        # Database.in_transaction() contract: task-scoped tx first, else the
        # thread-local BEGIN/COMMIT nesting depth.
        if self._task_tx() is not None:
            return True
        return self._tx_depth.depth > 0

    def on_commit(self, cb):
        self._pending.append(cb)
        return cb

    def run_commit(self):
        pending = self._pending[:]
        self._pending.clear()
        for cb in pending:
            cb()

    def run_rollback(self):
        # database.py discards on_commit callbacks on rollback.
        self._pending.clear()


class RecordingCache:
    def __init__(self):
        self.calls = []
        self._enabled = True

    def invalidate_row(self, table, pk):
        self.calls.append(("row", table, pk))

    def invalidate_table(self, table):
        self.calls.append(("table", table))

    def invalidate_all(self):
        self.calls.append(("all",))


class FakeMeta:
    def __init__(self, table):
        self.table = table


class FakeInstance:
    def __init__(self, table="widgets", pk=42):
        self._meta = FakeMeta(table)
        self.pk = pk


def _receiver(signal, uid):
    """Fetch the query_cache receiver by dispatch_uid, so we exercise ONLY it
    (never other subsystems' receivers) with a controlled fake instance."""
    for key, recv, _is_async in signal._receivers:
        if key == uid:
            return recv
    raise AssertionError(f"receiver {uid!r} not connected")


class _CommitCacheHarness:
    """Installs a RecordingCache + patched get_db(FakeDB) around one send."""

    def __init__(self, in_transaction):
        self.db = FakeDB(in_transaction=in_transaction)
        self.cache = RecordingCache()

    def __enter__(self):
        self._saved = qc_module._query_cache_manager
        set_query_cache(self.cache)
        self._patch = patch("hyperdjango.database.get_db", lambda: self.db)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        qc_module._query_cache_manager = self._saved
        return False


@test("#20 post_save inside a transaction DEFERS invalidation to on_commit")
def t_defer_on_commit():
    with _CommitCacheHarness(in_transaction=True) as h:
        _receiver(post_save, "query_cache_post_save")(None, instance=FakeInstance())
        # NOT invalidated yet — must wait for COMMIT.
        assert h.cache.calls == [], f"invalidated before commit: {h.cache.calls}"
        assert len(h.db._pending) == 1, "no on_commit callback registered"
        # Commit -> invalidation fires now.
        h.db.run_commit()
        assert h.cache.calls == [("row", "widgets", 42)], (
            f"invalidation did not fire on commit: {h.cache.calls}"
        )


@test("#20 rollback discards the deferred invalidation (rows unchanged)")
def t_no_invalidate_on_rollback():
    with _CommitCacheHarness(in_transaction=True) as h:
        _receiver(post_delete, "query_cache_post_delete")(None, instance=FakeInstance())
        assert h.cache.calls == [], f"invalidated before commit: {h.cache.calls}"
        assert len(h.db._pending) == 1, "no on_commit callback registered"
        h.db.run_rollback()
        assert h.cache.calls == [], f"invalidated on rollback: {h.cache.calls}"
        assert h.db._pending == [], "callback not discarded on rollback"


@test("#20 autocommit (no transaction) invalidates INLINE, no deferral")
def t_inline_when_autocommit():
    with _CommitCacheHarness(in_transaction=False) as h:
        _receiver(post_save, "query_cache_post_save")(None, instance=FakeInstance())
        assert h.cache.calls == [("row", "widgets", 42)], (
            f"expected inline invalidation: {h.cache.calls}"
        )
        assert h.db._pending == [], "should not defer when no transaction is active"


@test("#20 no DB configured (get_db raises) still invalidates inline")
def t_inline_when_no_db():
    saved = qc_module._query_cache_manager
    cache = RecordingCache()
    set_query_cache(cache)

    def boom():
        raise RuntimeError("No database configured")

    try:
        with patch("hyperdjango.database.get_db", boom):
            _receiver(post_save, "query_cache_post_save")(None, instance=FakeInstance())
        assert cache.calls == [("row", "widgets", 42)], (
            f"expected inline invalidation fallback: {cache.calls}"
        )
    finally:
        qc_module._query_cache_manager = saved


async def main():
    all_tests = [
        obj
        for _name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print("\n═══ Contract + Commit-Cache Round-12 Fix-Wave Tests ═══")
    for t in all_tests:
        await t()

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
    sys.exit(0 if asyncio.run(main()) else 1)
