# Real-time Patterns

High-level real-time patterns built on top of `channels.py` and `websocket.py`. Production-ready abstractions for common use cases: chat rooms, notifications, live queries, connection management, rate limiting, and authentication.

All patterns are thread-safe for Python 3.14t.

```python
from hyperdjango.realtime import (
    Room,
    RoomConfig,
    RoomMember,
    RoomMessage,
    NotificationManager,
    Notification,
    LiveQuery,
    ModelChange,
    ConnectionManager,
    ConnectionInfo,
    WebSocketRateLimiter,
    WSRateLimitConfig,
    ws_authenticated,
    ws_auth_from_query,
    ws_auth_from_cookie,
)
```

---

## Room

Chat/collaboration rooms with member management, roles, moderation, typing indicators, message history, and per-user rate limiting.

### RoomConfig

Configuration dataclass for rooms.

| Field                   | Type             | Default                               | Description                                                                                                                    |
| ----------------------- | ---------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `max_members`           | `int`            | 100                                   | Maximum concurrent members                                                                                                     |
| `history_size`          | `int`            | 100                                   | Maximum stored messages                                                                                                        |
| `require_auth`          | `bool`           | True                                  | Whether auth is required                                                                                                       |
| `allowed_message_types` | `frozenset[str]` | `{"text", "image", "file", "system"}` | Valid message types                                                                                                            |
| `rate_limit`            | `int`            | 30                                    | Messages per minute per user                                                                                                   |
| `max_message_length`    | `int`            | 65536                                 | Maximum message content length in bytes. `send_message()` raises `ValueError` if content exceeds this limit. Default is 64 KB. |

### RoomMember

Dataclass representing a room member.

| Field          | Type                | Description                             |
| -------------- | ------------------- | --------------------------------------- |
| `user_id`      | `str`               | Unique user identifier                  |
| `display_name` | `str`               | Display name in room                    |
| `role`         | `str`               | `"member"`, `"moderator"`, or `"admin"` |
| `joined_at`    | `float`             | Unix timestamp of join time             |
| `ws`           | `WebSocket \| None` | Associated WebSocket connection         |

### RoomMessage

Dataclass representing a message in a room.

| Field          | Type    | Description                 |
| -------------- | ------- | --------------------------- |
| `id`           | `str`   | Unique message ID           |
| `room_id`      | `str`   | Room the message belongs to |
| `user_id`      | `str`   | Sender's user ID            |
| `display_name` | `str`   | Sender's display name       |
| `content`      | `str`   | Message content             |
| `message_type` | `str`   | Type of message             |
| `timestamp`    | `float` | Unix timestamp              |
| `edited`       | `bool`  | Whether message was edited  |
| `deleted`      | `bool`  | Whether message was deleted |

**Methods:**

- `to_dict() -> dict[str, object]` -- Serialize to dict for transmission.

### Room class

```python
class Room:
    def __init__(self, room_id: str, layer: ChannelLayer, config: RoomConfig | None = None)
```

#### Basic usage

```python
from hyperdjango.channels import InMemoryChannelLayer
from hyperdjango.realtime import Room, RoomConfig

layer = InMemoryChannelLayer()

# Create room with custom config
config = RoomConfig(max_members=50, rate_limit=10)
room = Room("general", layer, config=config)

# Join
member = await room.join("user1", "Alice", role="admin")
member2 = await room.join("user2", "Bob")

# Send messages
msg = await room.send_message("user1", "Hello everyone!")
img_msg = await room.send_message("user1", "photo.jpg", message_type="image")

# System broadcast
await room.broadcast({"type": "system", "text": "Welcome to the room!"})

# Query members
members = room.get_members()  # list[RoomMember]
alice = room.get_member("user1")  # RoomMember | None

# History
history = room.get_history(limit=20)  # list[RoomMessage]

# Leave
await room.leave("user2")
```

#### Moderation

```python
# Kick (user can rejoin)
await room.kick("user2", reason="spam")

# Ban (user cannot rejoin until unbanned)
await room.ban("user3", reason="harassment")

# Check ban status
room.is_banned("user3")  # True

# Unban
await room.unban("user3")
```

#### Typing indicators

```python
await room.set_typing("user1", True)
typing_users = room.get_typing_users()  # ["user1"]

await room.set_typing("user1", False)
# Typing indicators auto-expire after 5 seconds
```

#### Methods reference

| Method                                                      | Returns              | Description                                                                                                    |
| ----------------------------------------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------- |
| `await join(user_id, display_name, ws=None, role="member")` | `RoomMember`         | Add member. Raises `PermissionError` if banned or full.                                                        |
| `await leave(user_id)`                                      | `bool`               | Remove member. Returns True if was present.                                                                    |
| `await send_message(user_id, content, message_type="text")` | `RoomMessage`        | Send message. Raises `PermissionError` if not member or rate limited. Raises `ValueError` if type not allowed. |
| `await broadcast(data)`                                     | `None`               | Broadcast system message to all members.                                                                       |
| `get_members()`                                             | `list[RoomMember]`   | Get all current members.                                                                                       |
| `get_member(user_id)`                                       | `RoomMember \| None` | Get member by user ID.                                                                                         |
| `get_history(limit=50)`                                     | `list[RoomMessage]`  | Get recent messages (newest last).                                                                             |
| `await kick(user_id, reason="")`                            | `bool`               | Kick user (can rejoin).                                                                                        |
| `await ban(user_id, reason="")`                             | `bool`               | Ban user (cannot rejoin).                                                                                      |
| `await unban(user_id)`                                      | `bool`               | Remove ban.                                                                                                    |
| `is_banned(user_id)`                                        | `bool`               | Check if user is banned.                                                                                       |
| `await set_typing(user_id, typing=True)`                    | `None`               | Set typing indicator.                                                                                          |
| `get_typing_users()`                                        | `list[str]`          | Get user IDs currently typing.                                                                                 |

---

## NotificationManager

User-targeted real-time notifications. Each user gets a personal channel `notifications:{user_id}`.

The `notification_type` parameter is validated against the `VALID_NOTIFICATION_TYPES` frozenset: `{"info", "warning", "error", "success"}`. Passing any other type raises `ValueError`.

### Notification

Dataclass for a notification.

| Field               | Type                        | Description                                   |
| ------------------- | --------------------------- | --------------------------------------------- |
| `id`                | `str`                       | Unique notification ID                        |
| `user_id`           | `str`                       | Target user ID                                |
| `title`             | `str`                       | Notification title                            |
| `body`              | `str`                       | Notification body                             |
| `notification_type` | `str`                       | `"info"`, `"warning"`, `"error"`, `"success"` |
| `data`              | `dict[str, object] \| None` | Optional payload                              |
| `read`              | `bool`                      | Read status                                   |
| `created_at`        | `float`                     | Unix timestamp                                |

**Methods:**

- `to_dict() -> dict[str, object]` -- Serialize to dict.

### NotificationManager class

```python
class NotificationManager:
    def __init__(self, layer: ChannelLayer, max_per_user: int = 1000)
```

| Parameter      | Default | Description                                                          |
| -------------- | ------- | -------------------------------------------------------------------- |
| `layer`        | --      | The channel layer for pub/sub                                        |
| `max_per_user` | 1000    | Maximum notifications stored per user (oldest evicted when exceeded) |

#### Basic usage

```python
from hyperdjango.channels import InMemoryChannelLayer
from hyperdjango.realtime import NotificationManager

layer = InMemoryChannelLayer()
notifier = NotificationManager(layer)

# Send to one user
n = await notifier.send("user1", "Welcome", "You have joined!", "info")

# Send with extra data
n = await notifier.send(
    "user1",
    "Order Shipped",
    "Your order is on the way",
    "success",
    data={"order_id": "12345", "tracking": "XYZ"},
)

# Send to multiple users
notifications = await notifier.send_many(
    ["user1", "user2", "user3"], "System Update", "New version deployed"
)

# Broadcast to all subscribed users
await notifier.broadcast_all("Maintenance", "Server restart in 5 minutes", "warning")
```

#### Subscribe to notifications

```python
def my_callback(msg):
    print(f"Notification: {msg.data}")


sub_id = notifier.subscribe("user1", my_callback)
# ... later
notifier.unsubscribe("user1", sub_id)
```

#### Read management

```python
unread = notifier.get_unread("user1")  # list[Notification]
notifier.mark_read("user1", notification.id)  # True/False
count = notifier.mark_all_read("user1")  # int (count marked)
cleared = notifier.clear("user1")  # int (count cleared)
```

#### Methods reference

| Method                                                                  | Returns              | Description                                        |
| ----------------------------------------------------------------------- | -------------------- | -------------------------------------------------- |
| `await send(user_id, title, body, notification_type="info", data=None)` | `Notification`       | Send to one user.                                  |
| `await send_many(user_ids, title, body, notification_type="info")`      | `list[Notification]` | Send to multiple users.                            |
| `await broadcast_all(title, body, notification_type="info")`            | `Notification`       | Broadcast to all.                                  |
| `subscribe(user_id, callback)`                                          | `int`                | Subscribe to user's notifications. Returns sub ID. |
| `unsubscribe(user_id, sub_id)`                                          | `bool`               | Unsubscribe.                                       |
| `get_unread(user_id)`                                                   | `list[Notification]` | Get unread notifications.                          |
| `mark_read(user_id, notification_id)`                                   | `bool`               | Mark one as read.                                  |
| `mark_all_read(user_id)`                                                | `int`                | Mark all as read. Returns count.                   |
| `clear(user_id)`                                                        | `int`                | Clear all notifications. Returns count.            |

---

## LiveQuery

Subscribe to model changes and receive real-time updates. Integrates with the signal system (post_save, post_delete) to detect changes and push to subscribers.

### ModelChange

Dataclass describing a model change event.

| Field            | Type                        | Description                           |
| ---------------- | --------------------------- | ------------------------------------- |
| `model_name`     | `str`                       | Name of the model                     |
| `action`         | `str`                       | `"create"`, `"update"`, or `"delete"` |
| `pk`             | `int \| str`                | Primary key                           |
| `data`           | `dict[str, object] \| None` | Serialized fields (create/update)     |
| `changed_fields` | `list[str] \| None`         | Changed field names (update only)     |
| `timestamp`      | `float`                     | Unix timestamp                        |

### LiveQuery class

```python
class LiveQuery:
    def __init__(self, layer: ChannelLayer)
```

#### Basic usage

```python
from hyperdjango.channels import InMemoryChannelLayer
from hyperdjango.realtime import LiveQuery

layer = InMemoryChannelLayer()
live = LiveQuery(layer)

# Watch all changes to a model
sub_id = live.watch("Post")

# Watch with filters (only matching changes delivered)
sub_id = live.watch("Comment", filters={"post_id": 42})

# Unwatch
live.unwatch(sub_id)
```

#### Change handlers

```python
@live.on_change("Post")
async def handle_post_change(change: ModelChange):
    if change.action == "create":
        print(f"New post: {change.data['title']}")
    elif change.action == "update":
        print(f"Post {change.pk} updated fields: {change.changed_fields}")
    elif change.action == "delete":
        print(f"Post {change.pk} deleted")
```

#### Notifying changes (from signal handlers)

```python
# In your signal handler or save/delete logic:
await live.notify_create("Post", post.id, {"title": post.title, "body": post.body})
await live.notify_update("Post", post.id, {"title": post.title}, ["title"])
await live.notify_delete("Post", post.id)
```

#### Methods reference

| Method                                                      | Returns   | Description                         |
| ----------------------------------------------------------- | --------- | ----------------------------------- |
| `watch(model_name, filters=None)`                           | `str`     | Subscribe. Returns subscription ID. |
| `unwatch(subscription_id)`                                  | `bool`    | Unsubscribe.                        |
| `on_change(model_name)`                                     | decorator | Register change handler.            |
| `await notify_create(model_name, pk, data)`                 | `None`    | Notify of creation.                 |
| `await notify_update(model_name, pk, data, changed_fields)` | `None`    | Notify of update.                   |
| `await notify_delete(model_name, pk)`                       | `None`    | Notify of deletion.                 |

---

## ConnectionManager

WebSocket connection lifecycle management. Tracks all active connections, provides user-to-connection mapping, and connect/disconnect hooks.

### ConnectionInfo

Dataclass for an active connection.

| Field           | Type                | Description                                |
| --------------- | ------------------- | ------------------------------------------ |
| `connection_id` | `str`               | Unique connection ID                       |
| `user_id`       | `str \| None`       | Authenticated user ID (None for anonymous) |
| `ws`            | `WebSocket`         | The WebSocket instance                     |
| `connected_at`  | `float`             | Unix timestamp                             |
| `rooms`         | `set[str]`          | Rooms this connection belongs to           |
| `metadata`      | `dict[str, object]` | Custom metadata                            |

### ConnectionManager class

```python
class ConnectionManager:
    def __init__(self, layer: ChannelLayer, max_connections: int = 10000, max_metadata_keys: int = 32)
```

| Parameter           | Default | Description                                                                                                                                                                  |
| ------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `layer`             | --      | The channel layer for pub/sub                                                                                                                                                |
| `max_connections`   | 10000   | Maximum concurrent connections. `connect()` raises `ConnectionError` when exceeded.                                                                                          |
| `max_metadata_keys` | 32      | Maximum number of keys allowed in the `metadata` dict passed to `connect()`. Raises `ValueError` if exceeded. Prevents unbounded memory consumption from oversized metadata. |

#### Basic usage

```python
from hyperdjango.channels import InMemoryChannelLayer
from hyperdjango.realtime import ConnectionManager

layer = InMemoryChannelLayer()
mgr = ConnectionManager(layer)

# Register connection
info = await mgr.connect(ws, user_id="user1", metadata={"device": "mobile"})

# Query connections
conn = mgr.get_connection(info.connection_id)
user_conns = mgr.get_user_connections("user1")
all_conns = mgr.get_all_connections()
count = mgr.connection_count

# Send messages
await mgr.send_to_connection(info.connection_id, {"type": "welcome"})
sent = await mgr.send_to_user("user1", {"type": "update"})  # returns count
total = await mgr.broadcast({"type": "announcement"})  # returns count

# Disconnect
await mgr.disconnect(info.connection_id)
```

#### Lifecycle hooks

```python
async def on_connect(info: ConnectionInfo):
    print(f"Connected: {info.user_id}")


async def on_disconnect(info: ConnectionInfo):
    print(f"Disconnected: {info.user_id}")


mgr.on_connect = on_connect
mgr.on_disconnect = on_disconnect
```

#### Methods reference

| Method                                           | Returns                  | Description                                                    |
| ------------------------------------------------ | ------------------------ | -------------------------------------------------------------- |
| `await connect(ws, user_id=None, metadata=None)` | `ConnectionInfo`         | Register connection. Raises `ConnectionError` if max exceeded. |
| `await disconnect(connection_id)`                | `bool`                   | Unregister. Returns True if found.                             |
| `get_connection(connection_id)`                  | `ConnectionInfo \| None` | Get by ID.                                                     |
| `get_user_connections(user_id)`                  | `list[ConnectionInfo]`   | Get all for user.                                              |
| `get_all_connections()`                          | `list[ConnectionInfo]`   | Get all active.                                                |
| `connection_count`                               | `int`                    | Property: number of active connections.                        |
| `await send_to_connection(connection_id, data)`  | `bool`                   | Send to one connection.                                        |
| `await send_to_user(user_id, data)`              | `int`                    | Send to all user connections. Returns count.                   |
| `await broadcast(data)`                          | `int`                    | Send to all connections. Returns count.                        |

---

## WebSocketRateLimiter

Per-connection rate limiting using a token bucket algorithm combined with sliding window counters.

### WSRateLimitConfig

| Field                 | Type  | Default | Description                 |
| --------------------- | ----- | ------- | --------------------------- |
| `messages_per_second` | `int` | 10      | Max messages per second     |
| `messages_per_minute` | `int` | 120     | Max messages per minute     |
| `burst_size`          | `int` | 20      | Token bucket burst capacity |

### WebSocketRateLimiter class

```python
class WebSocketRateLimiter:
    def __init__(self, config: WSRateLimitConfig | None = None)
```

#### Usage

```python
from hyperdjango.realtime import WebSocketRateLimiter, WSRateLimitConfig

# Default config
limiter = WebSocketRateLimiter()

# Custom config
config = WSRateLimitConfig(messages_per_second=5, messages_per_minute=60, burst_size=10)
limiter = WebSocketRateLimiter(config)

# Check if message is allowed
if limiter.check("connection_123"):
    process(message)
else:
    await ws.send_json({"error": "rate_limited"})

# Get stats
stats = limiter.get_stats("connection_123")
# {"tokens_remaining": 7.5, "per_second_count": 3, "per_minute_count": 15,
#  "burst_size": 10, "messages_per_second": 5, "messages_per_minute": 60}

# Reset on disconnect
limiter.reset("connection_123")
```

#### Methods reference

| Method                                  | Returns                   | Description                                                                                                                                                          |
| --------------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check(connection_id)`                  | `bool`                    | True if message allowed. Consumes one token.                                                                                                                         |
| `reset(connection_id)`                  | `None`                    | Reset state for connection.                                                                                                                                          |
| `cleanup_stale(max_idle_seconds=300.0)` | `int`                     | Remove token buckets idle longer than `max_idle_seconds`. Returns count removed. Call periodically to prevent unbounded memory growth from disconnected connections. |
| `get_stats(connection_id)`              | `dict[str, int \| float]` | Get rate limit stats.                                                                                                                                                |

---

## Auth Utilities

WebSocket authentication helpers.

> **CSWSH (Cross-Site WebSocket Hijacking) defense.** Both `@ws_authenticated`
> and the `@guard`-based WebSocket path validate the handshake `Origin` before
> authenticating. Same-origin connections are always allowed; a cross-origin
> handshake is refused with close code **4403** unless its Origin is listed in
> the `WS_ALLOWED_ORIGINS` setting (`[]` by default = same-origin only, `"*"` =
> any). Connections with no `Origin` header (native/CLI clients, which don't carry
> a browser's ambient cookies) are allowed.

### ws_authenticated decorator

```python
from hyperdjango.realtime import ws_authenticated


@app.websocket("/ws/chat")
@ws_authenticated
async def chat(ws, user_id):
    await ws.accept()
    # user_id is the authenticated user
    ...
```

Tries authentication in order:

1. Query string: `?token=<value>`
2. Cookie: `sessionid=<value>`

Closes the WebSocket with code 4001 if no auth is found.

### ws_auth_from_query

```python
from hyperdjango.realtime import ws_auth_from_query

token = ws_auth_from_query(ws)  # str | None
# Extracts ?token=... from query string
```

### ws_auth_from_cookie

```python
from hyperdjango.realtime import ws_auth_from_cookie

session = ws_auth_from_cookie(ws)  # default: "sessionid"
session = ws_auth_from_cookie(ws, cookie_name="my_sess")  # custom cookie name
```

---

## Full Example: Chat Application

```python
from hyperdjango import HyperApp
from hyperdjango.channels import InMemoryChannelLayer
from hyperdjango.realtime import (
    Room,
    RoomConfig,
    ConnectionManager,
    WebSocketRateLimiter,
    ws_authenticated,
)

app = HyperApp("chat")
layer = InMemoryChannelLayer()
rooms: dict[str, Room] = {}
connections = ConnectionManager(layer)
limiter = WebSocketRateLimiter()


def get_room(room_id: str) -> Room:
    if room_id not in rooms:
        rooms[room_id] = Room(room_id, layer, RoomConfig(max_members=50))
    return rooms[room_id]


@app.websocket("/ws/chat/{room_id}")
@ws_authenticated
async def chat(ws, user_id, room_id: str):
    await ws.accept()
    conn = await connections.connect(ws, user_id=user_id)
    room = get_room(room_id)
    await room.join(user_id, user_id, ws=ws)
    conn.rooms.add(room_id)

    try:
        async for text in ws.iter_text():
            if not limiter.check(conn.connection_id):
                await ws.send_json({"error": "rate_limited"})
                continue
            await room.send_message(user_id, text)
    finally:
        await room.leave(user_id)
        await connections.disconnect(conn.connection_id)
```

The loop above only handles the **inbound** direction (client → room). To
deliver **outbound** messages (room → this client), subscribe to the room's
channel and pump messages to the socket. Do this with a _cooperative_
bridge — never a blocking thread-queue get — so it works under both the
default and shared event-loop server models (see
[server.md](server.md#concurrency-model-and-how-to-write-handlers-that-scale)):

```python
import asyncio, contextlib


@app.websocket("/ws/chat/{room_id}")
@ws_authenticated
async def chat(ws, user_id, room_id: str):
    await ws.accept()
    room = get_room(room_id)
    await room.join(user_id, user_id, ws=ws)

    # Outbound: channel callbacks fire from other threads, so hop each onto
    # THIS connection's loop with call_soon_threadsafe into an asyncio.Queue.
    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def on_msg(msg):
        with contextlib.suppress(RuntimeError):  # loop closing/closed
            loop.call_soon_threadsafe(outgoing.put_nowait, msg)

    sub_id = room._channel.subscribe(on_msg)

    async def writer():
        while True:
            msg = await outgoing.get()
            await ws.send_json(msg.data)

    w = asyncio.create_task(writer())
    try:
        async for text in ws.iter_text():  # inbound
            await room.send_message(user_id, text)
    except WebSocketDisconnect:
        pass
    finally:
        w.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await w
        room._channel.unsubscribe(sub_id)
        await room.leave(user_id)
```

!!! warning "Do not park a thread per connection"
A blocking bridge like `await loop.run_in_executor(None, thread_queue.get)`
dedicates one OS thread to each connection. Under the **default** shared
event-loop model that exhausts the loop's executor and stalls delivery.
Always use the `asyncio.Queue` + `call_soon_threadsafe` pattern above (or
the framework's `websocket_channel_handler` / `Room`, which already do).
The `services/websocket_chat` app is the full, working reference. If a
handler genuinely must do heavy synchronous per-message work, opt that
deployment out with `WEBSOCKET_CONCURRENCY=thread` — see
[server.md](server.md#concurrency-model-and-how-to-write-handlers-that-scale).

---

## Thread Safety

All real-time components are thread-safe for Python 3.14t free-threading:

- **Room**: All member, typing indicator, rate count, and history operations acquire a `threading.Lock`. Message sending uses lock-protected member lookup to prevent TOCTOU races (e.g., a member leaving between permission check and message dispatch).
- **NotificationManager**: Notification storage and read state management are protected by a `threading.Lock`.
- **ConnectionManager**: Connection registration, user mapping, and lifecycle hooks are all lock-protected.
- **WebSocketRateLimiter**: Per-operation locking on all bucket access. The `check()`, `reset()`, `cleanup_stale()`, and `get_stats()` methods each acquire the lock for their entire operation.

---

## See Also

- [channels.md](channels.md) -- Lower-level Channel, ChannelGroup, ChannelLayer (the foundation for all real-time patterns)
- [tasks.md](tasks.md) -- Background task queue (for deferred work triggered by real-time events)
- [i18n.md](i18n.md) -- Internationalization (for localized notification content)
