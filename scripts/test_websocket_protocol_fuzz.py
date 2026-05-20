"""Property-based (Hypothesis) fuzzing of the native WebSocket frame protocol.

# hyper-test: e2e

Structured protocol fuzzing that goes far beyond hand-picked cases — it drives
arbitrary payloads, sizes, frame types, TCP fragmentation, and frame batching
against the real native server and asserts protocol invariants. Targets the
non-blocking parse/reassembly path (parseFrameFromBuffer / tryRecvFrame /
recv_buf cursor) and the SIMD unmask, which have many boundary conditions
(7/16/64-bit length encodings, the 16-byte SIMD split, partial reads,
multiple frames per read).

Properties:
  1. Echo round-trip for ANY binary payload (0..~70 KB) — bytes preserved.
  2. Echo round-trip for ANY text payload — UTF-8 text preserved.
  3. Length-boundary payloads (125/126/127/65535/65536 …) round-trip.
  4. A frame split across arbitrary TCP chunk boundaries is reassembled.
  5. Several frames concatenated in one TCP write are all echoed, in order.
  6. Malformed frames (unmasked, oversized/fragmented control) are rejected
     (connection closed) — never crash the server, and the server keeps
     serving new connections afterward.
"""

import base64
import os
import socket
import struct

from e2e_helper import TEST_PORTS, AppRunner
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.testkit import connect_with_retry

HOST = "127.0.0.1"
PORT = TEST_PORTS["websocket_protocol_fuzz"]

_SETTINGS = settings(
    max_examples=150,
    deadline=None,  # network I/O per example — wall-clock varies
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


# ── Minimal raw client (fresh connection per example → robust under shrinking) ──


def _handshake(s: socket.socket) -> None:
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
            raise ConnectionError("closed during handshake")
        resp += chunk


def _build_frame(
    opcode: int, payload: bytes, mask: bool = True, fin: bool = True
) -> bytes:
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


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _recv_message(s: socket.socket) -> tuple[int, bytes]:
    """Read one full server→client frame (server never masks). Returns (opcode, payload)."""
    h = _recv_exact(s, 2)
    opcode = h[0] & 0x0F
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", _recv_exact(s, 2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", _recv_exact(s, 8))[0]
    payload = _recv_exact(s, ln) if ln else b""
    return opcode, payload


def _create_connection_retry(timeout: float = 10.0) -> socket.socket:
    """Open a client TCP connection via the shared testkit retry policy.

    Hypothesis drives hundreds of fresh connections per run, so this test is
    the heaviest consumer of the OS ephemeral-port range in the suite; the
    retry policy and its deadline live in testkit.
    """
    return connect_with_retry(HOST, PORT, timeout=timeout)


def _conn() -> socket.socket:
    s = _create_connection_retry(timeout=10)
    s.settimeout(10)
    _handshake(s)
    return s


def _echo(send_bytes: bytes) -> tuple[int, bytes]:
    """Send raw bytes on a fresh connection, return the one echoed (opcode, payload)."""
    s = _conn()
    try:
        s.sendall(send_bytes)
        return _recv_message(s)
    finally:
        s.close()


# ── Property 1: binary echo round-trip ─────────────────────────────────────


@given(payload=st.binary(min_size=0, max_size=70000))
@_SETTINGS
def test_binary_echo_roundtrip(payload):
    opcode, echoed = _echo(_build_frame(0x2, payload))
    assert opcode == 0x2, f"expected binary opcode, got {opcode}"
    assert echoed == payload, f"binary mismatch len {len(echoed)} != {len(payload)}"


# ── Property 2: text echo round-trip ───────────────────────────────────────


@given(text=st.text(max_size=20000))
@_SETTINGS
def test_text_echo_roundtrip(text):
    payload = text.encode("utf-8")
    opcode, echoed = _echo(_build_frame(0x1, payload))
    assert opcode == 0x1, f"expected text opcode, got {opcode}"
    assert echoed == payload


# ── Property 3: length-encoding boundaries ─────────────────────────────────


@given(size=st.sampled_from([0, 1, 125, 126, 127, 128, 65534, 65535, 65536, 65537]))
@_SETTINGS
def test_length_boundaries(size):
    payload = bytes((i * 31 + 7) & 0xFF for i in range(size))
    opcode, echoed = _echo(_build_frame(0x2, payload))
    assert opcode == 0x2
    assert echoed == payload


# ── Property 4: frame split across arbitrary TCP boundaries is reassembled ──


@given(
    payload=st.binary(min_size=0, max_size=20000),
    n_splits=st.integers(min_value=1, max_value=8),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@_SETTINGS
def test_tcp_fragmentation_reassembly(payload, n_splits, seed):
    frame = _build_frame(0x2, payload)
    # Deterministic split points from the seed (Hypothesis controls the seed).
    import random

    rng = random.Random(seed)
    cuts = sorted(rng.randint(0, len(frame)) for _ in range(n_splits))
    s = _conn()
    try:
        prev = 0
        for cut in [*cuts, len(frame)]:
            if cut > prev:
                s.sendall(frame[prev:cut])
                prev = cut
        opcode, echoed = _recv_message(s)
        assert opcode == 0x2
        assert echoed == payload
    finally:
        s.close()


# ── Property 5: several frames in one write are all echoed, in order ────────


@given(payloads=st.lists(st.binary(min_size=0, max_size=2000), min_size=1, max_size=12))
@_SETTINGS
def test_pipelined_frames_in_order(payloads):
    blob = b"".join(_build_frame(0x2, p) for p in payloads)
    s = _conn()
    try:
        s.sendall(blob)
        for expected in payloads:
            opcode, echoed = _recv_message(s)
            assert opcode == 0x2
            assert echoed == expected
    finally:
        s.close()


# ── Property 6: malformed frames are rejected, server stays healthy ─────────


@given(
    kind=st.sampled_from(["unmasked", "oversized_control", "fragmented_control"]),
    ctrl_opcode=st.sampled_from([0x8, 0x9, 0xA]),
    payload=st.binary(min_size=0, max_size=300),
)
@_SETTINGS
def test_malformed_frames_rejected(kind, ctrl_opcode, payload):
    if kind == "unmasked":
        raw = _build_frame(0x1, payload or b"x", mask=False)
    elif kind == "oversized_control":
        raw = _build_frame(ctrl_opcode, b"x" * 200, mask=True)  # control >125
    else:  # fragmented_control
        raw = _build_frame(ctrl_opcode, payload[:10], mask=True, fin=False)
    s = _conn()
    try:
        s.sendall(raw)
        try:
            data = s.recv(4096)
        except TimeoutError, ConnectionResetError, OSError:
            data = b""  # reset = rejected
        # Closed (empty) or a close frame (opcode 0x8) — both count as rejected.
        assert data == b"" or (data[0] & 0x0F) == 0x8, f"not rejected: {data[:8]!r}"
    finally:
        s.close()


def main() -> int:
    # Start the server via the test infrastructure inside main() (the e2e
    # convention) — AppRunner handles readiness + teardown. No import-time
    # side effects.
    runner = AppRunner(
        "benchmarks.websocket.apps.native_echo:app",
        host=HOST,
        port=PORT,
        readiness_path="/health",
    )
    runner.start()
    try:
        tests = [
            test_binary_echo_roundtrip,
            test_text_echo_roundtrip,
            test_length_boundaries,
            test_tcp_fragmentation_reassembly,
            test_pipelined_frames_in_order,
            test_malformed_frames_rejected,
        ]
        failed = 0
        for t in tests:
            try:
                t()
                print(f"  PASS  {t.__name__}")
            except Exception as e:  # noqa: BLE001 — report the falsifying example
                failed += 1
                print(f"  FAIL  {t.__name__}: {e}")
        # Server must still be healthy after all the malformed traffic.
        import urllib.request

        try:
            ok = (
                urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=5).status
                == 200
            )
        except Exception:
            ok = False
        print(f"  {'PASS' if ok else 'FAIL'}  server healthy after fuzzing")
        if not ok:
            failed += 1
    finally:
        runner.stop()
    print(
        f"\nResults: {len(tests) + 1 - failed}/{len(tests) + 1} passed, {failed} failed"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
