#!/usr/bin/env python3
"""Round-8 security / serializer-validation regression suite.

Pure-Python, runs against the installed extension (no live DB). Covers the
confirmed findings fixed in this round:

  #1 ObjectPermission grants anon access to null/missing-owner rows (rest.py)
  #2 Anon session with a planted username is promoted to authenticated
     (auth/sessions.py + auth/user.py SessionUser.is_authenticated)
  #3 The TypedField serializer layer was INERT — to_internal_value/
     to_representation were never called; PK relational field never validated
     existence (rest.py + serializers.py)
  #4 str-typed field stringified None/list/dict into "None"/repr (serializers.py)
  #5 Nested serializer ignored partial/context on PATCH (serializers.py)
  #6 get_object() converted every failure into a silent 404 (rest.py)

Run: uv run python scripts/test_security_serializer_r8.py
"""

# hyper-test: unit

import asyncio
import datetime

from hyperdjango.auth.sessions import (
    _AUTH_IDENTITY_KEYS,
    _is_user_session,
    _SessionDict,
)
from hyperdjango.auth.user import AnonymousUser, SessionUser
from hyperdjango.models import Model
from hyperdjango.rest import (
    ChoiceField,
    DateTimeField,
    EmailField,
    NotFound,
    ObjectPermission,
    PrimaryKeyRelatedField,
    UUIDField,
    ViewSet,
)
from hyperdjango.serializers import Serializer, SerializerField
from hyperdjango.testkit import check, finish, run_main


class _FakeRequest:
    def __init__(self, user):
        self.user = user


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


# ── #1 ObjectPermission null-owner guard ─────────────────────────────────────


async def test_object_permission():
    print("#1 ObjectPermission null/missing-owner")
    perm = ObjectPermission()

    anon = _FakeRequest(AnonymousUser())  # .id is None
    # Anon must NOT own a null-owner row (None == None must not grant).
    check(
        "anon denied on owner_id=None row",
        await perm.has_object_permission(anon, None, {"owner_id": None}) is False,
    )
    check(
        "anon denied on owned row",
        await perm.has_object_permission(anon, None, {"owner_id": 5}) is False,
    )

    authed = _FakeRequest(_FakeUser(5))
    check(
        "owner granted on own row",
        await perm.has_object_permission(authed, None, {"owner_id": 5}) is True,
    )
    check(
        "authed user denied on null-owner row",
        await perm.has_object_permission(authed, None, {"owner_id": None}) is False,
    )
    check(
        "non-owner denied",
        await perm.has_object_permission(authed, None, {"owner_id": 9}) is False,
    )


# ── #2 session identity allow-list + SessionUser.is_authenticated ────────────


def test_session_auth():
    # The escalation is closed at the request.session BRIDGE (an anonymous
    # request cannot WRITE identity/authorization keys), NOT by removing keys
    # from the identity allow-list — that would break username/pk auth pinned by
    # test_security_regressions_round3, and group-only authorization pinned by
    # test_rbac_guards. See scripts/test_session_escalation_guard_r8.py.
    print("#2 anon request cannot plant identity via the session bridge")

    # Identity allow-list contract preserved (round-3): user_id/id/pk/username.
    for key in ("user_id", "id", "pk", "username"):
        check(f"{key} is an identity key", key in _AUTH_IDENTITY_KEYS)
        check(f"{key} session authenticates", _is_user_session({key: 1}) is True)

    # SessionUser is authenticated by construction (group-only authz is valid).
    check(
        "SessionUser(username) authed by construction",
        SessionUser({"username": "alice"}).is_authenticated is True,
    )
    check(
        "SessionUser(group-only) authed by construction",
        SessionUser({"groups": ["admin"]}).is_authenticated is True,
    )

    # THE FIX: an application/anonymous write of an identity key via the bridge
    # is dropped, so it can never load back as trusted state and self-escalate.
    s = _SessionDict({"cart": [1]})
    s["user_id"] = 999
    s["groups"] = ["superuser"]
    check("bridge drops planted user_id", "user_id" not in s)
    check("bridge drops planted groups", "groups" not in s)
    check("bridge keeps non-reserved app state", s.get("cart") == [1])


# ── #3 typed fields actually validate/coerce + relational existence ──────────


class TypedS(Serializer):
    email: str = EmailField()
    role: str = ChoiceField(choices=["admin", "user"])
    created: str = DateTimeField()
    uid: str = UUIDField(required=False)


async def test_typed_fields():
    print("#3 typed field validation/coercion is live")
    good = TypedS(
        input_data={
            "email": "a@b.com",
            "role": "admin",
            "created": "2020-01-02T03:04:05",
        }
    )
    check("valid typed input accepted", good.is_valid())
    if good.is_valid():
        vd = good.validated_data
        check(
            "DateTimeField coerced to datetime",
            isinstance(vd.get("created"), datetime.datetime),
        )

    bad_email = TypedS(
        input_data={"email": "not-an-email", "role": "admin", "created": "2020-01-02"}
    )
    check("EmailField rejects bad email", not bad_email.is_valid())
    check("email error recorded", "email" in bad_email.errors)

    bad_choice = TypedS(
        input_data={"email": "a@b.com", "role": "root", "created": "2020-01-02"}
    )
    check("ChoiceField rejects out-of-choice", not bad_choice.is_valid())

    bad_dt = TypedS(
        input_data={"email": "a@b.com", "role": "user", "created": "not-a-date"}
    )
    check("DateTimeField rejects bad datetime", not bad_dt.is_valid())

    # to_representation on output
    out = TypedS(
        obj={
            "email": "a@b.com",
            "role": "user",
            "created": datetime.datetime(2020, 1, 2, 3, 4, 5),
            "uid": None,
        }
    )
    check(
        "DateTimeField.to_representation emits ISO string on output",
        out.data.get("created") == "2020-01-02T03:04:05",
    )


class _FakeQS:
    """Minimal async queryset: .get(id=) hits an in-memory id set."""

    def __init__(self, existing):
        self.existing = set(existing)
        self._model = Model

    async def get(self, **kwargs):
        pk = kwargs.get("id")
        if pk in self.existing:
            return {"id": pk}
        raise Model.DoesNotExist(f"no row id={pk}")


class RelS(Serializer):
    author_id: int = PrimaryKeyRelatedField(queryset=_FakeQS({1, 2}))


async def test_relational_field():
    print("#3b PrimaryKeyRelatedField validates FK existence")
    ok = RelS(input_data={"author_id": 1})
    check("PK field passes sync is_valid()", ok.is_valid())
    check("existing FK accepted by avalidate_relations", await ok.avalidate_relations())

    missing = RelS(input_data={"author_id": 99})
    check("non-existent PK passes sync phase (coercion only)", missing.is_valid())
    check(
        "non-existent FK rejected by avalidate_relations",
        not await missing.avalidate_relations(),
    )
    check("FK error recorded on the field", "author_id" in missing.errors)


# ── #4 str field rejects None/list/dict ──────────────────────────────────────


class StrS(Serializer):
    username: str = SerializerField()


def test_str_field():
    print("#4 str field rejects non-string structured/None input")
    for bad, label in [
        (None, "null"),
        ([1, 2], "list"),
        ({"a": 1}, "dict"),
        (True, "bool"),
    ]:
        s = StrS(input_data={"username": bad})
        check(f"str field rejects {label}", not s.is_valid())

    ok = StrS(input_data={"username": "alice"})
    check("str accepts str", ok.is_valid() and ok.validated_data["username"] == "alice")
    # numeric scalar still coerces
    num = StrS(input_data={"username": 123})
    check(
        "str still coerces numeric scalar",
        num.is_valid() and num.validated_data["username"] == "123",
    )


# ── #5 nested serializer honors partial ──────────────────────────────────────


class _Child(Serializer):
    name: str = SerializerField()  # required


class _Parent(Serializer):
    child: _Child = SerializerField()


def test_nested_partial():
    print("#5 nested serializer honors partial on PATCH")
    full = _Parent(input_data={"child": {}})
    check("full create rejects empty required nested child", not full.is_valid())

    patch = _Parent(input_data={"child": {}}, partial=True)
    check("PATCH accepts empty nested child (partial propagated)", patch.is_valid())


# ── #6 get_object surfaces unexpected errors instead of a silent 404 ─────────


class _Boom(RuntimeError):
    pass


class _BoomQS:
    _model = Model

    async def get(self, **kwargs):
        raise _Boom("db exploded")


class _MissingQS:
    _model = Model

    async def get(self, **kwargs):
        raise Model.DoesNotExist("gone")


class _FakeView:
    """Minimal stand-in exposing exactly what ViewSet.get_object touches."""

    def __init__(self, qs):
        self._qs = qs
        self.lookup_url_kwarg = None
        self.lookup_field = "id"
        self.kwargs = {"id": "1"}
        self.request = _FakeRequest(AnonymousUser())

    def get_queryset(self):
        return self._qs

    def _decode_public_id(self, value, request=None):
        return ("id", int(value))

    async def check_object_permissions(self, request, obj):
        return None


async def test_get_object_error_surfacing():
    print("#6 get_object distinguishes not-found from infra failure")
    get_object = ViewSet.get_object

    # DoesNotExist → NotFound (benign 404)
    try:
        await get_object(_FakeView(_MissingQS()))
        check("DoesNotExist should raise NotFound", False)
    except NotFound:
        check("DoesNotExist mapped to NotFound", True)
    except Exception as exc:  # noqa: BLE001
        check(f"DoesNotExist raised {type(exc).__name__}, expected NotFound", False)

    # Unexpected error → propagates (NOT a silent 404)
    try:
        await get_object(_FakeView(_BoomQS()))
        check("infra error should not be swallowed", False)
    except NotFound:
        check("infra error was masked as NotFound (regression)", False)
    except _Boom:
        check("infra error propagates as itself (visible → 500)", True)


async def _main() -> bool:
    await test_object_permission()
    test_session_auth()
    await test_typed_fields()
    await test_relational_field()
    test_str_field()
    test_nested_partial()
    await test_get_object_error_surfacing()

    print()
    return finish()


if __name__ == "__main__":
    run_main(lambda: asyncio.run(_main()))
