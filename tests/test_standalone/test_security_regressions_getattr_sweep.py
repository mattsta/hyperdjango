"""Persistent regression tests for the AUTH/ADMIN/SECURITY getattr/setattr sweep.

The sweep removed dynamic getattr/setattr in favour of direct access wherever a
type/presence assumption could be PROVEN. Each removal that touches an
authentication, permission, or token decision is pinned here so it can never
silently regress into the getattr-on-a-dict class of bug (which returns the
default and, historically, stripped superuser rights).

Run: uv run pytest tests/test_standalone/test_security_regressions_getattr_sweep.py -q
"""

import dataclasses

import pytest

from hyperdjango.auth.password_reset import PasswordResetTokenGenerator
from hyperdjango.auth.permissions import _is_superuser, _user_pk
from hyperdjango.auth.user import AnonymousUser, SessionUser
from hyperdjango.native._crypto import hmac_sha256_hex
from hyperdjango.request import Request
from hyperdjango.signing import SigningKey, TokenEngine

# ── 1. _user_pk resolves identity across every user shape the checker accepts ─
# _user_pk feeds the user_id parameter of EVERY permission SQL lookup. A wrong
# value silently grants/denies the wrong permissions, so its behaviour across
# the heterogeneous user objects is locked in here.


class _UserProxy:
    """Mirrors the admin/password-reset proxies: has .id but NOT .pk."""


def test_user_pk_reads_id_from_session_user():
    su = SessionUser({"id": 5, "username": "bob"})
    assert _user_pk(su) == 5


def test_user_pk_reads_pk_when_id_falls_back():
    # An object exposing only .pk (no .id attribute) must still resolve.
    class OnlyPk:
        pk = 11

    assert _user_pk(OnlyPk()) == 11


def test_user_pk_proxy_without_pk_still_resolves_via_id():
    # The admin UserProxy is built from a DB dict with "id" but no "pk"; the
    # id→pk fallback must not require .pk to exist when .id is truthy.
    up = _UserProxy()
    up.id = 9
    assert _user_pk(up) == 9


def test_user_pk_anonymous_is_none():
    # AnonymousUser has id=None and pk=None → no permissions ever attach.
    assert _user_pk(AnonymousUser()) is None


def test_user_pk_none_default_when_both_absent():
    # A user object exposing neither .id nor .pk must degrade to None (fail
    # closed: user_id None → empty permission set), never raise.
    class Bare:
        pass

    assert _user_pk(Bare()) is None


# ── 2. SessionUser materializes groups/permissions (direct-assignment path) ──
# is_superuser / is_staff / has_perm all derive from the frozensets built in
# __post_init__ via direct slot assignment (was object.__setattr__). If that
# materialization broke, a superuser session would lose every privilege.


def test_session_user_materializes_groups_and_permissions():
    su = SessionUser(
        {
            "id": 1,
            "groups": ["superuser", "staff", "editors"],
            "permissions": ["blog.add_post", "blog.change_post"],
        }
    )
    assert su.groups == frozenset({"superuser", "staff", "editors"})
    assert su.permissions == frozenset({"blog.add_post", "blog.change_post"})


def test_session_user_superuser_and_staff_derived_from_groups():
    su = SessionUser({"id": 1, "groups": ["superuser", "staff"]})
    assert su.is_superuser is True
    assert su.is_staff is True
    assert _is_superuser(su) is True
    assert su.has_perm("anything.at_all") is True  # superuser group grants all


def test_session_user_non_privileged_is_stripped():
    su = SessionUser({"id": 2, "groups": ["viewers"], "permissions": ["blog.view"]})
    assert su.is_superuser is False
    assert su.is_staff is False
    assert _is_superuser(su) is False
    assert su.has_perm("blog.add_post") is False
    assert su.has_perm("blog.view") is True


def test_session_user_empty_groups_default_to_empty_frozensets():
    su = SessionUser({"id": 3})
    assert su.groups == frozenset()
    assert su.permissions == frozenset()
    assert su.is_superuser is False


# ── 3. Password-reset token derives from declared Protocol fields ────────────
# make_token now reads user.id / user.password_hash / user.last_login directly
# (PasswordResetUser Protocol declares all three). These tests prove the direct
# reads yield the IDENTICAL token to the old getattr(...) form and that the
# security property (password change invalidates the token) still holds.


def _proxy(uid, password_hash, last_login=None):
    class P:
        pass

    p = P()
    p.id = uid
    p.password_hash = password_hash
    p.last_login = last_login
    return p


def test_password_reset_token_matches_old_getattr_expression():
    gen = PasswordResetTokenGenerator(secret_key="k")
    user = _proxy(7, "HASH", last_login=None)
    ts = 1000

    new = gen._make_token_with_timestamp(user, ts)

    # Reconstruct the pre-sweep expression byte-for-byte.
    uid = getattr(user, "id", getattr(user, "pk", 0))
    ph = getattr(user, "password_hash", "")
    ll = str(getattr(user, "last_login", ""))
    val = f"{uid}:{ph}:{ll}:{ts}"
    old = f"{ts}-{hmac_sha256_hex(gen.secret_key.encode(), val.encode())}"

    assert new == old


def test_password_reset_token_roundtrips():
    gen = PasswordResetTokenGenerator(secret_key="k", timeout=3600)
    user = _proxy(42, "HASH1", last_login=None)
    token = gen.make_token(user)
    assert gen.check_token(user, token) is True


def test_password_reset_token_invalidated_by_password_change():
    gen = PasswordResetTokenGenerator(secret_key="k", timeout=3600)
    token = gen.make_token(_proxy(42, "HASH1"))
    # Same id, new password_hash → token must no longer verify.
    assert gen.check_token(_proxy(42, "HASH2"), token) is False


# ── 4. Declared Request fields — direct access, never AttributeError ─────────
# The sweep removed getattr(request, "<field>", default) for fields that are now
# declared on Request. These pin that they are real fields defaulting to None.


def test_request_declares_perm_checker_and_admin_session_id():
    fields = {f.name for f in dataclasses.fields(Request)}
    assert "_perm_checker" in fields
    assert "_admin_session_id" in fields
    assert "session" in fields


def test_fresh_request_declared_fields_default_none():
    req = Request(method="GET", path="/", headers={}, query_string="")
    # require_permission reads request._perm_checker directly; unset → None →
    # 403 "Permission system not configured" (fail closed).
    assert req._perm_checker is None
    assert req._admin_session_id is None
    assert req.session is None


def test_request_is_secure_is_a_property_not_a_stored_attr():
    # oauth2 now reads request.is_secure directly (declared property).
    assert isinstance(Request.__dict__["is_secure"], property)
    req = Request(method="GET", path="/", headers={}, query_string="")
    assert req.is_secure is False  # no scheme/proxy header → http


# ── 5. TokenEngine builds its version lookup via direct slot assignment ──────
# _key_by_version drives decode-time key selection; if __post_init__ failed to
# populate it, every signed session/API-key token would fail to verify.


def test_token_engine_populates_key_by_version():
    eng = TokenEngine(
        keys=[
            SigningKey(secret="s" * 32, version=2),
            SigningKey(secret="t" * 32, version=1),
        ]
    )
    assert sorted(eng._key_by_version.keys()) == [1, 2]


def test_token_engine_ref_roundtrip_after_slot_assignment():
    eng = TokenEngine(keys=[SigningKey(secret="s" * 32, version=1)])
    token = eng.encode_ref("sess_abc123")
    assert eng.decode_ref(token) == "sess_abc123"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
