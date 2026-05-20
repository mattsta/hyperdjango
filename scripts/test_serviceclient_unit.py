"""
Unit tests for hyperdjango.serviceclient: the retrying JSON transport and the
ordered, self-healing change-feed watcher.

# hyper-test: unit
# hyper-test-timeout: 300
#
# This suite deliberately drives real reconnect/backoff/epoch-restart cycles
# with generous ceilings (see _RECONNECT_TIMEOUT) — a longer wait only ever
# waits for something that SHOULD happen, so it never false-passes. Its ~18s
# standalone runtime balloons under the parallel suite's CPU oversubscription
# (observed 50s on arm, 77s on macOS, >180s on a peak-contended x86 runner).
# The 180s pure-tier default is the mistuned knob, not the test; 300s clears
# peak contention without shrinking any check.

Proves:
  - retries apply only to idempotent requests (GET retried; POST not; POST
    with idempotent=True retried)
  - HTTP status responses are definitive — never retried
  - status→error mapping: 401/403→AuthError, 404→RequestError,
    500→ServerError, with the server's `detail` carried through
  - backoff is exponential and bounded by max_backoff
  - watcher ordering property (ledger mode): out-of-order / duplicated wake
    hints over the live channel never disturb delivery — events arrive in exact
    ledger order, once each, because the replay endpoint is the single source
    of truth
  - reset flag → on_reset fires and the cursor jumps past the trimmed gap
  - a dropped wake still recovers via the periodic poll tick
  - a flapping hub keeps backing off (backoff does not reset to base)
  - ephemeral mode: hello → on_reset; events delivered in-frame via on_event;
    reconnect → on_reset again; no replay endpoint is ever pulled
  - catchup mode: first connect resyncs; a reconnect sends (client_id,
    last_seq) and the hub replays exactly the missed event frames, in order,
    once; an overrun below the ring floor resyncs with no partial delivery

In-process fake HTTP/WebSocket servers on ephemeral ports (no hardcoded ports,
condition-waits rather than sleeps) in the style of scripts/test_mtls_unit.py.
"""

import base64
import contextlib
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.serviceclient import (  # noqa: E402
    _WS_MAX_MISSED_PINGS,
    _WS_MAX_STALL_CREDIT_INTERVALS,
    AuthError,
    ChangeFeedWatcher,
    RequestError,
    ResponseError,
    RetryPolicy,
    ServerError,
    ServiceClient,
    ServiceError,
    ServiceUnavailable,
    _WebSocketConnection,
    build_ssl_context,
)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


# Positive assertions that a reconnect / flap eventually happens use this
# generous ceiling, not the default. The reconnect logic is sub-100ms when a
# core is free, but the full parallel suite on a few-core CI runner (macOS ~3
# cores) can starve the watcher thread for seconds — a tight 5s window then
# reports connects=1 for a reconnect that simply hadn't been scheduled yet. A
# longer ceiling only ever waits longer for a thing that should happen; it
# cannot cause a false pass. Negative "must NOT happen within N" checks keep
# their own short explicit timeouts.
_RECONNECT_TIMEOUT = 30.0

# Reproduces, on demand, the scheduling gap a loaded CI runner opens between a
# hub sending `hello` and registering that socket for broadcast. A test that
# pushes after a reconnect must wait for the REGISTRATION (HubServer.
# live_sockets), not for the earlier connect/hello signals; setting this makes
# the difference deterministic instead of load-dependent:
#
#     HYPER_TEST_WS_REGISTER_DELAY=0.4 uv run hyper-test serviceclient_unit
#
# env-boundary: test-harness fault injection, not framework configuration.
_WS_REGISTER_DELAY_S = float(os.environ.get("HYPER_TEST_WS_REGISTER_DELAY", "0") or 0)


def wait_for(pred, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``pred`` until true or the deadline; condition-wait, not sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def open_ws_retrying(client, path, tries: int = 40, **kw):
    """Open a wake WebSocket, retrying ONLY a transient connect failure.

    Under this socket-heavy suite the OS ephemeral-port pool briefly drains
    (EADDRNOTAVAIL / connect refused), which the client now surfaces as a typed
    ServiceUnavailable whose message mentions "connect". That is environmental
    noise, not the condition under test, so retry it; any other
    ServiceUnavailable (a handshake refusal, an oversized-frame/handshake cap)
    is the real signal and is re-raised at once.
    """
    from hyperdjango.serviceclient import ServiceUnavailable as _SU

    last = None
    for _ in range(tries):
        try:
            return client.open_websocket(path, **kw)
        except _SU as exc:
            if "connect" in str(exc).lower():
                last = exc
                time.sleep(0.05)
                continue
            raise
    raise last or _SU("connect never succeeded")


# ── Fake servers ─────────────────────────────────────────────────────────────


def _ws_send(conn: socket.socket, obj: dict) -> None:
    payload = json.dumps(obj).encode()
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x81, 126, n)
    else:
        head = struct.pack("!BBQ", 0x81, 127, n)
    conn.sendall(head + payload)  # server frames are unmasked


def _recv_all(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_read_frame(conn: socket.socket) -> tuple[int, bytes] | None:
    """Read one (client→server, masked) WebSocket frame, or None on EOF."""
    hdr = _recv_all(conn, 2)
    if hdr is None:
        return None
    opcode = hdr[0] & 0x0F
    length = hdr[1] & 0x7F
    if length == 126:
        ext = _recv_all(conn, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _recv_all(conn, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]
    mask = b""
    if hdr[1] & 0x80:
        mask = _recv_all(conn, 4)
        if mask is None:
            return None
    payload = b""
    if length:
        payload = _recv_all(conn, length)
        if payload is None:
            return None
    if mask:
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    return opcode, payload


class _BaseServer:
    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.port = self._sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):  # overridden
        conn.close()

    def close(self):
        self._stop = True
        # The accept loop is parked in accept() on this listener, from another
        # thread. Same rule as every other socket here: shutdown is what
        # unblocks a parked syscall — a bare close leaves that thread parked
        # (and its socket alive) for the rest of the run, and this file builds
        # around fifty of these servers.
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._sock.close()

    @staticmethod
    def _read_head(conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
        method, path, _ = head[0].decode().split(" ", 2)
        headers = {}
        for line in head[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode().strip().lower()] = v.decode().strip()
        return method, path, headers


class ProbeServer(_BaseServer):
    """Records requests and responds per configured behavior.

    behavior="drop": accept, read the head, then close without a response —
    a transport failure that urllib surfaces as a retryable URLError.
    behavior="status": respond with ``status`` and a JSON ``detail`` body.
    """

    def __init__(self, behavior="drop", status=200, detail=""):
        self.behavior = behavior
        self.status = status
        self.detail = detail
        self._lock = threading.Lock()
        self.counts = {}
        super().__init__()

    def count(self, method: str) -> int:
        with self._lock:
            return self.counts.get(method, 0)

    def _handle(self, conn):
        parsed = self._read_head(conn)
        if parsed is None:
            conn.close()
            return
        method, _path, _headers = parsed
        with self._lock:
            self.counts[method] = self.counts.get(method, 0) + 1
        if self.behavior == "drop":
            conn.close()
            return
        body = json.dumps({"detail": self.detail, "ok": self.status < 400}).encode()
        conn.sendall(
            b"HTTP/1.1 %d X\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (self.status, len(body), body)
        )
        conn.close()


class FeedServer(_BaseServer):
    """Serves an ordered replay endpoint and a WebSocket wake channel."""

    def __init__(
        self, ledger_ids=(), retention_floor=0, ws_mode="hold", pong_pings=True
    ):
        self.ws_mode = ws_mode
        self.pong_pings = pong_pings
        self._lock = threading.Lock()
        self.ledger = [{"id": i, "subject": f"s/{i}"} for i in ledger_ids]
        self.retention_floor = retention_floor
        self._ws_lock = threading.Lock()
        self._ws_socks: list[socket.socket] = []
        self.ws_connects = 0
        self.last_query: dict = {}
        super().__init__()

    def append(self, event_id: int) -> None:
        with self._lock:
            self.ledger.append({"id": event_id, "subject": f"s/{event_id}"})

    def set_floor(self, floor: int) -> None:
        with self._lock:
            self.retention_floor = floor

    def push_wake(self, obj: dict) -> None:
        with self._ws_lock:
            socks = list(self._ws_socks)
        for s in socks:
            with contextlib.suppress(OSError):
                _ws_send(s, obj)

    def push_wake_text(self, data: bytes) -> None:
        """Push a raw text frame whose payload is not JSON — the client's wake
        loop must treat it as a wake hint, not tear down and reconnect."""
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x81, n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x81, 126, n)
        else:
            head = struct.pack("!BBQ", 0x81, 127, n)
        with self._ws_lock:
            socks = list(self._ws_socks)
        for s in socks:
            with contextlib.suppress(OSError):
                s.sendall(head + data)  # server frames are unmasked

    def push_zero_fragments(self, count: int) -> None:
        """Stream ``count`` zero-length continuation frames with FIN never set —
        each is two bytes (0x00 0x00) that grow neither the byte total nor block
        on a read, so only a fragment-count cap can stop the reassembly loop."""
        frame = struct.pack("!BB", 0x00, 0x00)  # fin=0, opcode=0x0 (continuation)
        with self._ws_lock:
            socks = list(self._ws_socks)
        for s in socks:
            with contextlib.suppress(OSError):
                s.sendall(frame * count)

    def _replay(self, after: int, limit: int) -> dict:
        with self._lock:
            floor = self.retention_floor
            reset = after < floor
            start = max(after, floor)
            page = [dict(e) for e in self.ledger if e["id"] > start][:limit]
            if page:
                cursor = page[-1]["id"]
            else:
                tail = self.ledger[-1]["id"] if self.ledger else start
                cursor = max(start, tail)
            return {"events": page, "cursor": cursor, "reset": reset}

    def _handle(self, conn):
        parsed = self._read_head(conn)
        if parsed is None:
            conn.close()
            return
        method, path, headers = parsed
        if "websocket" in headers.get("upgrade", "").lower():
            self._handle_ws(conn, headers)
            return
        query = {}
        if "?" in path:
            for pair in path.split("?", 1)[1].split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    query[k] = v
        with self._lock:
            self.last_query = dict(query)
        after = int(query.get("after", "0"))
        limit = int(query.get("limit", "500"))
        body = json.dumps(self._replay(after, limit)).encode()
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(body), body)
        )
        conn.close()

    def _handle_ws(self, conn, headers):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept.encode()
        )
        with self._ws_lock:
            self.ws_connects += 1
        if self.ws_mode == "flap":
            with contextlib.suppress(OSError):
                conn.close()
            return
        if _WS_REGISTER_DELAY_S and self.ws_connects > 1:
            # Reconnects only: that is the window the guards protect, and
            # delaying every connection instead would slow the file to a crawl
            # without testing anything the first connect does not already cover.
            time.sleep(_WS_REGISTER_DELAY_S)  # see _WS_REGISTER_DELAY_S
        with self._ws_lock:
            self._ws_socks.append(conn)
        try:
            # Hold the connection open. Read client frames so a close is
            # detected, and answer keepalive pings with pongs like a real hub
            # (unless pong_pings is False — a black-holed peer that receives
            # but never sends, used to exercise the client's pong deadline).
            while not self._stop:
                frame = _ws_read_frame(conn)
                if frame is None:
                    break
                opcode, _payload = frame
                if opcode == 0x8:  # client close
                    break
                if opcode == 0x9 and self.pong_pings:  # ping → pong
                    with contextlib.suppress(OSError):
                        conn.sendall(struct.pack("!BB", 0x8A, 0))
        except OSError:
            pass
        finally:
            with self._ws_lock, contextlib.suppress(ValueError):
                self._ws_socks.remove(conn)
            with contextlib.suppress(OSError):
                conn.close()


# ── ServiceClient tests ──────────────────────────────────────────────────────


def test_retry_only_idempotent():
    print("\n== retry: idempotent only ==")
    server = ProbeServer(behavior="drop")
    client = ServiceClient(
        server.base_url, retry=RetryPolicy(max_attempts=3, base_backoff=0.001)
    )
    try:
        # GET is idempotent by default → retried up to the policy.
        with contextlib.suppress(ServiceUnavailable):
            client.request("GET", "/x")
        check(
            "GET retried to policy limit",
            wait_for(lambda: server.count("GET") == 3),
            f"got {server.count('GET')}",
        )

        # POST is not idempotent by default → a single attempt.
        with contextlib.suppress(ServiceUnavailable):
            client.request("POST", "/x", json_body={"a": 1})
        check(
            "POST not retried by default",
            wait_for(lambda: server.count("POST") == 1) and server.count("POST") == 1,
            f"got {server.count('POST')}",
        )

        # POST with an explicit idempotency opt-in → retried.
        with contextlib.suppress(ServiceUnavailable):
            client.request("POST", "/y", json_body={"a": 1}, idempotent=True)
        check(
            "POST idempotent=True retried",
            wait_for(lambda: server.count("POST") == 4),
            f"got {server.count('POST')}",
        )
    finally:
        server.close()


def test_status_errors_not_retried():
    print("\n== status errors: definitive, no retry ==")
    server = ProbeServer(behavior="status", status=500, detail="boom")
    client = ServiceClient(
        server.base_url, retry=RetryPolicy(max_attempts=3, base_backoff=0.001)
    )
    try:
        raised = None
        try:
            client.request("GET", "/x")
        except ServerError as exc:
            raised = exc
        check("5xx raises ServerError", isinstance(raised, ServerError))
        check(
            "5xx not retried (single request)",
            server.count("GET") == 1,
            f"got {server.count('GET')}",
        )
        check("detail carried through", raised is not None and raised.detail == "boom")
    finally:
        server.close()


def test_error_mapping():
    print("\n== status → error mapping ==")
    cases = [
        (401, AuthError),
        (403, AuthError),
        (404, RequestError),
        (500, ServerError),
    ]
    for status, exc_type in cases:
        server = ProbeServer(behavior="status", status=status, detail=f"d{status}")
        client = ServiceClient(server.base_url)
        try:
            raised = None
            try:
                client.request("GET", "/x")
            except Exception as exc:  # noqa: BLE001 — asserting the concrete type below
                raised = exc
            check(
                f"{status} → {exc_type.__name__}",
                isinstance(raised, exc_type),
                f"got {type(raised).__name__}",
            )
            check(
                f"{status} carries status+detail",
                raised is not None
                and raised.status == status
                and raised.detail == f"d{status}",
            )
        finally:
            server.close()


def test_backoff_bounded():
    print("\n== backoff: exponential + bounded ==")
    policy = RetryPolicy(max_attempts=10, base_backoff=0.1, max_backoff=10.0)
    # The capped portion never exceeds max_backoff; jitter adds < base_backoff.
    check("backoff(0) small", policy.backoff(0) <= 0.1 + 0.1 + 1e-9)
    check(
        "backoff grows with attempt",
        policy.backoff(3) >= policy.backoff(0),
    )
    check(
        "backoff bounded by max_backoff (+jitter)",
        policy.backoff(100) <= 10.0 + 0.1 + 1e-9,
        f"got {policy.backoff(100)}",
    )


# ── ChangeFeedWatcher tests ──────────────────────────────────────────────────


class _Sink:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: list[int] = []
        self.resets = 0

    def on_event(self, ev):
        with self.lock:
            self.events.append(ev["id"])

    def on_reset(self, resp):
        with self.lock:
            self.resets += 1

    def ids(self):
        with self.lock:
            return list(self.events)


def test_watcher_ordering_under_out_of_order_wakes():
    print("\n== watcher: ordering under out-of-order wakes ==")
    server = FeedServer(ledger_ids=range(1, 6), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        cursor=0,
        limit=3,  # small: force multi-page contiguous draining
        poll_interval=0.1,
        base_backoff=0.02,
    ).start()
    try:
        # First batch (1..5) delivered after connect/poll.
        check(
            "initial batch delivered in order",
            wait_for(lambda: sink.ids() == [1, 2, 3, 4, 5]),
            f"got {sink.ids()}",
        )

        # Append more, then fire scrambled + duplicated wake hints. Their
        # payload is irrelevant — replay defines order.
        for i in range(6, 11):
            server.append(i)
        for wid in (9, 6, 10, 6, 8, 7, 9):
            server.push_wake({"type": "event", "id": wid})

        check(
            "full ledger delivered in exact order, no dups",
            wait_for(lambda: sink.ids() == list(range(1, 11))),
            f"got {sink.ids()}",
        )
        check("no duplicate deliveries", len(sink.ids()) == len(set(sink.ids())))
        check("cursor at ledger tail", watcher.cursor == 10, f"got {watcher.cursor}")
    finally:
        watcher.stop()
        server.close()


def test_watcher_reset_jumps_cursor():
    print("\n== watcher: reset flag jumps cursor past trimmed gap ==")
    # Ledger holds 1..10 but the retention floor is 5: ids <= 5 are gone.
    server = FeedServer(ledger_ids=range(1, 11), retention_floor=5, ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        cursor=0,
        poll_interval=0.1,
        base_backoff=0.02,
    ).start()
    try:
        check(
            "only post-floor events delivered",
            wait_for(lambda: sink.ids() == [6, 7, 8, 9, 10]),
            f"got {sink.ids()}",
        )
        check("on_reset invoked", wait_for(lambda: sink.resets >= 1))
        check("watcher recorded a reset", watcher.resets >= 1)
        check(
            "cursor jumped to ledger tail",
            watcher.cursor == 10,
            f"got {watcher.cursor}",
        )
    finally:
        watcher.stop()
        server.close()


def test_watcher_dropped_wake_recovers_via_poll():
    print("\n== watcher: dropped wake recovered by poll tick ==")
    # WS stays connected but never pushes a wake; delivery must still happen
    # via the periodic poll.
    server = FeedServer(ledger_ids=(), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        cursor=0,
        poll_interval=0.15,
        base_backoff=0.02,
    ).start()
    try:
        check("connected to wake channel", wait_for(lambda: server.ws_connects >= 1))
        # Append without any wake — poll tick must find it.
        server.append(1)
        server.append(2)
        check(
            "events delivered without a wake",
            wait_for(lambda: sink.ids() == [1, 2], timeout=3.0),
            f"got {sink.ids()}",
        )
    finally:
        watcher.stop()
        server.close()


def test_watcher_flapping_backoff_grows():
    print("\n== watcher: flapping hub keeps backing off ==")
    # The hub accepts the upgrade then drops immediately: never stable, so
    # reconnect backoff must climb rather than reset to base.
    server = FeedServer(ledger_ids=(), ws_mode="flap")
    client = ServiceClient(server.base_url, timeout=2.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        cursor=0,
        poll_interval=5.0,
        base_backoff=0.02,
        max_backoff=1.0,
        stable_period=30.0,
    ).start()
    try:
        check(
            "hub flapped several times",
            wait_for(lambda: server.ws_connects >= 3, timeout=_RECONNECT_TIMEOUT),
        )
        check(
            "reconnect backoff grew beyond base",
            wait_for(
                lambda: watcher.reconnect_delay > 0.04, timeout=_RECONNECT_TIMEOUT
            ),
            f"got {watcher.reconnect_delay}",
        )
    finally:
        watcher.stop()
        server.close()


# ── Hardening fixtures ───────────────────────────────────────────────────────


class BodyServer(_BaseServer):
    """Answers every request with a fixed status/body (records request count).

    Used to exercise 2xx-non-JSON handling and the response size cap without
    the retry semantics of ProbeServer's ``drop`` behavior.
    """

    def __init__(self, status=200, body=b"", extra_headers=b""):
        self.status = status
        self.body = body
        self.extra_headers = extra_headers
        self._lock = threading.Lock()
        self.requests = 0
        self.auth_seen: list[str] = []
        self.headers_seen: list[dict] = []
        super().__init__()

    def count(self) -> int:
        with self._lock:
            return self.requests

    def _handle(self, conn):
        parsed = self._read_head(conn)
        if parsed is None:
            conn.close()
            return
        _method, _path, headers = parsed
        with self._lock:
            self.requests += 1
            self.auth_seen.append(headers.get("authorization", ""))
            self.headers_seen.append(dict(headers))
        conn.sendall(
            b"HTTP/1.1 %d X\r\nContent-Length: %d\r\nConnection: close\r\n%s\r\n%s"
            % (self.status, len(self.body), self.extra_headers, self.body)
        )
        conn.close()


class OversizedFrameServer(FeedServer):
    """Completes the WS upgrade, then announces a frame far larger than the
    client's cap without sending its payload."""

    def __init__(self, announced_len: int):
        self.announced_len = announced_len
        super().__init__(ledger_ids=(), ws_mode="hold")

    def _handle_ws(self, conn, headers):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept.encode()
        )
        with self._ws_lock:
            self.ws_connects += 1
        # 0x81 = FIN|text; 127 = 64-bit length follows.
        conn.sendall(struct.pack("!BBQ", 0x81, 127, self.announced_len))
        with contextlib.suppress(OSError):
            # Hold until the client hangs up after rejecting the announced size.
            conn.recv(4096)
        with contextlib.suppress(OSError):
            conn.close()


def _mint_self_signed() -> tuple[str, str, str]:
    """Write a self-signed cert + its key + an unrelated key to temp files.

    Returns ``(cert_path, key_path, wrong_key_path)``. The wrong key lets a
    test prove ``load_cert_chain`` actually runs (a cert/key mismatch raises).
    """
    import datetime
    import tempfile

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def _key_pem(key) -> bytes:
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

    key = ec.generate_private_key(ec.SECP256R1())
    wrong_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "svc-client")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    d = tempfile.mkdtemp(prefix="sc_certs_")
    cert_path = str(Path(d) / "client.crt")
    key_path = str(Path(d) / "client.key")
    wrong_key_path = str(Path(d) / "wrong.key")
    Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    Path(key_path).write_bytes(_key_pem(key))
    Path(wrong_key_path).write_bytes(_key_pem(wrong_key))
    return cert_path, key_path, wrong_key_path


# ── Hardening tests ──────────────────────────────────────────────────────────


def test_non_json_2xx_raises_response_error():
    print("\n== 2xx with non-JSON body → ResponseError ==")
    server = BodyServer(status=200, body=b"<html>captive portal</html>")
    client = ServiceClient(server.base_url, timeout=2.0)
    try:
        raised = None
        try:
            client.request("GET", "/x")
        except ServiceError as exc:  # ResponseError is a ServiceError
            raised = exc
        check("non-JSON 200 raises ResponseError", isinstance(raised, ResponseError))
        check(
            "ResponseError carries the 2xx status",
            raised is not None and raised.status == 200,
            f"got {getattr(raised, 'status', None)}",
        )
    finally:
        server.close()


def test_drain_thread_survives_non_json_page():
    print("\n== non-JSON 200 replay page does not kill the drain thread ==")
    server = BodyServer(status=200, body=b"not json {")
    client = ServiceClient(server.base_url, timeout=1.0)
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        on_event=lambda e: None,
        poll_interval=0.05,
    ).start()
    try:
        # After several poll ticks against a non-JSON page the thread must live.
        check(
            "server was polled repeatedly",
            wait_for(lambda: server.count() >= 3, timeout=3.0),
            f"got {server.count()}",
        )
        check("drain thread still alive", watcher._drain_thread.is_alive())
    finally:
        watcher.stop()
        server.close()


def test_response_size_cap():
    print("\n== oversized response body → ResponseError, not OOM ==")
    server = BodyServer(status=200, body=b"[" + b"0," * 5000 + b"0]")
    client = ServiceClient(server.base_url, timeout=2.0, max_response_bytes=128)
    try:
        raised = None
        try:
            client.request("GET", "/big")
        except ResponseError as exc:
            raised = exc
        check("oversized body raises ResponseError", isinstance(raised, ResponseError))
    finally:
        server.close()


def test_redirect_not_followed_token_not_leaked():
    print("\n== cross-host 302 not followed; token not re-sent ==")
    server = BodyServer(
        status=302, body=b"", extra_headers=b"Location: http://evil.example/steal"
    )
    client = ServiceClient(server.base_url, token="secret-tok", timeout=2.0)
    try:
        raised = None
        try:
            client.request("GET", "/x")
        except ServiceError as exc:
            raised = exc
        check("302 raises (not followed)", isinstance(raised, RequestError))
        check(
            "302 mapped with its status",
            raised is not None and raised.status == 302,
            f"got {getattr(raised, 'status', None)}",
        )
        check(
            "exactly one request reached the origin (no redirect hop)",
            server.count() == 1,
            f"got {server.count()}",
        )
        check(
            "token sent once, to the origin only",
            server.auth_seen == ["Bearer secret-tok"],
            f"got {server.auth_seen}",
        )
    finally:
        server.close()


def test_ws_handshake_crlf_rejected():
    print("\n== WS handshake rejects CRLF injection ==")
    server = FeedServer(ledger_ids=(), ws_mode="hold")
    try:
        # CRLF in the token (carried into the auth header value). The secret
        # token must never appear in the raised message — it lands in logs and
        # tracebacks.
        secret = "sup3r-secret-tok"
        evil = ServiceClient(
            server.base_url, token=f"{secret}\r\nX-Injected: 1", timeout=1.0
        )
        rejected = False
        msg = ""
        try:
            evil.open_websocket("/ws/feed")
        except ValueError as exc:
            rejected = True
            msg = str(exc)
        check("CRLF token rejected with ValueError", rejected)
        check(
            "credential redacted from the error message",
            secret not in msg and "X-Injected" not in msg,
            f"leaked: {msg!r}",
        )
        check(
            "error still names the field and offending byte",
            "header value" in msg and "0x" in msg,
            f"got {msg!r}",
        )

        # CRLF in the path (forged request line).
        ok = ServiceClient(server.base_url, token="tok", timeout=1.0)
        rejected_path = False
        try:
            ok.open_websocket("/ws/feed\r\nX-Injected: 1")
        except ValueError:
            rejected_path = True
        check("CRLF path rejected with ValueError", rejected_path)

        # CRLF in an extra header value.
        rejected_hdr = False
        try:
            ok.open_websocket("/ws/feed", extra_headers={"X-Sub": "a\r\nX-Evil: 1"})
        except ValueError:
            rejected_hdr = True
        check("CRLF extra header rejected with ValueError", rejected_hdr)

        check(
            "no connection was ever established for a rejected handshake",
            server.ws_connects == 0,
            f"got {server.ws_connects}",
        )
    finally:
        server.close()


def test_ws_oversized_frame_rejected():
    print("\n== oversized announced WS frame rejected ==")
    server = OversizedFrameServer(announced_len=1 << 30)  # 1 GiB announced
    client = ServiceClient(server.base_url, timeout=1.0, ws_max_frame_bytes=4096)
    try:
        conn = open_ws_retrying(client, "/ws/feed")
        raised = None
        try:
            conn.recv_json()
        except ServiceUnavailable as exc:
            raised = exc
        finally:
            conn.close()
        check("oversized frame raises before reading payload", raised is not None)
    finally:
        server.close()


def test_build_ssl_context_client_cert_without_ca():
    print("\n== client cert without ca_file is not silently dropped ==")
    cert, key, wrong_key = _mint_self_signed()
    check("no cert and no CA → None", build_ssl_context() is None)
    ctx = build_ssl_context(client_cert_file=cert, client_key_file=key)
    check("client cert without CA builds a context", ctx is not None)
    # A cert/key mismatch proves load_cert_chain actually ran (the old code
    # returned None here, loading nothing).
    mismatched = False
    try:
        build_ssl_context(client_cert_file=cert, client_key_file=wrong_key)
    except ssl.SSLError:
        mismatched = True
    check("mismatched key raises (cert chain was loaded)", mismatched)


def test_idle_ws_survives_and_backoff_stable():
    print("\n== idle wake channel survives past timeout; backoff stays at base ==")
    # A quiet hub must not trip a read timeout and churn reconnects; keepalive
    # pings hold it up. poll_interval is large so only the WS drives this. The
    # subject is the ESTABLISHED session — how many attempts it took to
    # establish one against a fixture hub is a different question, and not one
    # this test has an opinion about.
    server = FeedServer(ledger_ids=(), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=0.3, ws_ping_interval=0.1)
    base = 0.05
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=lambda e: None,
        poll_interval=30.0,
        base_backoff=base,
        stable_period=30.0,
    ).start()
    try:
        # Sequence on the client's OWN session state, not on the hub's socket
        # count. The two differ, and the difference is not churn: this client is
        # configured to give the upgrade 0.3s (`timeout`), and a thread-per-
        # connection fake hub on a loaded machine can take longer than that to
        # be scheduled to write its 101. The client then abandons that attempt
        # and retries — textbook, and the correct response to a peer that missed
        # the deadline it was given — but the hub has already counted the
        # socket. Asserting on `server.ws_connects` therefore measured how
        # promptly the runner scheduled a thread, not whether an established
        # session churned, which is the actual claim here.
        check(
            "connected once",
            watcher.wait_connected(timeout=_RECONNECT_TIMEOUT),
            f"never established (hub saw {server.ws_connects} sockets)",
        )
        established = watcher.connects
        delay_at_connect = watcher.reconnect_delay
        # "Outlives the read timeout" is now waited for rather than slept
        # through. Each keepalive is one idle interval (0.1s) this session
        # survived with zero inbound bytes, so twelve of them is 1.2s of real
        # idleness — four times the 0.3s read timeout — measured by the thing
        # under test instead of by this machine's clock. A loaded runner only
        # takes longer to get there; it cannot shorten the window the checks
        # below are made over, nor fabricate a disconnect.
        check(
            "idle session outlived the read timeout many times over",
            wait_for(lambda: watcher.keepalives >= 12, timeout=_RECONNECT_TIMEOUT),
            f"got {watcher.keepalives} keepalives",
        )
        # THE invariant: an established idle session is never dropped, because
        # nothing here is a cause to drop it. A drop cannot happen without one,
        # whatever the machine was doing at the time.
        check(
            "no reconnect churn on an idle hub",
            watcher.disconnects == 0 and watcher.connects == established,
            f"got {watcher.connects} sessions / {watcher.disconnects} drops after "
            f"{watcher.keepalives} keepalives (hub saw {server.ws_connects} "
            f"sockets, peer_timeouts={watcher.peer_timeouts}, "
            f"last_silence={watcher.last_peer_silence}, "
            f"stall={watcher.stall_seconds:.3f}s)",
        )
        check(
            "a hub that answers every ping is never declared unresponsive",
            watcher.peer_timeouts == 0,
            f"got {watcher.peer_timeouts}, last={watcher.last_peer_silence}",
        )
        # Backoff only ever moves when an attempt ends, so a healthy session
        # holding must leave it exactly where establishing it did — including
        # when establishing took a retry, which is not this session's doing.
        check(
            "backoff never grew on a healthy session",
            watcher.reconnect_delay == delay_at_connect,
            f"got {watcher.reconnect_delay}, was {delay_at_connect} at connect",
        )
    finally:
        watcher.stop()
        server.close()


def test_request_raw_returns_status():
    print("\n== request_raw surfaces status without raising ==")
    # Non-2xx status returned, not raised.
    for status in (404, 409, 500):
        server = ProbeServer(behavior="status", status=status, detail=f"d{status}")
        client = ServiceClient(server.base_url, timeout=2.0)
        try:
            st, headers, body = client.request_raw("GET", "/x")
            check(f"{status} returned as status", st == status, f"got {st}")
            check(
                f"{status} body parsed",
                isinstance(body, dict) and body.get("detail") == f"d{status}",
                f"got {body}",
            )
            check(f"{status} headers are a dict", isinstance(headers, dict))
        finally:
            server.close()

    # 302 is returned (not followed, not raised) so a caller sees the redirect.
    redir = BodyServer(
        status=302, body=b"", extra_headers=b"Location: http://elsewhere/x"
    )
    client = ServiceClient(redir.base_url, timeout=2.0)
    try:
        st, headers, _body = client.request_raw("GET", "/x")
        check("request_raw returns 302", st == 302, f"got {st}")
        check("request_raw exposes Location header", "http" in str(headers).lower())
    finally:
        redir.close()

    # 2xx JSON body parsed and returned with status.
    ok = FeedServer(ledger_ids=(1,), ws_mode="hold")
    client = ServiceClient(ok.base_url, timeout=2.0)
    try:
        st, _headers, body = client.request_raw("GET", "/v1/events")
        check("request_raw 200 status", st == 200, f"got {st}")
        check(
            "request_raw 200 body parsed", isinstance(body, dict) and "events" in body
        )
    finally:
        ok.close()

    # A transport failure still retries and raises ServiceUnavailable.
    drop = ProbeServer(behavior="drop")
    client = ServiceClient(
        drop.base_url, retry=RetryPolicy(max_attempts=2, base_backoff=0.001)
    )
    try:
        raised = None
        try:
            client.request_raw("GET", "/x")
        except ServiceUnavailable as exc:
            raised = exc
        check("request_raw still raises on transport exhaustion", raised is not None)
    finally:
        drop.close()


def test_wake_preserved_during_in_flight_drain():
    print("\n== wake raised during an in-flight drain triggers a later drain ==")
    drained = threading.Event()
    release = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def replay(after: int) -> dict:
        with calls_lock:
            calls.append(time.monotonic())
            n = len(calls)
        if n == 1:
            drained.set()
            release.wait(3.0)  # hold the first drain open while a wake arrives
        return {"events": [], "cursor": after, "reset": False}

    watcher = ChangeFeedWatcher(
        replay=replay,
        on_event=lambda e: None,
        poll_interval=30.0,  # a lost wake would otherwise stay lost for 30s
    ).start()
    try:
        check("first drain in flight", drained.wait(3.0))
        watcher._wake.set()  # wake arrives mid-drain
        release.set()  # let the first drain finish
        check(
            "a second drain runs promptly (wake was preserved)",
            wait_for(lambda: len(calls) >= 2, timeout=3.0),
            f"got {len(calls)} drains",
        )
    finally:
        watcher.stop()


class ResetBodyServer(_BaseServer):
    """Sends a non-2xx status announcing a body, then RSTs the connection so a
    read of the error body fails at the transport layer (SO_LINGER 0)."""

    def __init__(self, status=500):
        self.status = status
        super().__init__()

    def _handle(self, conn):
        if self._read_head(conn) is None:
            conn.close()
            return
        conn.sendall(
            b"HTTP/1.1 %d X\r\nContent-Length: 65536\r\nConnection: close\r\n\r\n"
            % self.status
        )
        with contextlib.suppress(OSError):
            conn.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        with contextlib.suppress(OSError):
            conn.close()


class TruncatedOkServer(_BaseServer):
    """Sends a 2xx with a chunked body, announces a chunk far larger than the
    bytes it then sends, and closes mid-chunk. A truncated chunked stream makes
    the client's success-path read raise http.client.IncompleteRead — an
    HTTPException, not an OSError — so it exercises the body-read transport gap
    on the 2xx path (distinct from ResetBodyServer's error-body RST)."""

    def __init__(self, status=200):
        self.status = status
        self._lock = threading.Lock()
        self.requests = 0
        super().__init__()

    def count(self) -> int:
        with self._lock:
            return self.requests

    def _handle(self, conn):
        if self._read_head(conn) is None:
            conn.close()
            return
        with self._lock:
            self.requests += 1
        conn.sendall(
            b"HTTP/1.1 %d OK\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n" % self.status
        )
        # Announce a 0x3e8 (1000) byte chunk but send only a few, then close:
        # the chunked reader hits EOF mid-chunk → IncompleteRead.
        with contextlib.suppress(OSError):
            conn.sendall(b"3e8\r\nshort")
        with contextlib.suppress(OSError):
            conn.close()


class HugeHandshakeServer(_BaseServer):
    """Accepts, then streams bytes that never contain the CRLFCRLF handshake
    terminator — the client's handshake read must cap the buffer and give up."""

    def _handle(self, conn):
        with contextlib.suppress(OSError):
            conn.recv(4096)  # the client's upgrade request
            conn.sendall(b"X" * (200 * 1024))  # 200 KiB, no CRLFCRLF ever
            while not self._stop:
                if not conn.recv(4096):
                    break
        with contextlib.suppress(OSError):
            conn.close()


class LaggingFeedServer(FeedServer):
    """Replay hides event 1 until it has been polled ``reveal_after`` times,
    modeling a replay ceiling that lags a wake (a long transaction holding the
    visibility horizon back). The wake frame still advertises cursor 1, so the
    watcher's re-drain must catch the event well before the poll interval."""

    def __init__(self, reveal_after=4):
        self.reveal_after = reveal_after
        self._polls = 0
        super().__init__(ledger_ids=(1,), ws_mode="hold")

    def _replay(self, after, limit):
        with self._lock:
            self._polls += 1
            revealed = self._polls >= self.reveal_after
        if not revealed:
            return {"events": [], "cursor": 0, "reset": False}
        return {
            "events": [{"id": 1, "subject": "s/1"}],
            "cursor": 1,
            "reset": False,
        }


class UpgradeRefuseServer(_BaseServer):
    """Accepts the TCP connection and reads the upgrade request, then refuses:
    a non-101 status (``mode='refuse'``) or a hangup with no reply
    (``mode='hangup'``). Exercises the client's construction-failure fd cleanup
    — ``open_websocket`` never returns the connection object, so a leaked socket
    would never reach the reconnect loop's finally-close and would accumulate."""

    def __init__(self, mode="refuse"):
        self.mode = mode
        super().__init__()

    def _handle(self, conn):
        with contextlib.suppress(OSError):
            self._read_head(conn)
            if self.mode == "refuse":
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
            # 'hangup': send nothing; the close below tears down mid-handshake.
        with contextlib.suppress(OSError):
            conn.close()


class UnreachableTargetServer(FeedServer):
    """Advertises a wake cursor far beyond anything replay will ever reveal
    while trickling one unrelated event per poll. Each drain advances the cursor
    a little (progress) yet never reaches the target — a compromised or
    mis-mapped hint. The re-drain backoff must decay toward the poll cadence,
    not pin at the floor on mere progress. Records each replay's arrival time so
    the test can prove the request interval grew."""

    def __init__(self):
        self._polls = 0
        self.poll_times: list[float] = []
        super().__init__(ledger_ids=(), ws_mode="hold")

    def _replay(self, after, limit):
        with self._lock:
            self._polls += 1
            self.poll_times.append(time.monotonic())
            nid = self._polls  # a fresh event each poll; a short page → break
            return {
                "events": [{"id": nid, "subject": f"s/{nid}"}],
                "cursor": nid,
                "reset": False,
            }


def _open_fd_count() -> int:
    """Open file-descriptor count for this process, or -1 if unavailable.

    Linux exposes descriptors under /proc/self/fd, macOS/BSD under /dev/fd;
    either lists one entry per live fd."""
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(list(Path(path).iterdir()))
        except OSError:
            continue
    return -1


def test_ws_handshake_failure_closes_socket():
    print("\n== a failed WS handshake closes the socket (no fd leak) ==")
    for mode, label in (
        ("refuse", "upgrade refused"),
        ("hangup", "closed mid-handshake"),
    ):
        server = UpgradeRefuseServer(mode=mode)
        client = ServiceClient(server.base_url, timeout=2.0)
        try:
            # Warm up so first-touch allocations don't skew the baseline.
            for _ in range(5):
                with contextlib.suppress(ServiceUnavailable):
                    client.open_websocket("/ws/feed")
            before = _open_fd_count()
            all_raised = True
            for _ in range(60):
                try:
                    client.open_websocket("/ws/feed")
                    all_raised = False  # a refused handshake must never succeed
                except ServiceUnavailable:
                    pass
            check(
                f"every failed handshake raised ServiceUnavailable ({label})",
                all_raised,
            )
            # A real leak (+1 fd per attempt) never settles; server-side conn
            # sockets close asynchronously, so allow a small settle window.
            settled = wait_for(lambda: 0 <= _open_fd_count() <= before + 8, timeout=3.0)
            after = _open_fd_count()
            check(
                f"open fds do not grow across 60 failed handshakes ({label})",
                before < 0 or settled,
                f"fds {before} -> {after}",
            )
        finally:
            server.close()


def test_unreachable_wake_target_decays_to_poll_cadence():
    print("\n== a never-reached wake target decays to poll cadence, not pinned ==")
    server = UnreachableTargetServer()
    client = ServiceClient(server.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        cursor=0,
        poll_interval=5.0,  # a floor-pinned re-drain would fire ~20×/s vs. this
        base_backoff=0.02,
        wake_cursor_field="cursor",
    ).start()
    try:
        check("connected to wake channel", wait_for(lambda: server.ws_connects >= 1))
        # Advertise an unreachable target; replay only ever trickles one event.
        server.push_wake({"cursor": 10_000})
        # Let the re-drain sequence run and double toward the poll cadence.
        time.sleep(3.0)
        with server._lock:
            times = list(server.poll_times)
        gaps = [b - a for a, b in zip(times, times[1:])]
        last_gap = gaps[-1] if gaps else 0.0
        check("replay was actually driven", len(times) >= 3, f"{len(times)} polls")
        check(
            "re-drain interval grew above the floor (not pinned at ~0.05s)",
            last_gap > 0.3,
            f"last gap {last_gap:.3f}s; gaps={[round(g, 3) for g in gaps]}",
        )
        check(
            "bounded request rate — decayed, not amplified (~20/s would be ~60)",
            len(times) < 20,
            f"{len(times)} polls in ~3s",
        )
    finally:
        watcher.stop()
        server.close()


def test_ws_scheme_guard():
    print("\n== a ws/wss base_url is rejected, not silently downgraded ==")
    for scheme in ("ws", "wss"):
        client = ServiceClient(f"{scheme}://127.0.0.1:1", timeout=1.0)
        rejected = False
        try:
            client.open_websocket("/ws/feed")
        except ValueError:
            rejected = True
        check(
            f"{scheme}:// base_url rejected with ValueError (no plaintext downgrade)",
            rejected,
        )

    # http/https pass the scheme guard: the failure is a connect-time
    # ServiceUnavailable, not a ValueError.
    client = ServiceClient("http://127.0.0.1:1", timeout=1.0)
    err = None
    try:
        client.open_websocket("/ws/feed")
    except Exception as exc:  # noqa: BLE001
        err = exc
    check(
        "http:// passes the scheme guard (connect-time failure, not ValueError)",
        isinstance(err, ServiceUnavailable),
        f"{err!r}",
    )


def test_error_body_capped_and_typed():
    print("\n== non-2xx error body is capped and always typed ==")
    # Item 1: an error body larger than the cap does not balloon memory; the
    # capped read degrades to empty detail and the status maps to a typed error.
    server = BodyServer(status=500, body=b"x" * 100_000)
    client = ServiceClient(server.base_url, timeout=2.0, max_response_bytes=256)
    try:
        raised = None
        try:
            client.request("GET", "/x")
        except ServiceError as exc:
            raised = exc
        check("oversized 500 body → typed ServerError", isinstance(raised, ServerError))
        check(
            "oversized error body maps with its status (detail degraded)",
            raised is not None and raised.status == 500,
            f"got {getattr(raised, 'status', None)}",
        )
    finally:
        server.close()

    # Item 8: a transport failure while reading the error body must not escape
    # untyped — request() still yields a typed ServiceError.
    reset = ResetBodyServer(status=500)
    client = ServiceClient(
        reset.base_url,
        timeout=2.0,
        retry=RetryPolicy(max_attempts=2, base_backoff=0.001),
    )
    try:
        raised = None
        escaped = None
        try:
            client.request("GET", "/x")
        except ServiceError as exc:
            raised = exc
        except Exception as exc:  # noqa: BLE001 — proving nothing untyped escapes
            escaped = exc
        check(
            "error-body read failure stays typed (no OSError/IncompleteRead)",
            escaped is None and isinstance(raised, ServiceError),
            f"raised={type(raised).__name__} escaped={escaped!r}",
        )
    finally:
        reset.close()

    # request_raw must likewise never leak an untyped read error.
    reset2 = ResetBodyServer(status=500)
    client = ServiceClient(
        reset2.base_url,
        timeout=2.0,
        retry=RetryPolicy(max_attempts=2, base_backoff=0.001),
    )
    try:
        escaped = None
        try:
            client.request_raw("GET", "/x")
        except ServiceError:
            pass
        except Exception as exc:  # noqa: BLE001
            escaped = exc
        check(
            "request_raw error-body read stays typed", escaped is None, f"{escaped!r}"
        )
    finally:
        reset2.close()


def test_ws_path_requires_client():
    print("\n== ws_path without a client is rejected at construction ==")
    rejected = False
    try:
        ChangeFeedWatcher(
            replay=lambda after: {"events": [], "cursor": after},
            on_event=lambda e: None,
            ws_path="/ws/feed",
        )
    except ValueError:
        rejected = True
    check("ws_path + no client → ValueError (no silent poll-only degrade)", rejected)

    # The same config with a client is accepted.
    ok = True
    try:
        ChangeFeedWatcher(
            ServiceClient("http://127.0.0.1:1"),
            replay_path="/v1/events",
            on_event=lambda e: None,
            ws_path="/ws/feed",
        )
    except ValueError:
        ok = False
    check("ws_path + client is accepted", ok)


def test_drain_unadvanced_full_page_breaks():
    print("\n== a full page that does not advance the cursor breaks, not loops ==")
    calls = []
    calls_lock = threading.Lock()

    def replay(after: int) -> dict:
        with calls_lock:
            calls.append(after)
        # A full page (len == limit) whose events carry no matching id and whose
        # echoed cursor never advances: the old code would loop forever.
        return {"events": [{"x": 1}, {"x": 2}], "cursor": after, "reset": False}

    watcher = ChangeFeedWatcher(
        replay=replay,
        on_event=lambda e: None,
        limit=2,
        poll_interval=5.0,  # a hot loop would ignore this and hammer replay
    ).start()
    try:
        check(
            "unadvanced full page counted as a drain error",
            wait_for(lambda: watcher.drain_errors >= 1, timeout=2.0),
            f"drain_errors={watcher.drain_errors}",
        )
        # Prove it broke instead of hammering. "Not a hot loop" is a RATE
        # claim, so measure a rate: snapshot the call count, observe a window,
        # and compare the DELTA against what a correct loop is entitled to make
        # in the time that actually elapsed (one drain per poll_interval, plus
        # slack for the tick in flight). A fixed ceiling would instead be a bet
        # that the window really was ~0.6s — on a runner that oversleeps to 30s
        # a perfectly-behaved loop would blow through it.
        with calls_lock:
            calls_before = len(calls)
        started = time.monotonic()
        # timing-window: a bounded NEGATIVE — nothing becomes true when the
        # drain declines to hammer replay, so an observation window is the only
        # construct available. The ceiling below is derived from the measured
        # elapsed time, so oversleeping cannot flip the result.
        time.sleep(0.6)
        elapsed = time.monotonic() - started
        with calls_lock:
            n = len(calls) - calls_before
        allowed = 2 + int(elapsed / 5.0)  # poll_interval=5.0
        check(
            "no hot loop (replay not hammered)",
            n <= allowed,
            f"got {n} replay calls in {elapsed:.2f}s (allowed {allowed})",
        )
        check("drain thread still alive", watcher._drain_thread.is_alive())
    finally:
        watcher.stop()


def test_ws_silent_peer_detected():
    print("\n== a black-holed wake peer is detected and reconnected ==")
    # The hub completes the handshake but never sends a byte (not even a pong):
    # the client's keepalive pings go unanswered, so after a few missed
    # intervals it must give up on the dead connection and reconnect.
    server = FeedServer(ledger_ids=(), ws_mode="hold", pong_pings=False)
    client = ServiceClient(server.base_url, timeout=2.0, ws_ping_interval=0.05)
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=lambda e: None,
        poll_interval=30.0,  # only the WS drives reconnects here
        base_backoff=0.02,
        max_backoff=0.1,
        stable_period=30.0,
    ).start()
    try:
        check(
            "silent peer detected → reconnect (not pinging forever)",
            wait_for(lambda: server.ws_connects >= 2, timeout=_RECONNECT_TIMEOUT),
            f"got {server.ws_connects} connects",
        )
        check(
            "the drop is attributed to the peer, not to a bare socket timeout",
            watcher.peer_timeouts >= 1,
            f"got {watcher.peer_timeouts}",
        )
        check("it pinged before giving up", watcher.keepalives >= 1)
        # The verdict carries its own evidence: silence this client actually
        # watched, measured past a deadline already widened by however long it
        # was not scheduled to watch. That is what makes the drop defensible
        # rather than a report of how busy the machine was.
        silence = watcher.last_peer_silence
        check("the drop recorded its evidence", silence is not None)
        check(
            "watched silence met the deadline it was judged against",
            silence.observed_seconds >= silence.deadline_seconds,
            f"got {silence}",
        )
        # No unearned grace: whatever the deadline grew to, it grew by exactly
        # the stall the client is reporting — nothing else may widen it. (Float
        # arithmetic on a 0.05 interval, hence the epsilon, not a tolerance on
        # the claim.)
        check(
            "the deadline is the documented one plus credited stall only",
            abs(
                silence.deadline_seconds
                - (_WS_MAX_MISSED_PINGS * 0.05 + silence.stall_seconds)
            )
            < 1e-9,
            f"got {silence}",
        )
    finally:
        watcher.stop()
        server.close()


def _blind_connection(sock, ping_interval: float):
    """A ``_WebSocketConnection`` around an already-open socket.

    White-box on purpose: the unit under test is the read loop's liveness
    accounting, and the alternative — a real hub plus a real overloaded host —
    is exactly the machine-dependent test this suite exists to stop writing.
    Everything ``_read_exact`` touches is set here, so a field it grows without
    this helper knowing fails loudly instead of silently skipping the check.
    """
    conn = object.__new__(_WebSocketConnection)
    conn._sock = sock
    conn._buf = b""
    conn._ping_interval = ping_interval
    conn.pings_sent = 0
    conn.stall_seconds = 0.0
    conn.peer_silence = None
    return conn


def test_ws_liveness_credits_local_stall():
    print("\n== a client that was not scheduled to look grants the peer grace ==")
    # A socketpair with nothing on the far end is a perfectly silent peer; what
    # varies is the CLIENT. Telling the read loop its window is `interval` while
    # the socket really blocks four times that long reproduces — with a real
    # socket and a real clock, no fake time and no sleeps — the one thing a
    # loaded host does to this loop: the kernel closed the window on time and
    # this thread was not scheduled to observe it until much later.
    interval = 0.02
    near, far = socket.socketpair()
    near.settimeout(interval * 4)  # each window overruns by 3 intervals
    conn = _blind_connection(near, interval)
    try:
        raised = None
        try:
            conn._read_exact(1)
        except ServiceUnavailable as exc:
            raised = exc
        check("a silent peer is still eventually declared dead", raised is not None)
        # Grace is bounded, so the count is exact on any machine: extra jitter
        # can only add stall, and stall is capped. The last window raises
        # instead of pinging, hence the -1.
        check(
            "every credited interval of blindness bought one more ping",
            conn.pings_sent
            == _WS_MAX_MISSED_PINGS + _WS_MAX_STALL_CREDIT_INTERVALS - 1,
            f"got {conn.pings_sent} pings",
        )
        silence = conn.peer_silence
        check(
            "the deadline was widened by the observed stall",
            silence.stall_seconds > 0.0
            and silence.deadline_seconds > _WS_MAX_MISSED_PINGS * interval,
            f"got {silence}",
        )
        check(
            "grace is capped — a permanently blind client still reconnects",
            silence.deadline_seconds
            <= (_WS_MAX_MISSED_PINGS + _WS_MAX_STALL_CREDIT_INTERVALS) * interval,
            f"got {silence}",
        )
        check(
            "stall is never charged to the peer as silence",
            silence.observed_seconds >= silence.deadline_seconds,
            f"got {silence}",
        )
        check(
            "the connection reports the stall it lived through",
            conn.stall_seconds >= silence.stall_seconds,
            f"got {conn.stall_seconds}",
        )
    finally:
        near.close()
        far.close()


def test_poll_only_watcher_reports_no_liveness_traffic():
    print("\n== a watcher with no wake channel has no liveness accounting ==")
    # A poll-only ledger watcher has no feed to keep alive, so every liveness
    # observable must read as "nothing happening" rather than as a stalled or
    # dead peer — an operator's health probe must not see a phantom drop.
    watcher = ChangeFeedWatcher(
        ServiceClient("http://127.0.0.1:1"),
        replay_path="/v1/events",
        on_event=lambda e: None,
    )
    check("no keepalives without a wake channel", watcher.keepalives == 0)
    check("no stall without a wake channel", watcher.stall_seconds == 0.0)
    check("no peer timeouts without a peer", watcher.peer_timeouts == 0)
    check("no silence verdict to report", watcher.last_peer_silence is None)


def test_wake_target_redrain():
    print("\n== a wake hint above the replay ceiling triggers short re-drain ==")
    server = LaggingFeedServer(reveal_after=4)
    client = ServiceClient(server.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        cursor=0,
        poll_interval=5.0,  # a wasted wake would delay delivery this long
        base_backoff=0.02,
        wake_cursor_field="cursor",
    ).start()
    try:
        check("connected to wake channel", wait_for(lambda: server.ws_connects >= 1))
        # Advertise cursor 1 while replay still hides event 1: the drain ends
        # below the hint, so the watcher must re-drain on a short backoff.
        server.push_wake({"cursor": 1})
        check(
            "event delivered well under poll_interval via re-drain",
            wait_for(lambda: sink.ids() == [1], timeout=2.0),
            f"got {sink.ids()} after {2.0}s (poll_interval=5.0)",
        )
        # Give any spurious re-drain a moment; delivery must stay exactly-once.
        # timing-window: a bounded NEGATIVE — "the hint never delivers a second
        # copy". A duplicate would arrive from a re-drain the watcher is still
        # entitled to make, so there is no state to wait for; only elapsed
        # observation can show it did not. Oversleeping widens the window and
        # strengthens the claim.
        time.sleep(0.3)
        check("no duplicate delivery from the hint", sink.ids() == [1], f"{sink.ids()}")
        check("cursor reached the hinted target", watcher.cursor == 1)
    finally:
        watcher.stop()
        server.close()


def test_handshake_response_capped():
    print("\n== an unbounded handshake response is capped ==")
    server = HugeHandshakeServer()
    client = ServiceClient(server.base_url, timeout=2.0)
    try:
        raised = None
        try:
            open_ws_retrying(client, "/ws/feed")
        except ServiceUnavailable as exc:
            raised = exc
        check(
            "oversized handshake → ServiceUnavailable (from the cap)",
            raised is not None and "exceeds" in str(raised),
            f"got {raised!r}",
        )
    finally:
        server.close()


def test_retry_policy_rejects_negative_backoff():
    print("\n== RetryPolicy rejects negative backoff, allows zero ==")
    rejected_base = False
    try:
        RetryPolicy(base_backoff=-1.0)
    except ValueError:
        rejected_base = True
    check("negative base_backoff rejected", rejected_base)

    rejected_max = False
    try:
        RetryPolicy(max_backoff=-0.5)
    except ValueError:
        rejected_max = True
    check("negative max_backoff rejected", rejected_max)

    ok = True
    try:
        RetryPolicy(base_backoff=0.0, max_backoff=0.0)  # immediate retry, allowed
    except ValueError:
        ok = False
    check("zero backoff allowed (immediate retry)", ok)


def test_local_resource_transient_classification():
    print("\n== EADDRNOTAVAIL/EADDRINUSE recognized as connect-time transients ==")
    import errno as _errno
    import urllib.error as _urllib_error

    from hyperdjango.serviceclient import _is_local_resource_transient

    bare = OSError(_errno.EADDRNOTAVAIL, "Can't assign requested address")
    wrapped = _urllib_error.URLError(OSError(_errno.EADDRINUSE, "Address in use"))
    check(
        "bare EADDRNOTAVAIL is a local-resource transient",
        _is_local_resource_transient(bare),
    )
    check(
        "URLError-wrapped EADDRINUSE is a local-resource transient",
        _is_local_resource_transient(wrapped),
    )
    # A genuine service-down / other error is NOT treated as a local transient
    # (so it still fails per the normal retry policy rather than waiting 30s).
    check(
        "ECONNREFUSED is NOT a local-resource transient",
        not _is_local_resource_transient(
            ConnectionRefusedError(_errno.ECONNREFUSED, "refused")
        ),
    )
    check(
        "a plain ValueError is not a local-resource transient",
        not _is_local_resource_transient(ValueError("nope")),
    )


def test_ws_connect_waits_out_local_port_exhaustion():
    print("\n== WS connect retries EADDRNOTAVAIL like the HTTP path ==")
    # Same policy as _request: a connect that never left this host (ephemeral
    # ports exhausted by TIME_WAIT churn) is waited out, not surfaced. Observed
    # live: a full-suite run failed e2e_hypermanager's open_websocket with
    # [Errno 49] while the HTTP path would have ridden it out.
    import errno as _errno
    import socket as _socket

    from hyperdjango import serviceclient as _sc

    calls = {"n": 0}
    real_create = _socket.create_connection

    def flaky_create(address, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError(_errno.EADDRNOTAVAIL, "Can't assign requested address")
        # After the transient window: a NON-transient failure must surface
        # typed immediately (keeps this test off the 30s deadline).
        raise ConnectionRefusedError(_errno.ECONNREFUSED, "refused")

    _socket.create_connection = flaky_create
    try:
        try:
            _sc._WebSocketConnection(
                "http://127.0.0.1:19999",
                "/ws",
                {},
                ssl_context=None,
                timeout=0.2,
            )
            check("connect raised", False, "no exception")
        except _sc.ServiceUnavailable as exc:
            check(
                "EADDRNOTAVAIL attempts were retried, not surfaced",
                calls["n"] >= 3,
                f"create_connection calls={calls['n']}",
            )
            check(
                "non-transient failure surfaces typed",
                "refused" in str(exc),
                str(exc),
            )
    finally:
        _socket.create_connection = real_create


def test_drain_survives_non_dict_page():
    print("\n== a top-level non-dict replay page is a typed bad page, no crash ==")

    def replay(after: int) -> dict:
        return ["not", "a", "dict"]  # a JSON top-level list

    watcher = ChangeFeedWatcher(
        replay=replay,
        on_event=lambda e: None,
        poll_interval=0.05,
    ).start()
    try:
        check(
            "non-dict page counted as a drain error",
            wait_for(lambda: watcher.drain_errors >= 1, timeout=2.0),
            f"drain_errors={watcher.drain_errors}",
        )
        check("drain thread survived the bad page", watcher._drain_thread.is_alive())
    finally:
        watcher.stop()


def test_callback_containment_and_counters():
    print("\n== on_event/on_reset exceptions are contained and counted ==")

    def replay_events(after: int) -> dict:
        return {"events": [{"id": i} for i in (1, 2, 3) if i > after], "cursor": 3}

    def boom_event(ev):
        raise RuntimeError("event handler blew up")

    watcher = ChangeFeedWatcher(
        replay=replay_events,
        on_event=boom_event,
        poll_interval=0.05,
    ).start()
    try:
        check(
            "on_event exceptions counted, thread lives",
            wait_for(lambda: watcher.event_callback_errors >= 3, timeout=2.0),
            f"event_callback_errors={watcher.event_callback_errors}",
        )
        check("cursor still advanced past failing events", watcher.cursor == 3)
        check(
            "drain thread alive after callback storm", watcher._drain_thread.is_alive()
        )
    finally:
        watcher.stop()

    def replay_reset(after: int) -> dict:
        return {"events": [], "cursor": 5, "reset": after < 5}

    def boom_reset(resp):
        raise RuntimeError("reset handler blew up")

    watcher = ChangeFeedWatcher(
        replay=replay_reset,
        on_event=lambda e: None,
        on_reset=boom_reset,
        poll_interval=0.05,
    ).start()
    try:
        check(
            "on_reset exceptions counted, thread lives",
            wait_for(lambda: watcher.reset_callback_errors >= 1, timeout=2.0),
            f"reset_callback_errors={watcher.reset_callback_errors}",
        )
        check("reset advanced the cursor past the floor", watcher.cursor == 5)
    finally:
        watcher.stop()


def test_service_client_from_env():
    print("\n== service_client_from_env reads {PREFIX}_* and honors overrides ==")
    import os

    from hyperdjango.serviceclient import service_client_from_env

    prefix = "SC_UNITTEST"
    keys = {
        f"{prefix}_URL": "https://svc.internal:9443",
        f"{prefix}_TOKEN": "env-token",
    }
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update(keys)
        client = service_client_from_env(
            prefix, token_header="X-API-Key", token_scheme=""
        )
        check("URL read from env", client.base_url == "https://svc.internal:9443")
        check(
            "token + header shape applied",
            client._auth_headers() == {"X-API-Key": "env-token"},
            f"got {client._auth_headers()}",
        )
        # An explicit override wins over the environment.
        client2 = service_client_from_env(prefix, token="override-tok")
        check(
            "override kwarg beats env",
            client2._auth_headers() == {"Authorization": "Bearer override-tok"},
            f"got {client2._auth_headers()}",
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_custom_field_names_and_extra_params():
    print("\n== custom events/id fields and extra_params ==")
    # Custom events_field / event_id_field via a replay override.
    collected = []

    def replay(after: int) -> dict:
        if after >= 2:
            return {"items": [], "end": 2}
        return {"items": [{"seq": 1}, {"seq": 2}], "end": 2}

    watcher = ChangeFeedWatcher(
        replay=replay,
        on_event=collected.append,
        events_field="items",
        cursor_field="end",
        event_id_field="seq",
        poll_interval=5.0,
    ).start()
    try:
        check(
            "events read from a custom events_field",
            wait_for(lambda: [e["seq"] for e in collected] == [1, 2], timeout=2.0),
            f"got {collected}",
        )
        check("cursor advanced via custom event_id_field", watcher.cursor == 2)
    finally:
        watcher.stop()

    # extra_params are attached to every replay request.
    server = FeedServer(ledger_ids=(1,), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0)
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        on_event=lambda e: None,
        extra_params={"prefix": "px"},
        poll_interval=1.0,  # the drain on start already issues the first pull
    ).start()
    try:
        check(
            "extra_params reach the replay request",
            wait_for(lambda: server.last_query.get("prefix") == "px", timeout=2.0),
            f"got {server.last_query}",
        )
        check(
            "cursor + limit params still present",
            "after" in server.last_query and "limit" in server.last_query,
            f"got {server.last_query}",
        )
    finally:
        watcher.stop()
        server.close()


def test_token_scheme_and_header_variants():
    print("\n== token_scheme='' and custom token_header ==")
    # token_scheme="" sends the raw token (API-key style), no "Bearer " prefix.
    server = BodyServer(status=200, body=b"{}")
    client = ServiceClient(
        server.base_url, token="rawkey", token_scheme="", timeout=2.0
    )
    try:
        client.request("GET", "/x")
        check(
            "empty scheme sends the raw token",
            server.auth_seen == ["rawkey"],
            f"got {server.auth_seen}",
        )
    finally:
        server.close()

    # Custom token_header routes the credential to a different header.
    server = BodyServer(status=200, body=b"{}")
    client = ServiceClient(
        server.base_url,
        token="k123",
        token_header="X-API-Key",
        token_scheme="",
        timeout=2.0,
    )
    try:
        client.request("GET", "/x")
        seen = server.headers_seen[0] if server.headers_seen else {}
        check(
            "custom header carries the token",
            seen.get("x-api-key") == "k123",
            f"{seen}",
        )
        check(
            "credential not also sent as Authorization",
            "authorization" not in seen,
            f"got {seen}",
        )
    finally:
        server.close()


def test_truncated_2xx_body_retries_and_typed():
    print("\n== a truncated 2xx body retries then surfaces typed ==")
    # A 200 announcing a Content-Length it never fully sends makes the success-
    # path body read raise IncompleteRead (HTTPException, not OSError). It must
    # retry to the policy limit and end as a typed ServiceUnavailable — never
    # escape raw and unretried.
    server = TruncatedOkServer(status=200)
    client = ServiceClient(
        server.base_url,
        timeout=2.0,
        retry=RetryPolicy(max_attempts=3, base_backoff=0.001),
    )
    try:
        raised = None
        escaped = None
        try:
            client.request("GET", "/x")
        except ServiceUnavailable as exc:
            raised = exc
        except Exception as exc:  # noqa: BLE001 — proving nothing untyped escapes
            escaped = exc
        # It must RETRY (more than one attempt) and end typed. Assert ">= 2"
        # rather than "== max_attempts": under heavy parallel load one attempt
        # can fail to connect (ephemeral-port pressure) before the server
        # accepts it, so the server-observed count is a lower bound on real
        # attempts — the meaningful guarantee is that a truncated 2xx is retried
        # at all rather than escaping raw on the first read.
        check(
            "truncated 2xx retried (not one-shot)",
            wait_for(lambda: server.count() >= 2),
            f"got {server.count()}",
        )
        check(
            "truncated 2xx surfaces as typed ServiceUnavailable",
            escaped is None and isinstance(raised, ServiceUnavailable),
            f"raised={type(raised).__name__} escaped={escaped!r}",
        )
    finally:
        server.close()

    # request_raw must likewise retry and end typed, never leak IncompleteRead.
    server2 = TruncatedOkServer(status=200)
    client = ServiceClient(
        server2.base_url,
        timeout=2.0,
        retry=RetryPolicy(max_attempts=2, base_backoff=0.001),
    )
    try:
        escaped = None
        try:
            client.request_raw("GET", "/x")
        except ServiceUnavailable:
            pass
        except Exception as exc:  # noqa: BLE001
            escaped = exc
        check("request_raw truncated 2xx stays typed", escaped is None, f"{escaped!r}")
    finally:
        server2.close()


def test_non_json_wake_drains_without_reconnect():
    print("\n== a non-JSON wake frame drives a drain without tearing down ==")
    # A non-JSON (or fragmented) wake frame is only a hint; recv_json would
    # raise ValueError and force a full reconnect. The tolerant wake path must
    # treat it as a wake — drain — and keep the connection.
    server = FeedServer(ledger_ids=(), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        cursor=0,
        poll_interval=30.0,  # only a wake can drive this delivery
        base_backoff=0.02,
    ).start()
    try:
        check("connected to wake channel", wait_for(lambda: server.ws_connects >= 1))
        # Let the connect-time drain settle before the real signal.
        check("initial drain issued", wait_for(lambda: server.last_query != {}, 2.0))
        server.append(1)
        server.push_wake_text(b"not json at all {")
        check(
            "non-JSON wake still drove a drain",
            wait_for(lambda: sink.ids() == [1], timeout=2.0),
            f"got {sink.ids()}",
        )
        check(
            "connection not torn down by the non-JSON frame",
            server.ws_connects == 1,
            f"got {server.ws_connects} connects",
        )
    finally:
        watcher.stop()
        server.close()


def test_zero_length_fragment_wake_is_bounded():
    print("\n== an endless zero-length fragment stream cannot wedge the wake loop ==")
    # A malicious/compromised hub can stream continuation frames whose length is
    # zero: they never grow the reassembly byte total and never block on a read,
    # so a byte-only cap would spin recv_wake forever. The fragment-count cap
    # must bound it — recv_wake raises rather than loops. This tests the cap
    # DIRECTLY (call recv_wake on a connection after flooding it) rather than
    # through the watcher's reconnect, so it is deterministic: no daemon-thread
    # scheduling or reconnect-timing race under full-parallel load. If the cap
    # were absent the call would hang and the runner's per-test timeout would
    # fail it — the opposite of a false pass.
    server = FeedServer(ledger_ids=(), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0, ws_ping_interval=5.0)
    conn = open_ws_retrying(client, "/ws/feed")
    try:
        # Far more than the reassembly cap; a byte-only bound would never trip.
        # The 1000 bytes are all in the socket buffer before recv_wake reads, so
        # the cap fires on this call with no dependence on wall-clock timing.
        server.push_zero_fragments(500)
        raised = None
        try:
            conn.recv_wake()
        except ServiceUnavailable as exc:
            raised = exc
        check(
            "fragment flood makes recv_wake raise (loop did not wedge)",
            isinstance(raised, ServiceUnavailable),
            f"got {raised!r}",
        )
    finally:
        conn.close()
        server.close()


def test_stuck_reset_fires_once():
    print("\n== a reset that never advances the cursor fires on_reset once ==")
    # reset=true forever, empty page, cursor echoing `after`: the cursor can
    # never advance, so a naive drain re-fires on_reset (and climbs `resets`)
    # every poll. The guard must fire it exactly once for the stuck position.
    fired = []
    fired_lock = threading.Lock()
    # The replay callable IS the poll tick, so counting its calls states the
    # condition the assertion below actually rests on — "many drains have run
    # against the stuck position" — instead of guessing how many a fixed sleep
    # buys. On a loaded runner a 0.6s sleep could buy only one tick, and the
    # loop-fire this test exists to catch would sail through.
    polls = []

    def replay(after: int) -> dict:
        with fired_lock:
            polls.append(after)
        return {"events": [], "cursor": after, "reset": True}

    def on_reset(resp):
        with fired_lock:
            fired.append(1)

    def poll_count() -> int:
        with fired_lock:
            return len(polls)

    watcher = ChangeFeedWatcher(
        replay=replay,
        on_event=lambda e: None,
        on_reset=on_reset,
        poll_interval=0.05,  # many drains in the observation window
    ).start()
    try:
        check(
            "on_reset fired at least once",
            wait_for(lambda: watcher.resets >= 1, timeout=2.0),
            f"resets={watcher.resets}",
        )
        check(
            "many poll ticks ran against the stuck reset",
            wait_for(lambda: poll_count() >= 10, timeout=10.0),
            f"polls={poll_count()}",
        )
        with fired_lock:
            n = len(fired)
        check(
            "stuck reset fired on_reset exactly once (no loop-fire)",
            n == 1 and watcher.resets == 1,
            f"on_reset fired {n} times, resets={watcher.resets}",
        )
    finally:
        watcher.stop()


def test_classify_status_taxonomy():
    print("\n== classify_status maps a status+detail to a typed error ==")
    from hyperdjango.serviceclient import classify_status

    cases = [
        (401, AuthError),
        (403, AuthError),
        (302, RequestError),
        (404, RequestError),
        (409, RequestError),
        (500, ServerError),
        (503, ServerError),
    ]
    for status, exc_type in cases:
        err = classify_status(status, f"d{status}")
        check(
            f"{status} → {exc_type.__name__}",
            type(err) is exc_type,
            f"got {type(err).__name__}",
        )
        check(
            f"{status} carries status + detail",
            err.status == status and err.detail == f"d{status}",
            f"got status={err.status} detail={err.detail!r}",
        )
    # A redirect with no detail still gets an explanatory default.
    redir = classify_status(302)
    check(
        "3xx without detail gets a default note",
        "redirect" in redir.detail,
        f"got {redir.detail!r}",
    )
    # It is a plain function, independent of any HTTPError object.
    check(
        "classify_status returns a ServiceError",
        isinstance(classify_status(500), ServiceError),
    )


def test_service_client_env_kwargs():
    print("\n== service_client_env_kwargs returns the shared ctor kwargs ==")
    import os

    from hyperdjango.serviceclient import service_client_env_kwargs

    prefix = "SC_ENVKW_TEST"
    keys = {
        f"{prefix}_URL": "https://svc.internal:9443",
        f"{prefix}_TOKEN": "env-token",
        f"{prefix}_CA_FILE": "/tmp/ca.crt",
        f"{prefix}_CLIENT_CERT": "/tmp/client.crt",
        f"{prefix}_CLIENT_KEY": "/tmp/client.key",
    }
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update(keys)
        kwargs = service_client_env_kwargs(prefix)
        check(
            "kwargs carry base_url + credential + mTLS identity",
            kwargs
            == {
                "base_url": "https://svc.internal:9443",
                "token": "env-token",
                "ca_file": "/tmp/ca.crt",
                "client_cert_file": "/tmp/client.crt",
                "client_key_file": "/tmp/client.key",
            },
            f"got {kwargs}",
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # An SDK subclass can splat the result straight into the constructor. Use a
    # URL+token-only prefix so no (nonexistent) mTLS file is loaded.
    prefix2 = "SC_ENVKW_SPLAT"
    keys2 = {f"{prefix2}_URL": "https://svc.internal:9443", f"{prefix2}_TOKEN": "tk"}
    saved2 = {k: os.environ.get(k) for k in keys2}
    try:
        os.environ.update(keys2)
        client = ServiceClient(**service_client_env_kwargs(prefix2), token_scheme="")
        check(
            "kwargs splat into a ServiceClient",
            client.base_url == "https://svc.internal:9443"
            and client._auth_headers() == {"Authorization": "tk"},
            f"got {client.base_url} {client._auth_headers()}",
        )
    finally:
        for k, v in saved2.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Unset prefix → empty strings, never a KeyError.
    empty = service_client_env_kwargs("SC_ENVKW_ABSENT")
    check(
        "absent env yields empty strings",
        set(empty.values()) == {""},
        f"got {empty}",
    )


# ── Three-tier hub fixture (ephemeral / catchup in-frame delivery) ───────────


class HubServer(_BaseServer):
    """A change-feed hub that speaks the subscribe/hello handshake and delivers
    events IN the frame (the ephemeral and catchup tiers).

    On each WS connect it reads the client's subscribe frame, replies with a
    hello declaring ``mode``, and (in catchup) replays the ring events the
    client missed before streaming live ones. It has no durable HTTP replay
    endpoint — a non-upgrade request is answered 404 and counted, so a test can
    prove the in-frame tiers never pull replay.
    """

    def __init__(self, mode: str, ring_floor: int = 0, epoch: str = "epoch-A"):
        self.mode = mode
        self._lock = threading.Lock()
        self.seq = 0
        self.ring_floor = ring_floor
        self.epoch = epoch
        self.ring: list[dict] = []
        self.ws_connects = 0
        self.http_requests = 0
        self.subscribes: list[dict] = []
        self._ws_lock = threading.Lock()
        self._ws_socks: list[socket.socket] = []
        super().__init__()

    def _next_event(self) -> dict:
        self.seq += 1
        return {
            "type": "event",
            "subject": f"s/{self.seq}",
            "kind": "changed",
            "seq": self.seq,
            "metadata": {},
        }

    def append_to_ring(self, n: int = 1) -> list[int]:
        """Add ``n`` events to the ring WITHOUT pushing to live sockets — events
        a reconnecting catchup client must replay (missed while disconnected)."""
        with self._lock:
            evs = [self._next_event() for _ in range(n)]
            self.ring.extend(evs)
        return [e["seq"] for e in evs]

    @property
    def live_sockets(self) -> int:
        """Sockets currently REGISTERED for broadcast.

        Distinct from ``ws_connects``, which counts accepted upgrades. A
        handler increments that counter and sends hello BEFORE appending the
        socket here, so a client can be fully connected — hello received,
        on_reset fired — while ``push_event`` still cannot reach it. Tests
        that push after a reconnect must wait on THIS, the state the push
        actually depends on, not on a proxy that fires earlier.
        """
        with self._ws_lock:
            return len(self._ws_socks)

    def push_event(self, n: int = 1) -> list[int]:
        """Emit ``n`` live events: ring them AND push to connected sockets."""
        with self._lock:
            evs = [self._next_event() for _ in range(n)]
            self.ring.extend(evs)
        with self._ws_lock:
            socks = list(self._ws_socks)
        for ev in evs:
            for s in socks:
                with contextlib.suppress(OSError):
                    _ws_send(s, ev)
        return [e["seq"] for e in evs]

    def set_floor(self, floor: int) -> None:
        with self._lock:
            self.ring_floor = floor

    def restart(self, new_epoch: str) -> None:
        """Simulate a process restart: a fresh incarnation mints a new epoch and
        resets the in-memory seq/ring to empty (floor 0). A client reconnecting
        with a stale last_seq from the previous incarnation must resync on the
        epoch change, not resume — even though the reset seq can burst back into
        the stale last_seq's numeric range."""
        with self._lock:
            self.epoch = new_epoch
            self.seq = 0
            self.ring = []
            self.ring_floor = 0

    def push_wake(self, cursor: int) -> None:
        """Push a ledger-style content-free wake hint to connected sockets."""
        with self._ws_lock:
            socks = list(self._ws_socks)
        for s in socks:
            with contextlib.suppress(OSError):
                _ws_send(s, {"type": "wake", "cursor": cursor})

    def drop_all(self) -> None:
        """Drop every live session — the hub-loss event the reconnect tests need.

        ``shutdown`` before ``close``, and for a reason that decides whether
        these tests measure a reconnect or a keepalive timeout. Each of these
        sockets has this hub's own handler thread parked in ``recv`` on it, and
        on Linux ``close()`` alone does not end such a connection: the in-flight
        syscall still holds the socket open, so no FIN is sent and the CLIENT
        never learns it was dropped. It then sits until its keepalive deadline
        expires — the drop under test never happens, and each reconnect
        assertion quietly pays several seconds to be satisfied by the wrong
        mechanism. (macOS tears the socket down on close, which is why this
        only ever showed up on the Linux runner.) ``shutdown(SHUT_RDWR)`` sends
        the FIN unconditionally, so the client observes the loss in under a
        millisecond, on every platform.
        """
        with self._ws_lock:
            socks = list(self._ws_socks)
            self._ws_socks.clear()
        for s in socks:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                s.close()

    def _handle(self, conn):
        parsed = self._read_head(conn)
        if parsed is None:
            conn.close()
            return
        _method, _path, headers = parsed
        if "websocket" in headers.get("upgrade", "").lower():
            self._handle_ws(conn, headers)
            return
        with self._lock:
            self.http_requests += 1
        conn.sendall(
            b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        conn.close()

    def _handle_ws(self, conn, headers):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept.encode()
        )
        with self._ws_lock:
            self.ws_connects += 1
        # Read the client's subscribe frame (masked, client→server).
        frame = _ws_read_frame(conn)
        if frame is None:
            with contextlib.suppress(OSError):
                conn.close()
            return
        opcode, payload = frame
        sub: dict = {}
        if opcode in (0x1, 0x2):
            with contextlib.suppress(ValueError):
                sub = json.loads(payload)
        with self._lock:
            self.subscribes.append(sub)
            last_seq = sub.get("last_seq")
            floor = self.ring_floor
            head = self.seq
            epoch = self.epoch
            ring = list(self.ring)
        if self.mode == "ledger":
            # A ledger hub advertises the durable model and thereafter speaks
            # content-free wake hints (never event frames). A ws-only watcher with
            # no replay endpoint cannot service them and must treat each as a
            # resync trigger.
            _ws_send(conn, {"type": "hello", "mode": "ledger", "cursor": head})
        elif self.mode == "ephemeral":
            _ws_send(
                conn,
                {
                    "type": "hello",
                    "mode": "ephemeral",
                    "seq": head,
                    "cursor": 0,
                    "epoch": epoch,
                    "resync": True,
                },
            )
        else:  # catchup
            resync = last_seq is None or last_seq < floor
            _ws_send(
                conn,
                {
                    "type": "hello",
                    "mode": "catchup",
                    "seq": head,
                    "cursor": 0,
                    "epoch": epoch,
                    "resync": resync,
                },
            )
            if not resync:
                # Replay exactly the events the client missed, in order.
                for ev in ring:
                    if ev["seq"] > last_seq:
                        _ws_send(conn, ev)
        if _WS_REGISTER_DELAY_S and self.ws_connects > 1:
            # Reconnects only: that is the window the guards protect, and
            # delaying every connection instead would slow the file to a crawl
            # without testing anything the first connect does not already cover.
            time.sleep(_WS_REGISTER_DELAY_S)  # see _WS_REGISTER_DELAY_S
        with self._ws_lock:
            self._ws_socks.append(conn)
        try:
            while not self._stop:
                f = _ws_read_frame(conn)
                if f is None:
                    break
                op, _p = f
                if op == 0x8:  # client close
                    break
                if op == 0x9:  # ping → pong
                    with contextlib.suppress(OSError):
                        conn.sendall(struct.pack("!BB", 0x8A, 0))
        except OSError:
            pass
        finally:
            with self._ws_lock, contextlib.suppress(ValueError):
                self._ws_socks.remove(conn)
            with contextlib.suppress(OSError):
                conn.close()


class _HubSink:
    """Sink for in-frame events, which carry ``seq`` (not the ledger ``id``)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seqs: list[int] = []
        self.resets = 0

    def on_event(self, ev):
        with self.lock:
            self.seqs.append(ev.get("seq"))

    def on_reset(self, resp):
        with self.lock:
            self.resets += 1

    def got(self):
        with self.lock:
            return list(self.seqs)


def test_watcher_ephemeral_mode():
    print("\n== watcher: ephemeral mode (on_reset + in-frame delivery) ==")
    hub = HubServer(mode="ephemeral")
    client = ServiceClient(hub.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _HubSink()
    watcher = ChangeFeedWatcher(
        client,
        ws_path="/ws/feed",  # no replay_path → pure in-frame delivery
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        poll_interval=30.0,
        base_backoff=0.02,
    ).start()
    try:
        check("connected", wait_for(lambda: hub.ws_connects >= 1))
        check(
            "on_reset fired on hello (full resync)",
            wait_for(lambda: sink.resets >= 1),
            f"resets={sink.resets}",
        )
        hub.push_event(3)  # seq 1,2,3 delivered in-frame
        check(
            "in-frame events delivered via on_event",
            # Live delivery crosses a background thread, so it gets the same
            # generous ceiling as the reconnect waits: the default 5s window is
            # ample on an idle box and expires spuriously on a loaded one.
            wait_for(lambda: sink.got() == [1, 2, 3], timeout=_RECONNECT_TIMEOUT),
            f"got {sink.got()}",
        )
        # Reconnect → on_reset again (the server kept no per-client state).
        resets_before = sink.resets
        hub.drop_all()
        check(
            "reconnected after drop",
            wait_for(lambda: hub.ws_connects >= 2, timeout=_RECONNECT_TIMEOUT),
            f"connects={hub.ws_connects}",
        )
        check(
            "on_reset fired again on reconnect",
            wait_for(lambda: sink.resets > resets_before, timeout=_RECONNECT_TIMEOUT),
            f"resets={sink.resets}",
        )
        # The hub registers a reconnected socket for broadcast AFTER
        # sending hello, so ws_connects/on_reset can both fire while a
        # push would still reach nobody. Wait on the registration.
        check(
            "hub ready to broadcast to the reconnected socket",
            wait_for(lambda: hub.live_sockets >= 1, timeout=_RECONNECT_TIMEOUT),
            f"live_sockets={hub.live_sockets}",
        )
        hub.push_event(1)  # seq 4
        check(
            "live delivery resumes after reconnect",
            wait_for(lambda: 4 in sink.got(), timeout=_RECONNECT_TIMEOUT),
            f"got {sink.got()}",
        )
        check(
            "no durable replay endpoint was ever hit",
            hub.http_requests == 0,
            f"http_requests={hub.http_requests}",
        )
        check(
            "no drain thread exists for a ws-only watcher",
            watcher._drain_thread is None,
        )
        check(
            "latest seq tracked for observability",
            wait_for(lambda: watcher.last_seq == 4, timeout=_RECONNECT_TIMEOUT),
            f"last_seq={watcher.last_seq}",
        )
    finally:
        watcher.stop()
        hub.close()


def test_watcher_catchup_mode():
    print("\n== watcher: catchup mode (resync, then replay only missed) ==")
    hub = HubServer(mode="catchup", ring_floor=0)
    client = ServiceClient(hub.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _HubSink()
    watcher = ChangeFeedWatcher(
        client,
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        client_id="stable-client-1",
        poll_interval=30.0,
        base_backoff=0.02,
    ).start()
    try:
        check("connected", wait_for(lambda: hub.ws_connects >= 1))
        # First connect carries no last_seq → the hub resyncs.
        check(
            "first connect resynced (no last_seq)",
            wait_for(lambda: sink.resets >= 1),
            f"resets={sink.resets}",
        )
        hub.push_event(3)  # seq 1,2,3 delivered live (K=3)
        check(
            "live events delivered in order",
            wait_for(lambda: sink.got() == [1, 2, 3]),
            f"got {sink.got()}",
        )
        check(
            "last_seq advanced to K",
            wait_for(lambda: watcher.last_seq == 3),
            f"last_seq={watcher.last_seq}",
        )
        # Events generated around the disconnect: ringed but never pushed, so a
        # reconnect must replay exactly them (and no more).
        hub.append_to_ring(2)  # seq 4,5 (missed)
        resets_before = sink.resets
        hub.drop_all()
        check(
            "reconnected after drop",
            wait_for(lambda: hub.ws_connects >= 2, timeout=_RECONNECT_TIMEOUT),
            f"connects={hub.ws_connects}",
        )
        check(
            "missed events K+1..N replayed in order, once",
            wait_for(lambda: sink.got() == [1, 2, 3, 4, 5]),
            f"got {sink.got()}",
        )
        check("no duplicate delivery", len(sink.got()) == len(set(sink.got())))
        check(
            "no resync on a within-ring reconnect",
            sink.resets == resets_before,
            f"resets={sink.resets}",
        )
        last_sub = hub.subscribes[-1]
        check(
            "reconnect carried the stable client_id",
            last_sub.get("client_id") == "stable-client-1",
            f"{last_sub}",
        )
        check(
            "reconnect carried the retained last_seq",
            last_sub.get("last_seq") == 3,
            f"{last_sub}",
        )
        # The hub registers a reconnected socket for broadcast AFTER
        # sending hello, so ws_connects/on_reset can both fire while a
        # push would still reach nobody. Wait on the registration.
        check(
            "hub ready to broadcast to the reconnected socket",
            wait_for(lambda: hub.live_sockets >= 1, timeout=_RECONNECT_TIMEOUT),
            f"live_sockets={hub.live_sockets}",
        )
        hub.push_event(1)  # seq 6
        check(
            "live delivery resumes after the replay",
            wait_for(lambda: sink.got() == [1, 2, 3, 4, 5, 6]),
            f"got {sink.got()}",
        )
    finally:
        watcher.stop()
        hub.close()


def test_watcher_catchup_ring_overrun():
    print("\n== watcher: catchup reconnect below the ring floor → resync ==")
    hub = HubServer(mode="catchup", ring_floor=0)
    client = ServiceClient(hub.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _HubSink()
    watcher = ChangeFeedWatcher(
        client,
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        client_id="stable-client-2",
        poll_interval=30.0,
        base_backoff=0.02,
    ).start()
    try:
        check("connected", wait_for(lambda: hub.ws_connects >= 1))
        check("first connect resynced", wait_for(lambda: sink.resets >= 1))
        hub.push_event(3)  # seq 1,2,3
        check(
            "live delivered",
            wait_for(lambda: sink.got() == [1, 2, 3]),
            f"got {sink.got()}",
        )
        check("last_seq advanced", wait_for(lambda: watcher.last_seq == 3))
        # The hub races ahead and evicts everything at/below the client's
        # last_seq: a reconnect now falls below the ring floor.
        hub.append_to_ring(5)  # seq 4..8 (about to be evicted)
        hub.set_floor(100)  # floor well above the client's last_seq=3
        resets_before = sink.resets
        got_before = set(sink.got())
        hub.drop_all()
        check(
            "reconnected after drop",
            wait_for(lambda: hub.ws_connects >= 2, timeout=_RECONNECT_TIMEOUT),
            f"connects={hub.ws_connects}",
        )
        check(
            "overrun (last_seq < floor) triggered a resync",
            wait_for(lambda: sink.resets > resets_before),
            f"resets={sink.resets}",
        )
        # No partial or duplicated delivery of the evicted range.
        # timing-window: a bounded NEGATIVE — the evicted seqs 4..8 must never
        # be delivered. An unwanted delivery would arrive on the ws thread at a
        # moment nothing announces, so there is no state to wait for; only an
        # observation window can show it did not happen. Oversleeping widens the
        # window and strengthens the claim.
        time.sleep(0.3)
        check(
            "no evicted events delivered (clean resync)",
            set(sink.got()) == got_before,
            f"got {sink.got()}",
        )
        # The hub registers a reconnected socket for broadcast AFTER
        # sending hello, so ws_connects/on_reset can both fire while a
        # push would still reach nobody. Wait on the registration.
        check(
            "hub ready to broadcast to the reconnected socket",
            wait_for(lambda: hub.live_sockets >= 1, timeout=_RECONNECT_TIMEOUT),
            f"live_sockets={hub.live_sockets}",
        )
        hub.push_event(1)  # seq 9
        check(
            "live delivery resumes after the resync",
            wait_for(lambda: 9 in sink.got()),
            f"got {sink.got()}",
        )
    finally:
        watcher.stop()
        hub.close()


def test_last_seq_advances_monotonically():
    print("\n== in-frame last_seq advances monotonically (out-of-order frames) ==")
    # Concurrent publishers assign seq in order under the hub lock but fan out
    # over independent channel sends, so a frame can arrive out of order (seq 2
    # before seq 1). Every frame is still delivered to on_event, but the RESUME
    # cursor (last_seq) must never regress — a regression would re-replay an
    # already-seen event on the next reconnect. Exercise _deliver_frame directly
    # (no sockets): deliver [2, 1, 3] and assert last_seq ends at the max.
    delivered: list[int] = []
    watcher = ChangeFeedWatcher(
        replay=lambda after: {"events": [], "cursor": after, "reset": False},
        on_event=lambda ev: delivered.append(ev["seq"]),
    )
    for seq in (2, 1, 3):
        watcher._deliver_frame(
            "ephemeral",
            {
                "type": "event",
                "subject": f"s/{seq}",
                "kind": "c",
                "seq": seq,
                "metadata": {},
            },
        )
    check(
        "every frame delivered to on_event (dedupe is the consumer's job)",
        delivered == [2, 1, 3],
        f"delivered={delivered}",
    )
    check(
        "last_seq ends at the max, never regressing to a late-arriving lower seq",
        watcher.last_seq == 3,
        f"last_seq={watcher.last_seq}",
    )


def test_watcher_catchup_epoch_restart_resyncs():
    print("\n== catchup: a hub restart (new epoch) forces resync, not misreplay ==")
    hub = HubServer(mode="catchup", ring_floor=0, epoch="epoch-A")
    client = ServiceClient(hub.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _HubSink()
    watcher = ChangeFeedWatcher(
        client,
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        client_id="stable-client-epoch",
        poll_interval=30.0,
        base_backoff=0.02,
    ).start()
    try:
        check("connected", wait_for(lambda: hub.ws_connects >= 1))
        check(
            "first connect resynced (no last_seq)", wait_for(lambda: sink.resets >= 1)
        )
        check(
            "first subscribe carried a null epoch",
            wait_for(lambda: hub.subscribes and hub.subscribes[0].get("epoch") is None),
            f"{hub.subscribes[:1]}",
        )
        hub.push_event(3)  # seq 1,2,3 live; watcher records last_seq=3, epoch-A
        check(
            "live events delivered; last_seq=3 under epoch-A",
            wait_for(lambda: sink.got() == [1, 2, 3] and watcher.last_seq == 3),
            f"got={sink.got()} last_seq={watcher.last_seq}",
        )
        # A new incarnation: fresh epoch, seq reset to 0, empty ring. The hub's
        # own resync check is FALSE here (last_seq=3 is not below floor 0), so
        # ONLY the watcher's epoch guard can catch the restart — proving it does
        # not rely on the server's resync flag.
        hub.restart("epoch-B")
        resets_before = sink.resets
        hub.drop_all()
        check(
            "reconnected after restart",
            wait_for(lambda: hub.ws_connects >= 2, timeout=_RECONNECT_TIMEOUT),
            f"connects={hub.ws_connects}",
        )
        check(
            "reconnect subscribe carried the OLD epoch it was resuming under",
            wait_for(lambda: hub.subscribes[-1].get("epoch") == "epoch-A"),
            f"{hub.subscribes[-1]}",
        )
        check(
            "epoch change forced a resync (hub said resync=false)",
            wait_for(lambda: sink.resets > resets_before),
            f"resets={sink.resets}",
        )
        # The stale last_seq (3) is discarded: the new incarnation's head is 0, so
        # events 1,2 published after the restart drive last_seq to 2, NOT pinned
        # at the dead incarnation's 3.
        # The hub registers a reconnected socket for broadcast AFTER
        # sending hello, so ws_connects/on_reset can both fire while a
        # push would still reach nobody. Wait on the registration.
        check(
            "hub ready to broadcast to the reconnected socket",
            wait_for(lambda: hub.live_sockets >= 1, timeout=_RECONNECT_TIMEOUT),
            f"live_sockets={hub.live_sockets}",
        )
        hub.push_event(2)  # seq 1,2 in the new incarnation
        check(
            "stale last_seq discarded; tracks the new incarnation's seq",
            wait_for(lambda: watcher.last_seq == 2),
            f"last_seq={watcher.last_seq}",
        )
    finally:
        watcher.stop()
        hub.close()


def test_watcher_ws_only_ledger_wake_resyncs():
    print("\n== ws-only watcher vs a ledger hub: every wake drives a resync ==")
    # A ws-only watcher (no replay_path) pointed at a ledger-mode hub falls back
    # to in-frame delivery, but a ledger hub speaks content-free wake hints, not
    # event frames. Without a replay endpoint the watcher cannot service a wake's
    # cursor, so it must treat each wake as a resync trigger (re-fetch all) rather
    # than silently ignore it and go stale until a reconnect.
    hub = HubServer(mode="ledger")
    client = ServiceClient(hub.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _HubSink()
    watcher = ChangeFeedWatcher(
        client,
        ws_path="/ws/feed",  # ws-only: no replay source
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        poll_interval=30.0,
        base_backoff=0.02,
    ).start()
    try:
        check("connected", wait_for(lambda: hub.ws_connects >= 1))
        # The ledger hello downgrades to ephemeral fallback → one resync on connect.
        check(
            "ledger hello resynced the ws-only fallback once",
            wait_for(lambda: sink.resets >= 1),
            f"resets={sink.resets}",
        )
        check(
            "no drain thread exists (ws-only watcher)",
            watcher._drain_thread is None,
        )
        # Each wake must drive one further resync (never silently dropped).
        for expected in (2, 3, 4):
            hub.push_wake(expected)
            check(
                f"wake #{expected - 1} drove a resync",
                wait_for(lambda e=expected: sink.resets >= e),
                f"resets={sink.resets}",
            )
        check(
            "no durable replay endpoint was ever pulled",
            hub.http_requests == 0,
            f"http_requests={hub.http_requests}",
        )
    finally:
        watcher.stop()
        hub.close()


def test_watcher_connection_state_observable():
    """The live feed's connection state is observable: a consumer serving cached
    state off the feed must be able to ask "is my feed up right now?" (health,
    degraded-mode) and to BLOCK on a transition instead of guessing a timeout."""
    print("\n== watcher: connection state observable (connect → loss → reconnect) ==")
    hub = HubServer(mode="ephemeral")
    client = ServiceClient(hub.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _HubSink()
    watcher = ChangeFeedWatcher(
        client,
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        poll_interval=30.0,
        base_backoff=0.02,
    ).start()
    try:
        check(
            "wait_connected returns once the feed is established",
            watcher.wait_connected(_RECONNECT_TIMEOUT),
        )
        check("connected reads True while the session is up", watcher.connected)
        check(
            "the connect is counted, no disconnect yet",
            watcher.connects >= 1 and watcher.disconnects == 0,
            f"connects={watcher.connects} disconnects={watcher.disconnects}",
        )
        connects_before = watcher.connects
        drops_before = watcher.disconnects

        # Hub loss with the listener still up: the watcher reconnects almost at
        # once, so the disconnected LEVEL is transient here — observe the
        # monotonic counters (edges), which cannot be missed. `connected` /
        # `wait_disconnected` are for a state that persists (a hub that is
        # really down), exercised by the stop() assertion below.
        hub.drop_all()
        check(
            "the hub loss is observed (a disconnect is counted)",
            wait_for(
                lambda: watcher.disconnects > drops_before, timeout=_RECONNECT_TIMEOUT
            ),
            f"disconnects={watcher.disconnects} before={drops_before}",
        )
        check(
            "the reconnect is observed as a further connect",
            wait_for(
                lambda: watcher.connects > connects_before, timeout=_RECONNECT_TIMEOUT
            ),
            f"connects={watcher.connects} before={connects_before}",
        )
    finally:
        watcher.stop()
        hub.close()
    # stop() joins the ws thread only up to its timeout, so the teardown is
    # itself asynchronous under load — wait for the transition rather than
    # assuming stop() already unwound it (the very mistake this API exists to
    # let callers avoid).
    check(
        "stop() ends with the watcher observably disconnected",
        watcher.wait_disconnected(_RECONNECT_TIMEOUT),
    )


def test_watcher_connected_flips_after_resync_applied():
    """``connected`` flips only AFTER the connect's resync has been handed to
    ``on_reset``. That ordering is the contract an owner relies on: seeing
    ``connected`` means the connect's invalidation has already happened, so no
    surprise reset from this same connect can land under a later read. Asserted
    from INSIDE the callback (state at reset time), so it is an ordering test,
    not a timing one."""
    print("\n== watcher: connected flips only after the connect resync ran ==")
    hub = HubServer(mode="ephemeral")
    client = ServiceClient(hub.base_url, timeout=2.0, ws_ping_interval=5.0)
    sink = _HubSink()
    holder: list[ChangeFeedWatcher] = []
    state_at_reset: list[bool] = []

    def on_reset(resp):
        state_at_reset.append(holder[0].connected)
        sink.on_reset(resp)

    watcher = ChangeFeedWatcher(
        client,
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=on_reset,
        poll_interval=30.0,
        base_backoff=0.02,
    )
    holder.append(watcher)
    watcher.start()
    try:
        check(
            "feed connected",
            watcher.wait_connected(_RECONNECT_TIMEOUT),
        )
        check(
            "the connect resync ran while still reported disconnected",
            state_at_reset and state_at_reset[0] is False,
            f"state_at_reset={state_at_reset}",
        )
        check(
            "the resync is applied by the time connected is observable",
            sink.resets >= 1,
            f"resets={sink.resets}",
        )
    finally:
        watcher.stop()
        hub.close()


def test_watcher_connection_state_ledger_and_poll_only():
    """Ledger intent: the session is established (and reported connected) as
    soon as the subscribe frame is away, with no hello required — replay is the
    delivery path. A poll-only watcher has no feed at all, so it never reports
    connected and ``wait_disconnected`` is immediately true."""
    print("\n== watcher: connection state for ledger and poll-only watchers ==")
    server = FeedServer(ledger_ids=range(1, 3), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0)
    sink = _Sink()
    watcher = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        ws_path="/ws/feed",
        on_event=sink.on_event,
        on_reset=sink.on_reset,
        poll_interval=0.2,
        base_backoff=0.02,
    ).start()
    try:
        check(
            "ledger watcher reports connected without any hello",
            watcher.wait_connected(_RECONNECT_TIMEOUT),
        )
        check(
            "events still delivered through replay",
            wait_for(lambda: sink.ids() == [1, 2]),
            f"got {sink.ids()}",
        )
    finally:
        watcher.stop()

    poll_only = ChangeFeedWatcher(
        client,
        replay_path="/v1/events",
        on_event=sink.on_event,
        poll_interval=0.2,
    ).start()
    try:
        check("poll-only watcher never reports connected", not poll_only.connected)
        check(
            "wait_disconnected is immediately true with no feed",
            poll_only.wait_disconnected(0.1),
        )
        check(
            "no connects are counted for a poll-only watcher",
            poll_only.connects == 0,
            f"connects={poll_only.connects}",
        )
    finally:
        poll_only.stop()
        server.close()


class StallingUpgradeServer(_BaseServer):
    """Accepts the TCP connection, reads the upgrade request, and then never
    answers it — the handshake window, held open on demand.

    This is the window a watcher's ``_active_ws`` cannot cover:
    ``open_websocket`` has not returned, so there is no connection object to
    interrupt. A stop arriving here must still reach the socket, or the ws
    thread sits in the handshake read until the connect timeout expires —
    outliving by many seconds the ``stop()`` that asked it to end.
    """

    def __init__(self):
        self.reached = threading.Event()
        super().__init__()

    def _handle(self, conn):
        with contextlib.suppress(OSError):
            self._read_head(conn)  # consume the upgrade request...
            self.reached.set()
            while not self._stop:  # ...and never answer it
                if not conn.recv(4096):
                    break
        with contextlib.suppress(OSError):
            conn.close()


def _timed_stop(watcher) -> tuple[float, str]:
    """``stop()`` the watcher; return (seconds, error message or "").

    A stop that cannot land now RAISES rather than returning quietly with a live
    thread, so the failure is captured as a counted check here instead of
    aborting the file.
    """
    started = time.monotonic()
    try:
        watcher.stop()
    except ServiceError as exc:
        return time.monotonic() - started, str(exc)
    return time.monotonic() - started, ""


def test_stop_is_prompt_and_leaves_no_thread():
    """``stop()`` means stopped: it returns at once AND both threads have exited.

    Returning while the ws thread still runs is not a slow shutdown, it is a
    broken one — the caller believes the watcher is gone while its callbacks can
    still fire and its socket is still held. The defect this guards was a race
    in the wakeup itself (a cross-thread ``close()`` that could destroy the fd
    before the parked reader's wakeup landed on it), lost roughly half the time,
    so the connected case is repeated: one round could pass on luck, twelve
    cannot.
    """
    print("\n== stop() returns promptly and leaves no live thread ==")
    rounds = 12
    server = FeedServer(ledger_ids=(1,), ws_mode="hold")
    client = ServiceClient(server.base_url, timeout=2.0)
    worst = 0.0
    survivors = 0
    errors: list[str] = []
    connected_all = True
    try:
        for _ in range(rounds):
            watcher = ChangeFeedWatcher(
                client,
                replay_path="/v1/events",
                ws_path="/ws/feed",
                on_event=lambda event: None,
                poll_interval=30.0,
                base_backoff=0.02,
            ).start()
            if not watcher.wait_connected(_RECONNECT_TIMEOUT):
                connected_all = False
                watcher.stop()
                break
            elapsed, error = _timed_stop(watcher)
            worst = max(worst, elapsed)
            if error:
                errors.append(error)
            if watcher._ws_thread.is_alive() or watcher._drain_thread.is_alive():
                survivors += 1
        check("every round reached a connected feed before stopping", connected_all)
        check(
            f"worst stop() of {rounds} idle-connected rounds was {worst:.2f}s (< 1s)",
            worst < 1.0,
            f"worst={worst:.2f}s — a stop that waits out a join is a leaked thread",
        )
        check(
            "no ws/drain thread survived any stop()",
            survivors == 0 and not errors,
            f"{survivors} round(s) left a thread running; errors={errors}",
        )
    finally:
        server.close()

    # The handshake window: no connection object exists yet, so only a socket
    # published before it blocks can make this interruptible. The connect
    # timeout is deliberately far longer than the stop budget, so a prompt
    # return can only mean the stop actually reached the socket.
    stalling = StallingUpgradeServer()
    try:
        stalled_client = ServiceClient(stalling.base_url, timeout=20.0)
        watcher = ChangeFeedWatcher(
            stalled_client,
            ws_path="/ws/feed",
            on_event=lambda event: None,
            base_backoff=0.02,
        ).start()
        check(
            "the watcher reached the stalled upgrade",
            stalling.reached.wait(_RECONNECT_TIMEOUT),
        )
        elapsed, error = _timed_stop(watcher)
        check(
            f"stop() mid-handshake returned in {elapsed:.2f}s (< 1s, timeout 20s)",
            elapsed < 1.0 and not error,
            f"elapsed={elapsed:.2f}s error={error}",
        )
        check(
            "no ws thread survives a stop() taken mid-handshake",
            not watcher._ws_thread.is_alive(),
        )
    finally:
        stalling.close()

    # Between reconnect attempts: nothing is listening, so the ws thread cycles
    # connect-failure → backoff wait. Stop must cut that short too.
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    reconnecting = ChangeFeedWatcher(
        ServiceClient(f"http://127.0.0.1:{dead_port}", timeout=2.0),
        ws_path="/ws/feed",
        on_event=lambda event: None,
        base_backoff=0.5,
        max_backoff=10.0,
    ).start()
    check(
        "the watcher entered its reconnect backoff",
        wait_for(lambda: reconnecting.reconnect_delay > 0, timeout=_RECONNECT_TIMEOUT),
    )
    elapsed, error = _timed_stop(reconnecting)
    check(
        f"stop() during reconnect backoff returned in {elapsed:.2f}s (< 1s)",
        elapsed < 1.0 and not error,
        f"elapsed={elapsed:.2f}s error={error}",
    )
    check(
        "no ws thread survives a stop() during reconnect backoff",
        not reconnecting._ws_thread.is_alive(),
    )

    # The primitive underneath: shutdown is the cross-thread wakeup and must
    # leave the fd for its owner to release; close is the owner's full teardown.
    live = FeedServer(ledger_ids=(), ws_mode="hold")
    try:
        conn = open_ws_retrying(ServiceClient(live.base_url, timeout=2.0), "/ws/feed")
        conn.shutdown()
        check(
            "shutdown() unblocks the reader without releasing the fd",
            conn._sock.fileno() != -1,
        )
        conn.close()
        check("close() releases the fd", conn._sock.fileno() == -1)
    finally:
        live.close()


def main() -> bool:
    print("hyperdjango.serviceclient unit tests")
    test_retry_only_idempotent()
    test_status_errors_not_retried()
    test_error_mapping()
    test_backoff_bounded()
    test_watcher_ordering_under_out_of_order_wakes()
    test_watcher_reset_jumps_cursor()
    test_watcher_dropped_wake_recovers_via_poll()
    test_watcher_flapping_backoff_grows()
    test_non_json_2xx_raises_response_error()
    test_drain_thread_survives_non_json_page()
    test_response_size_cap()
    test_redirect_not_followed_token_not_leaked()
    test_ws_handshake_crlf_rejected()
    test_ws_oversized_frame_rejected()
    test_build_ssl_context_client_cert_without_ca()
    test_idle_ws_survives_and_backoff_stable()
    test_request_raw_returns_status()
    test_wake_preserved_during_in_flight_drain()
    test_error_body_capped_and_typed()
    test_ws_path_requires_client()
    test_drain_unadvanced_full_page_breaks()
    test_ws_silent_peer_detected()
    test_wake_target_redrain()
    test_unreachable_wake_target_decays_to_poll_cadence()
    test_ws_handshake_failure_closes_socket()
    test_ws_scheme_guard()
    test_handshake_response_capped()
    test_retry_policy_rejects_negative_backoff()
    test_local_resource_transient_classification()
    test_ws_connect_waits_out_local_port_exhaustion()
    test_drain_survives_non_dict_page()
    test_callback_containment_and_counters()
    test_service_client_from_env()
    test_custom_field_names_and_extra_params()
    test_token_scheme_and_header_variants()
    test_truncated_2xx_body_retries_and_typed()
    test_non_json_wake_drains_without_reconnect()
    test_zero_length_fragment_wake_is_bounded()
    test_stuck_reset_fires_once()
    test_classify_status_taxonomy()
    test_service_client_env_kwargs()
    test_watcher_ephemeral_mode()
    test_watcher_catchup_mode()
    test_watcher_catchup_ring_overrun()
    test_last_seq_advances_monotonically()
    test_watcher_catchup_epoch_restart_resyncs()
    test_watcher_ws_only_ledger_wake_resyncs()
    test_watcher_connection_state_observable()
    test_watcher_connected_flips_after_resync_applied()
    test_watcher_connection_state_ledger_and_poll_only()
    test_ws_liveness_credits_local_stall()
    test_poll_only_watcher_reports_no_liveness_traffic()
    test_stop_is_prompt_and_leaves_no_thread()
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
