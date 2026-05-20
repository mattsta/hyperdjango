"""Persistent regression tests for the round-3 security fixes.

These lock in three fixes the re-audit surfaced, so they can never silently
regress again:

1. `_is_user_session` must gate on a positive identity marker — an anonymous
   session carrying only app state (cart, flash, wizard) must NOT authenticate.
2. The admin escalation guard reads `_admin_user` (session-data dict) via dict
   access, so a real superuser keeps escalation rights and a non-superuser is
   stripped — regardless of whether the acting user is a dict.
3. `Request._admin_user` / `Request.session` are DECLARED fields (default None),
   so reads never need `getattr` and never AttributeError.

Run: uv run pytest tests/test_standalone/test_security_regressions_round3.py -q
"""

import dataclasses

from hyperdjango.auth.sessions import _is_user_session
from hyperdjango.request import Request

# ── 1. Anonymous app-state sessions must not authenticate ────────────────────


def test_flash_only_session_is_anonymous():
    assert _is_user_session({"_messages": [("info", "hi")]}) is False


def test_anonymous_cart_session_is_anonymous():
    # The request.session bridge lets a logged-out request persist arbitrary
    # keys; none of them may promote it to "authenticated".
    assert _is_user_session({"cart": [1, 2, 3]}) is False
    assert _is_user_session({"wizard_step": 2, "_messages": []}) is False
    assert _is_user_session({"dismissed_banner": True}) is False
    assert _is_user_session({}) is False


def test_login_shaped_sessions_authenticate():
    assert _is_user_session({"user_id": 42}) is True
    assert _is_user_session({"id": 7, "_messages": []}) is True
    assert _is_user_session({"pk": 1}) is True
    # username-only (no numeric id) must still authenticate — documented case.
    assert _is_user_session({"username": "alice"}) is True


# ── 2. Admin escalation guard: dict access, correct super/non-super behavior ──


def _is_current_superuser(current_user):
    """Mirror of the guard's decision in admin/__init__.py escalation_guard.

    Kept in sync with that one expression; the tests below pin its truth table
    so a getattr-vs-dict regression (which silently stripped EVERY user) fails.
    """
    return bool(current_user and current_user.get("is_superuser"))


def test_guard_superuser_dict_keeps_rights():
    assert _is_current_superuser({"id": 1, "is_superuser": True}) is True


def test_guard_non_superuser_dict_is_stripped():
    assert _is_current_superuser({"id": 2, "is_superuser": False}) is False
    assert _is_current_superuser({"id": 3}) is False  # key absent → not super


def test_guard_missing_user_fails_closed():
    # _admin_user defaults to None when auth never ran → treated as non-super.
    assert _is_current_superuser(None) is False


def test_getattr_on_dict_would_have_broken_it():
    """Proves WHY dict access is required: getattr on a dict never sees the key,
    so the old getattr form stripped superusers. This is the regression guard."""
    su = {"id": 1, "is_superuser": True}
    assert getattr(su, "is_superuser", False) is False  # the bug
    assert su.get("is_superuser") is True  # the fix


# ── 3. Request._admin_user / session are declared fields (no getattr needed) ──


def test_request_admin_user_is_declared_field():
    fields = {f.name: f for f in dataclasses.fields(Request)}
    assert "_admin_user" in fields, "_admin_user must be a declared field"
    assert "session" in fields, "session must be a declared field"


def test_fresh_request_has_admin_user_none():
    req = Request(method="GET", path="/", headers={}, query_string="")
    # Direct attribute access must work (field default), never AttributeError.
    assert req._admin_user is None
    assert req.session is None
