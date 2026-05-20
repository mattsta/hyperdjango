"""Connection-lifecycle regression tests for the native server.

Covers the ws14-syscall-fixes work:
  - Connection: close is honored (RFC 7230 §6.1): the response carries
    `Connection: close` and the socket gets a prompt FIN instead of hanging
    until the idle timeout — in BOTH the reactor and threaded models.
  - Idle (zero-byte / slowloris) connections parked on the reactor are reaped
    by the idle sweep; an active keep-alive connection is NOT reaped.
  - The WebSocket receive path (the _readable-gated _recv_one) still echoes
    correctly, including a batch of frames sent in one write (buffer drain).

Each server runs as a subprocess (like test_native_server.py) so the real
accept loop / reactor / worker pool is exercised end to end.
"""

import base64
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

TEST_HOST = "127.0.0.1"
# Ports in the 19700-19730 band reserved for this agent's work.
PORTS = {"reactor": 19711, "threaded": 19712}
IDLE_TIMEOUT_MS = 2000

APP_CODE = """
import sys
sys.path.insert(0, ".")
from hyperdjango import HyperApp, Response

app = HyperApp(title="Lifecycle Test")

@app.get("/ping")
def ping(request):
    return Response.text("pong")

@app.websocket("/ws")
async def ws_echo(ws):
    await ws.accept()
    async for msg in ws.iter_text():
        await ws.send_text(msg)

if __name__ == "__main__":
    app.run(host="{host}", port={port})
"""


def _start_server(mode: str, port: int):
    env = dict(os.environ)
    env["HYPER_HTTP_SERVER_MODEL"] = mode
    env["HYPER_IDLE_TIMEOUT_MS"] = str(IDLE_TIMEOUT_MS)
    proc = subprocess.Popen(
        [sys.executable, "-c", APP_CODE.format(host=TEST_HOST, port=port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        env=env,
    )
    url = f"http://{TEST_HOST}:{port}/ping"
    for _ in range(50):
        time.sleep(0.1)
        try:
            urllib.request.urlopen(url, timeout=1)
            return proc
        except urllib.error.URLError, ConnectionRefusedError, OSError:
            if proc.poll() is not None:
                err = proc.stderr.read().decode() if proc.stderr else ""
                pytest.skip(f"{mode} server failed to start: {err[:400]}")
    proc.kill()
    pytest.skip(f"{mode} server didn't start in time")


@pytest.fixture(scope="module", params=["reactor", "threaded"])
def server(request):
    mode = request.param
    proc = _start_server(mode, PORTS[mode])
    yield mode, PORTS[mode]
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _read_http_response(sock: socket.socket) -> bytes:
    """Read one HTTP response (headers + Content-Length body)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return buf
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            clen = int(line.split(b":", 1)[1].strip())
    while len(body) < clen:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return buf


class TestConnectionClose:
    def test_close_request_gets_close_header_and_prompt_fin(self, server):
        mode, port = server
        sock = socket.create_connection((TEST_HOST, port), timeout=5)
        sock.settimeout(5)
        sock.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        resp = _read_http_response(sock)
        assert b"pong" in resp, resp
        assert b"connection: close" in resp.lower(), (
            f"[{mode}] expected Connection: close, got: {resp[:200]!r}"
        )
        # The server must FIN promptly (right after the response), NOT hang until
        # the idle timeout. Assert the FIN arrives well under IDLE_TIMEOUT_MS so
        # this proves the close path, not the idle sweep, did it.
        start = time.monotonic()
        tail = sock.recv(4096)  # returns b"" on FIN
        elapsed = time.monotonic() - start
        assert tail == b"", f"[{mode}] expected FIN, got {tail!r}"
        assert elapsed < (IDLE_TIMEOUT_MS / 1000) * 0.6, (
            f"[{mode}] FIN took {elapsed:.2f}s — looks like the idle timeout, "
            f"not prompt Connection: close handling"
        )
        sock.close()

    def test_keepalive_request_stays_open(self, server):
        mode, port = server
        sock = socket.create_connection((TEST_HOST, port), timeout=5)
        sock.settimeout(5)
        # Two requests on one keep-alive connection: the second only succeeds if
        # the first did NOT close the socket.
        for expect in (b"pong", b"pong"):
            sock.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\n")
            resp = _read_http_response(sock)
            assert expect in resp, f"[{mode}] {resp[:200]!r}"
            assert b"connection: keep-alive" in resp.lower(), resp[:200]
        sock.close()


class TestIdleReap:
    def test_silent_connection_is_reaped(self, server):
        mode, port = server
        # Connect and send NOTHING. A parked/blocked zero-byte connection must be
        # closed after the idle timeout in both modes (reactor sweep / threaded
        # SO_RCVTIMEO). Allow the timeout + the 1s sweep granularity + slack.
        sock = socket.create_connection((TEST_HOST, port), timeout=10)
        sock.settimeout(10)
        start = time.monotonic()
        tail = sock.recv(4096)  # blocks until the server FINs us
        elapsed = time.monotonic() - start
        assert tail == b"", f"[{mode}] expected idle FIN, got {tail!r}"
        assert elapsed >= (IDLE_TIMEOUT_MS / 1000) * 0.5, (
            f"[{mode}] closed too early ({elapsed:.2f}s) — not the idle path"
        )
        assert elapsed < 8.0, f"[{mode}] idle reap took too long: {elapsed:.2f}s"
        sock.close()

    def test_active_keepalive_not_reaped(self, server):
        mode, port = server
        # Keep a connection ACTIVE by issuing a request every ~1s across a span
        # LONGER than the idle timeout. Each request resets the activity clock, so
        # the connection must survive the whole span (never idle-reaped).
        sock = socket.create_connection((TEST_HOST, port), timeout=5)
        sock.settimeout(5)
        span_deadline = time.monotonic() + (IDLE_TIMEOUT_MS / 1000) * 2
        n = 0
        while time.monotonic() < span_deadline:
            sock.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\n")
            resp = _read_http_response(sock)
            assert b"pong" in resp, f"[{mode}] reaped mid-activity: {resp[:120]!r}"
            n += 1
            time.sleep(1.0)
        assert n >= 3, f"[{mode}] expected >=3 keepalive round-trips, got {n}"
        sock.close()


# ── Minimal RFC 6455 client for the WS receive-path checks ──────────────────


def _ws_handshake(sock, path="/ws"):
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        data = sock.recv(1024)
        if not data:
            raise ConnectionError("closed during handshake")
        resp += data
    assert b" 101 " in resp, resp[:120]


def _ws_text_frame(message: str) -> bytes:
    payload = message.encode()
    mask = os.urandom(4)
    frame = bytearray([0x81])
    if len(payload) < 126:
        frame.append(0x80 | len(payload))
    else:
        frame.append(0x80 | 126)
        frame.extend(struct.pack(">H", len(payload)))
    frame.extend(mask)
    frame.extend(bytes(payload[i] ^ mask[i % 4] for i in range(len(payload))))
    return bytes(frame)


def _ws_read_text(sock) -> str:
    b0 = sock.recv(1)
    b1 = sock.recv(1)
    length = b1[0] & 0x7F
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    payload = b""
    while len(payload) < length:
        payload += sock.recv(length - len(payload))
    return payload.decode()


class TestWebSocketReceivePath:
    def test_echo_roundtrip(self, server):
        mode, port = server
        sock = socket.create_connection((TEST_HOST, port), timeout=5)
        sock.settimeout(5)
        _ws_handshake(sock)
        for i in range(20):
            msg = f"hello-{i}"
            sock.sendall(_ws_text_frame(msg))
            assert _ws_read_text(sock) == msg
        sock.close()

    def test_batched_frames_drain(self, server):
        mode, port = server
        # Three frames written in ONE send() land in the native recv buffer as a
        # batch. The _readable-gated receive loop must drain all three without
        # waiting on a fresh kernel readiness event.
        sock = socket.create_connection((TEST_HOST, port), timeout=5)
        sock.settimeout(5)
        _ws_handshake(sock)
        batch = _ws_text_frame("a") + _ws_text_frame("b") + _ws_text_frame("c")
        sock.sendall(batch)
        got = [_ws_read_text(sock) for _ in range(3)]
        assert got == ["a", "b", "c"], f"[{mode}] batched drain: {got}"
        sock.close()
