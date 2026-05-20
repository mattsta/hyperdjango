"""Wire-level HTTP conformance gate for the native Zig server.

The production server is the native Zig HTTP path (``HyperApp.run`` →
``_run_native``). It has historically shipped whole classes of framing bugs
that ``http.client``-based tests never see, because ``http.client`` normalizes
the response (strips the body on HEAD, hides duplicate framing headers,
re-frames chunked). This gate drives **raw HTTP bytes** over a real socket
against a live ``AppRunner`` server and asserts on the exact bytes on the wire:

  1. HEAD → identical status/headers + correct Content-Length but ZERO body
     bytes after the header terminator (RFC 7230 §3.3.3 — a body here desyncs
     the next keep-alive/pipelined request).
  2. 204 / 304 → no body and no Content-Length framing.
  3. Duplicate Content-Length → 400. Transfer-Encoding + Content-Length → 400.
     Non-numeric Content-Length → 400. (request smuggling defense)
  4. Pipelined requests on one keep-alive connection → exactly N framed
     responses, in order, no desync.
  5. A Django-style response that sets its own Content-Length → exactly ONE
     Content-Length header on the wire (no duplication).
  6. A streaming / SSE response → a non-empty body on the wire (the native
     tuple has no chunked-send API, so the body must be materialized — a bug
     here ships a 200 with zero bytes).

The app under test is defined at module top level in THIS file; ``AppRunner``
imports it in the server subprocess via a ``PYTHONPATH`` that points at the
scripts dir (so no separate fixture module is needed — the gate is one file).

NOTE: drives the live Zig HTTP server, so it requires the rebuilt extension
(`uv run hyper-build`). Run via:  uv run hyper-test http_conformance
"""

# hyper-test: e2e

import asyncio
import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_helper import AppRunner  # noqa: E402

from hyperdjango import HyperApp, Response  # noqa: E402

PORT = 19150

# ── App under test ───────────────────────────────────────────────────────────
app = HyperApp(title="http-conformance-fixture")

_HEAD_BODY = "HEAD-BODY-STRIPPED-OK"  # 21 bytes


@app.get("/text")
async def text(request):
    # GET-only route; used for the HEAD-on-a-GET-route conformance check.
    return Response.text("plain-get-body")


@app.route("/head-ok", methods=["GET", "HEAD"])
async def head_ok(request):
    # Explicitly HEAD-registered so the native server routes HEAD here; the
    # server must return the identical Content-Length a GET would but no body.
    return Response.text(_HEAD_BODY)


@app.get("/n/{n}")
async def numbered(request, n):
    # Distinct, framed bodies so pipelined-response ORDER can be verified.
    return Response.text(f"n={n}")


@app.get("/empty204")
async def empty204(request):
    return Response.empty(status=204)


@app.get("/etag")
async def etag(request):
    # Emits 304 when the client sends If-None-Match matching the ETag.
    resp = Response.text("cacheable-body")
    resp.set_etag("conf-v1")
    resp.check_not_modified(request)
    return resp


@app.get("/own-cl")
async def own_cl(request):
    # Django-style: the handler sets its OWN Content-Length. The wire must
    # carry exactly ONE Content-Length, not the handler's plus a server one.
    body = b"twelve-chars"  # 12 bytes
    return Response(
        body=body,
        status=200,
        headers={"content-length": str(len(body)), "content-type": "text/plain"},
    )


@app.get("/stream")
async def stream(request):
    async def gen():
        yield "stream-chunk-1\n"
        yield "stream-chunk-2\n"

    return Response.stream(gen(), content_type="text/plain")


@app.get("/sse")
async def sse(request):
    async def events():
        yield {"event": "tick", "data": "one"}
        yield {"event": "tick", "data": "two"}

    return Response.sse(events())


@app.get("/stream-slow")
async def stream_slow(request):
    # First chunk is yielded IMMEDIATELY, then the generator sleeps before the
    # second — so an incremental (chunked) send delivers "FIRST-CHUNK" to the
    # client well before the response as a whole finishes. If the server
    # materialized the stream, the first bytes would only arrive after the sleep.
    async def gen():
        yield "FIRST-CHUNK\n"
        await asyncio.sleep(0.5)
        yield "SECOND-CHUNK\n"

    return Response.stream(gen(), content_type="text/plain")


@app.get("/sse-infinite")
async def sse_infinite(request):
    # The NORMAL SSE heartbeat pattern: an UNBOUNDED event stream. The old
    # materialize-then-send path drained this forever on a worker thread (a
    # thread-pool DoS) and never sent a byte. Real chunked streaming must deliver
    # events incrementally and stop when the client disconnects.
    async def events():
        i = 0
        while True:
            yield {"event": "tick", "data": str(i)}
            i += 1
            await asyncio.sleep(0.01)

    return Response.sse(events())


@app.post("/echo")
async def echo(request):
    body = await request.text()
    return Response.text(f"got:{len(body)}")


@app.get("/boom")
async def boom(request):
    raise RuntimeError("intentional handler failure for conformance")


@app.get("/inject-header")
async def inject_header(request):
    # A handler-set header value carrying an embedded CRLF must be sanitized —
    # it must NOT inject a second response header on the wire (C3-G1).
    r = Response.text("ok")
    r.headers["x-user"] = "safe\r\nX-Injected: pwned"
    return r


@app.route("/echo-req/{seg}", methods=["GET"])
async def echo_req(request, seg):
    # Echoes decoded request.path, the path param, and a header value so the
    # conformance suite can assert charset handling on the wire (F1/F3).
    return {
        "path": request.path,
        "seg": seg,
        "xfoo": request.headers.get("x-foo", ""),
    }


# ── Harness ──────────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0
XFAIL = 0


def _ok(name, detail=""):
    global PASS
    PASS += 1
    print(f"  PASS  {name}" + (f" ({detail})" if detail else ""))


def _bad(name, detail):
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name}: {detail}")


def check(name, cond, detail=""):
    if cond:
        _ok(name, detail)
    else:
        _bad(name, detail or "condition false")


def check_eq(name, got, expected):
    if got == expected:
        _ok(name, repr(got))
    else:
        _bad(name, f"expected {expected!r}, got {got!r}")


def xfail(name, cond_now, reason):
    """Record a KNOWN native-side divergence. Never fails the suite.

    ``cond_now`` is the CURRENT (broken) observation. If it flips (the native
    wave fixed it), we shout XPASS so the marker gets removed and enforcement
    turns on.
    """
    global XFAIL
    XFAIL += 1
    if cond_now:
        print(f"  XFAIL {name}: {reason}")
    else:
        print(f"  XPASS {name}: NOW FIXED — remove the xfail and enforce. ({reason})")


# ── Raw socket helpers ───────────────────────────────────────────────────────
def _recv_until_close(sock, deadline=5.0):
    """Read all bytes until the server closes (Connection: close)."""
    sock.settimeout(deadline)
    buf = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except TimeoutError:
        pass
    return buf


def raw_exchange(host, port, request_bytes):
    """Send raw bytes on a fresh connection, return the full raw response."""
    with socket.create_connection((host, port), timeout=5) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(request_bytes)
        return _recv_until_close(s)


def split_head_body(raw):
    """Split a raw HTTP response into (status:int, headers:list[(k,v)], body:bytes)."""
    if b"\r\n\r\n" not in raw:
        return None, [], b""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ")
    status = (
        int(status_parts[1])
        if len(status_parts) >= 2 and status_parts[1].isdigit()
        else None
    )
    headers = []
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers.append((k.strip().lower(), v.strip()))
    return status, headers, body


def header_values(headers, name):
    name = name.lower().encode()
    return [v for (k, v) in headers if k == name]


def dechunk(body):
    """Decode a Transfer-Encoding: chunked body into its payload bytes."""
    out = b""
    i = 0
    while i < len(body):
        j = body.find(b"\r\n", i)
        if j == -1:
            break
        try:
            size = int(body[i:j].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = j + 2
        out += body[start : start + size]
        i = start + size + 2  # skip payload + trailing CRLF
    return out


def read_until_marker(sock, marker, deadline=3.0):
    """Read until ``marker`` appears; return (elapsed_seconds, buffer) or (None, buf)."""
    sock.settimeout(deadline)
    start = time.monotonic()
    buf = b""
    try:
        while marker not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except TimeoutError:
        return None, buf
    return (time.monotonic() - start if marker in buf else None), buf


def read_some(sock, min_bytes, deadline=3.0):
    """Read at least ``min_bytes`` bytes (or until deadline), then return the buffer."""
    sock.settimeout(deadline)
    buf = b""
    try:
        while len(buf) < min_bytes:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except TimeoutError:
        pass
    return buf


def _read_framed(sock, count, deadline=10.0):
    """Read exactly ``count`` Content-Length-framed responses. Returns [(status, body)]."""
    sock.settimeout(deadline)
    buf = b""
    out = []
    while len(out) < count:
        while b"\r\n\r\n" in buf:
            head, _, rest = buf.partition(b"\r\n\r\n")
            cl = 0
            for line in head.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    cl = int(line.split(b":", 1)[1].strip())
            if len(rest) < cl:
                break
            status = int(head.split(b" ")[1])
            out.append((status, rest[:cl]))
            buf = rest[cl:]
            if len(out) >= count:
                return out
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError(
                f"connection closed after {len(out)}/{count} responses"
            )
        buf += chunk
    return out


# ── Tests ────────────────────────────────────────────────────────────────────
def run(host, port):
    # 1. HEAD returns headers + correct Content-Length but ZERO body bytes.
    get_raw = raw_exchange(
        host, port, b"GET /head-ok HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    g_status, g_headers, g_body = split_head_body(get_raw)
    head_raw = raw_exchange(
        host, port, b"HEAD /head-ok HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    h_status, h_headers, h_body = split_head_body(head_raw)
    check_eq("HEAD /head-ok status", h_status, 200)
    check_eq(
        "HEAD Content-Length == GET body length",
        header_values(h_headers, "content-length"),
        [str(len(g_body)).encode()],
    )
    check(
        "HEAD emits ZERO body bytes after header terminator",
        h_body == b"",
        f"got {h_body!r}",
    )
    check_eq("GET /head-ok body intact (control)", g_body, _HEAD_BODY.encode())

    # HEAD against a GET-ONLY route: RFC says it behaves like GET (200, no body).
    # The Zig router now aliases HEAD→GET when no explicit HEAD route exists
    # (fixed in the native wave), so a GET-only endpoint answers HEAD with the
    # status/headers and an empty body.
    head_getonly = raw_exchange(
        host, port, b"HEAD /text HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    ho_status, _ho_headers, ho_body = split_head_body(head_getonly)
    check_eq("HEAD on a GET-only route -> 200 (aliased to GET)", ho_status, 200)
    check(
        "HEAD on a GET-only route emits ZERO body", ho_body == b"", f"got {ho_body!r}"
    )

    # 2. 204 → no body, no Content-Length framing.
    r204 = raw_exchange(
        host, port, b"GET /empty204 HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    s204, h204, b204 = split_head_body(r204)
    check_eq("204 status", s204, 204)
    check("204 has ZERO body bytes", b204 == b"", f"got {b204!r}")
    check(
        "204 has NO Content-Length header",
        header_values(h204, "content-length") == [],
        f"got {header_values(h204, 'content-length')}",
    )
    # RFC 7231 permits representation metadata (Content-Type) on 204; the
    # framework's Response.empty() defaults one on BOTH dispatch paths, so a
    # Content-Type here is not a native-specific framing bug. The load-bearing
    # 204 invariants (no body, no Content-Length) are asserted above.

    # 3. 304 → no body, no Content-Length framing.
    r304 = raw_exchange(
        host,
        port,
        b'GET /etag HTTP/1.1\r\nHost: t\r\nIf-None-Match: "conf-v1"\r\nConnection: close\r\n\r\n',
    )
    s304, h304, b304 = split_head_body(r304)
    check_eq("304 status", s304, 304)
    check("304 has ZERO body bytes", b304 == b"", f"got {b304!r}")
    check(
        "304 has NO Content-Length header",
        header_values(h304, "content-length") == [],
        f"got {header_values(h304, 'content-length')}",
    )

    # 4. Malformed / conflicting Content-Length in a REQUEST → 400.
    dup = (
        b"POST /echo HTTP/1.1\r\nHost: t\r\n"
        b"Content-Length: 5\r\nContent-Length: 6\r\nConnection: close\r\n\r\nhello"
    )
    check_eq(
        "duplicate Content-Length -> 400",
        split_head_body(raw_exchange(host, port, dup))[0],
        400,
    )

    te_cl = (
        b"POST /echo HTTP/1.1\r\nHost: t\r\n"
        b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
    )
    check_eq(
        "Transfer-Encoding + Content-Length -> 400",
        split_head_body(raw_exchange(host, port, te_cl))[0],
        400,
    )

    nonnum = (
        b"POST /echo HTTP/1.1\r\nHost: t\r\n"
        b"Content-Length: abc\r\nConnection: close\r\n\r\n"
    )
    check_eq(
        "non-numeric Content-Length -> 400",
        split_head_body(raw_exchange(host, port, nonnum))[0],
        400,
    )

    # 5. Pipelined requests on ONE keep-alive connection → N framed responses in order.
    with socket.create_connection((host, port), timeout=5) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ns = [11, 22, 33, 44]
        pipelined = b"".join(
            f"GET /n/{n} HTTP/1.1\r\nHost: t\r\nConnection: keep-alive\r\n\r\n".encode()
            for n in ns
        )
        s.sendall(pipelined)
        got = _read_framed(s, len(ns))
    check_eq("pipelined: exactly N responses", len(got), len(ns))
    check_eq("pipelined: all 200", [st for st, _ in got], [200] * len(ns))
    check_eq(
        "pipelined: bodies in order (no desync)",
        [b for _, b in got],
        [f"n={n}".encode() for n in ns],
    )

    # 6. Django-style response with its OWN Content-Length → exactly ONE on the wire.
    r_owncl = raw_exchange(
        host, port, b"GET /own-cl HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    s_owncl, h_owncl, b_owncl = split_head_body(r_owncl)
    cls = header_values(h_owncl, "content-length")
    check_eq("own-Content-Length: status", s_owncl, 200)
    check_eq("own-Content-Length: exactly ONE Content-Length header", len(cls), 1)
    check_eq(
        "own-Content-Length: value == body length", cls, [str(len(b_owncl)).encode()]
    )
    check_eq("own-Content-Length: body intact", b_owncl, b"twelve-chars")

    # 7. Streaming / SSE responses → NON-EMPTY body on the wire.
    r_stream = raw_exchange(
        host, port, b"GET /stream HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    _, _, b_stream = split_head_body(r_stream)
    check(
        "streaming response has a NON-EMPTY body",
        len(b_stream) > 0,
        f"body={b_stream!r}",
    )
    check(
        "streaming body carries both chunks",
        b"stream-chunk-1" in b_stream and b"stream-chunk-2" in b_stream,
    )

    r_sse = raw_exchange(
        host, port, b"GET /sse HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    _, h_sse, b_sse = split_head_body(r_sse)
    check("SSE response has a NON-EMPTY body", len(b_sse) > 0, f"body={b_sse!r}")

    # 8. Streaming is REAL chunked transfer-encoding (not a materialized body).
    s_h, h_h, _ = split_head_body(r_stream)
    check(
        "stream: Transfer-Encoding: chunked",
        header_values(h_h, "transfer-encoding") == [b"chunked"],
        f"got {header_values(h_h, 'transfer-encoding')}",
    )
    check(
        "stream: NO Content-Length (chunked owns framing)",
        header_values(h_h, "content-length") == [],
    )
    check_eq(
        "stream: chunk-framed body decodes to both chunks in order",
        dechunk(b_stream),
        b"stream-chunk-1\nstream-chunk-2\n",
    )
    check(
        "SSE: Transfer-Encoding: chunked",
        header_values(h_sse, "transfer-encoding") == [b"chunked"],
    )
    check(
        "SSE: chunk-framed body carries both data lines",
        b"data: one" in dechunk(b_sse) and b"data: two" in dechunk(b_sse),
    )

    # 9. INCREMENTALITY: the first chunk must reach the client BEFORE the
    # generator finishes (it sleeps 0.5s between the two chunks). If the server
    # buffered/materialized the stream, "FIRST-CHUNK" would only arrive after the
    # full 0.5s. We require it well under that.
    with socket.create_connection((host, port), timeout=5) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(b"GET /stream-slow HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        elapsed, buf = read_until_marker(s, b"FIRST-CHUNK", deadline=3.0)
    check(
        "streaming is INCREMENTAL: first chunk arrives before generator finishes",
        elapsed is not None and elapsed < 0.4,
        f"elapsed={elapsed}",
    )
    check(
        "stream-slow eventually delivers the second chunk too",
        b"SECOND-CHUNK" in _recv_until_close_after(host, port, "/stream-slow"),
    )

    # 10. INFINITE stream must NOT hang: read a bounded prefix, confirm multiple
    # DISTINCT events arrived incrementally, then close — the server must detect
    # the disconnect and not wedge (a following request still succeeds).
    with socket.create_connection((host, port), timeout=5) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(
            b"GET /sse-infinite HTTP/1.1\r\nHost: t\r\nConnection: keep-alive\r\n\r\n"
        )
        prefix = read_some(s, 512, deadline=3.0)
    _, h_inf, b_inf = split_head_body(prefix)
    check(
        "infinite SSE: Transfer-Encoding: chunked",
        header_values(h_inf, "transfer-encoding") == [b"chunked"],
    )
    decoded_inf = dechunk(b_inf)
    check(
        "infinite SSE: multiple distinct events streamed incrementally",
        b"data: 0" in decoded_inf and b"data: 1" in decoded_inf,
        f"decoded={decoded_inf[:120]!r}",
    )
    # The connection was closed by us mid-stream; the server must recover and
    # still serve — proof the infinite stream did not hang the server.
    after = raw_exchange(
        host, port, b"GET /text HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    check(
        "server still responds after an infinite stream was disconnected",
        split_head_body(after)[0] == 200,
    )

    # 11. Unified error contract on the wire. The framework's structured error
    # bodies are {"detail":..., "status":...} — never bespoke {"error":...}.
    # (A Python handler that RAISES is a separate path: in DEBUG it renders an
    # HTML traceback page, so /boom below only asserts the server returns 500
    # without crashing. The native structured-error bodies — 400 malformed and
    # 404 unrouted — are what carry the unified JSON shape W3 unified.)
    def _assert_unified_error(name, status_line, body, expect_status):
        check_eq(f"{name}: status", status_line, expect_status)
        try:
            doc = json.loads(body)
        except ValueError, TypeError:
            _bad(name, f"body is not JSON: {body!r}")
            return
        check(f"{name}: has 'detail'", "detail" in doc, f"body={body!r}")
        check(
            f"{name}: 'status' == {expect_status}",
            doc.get("status") == expect_status,
            f"status field={doc.get('status')!r}",
        )
        check(f"{name}: no bespoke 'error' key", "error" not in doc, f"body={body!r}")

    # 11a. A raising handler must yield 500 and not crash the server (body is an
    # HTML debug page under DEBUG — path-dependent, so status-only here).
    r_boom = raw_exchange(
        host, port, b"GET /boom HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    s_boom, _h_boom, _b_boom = split_head_body(r_boom)
    check_eq("500 handler-raise: status", s_boom, 500)
    # The server must recover and keep serving after a handler exception.
    after_boom = raw_exchange(
        host, port, b"GET /text HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    check(
        "server still responds after a handler raised",
        split_head_body(after_boom)[0] == 200,
    )

    # 11b. Native 400 (duplicate Content-Length) — unified JSON error body.
    r_400 = raw_exchange(
        host,
        port,
        b"POST /echo HTTP/1.1\r\nHost: t\r\nContent-Length: 1\r\nContent-Length: 2\r\n"
        b"Connection: close\r\n\r\nx",
    )
    s_400, _h_400, b_400 = split_head_body(r_400)
    _assert_unified_error("400 duplicate-Content-Length", s_400, b_400, 400)

    # 11c. Native 404 (unrouted) — unified JSON error body.
    r_404 = raw_exchange(
        host,
        port,
        b"GET /no-such-route HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
    )
    s_404, _h_404, b_404 = split_head_body(r_404)
    _assert_unified_error("404 unrouted", s_404, b_404, 404)

    # 13. Header injection (C3-G1): a handler-set header value with an embedded
    # CRLF must NOT appear as a separate response header on the native wire.
    r_inj = raw_exchange(
        host,
        port,
        b"GET /inject-header HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
    )
    _s_inj, h_inj, _b_inj = split_head_body(r_inj)
    inj_names = [k.lower() for (k, _v) in h_inj]
    check(
        "CRLF in header value does not inject a header (C3-G1)",
        b"x-injected" not in inj_names,
        f"headers={inj_names}",
    )
    # A split attempt TRUNCATES: everything after the CRLF is attacker
    # payload, so only the safe prefix survives (it must not be concatenated
    # back into the value either).
    xuser = header_values(h_inj, "x-user")
    check(
        "injected header value truncated at the split attempt",
        xuser and xuser[0] == b"safe",
        f"x-user={xuser}",
    )

    # 12. Non-UTF-8 / non-ASCII charset handling on the parse path (F1/F3).
    #  - Header value carries a raw 0xE9 byte (Latin-1 'é'): must NOT 500 (F1) and
    #    must decode Latin-1 (matching request.py from_asgi).
    #  - Path `/echo-req/caf%C3%A9` percent-decodes to UTF-8 bytes for 'é': the
    #    decoded request.path and path param must be UTF-8 'café' (ASGI parity, F3).
    r_echo = raw_exchange(
        host,
        port,
        b"GET /echo-req/caf%C3%A9 HTTP/1.1\r\nHost: t\r\nX-Foo: caf\xe9\r\n"
        b"Connection: close\r\n\r\n",
    )
    s_echo, _h_echo, b_echo = split_head_body(r_echo)
    check_eq("non-UTF-8 header + UTF-8 path: status 200 (no F1 leak)", s_echo, 200)
    try:
        echo = json.loads(b_echo)
    except ValueError, TypeError:
        _bad("echo-req body is JSON", f"body={b_echo!r}")
        echo = {}
    check_eq("path decoded UTF-8 (F3)", echo.get("path"), "/echo-req/café")
    check_eq("path param decoded UTF-8 (F3)", echo.get("seg"), "café")
    check_eq("header decoded Latin-1 (F1 parity)", echo.get("xfoo"), "café")

    # The worker must be exception-clean afterwards (a leaked pending exception
    # from a non-UTF-8 decode would poison this next request) — F1.
    r_after_echo = raw_exchange(
        host, port, b"GET /text HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    check(
        "worker clean after non-UTF-8 request (no F1 leak)",
        split_head_body(r_after_echo)[0] == 200,
    )

    # 12. Adversarial request-line / header inputs must NOT crash the worker.
    # Each is fired, its response ignored (a 4xx or a bare connection close are
    # both acceptable), then a normal request MUST still return 200 — proving the
    # native parser rejected the garbage without killing the worker/loop.
    adversarial = [
        b"GET / HTTP/1.1\r\n"
        + b"X-Big: "
        + b"A" * 200_000
        + b"\r\nHost: t\r\nConnection: close\r\n\r\n",  # giant header
        b"GET /"
        + b"a" * 200_000
        + b" HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",  # giant request-target
        b"\x00\x01\x02 / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",  # NUL/control in method
        b"GET / HTTP/1.1\r\nHost: t\r\n"
        + (b"X-H: v\r\n" * 20000)
        + b"Connection: close\r\n\r\n",  # header flood
        b"PBLORGLE /x FTPS/9.9\r\nHost: t\r\nConnection: close\r\n\r\n",  # garbage method + version
        b"GET / HTTP/1.1\r\nContent-Length: -5\r\nHost: t\r\nConnection: close\r\n\r\n",  # negative CL
        b"GET /\r\n\r\n",  # no version, no headers
    ]
    for i, payload in enumerate(adversarial):
        # a connection reset on garbage is acceptable
        with contextlib.suppress(ConnectionError, OSError):
            raw_exchange(
                host, port, payload
            )  # response is allowed to be 4xx or empty/close
        alive = raw_exchange(
            host, port, b"GET /text HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
        )
        check(
            f"worker survives adversarial input #{i} (200 after)",
            split_head_body(alive)[0] == 200,
        )


def _recv_until_close_after(host, port, path):
    with socket.create_connection((host, port), timeout=5) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n".encode()
        )
        return dechunk(split_head_body(_recv_until_close(s))[2])


def main():
    print("=" * 64)
    print("HTTP wire-level conformance (native Zig server)")
    print("=" * 64)

    scripts_dir = str(Path(__file__).resolve().parent)
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = scripts_dir + (os.pathsep + existing if existing else "")

    with AppRunner(
        "test_http_conformance:app", port=PORT, env={"PYTHONPATH": pythonpath}
    ) as r:
        run(r.host, r.port)

    print(
        f"\nResults: {PASS} passed, {FAIL} failed, {XFAIL} xfail (known native-wave gaps)"
    )
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
