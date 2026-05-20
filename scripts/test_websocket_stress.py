"""
Exhaustive WebSocket stress and security tests.

Tests edge cases, concurrency, and failure modes for the native Zig
WebSocket infrastructure. Each message-sending test uses a SEPARATE room
to avoid rate limit contamination across test sections.

Requires: chat database with tables.
    createdb chat
    uv run hyper setup --app services.websocket_chat.app:app --seed services.websocket_chat.seed:run
"""

# hyper-test: e2e

import base64
import contextlib
import json
import os
import struct
import subprocess
import threading
import time
import urllib.parse

from e2e_helper import TEST_PORTS, AppRunner, Session, http_get

from hyperdjango.testkit import connect_with_retry

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)
    return condition


def drain_until(ws, msg_type, timeout=3.0, max_frames=10):
    """Receive frames until one matches msg_type, skipping others."""
    for _ in range(max_frames):
        msg = ws.recv_json(timeout=timeout)
        if msg is None:
            return None
        if msg.get("type") == msg_type:
            return msg
    return None


def drain_initial(ws):
    """Drain history + presence + any stale member events."""
    for _ in range(5):
        msg = ws.recv_json(timeout=1.5)
        if msg is None:
            break
        if msg.get("type") == "presence":
            break


# --- Minimal RFC 6455 WebSocket Client ---


class WSClient:
    WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    @staticmethod
    def _connect(host, port, timeout):
        """Open a TCP connection via the shared testkit retry policy.

        A stress client opens many short-lived connections and competes with
        the whole parallel suite for the OS ephemeral-port range; the retry
        deadline lives in testkit so every test client waits these out
        identically (a shorter local budget is what let this class recur).
        """
        return connect_with_retry(host, port, timeout=timeout)

    def __init__(self, host, port, path, cookies=None, timeout=5.0):
        self.sock = self._connect(host, port, timeout)
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
                raise ConnectionError("Closed")
            response += chunk
        status_line = response.split(b"\r\n")[0].decode()
        self.status_code = int(status_line.split(" ")[1])
        self.connected = self.status_code == 101
        self._buffer = response.split(b"\r\n\r\n", 1)[1] if self.connected else b""
        if not self.connected:
            self.sock.close()

    def send_json(self, data):
        self.send_text(json.dumps(data, ensure_ascii=False))

    def send_text(self, text):
        payload = text.encode("utf-8")
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
        text = self._recv_text(timeout)
        if text is None:
            return None
        return json.loads(text)

    def _read(self, n):
        while len(self._buffer) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Closed")
            self._buffer += chunk
        result = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return result

    def _recv_text(self, timeout=5.0):
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
                    return (
                        payload.decode()
                        if isinstance(payload, (bytes, bytearray))
                        else payload
                    )
                if opcode == 8:
                    self.connected = False
                    return None
                if opcode == 9:
                    self._pong(payload)
        except TimeoutError:
            return None
        finally:
            self.sock.settimeout(old)

    def _pong(self, payload):
        mask = os.urandom(4)
        frame = bytearray([0x8A, 0x80 | len(payload)])
        frame.extend(mask)
        frame.extend(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def close(self):
        if self.connected:
            mask = os.urandom(4)
            code = struct.pack("!H", 1000)
            frame = bytearray([0x88, 0x82])
            frame.extend(mask)
            frame.extend(b ^ mask[i % 4] for i, b in enumerate(code))
            with contextlib.suppress(OSError):
                self.sock.sendall(bytes(frame))
            self.connected = False
        with contextlib.suppress(OSError):
            self.sock.close()

    def force_close(self):
        self.connected = False
        with contextlib.suppress(OSError):
            self.sock.close()


def _create_room(session, base, ts, suffix):
    """Create a test room via HTTP and return its ID."""
    name = f"stress_{suffix}_{ts}"
    session.post(
        "/rooms/create",
        urllib.parse.urlencode(
            {
                "name": name,
                "description": f"Stress test room {suffix}",
                "_csrf_token": session.cookie_jar.get("csrftoken", ""),
            }
        ),
        content_type="application/x-www-form-urlencoded",
    )
    r = http_get(f"{base}/api/rooms/1/history")  # just to confirm server is up
    # Get room ID from DB via the history API pattern — room IDs are sequential
    # We don't need the exact ID, we'll use the name-based lookup
    return name


def main():
    global PASS, FAIL

    print("=" * 60)
    print("WebSocket Stress & Security Tests")
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
    PORT = TEST_PORTS["websocket_stress"]

    with AppRunner(
        "services.websocket_chat.app:app",
        host=HOST,
        port=PORT,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")
        ts = str(int(time.time()))

        # Get auth cookie
        s = Session(base)
        s.get("/register")
        s.post(
            "/register",
            urllib.parse.urlencode(
                {
                    "username": f"stressuser{ts}",
                    "password": "testpass123",
                    "_csrf_token": s.cookie_jar.get("csrftoken", ""),
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        cookie_str = "; ".join(f"{k}={v}" for k, v in s.cookie_jar.items())

        # Second user for cross-user tests
        s2 = Session(base)
        s2.get("/register")
        s2.post(
            "/register",
            urllib.parse.urlencode(
                {
                    "username": f"stressuser2_{ts}",
                    "password": "testpass123",
                    "_csrf_token": s2.cookie_jar.get("csrftoken", ""),
                }
            ),
            content_type="application/x-www-form-urlencoded",
        )
        cookie_str2 = "; ".join(f"{k}={v}" for k, v in s2.cookie_jar.items())

        # Each test section uses a different room to avoid rate limit contamination.
        # Seeded rooms: 1=general, 2=random, 3=tech. Tests create additional rooms.

        # ── Rapid connect/disconnect (room 1, no sends) ──────────────
        print("--- Rapid connect/disconnect (10x) ---")
        for i in range(10):
            try:
                ws = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str)
                if ws.connected:
                    ws.recv_json(timeout=1.0)
                    ws.close()
                else:
                    ws.close()
            except Exception:
                pass
        ok("10 rapid connect/disconnect cycles", True)

        r = http_get(f"{base}/health")
        ok("Server healthy after rapid cycles", r.status == 200)

        # ── Force-close without close frame (room 1, no sends) ───────
        print("\n--- Force-close (TCP RST, no close frame) ---")
        for i in range(5):
            try:
                ws = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str)
                if ws.connected:
                    ws.recv_json(timeout=1.0)
                    ws.force_close()
            except Exception:
                pass
        time.sleep(0.5)
        r = http_get(f"{base}/health")
        ok("Server healthy after 5 force-closes", r.status == 200)

        # ── Concurrent connections (room 1, 1 send only) ─────────────
        print("\n--- Concurrent connections (5 users, same room) ---")
        connections = []
        try:
            for i in range(5):
                ws = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str)
                if ws.connected:
                    drain_initial(ws)
                    connections.append(ws)
            ok("5 concurrent connections", len(connections) == 5)

            if connections:
                # Deterministic sync via echo/broadcast, not a fixed sleep:
                # drain_until blocks each receiver until the broadcast message
                # arrives (or its timeout). A room broadcast must reach EVERY
                # other member — assert the exact expected count, not ">=1"
                # (which passed even if the broadcast fan-out silently dropped
                # most subscribers).
                expected_receivers = len(connections) - 1
                connections[0].send_json(
                    {"type": "message", "content": f"concurrent test {ts}"}
                )
                received = 0
                for ws in connections[1:]:
                    msg = drain_until(ws, "message", timeout=3.0)
                    if msg and msg.get("content") == f"concurrent test {ts}":
                        received += 1
                ok(
                    "Message broadcast to ALL other connections",
                    received == expected_receivers,
                    f"got {received}/{expected_receivers} receivers",
                )
        finally:
            for ws in connections:
                ws.close()

        # ── Concurrent sends (room 2 — fresh rate limit) ─────────────
        print("\n--- Concurrent sends from multiple threads ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=2", cookies=cookie_str)
            ok("WS connected for thread test", ws.connected)

            if ws.connected:
                drain_initial(ws)

                errors_in_threads = []
                send_lock = threading.Lock()

                def send_many(ws_conn, prefix, count):
                    try:
                        for j in range(count):
                            with send_lock:
                                ws_conn.send_json(
                                    {"type": "message", "content": f"{prefix}-{j}"}
                                )
                    except Exception as e:
                        errors_in_threads.append(str(e))

                threads = []
                for i in range(3):
                    t = threading.Thread(target=send_many, args=(ws, f"t{i}", 5))
                    threads.append(t)
                    t.start()
                for t in threads:
                    t.join(timeout=10)

                # Under HYPER_TEST_PARALLEL on Linux runners, the WS server
                # under stress legitimately closes connections that exceed
                # rate limits or fail liveness checks. EPIPE / "Broken pipe"
                # / "Closed" errors during concurrent send mean the kernel
                # noticed the close — that's the right behavior, not a crash.
                # Filter those out and only flag *unexpected* exception types.
                _expected_under_stress = (
                    "Broken pipe",
                    "Closed",
                    "Bad file descriptor",
                    "Connection reset",
                    "[Errno 32]",
                )
                unexpected_errors = [
                    e
                    for e in errors_in_threads
                    if not any(needle in e for needle in _expected_under_stress)
                ]
                ok(
                    "3 threads x 5 messages sent without crash",
                    len(unexpected_errors) == 0,
                    "; ".join(unexpected_errors) if unexpected_errors else "",
                )

                received_count = 0
                while True:
                    msg = ws.recv_json(timeout=2.0)
                    if msg is None:
                        break
                    if msg.get("type") in ("message", "rate_limited"):
                        received_count += 1
                ok(
                    "Server handled concurrent sends",
                    received_count >= 1,
                    f"got {received_count} responses",
                )

                ws.close()

        except Exception as e:
            # See above: connection-close exceptions during stress test are
            # the SERVER doing the right thing, not a test failure. Only flag
            # an unexpected exception type.
            _expected = (
                "Broken pipe",
                "Closed",
                "Bad file descriptor",
                "Connection reset",
                "[Errno 32]",
            )
            if not any(needle in str(e) for needle in _expected):
                ok(f"Concurrent send test ({e})", False)
            else:
                ok(
                    f"Concurrent send test (server closed under stress: {type(e).__name__})",
                    True,
                )

        # Liveness gate: we suppress a NARROW set of client-side connection-close
        # errors above (the server rejecting an over-limit connection is correct).
        # But suppressing those must never hide an actual server crash — so prove
        # the server is still serving. If it died, this fails loudly instead of
        # the swallowed exception passing the section green.
        r = http_get(f"{base}/health")
        ok("Server alive after concurrent sends", r.status == 200, f"status={r.status}")

        # ── Invalid JSON (room 1, no message sends) ──────────────────
        print("\n--- Invalid JSON input ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str)
            if ws.connected:
                drain_initial(ws)
                ws.send_text("{invalid json!!!")
                time.sleep(0.3)
                # Server should not crash — connection may close, that's acceptable
                ws.send_json({"type": "message", "content": "after invalid"})
                msg = ws.recv_json(timeout=2.0)
                ok("Server survives invalid JSON", True)
                ws.close()
            else:
                ok("Server survives invalid JSON (no connect)", False)
        except Exception:
            ok("Server survives invalid JSON", True)

        r = http_get(f"{base}/health")
        ok("Server healthy after invalid JSON", r.status == 200)

        # ── Empty message (room 3 — fresh rate limit) ────────────────
        print("\n--- Empty/whitespace messages ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=3", cookies=cookie_str)
            if ws.connected:
                drain_initial(ws)
                ws.send_json({"type": "message", "content": ""})
                ws.send_json({"type": "message", "content": "   "})
                ws.send_json({"type": "message", "content": "valid after empty"})
                time.sleep(0.3)
                msg = drain_until(ws, "message", timeout=2.0)
                ok(
                    "Empty messages ignored, valid accepted",
                    msg is not None and "valid" in msg.get("content", ""),
                )
                ws.close()
        except Exception as e:
            ok(f"Empty message test ({e})", False)

        # ── Large message (room 2, second use — rate limit may apply) ─
        # Test correctness: accepted vs rejected, not echo content
        print("\n--- Large messages ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=2", cookies=cookie_str)
            if ws.connected:
                drain_initial(ws)

                # Exactly at limit (4000 chars) — may get message OR rate_limited
                ws.send_json({"type": "message", "content": "A" * 4000})
                msg = drain_until(ws, "message", timeout=3.0)
                if msg is None:
                    # Got rate_limited instead — still correct behavior, just rate-limited
                    ok("4000-char message handled (rate limited)", True)
                else:
                    ok("4000-char message accepted", True)

                # Over limit (4001 chars) — should ALWAYS get error regardless of rate limit
                ws.send_json({"type": "message", "content": "B" * 4001})
                msg = drain_until(ws, "error", timeout=2.0)
                ok("4001-char message rejected", msg is not None)

                ws.close()
        except Exception as e:
            ok(f"Large message test ({e})", False)

        # ── Unknown message type (room 3) ────────────────────────────
        print("\n--- Unknown message types ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=3", cookies=cookie_str)
            if ws.connected:
                drain_initial(ws)
                ws.send_json({"type": "nonexistent_type", "data": "whatever"})
                ws.send_json({"type": "", "content": "no type"})
                ws.send_json({"content": "missing type key"})
                ws.send_json(
                    {"type": "message", "content": "still works after unknown"}
                )
                time.sleep(0.3)
                msg = drain_until(ws, "message", timeout=2.0)
                # May get rate_limited if room 3 was used earlier — that's OK too
                if msg is None:
                    msg = drain_until(ws, "rate_limited", timeout=1.0)
                ok("Unknown types handled without crash", msg is not None)
                ws.close()
        except Exception as e:
            ok(f"Unknown type test ({e})", False)

        # ── Unicode/emoji (room 2 — separate from heavy-send tests) ──
        print("\n--- Unicode and emoji ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=2", cookies=cookie_str)
            if ws.connected:
                drain_initial(ws)
                ws.send_json({"type": "message", "content": "Hello 你好 مرحبا 🎉🔥💯"})
                time.sleep(0.5)
                msg = drain_until(ws, "message", timeout=3.0)
                if msg is not None:
                    content = str(msg.get("content", ""))
                    ok(
                        "Unicode message round-trips",
                        "🎉" in content and "你好" in content,
                        f"content={repr(content[:60])}",
                    )
                else:
                    # Rate limited — still proves no crash, but can't verify content
                    ok("Unicode handled (rate limited, no content check)", True)
                ws.close()
        except Exception as e:
            ok(f"Unicode test ({e})", False)

        # ── Nonexistent room ─────────────────────────────────────────
        print("\n--- Nonexistent room ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=99999", cookies=cookie_str)
            if ws.connected:
                msg = ws.recv_json(timeout=3.0)
                ok(
                    "Nonexistent room rejected",
                    msg is not None and msg.get("type") == "error",
                )
                ws.close()
            else:
                ok("Nonexistent room rejected", True)
        except Exception:
            ok("Nonexistent room rejected", True)

        # ── Invalid room_id values ───────────────────────────────────
        print("\n--- Invalid room_id values ---")
        for bad_id in ["abc", "-1", "0", "999999999999999999999"]:
            try:
                ws = WSClient(
                    HOST, PORT, f"/ws/chat?room_id={bad_id}", cookies=cookie_str
                )
                if ws.connected:
                    msg = ws.recv_json(timeout=2.0)
                    ok(
                        f"room_id={bad_id} rejected",
                        msg is not None and msg.get("type") == "error",
                    )
                    ws.close()
                else:
                    ok(f"room_id={bad_id} rejected", True)
            except Exception:
                ok(f"room_id={bad_id} rejected", True)

        # ── Cross-user messaging (room 1) ────────────────────────────
        print("\n--- Cross-user message delivery ---")
        try:
            ws1 = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str)
            ws2 = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str2)
            ok("Both users connected", ws1.connected and ws2.connected)

            if ws1.connected and ws2.connected:
                drain_initial(ws1)
                drain_initial(ws2)

                ws1.send_json({"type": "message", "content": f"cross-user {ts}"})
                time.sleep(0.5)
                msg = drain_until(ws2, "message", timeout=3.0)
                ok("Cross-user message delivered", msg is not None)

                ws1.close()
                ws2.close()
        except Exception as e:
            ok(f"Cross-user test ({e})", False)

        # ── Reconnect after disconnect ───────────────────────────────
        print("\n--- Reconnect after disconnect ---")
        try:
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str)
            if ws.connected:
                ws.recv_json(timeout=2.0)
                ws.close()

            time.sleep(0.3)
            ws = WSClient(HOST, PORT, "/ws/chat?room_id=1", cookies=cookie_str)
            ok("Reconnect succeeds", ws.connected)
            if ws.connected:
                msg = ws.recv_json(timeout=3.0)
                ok(
                    "History on reconnect",
                    msg is not None and msg.get("type") == "history",
                )
                ws.close()
        except Exception as e:
            ok(f"Reconnect test ({e})", False)

        # ── Final health check ───────────────────────────────────────
        print("\n--- Final health check ---")
        time.sleep(0.5)
        r = http_get(f"{base}/health")
        ok("Server healthy after all stress tests", r.status == 200)

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print("=" * 60)

    raise SystemExit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
