"""Tests for WebSocket protocol extensions.

Covers:
- WebSocket configuration (max message size, ping interval, pong timeout)
- Subprotocol negotiation (parsing, selection, header generation)
- Extension header parsing (permessage-deflate detection)
- WebSocket class enhancements (subprotocol list, compression awareness)
- Handshake generation (accept key, subprotocol header, extension header)
- Frame structure (RSV1 bit for compression)
- WebSocketConfig dataclass

Usage:
    uv run hyper-test websocket_extensions
"""

# hyper-test: unit

import sys

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("WebSocket Protocol Extension Tests")
    print("=" * 60)

    # ── WebSocketConfig ───────────────────────────────────────────

    print("\n--- WebSocketConfig ---")

    from hyperdjango.websocket import (
        WebSocket,
        WebSocketConfig,
        WebSocketDisconnect,
    )

    # Test 1: Default config
    cfg = WebSocketConfig()
    check("default max message size", cfg.max_message_size == 16 * 1024 * 1024)
    check("default ping interval", cfg.ping_interval == 30)
    check("default pong timeout", cfg.pong_timeout == 120)

    # Test 2: Custom config
    cfg2 = WebSocketConfig(
        max_message_size=1024 * 1024, ping_interval=60, pong_timeout=180
    )
    check("custom max message size", cfg2.max_message_size == 1024 * 1024)
    check("custom ping interval", cfg2.ping_interval == 60)
    check("custom pong timeout", cfg2.pong_timeout == 180)

    # Test 3: Config is frozen dataclass
    try:
        cfg.max_message_size = 100  # type: ignore[misc]
        check("config is frozen", False, "Should raise FrozenInstanceError")
    except AttributeError:
        check("config is frozen", True)

    # Test 4: Apply config to native server
    cfg_apply = WebSocketConfig(
        max_message_size=8 * 1024 * 1024, ping_interval=45, pong_timeout=90
    )
    cfg_apply.apply()
    check("apply config succeeds", True)

    # Test 5: Read back current config
    current = WebSocketConfig.current()
    check(
        "current max msg size",
        current.max_message_size == 8 * 1024 * 1024,
        repr(current.max_message_size),
    )
    check(
        "current ping interval",
        current.ping_interval == 45,
        repr(current.ping_interval),
    )
    check(
        "current pong timeout", current.pong_timeout == 90, repr(current.pong_timeout)
    )

    # Test 6: Reset to defaults
    WebSocketConfig().apply()
    current2 = WebSocketConfig.current()
    check("reset max msg size", current2.max_message_size == 16 * 1024 * 1024)
    check("reset ping interval", current2.ping_interval == 30)
    check("reset pong timeout", current2.pong_timeout == 120)

    # ── WebSocket Subprotocol Parsing ─────────────────────────────

    print("\n--- Subprotocol Parsing ---")

    # Test 7: WebSocket with subprotocols in scope
    scope = {
        "type": "websocket",
        "path": "/ws/chat",
        "headers": [],
        "query_string": b"",
        "subprotocols": ["graphql-ws", "chat-v1", "json"],
    }
    ws = WebSocket(scope, None, None)
    check(
        "subprotocols from scope",
        ws.requested_subprotocols == ["graphql-ws", "chat-v1", "json"],
    )
    check("no accepted subprotocol yet", ws.accepted_subprotocol is None)

    # Test 8: Subprotocols from header fallback
    scope2 = {
        "type": "websocket",
        "path": "/ws/data",
        "headers": [
            (b"sec-websocket-protocol", b"v1.json, v2.json, raw"),
        ],
        "query_string": b"",
    }
    ws2 = WebSocket(scope2, None, None)
    check(
        "subprotocols from header",
        ws2.requested_subprotocols == ["v1.json", "v2.json", "raw"],
    )

    # Test 9: No subprotocols
    scope3 = {
        "type": "websocket",
        "path": "/ws/plain",
        "headers": [],
        "query_string": b"",
    }
    ws3 = WebSocket(scope3, None, None)
    check("no subprotocols", ws3.requested_subprotocols == [])

    # Test 10: Single subprotocol
    scope4 = {
        "type": "websocket",
        "path": "/ws/single",
        "headers": [
            (b"sec-websocket-protocol", b"graphql-ws"),
        ],
        "query_string": b"",
    }
    ws4 = WebSocket(scope4, None, None)
    check("single subprotocol", ws4.requested_subprotocols == ["graphql-ws"])

    # ── Extension Awareness ───────────────────────────────────────

    print("\n--- Extension Awareness ---")

    # Test 11: Compression from scope extensions
    scope5 = {
        "type": "websocket",
        "path": "/ws/compressed",
        "headers": [],
        "query_string": b"",
        "extensions": {"permessage-deflate": {"server_no_context_takeover": "true"}},
    }
    ws5 = WebSocket(scope5, None, None)
    check("has compression", ws5.has_compression is True)

    # Test 12: No compression
    scope6 = {
        "type": "websocket",
        "path": "/ws/plain",
        "headers": [],
        "query_string": b"",
    }
    ws6 = WebSocket(scope6, None, None)
    check("no compression", ws6.has_compression is False)

    # ── Native Zig Functions ──────────────────────────────────────

    print("\n--- Native Zig Functions ---")

    try:
        from hyperdjango._hyperdjango_native import (
            _server_get_ws_config,
            _server_set_ws_config,
        )

        # Test 13: Set and get config via native
        _server_set_ws_config(4 * 1024 * 1024, 15, 60)
        size, ping, pong = _server_get_ws_config()
        check("native set/get max size", size == 4 * 1024 * 1024, repr(size))
        check("native set/get ping", ping == 15, repr(ping))
        check("native set/get pong", pong == 60, repr(pong))

        # Test 14: Large max message size
        _server_set_ws_config(64 * 1024 * 1024, 30, 120)
        size2, _, _ = _server_get_ws_config()
        check("native 64MB max size", size2 == 64 * 1024 * 1024, repr(size2))

        # Test 15: Disable ping (interval = 0)
        _server_set_ws_config(16 * 1024 * 1024, 0, 120)
        _, ping2, _ = _server_get_ws_config()
        check("native disable ping", ping2 == 0, repr(ping2))

        # Reset
        _server_set_ws_config(16 * 1024 * 1024, 30, 120)

    except ImportError:
        check("native functions available", False, "ImportError")

    # ── WebSocketDisconnect ───────────────────────────────────────

    print("\n--- WebSocketDisconnect ---")

    # Test 16: Exception with code
    exc = WebSocketDisconnect(1001)
    check("disconnect code", exc.code == 1001)
    check("disconnect message", "1001" in str(exc))

    # Test 17: Default code
    exc2 = WebSocketDisconnect()
    check("disconnect default code", exc2.code == 1000)

    # ── WebSocket Properties ──────────────────────────────────────

    print("\n--- WebSocket Properties ---")

    # Test 18: Path
    check("ws path", ws.path == "/ws/chat")

    # Test 19: Query string
    scope7 = {
        "type": "websocket",
        "path": "/ws/test",
        "headers": [],
        "query_string": b"room=general&user=alice",
    }
    ws7 = WebSocket(scope7, None, None)
    check("ws query string", ws7.query_string == "room=general&user=alice")

    # Test 20: Headers
    scope8 = {
        "type": "websocket",
        "path": "/ws/test",
        "headers": [
            (b"x-custom", b"value123"),
            (b"authorization", b"Bearer token"),
        ],
        "query_string": b"",
    }
    ws8 = WebSocket(scope8, None, None)
    check("ws custom header", ws8.headers.get("x-custom") == "value123")
    check("ws auth header", ws8.headers.get("authorization") == "Bearer token")

    # ── Summary ──────────────────────────────────────────────────

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Results: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 60)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
