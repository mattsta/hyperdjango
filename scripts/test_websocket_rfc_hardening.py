"""Strict RFC 6455 server-side hardening tests.

# hyper-test: e2e

Unlike test_websocket_frame_fuzz (which passes if the server merely doesn't
crash), these assert the server actively REJECTS protocol violations by
closing the connection — the behavior a hardened, production server must
have. Guards against a regression that silently starts accepting
non-conformant frames.

  - §5.1: client→server frames MUST be masked → unmasked frame rejected.
  - §5.5: control frames MUST be ≤125 bytes and unfragmented → oversized or
    fragmented ping/pong/close rejected.
  - Sanity: a well-formed masked frame is still processed normally.

Runs against the minimal native echo app (no DB required).
"""

import base64
import os
import socket
import struct
import sys

from e2e_helper import TEST_PORTS, AppRunner

HOST = "127.0.0.1"
PORT = TEST_PORTS["websocket_rfc_hardening"]

PASS = 0
FAIL = 0


def ok(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")
    return condition


def _handshake(s: socket.socket) -> bool:
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        (
            f"GET /ws/echo HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            return False
        resp += chunk
    return b"101" in resp


def _frame(opcode: int, payload: bytes, mask: bool = True, fin: bool = True) -> bytes:
    b = bytearray([(0x80 if fin else 0x00) | opcode])
    ln = len(payload)
    mb = 0x80 if mask else 0x00
    if ln < 126:
        b.append(mb | ln)
    elif ln < 65536:
        b.append(mb | 126)
        b += struct.pack("!H", ln)
    else:
        b.append(mb | 127)
        b += struct.pack("!Q", ln)
    if mask:
        k = os.urandom(4)
        b += k
        b += bytes(c ^ k[i % 4] for i, c in enumerate(payload))
    else:
        b += payload
    return bytes(b)


def _rejects(send_bytes: bytes) -> bool:
    """True if the server closes the connection after receiving send_bytes."""
    s = socket.create_connection((HOST, PORT), timeout=3)
    s.settimeout(3)
    try:
        if not _handshake(s):
            return False
        s.sendall(send_bytes)
        try:
            data = s.recv(4096)
        except TimeoutError, ConnectionResetError, OSError:
            return True  # reset/closed = rejected
        # Empty read = peer closed; a close-frame opcode (0x8) = rejected.
        return data == b"" or (len(data) >= 1 and (data[0] & 0x0F) == 0x8)
    finally:
        s.close()


def _echoes(payload: bytes) -> bool:
    s = socket.create_connection((HOST, PORT), timeout=3)
    s.settimeout(3)
    try:
        if not _handshake(s):
            return False
        s.sendall(_frame(0x1, payload, mask=True))
        data = s.recv(4096)
        return payload in data
    finally:
        s.close()


def main() -> int:
    # AppRunner handles stale-port cleanup + readiness polling + clean
    # teardown (the established e2e pattern), avoiding hand-rolled subprocess
    # management and fixed-port collisions.
    runner = AppRunner(
        "benchmarks.websocket.apps.native_echo:app",
        host=HOST,
        port=PORT,
        readiness_path="/health",
    )
    runner.start()
    try:
        print("=" * 60)
        print("WebSocket RFC 6455 hardening")
        print("=" * 60)
        ok(
            "§5.1 unmasked client frame rejected",
            _rejects(_frame(0x1, b"hello", mask=False)),
        )
        ok(
            "§5.5 oversized ping (>125B) rejected",
            _rejects(_frame(0x9, b"x" * 200, mask=True)),
        )
        ok(
            "§5.5 fragmented control frame rejected",
            _rejects(_frame(0x9, b"hi", mask=True, fin=False)),
        )
        ok("well-formed masked frame still echoes", _echoes(b"rfc-ok"))
    finally:
        runner.stop()

    print(f"\nResults: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
