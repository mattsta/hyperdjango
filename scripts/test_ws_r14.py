"""R14 WebSocket hardening regressions (DoS + RFC 6455 conformance).

# hyper-test: e2e

Companion to test_websocket_rfc_hardening.py. Guards the round-14 fixes:

  #1 (DoS)   Auto-pong / close-echo are routed through the NON-BLOCKING
             outbound path, so a peer that ping-floods while holding its
             receive window full can no longer head-of-line-block the shared
             event loop for up to SO_SNDTIMEO. Regression guard: a flooding
             connection must not stall a second connection's echo.
  #2 (§4.2.2) The handshake selects EXACTLY ONE subprotocol. A client offering
             "a, b, c" must get back exactly "a" — never the whole list.
  #4 (§5.2)  Non-minimal length encoding is rejected: a length <126 sent in the
             16-bit form, or a length ≤65535 sent in the 64-bit form, fails the
             connection (Autobahn strict).

ALL asserts here drive the real native server and therefore REQUIRE the native
extension to be built (the central gate builds it). This file cannot pass until
then; it is a no-op-skip if AppRunner can't start the app.

Runs against the minimal native echo app (no DB required).
"""

import base64
import os
import socket
import struct
import sys
import time

from e2e_helper import AppRunner

HOST = "127.0.0.1"
# Local, un-registered port (18813 = protocol_fuzz, 18815 = unicode_trace are
# the neighbors); avoids touching e2e_helper.TEST_PORTS from this file.
PORT = 18814

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


def _handshake(s: socket.socket, extra_headers: str = "") -> bytes:
    """Perform the upgrade handshake; return the full response header block
    (b"" on failure). `extra_headers` is inserted verbatim (each line must end
    with \\r\\n)."""
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        (
            f"GET /ws/echo HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n{extra_headers}\r\n"
        ).encode()
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            return b""
        resp += chunk
    return resp


def _mask(payload: bytes) -> tuple[bytes, bytes]:
    k = os.urandom(4)
    return k, bytes(c ^ k[i % 4] for i, c in enumerate(payload))


def _frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Minimal-encoding masked client frame (matches a conformant client)."""
    b = bytearray([(0x80 if fin else 0x00) | opcode])
    ln = len(payload)
    if ln < 126:
        b.append(0x80 | ln)
    elif ln < 65536:
        b.append(0x80 | 126)
        b += struct.pack("!H", ln)
    else:
        b.append(0x80 | 127)
        b += struct.pack("!Q", ln)
    k, masked = _mask(payload)
    return bytes(b) + k + masked


def _frame_forced_len(opcode: int, payload: bytes, form: int) -> bytes:
    """Masked client frame that DELIBERATELY uses a non-minimal length form.
    `form` is 16 or 64 — the width to encode the (small) length in."""
    ln = len(payload)
    b = bytearray([0x80 | opcode])
    if form == 16:
        b.append(0x80 | 126)
        b += struct.pack("!H", ln)
    else:
        b.append(0x80 | 127)
        b += struct.pack("!Q", ln)
    k, masked = _mask(payload)
    return bytes(b) + k + masked


def _rejects(send_bytes: bytes) -> bool:
    """True if the server closes/resets the connection after send_bytes."""
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
        return data == b"" or (len(data) >= 1 and (data[0] & 0x0F) == 0x8)
    finally:
        s.close()


def _echoes(payload: bytes) -> bool:
    s = socket.create_connection((HOST, PORT), timeout=3)
    s.settimeout(3)
    try:
        if not _handshake(s):
            return False
        s.sendall(_frame(0x1, payload))
        data = s.recv(4096)
        return payload in data
    finally:
        s.close()


# ── #2: subprotocol selection ───────────────────────────────────────────────


def _selected_subprotocol(offer: str) -> str | None:
    """Return the single Sec-WebSocket-Protocol value the server echoed for the
    given client offer list, or None if it echoed none. Returns the sentinel
    "<MULTIPLE>" if the server (wrongly) echoed more than one token."""
    s = socket.create_connection((HOST, PORT), timeout=3)
    s.settimeout(3)
    try:
        resp = _handshake(s, extra_headers=f"Sec-WebSocket-Protocol: {offer}\r\n")
        if not resp or b"101" not in resp:
            return None
        for line in resp.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-protocol:"):
                val = line.split(b":", 1)[1].strip().decode()
                return "<MULTIPLE>" if "," in val else val
        return None
    finally:
        s.close()


# ── #1: ping-flood must not head-of-line-block a second connection ──────────


def _ping_flood_does_not_stall(n_floods: int = 3, n_pings: int = 120_000) -> bool:
    """Open several connections that ping-flood WITHOUT ever reading their
    pongs (so the server's send buffer to them fills), then verify a fresh
    probe connection still gets a prompt echo. With the blocking auto-pong
    (the bug) the loop thread parks in writev up to SO_SNDTIMEO and the probe
    stalls; with the non-blocking route it stays responsive.

    Probabilistic in "shared" mode (connections round-robin across loops), so
    we open multiple flooders to cover the loop the probe lands on.
    """
    floods: list[socket.socket] = []
    try:
        ping = _frame(0x9, b"")  # empty-payload masked ping
        blast = ping * 1024
        for _ in range(n_floods):
            f = socket.create_connection((HOST, PORT), timeout=3)
            f.settimeout(3)
            if not _handshake(f):
                return False
            # Fire a large volume of pings and DO NOT read the pongs back.
            sent = 0
            f.setblocking(False)
            try:
                while sent < n_pings:
                    try:
                        f.send(blast)
                        sent += 1024
                    except BlockingIOError, OSError:
                        break  # our own send buffer to the server filled; enough
            finally:
                f.setblocking(True)
            floods.append(f)

        # Probe: a brand-new connection must echo promptly despite the flood.
        probe = socket.create_connection((HOST, PORT), timeout=3)
        probe.settimeout(3)
        try:
            if not _handshake(probe):
                return False
            t0 = time.monotonic()
            probe.sendall(_frame(0x1, b"probe-r14"))
            data = probe.recv(4096)
            elapsed = time.monotonic() - t0
            # Generous bound: the fix keeps this sub-second; the blocking bug
            # would push it toward SO_SNDTIMEO (30s) and blow the 3s socket
            # timeout entirely.
            return (b"probe-r14" in data) and (elapsed < 2.0)
        finally:
            probe.close()
    finally:
        for f in floods:
            f.close()


def _upgrade_with_version(version: str) -> bytes:
    """Send a WS upgrade offering the given Sec-WebSocket-Version; return the
    raw response header block."""
    s = socket.create_connection((HOST, PORT), timeout=3)
    s.settimeout(3)
    try:
        key = base64.b64encode(b"0123456789abcdef").decode()
        s.sendall(
            f"GET /ws/echo HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: {version}\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf
    finally:
        s.close()


def main() -> int:
    runner = AppRunner(
        "benchmarks.websocket.apps.native_echo:app",
        host=HOST,
        port=PORT,
        readiness_path="/health",
    )
    runner.start()
    try:
        print("=" * 60)
        print("WebSocket R14 hardening (DoS + conformance)")
        print("=" * 60)

        # #2 — §4.2.2 select exactly one subprotocol.
        ok(
            "§4.2.2 offer 'a, b, c' selects exactly 'a'",
            _selected_subprotocol("a, b, c") == "a",
        )
        ok(
            "§4.2.2 single offer 'chat' selects 'chat'",
            _selected_subprotocol("chat") == "chat",
        )
        ok(
            "§4.2.2 never echoes multiple tokens",
            _selected_subprotocol("x,y") != "<MULTIPLE>",
        )

        # #4 — §5.2 minimal length encoding required.
        ok(
            "§5.2 len 5 in 16-bit form rejected",
            _rejects(_frame_forced_len(0x1, b"hello", 16)),
        )
        ok(
            "§5.2 len 5 in 64-bit form rejected",
            _rejects(_frame_forced_len(0x1, b"hello", 64)),
        )
        ok(
            "§5.2 len 200 in 64-bit form rejected",
            _rejects(_frame_forced_len(0x1, b"x" * 200, 64)),
        )
        # Sanity: a genuinely large payload (needs the 16-bit form) still works.
        ok("§5.2 minimal 16-bit frame (300B) still echoes", _echoes(b"z" * 300))
        ok("well-formed small frame still echoes", _echoes(b"r14-ok"))

        # #1 — ping-flood HOL DoS regression guard.
        ok(
            "#1 ping flood does not stall a second connection",
            _ping_flood_does_not_stall(),
        )

        # A1-#3 — §4.4 Sec-WebSocket-Version negotiation: a non-13 offer must be
        # rejected with 426 Upgrade Required + Sec-WebSocket-Version: 13.
        r426 = _upgrade_with_version("8")
        ok(
            "§4.4 non-13 version → 426",
            b" 426 " in r426 or r426.startswith(b"HTTP/1.1 426"),
        )
        ok(
            "§4.4 426 advertises version 13",
            b"sec-websocket-version: 13" in r426.lower(),
        )
        # Regression: version 13 still completes the 101 handshake.
        ok(
            "§4.4 version 13 still upgrades (101)",
            b"101" in _upgrade_with_version("13"),
        )
    finally:
        runner.stop()

    print(f"\nResults: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
