"""Tests for WebSocket support."""

import json

import pytest

from hyperdjango.websocket import WebSocket, WebSocketDisconnect


class TestWebSocketCreation:
    def test_create_from_scope(self):
        scope = {
            "type": "websocket",
            "path": "/ws/chat",
            "headers": [],
            "query_string": b"token=abc",
        }
        ws = WebSocket(scope, receive=None, send=None)
        assert ws.path == "/ws/chat"
        assert ws.query_string == "token=abc"

    def test_default_path(self):
        ws = WebSocket({}, receive=None, send=None)
        assert ws.path == "/"

    def test_empty_query_string(self):
        ws = WebSocket({"path": "/ws"}, receive=None, send=None)
        assert ws.query_string == ""

    def test_headers_parsed(self):
        scope = {
            "path": "/ws",
            "headers": [(b"host", b"example.com")],
        }
        ws = WebSocket(scope, receive=None, send=None)
        assert ws.headers["host"] == "example.com"

    def test_not_accepted_initially(self):
        ws = WebSocket({"path": "/ws"}, receive=None, send=None)
        assert ws._accepted is False


class TestWebSocketAccept:
    async def test_accept_sends_message(self):
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        ws = WebSocket({"path": "/ws"}, receive=None, send=mock_send)
        await ws.accept()
        assert ws._accepted is True
        assert sent[0]["type"] == "websocket.accept"

    async def test_accept_with_subprotocol(self):
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        ws = WebSocket({"path": "/ws"}, receive=None, send=mock_send)
        await ws.accept(subprotocol="graphql-ws")
        assert sent[0]["subprotocol"] == "graphql-ws"


class TestWebSocketClose:
    async def test_close_default_code(self):
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        ws = WebSocket({"path": "/ws"}, receive=None, send=mock_send)
        await ws.close()
        assert sent[0]["type"] == "websocket.close"
        assert sent[0]["code"] == 1000

    async def test_close_custom_code(self):
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        ws = WebSocket({"path": "/ws"}, receive=None, send=mock_send)
        await ws.close(code=4001, reason="Unauthorized")
        assert sent[0]["code"] == 4001
        assert sent[0]["reason"] == "Unauthorized"


class TestWebSocketReceive:
    async def test_receive_text(self):
        async def mock_receive():
            return {"type": "websocket.receive", "text": "hello"}

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        text = await ws.receive_text()
        assert text == "hello"

    async def test_receive_text_disconnect(self):
        async def mock_receive():
            return {"type": "websocket.disconnect", "code": 1001}

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_text()
        assert exc_info.value.code == 1001

    async def test_receive_bytes(self):
        async def mock_receive():
            return {"type": "websocket.receive", "bytes": b"\x00\x01"}

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        data = await ws.receive_bytes()
        assert data == b"\x00\x01"

    async def test_receive_bytes_disconnect(self):
        async def mock_receive():
            return {"type": "websocket.disconnect", "code": 1000}

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        with pytest.raises(WebSocketDisconnect):
            await ws.receive_bytes()

    async def test_receive_json(self):
        async def mock_receive():
            return {"type": "websocket.receive", "text": '{"key": "value"}'}

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        data = await ws.receive_json()
        assert data == {"key": "value"}

    async def test_receive_type_preserving(self):
        # Unified receive(): text frame -> str, binary frame -> bytes
        async def recv_text():
            return {"type": "websocket.receive", "text": "raw"}

        ws = WebSocket({"path": "/ws"}, receive=recv_text, send=None)
        assert await ws.receive() == "raw"

        async def recv_bin():
            return {"type": "websocket.receive", "bytes": b"\x00\x01"}

        ws2 = WebSocket({"path": "/ws"}, receive=recv_bin, send=None)
        assert await ws2.receive() == b"\x00\x01"

    async def test_receive_raw_lowlevel(self):
        msg = {"type": "websocket.receive", "text": "raw"}

        async def mock_receive():
            return msg

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        assert await ws.receive_raw() == msg


class TestWebSocketSend:
    async def test_send_text(self):
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        ws = WebSocket({"path": "/ws"}, receive=None, send=mock_send)
        await ws.send_text("hello")
        assert sent[0] == {"type": "websocket.send", "text": "hello"}

    async def test_send_bytes(self):
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        ws = WebSocket({"path": "/ws"}, receive=None, send=mock_send)
        await ws.send_bytes(b"\x00\x01")
        assert sent[0] == {"type": "websocket.send", "bytes": b"\x00\x01"}

    async def test_send_json(self):
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        ws = WebSocket({"path": "/ws"}, receive=None, send=mock_send)
        await ws.send_json({"key": "value"})
        parsed = json.loads(sent[0]["text"])
        assert parsed == {"key": "value"}


class TestWebSocketIterators:
    async def test_iter_text(self):
        messages = [
            {"type": "websocket.receive", "text": "one"},
            {"type": "websocket.receive", "text": "two"},
            {"type": "websocket.disconnect", "code": 1000},
        ]
        idx = 0

        async def mock_receive():
            nonlocal idx
            msg = messages[idx]
            idx += 1
            return msg

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        collected = []
        async for text in ws.iter_text():
            collected.append(text)
        assert collected == ["one", "two"]

    async def test_iter_json(self):
        messages = [
            {"type": "websocket.receive", "text": '{"n": 1}'},
            {"type": "websocket.receive", "text": '{"n": 2}'},
            {"type": "websocket.disconnect", "code": 1000},
        ]
        idx = 0

        async def mock_receive():
            nonlocal idx
            msg = messages[idx]
            idx += 1
            return msg

        ws = WebSocket({"path": "/ws"}, receive=mock_receive, send=None)
        collected = []
        async for data in ws.iter_json():
            collected.append(data)
        assert collected == [{"n": 1}, {"n": 2}]


class TestWebSocketDisconnectException:
    def test_default_code(self):
        exc = WebSocketDisconnect()
        assert exc.code == 1000

    def test_custom_code(self):
        exc = WebSocketDisconnect(code=4001)
        assert exc.code == 4001

    def test_is_exception(self):
        assert issubclass(WebSocketDisconnect, Exception)

    def test_str(self):
        exc = WebSocketDisconnect(code=1001)
        assert "1001" in str(exc)


class TestZigWebSocketByteFastPath:
    """ZigWebSocket._send_text_bytes fast path + version-skew degradation.

    Built via object.__new__ to bypass __init__ (which imports native symbols
    and needs a live connection) — we only exercise the send routing.
    """

    def _make(self, native_stb):
        from hyperdjango.websocket import ZigWebSocket

        ws = object.__new__(ZigWebSocket)
        ws._conn_id = 7
        ws._native_send_text_bytes = native_stb
        ws.sent_text = []
        ws.sent_native = []
        # Non-blocking send path attributes (round-10): with _try_send/_fd None,
        # _send_text_bytes falls back to the blocking native path this test
        # exercises. Set them so the __init__-bypassed instance is coherent.
        ws._try_send = None
        ws._flush_send = None
        ws._send_ping = None
        ws._fd = None
        # self._send is the str-frame native fn; stub it to record decoded str.
        ws._send = lambda cid, text: ws.sent_text.append((cid, text))
        return ws

    def test_uses_native_symbol_when_present(self):
        ws = self._make(None)
        ws._native_send_text_bytes = lambda cid, data: ws.sent_native.append(
            (cid, data)
        )
        ws._send_text_bytes(b'{"k":"v"}')
        assert ws.sent_native == [(7, b'{"k":"v"}')]
        assert ws.sent_text == []  # native path — no str fallback

    async def test_send_json_uses_native_byte_path(self):
        ws = self._make(None)
        ws._native_send_text_bytes = lambda cid, data: ws.sent_native.append(
            (cid, data)
        )
        await ws.send_json({"key": "value"})
        assert len(ws.sent_native) == 1
        cid, data = ws.sent_native[0]
        assert cid == 7 and isinstance(data, bytes)
        assert json.loads(data.decode()) == {"key": "value"}

    def test_degrades_to_str_path_when_symbol_missing(self):
        # Simulate a version-skewed .so lacking _ws_send_text_bytes.
        ws = self._make(None)
        ws._send_text_bytes("héllo 🎉".encode())
        # Fell back to the str frame path, decoding the UTF-8 bytes losslessly.
        assert ws.sent_text == [(7, "héllo 🎉")]
        assert ws.sent_native == []

    async def test_send_json_degrades_when_symbol_missing(self):
        ws = self._make(None)
        await ws.send_json({"msg": "你好"})
        assert len(ws.sent_text) == 1
        cid, text = ws.sent_text[0]
        assert cid == 7 and json.loads(text) == {"msg": "你好"}
