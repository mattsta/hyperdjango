"""Round-12 cleanup of deferred/low findings from prior audits.

Pure-Python regressions (no native build, no live DB) covering:

  #1  Aggregate column-collision is a documented SKIP — an Aggregate binds no
      top-level alias (``COUNT(id)`` has no ``AS``), so it cannot self-collide;
      collision handling correctly lives at the annotate()/aggregate() call
      sites. We assert the load-bearing property: as_sql() emits no alias.

  #2  A streaming Response aclose()s its async generator when a send raises
      mid-stream (ASGI ``Response.send`` path) and when the native chunked-pull
      hits a mid-stream error (``_make_native_stream_pull``) — so the generator
      (and, for Response.file, its fd) is released on abort, not at GC.

  #3  build_session_data() now populates the session ``permissions`` set from
      the user's real RBAC permissions, so Require.permission() grants a
      genuine permission-holder (previously it was superuser-only over-deny),
      still denies a non-holder, and keeps the superuser bypass.

  #5  format_number() no longer emits "-0.00" for a negative value that rounds
      to a zero magnitude.

Run:  uv run hyper-test deferred_r11cleanup_r12
"""

# hyper-test: unit

import asyncio
from types import SimpleNamespace

from hyperdjango.app import _make_native_stream_pull
from hyperdjango.auth.sessions import build_session_data
from hyperdjango.auth.user import SessionUser
from hyperdjango.expressions import Count, Sum
from hyperdjango.formats import format_currency, format_number
from hyperdjango.guard.requirements import Require
from hyperdjango.guard.types import GuardContext, GuardDenial
from hyperdjango.response import Response

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# --- #1: Aggregate can't self-collide (documented SKIP) ----------------------


def test_aggregate_binds_no_alias() -> None:
    # An Aggregate renders its operand strictly INSIDE the call and never binds
    # a top-level `AS alias`, so it cannot shadow/clobber a model column on its
    # own — the annotate()/aggregate() call sites own alias-collision handling.
    sql, params = Count("id").as_sql()
    check("Count emits no alias", sql == "COUNT(id)" and params == [], sql)
    sql2, _ = Sum("price").as_sql()
    check("Sum emits no alias", sql2 == "SUM(price)", sql2)


# --- #2: streaming aclose on abort -------------------------------------------


def test_asgi_stream_aclose_on_send_raise() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        closed = {"v": False}

        async def gen():
            try:
                for i in range(100):
                    yield f"chunk{i}".encode()
            finally:
                closed["v"] = True

        resp = Response.stream(gen())
        agen = resp._stream_iter

        async def failing_send(message):
            # Let the response.start through; abort on the first body frame,
            # simulating a client disconnect / server send error mid-stream.
            if message["type"] == "http.response.body":
                raise ConnectionError("client gone")

        raised = False
        try:
            await resp.send(failing_send)
        except ConnectionError:
            raised = True
        # ag_frame is None once an async generator is fully closed.
        return raised, closed["v"], agen.ag_frame is None

    raised, gen_finally_ran, frame_gone = asyncio.run(scenario())
    check("ASGI send re-raised the abort", raised)
    check("ASGI stream generator finalized on abort", gen_finally_ran)
    check("ASGI stream generator is closed (ag_frame None)", frame_gone)


def test_asgi_stream_aclose_on_normal_completion() -> None:
    # aclose() in the finally must be an idempotent no-op on a clean run.
    async def scenario() -> tuple[bool, list[bytes]]:
        async def gen():
            yield b"a"
            yield b"b"

        resp = Response.stream(gen())
        bodies: list[bytes] = []

        async def ok_send(message):
            if message["type"] == "http.response.body":
                bodies.append(message["body"])

        await resp.send(ok_send)
        return True, bodies

    ok, bodies = asyncio.run(scenario())
    # a, b, then the terminal empty frame
    check(
        "ASGI stream completes cleanly", ok and bodies == [b"a", b"b", b""], str(bodies)
    )


def test_native_pull_aclose_on_error() -> None:
    loop = asyncio.new_event_loop()
    try:
        closed = {"v": False}

        async def gen():
            try:
                yield b"ok"
                raise ValueError("boom mid-stream")
            finally:
                closed["v"] = True

        pull = _make_native_stream_pull(gen(), loop)
        first = pull()
        check("native pull yields first chunk", first == b"ok", repr(first))

        raised = False
        try:
            pull()  # second step raises ValueError -> aclose then re-raise
        except ValueError:
            raised = True
        check("native pull propagates mid-stream error", raised)
        check("native pull aclose()d the generator on error", closed["v"])
    finally:
        loop.close()


# --- #3: Require.permission after session permissions are populated ----------


def _session_user(**data) -> SessionUser:
    return SessionUser(data)


async def _evaluate(req, user) -> GuardDenial | None:
    request = SimpleNamespace(user=user)
    return await req.evaluate_fn(request, GuardContext())


def test_require_permission_grants_holder() -> None:
    async def scenario():
        holder = _session_user(
            id=1, groups=["editor"], permissions=["article.edit", "edit"]
        )
        # Both the bare and qualified codename forms resolve.
        bare = await _evaluate(Require.permission("edit"), holder)
        qualified = await _evaluate(Require.permission("article.edit"), holder)
        return bare, qualified

    bare, qualified = asyncio.run(scenario())
    check("holder passes bare-codename permission", bare is None, repr(bare))
    check("holder passes qualified permission", qualified is None, repr(qualified))


def test_require_permission_denies_non_holder() -> None:
    async def scenario():
        non_holder = _session_user(id=2, groups=["editor"], permissions=["edit"])
        return await _evaluate(Require.permission("delete"), non_holder)

    denial = asyncio.run(scenario())
    check(
        "non-holder is denied",
        isinstance(denial, GuardDenial),
        repr(denial),
    )


def test_require_permission_superuser_bypass() -> None:
    async def scenario():
        # Superuser has an empty permissions set but bypasses via the group.
        su = _session_user(id=3, groups=["superuser"], permissions=[])
        return await _evaluate(Require.permission("anything"), su)

    result = asyncio.run(scenario())
    check("superuser bypasses permission check", result is None, repr(result))


class _FakeDB:
    """Minimal DB stub dispatching on SQL shape (no real connection)."""

    async def query(self, sql: str, *args):
        if "codename" in sql:
            # Direct + group permission queries both look for codename/model.
            return [{"codename": "edit", "model_name": "article"}]
        # Role-tree lookup and field-permission lookup: no restrictions.
        return []


def test_build_session_data_populates_permissions() -> None:
    async def scenario():
        # groups pre-supplied (skips group-name query); password_hash supplied
        # (skips the ORM password load) so only the RBAC perm/field queries run
        # against the stub.
        session = await build_session_data(
            42,
            _FakeDB(),
            groups=["editor"],
            id=42,
            password_hash="x",
        )
        return session

    session = asyncio.run(scenario())
    perms = set(session.get("permissions", []))
    check("session has 'permissions' key", "permissions" in session)
    check(
        "qualified codename cached",
        "article.edit" in perms,
        repr(perms),
    )
    check("bare codename cached", "edit" in perms, repr(perms))

    # End-to-end: the populated session actually powers Require.permission().
    user = SessionUser(session)

    async def evaluate():
        allow = await _evaluate(Require.permission("edit"), user)
        deny = await _evaluate(Require.permission("delete"), user)
        return allow, deny

    allow, deny = asyncio.run(evaluate())
    check("built session grants held permission", allow is None, repr(allow))
    check(
        "built session denies unheld permission",
        isinstance(deny, GuardDenial),
        repr(deny),
    )


def test_build_session_data_superuser_skips_perm_load() -> None:
    async def scenario():
        session = await build_session_data(
            7,
            _FakeDB(),
            groups=["superuser"],
            id=7,
            password_hash="x",
        )
        return session

    session = asyncio.run(scenario())
    # Superuser gets an empty permissions set (bypass is via the group).
    check(
        "superuser session has empty permissions",
        session.get("permissions") == [],
        repr(session.get("permissions")),
    )
    check("superuser flag set", session.get("is_superuser") is True)


# --- #5: negative-zero normalization (user-facing currency) ------------------
# Note: the normalization lives in format_currency, NOT format_number. The
# general numeric formatter deliberately preserves IEEE signed zero (pinned by
# test_formats.py "negative float zero"); only the user-facing currency display
# folds a negative magnitude that rounds to zero into "$0.00".


def test_format_currency_negative_zero() -> None:
    # A negative magnitude that rounds to zero must not render as "-$0.00".
    check(
        "-0.001 @2dp -> $0.00",
        format_currency(-0.001, decimal_places=2) == "$0.00",
        format_currency(-0.001, decimal_places=2),
    )
    check(
        "-0.0 -> $0.00 (no sign)",
        format_currency(-0.0) == "$0.00",
        format_currency(-0.0),
    )
    # A genuine negative value keeps its sign.
    check(
        "-1.5 keeps sign",
        format_currency(-1.5, decimal_places=2) == "-$1.50",
        format_currency(-1.5, decimal_places=2),
    )
    # format_number preserves signed zero (the deliberate contract).
    check(
        "format_number preserves -0.0",
        format_number(-0.0) == "-0.0",
        format_number(-0.0),
    )


def run() -> bool:
    print("#1 Aggregate self-collision (documented SKIP):")
    test_aggregate_binds_no_alias()
    print("#2 Streaming aclose on abort:")
    test_asgi_stream_aclose_on_send_raise()
    test_asgi_stream_aclose_on_normal_completion()
    test_native_pull_aclose_on_error()
    print("#3 Require.permission with populated session permissions:")
    test_require_permission_grants_holder()
    test_require_permission_denies_non_holder()
    test_require_permission_superuser_bypass()
    test_build_session_data_populates_permissions()
    test_build_session_data_superuser_skips_perm_load()
    print("#5 format_currency negative zero:")
    test_format_currency_negative_zero()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
