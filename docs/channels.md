# WebSocket Pub/Sub Channels

Named channels with subscribe, publish, broadcast, presence tracking, message history, per-channel authentication, and fan-out groups. HyperDjango channels provide real-time communication with two backend options: `InMemoryChannelLayer` for single-process applications and `PgChannelLayer` for cross-process pub/sub via PostgreSQL LISTEN/NOTIFY.

## Quick Start

```python
from hyperdjango.channels import InMemoryChannelLayer

layer = InMemoryChannelLayer()

# Create a channel
chat = layer.channel("chat:general")


# Subscribe
def on_message(msg):
    print(f"[{msg.sender}] {msg.data['text']}")


chat.subscribe(on_message)

# Publish
await chat.publish({"text": "Hello!"}, sender="alice")
```

## Channel Layers

A channel layer is the transport backend. It manages channel creation, message routing, and (for `PgChannelLayer`) cross-process delivery.

### InMemoryChannelLayer

Zero-latency, thread-safe, single-process. Messages are delivered directly to local subscribers via function calls. Ideal for development, testing, and single-server deployments.

```python
from hyperdjango.channels import InMemoryChannelLayer

layer = InMemoryChannelLayer(
    default_history_size=100,  # Default message history buffer per channel
)
```

Parameters:

- `default_history_size` -- how many recent messages to retain per channel for late joiners (default: 100). Set to 0 to disable history.

All state is in-memory. Restarting the process clears all channels, subscriptions, presence, and history.

### PgChannelLayer

Cross-process pub/sub via PostgreSQL LISTEN/NOTIFY. Messages published on one process are delivered to subscribers on all connected processes. Uses HyperDjango's native pg.zig driver for maximum throughput.

```python
from hyperdjango.channels import PgChannelLayer

layer = PgChannelLayer(
    database_url="postgres://localhost/mydb",
    default_history_size=100,
)
await layer.connect()

# Use channels normally -- messages cross process boundaries
chat = layer.channel("chat:general")
await chat.publish({"text": "Hello from process A!"}, sender="alice")

# When shutting down:
await layer.disconnect()
```

Parameters:

- `database_url` -- PostgreSQL connection string
- `default_history_size` -- message history buffer per channel (default: 100)

#### How It Works

1. When a channel is created, the layer issues `LISTEN channel_name` on a dedicated listener connection opened from the layer's `database_url`.
2. When a message is published, the layer issues `NOTIFY channel_name, 'payload'`.
3. PostgreSQL fans out the notification to all connections listening on that channel.
4. Each process receives the notification and delivers it to local subscribers.

#### Delivery Semantics

Cross-process delivery is a **lossy, wake-up-only signal — not a durable bus.**
Treat a delivered message as _"something changed, go read"_:

- A NOTIFY can be dropped — the per-subscriber bridge queue is bounded and drops
  on overflow, and a notification can be lost across a listener-connection blip.
- **If you need every event, keep a durable ledger** (an append-only table + a
  monotonic cursor) as the source of truth and use this layer only to wake
  readers. Don't rely on receiving every NOTIFY.

Within a single process, delivery is **exactly-once**. `publish()` delivers to
local subscribers inline _and_ fires a NOTIFY for other processes. The publishing
node's own listener also receives that NOTIFY, but each payload carries an origin
id (`_origin_id`) stamped into an envelope, so the node recognizes and drops the
echo of its own publish — a local subscriber never sees the same message twice.
This de-dup is automatic; you don't configure anything.

#### Large Message Handling

PostgreSQL NOTIFY has an 8000-byte payload limit. Messages exceeding 7500 bytes (after JSON serialization) are automatically handled:

1. The message body is inserted into an UNLOGGED staging table.
2. The NOTIFY payload contains only a reference ID.
3. Receiving processes fetch the full message body from the staging table.
4. Staging rows are cleaned up after delivery.

This is transparent -- publish and subscribe code does not change regardless of message size.

#### Connection Lifecycle

```python
layer = PgChannelLayer(database_url="postgres://localhost/mydb")

# connect() establishes the PG connection and starts the LISTEN loop
await layer.connect()

# ... use channels ...

# disconnect() stops listening, drains pending deliveries, closes connection
await layer.disconnect()
```

Always call `disconnect()` during application shutdown to avoid orphaned connections.

## Channel API

A channel is a named message bus. Create channels from a layer:

```python
channel = layer.channel("chat:room1")
channel = layer.channel("events", max_history=500)
```

Parameters:

- `name` -- channel name (any string, typically namespaced with `:`)
- `max_history` -- override the layer's default history size for this channel

### subscribe()

Register a callback to receive messages on this channel. Returns a subscription ID for later unsubscription.

```python
# Sync callback
sub_id = channel.subscribe(callback)

# Async callback
sub_id = channel.subscribe(async_callback)

# With filter (only receive matching messages)
sub_id = channel.subscribe(callback, filter_fn=lambda msg: msg.data.get("important"))

# With user ID (for auth check and presence)
sub_id = channel.subscribe(callback, user_id="alice")
```

Parameters:

- `callback` -- function or coroutine called with a `Message` for each published message
- `filter_fn` -- optional predicate `(Message) -> bool`. If provided, the callback is only called when the filter returns `True`.
- `user_id` -- optional user identifier. If the channel has an `auth_fn`, this is passed for authorization. Also used for presence tracking.

Returns an integer `sub_id`.

If the channel has an `auth_fn` and the user is not authorized, `subscribe()` raises `PermissionError`.

!!! warning "Sync callbacks run on the publisher's thread"
A **sync** `callback` is invoked inline on whatever thread called
`publish()` — which is usually a _different_ connection's worker/loop
thread, not the subscriber's. If the callback needs to deliver to a
WebSocket, do **not** touch that connection's event loop or objects
directly from the callback. Instead hop onto the subscriber's loop:

    ```python
    loop = asyncio.get_running_loop()  # the subscriber's loop
    outgoing: asyncio.Queue = asyncio.Queue(maxsize=1000)


    def on_message(msg):  # runs on the PUBLISHER's thread
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(outgoing.put_nowait, msg)


    channel.subscribe(on_message)
    # a writer task drains `outgoing` and calls await ws.send_json(...)
    ```

    Keep sync callbacks cheap and non-blocking — never park a thread in one
    (e.g. a blocking `queue.get`), which breaks the shared event-loop server
    model. See [realtime.md](realtime.md) and
    [server.md](server.md#concurrency-model-and-how-to-write-handlers-that-scale).

### unsubscribe()

Remove a subscription by ID.

```python
channel.unsubscribe(sub_id)
```

After unsubscribing, the callback will not receive any further messages. If `user_id` was provided on subscribe, presence is also removed.

### publish()

Send a message to all subscribers on this channel.

```python
await channel.publish(data, sender="alice")
await channel.publish({"text": "Hello!", "type": "chat"}, sender="alice")
await channel.publish("simple string data")
```

Parameters:

- `data` -- the message payload (any JSON-serializable value: dict, list, str, int, etc.)
- `sender` -- optional sender identifier (included in the `Message` object)

For `InMemoryChannelLayer`, callbacks are invoked synchronously (sync callbacks) or scheduled as tasks (async callbacks). For `PgChannelLayer`, the message is sent via NOTIFY and delivered asynchronously to all processes.

The message is also added to the channel's history buffer (if history is enabled).

### subscriber_count()

Return the number of active subscribers on this channel.

```python
count = channel.subscriber_count()
```

## ChannelGroup API

Groups provide fan-out: publish once, deliver to multiple channels. Useful for broadcasting to all users, all rooms, or all devices.

```python
group = layer.group("notifications")

# Add channels to the group
group.add("user:1")
group.add("user:2")
group.add("user:3")

# Publish to all channels in the group
await group.publish({"type": "alert", "text": "System maintenance at 2am"})
# Delivered to user:1, user:2, user:3

# Remove from group
group.discard("user:2")

# Query group state
members = group.members()  # {"user:1", "user:3"}
count = group.size()  # 2
```

### group.add(channel_name)

Add a channel to the group. If the channel doesn't exist yet, it is created when a message is published.

### group.discard(channel_name)

Remove a channel from the group. Safe to call even if the channel is not in the group.

### group.publish(data, sender=None)

Publish a message to all channels in the group. Each channel's subscribers receive the message independently.

### group.members()

Return a set of channel names currently in the group.

### group.size()

Return the number of channels in the group.

## Presence Tracking

Track who is connected to each channel in real-time. Presence is per-channel and managed explicitly with `join()` and `leave()`.

```python
channel.join("user42", metadata={"name": "Alice", "avatar": "/img/alice.png"})
channel.join("user43", metadata={"name": "Bob"})

# Get all present members
members = channel.presence()
# [
#   {"user_id": "user42", "name": "Alice", "avatar": "/img/alice.png", "joined_at": 1234567890.0},
#   {"user_id": "user43", "name": "Bob", "joined_at": 1234567891.0},
# ]

# Count
count = channel.presence_count()  # 2

# Leave
channel.leave("user42")

# Clear all presence
channel.clear_presence()
```

### channel.join(user_id, metadata=None)

Mark a user as present in the channel. The `metadata` dict is merged into the presence record and returned by `presence()`.

If the user is already present, their metadata is updated and `joined_at` remains unchanged.

### channel.leave(user_id)

Remove a user from the channel's presence. No-op if the user is not present.

### channel.presence()

Return a list of dicts, one per present user. Each dict contains:

- `user_id` -- the user identifier passed to `join()`
- `joined_at` -- Unix timestamp of when the user joined
- All keys from `metadata`

### channel.presence_count()

Return the number of currently present users.

### channel.clear_presence()

Remove all users from presence. Useful for channel cleanup.

## Message History

Recent messages are stored in a bounded ring buffer per channel. Late joiners can retrieve history to catch up on missed messages.

```python
channel = layer.channel("events", max_history=200)

await channel.publish({"event": "deploy", "version": "1.2.3"}, sender="ci")
await channel.publish({"event": "deploy", "version": "1.2.4"}, sender="ci")

# Get recent messages
recent = channel.history(limit=50)  # Last 50 messages (oldest first)
# [Message(channel="events", data={"event": "deploy", ...}, sender="ci", timestamp=...), ...]

# Clear history
channel.clear_history()
```

### channel.history(limit=None)

Return a list of `Message` objects from the history buffer, oldest first. If `limit` is specified, return only the most recent N messages. If `limit` is None, return all messages in the buffer.

### channel.clear_history()

Remove all messages from the history buffer.

### History Buffer Behavior

The history buffer is a fixed-size ring buffer. When it reaches capacity (`max_history`), the oldest message is dropped to make room for the new one. This means history always contains the most recent N messages.

## Per-Channel Authentication

Restrict who can subscribe to a channel with an `auth_fn` callback.

```python
def check_access(channel_name, user_id):
    """Only admins can access private channels."""
    if channel_name.startswith("private:"):
        return user_id in admin_ids
    return True


channel = layer.channel("private:staff", auth_fn=check_access)

# Authorized user: OK
channel.subscribe(callback, user_id="admin1")

# Unauthorized user: raises PermissionError
channel.subscribe(callback, user_id="hacker")
# PermissionError: User 'hacker' not authorized for channel 'private:staff'
```

The `auth_fn` has the signature `(channel_name: str, user_id: str) -> bool`. Return `True` to allow, `False` to deny.

Auth is checked at subscribe time only. Changing permissions after subscription does not affect existing subscribers. To revoke access, unsubscribe the user.

### Async Auth Functions

For auth checks that require database lookups:

```python
async def check_membership(channel_name, user_id):
    """Only members of the room can subscribe."""
    room_id = channel_name.split(":")[1]
    db = get_db()
    row = await db.query(
        "SELECT 1 FROM room_members WHERE room_id = $1 AND user_id = $2",
        int(room_id),
        user_id,
    )
    return len(row) > 0


channel = layer.channel("room:42", auth_fn=check_membership)
```

## Filtered Subscriptions

Receive only messages matching a filter predicate. The filter function receives a `Message` and returns `True` to deliver, `False` to skip.

```python
# Only error-level messages
channel.subscribe(
    handle_error,
    filter_fn=lambda msg: msg.data.get("level") == "error",
)

# Only from a specific sender
channel.subscribe(
    handle_system,
    filter_fn=lambda msg: msg.sender == "system",
)

# Only messages with a specific type
channel.subscribe(
    handle_chat,
    filter_fn=lambda msg: msg.data.get("type") == "chat",
)
```

Filters are evaluated per-subscriber. A message published once can be delivered to some subscribers and skipped for others based on their individual filters.

## WebSocket Integration

Bridge WebSocket connections to channels with `websocket_channel_handler`. This is the standard way to connect browser clients to the channel system.

### @app.websocket Decorator

```python
from hyperdjango.channels import websocket_channel_handler


@app.websocket("/ws/chat/{room}")
async def chat(ws):
    await ws.accept()
    room = ws.path_params["room"]
    channel = layer.channel(f"chat:{room}")
    await websocket_channel_handler(
        ws,
        channel,
        user_id="user42",
        on_connect=lambda ws, ch: ch.join("user42", metadata={"name": "Alice"}),
        on_disconnect=lambda ws, ch: ch.leave("user42"),
    )
```

### websocket_channel_handler()

```python
await websocket_channel_handler(
    ws,  # WebSocket connection
    channel,  # Channel object
    user_id=None,  # User ID for auth and presence
    on_connect=None,  # Callback(ws, channel) on connect
    on_disconnect=None,  # Callback(ws, channel) on disconnect
    on_message=None,  # Custom message handler(text, channel, ws)
    send_history=True,  # Send message history to new connections
)
```

The handler automatically:

1. Subscribes the WebSocket to the channel
2. Joins presence tracking (if `user_id` is provided)
3. Sends message history to the new subscriber (if `send_history=True`)
4. Forwards messages bidirectionally: WebSocket text frames are published to the channel, channel messages are sent as WebSocket text frames
5. Cleans up on disconnect: unsubscribes from channel, leaves presence

### Custom Message Handling

Override the default "forward everything" behavior with `on_message`:

```python
async def handle_message(text, channel, ws):
    """Custom message handler with command parsing."""
    data = json.loads(text)

    if data.get("type") == "typing":
        # Don't publish typing indicators to the channel
        return

    if data.get("type") == "dm":
        # Direct message to a specific user
        target = layer.channel(f"user:{data['target']}")
        await target.publish(data, sender="user42")
        return

    # Default: publish to the channel
    await channel.publish(data, sender="user42")


await websocket_channel_handler(ws, channel, on_message=handle_message)
```

## Global Layer

Set a global channel layer accessible from anywhere in the application:

```python
from hyperdjango.channels import set_channel_layer, get_channel_layer

# At startup
layer = InMemoryChannelLayer()
set_channel_layer(layer)

# Anywhere in your app
layer = get_channel_layer()
channel = layer.channel("events")
await channel.publish({"type": "order_created", "order_id": 42})
```

## Message Format

Messages are `Message` dataclass instances with JSON serialization:

```python
from hyperdjango.channels import Message

msg = Message(
    channel="chat:general",
    data={"text": "Hello!", "type": "chat"},
    sender="alice",
)

# Serialization
json_str = msg.to_json()
# '{"channel": "chat:general", "data": {"text": "Hello!", "type": "chat"}, "sender": "alice", "timestamp": 1711468200.123}'

# Deserialization
restored = Message.from_json(json_str)
assert restored.data["text"] == "Hello!"
assert restored.sender == "alice"
```

`Message` fields:

- `channel` -- channel name (str)
- `data` -- message payload (any JSON-serializable value)
- `sender` -- sender identifier (str or None)
- `timestamp` -- Unix timestamp, auto-set on creation (float)

## Example: Chat Room

A complete chat room with presence, history, and authentication:

```python
from hyperdjango import HyperApp
from hyperdjango.channels import (
    InMemoryChannelLayer,
    websocket_channel_handler,
    set_channel_layer,
)

app = HyperApp(title="Chat")
layer = InMemoryChannelLayer(default_history_size=200)
set_channel_layer(layer)


def chat_auth(channel_name, user_id):
    """All authenticated users can join chat rooms."""
    return user_id is not None


@app.websocket("/ws/chat/{room}")
async def chat_ws(ws):
    await ws.accept()
    room = ws.path_params["room"]
    user = ws.query_params.get("user", "anonymous")
    channel = layer.channel(f"chat:{room}", auth_fn=chat_auth)

    async def on_connect(ws, ch):
        ch.join(user, metadata={"name": user})
        await ch.publish({"type": "system", "text": f"{user} joined"}, sender="system")

    async def on_disconnect(ws, ch):
        ch.leave(user)
        await ch.publish({"type": "system", "text": f"{user} left"}, sender="system")

    await websocket_channel_handler(
        ws,
        channel,
        user_id=user,
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )


@app.get("/chat/{room}/presence")
async def get_presence(request):
    room = request.path_params["room"]
    channel = layer.channel(f"chat:{room}")
    return {"members": channel.presence(), "count": channel.presence_count()}
```

## Example: Real-Time Notifications

Push notifications to specific users via per-user channels:

```python
layer = InMemoryChannelLayer()
set_channel_layer(layer)
notifications_group = layer.group("all_notifications")


@app.websocket("/ws/notifications")
async def notification_ws(ws):
    await ws.accept()
    user_id = ws.query_params["user_id"]
    channel = layer.channel(f"notifications:{user_id}")
    notifications_group.add(f"notifications:{user_id}")

    await websocket_channel_handler(ws, channel, user_id=user_id)
    # On disconnect, remove from group
    notifications_group.discard(f"notifications:{user_id}")


# Send notification to a specific user (from anywhere in the app)
async def notify_user(user_id, title, body):
    layer = get_channel_layer()
    channel = layer.channel(f"notifications:{user_id}")
    await channel.publish({"title": title, "body": body, "type": "notification"})


# Broadcast to all connected users
async def broadcast_announcement(text):
    layer = get_channel_layer()
    group = layer.group("all_notifications")
    await group.publish({"title": "Announcement", "body": text, "type": "announcement"})
```

## Example: Live Dashboard

Stream real-time metrics to a dashboard using channels:

```python
import asyncio

layer = InMemoryChannelLayer(default_history_size=60)
set_channel_layer(layer)


async def metrics_publisher():
    """Background task that publishes metrics every second."""
    channel = layer.channel("metrics:system")
    while True:
        metrics = {
            "cpu": get_cpu_usage(),
            "memory": get_memory_usage(),
            "requests_per_sec": get_rps(),
            "active_connections": get_active_connections(),
            "db_pool_used": get_pool_stats()["used"],
        }
        await channel.publish(metrics, sender="monitor")
        await asyncio.sleep(1)


@app.on_startup
async def start_metrics():
    asyncio.create_task(metrics_publisher())


@app.websocket("/ws/dashboard")
async def dashboard_ws(ws):
    await ws.accept()
    channel = layer.channel("metrics:system")
    await websocket_channel_handler(
        ws,
        channel,
        send_history=True,  # New connections get last 60 seconds of metrics
    )


@app.get("/api/metrics/snapshot")
async def metrics_snapshot(request):
    """REST endpoint for non-WebSocket clients."""
    channel = layer.channel("metrics:system")
    history = channel.history(limit=60)
    return {"metrics": [msg.data for msg in history]}
```

## API Reference

### Channel Layer

| Method                      | Description                                   |
| --------------------------- | --------------------------------------------- |
| `layer.channel(name, **kw)` | Get or create a named channel                 |
| `layer.group(name)`         | Get or create a named group                   |
| `await layer.connect()`     | Connect to backend (PgChannelLayer only)      |
| `await layer.disconnect()`  | Disconnect from backend (PgChannelLayer only) |

### Channel

| Method                                                      | Description                         |
| ----------------------------------------------------------- | ----------------------------------- |
| `channel.subscribe(callback, filter_fn=None, user_id=None)` | Subscribe, returns sub_id           |
| `channel.unsubscribe(sub_id)`                               | Remove a subscription               |
| `await channel.publish(data, sender=None)`                  | Publish a message                   |
| `channel.subscriber_count()`                                | Number of active subscribers        |
| `channel.join(user_id, metadata=None)`                      | Join presence                       |
| `channel.leave(user_id)`                                    | Leave presence                      |
| `channel.presence()`                                        | List of present users with metadata |
| `channel.presence_count()`                                  | Number of present users             |
| `channel.clear_presence()`                                  | Remove all presence                 |
| `channel.history(limit=None)`                               | Get message history                 |
| `channel.clear_history()`                                   | Clear message history               |

### ChannelGroup

| Method                                   | Description                       |
| ---------------------------------------- | --------------------------------- |
| `group.add(channel_name)`                | Add a channel to the group        |
| `group.discard(channel_name)`            | Remove a channel from the group   |
| `await group.publish(data, sender=None)` | Publish to all channels in group  |
| `group.members()`                        | Set of channel names in the group |
| `group.size()`                           | Number of channels in the group   |

### Message

| Field                  | Type          | Description                  |
| ---------------------- | ------------- | ---------------------------- |
| `channel`              | `str`         | Channel name                 |
| `data`                 | `object`      | Message payload              |
| `sender`               | `str \| None` | Sender identifier            |
| `timestamp`            | `float`       | Unix timestamp               |
| `to_json()`            | `str`         | Serialize to JSON string     |
| `Message.from_json(s)` | `Message`     | Deserialize from JSON string |

### Utilities

| Function                                       | Description                     |
| ---------------------------------------------- | ------------------------------- |
| `set_channel_layer(layer)`                     | Set the global channel layer    |
| `get_channel_layer()`                          | Get the global channel layer    |
| `websocket_channel_handler(ws, channel, **kw)` | Bridge a WebSocket to a channel |
