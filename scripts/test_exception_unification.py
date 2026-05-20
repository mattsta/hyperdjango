"""
Regression tests for the UNIFIED exception hierarchy.

# hyper-test: unit

Background: the framework used to carry two parallel, unrelated exception
hierarchies — ``hyperdjango.exceptions.HTTPException`` and REST's
``APIException`` (+ subclasses). They mapped to responses differently at every
boundary, causing:
  F1  an APIException raised OUTSIDE a viewset → generic 500 (boundary blind).
  F2  the SAME exception → {"detail"} on plain paths vs {"detail","status"} on
      viewset paths.
  F3  three incompatible 429 contracts.
  F4  four different generic-500 bodies.
  F5  the Zig safety-net dropped HTTPException.headers.

Now ``APIException`` subclasses ``HTTPException`` and every boundary funnels
through the ONE mapper ``exception_to_response``. These tests lock that in — the
exact area that caused a prior framework-wide regression.

Run: uv run hyper-test test_exception_unification
"""

import asyncio
import inspect
import json
import sys

results = []
test_funcs = []


def test(name):
    def decorator(func):
        test_funcs.append((name, func))
        return func

    return decorator


def check(label, condition):
    results.append((label, condition))
    symbol = "✓" if condition else "✗"
    print(f"  {symbol} {label}")


def _body(resp):
    return json.loads(resp.body)


def _zig_drive(wrapped, **kw):
    kwargs = dict(
        method="GET", path="/", headers={}, query_string="", body=b"", path_params={}
    )
    kwargs.update(kw)
    return wrapped(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# F1: APIException raised OUTSIDE a viewset must map to its status, not 500
# ═══════════════════════════════════════════════════════════════════════════


@test("F1: APIException(NotFound) from a PLAIN @app.get handler → 404 (ASGI)")
async def test_apiexception_plain_handler_asgi():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.rest import NotFound

    app = HyperApp(title="Test")

    @app.get("/thing")
    async def get_thing(request):
        raise NotFound("no such thing")

    resp = await app.handle(Request(method="GET", path="/thing"))
    check("status is 404 (not 500)", resp.status == 404)
    body = _body(resp)
    check("detail preserved", body["detail"] == "no such thing")
    check("status field present", body["status"] == 404)


@test("F1: APIException(NotFound) from a PLAIN handler → 404 (Zig path)")
def test_apiexception_plain_handler_zig():
    # Sync test: the Zig wrapper drives its own thread-local event loop via
    # _run_dispatch, which must NOT be nested inside a running asyncio loop.
    from hyperdjango import HyperApp
    from hyperdjango.rest import NotFound

    app = HyperApp(title="Test")

    async def handler(request):
        raise NotFound("no such thing")

    wrapped = app._wrap_handler_for_zig(handler, None, app._resolve_exception)
    status, ct, body, extra = _zig_drive(wrapped)
    check("status is 404 (not 500)", status == 404)
    parsed = json.loads(body)
    check("detail preserved", parsed["detail"] == "no such thing")
    check("status field present", parsed["status"] == 404)


@test("F1: APIException IS an HTTPException (unified hierarchy)")
async def test_apiexception_is_httpexception():
    from hyperdjango.exceptions import HTTPException
    from hyperdjango.rest import (
        APIException,
        AuthenticationFailed,
        Conflict,
        MethodNotAllowed,
        NotFound,
        PermissionDenied,
        Throttled,
        ValidationError,
    )

    check(
        "APIException subclasses HTTPException", issubclass(APIException, HTTPException)
    )
    for sub, code in [
        (ValidationError, 400),
        (AuthenticationFailed, 401),
        (PermissionDenied, 403),
        (NotFound, 404),
        (MethodNotAllowed, 405),
        (Throttled, 429),
        (Conflict, 409),
    ]:
        inst = sub("x")
        check(f"{sub.__name__} is HTTPException", isinstance(inst, HTTPException))
        check(f"{sub.__name__} default status {code}", inst.status_code == code)
        check(f"{sub.__name__} has headers slot", inst.headers == {})
        check(f"{sub.__name__} has errors slot", inst.errors is None)


# ═══════════════════════════════════════════════════════════════════════════
# F2/F4: ONE body shape everywhere
# ═══════════════════════════════════════════════════════════════════════════


@test("F2: identical body shape on plain-handler and viewset paths")
async def test_identical_body_plain_and_viewset():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.rest import NotFound, ViewSet

    # Plain @app.get path
    app = HyperApp(title="Test")

    @app.get("/thing")
    async def get_thing(request):
        raise NotFound("gone")

    plain_resp = await app.handle(Request(method="GET", path="/thing"))

    # Viewset path
    class ThingViewSet(ViewSet):
        async def list(self, request, **kwargs):
            raise NotFound("gone")

    handler = ThingViewSet.as_view(actions={"get": "list"})
    vs_resp = await handler(Request(method="GET", path="/things"))

    check("both are 404", plain_resp.status == 404 and vs_resp.status == 404)
    check(
        "identical JSON body",
        _body(plain_resp) == _body(vs_resp) == {"detail": "gone", "status": 404},
    )


@test("F2: APIException.errors forwarded on the plain-handler path")
async def test_errors_field_forwarded_plain():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.rest import ValidationError

    app = HyperApp(title="Test")

    @app.post("/v")
    async def v(request):
        raise ValidationError("bad", errors={"name": ["required"]})

    resp = await app.handle(Request(method="POST", path="/v"))
    check("status 400", resp.status == 400)
    body = _body(resp)
    check("errors forwarded", body.get("errors") == {"name": ["required"]})
    check("detail + status present", body["detail"] == "bad" and body["status"] == 400)


@test("F4: unified generic-500 body shape (ASGI)")
async def test_unified_generic_500_asgi():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request

    app = HyperApp(title="Test", debug=False)

    @app.get("/crash")
    async def crash(request):
        raise RuntimeError("secret boom")

    asgi = await app.handle(Request(method="GET", path="/crash"))
    check("ASGI 500 status", asgi.status == 500)
    check(
        "ASGI 500 unified body",
        _body(asgi) == {"detail": "Internal Server Error", "status": 500},
    )
    check("ASGI 500 does not leak", b"secret boom" not in asgi.body)


@test("F4: unified generic-500 body shape (Zig)")
def test_unified_generic_500_zig():
    # Sync: drives the Zig wrapper's own loop — must not nest in a running loop.
    from hyperdjango import HyperApp

    app = HyperApp(title="Test", debug=False)

    async def handler(request):
        raise RuntimeError("secret boom")

    wrapped = app._wrap_handler_for_zig(handler, None, app._resolve_exception)
    status, ct, body, extra = _zig_drive(wrapped)
    check("Zig 500 status", status == 500)
    check(
        "Zig 500 unified body",
        json.loads(body) == {"detail": "Internal Server Error", "status": 500},
    )
    check("Zig 500 does not leak", b"secret boom" not in body)


# ═══════════════════════════════════════════════════════════════════════════
# F3: 429 Retry-After on all three producers
# ═══════════════════════════════════════════════════════════════════════════


@test("F3: ratelimit.build_429_response carries Retry-After")
async def test_ratelimit_429_retry_after():
    from hyperdjango.ratelimit import QuotaPolicy, ServiceLimit, build_429_response

    policy = QuotaPolicy(name="test", quota=10, window=60)
    limit = ServiceLimit(policy_name="test", remaining=0, reset=42)
    resp = build_429_response([policy], [limit], reset=42)
    check("status 429", resp.status == 429)
    check("Retry-After header", resp.headers.get("retry-after") == "42")


@test("F3: REST Throttled carries Retry-After + unified body")
async def test_rest_throttled_retry_after():
    from hyperdjango.request import Request
    from hyperdjango.rest import BaseThrottle, Throttled, ViewSet, exception_to_response

    class DenyThrottle(BaseThrottle):
        async def allow_request(self, request, view):
            return False

        def get_wait(self):
            return 30

    class ThrottledViewSet(ViewSet):
        throttle_classes = (DenyThrottle,)

        async def list(self, request, **kwargs):
            return None

    vs = ThrottledViewSet()
    raised = None
    try:
        await vs.check_throttles(Request(method="GET", path="/x"))
    except Throttled as exc:
        raised = exc
    check("Throttled raised", raised is not None)
    check("Throttled has Retry-After header", raised.headers.get("Retry-After") == "30")
    resp = exception_to_response(raised)
    check("mapped 429", resp.status == 429)
    check("response Retry-After survives", resp.headers.get("Retry-After") == "30")
    body = _body(resp)
    check("unified body detail+status", "detail" in body and body["status"] == 429)


@test("F3: guard RATE_LIMITED denial carries Retry-After")
async def test_guard_rate_limited_retry_after():
    from hyperdjango.exceptions import HTTPException, exception_to_response
    from hyperdjango.guard.evaluator import evaluate_guard
    from hyperdjango.guard.types import (
        DenyReason,
        GuardDenial,
        GuardRequirement,
        GuardSpec,
        RequirementKind,
    )

    async def rate_limited_eval(request, ctx):
        return GuardDenial(DenyReason.RATE_LIMITED, "slow down", retry_after=15)

    spec = GuardSpec(
        requirements=(
            GuardRequirement(
                kind=RequirementKind.PRECONDITION,
                name="rate",
                evaluate_fn=rate_limited_eval,
            ),
        )
    )

    class _Req:
        path = "/x"
        method = "GET"
        user = None
        headers: dict[str, str] = {}

    raised = None
    try:
        await evaluate_guard(_Req(), spec)
    except HTTPException as exc:
        raised = exc
    check("guard raised HTTPException", raised is not None)
    check("guard status 429", raised.status_code == 429)
    check("guard Retry-After header", raised.headers.get("Retry-After") == "15")
    resp = exception_to_response(raised)
    check("mapped response Retry-After", resp.headers.get("Retry-After") == "15")


# ═══════════════════════════════════════════════════════════════════════════
# F5: HTTPException.headers survive the Zig safety-net path
# ═══════════════════════════════════════════════════════════════════════════


@test("F5: HTTPException.headers survive the Zig safety-net")
def test_headers_survive_safety_net():
    # Sync: drives the Zig wrapper's own loop — must not nest in a running loop.
    from hyperdjango import HyperApp
    from hyperdjango.exceptions import HTTPException
    from hyperdjango.standalone_middleware import MiddlewareStack

    app = HyperApp(title="Test")

    async def raising_mw(request, call_next):
        # Raise BEFORE the inner dispatch boundary — this unwinds past
        # _inner_dispatch straight into the wrapper's outer safety-net except.
        raise HTTPException(429, "too many", headers={"Retry-After": "7"})

    stack = MiddlewareStack()
    stack.add(raising_mw)

    async def handler(request):
        return {"ok": True}

    # No exc_resolver: the outer safety-net (not _inner_dispatch) handles it.
    wrapped = app._wrap_handler_for_zig(handler, stack, None)
    status, ct, body, extra = _zig_drive(wrapped)
    check("safety-net status 429", status == 429)
    check(
        "safety-net forwards Retry-After",
        extra is not None and "Retry-After: 7" in extra,
    )
    parsed = json.loads(body)
    check("safety-net unified body", parsed == {"detail": "too many", "status": 429})


# ═══════════════════════════════════════════════════════════════════════════
# Precedence: a generic Exception catch-all must NOT swallow an HTTPException
# ═══════════════════════════════════════════════════════════════════════════


@test("precedence: catch-all does NOT swallow APIException")
async def test_catch_all_does_not_swallow_apiexception():
    from hyperdjango import HyperApp
    from hyperdjango.request import Request
    from hyperdjango.response import Response
    from hyperdjango.rest import NotFound

    app = HyperApp(title="Test")

    @app.exception_handler(Exception)
    async def catch_all(request, exc):
        return Response.json({"detail": "swallowed"}, status=500)

    @app.get("/thing")
    async def get_thing(request):
        raise NotFound("keep me")

    resp = await app.handle(Request(method="GET", path="/thing"))
    check("APIException not swallowed → 404", resp.status == 404)
    check("catch-all did not fire", b"swallowed" not in resp.body)
    check("detail preserved", _body(resp)["detail"] == "keep me")


@test("precedence: HTTPException-specific handler still wins")
async def test_http_specific_handler_wins():
    from hyperdjango import HyperApp
    from hyperdjango.exceptions import HTTPException
    from hyperdjango.request import Request
    from hyperdjango.response import Response
    from hyperdjango.rest import NotFound

    app = HyperApp(title="Test")

    @app.exception_handler(Exception)
    async def catch_all(request, exc):
        return Response.json({"detail": "swallowed"}, status=500)

    @app.exception_handler(HTTPException)
    async def http_handler(request, exc):
        return Response.json({"custom": exc.detail}, status=exc.status_code)

    @app.get("/thing")
    async def get_thing(request):
        raise NotFound("routed")

    resp = await app.handle(Request(method="GET", path="/thing"))
    check("HTTPException handler wins over catch-all", resp.status == 404)
    check("custom handler fired", _body(resp).get("custom") == "routed")


# ═══════════════════════════════════════════════════════════════════════════


def main():
    print(f"\n{'=' * 60}")
    print("Exception Unification Regression Tests")
    print(f"{'=' * 60}")

    for name, func in test_funcs:
        print(f"\n[TEST] {name}")
        try:
            # Async tests run in their OWN fresh loop; sync tests (which drive
            # the Zig wrapper's thread-local loop directly) run with NO running
            # loop — driving _run_dispatch inside a running loop corrupts the
            # asyncio running-loop state and hangs interpreter teardown.
            if inspect.iscoroutinefunction(func):
                asyncio.run(func())
            else:
                func()
        except Exception as e:
            check(f"EXCEPTION: {e}", False)
            import traceback

            traceback.print_exc()

    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)

    print(f"\n{'=' * 60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    if failed:
        print("\nFailed:")
        for label, ok in results:
            if not ok:
                print(f"  ✗ {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
