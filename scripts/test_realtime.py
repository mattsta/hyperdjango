"""
Tests for hyperdjango.realtime — high-level real-time patterns.

Coverage:
- Room: join/leave, messaging, moderation, typing, history, rate limiting, members
- Notifications: send/receive, mark read, unread count, send_many, broadcast, clear
- LiveQuery: watch/unwatch, notify_create/update/delete, filtered subscriptions, on_change
- ConnectionManager: connect/disconnect, user tracking, send_to_user/connection, hooks
- WebSocketRateLimiter: token bucket, burst, reset, per-connection isolation
- Auth utilities: ws_authenticated, ws_auth_from_query, ws_auth_from_cookie
"""

# hyper-test: unit

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperdjango.channels import InMemoryChannelLayer
from hyperdjango.realtime import (
    VALID_NOTIFICATION_TYPES,
    ConnectionInfo,
    ConnectionManager,
    LiveQuery,
    ModelChange,
    Notification,
    NotificationManager,
    Room,
    RoomConfig,
    RoomMember,
    RoomMessage,
    WebSocketRateLimiter,
    WSRateLimitConfig,
    ws_auth_from_cookie,
    ws_auth_from_query,
    ws_authenticated,
)

PASS = 0
FAIL = 0


def ok(test_name: str):
    global PASS
    PASS += 1
    print(f"  PASS: {test_name}")


def fail(test_name: str, msg: str = ""):
    global FAIL
    FAIL += 1
    detail = f" -- {msg}" if msg else ""
    print(f"  FAIL: {test_name}{detail}")


def check(condition: bool, test_name: str, msg: str = ""):
    if condition:
        ok(test_name)
    else:
        fail(test_name, msg)


# ---------------------------------------------------------------------------
# Mock WebSocket
# ---------------------------------------------------------------------------


class MockWebSocket:
    """Mock WebSocket for testing."""

    def __init__(
        self,
        query_string: str = "",
        headers: dict[str, str] | None = None,
        path: str = "/ws",
    ):
        raw_headers = []
        if headers:
            for k, v in headers.items():
                raw_headers.append((k.encode("latin-1"), v.encode("latin-1")))
        self.scope: dict[str, Any] = {
            "path": path,
            "headers": raw_headers,
            "query_string": query_string.encode(),
            "subprotocols": [],
            "extensions": {},
        }
        self.path: str = path
        self.query_string: str = query_string
        self.headers: dict[str, str] = headers or {}
        self._accepted: bool = False
        self._closed: bool = False
        self._close_code: int = 0
        self._close_reason: str = ""
        self.sent_messages: list[dict[str, Any]] = []

    async def accept(self, subprotocol: str | None = None):
        self._accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        self._closed = True
        self._close_code = code
        self._close_reason = reason

    async def send_json(self, data: Any):
        self.sent_messages.append(data)

    async def send_text(self, data: str):
        self.sent_messages.append(json.loads(data))

    async def receive_text(self) -> str:
        return ""


def run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Room tests
# ---------------------------------------------------------------------------


def test_room():
    print("\n=== Room ===")

    layer = InMemoryChannelLayer()

    # Basic join
    room = Room("test1", layer)
    member = run(room.join("user1", "Alice"))
    check(member.user_id == "user1", "room join returns member")
    check(member.display_name == "Alice", "room join display name")
    check(member.role == "member", "room join default role")
    check(member.ws is None, "room join no ws")
    check(member.joined_at > 0, "room join timestamp")

    # Join with role
    member2 = run(room.join("user2", "Bob", role="moderator"))
    check(member2.role == "moderator", "room join custom role")

    # Join with ws
    ws = MockWebSocket()
    member3 = run(room.join("user3", "Charlie", ws=ws))
    check(member3.ws is ws, "room join with ws")

    # Get members
    members = room.get_members()
    check(len(members) == 3, "room get_members count", f"got {len(members)}")

    # Get specific member
    m = room.get_member("user1")
    check(m is not None and m.display_name == "Alice", "room get_member found")
    check(room.get_member("nonexistent") is None, "room get_member not found")

    # Leave
    result = run(room.leave("user3"))
    check(result is True, "room leave returns True")
    check(len(room.get_members()) == 2, "room leave removes member")

    # Leave nonexistent
    result = run(room.leave("nonexistent"))
    check(result is False, "room leave nonexistent returns False")

    # Send message
    msg = run(room.send_message("user1", "Hello!"))
    check(isinstance(msg, RoomMessage), "room send_message returns RoomMessage")
    check(msg.content == "Hello!", "room message content")
    check(msg.user_id == "user1", "room message user_id")
    check(msg.display_name == "Alice", "room message display_name")
    check(msg.message_type == "text", "room message default type")
    check(msg.room_id == "test1", "room message room_id")
    check(len(msg.id) > 0, "room message has id")
    check(msg.timestamp > 0, "room message has timestamp")
    check(msg.edited is False, "room message not edited")
    check(msg.deleted is False, "room message not deleted")

    # Send with type
    msg2 = run(room.send_message("user1", "pic.jpg", message_type="image"))
    check(msg2.message_type == "image", "room message custom type")

    # Invalid message type
    try:
        run(room.send_message("user1", "test", message_type="video"))
        fail("room invalid message type", "should raise ValueError")
    except ValueError:
        ok("room invalid message type raises ValueError")

    # Non-member send
    try:
        run(room.send_message("nonexistent", "hello"))
        fail("room non-member send", "should raise PermissionError")
    except PermissionError:
        ok("room non-member send raises PermissionError")

    # History
    history = room.get_history()
    check(len(history) == 2, "room history count", f"got {len(history)}")
    check(history[0].content == "Hello!", "room history order")

    # History with limit
    history_limited = room.get_history(limit=1)
    check(len(history_limited) == 1, "room history limit")

    # Broadcast
    run(room.broadcast({"type": "system", "text": "test"}))
    ok("room broadcast succeeds")

    # RoomMessage.to_dict
    d = msg.to_dict()
    check(d["content"] == "Hello!", "RoomMessage to_dict content")
    check(d["room_id"] == "test1", "RoomMessage to_dict room_id")
    check("id" in d, "RoomMessage to_dict has id")


def test_room_moderation():
    print("\n=== Room Moderation ===")

    layer = InMemoryChannelLayer()
    room = Room("mod", layer)
    run(room.join("user1", "Alice"))
    run(room.join("user2", "Bob"))

    # Kick
    result = run(room.kick("user2", reason="spam"))
    check(result is True, "room kick returns True")
    check(room.get_member("user2") is None, "room kick removes member")

    # Kick nonexistent
    result = run(room.kick("nonexistent"))
    check(result is False, "room kick nonexistent returns False")

    # Ban
    result = run(room.ban("user2", reason="harassment"))
    check(result is True, "room ban returns True")
    check(room.is_banned("user2"), "room ban sets banned")

    # Ban already banned
    result = run(room.ban("user2"))
    check(result is False, "room ban already banned returns False")

    # Join banned user
    try:
        run(room.join("user2", "Bob"))
        fail("room join banned user", "should raise PermissionError")
    except PermissionError:
        ok("room join banned user raises PermissionError")

    # Unban
    result = run(room.unban("user2"))
    check(result is True, "room unban returns True")
    check(not room.is_banned("user2"), "room unban clears ban")

    # Unban not banned
    result = run(room.unban("user2"))
    check(result is False, "room unban not banned returns False")

    # Can rejoin after unban
    m = run(room.join("user2", "Bob"))
    check(m.user_id == "user2", "room rejoin after unban")

    # Ban removes member
    run(room.ban("user2"))
    check(room.get_member("user2") is None, "room ban removes member if present")

    # is_banned
    check(room.is_banned("user2"), "is_banned True for banned")
    check(not room.is_banned("user1"), "is_banned False for not banned")


def test_room_typing():
    print("\n=== Room Typing ===")

    layer = InMemoryChannelLayer()
    room = Room("typing", layer)
    run(room.join("user1", "Alice"))
    run(room.join("user2", "Bob"))

    # Set typing
    run(room.set_typing("user1", True))
    typing = room.get_typing_users()
    check("user1" in typing, "room typing set")

    # Multiple typing
    run(room.set_typing("user2", True))
    typing = room.get_typing_users()
    check(len(typing) == 2, "room multiple typing")

    # Clear typing
    run(room.set_typing("user1", False))
    typing = room.get_typing_users()
    check("user1" not in typing, "room typing cleared")
    check("user2" in typing, "room other user still typing")

    # Typing expiry
    room._typing["user2"] = time.time() - 10.0  # expired
    typing = room.get_typing_users()
    check(len(typing) == 0, "room typing expires")


def test_room_rate_limit():
    print("\n=== Room Rate Limiting ===")

    layer = InMemoryChannelLayer()
    config = RoomConfig(rate_limit=3)
    room = Room("ratelimit", layer, config=config)
    run(room.join("user1", "Alice"))

    # Within limit
    run(room.send_message("user1", "msg1"))
    run(room.send_message("user1", "msg2"))
    run(room.send_message("user1", "msg3"))
    ok("room messages within rate limit")

    # Exceed limit
    try:
        run(room.send_message("user1", "msg4"))
        fail("room rate limit exceeded", "should raise PermissionError")
    except PermissionError:
        ok("room rate limit exceeded raises PermissionError")


def test_room_max_members():
    print("\n=== Room Max Members ===")

    layer = InMemoryChannelLayer()
    config = RoomConfig(max_members=2)
    room = Room("maxmembers", layer, config=config)

    run(room.join("user1", "Alice"))
    run(room.join("user2", "Bob"))

    try:
        run(room.join("user3", "Charlie"))
        fail("room max members", "should raise PermissionError")
    except PermissionError:
        ok("room max members raises PermissionError")

    # Existing member can rejoin (update)
    run(room.join("user1", "Alice Updated"))
    check(
        room.get_member("user1").display_name == "Alice Updated",
        "room rejoin existing member",
    )


def test_room_config_defaults():
    print("\n=== Room Config ===")

    config = RoomConfig()
    check(config.max_members == 100, "config default max_members")
    check(config.history_size == 100, "config default history_size")
    check(config.require_auth is True, "config default require_auth")
    check("text" in config.allowed_message_types, "config default allowed text")
    check("image" in config.allowed_message_types, "config default allowed image")
    check("file" in config.allowed_message_types, "config default allowed file")
    check("system" in config.allowed_message_types, "config default allowed system")
    check(config.rate_limit == 30, "config default rate_limit")
    check(config.max_message_length == 65536, "config default max_message_length")


# ---------------------------------------------------------------------------
# Notification tests
# ---------------------------------------------------------------------------


def test_notifications():
    print("\n=== Notifications ===")

    layer = InMemoryChannelLayer()
    mgr = NotificationManager(layer)

    # Send
    n = run(mgr.send("user1", "Welcome", "You joined!"))
    check(isinstance(n, Notification), "notification send returns Notification")
    check(n.user_id == "user1", "notification user_id")
    check(n.title == "Welcome", "notification title")
    check(n.body == "You joined!", "notification body")
    check(n.notification_type == "info", "notification default type")
    check(n.read is False, "notification default unread")
    check(n.created_at > 0, "notification timestamp")
    check(len(n.id) > 0, "notification has id")
    check(n.data is None, "notification default no data")

    # Send with data
    n2 = run(mgr.send("user1", "Alert", "Check this", "warning", data={"url": "/foo"}))
    check(n2.notification_type == "warning", "notification custom type")
    check(n2.data is not None and n2.data["url"] == "/foo", "notification with data")

    # Unread
    unread = mgr.get_unread("user1")
    check(len(unread) == 2, "notification get_unread count", f"got {len(unread)}")

    # Mark read
    result = mgr.mark_read("user1", n.id)
    check(result is True, "notification mark_read returns True")
    unread = mgr.get_unread("user1")
    check(len(unread) == 1, "notification mark_read reduces unread")

    # Mark read nonexistent
    result = mgr.mark_read("user1", "nonexistent")
    check(result is False, "notification mark_read nonexistent returns False")

    # Mark read wrong user
    result = mgr.mark_read("user999", n.id)
    check(result is False, "notification mark_read wrong user returns False")

    # Mark all read
    run(mgr.send("user1", "Third", "message"))
    count = mgr.mark_all_read("user1")
    check(count == 2, "notification mark_all_read count", f"got {count}")
    check(len(mgr.get_unread("user1")) == 0, "notification mark_all_read clears all")

    # Mark all read no user
    count = mgr.mark_all_read("nonexistent")
    check(count == 0, "notification mark_all_read nonexistent returns 0")

    # Clear
    total = mgr.clear("user1")
    check(total == 3, "notification clear count", f"got {total}")
    check(len(mgr.get_unread("user1")) == 0, "notification clear empties")

    # Clear empty
    total = mgr.clear("user1")
    check(total == 0, "notification clear empty returns 0")

    # Notification.to_dict
    n3 = run(mgr.send("user2", "Test", "body"))
    d = n3.to_dict()
    check(d["title"] == "Test", "Notification to_dict title")
    check(d["user_id"] == "user2", "Notification to_dict user_id")
    check("data" not in d, "Notification to_dict no data when None")

    n4 = run(mgr.send("user2", "Test2", "body2", data={"k": "v"}))
    d4 = n4.to_dict()
    check(d4["data"] == {"k": "v"}, "Notification to_dict with data")


def test_notification_send_many():
    print("\n=== Notification send_many ===")

    layer = InMemoryChannelLayer()
    mgr = NotificationManager(layer)

    results = run(mgr.send_many(["user1", "user2", "user3"], "Alert", "System update"))
    check(len(results) == 3, "send_many returns 3 notifications")
    check(results[0].user_id == "user1", "send_many first user")
    check(results[1].user_id == "user2", "send_many second user")
    check(results[2].user_id == "user3", "send_many third user")
    check(all(r.title == "Alert" for r in results), "send_many same title")

    check(len(mgr.get_unread("user1")) == 1, "send_many user1 has 1 unread")
    check(len(mgr.get_unread("user2")) == 1, "send_many user2 has 1 unread")


def test_notification_broadcast():
    print("\n=== Notification broadcast_all ===")

    layer = InMemoryChannelLayer()
    mgr = NotificationManager(layer)

    n = run(mgr.broadcast_all("Maintenance", "Server restart"))
    check(n.user_id == "__broadcast__", "broadcast user_id is __broadcast__")
    check(n.title == "Maintenance", "broadcast title")
    check(n.notification_type == "info", "broadcast default type")


def test_notification_subscribe():
    print("\n=== Notification subscribe/unsubscribe ===")

    layer = InMemoryChannelLayer()
    mgr = NotificationManager(layer)

    received = []

    def callback(msg):
        received.append(msg)

    sub_id = mgr.subscribe("user1", callback)
    check(isinstance(sub_id, int), "subscribe returns int id")

    run(mgr.send("user1", "Test", "body"))
    check(len(received) == 1, "subscriber receives notification")

    result = mgr.unsubscribe("user1", sub_id)
    check(result is True, "unsubscribe returns True")

    run(mgr.send("user1", "Test2", "body2"))
    check(len(received) == 1, "unsubscribed does not receive")


# ---------------------------------------------------------------------------
# LiveQuery tests
# ---------------------------------------------------------------------------


def test_livequery():
    print("\n=== LiveQuery ===")

    layer = InMemoryChannelLayer()
    live = LiveQuery(layer)

    # Watch
    sub_id = live.watch("Post")
    check(isinstance(sub_id, str), "watch returns string sub_id")
    check(len(sub_id) > 0, "watch sub_id not empty")

    # Notify create
    run(live.notify_create("Post", 1, {"title": "Hello"}))
    ok("notify_create succeeds")

    # Notify update
    run(live.notify_update("Post", 1, {"title": "Updated"}, ["title"]))
    ok("notify_update succeeds")

    # Notify delete
    run(live.notify_delete("Post", 1))
    ok("notify_delete succeeds")

    # Unwatch
    result = live.unwatch(sub_id)
    check(result is True, "unwatch returns True")

    result = live.unwatch(sub_id)
    check(result is False, "unwatch already unwatched returns False")

    result = live.unwatch("nonexistent")
    check(result is False, "unwatch nonexistent returns False")


def test_livequery_on_change():
    print("\n=== LiveQuery on_change ===")

    layer = InMemoryChannelLayer()
    live = LiveQuery(layer)

    changes = []

    @live.on_change("Post")
    async def handle_post(change: ModelChange):
        changes.append(change)

    run(live.notify_create("Post", 1, {"title": "New"}))
    check(len(changes) == 1, "on_change receives create")
    check(changes[0].action == "create", "on_change action is create")
    check(changes[0].pk == 1, "on_change pk")
    check(changes[0].data == {"title": "New"}, "on_change data")

    run(live.notify_update("Post", 1, {"title": "Updated"}, ["title"]))
    check(len(changes) == 2, "on_change receives update")
    check(changes[1].action == "update", "on_change update action")
    check(changes[1].changed_fields == ["title"], "on_change changed_fields")

    run(live.notify_delete("Post", 1))
    check(len(changes) == 3, "on_change receives delete")
    check(changes[2].action == "delete", "on_change delete action")
    check(changes[2].data is None, "on_change delete no data")

    # Sync handler
    sync_changes = []

    @live.on_change("Comment")
    def handle_comment(change: ModelChange):
        sync_changes.append(change)

    run(live.notify_create("Comment", 10, {"text": "hi"}))
    check(len(sync_changes) == 1, "on_change sync handler")

    # Handler for different model does not trigger
    run(live.notify_create("Comment", 11, {"text": "hello"}))
    check(len(changes) == 3, "on_change different model no trigger")


def test_livequery_filtered():
    print("\n=== LiveQuery Filtered Subscriptions ===")

    layer = InMemoryChannelLayer()
    live = LiveQuery(layer)

    # Watch with filters
    sub_id = live.watch("Comment", filters={"post_id": 42})
    check(isinstance(sub_id, str), "filtered watch returns sub_id")

    # Notify matching
    run(live.notify_create("Comment", 1, {"post_id": 42, "text": "match"}))
    ok("filtered notify matching succeeds")

    # Notify non-matching (still publishes, filter is on subscribe side)
    run(live.notify_create("Comment", 2, {"post_id": 99, "text": "no match"}))
    ok("filtered notify non-matching succeeds")

    live.unwatch(sub_id)
    ok("filtered unwatch succeeds")


def test_livequery_model_change_dataclass():
    print("\n=== ModelChange dataclass ===")

    change = ModelChange(
        model_name="Post",
        action="create",
        pk=1,
        data={"title": "test"},
        changed_fields=None,
        timestamp=1000.0,
    )
    check(change.model_name == "Post", "ModelChange model_name")
    check(change.action == "create", "ModelChange action")
    check(change.pk == 1, "ModelChange pk int")

    change2 = ModelChange(
        model_name="Post",
        action="update",
        pk="abc",
        data={"title": "updated"},
        changed_fields=["title"],
        timestamp=2000.0,
    )
    check(change2.pk == "abc", "ModelChange pk str")
    check(change2.changed_fields == ["title"], "ModelChange changed_fields")

    # Default timestamp
    change3 = ModelChange(model_name="X", action="delete", pk=0)
    check(change3.timestamp == 0.0, "ModelChange default timestamp")
    check(change3.data is None, "ModelChange default data")
    check(change3.changed_fields is None, "ModelChange default changed_fields")


# ---------------------------------------------------------------------------
# ConnectionManager tests
# ---------------------------------------------------------------------------


def test_connection_manager():
    print("\n=== ConnectionManager ===")

    layer = InMemoryChannelLayer()
    mgr = ConnectionManager(layer)

    ws1 = MockWebSocket()
    ws2 = MockWebSocket()

    # Connect
    info1 = run(mgr.connect(ws1, user_id="user1"))
    check(isinstance(info1, ConnectionInfo), "connect returns ConnectionInfo")
    check(info1.user_id == "user1", "connect user_id")
    check(info1.ws is ws1, "connect ws")
    check(info1.connected_at > 0, "connect timestamp")
    check(len(info1.connection_id) > 0, "connect has connection_id")
    check(len(info1.rooms) == 0, "connect empty rooms")
    check(len(info1.metadata) == 0, "connect empty metadata")

    # Connect with metadata
    info2 = run(mgr.connect(ws2, user_id="user1", metadata={"device": "mobile"}))
    check(info2.metadata["device"] == "mobile", "connect with metadata")

    # Connection count
    check(mgr.connection_count == 2, "connection_count", f"got {mgr.connection_count}")

    # Get connection
    found = mgr.get_connection(info1.connection_id)
    check(found is info1, "get_connection found")
    check(mgr.get_connection("nonexistent") is None, "get_connection not found")

    # Get user connections
    user_conns = mgr.get_user_connections("user1")
    check(len(user_conns) == 2, "get_user_connections count")
    check(mgr.get_user_connections("nonexistent") == [], "get_user_connections empty")

    # Get all connections
    all_conns = mgr.get_all_connections()
    check(len(all_conns) == 2, "get_all_connections count")

    # Disconnect
    result = run(mgr.disconnect(info1.connection_id))
    check(result is True, "disconnect returns True")
    check(mgr.connection_count == 1, "disconnect reduces count")
    check(
        mgr.get_connection(info1.connection_id) is None, "disconnect removes connection"
    )

    # Disconnect nonexistent
    result = run(mgr.disconnect("nonexistent"))
    check(result is False, "disconnect nonexistent returns False")

    # User connections after partial disconnect
    user_conns = mgr.get_user_connections("user1")
    check(len(user_conns) == 1, "user connections after disconnect")

    # Disconnect last connection for user
    run(mgr.disconnect(info2.connection_id))
    user_conns = mgr.get_user_connections("user1")
    check(len(user_conns) == 0, "no user connections after all disconnected")


def test_connection_manager_send():
    print("\n=== ConnectionManager Send ===")

    layer = InMemoryChannelLayer()
    mgr = ConnectionManager(layer)

    ws1 = MockWebSocket()
    ws2 = MockWebSocket()
    ws3 = MockWebSocket()

    info1 = run(mgr.connect(ws1, user_id="user1"))
    info2 = run(mgr.connect(ws2, user_id="user1"))
    info3 = run(mgr.connect(ws3, user_id="user2"))

    # Send to connection
    result = run(mgr.send_to_connection(info1.connection_id, {"type": "hello"}))
    check(result is True, "send_to_connection returns True")
    check(len(ws1.sent_messages) == 1, "send_to_connection delivers")
    check(ws1.sent_messages[0]["type"] == "hello", "send_to_connection content")

    # Send to nonexistent connection
    result = run(mgr.send_to_connection("nonexistent", {"type": "x"}))
    check(result is False, "send_to_connection nonexistent returns False")

    # Send to user
    count = run(mgr.send_to_user("user1", {"type": "update"}))
    check(count == 2, "send_to_user count", f"got {count}")
    check(len(ws1.sent_messages) == 2, "send_to_user delivers to ws1")
    check(len(ws2.sent_messages) == 1, "send_to_user delivers to ws2")
    check(len(ws3.sent_messages) == 0, "send_to_user does not deliver to other user")

    # Send to nonexistent user
    count = run(mgr.send_to_user("nonexistent", {"type": "x"}))
    check(count == 0, "send_to_user nonexistent returns 0")

    # Broadcast
    count = run(mgr.broadcast({"type": "announcement"}))
    check(count == 3, "broadcast count", f"got {count}")
    check(len(ws3.sent_messages) == 1, "broadcast reaches all users")


def test_connection_manager_hooks():
    print("\n=== ConnectionManager Hooks ===")

    layer = InMemoryChannelLayer()
    mgr = ConnectionManager(layer)

    connect_events = []
    disconnect_events = []

    async def on_connect(info: ConnectionInfo):
        connect_events.append(info.connection_id)

    async def on_disconnect(info: ConnectionInfo):
        disconnect_events.append(info.connection_id)

    mgr.on_connect = on_connect
    mgr.on_disconnect = on_disconnect

    ws = MockWebSocket()
    info = run(mgr.connect(ws, user_id="user1"))
    check(len(connect_events) == 1, "on_connect hook called")
    check(connect_events[0] == info.connection_id, "on_connect has connection_id")

    run(mgr.disconnect(info.connection_id))
    check(len(disconnect_events) == 1, "on_disconnect hook called")
    check(disconnect_events[0] == info.connection_id, "on_disconnect has connection_id")

    # Sync hooks
    sync_connect = []

    def sync_on_connect(info: ConnectionInfo):
        sync_connect.append(info.connection_id)

    mgr.on_connect = sync_on_connect
    mgr.on_disconnect = None

    ws2 = MockWebSocket()
    info2 = run(mgr.connect(ws2))
    check(len(sync_connect) == 1, "sync on_connect hook called")


def test_connection_manager_anonymous():
    print("\n=== ConnectionManager Anonymous ===")

    layer = InMemoryChannelLayer()
    mgr = ConnectionManager(layer)

    ws = MockWebSocket()
    info = run(mgr.connect(ws))
    check(info.user_id is None, "anonymous connect user_id is None")
    check(mgr.connection_count == 1, "anonymous connection counted")

    run(mgr.disconnect(info.connection_id))
    check(mgr.connection_count == 0, "anonymous disconnect")


# ---------------------------------------------------------------------------
# WebSocketRateLimiter tests
# ---------------------------------------------------------------------------


def test_rate_limiter_basic():
    print("\n=== WebSocketRateLimiter Basic ===")

    limiter = WebSocketRateLimiter()
    check(limiter.config.messages_per_second == 10, "default messages_per_second")
    check(limiter.config.messages_per_minute == 120, "default messages_per_minute")
    check(limiter.config.burst_size == 20, "default burst_size")

    # First messages allowed up to per_second limit (10), since per_second < burst_size
    for i in range(10):
        result = limiter.check("conn1")
        if not result:
            fail("rate_limiter first 10 allowed", f"blocked at message {i}")
            break
    else:
        ok("rate_limiter first 10 allowed (per_second)")

    # 11th should be blocked (per_second exhausted)
    result = limiter.check("conn1")
    check(result is False, "rate_limiter blocks after per_second limit")


def test_rate_limiter_per_connection():
    print("\n=== WebSocketRateLimiter Per-Connection ===")

    config = WSRateLimitConfig(
        burst_size=5, messages_per_second=5, messages_per_minute=100
    )
    limiter = WebSocketRateLimiter(config)

    # Exhaust conn1
    for _ in range(5):
        limiter.check("conn1")

    check(limiter.check("conn1") is False, "conn1 exhausted")
    check(limiter.check("conn2") is True, "conn2 independent")


def test_rate_limiter_reset():
    print("\n=== WebSocketRateLimiter Reset ===")

    config = WSRateLimitConfig(
        burst_size=3, messages_per_second=3, messages_per_minute=100
    )
    limiter = WebSocketRateLimiter(config)

    for _ in range(3):
        limiter.check("conn1")
    check(limiter.check("conn1") is False, "conn1 blocked before reset")

    limiter.reset("conn1")
    check(limiter.check("conn1") is True, "conn1 allowed after reset")

    # Reset nonexistent is safe
    limiter.reset("nonexistent")
    ok("reset nonexistent is safe")


def test_rate_limiter_stats():
    print("\n=== WebSocketRateLimiter Stats ===")

    config = WSRateLimitConfig(
        burst_size=10, messages_per_second=10, messages_per_minute=100
    )
    limiter = WebSocketRateLimiter(config)

    # Stats for new connection
    stats = limiter.get_stats("conn1")
    check(stats["tokens_remaining"] == 10.0, "stats initial tokens")
    check(stats["per_second_count"] == 0, "stats initial per_second")
    check(stats["per_minute_count"] == 0, "stats initial per_minute")
    check(stats["burst_size"] == 10, "stats burst_size")
    check(stats["messages_per_second"] == 10, "stats messages_per_second")
    check(stats["messages_per_minute"] == 100, "stats messages_per_minute")

    # After some messages
    for _ in range(3):
        limiter.check("conn1")

    stats = limiter.get_stats("conn1")
    check(stats["per_second_count"] == 3, "stats per_second after 3")
    check(stats["per_minute_count"] == 3, "stats per_minute after 3")
    check(stats["tokens_remaining"] < 10.0, "stats tokens decreased")


def test_rate_limiter_custom_config():
    print("\n=== WebSocketRateLimiter Custom Config ===")

    config = WSRateLimitConfig(
        messages_per_second=2,
        messages_per_minute=10,
        burst_size=3,
    )
    limiter = WebSocketRateLimiter(config)

    check(limiter.config.messages_per_second == 2, "custom messages_per_second")
    check(limiter.config.messages_per_minute == 10, "custom messages_per_minute")
    check(limiter.config.burst_size == 3, "custom burst_size")

    # Burst allows 3
    # burst_size=3, per_second=2: per_second kicks in first
    check(limiter.check("c") is True, "custom msg 1 allowed")
    check(limiter.check("c") is True, "custom msg 2 allowed")
    check(limiter.check("c") is False, "custom msg 3 blocked by per_second")


def test_rate_limiter_per_second_limit():
    print("\n=== WebSocketRateLimiter Per-Second Limit ===")

    config = WSRateLimitConfig(
        messages_per_second=2,
        messages_per_minute=1000,
        burst_size=100,  # high burst so token bucket doesn't interfere
    )
    limiter = WebSocketRateLimiter(config)

    check(limiter.check("c") is True, "per_sec msg 1")
    check(limiter.check("c") is True, "per_sec msg 2")
    check(limiter.check("c") is False, "per_sec msg 3 blocked")


def test_rate_limiter_per_minute_limit():
    print("\n=== WebSocketRateLimiter Per-Minute Limit ===")

    config = WSRateLimitConfig(
        messages_per_second=1000,
        messages_per_minute=5,
        burst_size=1000,
    )
    limiter = WebSocketRateLimiter(config)

    for i in range(5):
        check(limiter.check("c") is True, f"per_min msg {i + 1}")

    check(limiter.check("c") is False, "per_min msg 6 blocked")


# ---------------------------------------------------------------------------
# Auth utility tests
# ---------------------------------------------------------------------------


def test_auth_from_query():
    print("\n=== ws_auth_from_query ===")

    ws = MockWebSocket(query_string="token=abc123")
    result = ws_auth_from_query(ws)
    check(result == "abc123", "auth_from_query extracts token")

    ws2 = MockWebSocket(query_string="")
    check(ws_auth_from_query(ws2) is None, "auth_from_query empty qs returns None")

    ws3 = MockWebSocket(query_string="foo=bar")
    check(ws_auth_from_query(ws3) is None, "auth_from_query no token returns None")

    ws4 = MockWebSocket(query_string="token=")
    check(ws_auth_from_query(ws4) is None, "auth_from_query empty token returns None")

    ws5 = MockWebSocket(query_string="token=xyz&other=val")
    check(ws_auth_from_query(ws5) == "xyz", "auth_from_query with extra params")


def test_auth_from_cookie():
    print("\n=== ws_auth_from_cookie ===")

    ws = MockWebSocket(headers={"cookie": "sessionid=abc123; other=val"})
    result = ws_auth_from_cookie(ws)
    check(result == "abc123", "auth_from_cookie extracts sessionid")

    ws2 = MockWebSocket(headers={})
    check(ws_auth_from_cookie(ws2) is None, "auth_from_cookie no cookie returns None")

    ws3 = MockWebSocket(headers={"cookie": "other=val"})
    check(
        ws_auth_from_cookie(ws3) is None, "auth_from_cookie no sessionid returns None"
    )

    ws4 = MockWebSocket(headers={"cookie": "custom=mytoken"})
    check(
        ws_auth_from_cookie(ws4, cookie_name="custom") == "mytoken",
        "auth_from_cookie custom name",
    )

    ws5 = MockWebSocket(headers={"cookie": "sessionid="})
    check(ws_auth_from_cookie(ws5) is None, "auth_from_cookie empty value returns None")

    ws6 = MockWebSocket(headers={"cookie": " sessionid = abc ; foo = bar "})
    check(ws_auth_from_cookie(ws6) == "abc", "auth_from_cookie with spaces")


def test_ws_authenticated_decorator():
    print("\n=== ws_authenticated ===")

    # SECURE DEFAULT: a raw/forged token (not a validly-signed session) is now
    # REJECTED — previously the raw value was trusted as the user_id (a full
    # auth bypass: ?token=admin -> authed as admin). The verified-session accept
    # path is covered end-to-end by scripts/test_ws_security_r9.py.
    ws = MockWebSocket(query_string="token=user123")

    results = []

    @ws_authenticated
    async def handler(ws_inner, user_id):
        results.append(user_id)

    run(handler(ws))
    check(len(results) == 0, "ws_authenticated rejects a forged raw token")
    check(ws._closed and ws._close_code == 4001, "forged token closed with 4001")

    # Without auth -- ASGI requires accept before close
    ws2 = MockWebSocket()
    run(handler(ws2))
    check(ws2._accepted, "ws_authenticated accepts before closing")
    check(ws2._closed, "ws_authenticated closes unauthenticated")
    check(ws2._close_code == 4001, "ws_authenticated close code 4001")

    # Opt-in dev-only insecure mode trusts the raw token (loud per-connection
    # warning) — exercises the decorator plumbing without a real signed session.
    results.clear()

    @ws_authenticated(allow_insecure_raw_token=True)
    async def insecure_handler(ws_inner, user_id):
        results.append(user_id)

    ws3 = MockWebSocket(query_string="token=user123")
    run(insecure_handler(ws3))
    check(results == ["user123"], "insecure raw-token mode passes the value through")


# ---------------------------------------------------------------------------
# Additional edge case tests
# ---------------------------------------------------------------------------


def test_room_history_size():
    print("\n=== Room History Size ===")

    layer = InMemoryChannelLayer()
    config = RoomConfig(history_size=3)
    room = Room("histsize", layer, config=config)
    run(room.join("user1", "Alice"))

    for i in range(5):
        run(room.send_message("user1", f"msg{i}"))

    history = room.get_history()
    check(len(history) == 3, "room history capped at size", f"got {len(history)}")
    check(history[0].content == "msg2", "room history drops oldest")
    check(history[2].content == "msg4", "room history keeps newest")


def test_room_leave_clears_typing():
    print("\n=== Room Leave Clears Typing ===")

    layer = InMemoryChannelLayer()
    room = Room("typingleave", layer)
    run(room.join("user1", "Alice"))
    run(room.set_typing("user1", True))
    check("user1" in room.get_typing_users(), "typing before leave")

    run(room.leave("user1"))
    check("user1" not in room.get_typing_users(), "typing cleared after leave")


def test_connection_manager_rooms_set():
    print("\n=== ConnectionInfo rooms set ===")

    layer = InMemoryChannelLayer()
    mgr = ConnectionManager(layer)

    ws = MockWebSocket()
    info = run(mgr.connect(ws, user_id="user1"))

    # Rooms set is mutable from outside
    info.rooms.add("room1")
    info.rooms.add("room2")
    check(len(info.rooms) == 2, "connection rooms mutable")

    found = mgr.get_connection(info.connection_id)
    check("room1" in found.rooms, "rooms persisted in manager")


def test_multiple_rooms():
    print("\n=== Multiple Rooms ===")

    layer = InMemoryChannelLayer()
    room1 = Room("room1", layer)
    room2 = Room("room2", layer)

    run(room1.join("user1", "Alice"))
    run(room2.join("user1", "Alice"))

    check(len(room1.get_members()) == 1, "room1 has member")
    check(len(room2.get_members()) == 1, "room2 has member")

    run(room1.leave("user1"))
    check(len(room1.get_members()) == 0, "room1 empty after leave")
    check(len(room2.get_members()) == 1, "room2 still has member")


def test_notification_types():
    print("\n=== Notification Types ===")

    layer = InMemoryChannelLayer()
    mgr = NotificationManager(layer)

    for ntype in ("info", "warning", "error", "success"):
        n = run(mgr.send("user1", "Title", "Body", notification_type=ntype))
        check(n.notification_type == ntype, f"notification type '{ntype}'")


def test_livequery_multiple_handlers():
    print("\n=== LiveQuery Multiple Handlers ===")

    layer = InMemoryChannelLayer()
    live = LiveQuery(layer)

    results1 = []
    results2 = []

    @live.on_change("Post")
    async def handler1(change: ModelChange):
        results1.append(change.pk)

    @live.on_change("Post")
    async def handler2(change: ModelChange):
        results2.append(change.pk)

    run(live.notify_create("Post", 42, {"title": "test"}))
    check(len(results1) == 1, "multi handler 1 called")
    check(len(results2) == 1, "multi handler 2 called")
    check(results1[0] == 42, "multi handler 1 correct pk")
    check(results2[0] == 42, "multi handler 2 correct pk")


def test_livequery_multiple_watches():
    print("\n=== LiveQuery Multiple Watches ===")

    layer = InMemoryChannelLayer()
    live = LiveQuery(layer)

    sub1 = live.watch("Post")
    sub2 = live.watch("Post")
    check(sub1 != sub2, "multiple watches get different ids")

    live.unwatch(sub1)
    live.unwatch(sub2)
    ok("both watches unwatched")


def test_room_member_dataclass():
    print("\n=== RoomMember dataclass ===")

    member = RoomMember(
        user_id="u1",
        display_name="Alice",
        role="admin",
        joined_at=1000.0,
    )
    check(member.user_id == "u1", "RoomMember user_id")
    check(member.display_name == "Alice", "RoomMember display_name")
    check(member.role == "admin", "RoomMember role")
    check(member.joined_at == 1000.0, "RoomMember joined_at")
    check(member.ws is None, "RoomMember default ws None")


def test_connection_info_dataclass():
    print("\n=== ConnectionInfo dataclass ===")

    ws = MockWebSocket()
    info = ConnectionInfo(
        connection_id="abc",
        user_id="user1",
        ws=ws,
        connected_at=1000.0,
        rooms=set(),
        metadata={"key": "val"},
    )
    check(info.connection_id == "abc", "ConnectionInfo connection_id")
    check(info.user_id == "user1", "ConnectionInfo user_id")
    check(info.ws is ws, "ConnectionInfo ws")
    check(info.connected_at == 1000.0, "ConnectionInfo connected_at")
    check(len(info.rooms) == 0, "ConnectionInfo empty rooms")
    check(info.metadata["key"] == "val", "ConnectionInfo metadata")


def test_ws_rate_limit_config_dataclass():
    print("\n=== WSRateLimitConfig dataclass ===")

    config = WSRateLimitConfig()
    check(config.messages_per_second == 10, "WSRateLimitConfig default per_second")
    check(config.messages_per_minute == 120, "WSRateLimitConfig default per_minute")
    check(config.burst_size == 20, "WSRateLimitConfig default burst")

    config2 = WSRateLimitConfig(
        messages_per_second=5, messages_per_minute=50, burst_size=10
    )
    check(config2.messages_per_second == 5, "WSRateLimitConfig custom per_second")
    check(config2.messages_per_minute == 50, "WSRateLimitConfig custom per_minute")
    check(config2.burst_size == 10, "WSRateLimitConfig custom burst")


def test_message_length_limit():
    print("\n=== Room Message Length Limit ===")

    layer = InMemoryChannelLayer()
    config = RoomConfig(max_message_length=100)
    room = Room("lencheck", layer, config=config)
    run(room.join("user1", "Alice"))

    # Within limit
    msg = run(room.send_message("user1", "x" * 100))
    check(msg.content == "x" * 100, "message at max length allowed")

    # Exceeds limit
    try:
        run(room.send_message("user1", "x" * 101))
        fail("message over max length", "should raise ValueError")
    except ValueError as e:
        check("101 bytes" in str(e), "message length error mentions size")
        ok("message over max length raises ValueError")


def test_notification_type_validation():
    print("\n=== Notification Type Validation ===")

    layer = InMemoryChannelLayer()
    mgr = NotificationManager(layer)

    # Valid types all work
    for ntype in ("info", "warning", "error", "success"):
        n = run(mgr.send("user1", "T", "B", notification_type=ntype))
        check(n.notification_type == ntype, f"valid notification_type '{ntype}'")

    # Invalid type
    try:
        run(mgr.send("user1", "T", "B", notification_type="invalid"))
        fail("invalid notification_type", "should raise ValueError")
    except ValueError as e:
        check("invalid" in str(e), "error mentions invalid type")
        ok("invalid notification_type raises ValueError")

    # broadcast_all also validates
    try:
        run(mgr.broadcast_all("T", "B", notification_type="bogus"))
        fail("invalid broadcast notification_type", "should raise ValueError")
    except ValueError:
        ok("broadcast_all invalid type raises ValueError")

    # VALID_NOTIFICATION_TYPES is a frozenset
    check(
        isinstance(VALID_NOTIFICATION_TYPES, frozenset),
        "VALID_NOTIFICATION_TYPES is frozenset",
    )
    check(
        {"info", "warning", "error", "success"} == VALID_NOTIFICATION_TYPES,
        "VALID_NOTIFICATION_TYPES correct values",
    )


def test_metadata_size_limit():
    print("\n=== ConnectionManager Metadata Size Limit ===")

    layer = InMemoryChannelLayer()
    mgr = ConnectionManager(layer, max_metadata_keys=3)

    ws = MockWebSocket()

    # Within limit
    info = run(mgr.connect(ws, user_id="u1", metadata={"a": 1, "b": 2, "c": 3}))
    check(len(info.metadata) == 3, "metadata at limit allowed")

    # Exceeds limit
    ws2 = MockWebSocket()
    try:
        run(mgr.connect(ws2, user_id="u2", metadata={"a": 1, "b": 2, "c": 3, "d": 4}))
        fail("metadata over limit", "should raise ValueError")
    except ValueError as e:
        check("4 keys" in str(e), "metadata error mentions count")
        ok("metadata over limit raises ValueError")


def test_rate_limiter_cleanup_stale():
    print("\n=== WebSocketRateLimiter cleanup_stale ===")

    config = WSRateLimitConfig(
        burst_size=5, messages_per_second=5, messages_per_minute=100
    )
    limiter = WebSocketRateLimiter(config)

    # Create some buckets
    limiter.check("conn1")
    limiter.check("conn2")
    limiter.check("conn3")

    # Nothing stale yet (just created)
    removed = limiter.cleanup_stale(max_idle_seconds=300.0)
    check(removed == 0, "cleanup_stale nothing stale")

    # Make conn1 look old (buckets are sharded by connection id)
    _buckets, _lock = limiter._shard_for("conn1")
    with _lock:
        _buckets["conn1"].last_refill = time.time() - 400.0

    removed = limiter.cleanup_stale(max_idle_seconds=300.0)
    check(removed == 1, "cleanup_stale removes 1 stale bucket")

    # conn1 is gone
    stats = limiter.get_stats("conn1")
    check(stats["tokens_remaining"] == 5.0, "cleaned bucket returns fresh stats")

    # conn2 and conn3 still exist
    check(limiter.check("conn2") is True, "conn2 still alive after cleanup")
    check(limiter.check("conn3") is True, "conn3 still alive after cleanup")


def test_all_exports():
    print("\n=== __all__ exports ===")

    from hyperdjango import realtime

    expected = {
        "VALID_NOTIFICATION_TYPES",
        "RoomMember",
        "RoomConfig",
        "RoomMessage",
        "Room",
        "Notification",
        "NotificationManager",
        "ModelChange",
        "LiveQuery",
        "ConnectionInfo",
        "ConnectionManager",
        "WSRateLimitConfig",
        "WebSocketRateLimiter",
        "ws_authenticated",
        "ws_auth_from_query",
        "ws_auth_from_cookie",
    }

    actual = set(realtime.__all__)
    for name in expected:
        check(name in actual, f"__all__ contains {name}")
    check(
        len(actual) == len(expected),
        "__all__ correct size",
        f"got {len(actual)}, expected {len(expected)}",
    )


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    test_room()
    test_room_moderation()
    test_room_typing()
    test_room_rate_limit()
    test_room_max_members()
    test_room_config_defaults()
    test_notifications()
    test_notification_send_many()
    test_notification_broadcast()
    test_notification_subscribe()
    test_livequery()
    test_livequery_on_change()
    test_livequery_filtered()
    test_livequery_model_change_dataclass()
    test_connection_manager()
    test_connection_manager_send()
    test_connection_manager_hooks()
    test_connection_manager_anonymous()
    test_rate_limiter_basic()
    test_rate_limiter_per_connection()
    test_rate_limiter_reset()
    test_rate_limiter_stats()
    test_rate_limiter_custom_config()
    test_rate_limiter_per_second_limit()
    test_rate_limiter_per_minute_limit()
    test_auth_from_query()
    test_auth_from_cookie()
    test_ws_authenticated_decorator()
    test_room_history_size()
    test_room_leave_clears_typing()
    test_connection_manager_rooms_set()
    test_multiple_rooms()
    test_notification_types()
    test_livequery_multiple_handlers()
    test_livequery_multiple_watches()
    test_room_member_dataclass()
    test_connection_info_dataclass()
    test_ws_rate_limit_config_dataclass()
    test_message_length_limit()
    test_notification_type_validation()
    test_metadata_size_limit()
    test_rate_limiter_cleanup_stale()
    test_all_exports()

    loop.close()

    print(f"\n{'=' * 60}")
    print(f"realtime: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 60}")

    if FAIL > 0:
        sys.exit(1)
