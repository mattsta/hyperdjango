"""
WebSocket pub/sub channels — named channels with subscribe, publish, broadcast.

Two-layer architecture:
1. **Channel** — named topic with subscribers, presence tracking, message history
2. **ChannelLayer** — backend for message transport (InMemory or PostgreSQL LISTEN/NOTIFY)

InMemoryChannelLayer: single-process, thread-safe, zero-latency
PgChannelLayer: multi-process via PostgreSQL LISTEN/NOTIFY, cross-server broadcast

Usage:
    from hyperdjango.channels import (
        Channel, ChannelGroup, InMemoryChannelLayer, PgChannelLayer,
        get_channel_layer, set_channel_layer,
    )

    # Single-process
    layer = InMemoryChannelLayer()

    # Multi-process (PostgreSQL)
    layer = PgChannelLayer(database_url="postgres://localhost/mydb")
    await layer.connect()

    # Subscribe
    channel = layer.channel("chat:room1")
    sub_id = channel.subscribe(callback)

    # Publish to all subscribers
    await channel.publish({"type": "message", "text": "Hello!"})

    # Presence
    channel.join(user_id="user42", metadata={"name": "Alice"})
    members = channel.presence()  # [{"user_id": "user42", "name": "Alice"}]
    channel.leave(user_id="user42")

    # Groups (fan-out to multiple channels)
    group = layer.group("notifications")
    group.add("user:1")
    group.add("user:2")
    await group.publish({"type": "alert", "text": "System update"})

    # History
    recent = channel.history(limit=50)
"""

import asyncio
import contextlib
import inspect
import threading
import time
import uuid as _uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from hyperdjango.database import Database
from hyperdjango.logging import logger
from hyperdjango.native import fast_json_dumps, fast_json_loads
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.types import JSONValue

# ── Native telemetry metrics (P5.2) ────────────────────────────────────────

_channels_published = _tel_metrics.Counter(
    "hyperdjango_channels_published_total",
    "Total pub/sub messages published across all channels",
)
_channels_subscribers = _tel_metrics.Gauge(
    "hyperdjango_channels_subscribers",
    "Current number of active channel subscriptions",
)

# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """A channel message with metadata.

    `slots=True` drops the `__dict__` per instance — smaller memory
    footprint AND faster `__init__` (no dict creation). Matters on
    the channel publish hot path where every publish allocates one
    Message; at 300K publish/s on a single-subscriber channel the
    instance alloc is a visible chunk of self time.
    """

    channel: str
    data: JSONValue
    timestamp: float = field(default_factory=time.time)
    sender: str | None = None

    # Lazy one-time serialization cache. A single broadcast fans one Message
    # out to N subscribers; without this, each subscriber re-encodes identical
    # bytes (N encodes + N dict builds). Excluded from init/repr/compare/hash —
    # it's a memo, not state. Set via object.__setattr__ (the dataclass is
    # frozen). A broadcast becomes 1 encode + N sends.
    _json_bytes: bytes | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def to_json_bytes(self) -> bytes:
        """Serialize to UTF-8 JSON bytes, encoding at most once per Message."""
        cached = self._json_bytes
        if cached is not None:
            return cached
        obj = {
            "channel": self.channel,
            "data": self.data,
            "timestamp": self.timestamp,
        }
        if self.sender is not None:
            obj["sender"] = self.sender
        result = fast_json_dumps(obj)
        if not isinstance(result, bytes):
            result = result.encode("utf-8")
        # dynamic-attr: Message is a frozen dataclass — object.__setattr__ is the only way to populate the one-time _json_bytes cache
        object.__setattr__(self, "_json_bytes", result)
        return result

    def to_json(self) -> str:
        """Serialize to JSON string (backed by the one-time byte cache)."""
        return self.to_json_bytes().decode("utf-8")

    @classmethod
    def from_json(cls, raw: str | bytes) -> Message:
        """Deserialize from JSON string."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        obj = fast_json_loads(raw)
        return cls(
            channel=obj["channel"],
            data=obj["data"],
            timestamp=obj.get("timestamp", time.time()),
            sender=obj.get("sender"),
        )


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

_sub_counter = 0
_sub_lock = threading.Lock()


def _next_sub_id() -> int:
    global _sub_counter
    with _sub_lock:
        _sub_counter += 1
        return _sub_counter


@dataclass
class Subscription:
    """A subscriber registration on a channel."""

    id: int
    callback: Callable
    channel_name: str
    filter_fn: Callable | None = None
    # Coroutine-ness resolved once at subscribe() time — iscoroutinefunction is
    # costly to call per-subscriber on every publish.
    is_async: bool = False


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


@dataclass
class Channel:
    """A named pub/sub channel with subscribers, presence, and history.

    Thread-safe. Subscribers receive messages via callbacks.
    """

    name: str
    layer: ChannelLayer
    max_history: int = 100
    auth_fn: Callable | None = None

    # Internal state
    _subscribers: dict[int, Subscription] = field(
        default_factory=dict, init=False, repr=False
    )
    _presence: dict[str, dict[str, str | float]] = field(
        default_factory=dict, init=False, repr=False
    )
    # Per-user connection count so a user with multiple live connections (tabs,
    # devices) stays "present" until the LAST one leaves — a plain pop() marked
    # them offline the moment any single connection closed.
    _presence_refs: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _history: deque = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self):
        self._history = deque(maxlen=self.max_history)

    def subscribe(
        self,
        callback: Callable,
        filter_fn: Callable | None = None,
        user_id: str | None = None,
    ) -> int:
        """Subscribe to this channel. Returns subscription ID.

        Args:
            callback: Called with (Message) for each published message.
            filter_fn: Optional filter — callback(message) -> bool. Only deliver if True.
            user_id: Optional user ID for auth check.

        Raises:
            PermissionError: If auth_fn is set and denies access.
        """
        if self.auth_fn and not self.auth_fn(self.name, user_id):
            raise PermissionError(
                f"Access denied to channel '{self.name}' for user '{user_id}'"
            )

        sub = Subscription(
            id=_next_sub_id(),
            callback=callback,
            channel_name=self.name,
            filter_fn=filter_fn,
            is_async=inspect.iscoroutinefunction(callback),
        )
        with self._lock:
            self._subscribers[sub.id] = sub
        _channels_subscribers.inc(1)
        return sub.id

    def subscribe_with_history(
        self,
        callback: Callable,
        filter_fn: Callable | None = None,
        user_id: str | None = None,
        history_limit: int = 50,
    ) -> tuple[int, list[Message]]:
        """Atomically snapshot history AND register the subscriber under one lock.

        Returns ``(sub_id, history_snapshot)``.

        This closes the subscribe-then-replay double-delivery race: with the
        two-step ``subscribe()`` + ``history()`` sequence, a message published
        in between is BOTH queued to the fresh subscriber's callback AND present
        in the replayed history, so the client receives it twice (and possibly
        out of order). Because publish() appends-to-history and snapshots
        subscribers under this same lock, taking the snapshot and registering
        the subscriber in one acquisition makes every message exactly-once:
        anything already in the snapshot was published before we registered
        (so it was NOT queued to us), and anything published afterwards is
        queued to us (so it is NOT in the snapshot).
        """
        if self.auth_fn and not self.auth_fn(self.name, user_id):
            raise PermissionError(
                f"Access denied to channel '{self.name}' for user '{user_id}'"
            )

        sub = Subscription(
            id=_next_sub_id(),
            callback=callback,
            channel_name=self.name,
            filter_fn=filter_fn,
            is_async=inspect.iscoroutinefunction(callback),
        )
        with self._lock:
            history = list(self._history)[-history_limit:]
            self._subscribers[sub.id] = sub
        _channels_subscribers.inc(1)
        return sub.id, history

    def unsubscribe(self, sub_id: int) -> bool:
        """Remove a subscription. Returns True if found."""
        with self._lock:
            removed = self._subscribers.pop(sub_id, None) is not None
        if removed:
            _channels_subscribers.dec(1)
        return removed

    async def publish(self, data: JSONValue, sender: str | None = None):
        """Publish a message to all subscribers on this channel.

        Also publishes through the channel layer for cross-process delivery.
        """
        msg = Message(channel=self.name, data=data, sender=sender)

        # Store in history + snapshot subscribers in single lock acquisition
        with self._lock:
            self._history.append(msg)
            subs = list(self._subscribers.values())

        _channels_published.inc(1)

        # Deliver to local subscribers
        await self._deliver_to(subs, msg)

        # Propagate through layer (for PgChannelLayer cross-process)
        await self.layer._propagate(msg)

    async def _deliver(self, msg: Message):
        """Deliver a message to local subscribers (used by PgChannelLayer on_notify)."""
        with self._lock:
            subs = list(self._subscribers.values())
        await self._deliver_to(subs, msg)

    async def _deliver_to(self, subs: list, msg: Message):
        """Deliver a message to a list of subscribers.

        Async callbacks are gathered in parallel for fan-out performance.
        Sync callbacks are called sequentially.
        """
        async_tasks = []
        for sub in subs:
            try:
                if sub.filter_fn and not sub.filter_fn(msg):
                    continue

                if sub.is_async:
                    async_tasks.append(sub.callback(msg))
                else:
                    sub.callback(msg)
            # Fan-out delivery: one subscriber's callback/filter_fn raising must
            # not abort delivery to the remaining subscribers.
            # blind-except: isolate per-subscriber failures during broadcast.
            except Exception as e:
                logger.warning(
                    "Channel {name}: subscriber error: {err}", name=self.name, err=e
                )

        if async_tasks:
            if len(async_tasks) == 1:
                try:
                    await async_tasks[0]
                # A single async subscriber's coroutine failing is isolated
                # per-subscriber (the multi-task branch below uses
                # gather(return_exceptions=True) for the same reason).
                # blind-except: isolate per-subscriber failures during fan-out.
                except Exception as e:
                    logger.warning(
                        "Channel {name}: async subscriber error: {err}",
                        name=self.name,
                        err=e,
                    )
            else:
                results = await asyncio.gather(*async_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning(
                            "Channel {name}: async subscriber error: {err}",
                            name=self.name,
                            err=result,
                        )

    def join(self, user_id: str, metadata: dict | None = None):
        """Add a user to presence tracking.

        ``metadata`` is caller/client-supplied presence data (display name,
        avatar, etc.). The authoritative ``user_id`` and ``joined_at`` are
        applied AFTER the metadata spread so metadata can never override them —
        otherwise a client passing ``metadata={"user_id": "someone_else"}`` would
        spoof its presence identity in the "who's online" list.
        """
        with self._lock:
            # Preserve the original "online since" across additional connections
            # for the same user (a second tab shouldn't reset joined_at).
            existing = self._presence.get(user_id)
            joined_at = existing["joined_at"] if existing else time.time()
            self._presence[user_id] = {
                **(metadata or {}),
                "user_id": user_id,
                "joined_at": joined_at,
            }
            self._presence_refs[user_id] = self._presence_refs.get(user_id, 0) + 1

    def leave(self, user_id: str) -> bool:
        """Drop one connection for a user from presence tracking.

        Refcounted: the user stays present until their LAST connection leaves, so
        closing one of several tabs/devices doesn't falsely mark them offline.
        Returns True if the user was present (tracked) before this call.
        """
        with self._lock:
            n = self._presence_refs.get(user_id, 0)
            if n > 1:
                # Other live connections remain — keep the user present.
                self._presence_refs[user_id] = n - 1
                return True
            self._presence_refs.pop(user_id, None)
            return self._presence.pop(user_id, None) is not None

    def presence(self) -> list[dict[str, str | float]]:
        """Get current presence list."""
        with self._lock:
            return list(self._presence.values())

    def presence_count(self) -> int:
        """Get number of present users."""
        with self._lock:
            return len(self._presence)

    def history(self, limit: int = 50) -> list[Message]:
        """Get recent message history."""
        with self._lock:
            items = list(self._history)
        return items[-limit:]

    def subscriber_count(self) -> int:
        """Get number of active subscribers."""
        with self._lock:
            return len(self._subscribers)

    def clear_history(self):
        """Clear message history."""
        with self._lock:
            self._history.clear()

    def stats(self) -> dict[str, str | int]:
        """Get channel statistics."""
        with self._lock:
            return {
                "name": self.name,
                "subscribers": len(self._subscribers),
                "presence": len(self._presence),
                "history_size": len(self._history),
                "max_history": self.max_history,
            }

    def clear_presence(self):
        """Clear all presence entries."""
        with self._lock:
            self._presence.clear()


# ---------------------------------------------------------------------------
# Channel Group (fan-out)
# ---------------------------------------------------------------------------


@dataclass
class ChannelGroup:
    """A group of channels for fan-out publishing.

    Publish once → deliver to all channels in the group.

    Usage:
        group = layer.group("notifications")
        group.add("user:1")
        group.add("user:2")
        await group.publish({"text": "Hello all!"})
    """

    name: str
    layer: ChannelLayer
    _members: set[str] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def add(self, channel_name: str):
        """Add a channel to this group."""
        with self._lock:
            self._members.add(channel_name)

    def discard(self, channel_name: str):
        """Remove a channel from this group."""
        with self._lock:
            self._members.discard(channel_name)

    def members(self) -> set[str]:
        """Get set of channel names in this group."""
        with self._lock:
            return set(self._members)

    async def publish(self, data: JSONValue, sender: str | None = None):
        """Publish to all channels in this group (parallel fan-out).

        Per-channel failures are isolated (mirrors Channel._deliver_to): one
        member channel whose publish raises — e.g. a PgChannelLayer NOTIFY
        failing — must not abort delivery to the rest of the group, or a single
        bad channel would drop the whole fan-out (every other member's message).
        """
        with self._lock:
            names = list(self._members)

        tasks = [
            self.layer.channel(name).publish(data, sender=sender) for name in names
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(names, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "Group {group}: publish to channel {name} failed: {err}",
                        group=self.name,
                        name=name,
                        err=result,
                    )

    def size(self) -> int:
        """Number of channels in group."""
        with self._lock:
            return len(self._members)


# ---------------------------------------------------------------------------
# Channel Layer (abstract)
# ---------------------------------------------------------------------------


@dataclass
class ChannelLayer:
    """Base channel layer — manages channels and groups."""

    _channels: dict[str, Channel] = field(default_factory=dict, init=False, repr=False)
    _groups: dict[str, ChannelGroup] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    default_history_size: int = 100

    def channel(self, name: str, **kwargs) -> Channel:
        """Get or create a named channel."""
        with self._lock:
            if name not in self._channels:
                self._channels[name] = Channel(
                    name=name,
                    layer=self,
                    max_history=kwargs.get("max_history", self.default_history_size),
                    auth_fn=kwargs.get("auth_fn"),
                )
            return self._channels[name]

    def group(self, name: str) -> ChannelGroup:
        """Get or create a named group."""
        with self._lock:
            if name not in self._groups:
                self._groups[name] = ChannelGroup(name=name, layer=self)
            return self._groups[name]

    def remove_channel(self, name: str) -> bool:
        """Remove a channel. Returns True if found."""
        with self._lock:
            return self._channels.pop(name, None) is not None

    def remove_group(self, name: str) -> bool:
        """Remove a group. Returns True if found."""
        with self._lock:
            return self._groups.pop(name, None) is not None

    def channel_names(self) -> list[str]:
        """List all channel names."""
        with self._lock:
            return list(self._channels.keys())

    def group_names(self) -> list[str]:
        """List all group names."""
        with self._lock:
            return list(self._groups.keys())

    def stats(self) -> dict[str, int]:
        """Get aggregate stats across all channels."""
        with self._lock:
            channels = list(self._channels.values())
            num_groups = len(self._groups)
        total_subs = 0
        total_presence = 0
        total_history = 0
        for ch in channels:
            s = ch.stats()
            total_subs += s["subscribers"]
            total_presence += s["presence"]
            total_history += s["history_size"]
        return {
            "channels": len(channels),
            "groups": num_groups,
            "total_subscribers": total_subs,
            "total_presence": total_presence,
            "total_history": total_history,
        }

    async def _propagate(self, msg: Message):
        """Propagate a message through the layer backend.

        Override in subclasses for cross-process delivery.
        InMemoryChannelLayer is no-op (already delivered locally).
        """
        pass

    async def connect(self):
        """Connect the layer backend (if needed)."""
        pass

    async def disconnect(self):
        """Disconnect the layer backend."""
        pass


# ---------------------------------------------------------------------------
# InMemory Channel Layer
# ---------------------------------------------------------------------------


@dataclass
class InMemoryChannelLayer(ChannelLayer):
    """In-process channel layer. Thread-safe. Zero-latency.

    Messages are delivered directly to local subscribers.
    No cross-process support — use PgChannelLayer for that.
    """

    pass  # All functionality is in ChannelLayer base


# ---------------------------------------------------------------------------
# PostgreSQL LISTEN/NOTIFY Channel Layer
# ---------------------------------------------------------------------------


@dataclass
class PgChannelLayer(ChannelLayer):
    """Cross-process channel layer using PostgreSQL LISTEN/NOTIFY.

    Messages published on one process are delivered to subscribers on
    all processes connected to the same PostgreSQL database.

    Architecture:
    - PUBLISH: ``SELECT pg_notify($1, $2)`` with bound parameters (injection-safe,
      escaping-free) on a pooled connection.
    - SUBSCRIBE: LISTEN on a single MULTIPLEXED listener connection per database,
      shared across ALL channels and demultiplexed by channel name in the native
      layer. One background connection + thread per database (O(distinct databases)),
      NOT one per channel — a per-room/per-user app with thousands of channels stays
      at one listener connection. The listener self-heals: on a dropped connection it
      reconnects and re-subscribes every registered channel.
    - Messages serialized as JSON in the NOTIFY payload.

    PostgreSQL NOTIFY payload limit is ~8000 bytes. For larger messages,
    the payload contains a reference to a row in an UNLOGGED staging table.

    DELIVERY CONTRACT — this is a LOSSY, WAKE-UP-ONLY signal, NOT a durable bus:
    - The per-subscriber bridge queue (``websocket_channel_handler``) is bounded and
      DROPS on overflow; a NOTIFY can also be lost on listener-connection blips. Treat a
      delivered message as "something changed, go read" — if you need every event, keep a
      durable ledger (an append-only table + a monotonic cursor) as the source of truth and
      use this layer only to wake readers. (MESH does exactly this with its realtime ledger.)
    - RECONNECT BLAST RADIUS: because one connection serves all channels, a listener
      reconnect momentarily affects EVERY channel at once (NOTIFYs during the gap are lost
      — inherent to LISTEN/NOTIFY, which has no cross-disconnect durability). The
      wake-up-only contract already covers this; durability-sensitive consumers must use a
      ledger as above.
    - CROSS-LOOP DELIVERY: a NOTIFY is dispatched on the layer's captured loop, but
      subscribers may live on OTHER event loops (e.g. the shared WebSocket loop pool). The
      bridge hops to each consumer's own loop via ``call_soon_threadsafe`` — subscriber
      callbacks must never touch another loop's objects directly.
    - EXACTLY-ONCE-PER-NODE locally: ``Channel.publish`` delivers to local subscribers
      synchronously AND fires a NOTIFY for OTHER processes. The publishing node's own
      multiplexed listener also receives that NOTIFY; an ``_origin_id`` stamped into the
      payload lets ``on_notify`` DROP the echo so a local subscriber is never delivered the
      same publish twice (once inline + once via the NOTIFY round-trip).

    Usage:
        layer = PgChannelLayer(database_url="postgres://localhost/mydb")
        await layer.connect()

        channel = layer.channel("events")
        channel.subscribe(my_callback)

        await channel.publish({"type": "update", "data": "hello"})

        await layer.disconnect()
    """

    database_url: str = ""
    pg_channel_prefix: str = "hyper_ch_"
    # Rows in the large-message staging table are deleted the instant they're
    # delivered; these bound the UNLOGGED table against rows whose NOTIFY was
    # lost (listener blip / no live subscriber), which would otherwise leak
    # forever. Reap opportunistically, at most once per interval, on publish.
    staging_message_ttl_seconds: int = 300
    staging_reap_interval_seconds: float = 30.0
    _db: Database | None = field(default=None, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(
        default=None, init=False, repr=False
    )
    _listener_channels: set[str] = field(default_factory=set, init=False, repr=False)
    _staging_table_created: bool = field(default=False, init=False, repr=False)
    _last_staging_reap: float = field(default=0.0, init=False, repr=False)
    # Per-layer-instance id stamped into every NOTIFY payload so this node's own listener
    # can DROP the echo of a publish it already delivered to local subscribers inline.
    # uuid4 hex is unique per process/instance — two nodes never collide.
    _origin_id: str = field(
        default_factory=lambda: _uuid.uuid4().hex, init=False, repr=False
    )

    async def connect(self):
        """Connect to PostgreSQL for NOTIFY sends."""
        self._db = Database(self.database_url)
        await self._db.connect()
        # Capture the event loop for cross-thread delivery
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def is_connected(self) -> bool:
        """True once ``connect()`` has established the publish (NOTIFY-send)
        connection and it has not been torn down by ``disconnect()``.

        HONEST SCOPE — this is a coarse "layer wired up" signal, NOT a liveness
        probe. It inspects only the publish-side ``Database`` handle. It does
        NOT observe the multiplexed LISTEN connection (owned natively by
        ``_db_listen``, with no liveness accessor exposed to Python), so it will
        NOT flip to False when a cross-replica LISTEN/NOTIFY link drops while the
        process keeps running — the exact silent failure a readiness check most
        wants to catch. Treat a True result as "the layer was started", not as
        "cross-node delivery is currently healthy". If true LISTEN liveness
        becomes observable (a native listener status accessor), probe that here
        instead."""
        return self._db is not None

    async def disconnect(self):
        """Disconnect and stop listener."""
        if self._db is not None:
            await self._db.disconnect()
            self._db = None
        self._loop = None
        self._listener_channels.clear()
        self._staging_table_created = False

    def channel(self, name: str, **kwargs) -> Channel:
        """Get or create a channel, auto-start LISTEN if connected."""
        ch = super().channel(name, **kwargs)

        # Start listening for this channel's PG notifications. Compare the
        # PREFIXED name — ``_listener_channels`` only ever holds prefixed names
        # (added in ``_start_listener``), so testing the raw ``name`` was always
        # true and re-ran ``_start_listener`` on every ``channel()`` call.
        if (
            self._db is not None
            and self._pg_channel_name(name) not in self._listener_channels
        ):
            self._start_listener(name)

        return ch

    def _start_listener(self, channel_name: str):
        """Register a channel on the shared multiplexed listener via native _db_listen.

        ``self.database_url`` (the app's configured DSN) is passed to
        ``_db_listen``, which routes the channel to the ONE multiplexed listener
        connection for that database (created on first use), issues its LISTEN,
        and demultiplexes NOTIFYs back to ``on_notify`` by channel name. Many
        channels therefore share a single connection + thread — see the
        PgChannelLayer class docstring for the delivery contract. The native
        extension must be rebuilt+installed (``uv run hyper-build --install``)
        for changes to the listener to take effect.
        """
        pg_channel = self._pg_channel_name(channel_name)

        if pg_channel in self._listener_channels:
            return

        try:
            from hyperdjango._hyperdjango_native import _db_listen
        except ImportError:
            raise RuntimeError("PgChannelLayer requires native extension (_db_listen)")

        def on_notify(pg_ch, payload):
            """Called from listener thread when NOTIFY arrives."""
            try:
                inner, origin = self._unwrap_payload(payload)
                # Drop the echo of our OWN publish: Channel.publish already delivered
                # it to local subscribers inline, so re-delivering it here (via the
                # NOTIFY round-trip on our own dedicated listener) would double-fire.
                # Every payload carries the origin envelope (`_wrap_payload` always
                # wraps), so the de-dup always applies.
                if origin == self._origin_id:
                    return
                # Large messages (>7500B) are NOT inlined in the NOTIFY payload —
                # `_propagate` stages them in `hyper_channel_messages` and sends a
                # `{"_ref": <row_id>, ...}` envelope. Detect that here and resolve the
                # payload on the loop (`Message.from_json` on the envelope would raise
                # KeyError on the missing `data` field → the message would be dropped
                # AND its staging row would leak forever).
                obj = fast_json_loads(inner)
                if isinstance(obj, dict) and "_ref" in obj:
                    if self._loop is not None and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._deliver_ref(obj["_ref"]), self._loop
                        )
                    else:
                        logger.warning(
                            "PgChannelLayer: no running event loop to resolve large "
                            "message ref {ref}.",
                            ref=obj["_ref"],
                        )
                    return
                msg = Message.from_json(inner)
                ch = self._channels.get(msg.channel)
                if ch is not None:
                    # Schedule delivery on the main event loop (thread-safe)
                    if self._loop is not None and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(ch._deliver(msg), self._loop)
                    else:
                        logger.warning(
                            "PgChannelLayer: no running event loop for delivery on channel {channel}. "
                            "Ensure layer.connect() is called from within an async context.",
                            channel=msg.channel,
                        )
            # Runs inside the native NOTIFY callback thread — there is no caller
            # to propagate to; a bad inbound envelope is logged loudly (with
            # traceback) and the callback returns to keep the listener alive.
            # blind-except: callback thread — log message-loss loudly, stay alive.
            except Exception as e:
                # A cross-process message drop (bad envelope, Message.from_json
                # KeyError, unexpected payload shape, …) is a correctness event,
                # not noise. Log it loudly with the traceback and channel
                # context so silent inter-node message loss is visible in prod.
                logger.opt(exception=e).error(
                    "PgChannelLayer: dropped inbound NOTIFY on {pg_channel} "
                    "(payload could not be decoded/delivered).",
                    pg_channel=pg_ch,
                )

        # Register under the layer lock, double-checking inside: two threads
        # racing `channel()` for the same name must not both issue a PG LISTEN
        # (the native listener would fire every cross-process NOTIFY twice).
        with self._lock:
            if pg_channel in self._listener_channels:
                return
            _db_listen(self.database_url, pg_channel, on_notify)
            self._listener_channels.add(pg_channel)

    def _wrap_payload(self, inner: str) -> str:
        """Wrap a Message-JSON string in a NOTIFY envelope carrying this node's origin id.

        The envelope ``{"_o": <origin>, "_p": <message-json>}`` keeps the inner ``Message``
        wire format untouched (``Message.to_json``/``from_json`` are unchanged) while letting
        a node recognise — and drop — the echo of its OWN publish in ``on_notify``.
        """
        env = fast_json_dumps({"_o": self._origin_id, "_p": inner})
        if isinstance(env, bytes):
            env = env.decode("utf-8")
        return env

    @staticmethod
    def _unwrap_payload(payload: str | bytes):
        """Return ``(inner_message_json, origin_id)`` from a NOTIFY payload.

        Every NOTIFY payload carries the origin envelope ``{"_o": <origin>, "_p": <message-json>}``
        (``_wrap_payload`` always wraps). A missing or corrupt envelope is NOT a deliverable
        frame — it is dropped/raised so the caller's ``except`` logs it; there is no
        un-enveloped/raw payload form.
        """
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        obj = fast_json_loads(payload)
        if isinstance(obj, dict) and "_o" in obj and "_p" in obj:
            return obj["_p"], obj["_o"]
        raise ValueError("PgChannelLayer: NOTIFY payload missing origin envelope")

    async def _propagate(self, msg: Message):
        """Send NOTIFY to PostgreSQL for cross-process delivery."""
        if self._db is None:
            logger.warning(
                "PgChannelLayer._propagate called but not connected. "
                "Call await layer.connect() first."
            )
            return

        pg_channel = self._pg_channel_name(msg.channel)
        payload = msg.to_json()

        # PostgreSQL NOTIFY payload limit ~8000 bytes
        if len(payload) > 7500:
            # Store in staging table, send reference (wrapped with this node's origin).
            row_id = await self._stage_large_message(payload)
            ref_payload = fast_json_dumps({"_ref": row_id, "channel": msg.channel})
            if isinstance(ref_payload, bytes):
                ref_payload = ref_payload.decode("utf-8")
            wrapped = self._wrap_payload(ref_payload)
        else:
            wrapped = self._wrap_payload(payload)

        # Use the pg_notify(text, text) FUNCTION with bound parameters rather
        # than the NOTIFY command with a hand-escaped string literal: it is
        # injection-safe and, crucially, escaping-free — the previous
        # single-quote+backslash escaping corrupted every payload under
        # standard_conforming_strings=on (the default), where backslashes in a
        # '...' literal are literal, so doubling them reached the receiver as
        # invalid JSON and silently dropped the message.
        await self._db.execute("SELECT pg_notify($1, $2)", pg_channel, wrapped)

    async def _stage_large_message(self, payload: str) -> int:
        """Store a large message in a staging table and return its ID."""
        if not self._staging_table_created:
            await self._db.execute("""
                CREATE UNLOGGED TABLE IF NOT EXISTS hyper_channel_messages (
                    id SERIAL PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            self._staging_table_created = True
        await self._reap_stale_staging_rows()
        rows = await self._db.query(
            "INSERT INTO hyper_channel_messages (payload) VALUES ($1) RETURNING id",
            payload,
        )
        return rows[0]["id"]

    async def _reap_stale_staging_rows(self) -> None:
        """Delete staged large-message rows older than the TTL.

        This is the SOLE deleter of staging rows. `_deliver_ref` deliberately
        does not delete on receipt: in a multi-node deployment the same row must
        be read independently by every listening node, so a delivery-time delete
        would race and starve other nodes' subscribers. Time-based reaping gives
        all nodes a bounded window (``staging_message_ttl_seconds``) to read the
        payload, then GCs it. The staging table is shared, so whichever node
        publishes runs this and cleans rows for the whole cluster. Runs at most
        once per ``staging_reap_interval_seconds`` (monotonic-clocked, so no
        delete storm on a publish burst).
        """
        now = time.monotonic()
        if now - self._last_staging_reap < self.staging_reap_interval_seconds:
            return
        self._last_staging_reap = now
        with contextlib.suppress(Exception):
            await self._db.execute(
                "DELETE FROM hyper_channel_messages "
                "WHERE created_at < NOW() - $1 * INTERVAL '1 second'",
                self.staging_message_ttl_seconds,
            )

    async def _deliver_ref(self, row_id: int) -> None:
        """Resolve a large message from the staging table and deliver it locally.

        Runs on the layer's event loop (scheduled from the listener thread's
        ``on_notify``) on every node that received the ``_ref`` NOTIFY EXCEPT the
        publisher (which already delivered inline and drops its own echo). A
        missing row (already TTL-reaped) is a no-op.

        This path does NOT delete the row. In a multi-node deployment — the
        reason PgChannelLayer exists — the SAME row_id is broadcast to every
        listening node, and each must independently SELECT the payload to deliver
        to ITS local subscribers. If any one node deleted the row on delivery,
        it would race the others and starve their subscribers of the message
        (with 3+ nodes, all but the delete-winner miss it). Cleanup is therefore
        purely time-based: ``_reap_stale_staging_rows`` (TTL) is the sole
        deleter, giving every node a bounded window to read the payload. The
        staging table is shared, so the publishing node's reaper GCs rows for the
        whole cluster — a subscribe-only node need not (and does not) delete.
        """
        try:
            rows = await self._db.query(
                "SELECT payload FROM hyper_channel_messages WHERE id = $1", row_id
            )
            if not rows:
                return
            payload = rows[0]["payload"]
            msg = Message.from_json(payload)
            ch = self._channels.get(msg.channel)
            if ch is not None:
                await ch._deliver(msg)
        # Staged large-message resolution — any failure is logged loudly; the row
        # stays (TTL reaper owns deletion) so a transient parse/delivery failure
        # is retryable, not a silent loss, and there is no caller to re-raise to.
        # blind-except: keep the listener alive; never lose a staged message.
        except Exception as e:
            logger.opt(exception=e).error(
                "PgChannelLayer: failed to resolve/deliver staged large message "
                "(row {row_id}); leaving staging row for TTL reaper.",
                row_id=row_id,
            )

    def _pg_channel_name(self, channel_name: str) -> str:
        """Convert channel name to PostgreSQL channel identifier.

        Truncates to 63 chars (PostgreSQL identifier limit).
        """
        safe = "".join(c if c.isalnum() or c == "_" else "_" for c in channel_name)
        name = f"{self.pg_channel_prefix}{safe}"
        return name[:63]


# ---------------------------------------------------------------------------
# WebSocket channel helper
# ---------------------------------------------------------------------------


async def websocket_channel_handler(
    ws,
    channel: Channel,
    user_id: str | None = None,
    on_message: Callable | None = None,
    on_connect: Callable | None = None,
    on_disconnect: Callable | None = None,
    queue_size: int = 1000,
):
    """Standard WebSocket-to-channel bridge.

    Subscribes the WebSocket to a channel, forwards messages bidirectionally,
    manages presence, and handles clean disconnect.

    Usage:
        @app.websocket("/ws/chat/{room}")
        async def chat(ws):
            await ws.accept()
            channel = layer.channel(f"chat:{ws.path_params['room']}")
            await websocket_channel_handler(ws, channel, user_id="user42")

    Message flow:
        Client → WebSocket → on_message callback → channel.publish()
        Channel → subscriber callback → WebSocket.send_text()
    """
    # Bounded queue for messages from channel → WebSocket (backpressure).
    # The subscriber callback fires SYNCHRONOUSLY on the *publisher's* thread
    # (see Channel._deliver_to) — which is another connection's loop/worker
    # thread, not this connection's. asyncio.Queue is NOT thread-safe, so we
    # must hop onto this connection's loop with call_soon_threadsafe rather
    # than touch the queue directly from the callback. This is required for
    # correctness under every server model (it was racy before this fix even
    # in the thread-per-connection model), and it is cooperative — it never
    # parks a thread per connection.
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=queue_size)

    def _enqueue(msg: Message):
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.debug(
                "WebSocket queue full for channel {name}, dropping message",
                name=channel.name,
            )

    def forward_to_ws(msg: Message):
        """Channel subscriber callback (runs on the publisher's thread)."""
        with contextlib.suppress(RuntimeError):  # loop closing/closed
            loop.call_soon_threadsafe(_enqueue, msg)

    # Everything from subscribe onward runs INSIDE the try so the finally always
    # unsubscribes — a client that disconnects during history replay or on_connect
    # (both of which await/send and can raise WebSocketDisconnect) must NOT escape
    # before cleanup, or its Subscription leaks in Channel._subscribers forever
    # (its queue keeps taking refs and every publish keeps doing a dead
    # call_soon_threadsafe). sub_id/joined start unset so the finally can tell
    # what actually happened before any failure.
    sub_id: int | None = None
    joined = False
    try:
        # Subscribe to channel AND snapshot history atomically. Doing this in one
        # lock acquisition prevents double delivery of a message published in the
        # subscribe→replay window (it would otherwise be both queued to us and
        # present in the replayed history). See Channel.subscribe_with_history.
        sub_id, history_snapshot = channel.subscribe_with_history(
            forward_to_ws, user_id=user_id
        )

        # Join presence
        if user_id:
            channel.join(user_id)
            joined = True

        # Replay the history snapshot taken atomically at subscribe time.
        for msg in history_snapshot:
            await ws.send_json(
                {"channel": msg.channel, "data": msg.data, "timestamp": msg.timestamp}
            )

        # Notify connect
        if on_connect:
            if inspect.iscoroutinefunction(on_connect):
                await on_connect(ws, channel)
            else:
                on_connect(ws, channel)

        async def ws_to_channel():
            """Read from WebSocket, publish to channel. Always sends sentinel on exit."""
            try:
                async for text in ws.iter_text():
                    if on_message:
                        if inspect.iscoroutinefunction(on_message):
                            await on_message(text, channel, ws)
                        else:
                            on_message(text, channel, ws)
                    else:
                        try:
                            data = fast_json_loads(text)
                        except ValueError, TypeError:
                            data = {"text": text}
                        await channel.publish(data, sender=user_id)
            finally:
                await queue.put(None)

        # Fast path: if the WS backend accepts pre-encoded UTF-8 text bytes, the
        # whole fan-out is 1 encode (shared + cached on the Message) + N native
        # byte-sends — no per-subscriber decode/re-encode. The Message payload is
        # produced by our own JSON encoder, so it is valid UTF-8. Absent on the
        # ASGI WebSocket, where getattr returns None and we use send_text.
        # dynamic-attr: ws may be our native WebSocket (exposes the _send_text_bytes fast path) or an ASGI WebSocket (does not) — a genuine cross-backend capability probe
        send_text_bytes = getattr(ws, "_send_text_bytes", None)

        async def channel_to_ws():
            """Read from channel queue, send to WebSocket. Stops on sentinel."""
            while True:
                msg = await queue.get()
                if msg is None:
                    break
                try:
                    if send_text_bytes is not None:
                        send_text_bytes(msg.to_json_bytes())
                    else:
                        await ws.send_text(msg.to_json())
                # Any send failure means the peer is gone or the transport is
                # broken; break the pump loop and let outer teardown/cleanup run.
                # blind-except: peer gone / dead transport ends the outbound pump.
                except Exception:
                    break

        await asyncio.gather(ws_to_channel(), channel_to_ws())
    except PermissionError:
        # Auth denial from channel.subscribe() is a control signal, not a
        # transport error — it must propagate so the caller can reject/close the
        # WebSocket. Swallowing it (below) would let a denied client "connect".
        raise
    # A mid-session disconnect (WebSocketDisconnect) or send failure is expected
    # end-of-connection; fall through to the finally cleanup. PermissionError
    # (auth denial) is caught above and re-raised so denied clients never connect.
    # blind-except: normal WebSocket teardown — disconnect falls to cleanup.
    except Exception:
        # A disconnect (WebSocketDisconnect) or send failure mid-session is
        # normal teardown — fall through to cleanup.
        pass
    finally:
        # on_disconnect pairs with a real subscription: fire it only if we
        # actually subscribed (sub_id set). A pre-subscribe failure (e.g. auth
        # denial) never "connected", so it must not emit a disconnect.
        if sub_id is not None:
            channel.unsubscribe(sub_id)
            if joined and user_id:
                channel.leave(user_id)

            if on_disconnect:
                if inspect.iscoroutinefunction(on_disconnect):
                    await on_disconnect(ws, channel)
                else:
                    on_disconnect(ws, channel)


# ---------------------------------------------------------------------------
# Global channel layer singleton
# ---------------------------------------------------------------------------

_channel_layer: ChannelLayer | None = None


def get_channel_layer() -> ChannelLayer:
    """Get the global channel layer."""
    if _channel_layer is None:
        raise RuntimeError(
            "No channel layer configured. Call set_channel_layer() first."
        )
    return _channel_layer


def set_channel_layer(layer: ChannelLayer | None):
    """Set the global channel layer."""
    global _channel_layer
    _channel_layer = layer
