"""Regression: a channel auth denial must reject the connection, not be swallowed.

ws14's leak fix moved `channel.subscribe()` inside the handler's try/except.
`subscribe()` raises PermissionError when `auth_fn` denies — which the broad
`except Exception: pass` then swallowed, so the handler returned "normally" AND
fired `on_disconnect` without a paired `on_connect`. These tests lock in:
  * auth denial propagates (PermissionError is not swallowed),
  * no subscription is leaked (subscribe never succeeded),
  * `on_disconnect` does NOT fire when the client never connected,
  * a normal disconnect (client leaves after connecting) still fires on_disconnect.

Run: uv run pytest tests/test_standalone/test_channel_auth_denial.py -q
"""

import asyncio

import pytest

from hyperdjango.channels import Channel, ChannelLayer, websocket_channel_handler


class _FakeWS:
    """Minimal WebSocket double: iter_text yields nothing then the client leaves."""

    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)

    async def send_text(self, text):
        self.sent.append(text)

    async def iter_text(self):
        for t in self._incoming:
            yield t
        # then the async generator ends → client disconnected


def _mk_channel(auth_fn=None):
    layer = ChannelLayer()
    return Channel(name="room", layer=layer, auth_fn=auth_fn)


def test_auth_denial_propagates_and_leaks_nothing():
    ch = _mk_channel(auth_fn=lambda name, uid: False)  # deny everyone
    ws = _FakeWS()
    disconnect_calls = []

    async def on_connect(w, c):
        raise AssertionError("on_connect must not fire when auth denies")

    async def on_disconnect(w, c):
        disconnect_calls.append(True)

    with pytest.raises(PermissionError):
        asyncio.run(
            websocket_channel_handler(
                ws,
                ch,
                user_id="u1",
                on_connect=on_connect,
                on_disconnect=on_disconnect,
            )
        )

    # No subscription leaked, and on_disconnect never fired (never connected).
    assert len(ch._subscribers) == 0
    assert disconnect_calls == []


def test_normal_connect_disconnect_still_pairs():
    ch = _mk_channel(auth_fn=lambda name, uid: True)  # allow
    ws = _FakeWS(incoming=[])  # connects, then client leaves immediately
    events = []

    async def on_connect(w, c):
        events.append("connect")

    async def on_disconnect(w, c):
        events.append("disconnect")

    asyncio.run(
        websocket_channel_handler(
            ws,
            ch,
            user_id="u1",
            on_connect=on_connect,
            on_disconnect=on_disconnect,
        )
    )

    # Connected then disconnected → both hooks fire, in order, no leak.
    assert events == ["connect", "disconnect"]
    assert len(ch._subscribers) == 0
