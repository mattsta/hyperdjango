"""
Tests for WebSocket pub/sub channels.

Tests Channel, ChannelGroup, InMemoryChannelLayer, PgChannelLayer,
subscribe/unsubscribe, publish/broadcast, presence tracking, message
history, auth, groups, WebSocket bridge, and cross-process NOTIFY.

Usage:
    uv run hyper-test channels
"""

# hyper-test: unit

import asyncio
import contextlib
import inspect
import os
import sys
import time
import traceback

from hyperdjango.channels import (
    InMemoryChannelLayer,
    Message,
    PgChannelLayer,
    get_channel_layer,
    set_channel_layer,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Tests: Message
# ---------------------------------------------------------------------------


@test("Message: create with defaults")
def test_message_create():
    msg = Message(channel="test", data={"text": "hello"})
    assert msg.channel == "test"
    assert msg.data == {"text": "hello"}
    assert msg.timestamp > 0
    assert msg.sender is None


@test("Message: create with sender")
def test_message_sender():
    msg = Message(channel="chat", data="hi", sender="user42")
    assert msg.sender == "user42"


@test("Message: to_json and from_json roundtrip")
def test_message_json():
    original = Message(channel="test", data={"key": "value"}, sender="alice")
    json_str = original.to_json()
    restored = Message.from_json(json_str)

    assert restored.channel == original.channel
    assert restored.data == original.data
    assert restored.sender == original.sender
    assert abs(restored.timestamp - original.timestamp) < 0.01


@test("Message: from_json with bytes input")
def test_message_from_bytes():
    msg = Message(channel="test", data=42)
    json_bytes = msg.to_json().encode("utf-8")
    restored = Message.from_json(json_bytes)
    assert restored.data == 42


@test("Message: frozen (immutable)")
def test_message_frozen():
    msg = Message(channel="test", data="x")
    try:
        msg.channel = "other"
        assert False, "Should be frozen"
    except AttributeError, TypeError:
        pass


# ---------------------------------------------------------------------------
# Tests: Channel — subscribe / unsubscribe
# ---------------------------------------------------------------------------


@test("Channel: subscribe returns unique IDs")
def test_channel_subscribe_ids():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    id1 = ch.subscribe(lambda msg: None)
    id2 = ch.subscribe(lambda msg: None)
    assert id1 != id2
    assert isinstance(id1, int)


@test("Channel: unsubscribe removes subscriber")
def test_channel_unsubscribe():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    sub_id = ch.subscribe(lambda msg: None)
    assert ch.subscriber_count() == 1
    assert ch.unsubscribe(sub_id) is True
    assert ch.subscriber_count() == 0


@test("Channel: unsubscribe non-existent returns False")
def test_channel_unsubscribe_missing():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")
    assert ch.unsubscribe(99999) is False


@test("Channel: subscriber_count tracks correctly")
def test_channel_subscriber_count():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    ids = [ch.subscribe(lambda msg: None) for _ in range(5)]
    assert ch.subscriber_count() == 5

    ch.unsubscribe(ids[0])
    ch.unsubscribe(ids[2])
    assert ch.subscriber_count() == 3


# ---------------------------------------------------------------------------
# Tests: Channel — publish
# ---------------------------------------------------------------------------


@test("Channel: publish delivers to sync callback")
async def test_channel_publish_sync():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    received = []
    ch.subscribe(lambda msg: received.append(msg))
    await ch.publish({"text": "hello"})

    assert len(received) == 1
    assert received[0].data == {"text": "hello"}
    assert received[0].channel == "test"


@test("Channel: publish delivers to async callback")
async def test_channel_publish_async():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    received = []

    async def callback(msg):
        received.append(msg)

    ch.subscribe(callback)
    await ch.publish("async message")

    assert len(received) == 1
    assert received[0].data == "async message"


@test("Channel: publish delivers to multiple subscribers")
async def test_channel_publish_multiple():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    results = {"a": [], "b": [], "c": []}
    ch.subscribe(lambda msg: results["a"].append(msg.data))
    ch.subscribe(lambda msg: results["b"].append(msg.data))
    ch.subscribe(lambda msg: results["c"].append(msg.data))

    await ch.publish("broadcast")

    assert results["a"] == ["broadcast"]
    assert results["b"] == ["broadcast"]
    assert results["c"] == ["broadcast"]


@test("Channel: publish with sender")
async def test_channel_publish_sender():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    received = []
    ch.subscribe(lambda msg: received.append(msg))
    await ch.publish({"text": "hi"}, sender="user42")

    assert received[0].sender == "user42"


@test("Channel: subscriber error doesn't break other subscribers")
async def test_channel_subscriber_error():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    received = []

    def bad_callback(msg):
        raise ValueError("boom")

    ch.subscribe(bad_callback)
    ch.subscribe(lambda msg: received.append(msg.data))

    await ch.publish("still works")
    assert received == ["still works"]


@test("Channel: filter_fn controls delivery")
async def test_channel_filter():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    received = []
    ch.subscribe(
        lambda msg: received.append(msg.data),
        filter_fn=lambda msg: isinstance(msg.data, dict) and msg.data.get("important"),
    )

    await ch.publish({"important": False, "text": "skip"})
    await ch.publish({"important": True, "text": "keep"})
    await ch.publish("not a dict")

    assert len(received) == 1
    assert received[0]["text"] == "keep"


# ---------------------------------------------------------------------------
# Tests: Channel — presence
# ---------------------------------------------------------------------------


@test("Channel: join adds to presence")
def test_channel_join():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    ch.join("user1", metadata={"name": "Alice"})
    ch.join("user2", metadata={"name": "Bob"})

    members = ch.presence()
    assert len(members) == 2

    names = {m["name"] for m in members}
    assert names == {"Alice", "Bob"}

    user_ids = {m["user_id"] for m in members}
    assert user_ids == {"user1", "user2"}


@test("Channel: join includes joined_at timestamp")
def test_channel_join_timestamp():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    before = time.time()
    ch.join("user1")
    after = time.time()

    members = ch.presence()
    assert before <= members[0]["joined_at"] <= after


@test("Channel: join metadata cannot spoof the authoritative identity")
def test_channel_join_metadata_cannot_override_identity():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    # Client-supplied metadata tries to forge user_id + joined_at.
    before = time.time()
    ch.join(
        "attacker",
        metadata={"user_id": "admin", "joined_at": 0, "name": "Mallory"},
    )
    after = time.time()

    rec = ch.presence()[0]
    # Authoritative fields win over metadata.
    assert rec["user_id"] == "attacker", f"identity spoofed: {rec['user_id']}"
    assert before <= rec["joined_at"] <= after, "joined_at spoofed by metadata"
    # Non-reserved metadata is still retained.
    assert rec["name"] == "Mallory"
    # The presence record is keyed under the real id (leave works on it).
    assert ch.leave("attacker") is True


@test("Channel: leave removes from presence")
def test_channel_leave():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    ch.join("user1")
    ch.join("user2")
    assert ch.presence_count() == 2

    assert ch.leave("user1") is True
    assert ch.presence_count() == 1
    assert ch.presence()[0]["user_id"] == "user2"


@test("Channel: leave non-existent returns False")
def test_channel_leave_missing():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")
    assert ch.leave("nobody") is False


@test("Channel: presence is refcounted across a user's multiple connections")
def test_channel_presence_refcounted():
    # A user with two tabs/devices must stay present until the LAST one leaves —
    # a plain pop() marked them offline the moment any single connection closed.
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    ch.join("user1", metadata={"name": "Alice"})
    first_joined = ch.presence()[0]["joined_at"]
    ch.join("user1", metadata={"name": "Alice-tab2"})  # second connection
    assert ch.presence_count() == 1  # still one distinct user

    # Second connection preserves the original "online since".
    assert ch.presence()[0]["joined_at"] == first_joined

    # Closing ONE connection keeps the user present (other tab still open).
    assert ch.leave("user1") is True
    assert ch.presence_count() == 1, (
        "user went offline while another connection was live"
    )

    # Closing the LAST connection removes them.
    assert ch.leave("user1") is True
    assert ch.presence_count() == 0

    # Over-leaving is a no-op, not a negative refcount / ghost entry.
    assert ch.leave("user1") is False
    assert ch.presence_count() == 0


@test("Channel: presence_count")
def test_channel_presence_count():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    assert ch.presence_count() == 0
    ch.join("a")
    ch.join("b")
    assert ch.presence_count() == 2


@test("Channel: clear_presence")
def test_channel_clear_presence():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    ch.join("a")
    ch.join("b")
    ch.clear_presence()
    assert ch.presence_count() == 0


@test("Channel: join overwrites existing entry")
def test_channel_join_overwrite():
    layer = InMemoryChannelLayer()
    ch = layer.channel("room")

    ch.join("user1", metadata={"name": "Alice"})
    ch.join("user1", metadata={"name": "Alice Updated"})

    members = ch.presence()
    assert len(members) == 1
    assert members[0]["name"] == "Alice Updated"


# ---------------------------------------------------------------------------
# Tests: Channel — history
# ---------------------------------------------------------------------------


@test("Channel: history stores messages")
async def test_channel_history():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    await ch.publish("msg1")
    await ch.publish("msg2")
    await ch.publish("msg3")

    hist = ch.history()
    assert len(hist) == 3
    assert [m.data for m in hist] == ["msg1", "msg2", "msg3"]


@test("Channel: history respects limit")
async def test_channel_history_limit():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    for i in range(10):
        await ch.publish(f"msg{i}")

    hist = ch.history(limit=3)
    assert len(hist) == 3
    assert [m.data for m in hist] == ["msg7", "msg8", "msg9"]


@test("Channel: history bounded by max_history")
async def test_channel_max_history():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test", max_history=5)

    for i in range(20):
        await ch.publish(i)

    hist = ch.history(limit=100)
    assert len(hist) == 5
    assert [m.data for m in hist] == [15, 16, 17, 18, 19]


@test("Channel: clear_history")
async def test_channel_clear_history():
    layer = InMemoryChannelLayer()
    ch = layer.channel("test")

    await ch.publish("x")
    assert len(ch.history()) == 1
    ch.clear_history()
    assert len(ch.history()) == 0


# ---------------------------------------------------------------------------
# Tests: Channel — auth
# ---------------------------------------------------------------------------


@test("Channel: auth_fn allows access")
def test_channel_auth_allow():
    def auth(channel_name, user_id):
        return user_id == "admin"

    layer = InMemoryChannelLayer()
    ch = layer.channel("private", auth_fn=auth)

    sub_id = ch.subscribe(lambda msg: None, user_id="admin")
    assert sub_id > 0


@test("Channel: auth_fn denies access")
def test_channel_auth_deny():
    def auth(channel_name, user_id):
        return user_id == "admin"

    layer = InMemoryChannelLayer()
    ch = layer.channel("private", auth_fn=auth)

    try:
        ch.subscribe(lambda msg: None, user_id="hacker")
        assert False, "Should have raised PermissionError"
    except PermissionError as e:
        assert "private" in str(e)
        assert "hacker" in str(e)


@test("Channel: no auth_fn means open access")
def test_channel_no_auth():
    layer = InMemoryChannelLayer()
    ch = layer.channel("public")

    sub_id = ch.subscribe(lambda msg: None)
    assert sub_id > 0


# ---------------------------------------------------------------------------
# Tests: ChannelGroup
# ---------------------------------------------------------------------------


@test("ChannelGroup: add and list members")
def test_group_members():
    layer = InMemoryChannelLayer()
    group = layer.group("notifications")

    group.add("user:1")
    group.add("user:2")
    group.add("user:3")

    members = group.members()
    assert members == {"user:1", "user:2", "user:3"}
    assert group.size() == 3


@test("ChannelGroup: discard removes member")
def test_group_discard():
    layer = InMemoryChannelLayer()
    group = layer.group("notifications")

    group.add("user:1")
    group.add("user:2")
    group.discard("user:1")

    assert group.members() == {"user:2"}


@test("ChannelGroup: discard non-existent is safe")
def test_group_discard_missing():
    layer = InMemoryChannelLayer()
    group = layer.group("notifications")
    group.discard("nonexistent")  # Should not raise


@test("ChannelGroup: publish fans out to all channels")
async def test_group_publish():
    layer = InMemoryChannelLayer()
    group = layer.group("alerts")

    results = {"user1": [], "user2": [], "user3": []}

    ch1 = layer.channel("user:1")
    ch2 = layer.channel("user:2")
    ch3 = layer.channel("user:3")

    ch1.subscribe(lambda msg: results["user1"].append(msg.data))
    ch2.subscribe(lambda msg: results["user2"].append(msg.data))
    ch3.subscribe(lambda msg: results["user3"].append(msg.data))

    group.add("user:1")
    group.add("user:2")
    group.add("user:3")

    await group.publish({"text": "System update"})

    assert results["user1"] == [{"text": "System update"}]
    assert results["user2"] == [{"text": "System update"}]
    assert results["user3"] == [{"text": "System update"}]


@test("ChannelGroup: one failing channel does not abort the fan-out")
async def test_group_publish_isolates_failures():
    layer = InMemoryChannelLayer()
    group = layer.group("alerts")

    got = {"u1": [], "u3": []}
    layer.channel("user:1").subscribe(lambda m: got["u1"].append(m.data))
    layer.channel("user:3").subscribe(lambda m: got["u3"].append(m.data))

    # A member channel whose publish() itself raises (e.g. a NOTIFY failure).
    bad = layer.channel("user:2")

    async def boom(*a, **k):
        raise RuntimeError("NOTIFY failed")

    bad.publish = boom

    group.add("user:1")
    group.add("user:2")
    group.add("user:3")

    # Must NOT raise, and healthy members must still receive.
    await group.publish({"text": "hi"})
    assert got["u1"] == [{"text": "hi"}], "healthy channel u1 missed the fan-out"
    assert got["u3"] == [{"text": "hi"}], "healthy channel u3 missed the fan-out"


@test("ChannelGroup: publish only to group members")
async def test_group_partial():
    layer = InMemoryChannelLayer()
    group = layer.group("vip")

    results = {"member": [], "nonmember": []}

    ch1 = layer.channel("ch1")
    ch2 = layer.channel("ch2")

    ch1.subscribe(lambda msg: results["member"].append(msg.data))
    ch2.subscribe(lambda msg: results["nonmember"].append(msg.data))

    group.add("ch1")  # Only ch1 is in group

    await group.publish("vip only")

    assert results["member"] == ["vip only"]
    assert results["nonmember"] == []


# ---------------------------------------------------------------------------
# Tests: InMemoryChannelLayer
# ---------------------------------------------------------------------------


@test("Layer: channel returns same instance for same name")
def test_layer_channel_identity():
    layer = InMemoryChannelLayer()
    ch1 = layer.channel("test")
    ch2 = layer.channel("test")
    assert ch1 is ch2


@test("Layer: different names return different channels")
def test_layer_different_channels():
    layer = InMemoryChannelLayer()
    ch1 = layer.channel("a")
    ch2 = layer.channel("b")
    assert ch1 is not ch2


@test("Layer: channel_names lists all channels")
def test_layer_channel_names():
    layer = InMemoryChannelLayer()
    layer.channel("alpha")
    layer.channel("beta")
    layer.channel("gamma")

    names = layer.channel_names()
    assert set(names) == {"alpha", "beta", "gamma"}


@test("Layer: remove_channel")
def test_layer_remove_channel():
    layer = InMemoryChannelLayer()
    layer.channel("ephemeral")
    assert layer.remove_channel("ephemeral") is True
    assert "ephemeral" not in layer.channel_names()
    assert layer.remove_channel("ephemeral") is False


@test("Layer: group returns same instance for same name")
def test_layer_group_identity():
    layer = InMemoryChannelLayer()
    g1 = layer.group("team")
    g2 = layer.group("team")
    assert g1 is g2


@test("Layer: group_names lists all groups")
def test_layer_group_names():
    layer = InMemoryChannelLayer()
    layer.group("a")
    layer.group("b")
    assert set(layer.group_names()) == {"a", "b"}


@test("Layer: remove_group")
def test_layer_remove_group():
    layer = InMemoryChannelLayer()
    layer.group("temp")
    assert layer.remove_group("temp") is True
    assert layer.remove_group("temp") is False


@test("Layer: default_history_size applies to new channels")
def test_layer_default_history():
    layer = InMemoryChannelLayer(default_history_size=10)
    ch = layer.channel("test")
    assert ch.max_history == 10


# ---------------------------------------------------------------------------
# Tests: Cross-channel isolation
# ---------------------------------------------------------------------------


@test("Channels: messages isolated between channels")
async def test_channel_isolation():
    layer = InMemoryChannelLayer()
    ch_a = layer.channel("a")
    ch_b = layer.channel("b")

    results_a = []
    results_b = []

    ch_a.subscribe(lambda msg: results_a.append(msg.data))
    ch_b.subscribe(lambda msg: results_b.append(msg.data))

    await ch_a.publish("for a")
    await ch_b.publish("for b")

    assert results_a == ["for a"]
    assert results_b == ["for b"]


# ---------------------------------------------------------------------------
# Tests: Global singleton
# ---------------------------------------------------------------------------


@test("Global: set_channel_layer and get_channel_layer")
def test_global_layer():
    layer = InMemoryChannelLayer()
    old = None
    with contextlib.suppress(RuntimeError):
        old = get_channel_layer()

    set_channel_layer(layer)
    assert get_channel_layer() is layer

    set_channel_layer(old)


@test("Global: get_channel_layer raises without set")
def test_global_layer_missing():
    saved = None
    with contextlib.suppress(RuntimeError):
        saved = get_channel_layer()

    set_channel_layer(None)
    try:
        get_channel_layer()
        assert False, "Should have raised"
    except RuntimeError as e:
        assert "No channel layer" in str(e)
    finally:
        set_channel_layer(saved)


# ---------------------------------------------------------------------------
# Tests: PgChannelLayer (requires database)
# ---------------------------------------------------------------------------


@test("PgChannelLayer: connect and disconnect")
async def test_pg_connect():
    layer = PgChannelLayer(database_url=DB_URL)
    await layer.connect()
    assert layer._db is not None
    await layer.disconnect()
    assert layer._db is None


@test("PgChannelLayer: _pg_channel_name sanitizes names")
def test_pg_channel_name():
    layer = PgChannelLayer()
    assert layer._pg_channel_name("chat:room1") == "hyper_ch_chat_room1"
    assert layer._pg_channel_name("user.events") == "hyper_ch_user_events"
    assert layer._pg_channel_name("simple") == "hyper_ch_simple"


@test("PgChannelLayer: publish sends NOTIFY")
async def test_pg_publish():
    layer = PgChannelLayer(database_url=DB_URL)
    await layer.connect()

    received = []
    ch = layer.channel("pg_test")
    ch.subscribe(lambda msg: received.append(msg.data))

    # Publish locally (NOTIFY will also fire but we test local delivery)
    await ch.publish({"text": "pg message"})
    assert len(received) == 1
    assert received[0] == {"text": "pg message"}

    await layer.disconnect()


@test("PgChannelLayer: channel creates and auto-registers")
async def test_pg_channel_create():
    layer = PgChannelLayer(database_url=DB_URL)
    await layer.connect()

    ch = layer.channel("auto_registered")
    assert ch.name == "auto_registered"
    assert "auto_registered" in layer.channel_names()

    await layer.disconnect()


@test("PgChannelLayer: large message staging")
async def test_pg_large_message():
    layer = PgChannelLayer(database_url=DB_URL)
    await layer.connect()

    # Create a message > 7500 bytes
    large_data = "x" * 8000

    received = []
    ch = layer.channel("large_test")
    ch.subscribe(lambda msg: received.append(msg.data))

    await ch.publish(large_data)

    # Local delivery should still work
    assert len(received) == 1
    assert received[0] == large_data

    # Cleanup
    with contextlib.suppress(Exception):
        await layer._db.execute("DROP TABLE IF EXISTS hyper_channel_messages")
    await layer.disconnect()


@test("PgChannelLayer: multiplexed cross-process NOTIFY + O(1) listener connections")
async def test_pg_multiplex_delivery():
    # Two layer instances = two "nodes" on the same database. Node A subscribes
    # to MANY channels; Node B publishes to them. Delivery must arrive at Node A
    # via the real NOTIFY round-trip (Node A's origin-dedup does not drop B's
    # publishes), and — the whole point of the multiplexed listener — all those
    # channels must be served by ONE listener connection per node, not one per
    # channel.
    node_a = PgChannelLayer(database_url=DB_URL)
    node_b = PgChannelLayer(database_url=DB_URL)
    await node_a.connect()
    await node_b.connect()
    try:
        n = 20
        received: dict[str, list] = {}

        def make_sub(name):
            return lambda msg: received.setdefault(name, []).append(msg.data)

        for i in range(n):
            name = f"mux_room_{i}"
            ch = node_a.channel(name)  # auto-starts the shared listener + LISTEN
            ch.subscribe(make_sub(name))

        # Let the single multiplexed listener issue all N LISTENs on the wire.
        await asyncio.sleep(0.6)

        for i in range(n):
            await node_b.channel(f"mux_room_{i}").publish({"i": i})

        # Await cross-process delivery through the listener thread.
        for _ in range(60):
            if sum(len(v) for v in received.values()) >= n:
                break
            await asyncio.sleep(0.05)

        missing = [f"mux_room_{i}" for i in range(n) if f"mux_room_{i}" not in received]
        assert not missing, f"cross-process NOTIFY not delivered for: {missing}"

        # O(1): count backends parked on a LISTEN for this database. With the
        # multiplexed listener it is ~1 per node (here 2); a per-channel
        # regression would show ~n per node (40+).
        rows = await node_b._db.query(
            "SELECT count(*) AS c FROM pg_stat_activity "
            "WHERE datname = current_database() AND query ILIKE 'LISTEN%'"
        )
        listen_conns = int(rows[0]["c"])
        assert listen_conns <= 4, (
            f"expected O(1) listener connections, got {listen_conns} for "
            f"{n} channels × 2 nodes — per-channel regression?"
        )
    finally:
        await node_a.disconnect()
        await node_b.disconnect()


# ---------------------------------------------------------------------------
# Tests: Concurrency
# ---------------------------------------------------------------------------


@test("Channel: concurrent subscribe/unsubscribe is thread-safe")
async def test_concurrent_subscribe():
    import threading

    layer = InMemoryChannelLayer()
    ch = layer.channel("concurrent")

    ids = []
    lock = threading.Lock()

    def subscribe_many():
        for _ in range(100):
            sub_id = ch.subscribe(lambda msg: None)
            with lock:
                ids.append(sub_id)

    threads = [threading.Thread(target=subscribe_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == 400
    assert len(set(ids)) == 400  # All unique


@test("Channel: concurrent publish is safe")
async def test_concurrent_publish():
    layer = InMemoryChannelLayer()
    ch = layer.channel("concurrent")

    received = []
    lock = asyncio.Lock()

    async def safe_append(msg):
        async with lock:
            received.append(msg.data)

    ch.subscribe(safe_append)

    tasks = [ch.publish(i) for i in range(50)]
    await asyncio.gather(*tasks)

    assert len(received) == 50
    assert set(received) == set(range(50))


# ---------------------------------------------------------------------------
# Tests: Complex scenarios
# ---------------------------------------------------------------------------


@test("Scenario: chat room with presence and history")
async def test_chat_scenario():
    layer = InMemoryChannelLayer()
    room = layer.channel("chat:general", max_history=10)

    # Users join
    alice_msgs = []
    bob_msgs = []

    room.subscribe(lambda msg: alice_msgs.append(msg))
    room.subscribe(lambda msg: bob_msgs.append(msg))

    room.join("alice", {"name": "Alice"})
    room.join("bob", {"name": "Bob"})

    assert room.presence_count() == 2

    # Send messages
    await room.publish({"text": "Hello!"}, sender="alice")
    await room.publish({"text": "Hi Alice!"}, sender="bob")
    await room.publish({"text": "How are you?"}, sender="alice")

    # Both receive all messages
    assert len(alice_msgs) == 3
    assert len(bob_msgs) == 3

    # History preserved
    hist = room.history()
    assert len(hist) == 3
    assert hist[0].sender == "alice"
    assert hist[1].sender == "bob"

    # Bob leaves
    room.leave("bob")
    assert room.presence_count() == 1
    assert room.presence()[0]["name"] == "Alice"


@test("Scenario: notification broadcast to user channels")
async def test_notification_scenario():
    layer = InMemoryChannelLayer()

    # Create per-user channels
    user_msgs = {1: [], 2: [], 3: []}
    for uid in user_msgs:
        ch = layer.channel(f"user:{uid}")
        ch.subscribe(lambda msg, uid=uid: user_msgs[uid].append(msg.data))

    # Create notification group
    group = layer.group("all_users")
    group.add("user:1")
    group.add("user:2")
    group.add("user:3")

    # Broadcast notification
    await group.publish({"type": "alert", "text": "Server maintenance at 2am"})

    for uid in user_msgs:
        assert len(user_msgs[uid]) == 1
        assert user_msgs[uid][0]["type"] == "alert"

    # Remove user from group
    group.discard("user:2")
    await group.publish({"type": "alert", "text": "Maintenance complete"})

    assert len(user_msgs[1]) == 2
    assert len(user_msgs[2]) == 1  # Didn't get second message
    assert len(user_msgs[3]) == 2


@test("Scenario: filtered subscription")
async def test_filtered_scenario():
    layer = InMemoryChannelLayer()
    ch = layer.channel("events")

    errors = []
    warnings = []
    all_msgs = []

    ch.subscribe(
        lambda msg: errors.append(msg.data),
        filter_fn=lambda msg: msg.data.get("level") == "error",
    )
    ch.subscribe(
        lambda msg: warnings.append(msg.data),
        filter_fn=lambda msg: msg.data.get("level") == "warning",
    )
    ch.subscribe(lambda msg: all_msgs.append(msg.data))

    await ch.publish({"level": "info", "text": "Started"})
    await ch.publish({"level": "warning", "text": "Slow query"})
    await ch.publish({"level": "error", "text": "Connection lost"})

    assert len(errors) == 1
    assert len(warnings) == 1
    assert len(all_msgs) == 3


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nWebSocket Pub/Sub Channels Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
