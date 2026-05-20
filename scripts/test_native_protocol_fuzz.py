"""Native protocol safety-validation suite — adversarial bytes at the live Zig server.

# hyper-test: e2e

Drives MALFORMED raw bytes at the running native server over real sockets and
asserts, after every case, that the worker still answers a normal request 200
(i.e. the parser rejected/closed the garbage WITHOUT crashing the worker thread).
This is the discipline that caught the `GET /\r\n\r\n` out-of-bounds worker DoS
(server.zig handleOneRequest); the request-line probes for that live in
test_http_conformance.py. THIS file is the growable home for every OTHER native
parser that touches untrusted bytes:

    - WebSocket frame parser  (unmasked, reserved opcodes, RSV, fragmented
                               control, oversized control, 64-bit length,
                               truncated, bad close codes, random garbage)
    - multipart/form-data     (bad/empty/missing boundary, header-less parts,
                               no blank line / no trailing boundary, giant field
                               name, NUL bytes, empty body, part-count flood)
    - chunked transfer-encoding (bad hex size, overflow, missing CRLF, no final
                               0-chunk, chunk-ext garbage, trailer flood)
    - reactor / keep-alive    (slowloris partial headers, pipelined flood,
                               immediate RST, oversized single request)

ADD NEW CASES HERE as new native parsers/edge cases are found — each case is one
line via the `probe_*` helpers, so this suite grows cheaply and re-runs forever.

Requires the built native extension (`uv run hyper-build --install`).
Run: uv run hyper-test native_protocol_fuzz
"""

import base64
import os
import socket
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib

from e2e_helper import TEST_PORTS, AppRunner  # noqa: E402

from hyperdjango import HyperApp, Response  # noqa: E402

# Unique, collision-free port from the shared registry (never hardcode — that is
# what caused a spurious cross-test failure under parallel execution).
PORT = TEST_PORTS["native_protocol_fuzz"]

# ── App under test — one server exercises every parser ───────────────────────
app = HyperApp(title="native-protocol-fuzz-fixture")


@app.get("/alive")
def alive_route(request):
    return Response.text("ok")


@app.post("/upload")
async def upload_route(request):
    # Exercise the multipart decoder. Any parse failure is a clean 400, never a
    # crash — that is exactly what these cases assert.
    try:
        form = await request.form()
        return Response.json({"fields": len(form) if form else 0})
    except Exception:
        return Response.error(400, "bad form")


@app.post("/echo")
async def echo_route(request):
    # Exercise the request-body reader (Content-Length / chunked).
    body = await request.bytes()
    return Response.json({"len": len(body)})


@app.websocket("/ws")
async def ws_echo_route(ws):
    await ws.accept()
    try:
        async for msg in ws.iter_text():
            await ws.send_text(msg)
    except Exception:
        pass


# A known-size on-disk file for exercising the native Range parser (serveFile).
# 1000 bytes of deterministic content so range math can be checked exactly.
_RANGE_FILE = Path(tempfile.gettempdir()) / "hd_native_fuzz_range.bin"
_RANGE_FILE_SIZE = 1000
_RANGE_FILE.write_bytes(bytes(i % 256 for i in range(_RANGE_FILE_SIZE)))


@app.get("/file")
def file_route(request):
    # Pass request= so Response.file honors HTTP Range (RFC 7233) — this is what
    # makes <video>/<audio> seeking + resumable downloads work.
    return Response.file(
        str(_RANGE_FILE), content_type="application/octet-stream", request=request
    )


# ── Result tracking ──────────────────────────────────────────────────────────
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: {detail}")


# ── Reusable socket helpers (grow these as new protocols are fuzzed) ──────────
class Server:
    """Thin handle carrying host/port + the primitives every probe reuses."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def _conn(self, timeout: float = 3.0) -> socket.socket:
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def alive(self) -> bool:
        """True iff a normal GET still returns 200 (worker survived)."""
        try:
            s = self._conn()
            s.sendall(b"GET /alive HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
            data = s.recv(64)
            s.close()
            return data[:12] == b"HTTP/1.1 200"
        except OSError:
            return False

    def raw(self, payload: bytes, read: int = 256) -> bytes:
        """Send raw bytes on a fresh connection; return whatever comes back."""
        try:
            s = self._conn(timeout=4.0)
            s.sendall(payload)
            s.settimeout(2.0)
            try:
                out = s.recv(read)
            except OSError:
                out = b""
            s.close()
            return out
        except OSError:
            return b""

    def raw_full(self, payload: bytes) -> bytes:
        """Send raw bytes; read the FULL response until the peer closes."""
        buf = b""
        try:
            s = self._conn(timeout=4.0)
            s.sendall(payload)
            s.settimeout(2.0)
            try:
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            s.close()
        except OSError:
            pass
        return buf

    # WebSocket ------------------------------------------------------------
    def ws_open(self) -> socket.socket | None:
        """Perform the WS handshake; return the upgraded socket or None."""
        s = self._conn(timeout=4.0)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(
            f"GET /ws HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        resp = s.recv(1024)
        if b"101" not in resp:
            s.close()
            return None
        return s

    def ws_send_raw(self, frame_bytes: bytes) -> None:
        """Open a WS, send raw frame bytes, drain, close. Never raises."""
        s = self.ws_open()
        if s is None:
            return
        try:
            s.sendall(frame_bytes)
            s.settimeout(1.0)
            with contextlib.suppress(OSError):
                s.recv(256)
        except OSError:
            pass
        finally:
            s.close()


def ws_frame(
    opcode: int,
    payload: bytes,
    *,
    fin: bool = True,
    rsv: int = 0,
    masked: bool = True,
    length_override: int | None = None,
) -> bytes:
    """Build a raw WebSocket frame (client frames should be masked)."""
    b0 = (0x80 if fin else 0) | (rsv << 4) | (opcode & 0x0F)
    n = length_override if length_override is not None else len(payload)
    mbit = 0x80 if masked else 0
    hdr = bytes([b0])
    if n < 126:
        hdr += bytes([mbit | n])
    elif n < 65536:
        hdr += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        hdr += bytes([mbit | 127]) + struct.pack(">Q", n)
    body = payload
    if masked:
        mk = os.urandom(4)
        hdr += mk
        body = bytes(payload[i] ^ mk[i % 4] for i in range(len(payload)))
    return hdr + body


def multipart(parts: list[tuple[str, str]], boundary: str = "----b0undary") -> bytes:
    """Build a well-formed multipart body from (name, value) pairs."""
    out = b""
    for name, value in parts:
        out += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
            f"\r\n{value}\r\n"
        ).encode()
    return out + f"--{boundary}--\r\n".encode()


def http_post(path: str, body: bytes, content_type: str) -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\nHost: t\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


# ── The fuzz cases ───────────────────────────────────────────────────────────
def fuzz_websocket_frames(sv: Server) -> None:
    print("\n── WebSocket frame parser ──")
    B = "----b0undary"  # noqa: F841 (kept for parity; unused here)
    cases = [
        ("unmasked client text (RFC: must close)", ws_frame(0x1, b"hi", masked=False)),
        ("reserved data opcode 0x3", ws_frame(0x3, b"x")),
        ("reserved control opcode 0xB", ws_frame(0xB, b"x")),
        ("RSV1 set without extension", ws_frame(0x1, b"x", rsv=4)),
        ("fragmented control frame (FIN=0 ping)", ws_frame(0x9, b"x", fin=False)),
        ("control frame payload > 125", ws_frame(0x9, b"y" * 200)),
        (
            "64-bit length header, huge value",
            bytes([0x81, 0xFF]) + struct.pack(">Q", 2**63) + os.urandom(8),
        ),
        ("truncated frame (1 byte)", bytes([0x81])),
        (
            "truncated frame (header only, no payload)",
            bytes([0x81, 0x85]) + os.urandom(4),
        ),
        ("close frame, invalid code 999", ws_frame(0x8, struct.pack(">H", 999) + b"x")),
        (
            "close frame, masked garbage",
            bytes([0x88, 0x82]) + os.urandom(4) + b"\x00\x00",
        ),
        ("random garbage frame", os.urandom(64)),
        ("binary frame with embedded NUL", ws_frame(0x2, b"\xff\xfe\x00bin")),
        ("length claims 126 but no ext-len bytes", bytes([0x81, 0xFE])),
        ("length claims 127 but no ext-len bytes", bytes([0x81, 0xFF])),
    ]
    for label, frame in cases:
        sv.ws_send_raw(frame)
        check(f"WS: {label}", sv.alive())


def fuzz_multipart(sv: Server) -> None:
    print("\n── multipart/form-data decoder ──")
    B = "----b0undary"
    ct = f"multipart/form-data; boundary={B}"
    good = multipart([("f", "val")], B)
    cases = [
        ("valid multipart", good, ct),
        ("no boundary present in body", b"garbage with no boundary", ct),
        ("part with no headers", f"--{B}\r\n\r\nnoheaders\r\n--{B}--\r\n".encode(), ct),
        (
            "part with no blank line before body",
            f'--{B}\r\nContent-Disposition: form-data; name="f"\r\nval--{B}--'.encode(),
            ct,
        ),
        (
            "no trailing boundary",
            f'--{B}\r\nContent-Disposition: form-data; name="f"\r\n\r\nval'.encode(),
            ct,
        ),
        (
            "giant field name (100k)",
            f'--{B}\r\nContent-Disposition: form-data; name="{"A" * 100000}"\r\n\r\nv\r\n--{B}--\r\n'.encode(),
            ct,
        ),
        ("giant boundary-ish garbage", b"--" + b"X" * 100000, ct),
        (
            "NUL bytes in value",
            f'--{B}\r\nContent-Disposition: form-data; name="f"\r\n\r\n\x00\x01\xff\r\n--{B}--\r\n'.encode(),
            ct,
        ),
        ("empty body", b"", ct),
        ("boundary-only (immediate end)", f"--{B}--\r\n".encode(), ct),
        ("empty boundary parameter", good, "multipart/form-data; boundary="),
        ("no boundary parameter", good, "multipart/form-data"),
    ]
    for label, body, content_type in cases:
        resp = sv.raw(http_post("/upload", body, content_type))
        check(f"multipart: {label}", sv.alive(), f"resp={resp[:16]!r}")

    # Part-count flood must be BOUNDED (413/400), not OOM — assert the bound
    # actually fires and the worker survives.
    flood = (
        f'--{B}\r\nContent-Disposition: form-data; name="f"\r\n\r\nv\r\n' * 5000
    ).encode() + f"--{B}--\r\n".encode()
    resp = sv.raw(http_post("/upload", flood, ct))
    status = resp.split(b" ", 2)[1] if resp[:4] == b"HTTP" else b"?"
    check(
        "multipart: 5000-part flood is rejected (bounded), worker alive",
        sv.alive() and status in (b"400", b"413"),
        f"status={status!r}",
    )


def fuzz_chunked(sv: Server) -> None:
    print("\n── chunked transfer-encoding decoder ──")

    def chunked_post(chunk_body: bytes, label: str) -> None:
        req = (
            b"POST /echo HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n" + chunk_body
        )
        resp = sv.raw(req)
        check(f"chunked: {label}", sv.alive(), f"resp={resp[:16]!r}")

    chunked_post(b"5\r\nhello\r\n0\r\n\r\n", "valid chunked")
    chunked_post(b"ZZ\r\nhello\r\n0\r\n\r\n", "non-hex chunk size")
    chunked_post(b"FFFFFFFFFFFFFFFF\r\nx\r\n0\r\n\r\n", "overflow chunk size")
    chunked_post(b"5\r\nhello0\r\n\r\n", "missing CRLF after chunk data")
    chunked_post(b"5\r\nhello\r\n", "missing final 0-chunk")
    chunked_post(
        b"5;ext=" + b"A" * 10000 + b"\r\nhello\r\n0\r\n\r\n", "giant chunk extension"
    )
    chunked_post(b"-5\r\nhello\r\n0\r\n\r\n", "negative-looking chunk size")
    chunked_post(b"0\r\n" + b"X-Trailer: v\r\n" * 5000 + b"\r\n", "trailer flood")
    chunked_post(b"", "empty chunked body")
    chunked_post(b"\r\n\r\n", "blank chunked body")


def fuzz_reactor_abuse(sv: Server) -> None:
    print("\n── reactor / keep-alive lifecycle ──")

    # Slowloris: send a partial request, hold, then drop — must not wedge a worker.
    try:
        s = sv._conn(timeout=3.0)
        s.sendall(b"GET /alive HTTP/1.1\r\nHost: t\r\n")  # no terminating blank line
        s.close()
    except OSError:
        pass
    check("reactor: partial (slowloris) request then drop", sv.alive())

    # Immediate RST after connect.
    try:
        s = sv._conn(timeout=3.0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()  # sends RST
    except OSError:
        pass
    check("reactor: immediate RST after connect", sv.alive())

    # Pipelined flood on ONE keep-alive connection (one FD — cheap, high value).
    try:
        s = sv._conn(timeout=5.0)
        s.sendall(
            b"GET /alive HTTP/1.1\r\nHost: t\r\nConnection: keep-alive\r\n\r\n" * 300
        )
        s.settimeout(2.0)
        try:
            while s.recv(65536):
                pass
        except OSError:
            pass
        s.close()
    except OSError:
        pass
    check("reactor: 300 pipelined requests on one connection", sv.alive())

    # Concurrent connections held open then released. Kept modest (48) so this
    # suite stays a good citizen under the full parallel test run — a larger burst
    # contended for system-wide FDs/ephemeral ports and starved neighbouring e2e
    # servers. 48 still proves the reactor accepts many simultaneous connections.
    conns = []
    try:
        for _ in range(48):
            conns.append(sv._conn(timeout=3.0))
    except OSError:
        pass
    for c in conns:
        with contextlib.suppress(OSError):
            c.close()
    check("reactor: 48 concurrent connections held then released", sv.alive())

    # Oversized single request (well past the header buffer) → 4xx/close, no crash.
    sv.raw(
        b"GET /" + b"a" * 500000 + b" HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
    )
    check("reactor: 500KB request target", sv.alive())


def _status_of(resp: bytes) -> bytes:
    return resp.split(b" ", 2)[1] if resp[:4] == b"HTTP" else b"?"


def _body_of(resp: bytes) -> bytes:
    i = resp.find(b"\r\n\r\n")
    return resp[i + 4 :] if i >= 0 else b""


def _valid_http_status(resp: bytes) -> bool:
    """True iff the response is a well-formed HTTP status line with a 3-digit
    code in [100, 599] — proves the worker produced a real response, not a crash
    or garbage bytes."""
    if not resp.startswith(b"HTTP/1."):
        return False
    st = _status_of(resp)
    return st.isdigit() and 100 <= int(st) <= 599


def fuzz_range(sv: Server) -> None:
    # SAFETY scope: the Range header on a served file must never crash the worker
    # or emit garbage, for ANY adversarial spec. (#124 added full RFC 7233 range
    # semantics — 206/416/suffix — to Response.file(request=…); the /file route
    # passes request=, so a real conformance check follows the crash-safety loop.)
    print("\n── Range header on served file (crash-safety) ──")
    specs = [
        "bytes=0-99",
        "bytes=0-",
        "bytes=-100",
        f"bytes=-{_RANGE_FILE_SIZE + 5000}",
        f"bytes={_RANGE_FILE_SIZE + 10}-{_RANGE_FILE_SIZE + 20}",
        "bytes=500-100",
        "bytes=-0",
        "bytes=abc-def",
        "bytes=99999999999999999999999999-",
        "bytes=-99999999999999999999999999",
        "bytes=--5",
        "bytes=",
        "bytes=0-10,20-30",
        "items=0-10",
        "0-10",
        "bytes=" + "-" * 5000,
        "bytes=0-" + "9" * 5000,
        "bytes=\x00\x01",
    ]
    for spec in specs:
        req = (
            f"GET /file HTTP/1.1\r\nHost: t\r\nRange: {spec}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        resp = sv.raw_full(req)
        check(
            f"Range spec {spec[:40]!r}",
            sv.alive() and _valid_http_status(resp),
            f"status={_status_of(resp)!r}",
        )

    # CONFORMANCE: a valid single range → 206 with Content-Range over the wire.
    print("── Range conformance (206 / 416 over the wire) ──")
    resp = sv.raw_full(
        b"GET /file HTTP/1.1\r\nHost: t\r\nRange: bytes=0-99\r\nConnection: close\r\n\r\n"
    )
    check(
        "valid range → 206", _status_of(resp) == b"206", f"status={_status_of(resp)!r}"
    )
    check(
        "206 carries Content-Range",
        b"content-range: bytes 0-99/" in resp.lower(),
        f"head={resp[:200]!r}",
    )
    # Unsatisfiable range → 416.
    resp = sv.raw_full(
        f"GET /file HTTP/1.1\r\nHost: t\r\nRange: bytes={_RANGE_FILE_SIZE + 100}-\r\n"
        f"Connection: close\r\n\r\n".encode()
    )
    check(
        "unsatisfiable range → 416",
        _status_of(resp) == b"416",
        f"status={_status_of(resp)!r}",
    )


def fuzz_header_edges(sv: Server) -> None:
    print("\n── header edge cases ──")
    base = b"GET /alive HTTP/1.1\r\nHost: t\r\n"
    tail = b"Connection: close\r\n\r\n"
    cases = [
        (
            "obs-fold continuation (leading SP)",
            base + b"X-A: v1\r\n v2continued\r\n" + tail,
        ),
        ("obs-fold continuation (leading TAB)", base + b"X-A: v1\r\n\tv2\r\n" + tail),
        ("whitespace before colon", base + b"X-A : v\r\n" + tail),
        ("header with no value", base + b"X-Empty:\r\n" + tail),
        ("header with no colon", base + b"MalformedHeaderLine\r\n" + tail),
        ("duplicate Host", base + b"Host: evil.com\r\n" + tail),
        (
            "duplicate Content-Type",
            base + b"Content-Type: a\r\nContent-Type: b\r\n" + tail,
        ),
        ("10000 headers", base + (b"X-H: v\r\n" * 10000) + tail),
        (
            "single 1MB header value",
            base + b"X-Big: " + b"A" * (1024 * 1024) + b"\r\n" + tail,
        ),
        ("NUL in header value", base + b"X-N: a\x00b\r\n" + tail),
        ("bare LF line ending", b"GET /alive HTTP/1.1\nHost: t\nConnection: close\n\n"),
        ("colon-only header", base + b":\r\n" + tail),
        ("leading empty header line", base + b"\r\nX-A: v\r\n" + tail),
    ]
    for label, payload in cases:
        sv.raw(payload)
        check(f"header: {label}", sv.alive())

    # WebSocket extension/subprotocol negotiation with adversarial params — the
    # handshake header parser must not choke.
    for label, extra in [
        ("permessage-deflate", "Sec-WebSocket-Extensions: permessage-deflate"),
        (
            "permessage-deflate w/ params",
            "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits=15; server_no_context_takeover",
        ),
        ("garbage extension", "Sec-WebSocket-Extensions: " + "x" * 10000),
        ("subprotocol list", "Sec-WebSocket-Protocol: chat, superchat, " + "p" * 5000),
    ]:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /ws HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n{extra}\r\n\r\n"
        ).encode()
        sv.raw(req)
        check(f"ws-handshake: {label}", sv.alive())


def fuzz_version_and_connection(sv: Server) -> None:
    print("\n── HTTP version / Connection semantics ──")
    cases = [
        ("HTTP/1.0 no keep-alive", b"GET /alive HTTP/1.0\r\nHost: t\r\n\r\n"),
        (
            "HTTP/1.0 explicit keep-alive",
            b"GET /alive HTTP/1.0\r\nHost: t\r\nConnection: keep-alive\r\n\r\n",
        ),
        (
            "HTTP/1.1 Connection: close",
            b"GET /alive HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "HTTP/2.0 over cleartext line",
            b"GET /alive HTTP/2.0\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "HTTP/9.9 bogus version",
            b"GET /alive HTTP/9.9\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "no HTTP version token",
            b"GET /alive\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "lowercase method",
            b"get /alive HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "absolute-form target",
            b"GET http://evil.com/alive HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        ("OPTIONS *", b"OPTIONS * HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"),
        (
            "TRACE method",
            b"TRACE /alive HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "CONNECT method",
            b"CONNECT evil.com:443 HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "oversized method token",
            b"A" * 100000 + b" /alive HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
        (
            "garbage before request line",
            b"\x00\x01\x02\r\nGET /alive HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n",
        ),
    ]
    for label, payload in cases:
        sv.raw(payload)
        check(f"version/conn: {label}", sv.alive())


def run(host: str, port: int) -> None:
    sv = Server(host, port)
    check("baseline: server answers 200", sv.alive())
    fuzz_websocket_frames(sv)
    fuzz_multipart(sv)
    fuzz_chunked(sv)
    fuzz_reactor_abuse(sv)
    fuzz_range(sv)
    fuzz_header_edges(sv)
    fuzz_version_and_connection(sv)


def main() -> bool:
    print("=" * 64)
    print("Native protocol safety validation (adversarial bytes, worker survives)")
    print("=" * 64)
    scripts_dir = str(Path(__file__).resolve().parent)
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = scripts_dir + (os.pathsep + existing if existing else "")
    with AppRunner(
        "test_native_protocol_fuzz:app",
        port=PORT,
        readiness_path="/alive",
        env={"PYTHONPATH": pythonpath},
    ) as r:
        run(r.host, r.port)
    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
