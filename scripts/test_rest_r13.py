# hyper-test: unit
"""
Regression tests for the REST/serializers fix-wave (round 13).

Covers the confirmed findings, all with in-process fakes (no live DB):

  #1  HiddenField + CurrentUserDefault — a client cannot override a HiddenField
      via input (mass-assignment/IDOR), an ABSENT hidden field resolves its
      callable default against the serializer context (no 500), and the happy
      path stamps the server-side user id.
  #2  BulkCreateMixin.bulk_create runs avalidate_relations() (FK-existence /
      slug resolution), matching single create() and bulk_update().
  #3  CursorPagination "previous" returns the page IMMEDIATELY preceding the
      cursor, not the first-page region.
  #4  DecimalField enforces max_digits and quantizes to decimal_places.
  #5  ServerCursorPagination user-binding — an anon user can resume its OWN
      cursor but not another user's (and cross-user is still blocked).
  #6  Bulk error bodies use the uniform field→list shape.
  #8  HiddenField is absent from serialized output.
  #9  ChoiceField coerces input to the declared choice type ("1" -> 1).

Usage:
    uv run hyper-test rest_r13
"""

import asyncio
import base64
import decimal
import inspect
import json
import sys
import traceback

from hyperdjango.rest import (
    BulkCreateMixin,
    ChoiceField,
    CurrentUserDefault,
    CursorPagination,
    DecimalField,
    HiddenField,
    NotFound,
    PermissionDenied,
    ServerCursorPagination,
    _get_cursor_secret,
    hmac_sha256_hex_truncated,
)
from hyperdjango.serializers import Serializer, SerializerField

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


# ── Shared fakes ──────────────────────────────────────────────────────────


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeCtxRequest:
    def __init__(self, user=None, client_ip="1.2.3.4"):
        self.user = user
        self.client_ip = client_ip


class PostSerializer(Serializer):
    title: str = SerializerField()
    author_id: int = HiddenField(default=CurrentUserDefault())


# ── #1 HiddenField / CurrentUserDefault ────────────────────────────────────


@test("#1 client CANNOT override a HiddenField via input (IDOR blocked)")
def t_hidden_no_override():
    ctx = {"request": FakeCtxRequest(user=FakeUser(42))}
    s = PostSerializer(input_data={"title": "hi", "author_id": 999}, context=ctx)
    assert s.is_valid(), s.errors
    assert s.validated_data["author_id"] == 42, (
        f"client value 999 leaked through: {s.validated_data['author_id']!r}"
    )
    assert s.validated_data["title"] == "hi"


@test("#1 absent HiddenField resolves callable default from context (no 500)")
def t_hidden_absent_resolves():
    ctx = {"request": FakeCtxRequest(user=FakeUser(7))}
    s = PostSerializer(input_data={"title": "hi"}, context=ctx)
    assert s.is_valid(), s.errors
    val = s.validated_data["author_id"]
    assert val == 7, f"callable default not invoked, got {val!r}"
    # The raw CurrentUserDefault object must never reach validated_data.
    assert not isinstance(val, CurrentUserDefault)


# ── #8 HiddenField absent from output ──────────────────────────────────────


@test("#8 HiddenField is excluded from serialized output")
def t_hidden_not_in_output():
    s = PostSerializer(obj={"title": "hi", "author_id": 42})
    data = s.data
    assert data == {"title": "hi"}, f"author_id leaked into output: {data!r}"


# ── #9 ChoiceField coercion ────────────────────────────────────────────────


@test("#9 ChoiceField coerces string input to int-typed choices")
def t_choice_coerce():
    f = ChoiceField(choices=[1, 2, 3])
    assert f.to_internal_value("1") == 1
    assert f.to_internal_value(2) == 2
    try:
        f.to_internal_value("9")
        raise AssertionError("expected ValueError for out-of-choice value")
    except ValueError:
        pass
    # String choices still work.
    fs = ChoiceField(choices=["a", "b"])
    assert fs.to_internal_value("a") == "a"


# ── #4 DecimalField ────────────────────────────────────────────────────────


@test("#4 DecimalField enforces max_digits and quantizes to decimal_places")
def t_decimal_constraints():
    f = DecimalField(max_digits=5, decimal_places=2)
    ok = f.to_internal_value("123.45")
    assert ok == decimal.Decimal("123.45")

    # too many total digits → clean validation error
    try:
        f.to_internal_value("1234.56")  # 6 digits
        raise AssertionError("expected ValueError for max_digits overflow")
    except ValueError:
        pass

    # quantize scale up to decimal_places
    q = DecimalField(decimal_places=2)
    v = q.to_internal_value("1.2")
    assert v.as_tuple().exponent == -2, f"not quantized to 2 places: {v!r}"
    # quantize scale DOWN (rounding excess)
    v2 = q.to_internal_value("1.239")
    assert v2 == decimal.Decimal("1.24"), f"round-half-up failed: {v2!r}"

    # NaN / Infinity rejected
    for bad in ("nan", "inf"):
        try:
            f.to_internal_value(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


# ── #2 / #6 bulk_create validates relations + uniform error shape ──────────


class FakeRelSerializer:
    def __init__(self, input_data=None, obj=None):
        self.input_data = input_data
        self.obj = obj
        self.errors = {}
        self.relations_checked = False

    def is_valid(self):
        return True

    async def avalidate_relations(self):
        self.relations_checked = True
        if self.input_data and self.input_data.get("bad_fk"):
            self.errors = {"author": "Invalid pk=999 - object does not exist"}
            return False
        return True

    @property
    def data(self):
        return {"id": 1, **(self.obj or {})}


class FakeBulkView(BulkCreateMixin):
    def __init__(self):
        self.perform_create_calls = 0
        self.serializers = []

    async def get_request_data(self, request):
        return request  # `request` is the list body

    def get_serializer(self, input_data=None, obj=None):
        s = FakeRelSerializer(input_data=input_data, obj=obj)
        self.serializers.append(s)
        return s

    async def perform_create(self, serializer):
        self.perform_create_calls += 1
        return dict(serializer.input_data)


@test("#2 bulk_create validates relations (bad FK → 400, no create)")
async def t_bulk_create_validates_relations():
    view = FakeBulkView()
    resp = await view.bulk_create([{"bad_fk": True}])
    assert resp.status == 400, f"expected 400, got {resp.status}"
    assert view.perform_create_calls == 0, "create ran despite invalid relation"
    input_ser = view.serializers[0]
    assert input_ser.relations_checked, "avalidate_relations was never called"
    body = json.loads(resp.body)
    # #6 uniform field→list shape
    assert body["errors"]["0"] == {
        "author": ["Invalid pk=999 - object does not exist"]
    }, f"non-uniform error shape: {body['errors']!r}"


@test("#2 bulk_create happy path creates when relations valid")
async def t_bulk_create_ok():
    view = FakeBulkView()
    resp = await view.bulk_create([{"name": "a"}, {"name": "b"}])
    assert resp.status == 201, f"expected 201, got {resp.status}"
    assert view.perform_create_calls == 2
    # every input serializer had its relations validated
    inputs = [s for s in view.serializers if s.input_data is not None]
    assert all(s.relations_checked for s in inputs)


# ── #3 CursorPagination previous window ────────────────────────────────────


class FakeQuerySet:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, **kw):
        rows = self.rows
        for key, val in kw.items():
            field, _, op = key.partition("__")
            op = op or "eq"

            def keep(r, field=field, op=op, val=val):
                v = r[field]
                if op == "lt":
                    return v < val
                if op == "gt":
                    return v > val
                if op == "eq":
                    return v == val
                raise AssertionError(f"unexpected op {op}")

            rows = [r for r in rows if keep(r)]
        return FakeQuerySet(rows)

    def order_by(self, ordering):
        desc = ordering.startswith("-")
        field = ordering[1:] if desc else ordering
        return FakeQuerySet(sorted(self.rows, key=lambda r: r[field], reverse=desc))

    def limit(self, n):
        return FakeQuerySet(self.rows[:n])

    async def all(self):
        return list(self.rows)


class FakeGET:
    def __init__(self, cursor=None):
        self._cursor = cursor

    def get(self, key, default=None):
        return self._cursor if self._cursor is not None else default


class FakePageRequest:
    def __init__(self, cursor=None):
        self.GET = FakeGET(cursor)


async def _page(pag, rows, cursor=None):
    items = await pag.paginate_queryset(FakeQuerySet(rows), FakePageRequest(cursor))
    return items, pag._next_cursor, pag._prev_cursor


@test("#3 CursorPagination prev returns the immediately-preceding page")
async def t_cursor_prev_window():
    rows = [{"id": i} for i in range(1, 13)]  # ids 1..12

    def ids(items):
        return [r["id"] for r in items]

    pag = CursorPagination()
    pag.page_size = 3
    pag.ordering = "-id"

    p1, next1, _ = await _page(pag, rows)
    assert ids(p1) == [12, 11, 10], ids(p1)

    p2, next2, _ = await _page(pag, rows, next1)
    assert ids(p2) == [9, 8, 7], ids(p2)

    p3, _, prev3 = await _page(pag, rows, next2)
    assert ids(p3) == [6, 5, 4], ids(p3)

    # Go BACK from page 3 → must land exactly on page 2 (9,8,7), NOT page 1.
    pback, _, _ = await _page(pag, rows, prev3)
    assert ids(pback) == [9, 8, 7], (
        f"prev returned the wrong window: {ids(pback)} (expected page 2 [9,8,7])"
    )


# ── #5 ServerCursorPagination user-binding ─────────────────────────────────


def _make_server_cursor_token(user_id):
    """Mirror ServerCursorPagination._create_new_cursor token construction."""
    raw_id = f"{user_id}:deadbeef:123.0"
    secret = _get_cursor_secret()
    sig = hmac_sha256_hex_truncated(secret.encode(), raw_id.encode(), 32)
    cursor_id = f"{raw_id}:{sig}"
    return base64.urlsafe_b64encode(cursor_id.encode()).decode()


@test("#5 anon can resume its OWN cursor; another anon/user cannot")
async def t_server_cursor_binding():
    pag = ServerCursorPagination()

    # Anonymous user, identified by IP → user_id "anon:1.2.3.4".
    token = _make_server_cursor_token("anon:1.2.3.4")

    # Same anon user: binding PASSES → proceeds to registry lookup → NotFound
    # (cursor not registered). NotFound proves the binding check let it through.
    same = FakeCtxRequest(user=None, client_ip="1.2.3.4")
    try:
        await pag._fetch_existing_cursor(token, same)
        raise AssertionError("expected NotFound after passing binding")
    except NotFound:
        pass
    except PermissionDenied:
        raise AssertionError("anon user wrongly denied its OWN cursor (#5 bug)")

    # Different anon IP: binding must FAIL with PermissionDenied.
    other = FakeCtxRequest(user=None, client_ip="9.9.9.9")
    try:
        await pag._fetch_existing_cursor(token, other)
        raise AssertionError("expected PermissionDenied for a different anon user")
    except PermissionDenied:
        pass

    # Cross-user (authenticated) is still blocked — security preserved.
    tok42 = _make_server_cursor_token("42")
    attacker = FakeCtxRequest(user=FakeUser(99))
    try:
        await pag._fetch_existing_cursor(tok42, attacker)
        raise AssertionError("expected PermissionDenied for cross-user cursor")
    except PermissionDenied:
        pass

    # Same authenticated owner passes binding → NotFound (unregistered).
    owner = FakeCtxRequest(user=FakeUser(42))
    try:
        await pag._fetch_existing_cursor(tok42, owner)
        raise AssertionError("expected NotFound after passing binding")
    except NotFound:
        pass
    except PermissionDenied:
        raise AssertionError("owner wrongly denied its OWN cursor")


async def main():
    all_tests = [
        obj
        for _name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]
    print("\n═══ REST Round-13 Fix-Wave Tests ═══")
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
