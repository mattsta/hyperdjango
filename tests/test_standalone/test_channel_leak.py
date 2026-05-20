"""Regression: channel subscriber leak on abrupt WS disconnect.

Before the ws14-syscall-fixes work, websocket_channel_handler ran
channel.subscribe()/join() and the history-replay + on_connect awaits BEFORE
its try/finally. A client that disconnected in that window (an await that
raises WebSocketDisconnect) escaped the function before the finally could
unsubscribe, leaking a Subscription in Channel._subscribers forever — each
dead subscriber keeps its asyncio.Queue alive and every subsequent publish
does a dead call_soon_threadsafe.

These tests drive a disconnect during history replay and during on_connect and
assert the subscriber count returns to 0.
"""

from hyperdjango.channels import InMemoryChannelLayer, websocket_channel_handler
from hyperdjango.websocket import WebSocketDisconnect


class _DisconnectOnSendJson:
    """Fake ws that disconnects the instant history replay tries to send."""

    async def accept(self):  # pragma: no cover - handler doesn't call it here
        pass

    async def send_json(self, data):
        raise WebSocketDisconnect(1000)


class _DisconnectOnConnect:
    """Fake ws that survives history replay but nothing is replayed here."""

    async def send_json(self, data):  # pragma: no cover - no history seeded
        pass


async def test_disconnect_during_history_replay_leaves_no_subscriber():
    layer = InMemoryChannelLayer()
    channel = layer.channel("leak-history")
    # Seed one history entry so the replay loop actually runs send_json.
    await channel.publish({"n": 1})
    assert len(channel.history()) >= 1

    # Must return cleanly (exception caught) and leave zero subscribers.
    await websocket_channel_handler(_DisconnectOnSendJson(), channel, user_id="u1")

    assert channel.subscriber_count() == 0
    assert channel.presence_count() == 0


async def test_disconnect_during_on_connect_leaves_no_subscriber():
    layer = InMemoryChannelLayer()
    channel = layer.channel("leak-onconnect")

    async def on_connect(ws, ch):
        raise WebSocketDisconnect(1000)

    await websocket_channel_handler(
        _DisconnectOnConnect(), channel, user_id="u2", on_connect=on_connect
    )

    assert channel.subscriber_count() == 0
    assert channel.presence_count() == 0


async def test_many_abrupt_disconnects_do_not_accumulate():
    layer = InMemoryChannelLayer()
    channel = layer.channel("leak-many")
    await channel.publish({"n": 1})

    for _ in range(50):
        await websocket_channel_handler(_DisconnectOnSendJson(), channel, user_id=None)

    # 0 → 50 permanently was the bug; must stay 0.
    assert channel.subscriber_count() == 0
