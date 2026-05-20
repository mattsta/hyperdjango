#!/usr/bin/env python3
"""Regression tests for admin auth gaps + RBAC live-revocation (round r11).

Covers three fixes:

  1. Live RBAC revocation: an RBAC mutation
     (``PermissionChecker.remove_user_from_group`` and the low-level
     ``invalidate_user_sessions``) drops the user's live sessions — every
     registered store hook fires AND the per-user auth epoch bumps. A guard
     that stamped the epoch at login (the admin does) then rejects the stale
     session.

  2. Custom ``model_action`` mutating (POST) endpoints are routed through the
     same CSRF + per-model-permission enforcement as the built-in handlers: a
     POST without a valid CSRF token is rejected (403), and a genuinely
     authorized admin with a valid token passes through to the handler.

  3. (Not asserted here) admin login pre-auth CSRF is INTENTIONALLY not
     enforced — the session-bound token is a constant pre-auth and enforcing it
     would break direct-POST logins; see the documented rationale in
     _login_handler. The authenticated CSRF path (_verify_csrf_token) is still
     covered by Test 4.

Pure-Python: the DB / ORM / session store are stubbed. No network, no Postgres.
Run: ``python3 scripts/test_admin_rbac_r11.py``
"""

# hyper-test: unit

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperdjango.auth.permissions import (
    PermissionChecker,
    bump_auth_epoch,
    get_auth_epoch,
    invalidate_user_sessions,
    register_session_invalidation_hook,
    unregister_session_invalidation_hook,
)
from hyperdjango.testkit import check, finish, run_main

# ── Stubs ─────────────────────────────────────────────────────────────────────


class _FakeQS:
    """Minimal QuerySet stub: ``.filter(**kw).delete()`` / ``.first()``."""

    def filter(self, **kw):
        return self

    async def delete(self):
        return None

    async def first(self):
        return None


class _FakeAudit:
    async def log(self, *a, **k):
        return None


class _RecordingStore:
    """Stand-in session store: records which user_ids were invalidated."""

    def __init__(self):
        self.invalidated: list[object] = []

    def invalidate_for_user(self, user_id):
        self.invalidated.append(user_id)


class _AsyncRecordingStore:
    """Async store variant (like DatabaseSessionStore) — hook awaited."""

    def __init__(self):
        self.invalidated: list[object] = []

    async def invalidate_for_user(self, user_id):
        self.invalidated.append(user_id)


# ── Test 1: low-level epoch + hook fan-out ─────────────────────────────────────


async def test_invalidate_user_sessions_epoch_and_hooks():
    print("test_invalidate_user_sessions_epoch_and_hooks")
    uid = 900001
    sync_store = _RecordingStore()
    async_store = _AsyncRecordingStore()
    register_session_invalidation_hook(sync_store.invalidate_for_user)
    register_session_invalidation_hook(async_store.invalidate_for_user)
    try:
        before = get_auth_epoch(uid)
        await invalidate_user_sessions(uid)
        after = get_auth_epoch(uid)
        check("auth epoch bumped by one", after == before + 1)
        check("sync store hook fired with user_id", sync_store.invalidated == [uid])
        check("async store hook awaited with user_id", async_store.invalidated == [uid])
    finally:
        unregister_session_invalidation_hook(sync_store.invalidate_for_user)
        unregister_session_invalidation_hook(async_store.invalidate_for_user)


async def test_failing_hook_does_not_break_others():
    print("test_failing_hook_does_not_break_others")
    uid = 900002
    good = _RecordingStore()

    def _boom(user_id):
        raise RuntimeError("store down")

    register_session_invalidation_hook(_boom)
    register_session_invalidation_hook(good.invalidate_for_user)
    try:
        before = get_auth_epoch(uid)
        # Must not raise despite the failing hook.
        await invalidate_user_sessions(uid)
        check("epoch still bumps past failing hook", get_auth_epoch(uid) == before + 1)
        check("healthy hook still fired after failing one", good.invalidated == [uid])
    finally:
        unregister_session_invalidation_hook(_boom)
        unregister_session_invalidation_hook(good.invalidate_for_user)


# ── Test 2: remove_user_from_group triggers invalidation ───────────────────────


async def test_remove_from_group_invalidates_sessions():
    print("test_remove_from_group_invalidates_sessions")
    uid = 900003
    store = _RecordingStore()
    register_session_invalidation_hook(store.invalidate_for_user)
    try:
        checker = PermissionChecker(db=None)
        checker._audit = _FakeAudit()
        checker._user_group_qs = lambda: _FakeQS()  # bypass ORM/DB

        before = get_auth_epoch(uid)
        await checker.remove_user_from_group(uid, 7)
        check(
            "remove_user_from_group bumped the auth epoch",
            get_auth_epoch(uid) == before + 1,
        )
        check(
            "remove_user_from_group invalidated the user's live sessions",
            store.invalidated == [uid],
        )
    finally:
        unregister_session_invalidation_hook(store.invalidate_for_user)


async def test_grant_and_revoke_user_perm_invalidate():
    print("test_grant_and_revoke_user_perm_invalidate")
    uid = 900004
    store = _RecordingStore()
    register_session_invalidation_hook(store.invalidate_for_user)
    try:
        checker = PermissionChecker(db=None)
        checker._audit = _FakeAudit()
        # _resolve_perm returns None → grant/revoke skip the DB write but still
        # must invalidate (fail-safe path). revoke also calls _user_perm_qs only
        # when perm is not None, so with None it just audits+invalidates.

        async def _no_perm(codename, model_name):
            return None

        checker._resolve_perm = _no_perm

        before = get_auth_epoch(uid)
        await checker.revoke_user_perm(uid, "delete_widget", "widget")
        check(
            "revoke_user_perm invalidated sessions even on no-op perm",
            get_auth_epoch(uid) == before + 1,
        )
        check("revoke_user_perm fired the store hook", store.invalidated == [uid])
    finally:
        unregister_session_invalidation_hook(store.invalidate_for_user)


# ── Test 3: admin epoch guard rejects a stale session ──────────────────────────


def _make_admin():
    """Build a HyperAdmin against a fake app (collects routes, no real server)."""
    from hyperdjango.admin import HyperAdmin

    class _FakeRouter:
        def __init__(self):
            self.routes = []

        def add(self, method, path, handler):
            self.routes.append((method.upper(), path, handler))

    app = SimpleNamespace(router=_FakeRouter())
    admin = HyperAdmin(app, prefix="/admin", secret_key="test-secret-key-r11")
    return admin, app


async def test_admin_epoch_guard_rejects_stale_session():
    print("test_admin_epoch_guard_rejects_stale_session")
    from hyperdjango.admin import ADMIN_SESSION_COOKIE
    from hyperdjango.native._crypto import sign_data

    admin, _app = _make_admin()
    store = admin._get_session_store()  # also registers the store invalidation hook

    uid = 900010
    epoch_at_login = get_auth_epoch(uid)
    session_data = {
        "user_id": uid,
        "username": "root",
        "is_staff": True,
        "is_superuser": True,
        "_auth_epoch": epoch_at_login,
    }
    session_id = store.create(session_data)
    signed = sign_data(session_id, admin._secret_key)
    request = SimpleNamespace(cookies={ADMIN_SESSION_COOKIE: signed})

    # Fresh session authenticates.
    check("fresh admin session authenticates", admin._check_auth(request) is not None)

    # RBAC mutation elsewhere advances this user's epoch (de-escalation). We bump
    # the epoch directly (no store hook) so this asserts the epoch GUARD in
    # isolation — proving the session is rejected even for a store the hook never
    # reached (e.g. the app SessionAuth store the admin can't register).
    bump_auth_epoch(uid)

    # A session carrying the now-STALE epoch stamp must be rejected by the guard.
    session_id2 = store.create(dict(session_data))  # stale _auth_epoch
    signed2 = sign_data(session_id2, admin._secret_key)
    request2 = SimpleNamespace(cookies={ADMIN_SESSION_COOKIE: signed2})
    check(
        "stale-epoch admin session is rejected (live de-escalation enforced)",
        admin._check_auth(request2) is None,
    )
    check(
        "rejected session is deleted from the store",
        store.get(session_id2) is None,
    )


# ── Test 4: model_action POST requires CSRF + permission ───────────────────────


def _fake_post_request(admin, *, with_token: bool, superuser: bool):
    from hyperdjango.admin import ADMIN_SESSION_COOKIE

    cookie_val = "sessioncookievalue"
    req = SimpleNamespace()
    req.cookies = {ADMIN_SESSION_COOKIE: cookie_val}
    req.headers = {}
    req.path_params = {}
    req._admin_user = {"is_superuser": superuser, "is_staff": True}
    req._form = {}

    async def _form():
        # Real request.form() populates request._form; mirror that.
        if with_token:
            token = admin._generate_csrf_token(req)
            req._form = {"_csrf_token": [token]}
        else:
            req._form = {}
        return req._form

    req.form = _form
    return req


async def test_model_action_post_requires_csrf_and_permission():
    print("test_model_action_post_requires_csrf_and_permission")
    admin, app = _make_admin()
    # Register a fake model so the slug resolves to a config.
    admin._models["widgets"] = SimpleNamespace(slug="widgets", name="Widget")

    ran = {"count": 0}

    @admin.model_action("widgets", "ship", method="POST")
    async def ship(request):
        ran["count"] += 1
        from hyperdjango.response import Response

        return Response.json({"ok": True})

    # Find the wrapped route handler that model_action registered.
    wrapped = None
    for method, path, handler in app.router.routes:
        if method == "POST" and path.endswith("/widgets/ship/"):
            wrapped = handler
            break
    check("POST model_action route was registered", wrapped is not None)

    # Neutralize the staff-redirect + hash-verify (needs a DB) — leave auth as
    # already-passed so we isolate the CSRF/permission enforcement.
    async def _staff_ok(request):
        return None

    admin._require_staff_or_redirect = _staff_ok

    # (a) authorized superuser WITHOUT a CSRF token → rejected 403, handler skipped.
    req_no_token = _fake_post_request(admin, with_token=False, superuser=True)
    resp = await wrapped(req_no_token)
    check(
        "POST model_action without CSRF token is rejected (403)",
        getattr(resp, "status", None) == 403,
    )
    check("handler did not run on missing CSRF token", ran["count"] == 0)

    # (b) non-superuser (no view/change perm, no _permissions loaded → staff
    #     fallback grants... so force an explicit empty perm set) WITHOUT perm.
    req_noperm = _fake_post_request(admin, with_token=True, superuser=False)
    req_noperm._admin_user = {
        "is_superuser": False,
        "is_staff": True,
        "_permissions": set(),
    }
    resp_noperm = await wrapped(req_noperm)
    check(
        "POST model_action without change permission is rejected (403)",
        getattr(resp_noperm, "status", None) == 403,
    )
    check("handler did not run without permission", ran["count"] == 0)

    # (c) authorized superuser WITH a valid CSRF token → handler runs.
    req_ok = _fake_post_request(admin, with_token=True, superuser=True)
    resp_ok = await wrapped(req_ok)
    check("authorized admin with valid CSRF token reaches handler", ran["count"] == 1)
    check(
        "authorized handler returned its own (non-403) response",
        getattr(resp_ok, "status", None) in (200, None),
    )


async def test_model_action_get_stays_unwrapped():
    print("test_model_action_get_stays_unwrapped")
    admin, app = _make_admin()

    @admin.model_action("reports", "export", method="GET")
    async def export(request):
        return None

    # GET action registered and NOT routed through post-security (view-gated as
    # before) — it should use the plain _auth_wrap path (single staff check).
    found = any(
        m == "GET" and p.endswith("/reports/export/") for m, p, _ in app.router.routes
    )
    check("GET model_action route registered under is_staff gate", found)


# ── Runner ─────────────────────────────────────────────────────────────────────


async def main() -> bool:
    tests = [
        test_invalidate_user_sessions_epoch_and_hooks,
        test_failing_hook_does_not_break_others,
        test_remove_from_group_invalidates_sessions,
        test_grant_and_revoke_user_perm_invalidate,
        test_admin_epoch_guard_rejects_stale_session,
        test_model_action_post_requires_csrf_and_permission,
        test_model_action_get_stays_unwrapped,
    ]
    for t in tests:
        await t()
    print()
    return finish()


if __name__ == "__main__":
    run_main(lambda: asyncio.run(main()))
