"""
Round-10 WebSocket send-path (HOL-blocking fix) + native keepalive regression.

Covers the fixes for:

  #1 (HOL BLOCKING) websocket.ZigWebSocket send path is non-blocking: a send
     issues a SINGLE native MSG_DONTWAIT attempt (_ws_try_send) and, on
     backpressure, parks on the loop's own selector writer (add_writer) instead
     of blocking the shared event-loop thread for up to SO_SNDTIMEO (30s). A
     slow/stalled consumer therefore CANNOT stall a second connection's send —
     proven here with real socketpair fds + a real asyncio loop, only the native
     send/flush faked to model backpressure deterministically.

  #2 (BACKPRESSURE SHED) an outbound backlog past the native high-water mark
     sheds the connection (WebSocketDisconnect 1013) rather than buffering
     without bound — and suppresses the blocking graceful close frame so the
     shed itself never re-introduces HOL blocking.

  #3 (KEEPALIVE) ping_interval/pong_timeout now take effect: the loop-driven
     keepalive pings via the non-blocking send path, re-arms while the peer is
     heard from, and reaps a peer gone silent past pong_timeout. A pong (any
     inbound frame) resets the deadline.

NATIVE-REBUILD NOTES (assertions that need the freshly-built .so):
  - The native-symbol smoke test (test_native_symbols) calls the REAL
    _ws_try_send / _ws_flush_send / _ws_send_ping / _ws_pong_age and requires
    the round-10 rebuild:  uv run python zig/build_hyperdjango.py --safe --install
  - The Python-layer tests fake the native send/flush, so they validate the
    asyncio state machine (add_writer/drain/shed/keepalive) WITHOUT the rebuild.
  - The native last_recv refresh on inbound frames and the real socket-level
    high-water buffering are exercised end-to-end by `uv run hyper-test
    websocket_native realtime` against a running server; here #3's reset is
    demonstrated with a mocked pong-age.
"""

# hyper-test: unit

import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperdjango.websocket import (  # noqa: E402
    _WS_CLOSED,
    _WS_SENT,
    _WS_SHED,
    _WS_WOULD_BLOCK,
    WebSocketDisconnect,
    ZigWebSocket,
)

PASS = 0
FAIL = 0


def ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {name}")


def fail(name: str, msg: str = "") -> None:
    global FAIL
    FAIL += 1
    detail = f" -- {msg}" if msg else ""
    print(f"  FAIL: {name}{detail}")


def check(cond: bool, name: str, msg: str = "") -> None:
    ok(name) if cond else fail(name, msg)


def run(coro):
    return asyncio.run(coro)


# ── Fake native send layer ──────────────────────────────────────────────────
#
# Emulates the SendResult protocol of _ws_try_send/_ws_flush_send/_ws_send_ping
# so the Python send-path state machine can be driven deterministically. Real
# socketpair fds still gate add_writer, so the selector behavior is genuine.


class FakeNative:
    def __init__(self):
        self.blocked: dict[int, bool] = {}
        self.shed: set[int] = set()
        self.pong_age_val: dict[int, float] = {}
        self.pings_sent: dict[int, int] = {}

    def try_send(self, conn_id, opcode, data):
        if conn_id in self.shed:
            return _WS_SHED
        return _WS_WOULD_BLOCK if self.blocked.get(conn_id) else _WS_SENT

    def flush_send(self, conn_id):
        return _WS_WOULD_BLOCK if self.blocked.get(conn_id) else _WS_SENT

    def send_ping(self, conn_id, payload):
        self.pings_sent[conn_id] = self.pings_sent.get(conn_id, 0) + 1
        if conn_id in self.shed:
            return _WS_SHED
        return _WS_WOULD_BLOCK if self.blocked.get(conn_id) else _WS_SENT

    def pong_age(self, conn_id):
        return self.pong_age_val.get(conn_id, 0.0)


def make_ws(fake: FakeNative, conn_id: int, fd: int | None) -> ZigWebSocket:
    """Build a ZigWebSocket wired to the fake native layer.

    __init__ imports the (real) native symbols and probes the fd for the given
    (unknown) conn_id — harmless; we then override every native binding with the
    fake and pin the real fd we want the selector to watch."""
    ws = ZigWebSocket(conn_id, {}, "/ws", "")
    ws._conn_id = conn_id
    ws._fd = fd
    ws._try_send = fake.try_send
    ws._flush_send = fake.flush_send
    ws._send_ping = fake.send_ping
    ws._pong_age_fn = fake.pong_age
    ws._close_fn = lambda *a: None
    ws._release_fn = lambda *a: None
    return ws


def _fill_send_buffer(sock: socket.socket) -> None:
    """Fill a socket's send buffer so its fd is NOT writable (a stalled peer)."""
    sock.setblocking(False)
    try:
        while True:
            sock.send(b"x" * 65536)
    except BlockingIOError, OSError:
        pass


def _drain(sock: socket.socket) -> None:
    """Drain a socket's receive buffer so its peer's fd becomes writable again."""
    sock.setblocking(False)
    try:
        while True:
            if not sock.recv(65536):
                break
    except BlockingIOError, OSError:
        pass


# ── #1: head-of-line isolation ──────────────────────────────────────────────


async def _test_hol_isolation():
    loop = asyncio.get_running_loop()
    fake = FakeNative()

    # Connection 1: a real socketpair whose send buffer we fill, so its fd is
    # not writable — a genuinely stalled/zero-window consumer.
    a1, b1 = socket.socketpair()
    a1.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    b1.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    _fill_send_buffer(a1)
    ws1 = make_ws(fake, 1, a1.fileno())
    fake.blocked[1] = True
    ws1._idle_timeout = None  # park indefinitely until we unblock it

    # Connection 2: healthy — its native try_send reports SENT immediately.
    a2, b2 = socket.socketpair()
    ws2 = make_ws(fake, 2, a2.fileno())
    fake.blocked[2] = False

    try:
        # Start the stalled send; it must park on add_writer, not block the loop.
        task1 = loop.create_task(ws1.send_bytes(b"stalled-payload"))
        await asyncio.sleep(0)  # let task1 reach the parked await

        t0 = loop.time()
        await asyncio.wait_for(ws2.send_text("prompt"), 1.0)
        elapsed = loop.time() - t0
        check(
            elapsed < 0.5,
            "second connection's send completes promptly while the first is stalled",
            f"took {elapsed:.3f}s",
        )
        check(
            not task1.done(),
            "stalled connection's send stays parked (head-of-line isolated)",
        )

        # Peer drains → fd writable → the writer callback flushes → send resumes.
        fake.blocked[1] = False
        _drain(b1)
        await asyncio.wait_for(task1, 1.0)
        check(
            task1.done() and task1.exception() is None,
            "stalled send resumes and completes once the peer drains",
        )
    except TimeoutError:
        fail("HOL isolation", "a send blocked/parked longer than its 1s bound")
    finally:
        ws1._remove_writer()
        ws2._remove_writer()
        for s in (a1, b1, a2, b2):
            s.close()


# ── #2: high-water shed ─────────────────────────────────────────────────────


async def _test_shed():
    fake = FakeNative()
    a, b = socket.socketpair()
    ws = make_ws(fake, 3, a.fileno())
    fake.shed.add(3)  # native reports the backlog exceeded the high-water mark
    try:
        raised = None
        try:
            await ws.send_text("over-the-mark")
        except WebSocketDisconnect as e:
            raised = e
        check(
            raised is not None and raised.code == 1013,
            "outbound high-water sheds the stuck connection with close 1013",
            f"got {raised!r}",
        )
        check(
            ws._close_frame_sent,
            "shed suppresses the blocking graceful close frame (no re-introduced HOL block)",
        )
    finally:
        a.close()
        b.close()


# ── #3: keepalive + pong/idle reset ─────────────────────────────────────────


async def _test_keepalive():
    fake = FakeNative()
    a, b = socket.socketpair()
    ws = make_ws(fake, 4, a.fileno())
    ws._accepted = True
    ws._ping_interval = 30.0
    ws._pong_timeout = 60.0
    try:
        # Peer recently heard from (pong_age small) → tick pings and re-arms.
        fake.pong_age_val[4] = 1.0
        ws._keepalive_tick()
        check(
            fake.pings_sent.get(4, 0) == 1,
            "keepalive tick sends a ping through the non-blocking send path",
        )
        check(
            ws._keepalive_handle is not None,
            "keepalive re-arms while the peer is alive (ping_interval takes effect)",
        )
        ws._keepalive_handle.cancel()
        ws._keepalive_handle = None

        # Simulate a pong arriving (native refreshes last_recv → pong_age ~0):
        # the deadline is reset, so a subsequent tick still re-arms rather than
        # reaping. (Native last_recv refresh verified by the websocket_native
        # suite against a live server; mocked here.)
        fake.pong_age_val[4] = 0.0
        ws._keepalive_tick()
        check(
            ws._keepalive_handle is not None and not ws._close_frame_sent,
            "a pong resets the idle deadline — an alive-but-idle peer stays open",
        )
        ws._keepalive_handle.cancel()
        ws._keepalive_handle = None

        # Peer gone silent past pong_timeout → reap: wake a parked receive with a
        # disconnect and stop pinging.
        loop = asyncio.get_running_loop()
        parked = loop.create_future()
        ws._readable_fut = parked
        fake.pong_age_val[4] = 999.0
        ws._keepalive_tick()
        check(
            ws._keepalive_handle is None,
            "keepalive stops re-arming once the peer is reaped",
        )
        check(
            ws._close_frame_sent,
            "reaped peer suppresses the blocking close frame",
        )
        check(
            parked.done() and isinstance(parked.exception(), WebSocketDisconnect),
            "pong_timeout wakes a parked receive with WebSocketDisconnect",
        )
        ws._readable_fut = None
    finally:
        a.close()
        b.close()


# ── Native-symbol smoke test (REQUIRES the round-10 rebuild) ─────────────────


def test_native_symbols():
    """Exercise the REAL native send primitives on an unknown connection id.
    Fails to import (and is reported) on a pre-round-10 .so."""
    try:
        from hyperdjango._hyperdjango_native import (
            _ws_flush_send,
            _ws_pong_age,
            _ws_send_ping,
            _ws_try_send,
        )
    except ImportError as e:
        fail(
            "native round-10 send symbols exported",
            f"{e} — rebuild: uv run python zig/build_hyperdjango.py --safe --install",
        )
        return

    # Unknown conn id → CLOSED(3) for sends, None for pong-age. Deterministic,
    # no server needed, but proves the freshly-built symbols are wired.
    check(
        _ws_try_send(0, 0x1, b"x") == _WS_CLOSED,
        "native _ws_try_send(unknown) -> CLOSED",
    )
    check(_ws_flush_send(0) == _WS_CLOSED, "native _ws_flush_send(unknown) -> CLOSED")
    check(
        _ws_send_ping(0, b"") == _WS_CLOSED, "native _ws_send_ping(unknown) -> CLOSED"
    )
    check(_ws_pong_age(0) is None, "native _ws_pong_age(unknown) -> None")


def main():
    print("Round-10 WebSocket send-path + keepalive regression:")
    test_native_symbols()
    run(_test_hol_isolation())
    run(_test_shed())
    run(_test_keepalive())
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
