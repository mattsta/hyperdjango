"""Round-13 observability/leak hardening for hyperdjango/app.py.

Pure-Python regressions (no live server, no live DB) proving the five app.py
fixes from the R13 audit wave. Fakes/monkeypatch throughout.

  #A5.2  /ready info leak — a DB (or custom) check failure must report a GENERIC
         status in the (unauthenticated) readiness body, NEVER the raw exception
         string (which leaks host/port/DSN fragments/SQLSTATE); the detail is
         logged server-side instead.

  #A5.4  Correlation gap — the native safety net (unhandled middleware/finalize
         500) must log/resolve WHILE the request_id log-context is still active.
         Previously _finalize_native reset the context before the exception
         unwound to the safety net, so the hardest native 500 had no request_id.

  #A5.7  Spoofable correlation — an inbound X-Request-ID is adopted ONLY when it
         is a bounded, safe token; a malformed/oversized inbound id is ignored
         and a fresh id minted. A valid id is still echoed.

  #A4    app.listen leak — registering the SAME channel twice issues exactly one
         native _db_listen (dedup registry), preventing a hot-reload from
         accumulating listener threads.

  #A4    Native shutdown symmetry — _native_shutdown_databases() surfaces the
         Database(s) the native run path connected so native shutdown disconnects
         the pool, matching the ASGI _shutdown().

Run:  uv run hyper-test app_obs_r13

# hyper-test: unit
"""

import asyncio
from types import SimpleNamespace

import hyperdjango.app as appmod
from hyperdjango.app import HyperApp, _finalize_native, _resolve_request_id
from hyperdjango.logging._core import log_context
from hyperdjango.response import Response
from hyperdjango.standalone_middleware import MiddlewareStack

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


class _LogRecorder:
    """Minimal loguru-shaped stand-in capturing .opt(exception=).warning(...)."""

    def __init__(self):
        self.records = []  # list of (msg, exception, kwargs)
        self._pending_exc = None

    def opt(self, exception=None, **_):
        self._pending_exc = exception
        return self

    def warning(self, msg, **kwargs):
        self.records.append((msg, self._pending_exc, kwargs))
        self._pending_exc = None

    # tolerate any other level being called during the tested code paths
    def __getattr__(self, _name):
        def _noop(*_a, **_k):
            self._pending_exc = None

        return _noop


def _find_handler(app, path):
    for route in app.router.routes():
        if route.pattern == path:
            return route.handler
    raise AssertionError(f"no route for {path}")


# --- #A5.2: /ready never leaks the raw DB exception ---------------------------

SECRET = "host=db.internal port=5432 password=hunter2 SQLSTATE=08006"


def test_ready_db_failure_is_generic_and_logged():
    app = HyperApp()

    class _FakeDB:
        async def query(self, _sql):
            raise RuntimeError(SECRET)

    app._db = _FakeDB()
    app.mount_health()
    readiness = _find_handler(app, "/ready")

    rec = _LogRecorder()
    orig = appmod.logger
    appmod.logger = rec
    try:
        resp = asyncio.run(readiness(SimpleNamespace()))
    finally:
        appmod.logger = orig

    body = resp.body.decode("utf-8")
    check("DB-failure readiness is 503", resp.status == 503, str(resp.status))
    check(
        "DB-failure body does NOT leak the raw exception",
        SECRET not in body and "hunter2" not in body and "SQLSTATE" not in body,
        body,
    )
    check(
        "DB-failure body reports generic error",
        '"database":"error"' in body.replace(" ", ""),
        body,
    )
    check(
        "DB-failure detail is logged server-side (with exception)",
        len(rec.records) == 1 and isinstance(rec.records[0][1], RuntimeError),
        str(rec.records),
    )


def test_ready_custom_check_failure_is_generic_and_logged():
    app = HyperApp()

    def _bad_check():
        raise RuntimeError(SECRET)

    app.add_health_check("cache", _bad_check)
    app.mount_health()
    readiness = _find_handler(app, "/ready")

    rec = _LogRecorder()
    orig = appmod.logger
    appmod.logger = rec
    try:
        resp = asyncio.run(readiness(SimpleNamespace()))
    finally:
        appmod.logger = orig

    body = resp.body.decode("utf-8")
    check("custom-check failure is 503", resp.status == 503, str(resp.status))
    check(
        "custom-check body does NOT leak the raw exception",
        SECRET not in body,
        body,
    )
    check(
        "custom-check body reports generic error",
        '"cache":"error"' in body.replace(" ", ""),
        body,
    )
    check(
        "custom-check detail is logged server-side",
        len(rec.records) == 1 and isinstance(rec.records[0][1], RuntimeError),
        str(rec.records),
    )


def test_ready_healthy_still_ok():
    app = HyperApp()

    class _OkDB:
        async def query(self, _sql):
            return [{"?column?": 1}]

    app._db = _OkDB()
    app.mount_health()
    readiness = _find_handler(app, "/ready")
    resp = asyncio.run(readiness(SimpleNamespace()))
    body = resp.body.decode("utf-8")
    check("healthy readiness is 200", resp.status == 200, str(resp.status))
    check(
        "healthy readiness reports database ok",
        '"database":"ok"' in body.replace(" ", ""),
        body,
    )


# --- #A5.7: inbound X-Request-ID is validated before adoption -----------------


def _req_with_headers(headers):
    return SimpleNamespace(headers=headers)


def test_valid_inbound_request_id_is_adopted():
    rid = _resolve_request_id(_req_with_headers({"x-request-id": "abc-123_ID.9"}))
    check("valid inbound id is adopted verbatim", rid == "abc-123_ID.9", rid)


def test_invalid_inbound_request_id_is_replaced():
    # Contains characters outside [A-Za-z0-9._-]
    rid = _resolve_request_id(
        _req_with_headers({"x-request-id": "evil id\twith spaces/../"})
    )
    check(
        "malformed inbound id is NOT adopted (fresh minted)",
        rid != "evil id\twith spaces/../" and len(rid) == 32,  # uuid4 hex
        rid,
    )


def test_oversized_inbound_request_id_is_replaced():
    huge = "a" * 5000
    rid = _resolve_request_id(_req_with_headers({"x-request-id": huge}))
    check(
        "oversized inbound id is NOT adopted (fresh minted)",
        rid != huge and len(rid) == 32,
        f"len={len(rid)}",
    )


def test_no_inbound_mints_fresh():
    rid = _resolve_request_id(_req_with_headers({}))
    check(
        "absent inbound id mints a fresh uuid hex",
        len(rid) == 32 and rid.isalnum(),
        rid,
    )


# --- #A5.4: native safety net logs/resolves inside the request_id context -----


def test_native_safety_net_retains_request_id():
    app = HyperApp()

    captured = {}

    async def _capturing_resolver(req, exc):
        # This is where exception_to_response → _logger.exception would run in
        # prod; assert the request_id log-context is live at THIS point.
        captured["ctx"] = dict(log_context.get() or {})
        return Response.json({"detail": "boom", "status": 500}, status=500)

    app._resolve_exception = _capturing_resolver  # dynamic test double

    async def _raising_mw(request, nxt):
        raise RuntimeError("middleware exploded")

    ms = MiddlewareStack()
    ms.add(_raising_mw)

    async def _handler(request):
        return Response.json({"ok": True})

    wrapper = HyperApp._wrap_handler_for_zig(
        _handler, middleware_stack=ms, exc_resolver=app._resolve_exception, app=app
    )

    result = wrapper(
        method="GET",
        path="/x",
        headers={"x-request-id": "req-safety-net-42"},
    )

    check(
        "safety-net resolver ran inside the request_id context",
        captured.get("ctx", {}).get("request_id") == "req-safety-net-42",
        str(captured),
    )
    # result is a Zig response tuple (status, ct, body, extra_headers[, pull])
    check("safety-net produced a 500 tuple", result[0] == 500, str(result[0]))
    check(
        "safety-net echoes request_id header",
        "req-safety-net-42" in (result[3] or ""),
        str(result[3]),
    )


def test_finalize_native_common_case_unaffected():
    # A handler-raised exception is converted to a Response inside the chain, so
    # _finalize_native returns normally and echoes the id — the common path.
    app = HyperApp()

    async def _chain():
        return Response.json({"ok": True})

    req = SimpleNamespace(headers={"x-request-id": "req-happy-7"}, request_id=None)
    resp = asyncio.run(_finalize_native(app, req, _chain()))
    check(
        "finalize echoes request id on success",
        resp.headers.get("x-request-id") == "req-happy-7",
        str(resp.headers),
    )
    check(
        "finalize sets req.request_id", req.request_id == "req-happy-7", req.request_id
    )


# --- #A4: app.listen dedup registry -------------------------------------------


def test_listen_twice_registers_native_once():
    app = HyperApp(database="postgres://u:p@localhost/db")
    calls = []

    def _fake_db_listen(url, channel, cb):
        calls.append((url, channel, cb))

    orig = appmod._db_listen
    appmod._db_listen = _fake_db_listen
    try:

        def cb(channel, payload):
            pass

        app.listen("orders", cb)
        app.listen("orders", cb)  # duplicate — must be a no-op
        app.listen("events", cb)  # distinct channel — registers
    finally:
        appmod._db_listen = orig

    orders_calls = [c for c in calls if c[1] == "orders"]
    events_calls = [c for c in calls if c[1] == "events"]
    check(
        "duplicate listen('orders') issues ONE native _db_listen",
        len(orders_calls) == 1,
        str(calls),
    )
    check("distinct listen('events') registers", len(events_calls) == 1, str(calls))
    check(
        "registry tracks both channels",
        set(app._listeners) == {"orders", "events"},
        str(app._listeners),
    )


# --- #A4: native shutdown surfaces the DB(s) to disconnect --------------------


def test_native_shutdown_databases_surfaces_self_db():
    app = HyperApp()

    class _FakeDB:
        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    fake = _FakeDB()
    app._db = fake
    dbs = app._native_shutdown_databases()
    check("native shutdown surfaces self._db", fake in dbs, str(dbs))

    # Prove the disconnect coroutine the native cleanup awaits actually runs.
    asyncio.run(dbs[0].disconnect())
    check(
        "surfaced db.disconnect() runs",
        fake.disconnected is True,
        str(fake.disconnected),
    )


def test_native_shutdown_databases_empty_when_none():
    app = HyperApp()  # no database_url, no self._db
    check(
        "no DB → nothing to disconnect",
        app._native_shutdown_databases() == [],
        "expected empty",
    )


def run() -> bool:
    print("#A5.2 /ready never leaks raw exception:")
    test_ready_db_failure_is_generic_and_logged()
    test_ready_custom_check_failure_is_generic_and_logged()
    test_ready_healthy_still_ok()
    print("#A5.7 inbound X-Request-ID validation:")
    test_valid_inbound_request_id_is_adopted()
    test_invalid_inbound_request_id_is_replaced()
    test_oversized_inbound_request_id_is_replaced()
    test_no_inbound_mints_fresh()
    print("#A5.4 native safety-net correlation:")
    test_native_safety_net_retains_request_id()
    test_finalize_native_common_case_unaffected()
    print("#A4 app.listen dedup:")
    test_listen_twice_registers_native_once()
    print("#A4 native shutdown DB symmetry:")
    test_native_shutdown_databases_surfaces_self_db()
    test_native_shutdown_databases_empty_when_none()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
