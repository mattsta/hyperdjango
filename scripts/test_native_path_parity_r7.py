#!/usr/bin/env python3
"""Round-7 native-vs-ASGI request-lifecycle parity regressions.

Proves the five correctness fixes where the native Zig dispatch path had
diverged from the ASGI/test path (or where a permission/serializer helper
crashed / silently accepted junk):

1. request.app is set on the native wrapper path (parity with _dispatch).
2. Service DI (app.provide) is injected on the native path too.
3. IsAdminUser grants an is_staff / is_superuser User (no AttributeError),
   and denies a plain authenticated user + anonymous.
4. IDMixin include_user binding threads `request` through both the decode
   (get_object) and encode (_encode_response_ids) callsites → user_id is
   non-None when a request with a user is present.
5. Serializer nested-field validation rejects non-dict input with a clean
   field error instead of storing junk unvalidated.

Pure-Python — exercises the existing built .so, no rebuild required.
"""

# hyper-test: db_django

import asyncio
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.admin_settings")

import django

django.setup()

from hyperdjango.app import HyperApp


def _dict_to_user(user_dict):
    """Build a (possibly partially-hydrated) User from a dict — used to exercise
    IsAdminUser against unset is_staff/is_superuser fields. Production auth builds
    SessionUser, not User, so this lives with the test that needs it."""
    from hyperdjango.auth.user import User

    user = object.__new__(User)
    for key, value in user_dict.items():
        # dynamic-attr: test projects a runtime dict onto a freshly-allocated User to simulate partial hydration
        setattr(user, key, value)
    return user


from hyperdjango.auth.user import AnonymousUser
from hyperdjango.public_id import IDMode
from hyperdjango.rest import IsAdminUser, ViewSet
from hyperdjango.serializers import Serializer
from hyperdjango.testkit import check, finish, run_main


def _zig_kwargs(**over):
    base = {
        "method": "GET",
        "path": "/",
        "headers": {},
        "query_string": "",
        "body": b"",
        "path_params": {},
    }
    base.update(over)
    return base


# ── 1 + 2: request.app + service DI on the native wrapper path ───────────────


class _MyService:
    def __init__(self):
        self.token = "svc-42"


def test_native_request_app_and_di():
    print("test_native_request_app_and_di")
    app = HyperApp(title="parity")
    svc = _MyService()
    app.provide(_MyService, svc)

    seen = {}

    async def handler(request, svc: _MyService):
        seen["app"] = request.app
        seen["svc"] = svc
        return {"ok": True}

    wrapper = HyperApp._wrap_handler_for_zig(handler, app=app)
    result = wrapper(**_zig_kwargs())

    check("request.app is the owning HyperApp on native path", seen.get("app") is app)
    check(
        "service DI injected on native path (svc by annotation)", seen.get("svc") is svc
    )
    # Response tuple: (status, content_type, body, extra_headers)
    check("native handler returned 200", isinstance(result, tuple) and result[0] == 200)


# ── 3: IsAdminUser ───────────────────────────────────────────────────────────


class _Req:
    def __init__(self, user):
        self.user = user
        self.method = "GET"


def test_is_admin_user():
    print("test_is_admin_user")
    perm = IsAdminUser()

    # Production feeds _dict_to_user from user.to_dict() — a full row that
    # always carries is_staff/is_superuser as real bools.
    staff = _dict_to_user(
        {
            "id": 1,
            "username": "s",
            "is_active": True,
            "is_staff": True,
            "is_superuser": False,
        }
    )
    superu = _dict_to_user(
        {
            "id": 2,
            "username": "su",
            "is_active": True,
            "is_staff": False,
            "is_superuser": True,
        }
    )
    plain = _dict_to_user(
        {
            "id": 3,
            "username": "p",
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
        }
    )
    # Partially-hydrated User: is_staff/is_superuser never set → attribute reads
    # back the class-level FieldInfo descriptor (truthy). Must NOT grant admin.
    unhydrated = _dict_to_user({"id": 4, "username": "u", "is_active": True})

    # Does not raise (the original in_group AttributeError bug) and grants staff.
    granted_staff = asyncio.run(perm.has_permission(_Req(staff), None))
    check("is_staff User granted (legacy fallback, no crash)", granted_staff is True)

    granted_super = asyncio.run(perm.has_permission(_Req(superu), None))
    check("is_superuser User granted", granted_super is True)

    denied_plain = asyncio.run(perm.has_permission(_Req(plain), None))
    check("plain authenticated User denied (no crash)", denied_plain is False)

    denied_unhydrated = asyncio.run(perm.has_permission(_Req(unhydrated), None))
    check(
        "un-hydrated User (FieldInfo fallback) denied (no privilege escalation)",
        denied_unhydrated is False,
    )

    denied_anon = asyncio.run(perm.has_permission(_Req(AnonymousUser()), None))
    check("AnonymousUser denied", denied_anon is False)


# ── 4: IDMixin include_user request threading ────────────────────────────────


class _FakeConfig:
    def __init__(self):
        self.mode = IDMode.SIGNED
        self.include_user = True


class _FakeIDManager:
    def __init__(self):
        self.config = _FakeConfig()
        self.encode_user_ids: list = []
        self.decode_user_ids: list = []

    def encode(self, pk, user_id=None):
        self.encode_user_ids.append(user_id)
        return f"signed:{pk}:{user_id}"

    def decode(self, external_id, user_id=None):
        self.decode_user_ids.append(user_id)
        return 123


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeQS:
    async def get(self, **kw):
        return {"id": 123}


class _BoundVS(ViewSet):
    def __init__(self, mgr):
        self._mgr = mgr
        self._qs = _FakeQS()

    def _get_id_manager(self):
        return self._mgr

    def get_queryset(self):
        return self._qs

    async def check_object_permissions(self, request, obj):
        return None


def test_idmixin_request_threading():
    print("test_idmixin_request_threading")

    # Decode side — get_object() is a real fixed callsite.
    mgr = _FakeIDManager()
    vs = _BoundVS(mgr)
    vs.request = _Req(_FakeUser(42))
    vs.kwargs = {"id": "signed-ext"}
    asyncio.run(vs.get_object())
    check(
        "get_object threads request.user.id (42) into decode",
        mgr.decode_user_ids and mgr.decode_user_ids[-1] == 42,
    )

    # Encode side — _encode_response_ids used by list/create/retrieve.
    mgr2 = _FakeIDManager()
    vs2 = _BoundVS(mgr2)
    vs2.request = _Req(_FakeUser(42))
    data = vs2._encode_response_ids({"id": 5}, request=vs2.request)
    check(
        "encode threads request.user.id (42) into encode",
        mgr2.encode_user_ids and mgr2.encode_user_ids[-1] == 42,
    )
    check("encoded id reflects the bound user", data["id"] == "signed:5:42")

    # Negative control: no request → user_id None (the pre-fix symptom).
    mgr2._encode_user_ids = None
    vs2._encode_response_ids({"id": 5})  # no request
    check(
        "without request user_id is None (proves binding actually depends on request)",
        mgr2.encode_user_ids[-1] is None,
    )


# ── 5: Serializer nested-field validation for non-dict input ─────────────────


class _Child(Serializer):
    name: str


class _Parent(Serializer):
    nested: _Child


def test_serializer_nested_nondict():
    print("test_serializer_nested_nondict")

    bad = _Parent(input_data={"nested": "junk"})
    valid = bad.is_valid()
    check("scalar for nested field is invalid (not stored as junk)", valid is False)
    check(
        "nested scalar produces 'Expected an object' field error",
        bad.errors.get("nested") == "Expected an object",
    )

    good = _Parent(input_data={"nested": {"name": "hi"}})
    check("valid nested object accepted", good.is_valid() is True)
    check(
        "valid nested object validated_data correct",
        good.validated_data.get("nested", {}).get("name") == "hi",
    )

    many = _Parent(input_data={"nested": [{"name": "a"}, {"name": "b"}]})
    check("list of nested objects accepted (many)", many.is_valid() is True)


def main() -> bool:
    for test in (
        test_native_request_app_and_di,
        test_is_admin_user,
        test_idmixin_request_threading,
        test_serializer_nested_nondict,
    ):
        test()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
