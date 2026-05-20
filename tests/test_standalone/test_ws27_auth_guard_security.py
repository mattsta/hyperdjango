"""ws27 authorization/auth-logic security regressions.

Each test locks in one fix from the ws27 audit. Grouped by item number.

Run: uv run pytest tests/test_standalone/test_ws27_auth_guard_security.py -q
"""

from unittest.mock import patch

import pytest

from hyperdjango.auth.user import AnonymousUser, SessionUser

# ── Test doubles ─────────────────────────────────────────────────────────────


class _Req:
    """Minimal request stand-in for middleware / guard evaluation."""

    def __init__(self, user=None, cookies=None):
        self.user = user
        self.cookies = cookies or {}


async def _handler(request):
    return object()


# ── Item 1: anonymous representation is consistent + safe ────────────────────


async def test_session_auth_anon_user_not_authenticated():
    from hyperdjango.auth.sessions import SessionAuth

    auth = SessionAuth(secret="x" * 32)
    req = _Req()
    await auth(req, _handler)

    # An anonymous request must expose a real AnonymousUser() sentinel (never
    # None), so any guard/permission class reading request.user.is_authenticated
    # gets False instead of AttributeError-ing on None (historically swallowed
    # into an allow).
    assert req.user is not None
    assert isinstance(req.user, AnonymousUser)
    assert req.user.is_authenticated is False
    # The async accessor resolves to the same anonymous user.
    assert (await req.auser()) is req.user


async def test_session_auth_installs_perm_checker_only_with_db():
    """@require_permission needs request._perm_checker; SessionAuth installs it
    only when given db=. Without db the field stays None (routes 403)."""
    from hyperdjango.auth.sessions import SessionAuth

    r_nodb = _Req()
    await SessionAuth(secret="s" * 32)(r_nodb, _handler)
    assert r_nodb._perm_checker is None

    class _FakeDB:
        pass

    r_db = _Req()
    await SessionAuth(secret="s" * 32, db=_FakeDB())(r_db, _handler)
    assert r_db._perm_checker is not None


# ── Item 6: OAuth2 must not trust an unverified provider email ────────────────


def _google():
    from hyperdjango.auth.oauth2 import google

    return google(client_id="id", client_secret="sec")


def _github():
    from hyperdjango.auth.oauth2 import github

    return github(client_id="id", client_secret="sec")


def test_oauth_unverified_oidc_email_not_trusted():
    from hyperdjango.auth.oauth2 import extract_user_data

    # No email_verified claim at all → not trusted.
    data = extract_user_data(_google(), {"sub": "1", "email": "victim@corp.com"})
    assert data["email"] == ""
    assert data["email_verified"] is False

    # Explicit email_verified False → not trusted.
    data = extract_user_data(
        _google(), {"sub": "1", "email": "victim@corp.com", "email_verified": False}
    )
    assert data["email"] == ""
    assert data["email_verified"] is False


def test_oauth_verified_oidc_email_trusted():
    from hyperdjango.auth.oauth2 import extract_user_data

    data = extract_user_data(
        _google(), {"sub": "1", "email": "real@corp.com", "email_verified": True}
    )
    assert data["email"] == "real@corp.com"
    assert data["email_verified"] is True

    # Providers that stringify the claim ("true") are honored too.
    data = extract_user_data(
        _google(), {"sub": "1", "email": "real@corp.com", "email_verified": "true"}
    )
    assert data["email"] == "real@corp.com"
    assert data["email_verified"] is True


def test_oauth_github_requires_verified_primary_email():
    from hyperdjango.auth.oauth2 import extract_user_data

    # /user email present but no verified emails list → not trusted.
    prof = {"id": 7, "login": "octo", "email": "octo@corp.com"}
    data = extract_user_data(_github(), prof)
    assert data["email"] == ""
    assert data["email_verified"] is False

    # Verified primary present in the /user/emails list → trusted (that one).
    prof_ok = {
        "id": 7,
        "login": "octo",
        "email": "octo@corp.com",
        "emails": [
            {"email": "unverified@evil.com", "primary": False, "verified": False},
            {"email": "verified@corp.com", "primary": True, "verified": True},
        ],
    }
    data = extract_user_data(_github(), prof_ok)
    assert data["email"] == "verified@corp.com"
    assert data["email_verified"] is True

    # Only an unverified primary → still not trusted.
    prof_bad = {
        "id": 7,
        "login": "octo",
        "emails": [{"email": "x@evil.com", "primary": True, "verified": False}],
    }
    data = extract_user_data(_github(), prof_bad)
    assert data["email"] == ""
    assert data["email_verified"] is False


# ── Item 7: password-reset tokens must use a real secret ─────────────────────


def test_password_reset_rejects_placeholder_secret():
    from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

    for bad in ("change-me", "", "  ", "CHANGE-ME"):
        with pytest.raises(ValueError):
            PasswordResetTokenGenerator(secret_key=bad)


def test_password_reset_helper_rejects_placeholder():
    from hyperdjango.auth import password_reset as pr

    with pytest.raises(ValueError):
        pr.get_token_generator("change-me")


def test_password_reset_real_secret_roundtrips():
    from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

    class _U:
        id = 42
        password_hash = "argon2$abc"
        last_login = None

    gen = PasswordResetTokenGenerator(secret_key="a-strong-random-secret-value-1234")
    token = gen.make_token(_U())
    assert gen.check_token(_U(), token) is True
    assert gen.check_token(_U(), "0-deadbeef") is False


# ── Item 8: DatabaseSessionStore writes the correct session_hash key ─────────


async def test_db_session_store_uses_canonical_hash_key():
    from hyperdjango.auth import db_sessions
    from hyperdjango.auth.sessions import _SESSION_HASH_KEY

    assert _SESSION_HASH_KEY == "_session_auth_hash"

    captured = {}

    class _FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def save(self):
            return None

    store = db_sessions.DatabaseSessionStore()
    with patch.object(db_sessions, "HyperSession", _FakeSession):
        await store.create({"id": 1, _SESSION_HASH_KEY: "the-real-hash"})

    # The hash written to the column must be the one SessionAuth actually stores,
    # not "" (which would make invalidate_by_hash delete every session).
    assert captured["session_hash"] == "the-real-hash"


# ── Item 2: mixed AND/OR must not over-allow ─────────────────────────────────


def _field(source, field, op, value):
    from hyperdjango.guard.parser import ConditionOp, FieldConditionAST

    return FieldConditionAST(
        source=source, field=field, op=ConditionOp(op), value=value
    )


def _rule(conditions, or_indices, action="read"):
    from hyperdjango.guard.parser import RuleAST, RuleEffect

    return RuleAST(
        effect=RuleEffect.ALLOW,
        action=action,
        conditions=tuple(conditions),
        or_indices=frozenset(or_indices),
        line=1,
    )


def test_parser_rejects_or_on_first_condition():
    from hyperdjango.guard.parser import ParseError, parse_policy

    src = "resource Doc {\n allow read where {\n OR user.level = 5\n }\n}\n"
    with pytest.raises(ParseError):
        parse_policy(src)


def test_validator_flags_mixed_or_positions():
    from hyperdjango.guard.parser import PolicyAST, ResourceAST
    from hyperdjango.guard.validator import validate_policy

    conds = [
        _field("user", "level", "=", 5),
        _field("resource", "open", "=", True),
        _field("resource", "vip", "=", True),
    ]

    def _validate(or_indices):
        ast = PolicyAST(
            resources=(
                ResourceAST(name="Doc", rules=(_rule(conds, or_indices),), line=1),
            ),
            source_path="<t>",
        )
        return validate_policy(ast).is_valid

    # {0, 2}: old count-based check accepted this (count 2 == n-1) though it is
    # genuinely mixed. Position check must reject it.
    assert _validate({0, 2}) is False
    # {2}: (A AND B) OR C — mixed, rejected.
    assert _validate({2}) is False
    # Pure OR (all non-first) and pure AND are the only accepted shapes.
    assert _validate({1, 2}) is True
    assert _validate(set()) is True


def test_mixed_rule_compiles_fail_closed():
    from hyperdjango.guard.parser import ResourceAST
    from hyperdjango.guard.registry import _compile_resource, _compile_rule
    from hyperdjango.guard.sql import generate_where

    conds = [
        _field("user", "level", "=", 5),
        _field("resource", "open", "=", True),
        _field("resource", "vip", "=", True),
    ]

    # Mixed "(A AND B) OR C" must NOT compile to a flat combine (which would be
    # the over-allowing "A OR B OR C"). It is deferred to Python instead.
    mixed = _compile_rule(_rule(conds, {2}))
    assert mixed.needs_python is True
    assert mixed.compiled is None

    # Pure OR still compiles to bytecode.
    pure_or = _compile_rule(_rule(conds, {1, 2}))
    assert pure_or.compiled is not None

    # And through the SQL generator, the mixed rule emits no allow fragment, so
    # the WHERE fails closed to deny-all rather than the wrong OR clause.
    res = _compile_resource(ResourceAST(name="Doc", rules=(_rule(conds, {2}),), line=1))
    frag = generate_where(res, "read", user_fields={"level": 5})
    assert frag.sql == "FALSE"

    # Contrast: the pure-OR variant produces a real (permissive) WHERE.
    res_or = _compile_resource(
        ResourceAST(name="Doc", rules=(_rule(conds, {1, 2}),), line=1)
    )
    frag_or = generate_where(res_or, "read", user_fields={"level": 5})
    assert frag_or.sql != "FALSE"


# ── Item 3: deny rules inapplicable without context must fail closed ─────────


def test_evaluators_return_inapplicable_without_context():
    from hyperdjango.auth.permissions import (
        INAPPLICABLE,
        _eval_field_match,
        _eval_ip_range,
    )
    from hyperdjango.auth.user import FieldMatchConfig, IpRangeConfig

    assert (
        _eval_field_match(None, FieldMatchConfig(field_name="status", values=["x"]))
        is INAPPLICABLE
    )
    assert _eval_ip_range(None, IpRangeConfig(ranges=["10.0.0.0/8"])) is INAPPLICABLE


async def test_deny_only_rule_without_obj_fails_closed():
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import FieldMatchConfig

    checker = PermissionChecker(db=None)

    async def _has_perm(user, perm, model_name=None):
        return True  # model-level permission granted

    deny_rule = {
        "rule_type": "field_match",
        "rule_config": FieldMatchConfig(field_name="status", values=["published"]),
        "is_deny": True,
        "priority": 0,
    }

    async def _load_rules(user, codename, model_name):
        return [deny_rule]

    checker.has_perm = _has_perm
    checker._load_rules = _load_rules

    user = SessionUser({"id": 1})

    # obj=None → the field_match DENY cannot be evaluated. It must FAIL CLOSED
    # (deny), not fall through to the granted model perm.
    allowed = await checker.has_perm_with_rules(
        user, "view", "post", obj=None, request=None
    )
    assert allowed is False

    # Sanity: with an obj that does NOT match, the deny legitimately does not
    # fire, and the model-level grant stands.
    allowed_ok = await checker.has_perm_with_rules(
        user, "view", "post", obj={"status": "draft"}, request=None
    )
    assert allowed_ok is True

    # And an obj that DOES match fires the deny.
    denied = await checker.has_perm_with_rules(
        user, "view", "post", obj={"status": "published"}, request=None
    )
    assert denied is False


# ── Item 4: not_banned must fail closed on a real DB error ───────────────────


def _authed_request(**data):
    data.setdefault("id", 99)
    return _Req(user=SessionUser(data))


class _RaisingTimeline:
    def __init__(self, exc):
        self._exc = exc

    async def active_statuses(self, entity, entity_id):
        raise self._exc


async def test_not_banned_fails_closed_on_db_error():
    from hyperdjango.guard.requirements import Require
    from hyperdjango.guard.types import GuardContext

    req = Require.not_banned()
    request = _authed_request()

    # Real DB error → deny (fail closed).
    with patch(
        "hyperdjango.guard.requirements.get_timeline",
        return_value=_RaisingTimeline(ValueError("db")),
    ):
        denial = await req.evaluate_fn(request, GuardContext())
    assert denial is not None

    # No DB configured (get_timeline raises RuntimeError) → allow.
    def _no_db():
        raise RuntimeError("no database configured")

    with patch("hyperdjango.guard.requirements.get_timeline", side_effect=_no_db):
        allow = await req.evaluate_fn(_authed_request(), GuardContext())
    assert allow is None


async def test_not_muted_fails_closed_on_db_error():
    from hyperdjango.guard.requirements import Require
    from hyperdjango.guard.types import GuardContext

    req = Require.not_muted()
    with patch(
        "hyperdjango.guard.requirements.get_timeline",
        return_value=_RaisingTimeline(ValueError("db")),
    ):
        denial = await req.evaluate_fn(_authed_request(), GuardContext())
    assert denial is not None


# ── Item 5a: field_access defaults to the most restrictive level ─────────────


async def test_field_access_absent_field_denied():
    from hyperdjango.guard.requirements import Require
    from hyperdjango.guard.types import GuardContext

    # Field not present in the map → must default to hidden (deny), not writable.
    req = Require.field_access("secret", "employee", level="readonly")
    request = _authed_request(field_access={"employee": {}})
    assert await req.evaluate_fn(request, GuardContext()) is not None


async def test_field_access_unknown_level_denied():
    from hyperdjango.guard.requirements import Require
    from hyperdjango.guard.types import GuardContext

    # A typo'd stored level ("writeable") must not fail open to writable.
    req = Require.field_access("secret", "employee", level="readonly")
    request = _authed_request(field_access={"employee": {"secret": "writeable"}})
    assert await req.evaluate_fn(request, GuardContext()) is not None


async def test_field_access_explicit_writable_allowed():
    from hyperdjango.guard.requirements import Require
    from hyperdjango.guard.types import GuardContext

    req = Require.field_access("bio", "employee", level="readonly")
    request = _authed_request(field_access={"employee": {"bio": "writable"}})
    assert await req.evaluate_fn(request, GuardContext()) is None


# ── Item 5b: require_permission codename must scope to the model ──────────────


async def test_has_perm_scopes_codename_to_model():
    from hyperdjango.auth.permissions import PermissionChecker

    checker = PermissionChecker(db=None)

    async def _perms(user):
        return {"articleB.publish"}

    checker._get_all_permissions = _perms
    user = SessionUser({"id": 1})

    # Scoped to the wrong model → denied (no cross-model bleed).
    assert await checker.has_perm(user, "publish", "articleA") is False
    # Fully-qualified "model.codename" is self-scoping → denied for articleA.
    assert await checker.has_perm(user, "articleA.publish") is False
    # Correct model → allowed.
    assert await checker.has_perm(user, "articleB.publish") is True
    # Bare codename with no model still bleeds across models — the reason
    # callers must scope. (Documents the residual unscoped behavior.)
    assert await checker.has_perm(user, "publish") is True
