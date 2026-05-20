"""Shared event-loop pool (HYPER_WEBSOCKET_CONCURRENCY=shared) end-to-end tests.

# hyper-test: e2e

Validates the opt-in shared-loop server model against the websocket_chat
example (the realistic channel/room/DB-backed app), proving:

  1. Connection ceiling is lifted — far more than HYPER_THREAD_POOL_SIZE
     concurrent connections can be held (the default thread-per-connection
     model caps at the pool size).
  2. Cross-user pub/sub delivery is correct on a shared loop — a message
     from one user reaches another user subscribed to the same room, using
     the cooperative call_soon_threadsafe + asyncio.Queue bridge.

Isolation: uses a freshly created room per delivery check so results are
independent of rate-limit state from other sections. Runs the server with
HYPER_WEBSOCKET_CONCURRENCY=shared via AppRunner's env override.

Requires: chat database (created/seeded automatically here).
    createdb chat  (done below)
"""

import base64
import contextlib
import json
import os
import struct
import subprocess
import time
import urllib.parse

from e2e_helper import TEST_PORTS, AppRunner

from hyperdjango.testkit import Session, connect_with_retry, http_get

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")
        ERRORS.append(name)
    return condition


# --- Minimal RFC 6455 client (pure stdlib, self-contained) ---


def _connect_with_retry(host, port, timeout=5.0):
    """Open a TCP connection via the shared testkit retry policy.

    This test opens 40 connections in a burst; dropping one to a transient
    local-resource failure would silently understate the ceiling count. The
    retry policy and its deadline live in testkit.
    """
    return connect_with_retry(host, port, timeout=timeout)


class WSClient:
    WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host, port, path, cookies=None, timeout=5.0):
        self.sock = _connect_with_retry(host, port, timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        cookie_header = f"Cookie: {cookies}\r\n" if cookies else ""
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"{cookie_header}\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            response += chunk
        self.status_code = int(response.split(b"\r\n")[0].decode().split(" ")[1])
        self.connected = self.status_code == 101
        self._buffer = response.split(b"\r\n\r\n", 1)[1] if self.connected else b""
        if not self.connected:
            self.sock.close()

    def send_json(self, data):
        payload = json.dumps(data).encode()
        mask = os.urandom(4)
        frame = bytearray([0x81])
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack("!Q", length))
        frame.extend(mask)
        frame.extend(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def recv_json(self, timeout=5.0):
        old = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            while True:
                header = self._read(2)
                opcode = header[0] & 0x0F
                length = header[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._read(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._read(8))[0]
                if header[1] & 0x80:
                    mask = self._read(4)
                    payload = bytearray(self._read(length))
                    for i in range(len(payload)):
                        payload[i] ^= mask[i % 4]
                else:
                    payload = self._read(length)
                if opcode == 1:
                    return json.loads(payload)
                if opcode == 8:
                    self.connected = False
                    return None
                if opcode == 9:
                    self._pong(payload)
        except TimeoutError, ConnectionError:
            return None
        finally:
            self.sock.settimeout(old)

    def _read(self, n):
        while len(self._buffer) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self._buffer += chunk
        result, self._buffer = self._buffer[:n], self._buffer[n:]
        return result

    def _pong(self, payload):
        mask = os.urandom(4)
        frame = bytearray([0x8A, 0x80 | len(payload)])
        frame.extend(mask)
        frame.extend(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def close(self):
        with contextlib.suppress(OSError):
            self.sock.close()
        self.connected = False


def drain_until(ws, msg_type, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = ws.recv_json(timeout=max(0.1, deadline - time.time()))
        if msg is None:
            continue
        if msg.get("type") == msg_type:
            return msg
    return None


def _register(base, username):
    s = Session(base)
    s.get("/register")
    s.post(
        "/register",
        urllib.parse.urlencode(
            {
                "username": username,
                "password": "testpass123",
                "_csrf_token": s.cookie_jar.get("csrftoken", ""),
            }
        ),
        content_type="application/x-www-form-urlencoded",
    )
    return "; ".join(f"{k}={v}" for k, v in s.cookie_jar.items()), s


def main():
    print("=" * 60)
    print("WebSocket Shared Event-Loop Pool Tests")
    print("=" * 60)

    subprocess.run(["createdb", "chat"], capture_output=True)
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.websocket_chat.app:app",
            "--drop",
            "--seed",
            "services.websocket_chat.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    HOST = "127.0.0.1"
    PORT = TEST_PORTS["websocket_shared_loops"]
    POOL_LOOPS = 4

    runner = AppRunner(
        "services.websocket_chat.app:app",
        host=HOST,
        port=PORT,
        readiness_path="/health",
        env={
            "HYPER_WEBSOCKET_CONCURRENCY": "shared",
            "HYPER_WEBSOCKET_LOOP_COUNT": str(POOL_LOOPS),
            "HYPER_THREAD_POOL_SIZE": "8",
        },  # small pool: proves conns exceed it
    )
    runner.start()
    try:
        base = runner.url()
        ts = str(int(time.time()))
        cookie1, s1 = _register(base, f"sharedu1_{ts}")
        cookie2, _s2 = _register(base, f"sharedu2_{ts}")

        # ── Test 1: connection ceiling lifted ────────────────────────
        # Thread pool is 8; open 40 concurrent connections to a seeded room.
        # The default model would cap at 8; shared loops should hold all 40.
        print("\n--- Connection ceiling (pool=8, open 40 concurrent) ---")
        conns = []
        for _ in range(40):
            try:
                c = WSClient(
                    HOST, PORT, "/ws/chat?room_id=1", cookies=cookie1, timeout=8.0
                )
                if c.connected:
                    conns.append(c)
            except Exception:
                pass
        ok(
            f"40/40 concurrent connections held (got {len(conns)}) — exceeds pool=8",
            len(conns) == 40,
        )
        for c in conns:
            c.close()
        time.sleep(0.5)

        # ── Test 2: cross-user delivery on shared loop (fresh room) ───
        print("\n--- Cross-user delivery on shared loop (fresh room) ---")
        room_name = f"shared_deliver_{ts}"
        s1.post(
            "/rooms/create",
            urllib.parse.urlencode(
                {
                    "name": room_name,
                    "description": "shared-loop delivery test",
                    "_csrf_token": s1.cookie_jar.get("csrftoken", ""),
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        rooms = json.loads(http_get(f"{base}/api/rooms/").body)
        room_id = next((r["id"] for r in rooms if r["name"] == room_name), None)
        ok("Fresh room created", room_id is not None)

        if room_id is not None:
            ws1 = WSClient(HOST, PORT, f"/ws/chat?room_id={room_id}", cookies=cookie1)
            ws2 = WSClient(HOST, PORT, f"/ws/chat?room_id={room_id}", cookies=cookie2)
            ok("Both users connected", ws1.connected and ws2.connected)
            if ws1.connected and ws2.connected:
                p1 = drain_until(ws1, "presence", timeout=2.0)
                p2 = drain_until(ws2, "presence", timeout=2.0)
                print(
                    f"    [diag] ws1 presence={p1 is not None} ws2 presence={p2 is not None}"
                )
                time.sleep(0.5)  # settle: ensure both handlers past subscribe()
                marker = f"shared-hello-{ts}"
                ws1.send_json({"type": "message", "content": marker})
                # ws1 is also subscribed → should receive its own broadcast.
                self_echo = drain_until(ws1, "message", timeout=4.0)
                got = drain_until(ws2, "message", timeout=4.0)
                print(
                    f"    [diag] ws1 self-echo={self_echo.get('content') if self_echo else None}"
                )
                print(f"    [diag] ws2 received={got.get('content') if got else None}")
                ok(
                    "Sender receives own room broadcast (shared loop)",
                    self_echo is not None and self_echo.get("content") == marker,
                )
                ok(
                    "Cross-user message delivered on shared loop",
                    got is not None and got.get("content") == marker,
                )
            ws1.close()
            ws2.close()
    finally:
        if FAIL:
            print("\n--- server stderr (tail) ---")
            for line in runner._stderr_lines[-40:]:
                print("   ", line.rstrip())
        runner.stop()

    print(f"\nResults: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
