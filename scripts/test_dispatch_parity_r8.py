#!/usr/bin/env python3
"""Round-8 native-vs-ASGI DISPATCH PARITY + request-id observability regressions.

Proves the fixes where the native Zig dispatch path (_wrap_handler_for_zig /
_inner_dispatch / _response_to_zig_tuple) had diverged from the ASGI path
(_dispatch), plus the new request-id observability wired into the shared
dispatch boundary. Pure-Python — exercises the existing built .so, no rebuild.

Findings covered:
  1. Streaming/SSE responses ship a NON-EMPTY body on native (materialized).
  2. Handler return-type coercion is ONE shared contract (coerce_response):
     str→text/plain, int/None/float→JSON 200, (body,status[,headers]) tuples.
  3. client_ip resolves through a real scope on native (peer hook); no collapse
     to 127.0.0.1 when the peer is supplied.
  7. static-404 honors the unified {"detail","status"} shape (no {"error":...}).
  8. request_id: minted / inbound-honored / traceparent-derived, echoed as the
     X-Request-ID response header on both success and safety-net paths.

Assertions that require the LIVE native server (differential harness) are noted
inline with [LIVE-NATIVE]; here we drive the Python code paths directly.
"""

# hyper-test: db_django

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.admin_settings")

import django

django.setup()

from hyperdjango.app import (
    HyperApp,
    _build_native_scope,
    _resolve_request_id,
    coerce_response,
)
from hyperdjango.exceptions import HTTPException, exception_to_response
from hyperdjango.native import fast_json_loads
from hyperdjango.request import Request
from hyperdjango.response import Response
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


# --- Finding 2: coerce_response ONE contract -------------------------------
def test_coerce_response():
    print("Finding 2 — coerce_response shared contract:")

    r = coerce_response("hello")
    check(
        "str -> text/plain 200 (NOT text/html — no XSS surprise)",
        r.status == 200 and r.headers["content-type"].startswith("text/plain"),
    )

    r = coerce_response(42)
    check(
        "int -> JSON 200 (NOT 500 'Unknown response type')",
        r.status == 200 and fast_json_loads(r.body) == 42,
    )

    r = coerce_response(None)
    check(
        "None -> JSON null 200",
        r.status == 200 and fast_json_loads(r.body) is None,
    )

    r = coerce_response(3.14)
    check(
        "float -> JSON 200",
        r.status == 200 and abs(fast_json_loads(r.body) - 3.14) < 1e-9,
    )

    r = coerce_response({"a": 1})
    check("dict -> JSON 200", r.status == 200 and fast_json_loads(r.body) == {"a": 1})

    r = coerce_response(["a", "b"])
    check("list -> JSON 200", r.status == 200 and fast_json_loads(r.body) == ["a", "b"])

    r = coerce_response(("created", 201))
    check(
        "(body, status) tuple -> body coerced + status applied",
        r.status == 201 and r.body == b"created",
    )

    r = coerce_response(({"ok": True}, 202, {"x-test": "y"}))
    check(
        "(body, status, headers) tuple -> status + headers applied",
        r.status == 202
        and fast_json_loads(r.body) == {"ok": True}
        and r.headers.get("x-test") == "y",
    )

    r = coerce_response((1, 2, 3))
    check(
        "non-status tuple (len!=2/3-with-int) -> JSON array 200",
        r.status == 200 and fast_json_loads(r.body) == [1, 2, 3],
    )

    resp_in = Response.json({"x": 1}, status=418)
    check("Response passthrough (identity)", coerce_response(resp_in) is resp_in)


# --- Finding 8: request_id resolution --------------------------------------
def test_request_id():
    print("Finding 8 — request_id resolution:")

    req = Request(headers={"X-Request-ID": "  inbound-abc  "})
    check(
        "inbound X-Request-ID honored + trimmed",
        _resolve_request_id(req) == "inbound-abc",
    )

    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    req = Request(headers={"traceparent": tp})
    check(
        "W3C traceparent -> trace-id field promoted",
        _resolve_request_id(req) == "4bf92f3577b34da6a3ce929d0e0e4736",
    )

    req = Request(headers={})
    rid = _resolve_request_id(req)
    check("no inbound -> fresh uuid4 minted", bool(rid) and len(rid) >= 16)


# --- Finding 3: client_ip / native scope -----------------------------------
def test_client_ip_scope():
    print("Finding 3 — client_ip resolution parity:")

    # Native scope built from the _peer hook -> peer_ip is the real socket addr.
    scope = _build_native_scope(_zig_kwargs(_peer=("203.0.113.7", 54321)))
    req = Request(scope=scope)
    check("native _peer hook -> peer_ip is the real addr", req.peer_ip == "203.0.113.7")
    check(
        "native _peer hook -> client_ip is the real addr",
        req.client_ip == "203.0.113.7",
    )

    # Parity: ASGI scope with the same client resolves identically.
    asgi_req = Request.from_asgi(
        {"method": "GET", "path": "/", "headers": [], "client": ("203.0.113.7", 54321)}
    )
    check(
        "native client_ip == ASGI client_ip for the same peer",
        asgi_req.client_ip == req.client_ip,
    )

    # Without the peer hook (Zig does not thread it yet) scope is None and
    # peer_ip falls back to 127.0.0.1 — documented + flagged for the native wave.
    scope_none = _build_native_scope(_zig_kwargs())
    check(
        "no _peer -> scope None (peer NOT fabricated from headers)", scope_none is None
    )
    check(
        "[LIVE-NATIVE] real peer needs Zig _peer hook; falls back to 127.0.0.1 for now",
        Request(scope=scope_none).peer_ip == "127.0.0.1",
    )


# --- Finding 7: static-404 unified shape -----------------------------------
def test_static_404_shape():
    print("Finding 7 — static-404 unified {'detail','status'} shape:")
    resp = exception_to_response(HTTPException(404, "Not Found"))
    body = fast_json_loads(resp.body)
    check(
        "HTTPException(404) -> {'detail','status'} (the shape _static_handler now raises)",
        resp.status == 404
        and body.get("detail") == "Not Found"
        and body.get("status") == 404,
    )
    check("no bespoke {'error': ...} key", "error" not in body)


# --- Findings 1 + 8: native wrapper end-to-end (streaming + request-id) -----
def test_native_wrapper_streaming_and_reqid():
    print("Findings 1 + 8 — native wrapper: streaming body + X-Request-ID echo:")
    app = HyperApp(title="r8")

    async def stream_handler(request):
        async def gen():
            yield b"hello "
            yield b"world"

        return Response.stream(gen())

    wrapped = HyperApp._wrap_handler_for_zig(
        stream_handler, app._middleware, app._resolve_exception, app=app
    )

    # A streaming Response now returns the CHUNKED 5-tuple
    #   (status, ct, b"", extra_headers, pull)
    # where pull() yields chunks INCREMENTALLY (the Zig side frames each as a
    # Transfer-Encoding: chunked frame) — replacing the old materialize-the-whole-
    # stream-into-body behavior that hung a worker on an infinite SSE stream.
    def _drain(pull):
        out = []
        while True:
            chunk = pull()
            if chunk is None:
                break
            out.append(chunk)
        return b"".join(out)

    result = wrapped(**_zig_kwargs(path="/stream"))
    check(
        "streaming -> 5-tuple with a pull callable",
        len(result) == 5 and callable(result[4]),
    )
    status, ct, body, extra, pull = result
    check("streaming response -> 200", status == 200)
    check(
        "streaming tuple carries EMPTY inline body (chunks come from pull)", body == b""
    )
    check(
        "pull() yields the streamed chunks incrementally",
        _drain(pull) == b"hello world",
    )
    check(
        "X-Request-ID echoed in extra headers",
        extra is not None and "x-request-id" in extra.lower(),
    )

    # SSE variant
    async def sse_handler(request):
        async def events():
            yield "one"
            yield "two"

        return Response.sse(events())

    wrapped_sse = HyperApp._wrap_handler_for_zig(
        sse_handler, app._middleware, app._resolve_exception, app=app
    )
    status, ct, body, extra, pull = wrapped_sse(**_zig_kwargs(path="/sse"))
    sse_body = _drain(pull)
    check(
        "SSE chunks stream event frames via pull()",
        status == 200 and b"data: one" in sse_body and b"data: two" in sse_body,
    )

    # Inbound request-id is honored end-to-end and echoed unchanged.
    async def echo_handler(request):
        return Response.json({"rid": request.request_id})

    wrapped_echo = HyperApp._wrap_handler_for_zig(
        echo_handler, app._middleware, app._resolve_exception, app=app
    )
    status, ct, body, extra = wrapped_echo(
        **_zig_kwargs(path="/echo", headers={"X-Request-ID": "trace-777"})
    )
    check(
        "handler sees inbound request_id on request.request_id",
        fast_json_loads(body)["rid"] == "trace-777",
    )
    check("inbound request_id echoed in response headers", "trace-777" in (extra or ""))

    # Non-Response scalar return goes through the shared coercion on native too.
    def scalar_handler(request):
        return 123

    wrapped_scalar = HyperApp._wrap_handler_for_zig(
        scalar_handler, app._middleware, app._resolve_exception, app=app
    )
    status, ct, body, extra = wrapped_scalar(**_zig_kwargs(path="/scalar"))
    check(
        "native scalar return -> JSON 200 (parity, not a 500)",
        status == 200 and fast_json_loads(body) == 123,
    )


# --- Finding 6: native safety-net routes through app._resolve_exception -----
def test_native_safety_net_custom_handler():
    print("Finding 6 — native safety-net uses app._resolve_exception:")
    app = HyperApp(title="r8-exc")

    class Boom(Exception):
        pass

    @app.exception_handler(Boom)
    async def handle_boom(request, exc):
        return Response.json({"handled": True}, status=418)

    # A middleware that raises AFTER _inner_dispatch's boundary lands in the
    # wrapper safety-net; it must still hit the custom handler (parity w/ ASGI).
    async def raising_mw(request, call_next):
        raise Boom("kaboom")

    app.use(raising_mw)

    async def ok_handler(request):
        return Response.json({"ok": True})

    wrapped = HyperApp._wrap_handler_for_zig(
        ok_handler, app._middleware, app._resolve_exception, app=app
    )
    status, ct, body, extra = wrapped(**_zig_kwargs(path="/boom"))
    check(
        "middleware-raised exc -> custom @app.exception_handler fires on native path",
        status == 418 and fast_json_loads(body).get("handled") is True,
    )


def main() -> bool:
    print("=" * 70)
    print("Round-8 dispatch-parity regression suite")
    print("=" * 70)
    test_coerce_response()
    test_request_id()
    test_client_ip_scope()
    test_static_404_shape()
    test_native_wrapper_streaming_and_reqid()
    test_native_safety_net_custom_handler()
    print("=" * 70)
    return finish()


if __name__ == "__main__":
    run_main(main)
