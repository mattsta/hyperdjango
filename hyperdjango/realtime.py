"""
High-level real-time patterns built on channels.py and websocket.py.

Provides production-ready abstractions for common real-time use cases:

1. **Room** -- Chat/collaboration rooms with member management, roles, moderation
2. **NotificationManager** -- User-targeted real-time notifications
3. **LiveQuery** -- Model change subscriptions (create/update/delete push)
4. **ConnectionManager** -- WebSocket connection lifecycle management
5. **WebSocketRateLimiter** -- Per-connection token bucket rate limiting
6. **Auth utilities** -- WebSocket authentication helpers

All patterns are built on top of Channel/ChannelLayer from channels.py
and WebSocket from websocket.py. Thread-safe for Python 3.14t.

Usage:
    from hyperdjango.realtime import (
        Room, RoomConfig, RoomMember, RoomMessage,
        NotificationManager, Notification,
        LiveQuery, ModelChange,
        ConnectionManager, ConnectionInfo,
        WebSocketRateLimiter, WSRateLimitConfig,
        ws_authenticated, ws_auth_from_query, ws_auth_from_cookie,
    )

    # Chat room
    layer = InMemoryChannelLayer()
    room = Room("general", layer)
    member = await room.join("user1", "Alice")
    msg = await room.send_message("user1", "Hello!")

    # Notifications
    notifier = NotificationManager(layer)
    await notifier.send("user1", "Welcome", "You have joined!")

    # Live queries
    live = LiveQuery(layer)
    sub_id = live.watch("Post")
    await live.notify_create("Post", 1, {"title": "New post"})

    # Connection management
    mgr = ConnectionManager(layer)
    info = await mgr.connect(ws, user_id="user1")
    await mgr.send_to_user("user1", {"type": "ping"})
"""

import asyncio
import functools
import inspect
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs

from hyperdjango.channels import Channel, ChannelLayer, Message
from hyperdjango.native import fast_json_dumps
from hyperdjango.websocket import WebSocket, is_ws_origin_allowed

_logger = logging.getLogger("hyperdjango.realtime")

VALID_NOTIFICATION_TYPES: frozenset[str] = frozenset(
    {"info", "warning", "error", "success"}
)

# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------

__all__ = [
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
]


@dataclass(slots=True)
class RoomMember:
    """A member of a Room with role and connection state."""

    user_id: str
    display_name: str
    role: str  # "member", "moderator", "admin"
    joined_at: float
    ws: WebSocket | None = None  # None if joined but not connected


@dataclass(slots=True)
class RoomConfig:
    """Configuration for a Room."""

    max_members: int = 100
    history_size: int = 100
    require_auth: bool = True
    allowed_message_types: frozenset[str] = frozenset(
        {"text", "image", "file", "system"}
    )
    rate_limit: int = 30  # messages per minute per user
    max_message_length: int = 65536  # 64 KB max message content


@dataclass(slots=True)
class RoomMessage:
    """A message sent in a Room."""

    id: str
    room_id: str
    user_id: str
    display_name: str
    content: str
    message_type: str
    timestamp: float
    edited: bool = False
    deleted: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict for transmission."""
        return {
            "id": self.id,
            "room_id": self.room_id,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "content": self.content,
            "message_type": self.message_type,
            "timestamp": self.timestamp,
            "edited": self.edited,
            "deleted": self.deleted,
        }


class Room:
    """High-level chat room with member management, roles, moderation.

    Built on Channel + ChannelGroup for pub/sub and presence.

    Usage:
        layer = InMemoryChannelLayer()
        room = Room("general", layer)

        member = await room.join("user1", "Alice")
        msg = await room.send_message("user1", "Hello everyone!")

        await room.kick("user2", reason="spam")
        await room.ban("user3", reason="harassment")

        members = room.get_members()
        history = room.get_history(limit=20)
    """

    def __init__(
        self,
        room_id: str,
        layer: ChannelLayer,
        config: RoomConfig | None = None,
    ):
        self.room_id: str = room_id
        self.layer: ChannelLayer = layer
        self.config: RoomConfig = config or RoomConfig()
        self._channel: Channel = layer.channel(
            f"room:{room_id}", max_history=self.config.history_size
        )
        self._members: dict[str, RoomMember] = {}
        self._banned: set[str] = set()
        self._history: deque[RoomMessage] = deque(maxlen=self.config.history_size)
        self._typing: dict[str, float] = {}  # user_id -> timestamp
        self._rate_counts: dict[str, deque[float]] = {}  # user_id -> timestamps
        self._lock: threading.Lock = threading.Lock()
        self._typing_timeout: float = 5.0  # seconds before typing expires

    async def join(
        self,
        user_id: str,
        display_name: str,
        ws: WebSocket | None = None,
        role: str = "member",
    ) -> RoomMember:
        """Add a member to the room.

        Args:
            user_id: Unique user identifier.
            display_name: Display name shown in room.
            ws: Optional WebSocket connection.
            role: Member role -- "member", "moderator", or "admin".

        Returns:
            RoomMember instance.

        Raises:
            PermissionError: If user is banned or room is full.
        """
        with self._lock:
            if user_id in self._banned:
                raise PermissionError(
                    f"User '{user_id}' is banned from room '{self.room_id}'"
                )
            if (
                len(self._members) >= self.config.max_members
                and user_id not in self._members
            ):
                raise PermissionError(
                    f"Room '{self.room_id}' is full ({self.config.max_members} members)"
                )
            member = RoomMember(
                user_id=user_id,
                display_name=display_name,
                role=role,
                joined_at=time.time(),
                ws=ws,
            )
            self._members[user_id] = member

        self._channel.join(user_id, metadata={"name": display_name, "role": role})

        await self.broadcast(
            {
                "type": "member_joined",
                "user_id": user_id,
                "display_name": display_name,
                "role": role,
            }
        )

        return member

    async def leave(self, user_id: str) -> bool:
        """Remove a member from the room.

        Returns:
            True if the member was present and removed.
        """
        with self._lock:
            removed = self._members.pop(user_id, None)
            self._typing.pop(user_id, None)
            self._rate_counts.pop(user_id, None)
        if removed is None:
            return False

        self._channel.leave(user_id)

        await self.broadcast(
            {
                "type": "member_left",
                "user_id": user_id,
                "display_name": removed.display_name,
            }
        )

        return True

    def _check_rate_limit(self, user_id: str) -> bool:
        """Check if user is within rate limit. Returns True if allowed."""
        now = time.time()
        window_start = now - 60.0

        with self._lock:
            timestamps = self._rate_counts.get(user_id)
            if timestamps is None:
                timestamps = deque()
                self._rate_counts[user_id] = timestamps

            # Remove expired entries (O(1) popleft on deque)
            while timestamps and timestamps[0] < window_start:
                timestamps.popleft()

            if len(timestamps) >= self.config.rate_limit:
                return False

            timestamps.append(now)
            return True

    async def send_message(
        self,
        user_id: str,
        content: str,
        message_type: str = "text",
    ) -> RoomMessage:
        """Send a message to the room.

        Args:
            user_id: Sender's user ID (must be a member).
            content: Message content.
            message_type: Type of message ("text", "image", "file", "system").

        Returns:
            RoomMessage instance.

        Raises:
            PermissionError: If user is not a member, banned, or rate limited.
            ValueError: If message_type is not allowed.
        """
        if message_type not in self.config.allowed_message_types:
            raise ValueError(
                f"Message type '{message_type}' not allowed. "
                f"Allowed: {self.config.allowed_message_types}"
            )

        if len(content) > self.config.max_message_length:
            raise ValueError(
                f"Message content too long ({len(content)} bytes, "
                f"max {self.config.max_message_length})"
            )

        # Hold lock for the full check-and-get to prevent TOCTOU race
        # where member could leave between the check and reading display_name.
        with self._lock:
            member = self._members.get(user_id)
            if member is None:
                raise PermissionError(
                    f"User '{user_id}' is not a member of room '{self.room_id}'"
                )
            display_name = member.display_name

        if not self._check_rate_limit(user_id):
            raise PermissionError(
                f"Rate limit exceeded ({self.config.rate_limit} messages/minute)"
            )

        msg = RoomMessage(
            id=uuid.uuid4().hex,
            room_id=self.room_id,
            user_id=user_id,
            display_name=display_name,
            content=content,
            message_type=message_type,
            timestamp=time.time(),
        )

        with self._lock:
            self._history.append(msg)

        await self._channel.publish(
            {"type": "message", **msg.to_dict()},
            sender=user_id,
        )

        return msg

    async def broadcast(self, data: dict[str, object]) -> None:
        """Broadcast a system message to all room members."""
        await self._channel.publish(data, sender="system")

    def get_members(self) -> list[RoomMember]:
        """Get list of all current room members."""
        with self._lock:
            return list(self._members.values())

    def get_member(self, user_id: str) -> RoomMember | None:
        """Get a specific member by user ID."""
        with self._lock:
            return self._members.get(user_id)

    def get_history(self, limit: int = 50) -> list[RoomMessage]:
        """Get recent message history.

        Args:
            limit: Maximum number of messages to return.

        Returns:
            List of RoomMessage, most recent last.
        """
        with self._lock:
            items = list(self._history)
        return items[-limit:]

    # -- Moderation --

    async def kick(self, user_id: str, reason: str = "") -> bool:
        """Kick a user from the room (they can rejoin).

        Returns:
            True if the user was a member and was kicked.
        """
        # leave() already checks membership atomically under lock.
        # No separate membership check needed -- avoids TOCTOU race
        # and avoids holding lock across the await in leave().
        removed = await self.leave(user_id)
        if removed:
            await self.broadcast(
                {
                    "type": "member_kicked",
                    "user_id": user_id,
                    "reason": reason,
                }
            )
        return removed

    async def ban(self, user_id: str, reason: str = "") -> bool:
        """Ban a user from the room (they cannot rejoin).

        Returns:
            True if the ban was applied (user was not already banned).
        """
        with self._lock:
            if user_id in self._banned:
                return False
            self._banned.add(user_id)
            was_member = user_id in self._members

        if was_member:
            await self.leave(user_id)

        await self.broadcast(
            {
                "type": "member_banned",
                "user_id": user_id,
                "reason": reason,
            }
        )

        return True

    async def unban(self, user_id: str) -> bool:
        """Remove a ban on a user.

        Returns:
            True if the user was banned and is now unbanned.
        """
        with self._lock:
            if user_id not in self._banned:
                return False
            self._banned.discard(user_id)
        return True

    def is_banned(self, user_id: str) -> bool:
        """Check if a user is banned from the room."""
        with self._lock:
            return user_id in self._banned

    # -- Typing indicators --

    async def set_typing(self, user_id: str, typing: bool = True) -> None:
        """Set typing indicator for a user.

        Args:
            user_id: The user who is typing.
            typing: True if typing, False to clear.
        """
        with self._lock:
            if typing:
                self._typing[user_id] = time.time()
            else:
                self._typing.pop(user_id, None)

        await self.broadcast(
            {
                "type": "typing",
                "user_id": user_id,
                "typing": typing,
            }
        )

    def get_typing_users(self) -> list[str]:
        """Get list of user IDs currently typing.

        Automatically expires typing indicators older than the timeout.
        """
        now = time.time()
        cutoff = now - self._typing_timeout
        with self._lock:
            expired = [uid for uid, ts in self._typing.items() if ts < cutoff]
            for uid in expired:
                del self._typing[uid]
            return list(self._typing.keys())


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Notification:
    """A notification sent to a specific user."""

    id: str
    user_id: str
    title: str
    body: str
    notification_type: str  # "info", "warning", "error", "success"
    data: dict[str, object] | None = None
    read: bool = False
    created_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """Serialize to dict for transmission."""
        result: dict[str, object] = {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "body": self.body,
            "notification_type": self.notification_type,
            "read": self.read,
            "created_at": self.created_at,
        }
        if self.data is not None:
            result["data"] = self.data
        return result


class NotificationManager:
    """Send real-time notifications to specific users.

    Each user has a personal channel "notifications:{user_id}".
    When a WebSocket connects, it subscribes to that channel.

    Usage:
        layer = InMemoryChannelLayer()
        notifier = NotificationManager(layer)

        # Send to one user
        n = await notifier.send("user1", "Welcome", "You have joined!")

        # Send to many users
        ns = await notifier.send_many(["user1", "user2"], "Update", "New version")

        # Broadcast to all subscribed users
        await notifier.broadcast_all("Maintenance", "Server restart in 5 min")

        # Subscribe to notifications
        sub_id = notifier.subscribe("user1", my_callback)
        notifier.unsubscribe("user1", sub_id)

        # Read management
        unread = notifier.get_unread("user1")
        notifier.mark_read("user1", notification_id)
        notifier.mark_all_read("user1")
    """

    def __init__(self, layer: ChannelLayer, max_per_user: int = 1000):
        self.layer: ChannelLayer = layer
        self._notifications: dict[
            str, deque[Notification]
        ] = {}  # user_id -> bounded deque
        self._max_per_user: int = max_per_user
        self._lock: threading.Lock = threading.Lock()
        self._broadcast_channel_name: str = "notifications:__broadcast__"

    def _user_channel(self, user_id: str) -> Channel:
        """Get the notification channel for a user."""
        return self.layer.channel(f"notifications:{user_id}")

    async def send(
        self,
        user_id: str,
        title: str,
        body: str,
        notification_type: str = "info",
        data: dict[str, object] | None = None,
    ) -> Notification:
        """Send a notification to a specific user.

        Args:
            user_id: Target user.
            title: Notification title.
            body: Notification body text.
            notification_type: One of "info", "warning", "error", "success".
            data: Optional additional data payload.

        Returns:
            The created Notification.
        """
        if notification_type not in VALID_NOTIFICATION_TYPES:
            raise ValueError(
                f"Invalid notification_type '{notification_type}'. "
                f"Must be one of: {sorted(VALID_NOTIFICATION_TYPES)}"
            )

        notification = Notification(
            id=uuid.uuid4().hex,
            user_id=user_id,
            title=title,
            body=body,
            notification_type=notification_type,
            data=data,
            read=False,
            created_at=time.time(),
        )

        with self._lock:
            user_notifs = self._notifications.get(user_id)
            if user_notifs is None:
                user_notifs = deque(maxlen=self._max_per_user)
                self._notifications[user_id] = user_notifs
            user_notifs.append(notification)

        channel = self._user_channel(user_id)
        await channel.publish(
            {"type": "notification", **notification.to_dict()},
            sender="system",
        )

        return notification

    async def send_many(
        self,
        user_ids: list[str],
        title: str,
        body: str,
        notification_type: str = "info",
    ) -> list[Notification]:
        """Send the same notification to multiple users.

        Args:
            user_ids: List of target user IDs.
            title: Notification title.
            body: Notification body text.
            notification_type: One of "info", "warning", "error", "success".

        Returns:
            List of created Notifications.
        """
        coros = [
            self.send(user_id, title, body, notification_type) for user_id in user_ids
        ]
        # return_exceptions=True so one failed send doesn't abandon its siblings
        # (bare gather cancels/detaches the rest, leaking unretrieved exceptions).
        # Log each failure and return the notifications that did succeed.
        results = await asyncio.gather(*coros, return_exceptions=True)
        notifications: list[Notification] = []
        for user_id, result in zip(user_ids, results):
            if isinstance(result, Exception):
                _logger.warning(
                    "send_many: notification to user %s failed: %r", user_id, result
                )
            else:
                notifications.append(result)
        return notifications

    async def broadcast_all(
        self,
        title: str,
        body: str,
        notification_type: str = "info",
    ) -> Notification:
        """Broadcast a notification to all subscribed users.

        Publishes on the broadcast channel. Individual user tracking
        is not performed for broadcasts.

        Returns:
            The broadcast Notification (user_id is "__broadcast__").
        """
        if notification_type not in VALID_NOTIFICATION_TYPES:
            raise ValueError(
                f"Invalid notification_type '{notification_type}'. "
                f"Must be one of: {sorted(VALID_NOTIFICATION_TYPES)}"
            )

        notification = Notification(
            id=uuid.uuid4().hex,
            user_id="__broadcast__",
            title=title,
            body=body,
            notification_type=notification_type,
            read=False,
            created_at=time.time(),
        )

        channel = self.layer.channel(self._broadcast_channel_name)
        await channel.publish(
            {"type": "notification_broadcast", **notification.to_dict()},
            sender="system",
        )

        return notification

    def subscribe(self, user_id: str, callback: Callable) -> int:
        """Subscribe to notifications for a user.

        Args:
            user_id: User whose notifications to receive.
            callback: Called with (Message) for each notification.

        Returns:
            Subscription ID for unsubscribe.
        """
        channel = self._user_channel(user_id)
        return channel.subscribe(callback)

    def unsubscribe(self, user_id: str, sub_id: int) -> bool:
        """Unsubscribe from a user's notifications.

        Args:
            user_id: User whose channel to unsubscribe from.
            sub_id: Subscription ID returned by subscribe().

        Returns:
            True if subscription was found and removed.
        """
        channel = self._user_channel(user_id)
        return channel.unsubscribe(sub_id)

    def get_unread(self, user_id: str) -> list[Notification]:
        """Get all unread notifications for a user."""
        with self._lock:
            user_notifs = self._notifications.get(user_id)
            if user_notifs is None:
                return []
            return [n for n in user_notifs if not n.read]

    def mark_read(self, user_id: str, notification_id: str) -> bool:
        """Mark a notification as read.

        Returns:
            True if the notification was found and marked.
        """
        with self._lock:
            user_notifs = self._notifications.get(user_id)
            if user_notifs is None:
                return False
            for n in user_notifs:
                if n.id == notification_id:
                    n.read = True
                    return True
            return False

    def mark_all_read(self, user_id: str) -> int:
        """Mark all notifications for a user as read.

        Returns:
            Number of notifications marked as read.
        """
        count = 0
        with self._lock:
            user_notifs = self._notifications.get(user_id)
            if user_notifs is None:
                return 0
            for n in user_notifs:
                if not n.read:
                    n.read = True
                    count += 1
        return count

    def clear(self, user_id: str) -> int:
        """Clear all notifications for a user.

        Returns:
            Number of notifications cleared.
        """
        with self._lock:
            user_notifs = self._notifications.pop(user_id, None)
            if user_notifs is None:
                return 0
            return len(user_notifs)


# ---------------------------------------------------------------------------
# LiveQuery
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ModelChange:
    """Describes a model change event."""

    model_name: str
    action: str  # "create", "update", "delete"
    pk: int | str
    data: dict[str, object] | None = None  # serialized fields (create/update)
    changed_fields: list[str] | None = None  # only for update
    timestamp: float = 0.0


@dataclass(slots=True)
class _LiveSubscription:
    """Internal: a live query subscription."""

    subscription_id: str
    model_name: str
    filters: dict[str, object] | None
    channel_sub_id: int


class LiveQuery:
    """Subscribe to model changes and receive real-time updates.

    Integrates with signals.py (pre/post_save, post_delete) to detect
    changes and push them to subscribed WebSocket clients.

    Usage:
        layer = InMemoryChannelLayer()
        live = LiveQuery(layer)

        # Watch all Post changes
        sub_id = live.watch("Post")

        # Watch with filters
        sub_id = live.watch("Comment", filters={"post_id": 42})

        # Decorator for change handlers
        @live.on_change("Post")
        async def handle_post_change(change: ModelChange):
            print(f"Post {change.pk} was {change.action}d")

        # Notify from signal handlers or manually
        await live.notify_create("Post", 1, {"title": "Hello"})
        await live.notify_update("Post", 1, {"title": "Updated"}, ["title"])
        await live.notify_delete("Post", 1)

        live.unwatch(sub_id)
    """

    def __init__(self, layer: ChannelLayer):
        self.layer: ChannelLayer = layer
        self._subscriptions: dict[str, _LiveSubscription] = {}
        # model_name -> [(callback, is_async)]; coroutine-ness resolved once at
        # registration so _notify never calls iscoroutinefunction on the hot path.
        self._handlers: dict[str, list[tuple[Callable, bool]]] = {}
        self._lock: threading.Lock = threading.Lock()

    def _model_channel(self, model_name: str) -> Channel:
        """Get the channel for a model's change events."""
        return self.layer.channel(f"livequery:{model_name}")

    def watch(
        self,
        model_name: str,
        filters: dict[str, object] | None = None,
    ) -> str:
        """Subscribe to changes on a model.

        Args:
            model_name: Name of the model to watch.
            filters: Optional dict of field=value filters. Only changes
                     matching all filters will be delivered.

        Returns:
            Subscription ID for unwatch().
        """
        sub_id = uuid.uuid4().hex
        channel = self._model_channel(model_name)

        filter_fn: Callable | None = None
        if filters:
            frozen_filters = dict(filters)

            def filter_fn(msg: Message) -> bool:
                data = msg.data
                if not isinstance(data, dict):
                    return True
                change_data = data.get("data")
                if change_data is None:
                    return True
                for key, value in frozen_filters.items():
                    if change_data.get(key) != value:
                        return False
                return True

        # The subscription callback is intentionally a no-op: subscriptions
        # exist for channel-level presence tracking and filter_fn gating.
        # Actual delivery happens via on_change handlers registered separately.
        channel_sub_id = channel.subscribe(lambda msg: None, filter_fn=filter_fn)

        sub = _LiveSubscription(
            subscription_id=sub_id,
            model_name=model_name,
            filters=filters,
            channel_sub_id=channel_sub_id,
        )

        with self._lock:
            self._subscriptions[sub_id] = sub

        return sub_id

    def unwatch(self, subscription_id: str) -> bool:
        """Remove a live query subscription.

        Returns:
            True if subscription was found and removed.
        """
        with self._lock:
            sub = self._subscriptions.pop(subscription_id, None)
        if sub is None:
            return False

        channel = self._model_channel(sub.model_name)
        channel.unsubscribe(sub.channel_sub_id)
        return True

    def subscription_count(self) -> int:
        """Return the current number of active live-query subscriptions.

        Thread-safe: takes the internal lock for a consistent snapshot.
        Use this for monitoring/debugging endpoints instead of reaching
        into ``_subscriptions``, which is private and may change shape
        in future platform releases.
        """
        with self._lock:
            return len(self._subscriptions)

    def watched_models(self) -> list[str]:
        """Return the sorted list of distinct model names currently
        being watched across all active subscriptions.

        Public counterpart to ``_subscriptions`` iteration — use this
        for live-query status dashboards and debug endpoints.
        """
        with self._lock:
            return sorted({s.model_name for s in self._subscriptions.values()})

    def on_change(self, model_name: str) -> Callable:
        """Decorator to register a change handler for a model.

        Usage:
            @live.on_change("Post")
            async def handle_post_change(change: ModelChange):
                print(f"Post {change.pk} was {change.action}d")
        """

        def decorator(func: Callable) -> Callable:
            with self._lock:
                handlers = self._handlers.get(model_name)
                if handlers is None:
                    handlers = []
                    self._handlers[model_name] = handlers
                handlers.append((func, inspect.iscoroutinefunction(func)))
            return func

        return decorator

    async def _notify(self, change: ModelChange) -> None:
        """Internal: publish a change and invoke handlers."""
        channel = self._model_channel(change.model_name)

        change_dict: dict[str, object] = {
            "type": "model_change",
            "model_name": change.model_name,
            "action": change.action,
            "pk": change.pk,
            "timestamp": change.timestamp,
        }
        if change.data is not None:
            change_dict["data"] = change.data
        if change.changed_fields is not None:
            change_dict["changed_fields"] = change.changed_fields

        await channel.publish(change_dict, sender="livequery")

        # Invoke registered on_change handlers
        with self._lock:
            handlers = list(self._handlers.get(change.model_name, []))

        async_coros = []
        for handler, is_async in handlers:
            if is_async:
                async_coros.append(handler(change))
            else:
                handler(change)

        if async_coros:
            # return_exceptions=True so one failed on_change handler doesn't
            # abandon its siblings (bare gather cancels/detaches the rest,
            # leaking unretrieved exceptions). Log each failure; results discarded.
            results = await asyncio.gather(*async_coros, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    _logger.warning("on_change handler failed: %r", result)

    async def notify_create(
        self,
        model_name: str,
        pk: int | str,
        data: dict[str, object],
    ) -> None:
        """Notify subscribers of a model creation.

        Args:
            model_name: Model that was created.
            pk: Primary key of the new instance.
            data: Serialized fields of the new instance.
        """
        change = ModelChange(
            model_name=model_name,
            action="create",
            pk=pk,
            data=data,
            timestamp=time.time(),
        )
        await self._notify(change)

    async def notify_update(
        self,
        model_name: str,
        pk: int | str,
        data: dict[str, object],
        changed_fields: list[str],
    ) -> None:
        """Notify subscribers of a model update.

        Args:
            model_name: Model that was updated.
            pk: Primary key of the updated instance.
            data: Serialized fields after update.
            changed_fields: List of field names that changed.
        """
        change = ModelChange(
            model_name=model_name,
            action="update",
            pk=pk,
            data=data,
            changed_fields=changed_fields,
            timestamp=time.time(),
        )
        await self._notify(change)

    async def notify_delete(
        self,
        model_name: str,
        pk: int | str,
    ) -> None:
        """Notify subscribers of a model deletion.

        Args:
            model_name: Model that was deleted.
            pk: Primary key of the deleted instance.
        """
        change = ModelChange(
            model_name=model_name,
            action="delete",
            pk=pk,
            timestamp=time.time(),
        )
        await self._notify(change)


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConnectionInfo:
    """Information about an active WebSocket connection."""

    connection_id: str
    user_id: str | None
    ws: WebSocket
    connected_at: float
    rooms: set[str]  # rooms this connection is in
    metadata: dict[str, object]


class ConnectionManager:
    """Manage WebSocket connection lifecycle.

    Tracks all active connections, handles auth, and provides
    hooks for connect/disconnect events.

    Usage:
        layer = InMemoryChannelLayer()
        mgr = ConnectionManager(layer)

        # On WebSocket connect
        info = await mgr.connect(ws, user_id="user1")

        # Send to specific connection
        await mgr.send_to_connection(info.connection_id, {"type": "welcome"})

        # Send to all connections of a user
        count = await mgr.send_to_user("user1", {"type": "update"})

        # Broadcast to all connections
        total = await mgr.broadcast({"type": "announcement"})

        # On disconnect
        await mgr.disconnect(info.connection_id)
    """

    def __init__(
        self,
        layer: ChannelLayer,
        max_connections: int = 10000,
        max_metadata_keys: int = 32,
    ):
        self.layer: ChannelLayer = layer
        self._connections: dict[str, ConnectionInfo] = {}
        self._user_connections: dict[str, set[str]] = {}  # user_id -> {connection_ids}
        self._max_connections: int = max_connections
        self._max_metadata_keys: int = max_metadata_keys
        self._lock: threading.Lock = threading.Lock()
        # Each hook is stored as ONE (callable, is_async) tuple (or None) so the
        # callable and its cached coroutine-ness are published and read together
        # in a single atomic reference. Storing them as two separate fields let
        # a reader pair a freshly-set callable with the stale is_async flag
        # (async fn called without await, or `await None`). iscoroutinefunction
        # runs at assignment, not per event.
        self._on_connect_hook: tuple[Callable, bool] | None = None
        self._on_disconnect_hook: tuple[Callable, bool] | None = None

    @property
    def on_connect(self) -> Callable | None:
        """Hook invoked after a connection is registered: (ConnectionInfo) -> None."""
        hook = self._on_connect_hook
        return hook[0] if hook is not None else None

    @on_connect.setter
    def on_connect(self, fn: Callable | None) -> None:
        self._on_connect_hook = (fn, inspect.iscoroutinefunction(fn)) if fn else None

    @property
    def on_disconnect(self) -> Callable | None:
        """Hook invoked after a connection is removed: (ConnectionInfo) -> None."""
        hook = self._on_disconnect_hook
        return hook[0] if hook is not None else None

    @on_disconnect.setter
    def on_disconnect(self, fn: Callable | None) -> None:
        self._on_disconnect_hook = (fn, inspect.iscoroutinefunction(fn)) if fn else None

    async def connect(
        self,
        ws: WebSocket,
        user_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ConnectionInfo:
        """Register a new WebSocket connection.

        Args:
            ws: The WebSocket instance.
            user_id: Optional authenticated user ID.
            metadata: Optional connection metadata.

        Returns:
            ConnectionInfo for the new connection.
        """
        actual_metadata = metadata or {}
        if len(actual_metadata) > self._max_metadata_keys:
            raise ValueError(
                f"Metadata has {len(actual_metadata)} keys, "
                f"max allowed is {self._max_metadata_keys}"
            )

        connection_id = uuid.uuid4().hex
        info = ConnectionInfo(
            connection_id=connection_id,
            user_id=user_id,
            ws=ws,
            connected_at=time.time(),
            rooms=set(),
            metadata=actual_metadata,
        )

        with self._lock:
            if len(self._connections) >= self._max_connections:
                raise ConnectionError(
                    f"Maximum connections ({self._max_connections}) exceeded"
                )
            self._connections[connection_id] = info
            if user_id is not None:
                user_conns = self._user_connections.get(user_id)
                if user_conns is None:
                    user_conns = set()
                    self._user_connections[user_id] = user_conns
                user_conns.add(connection_id)

        # Snapshot the hook tuple ONCE so the callable and its is_async flag
        # can never be read torn against a concurrent reassignment.
        hook = self._on_connect_hook
        if hook is not None:
            fn, is_async = hook
            if is_async:
                await fn(info)
            else:
                fn(info)

        return info

    async def disconnect(self, connection_id: str) -> bool:
        """Unregister a WebSocket connection.

        Returns:
            True if the connection was found and removed.
        """
        with self._lock:
            info = self._connections.pop(connection_id, None)
            if info is None:
                return False
            if info.user_id is not None:
                user_conns = self._user_connections.get(info.user_id)
                if user_conns is not None:
                    user_conns.discard(connection_id)
                    if not user_conns:
                        del self._user_connections[info.user_id]

        # Snapshot the hook tuple ONCE (see connect()).
        hook = self._on_disconnect_hook
        if hook is not None:
            fn, is_async = hook
            if is_async:
                await fn(info)
            else:
                fn(info)

        return True

    def get_connection(self, connection_id: str) -> ConnectionInfo | None:
        """Get connection info by connection ID."""
        with self._lock:
            return self._connections.get(connection_id)

    def get_user_connections(self, user_id: str) -> list[ConnectionInfo]:
        """Get all connections for a user."""
        with self._lock:
            conn_ids = self._user_connections.get(user_id)
            if conn_ids is None:
                return []
            return [
                self._connections[cid] for cid in conn_ids if cid in self._connections
            ]

    def get_all_connections(self) -> list[ConnectionInfo]:
        """Get all active connections."""
        with self._lock:
            return list(self._connections.values())

    @property
    def connection_count(self) -> int:
        """Number of active connections."""
        with self._lock:
            return len(self._connections)

    async def send_to_connection(
        self, connection_id: str, data: dict[str, object]
    ) -> bool:
        """Send data to a specific connection.

        Returns:
            True if the connection was found and data was sent successfully.
            False if the connection was not found or the send failed
            (e.g., the WebSocket disconnected between lookup and send).
        """
        with self._lock:
            info = self._connections.get(connection_id)
        if info is None:
            return False

        try:
            await info.ws.send_json(data)
        # The peer may have disconnected between lookup and send; report failure
        # to the caller as return False (the documented contract).
        # blind-except: single-connection send failure is reported via return False.
        except Exception:
            _logger.warning(
                "Failed to send to connection %s",
                connection_id,
                exc_info=True,
            )
            return False
        return True

    async def send_to_user(self, user_id: str, data: dict[str, object]) -> int:
        """Send data to all connections of a user.

        Returns:
            Number of connections data was sent to.
        """
        with self._lock:
            conn_ids = self._user_connections.get(user_id)
            if conn_ids is None:
                return 0
            infos = [
                self._connections[cid] for cid in conn_ids if cid in self._connections
            ]

        # Encode the identical payload ONCE, then fan it out as pre-encoded
        # bytes. Without this, each info.ws.send_json(data) re-runs
        # fast_json_dumps(data) per socket → N encodes of one payload. The
        # native WebSocket accepts pre-encoded UTF-8 text bytes via
        # _send_text_bytes (exactly what its send_json does internally); the
        # ASGI WebSocket takes the decoded str via send_text — both produce the
        # byte-identical frame send_json would have. Mirrors channels.py fan-out.
        payload = fast_json_dumps(data)
        count = 0
        for info in infos:
            try:
                ws = info.ws
                # dynamic-attr: ws may be our native WebSocket (exposes the _send_text_bytes fast path) or an ASGI WebSocket (does not) — a genuine cross-backend capability probe
                send_text_bytes = getattr(ws, "_send_text_bytes", None)
                if send_text_bytes is not None:
                    send_text_bytes(payload)
                else:
                    await ws.send_text(payload.decode())
                count += 1
            # One dead/disconnected connection must not abort delivery to the
            # user's other connections; the returned count reflects successes.
            # blind-except: per-user fan-out isolates one dead connection.
            except Exception:
                _logger.warning(
                    "Failed to send to connection %s for user %s",
                    info.connection_id,
                    user_id,
                    exc_info=True,
                )
        return count

    async def broadcast(self, data: dict[str, object]) -> int:
        """Broadcast data to all active connections.

        Returns:
            Number of connections data was sent to.
        """
        with self._lock:
            infos = list(self._connections.values())

        # Encode ONCE, fan out pre-encoded bytes — see send_to_user for the
        # rationale. A broadcast to N sockets becomes 1 encode + N sends
        # instead of N encodes of one identical payload.
        payload = fast_json_dumps(data)
        count = 0
        for info in infos:
            try:
                ws = info.ws
                # dynamic-attr: ws may be our native WebSocket (exposes the _send_text_bytes fast path) or an ASGI WebSocket (does not) — a genuine cross-backend capability probe
                send_text_bytes = getattr(ws, "_send_text_bytes", None)
                if send_text_bytes is not None:
                    send_text_bytes(payload)
                else:
                    await ws.send_text(payload.decode())
                count += 1
            # One dead/disconnected connection must not abort the broadcast to
            # all other connections; the returned count reflects successes.
            # blind-except: broadcast fan-out isolates one dead connection.
            except Exception:
                _logger.warning(
                    "Failed to broadcast to connection %s",
                    info.connection_id,
                    exc_info=True,
                )
        return count


# ---------------------------------------------------------------------------
# WebSocket Rate Limiter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WSRateLimitConfig:
    """Configuration for WebSocket rate limiting."""

    messages_per_second: int = 10
    messages_per_minute: int = 120
    burst_size: int = 20  # token bucket burst


@dataclass(slots=True)
class _TokenBucket:
    """Internal: token bucket state for a connection."""

    tokens: float
    last_refill: float
    per_second_count: int
    per_second_window: float
    per_minute_count: int
    per_minute_window: float


class WebSocketRateLimiter:
    """Per-connection rate limiting using token bucket algorithm.

    Combines token bucket (for burst control) with sliding window
    counters (per-second and per-minute limits).

    Usage:
        limiter = WebSocketRateLimiter()

        if limiter.check("conn_123"):
            # Message allowed
            process(message)
        else:
            # Rate limited
            await ws.send_json({"error": "rate_limited"})

        stats = limiter.get_stats("conn_123")
        limiter.reset("conn_123")
    """

    _NUM_SHARDS = 16

    def __init__(self, config: WSRateLimitConfig | None = None):
        self.config: WSRateLimitConfig = config or WSRateLimitConfig()
        # Shard buckets + locks by hash(connection_id) so thousands of live
        # connections don't serialize every message check on one global lock.
        self._shards: list[dict[str, _TokenBucket]] = [
            {} for _ in range(self._NUM_SHARDS)
        ]
        self._locks: list[threading.Lock] = [
            threading.Lock() for _ in range(self._NUM_SHARDS)
        ]

    def _shard_for(
        self, connection_id: str
    ) -> tuple[dict[str, _TokenBucket], threading.Lock]:
        """Return the (buckets, lock) shard owning this connection."""
        idx = hash(connection_id) % self._NUM_SHARDS
        return self._shards[idx], self._locks[idx]

    def _get_bucket(
        self, buckets: dict[str, _TokenBucket], connection_id: str
    ) -> _TokenBucket:
        """Get or create a token bucket. Must hold the shard's lock."""
        bucket = buckets.get(connection_id)
        if bucket is None:
            now = time.time()
            bucket = _TokenBucket(
                tokens=float(self.config.burst_size),
                last_refill=now,
                per_second_count=0,
                per_second_window=now,
                per_minute_count=0,
                per_minute_window=now,
            )
            buckets[connection_id] = bucket
        return bucket

    def _refill(self, bucket: _TokenBucket, now: float) -> None:
        """Refill tokens based on elapsed time. Must hold _lock."""
        elapsed = now - bucket.last_refill
        if elapsed > 0:
            refill_amount = elapsed * self.config.messages_per_second
            bucket.tokens = min(
                float(self.config.burst_size),
                bucket.tokens + refill_amount,
            )
            bucket.last_refill = now

    def check(self, connection_id: str) -> bool:
        """Check if a message is allowed for this connection.

        Consumes one token if allowed. Returns True if allowed, False if rate limited.
        """
        now = time.time()

        buckets, lock = self._shard_for(connection_id)
        with lock:
            bucket = self._get_bucket(buckets, connection_id)
            self._refill(bucket, now)

            # Check token bucket (burst)
            if bucket.tokens < 1.0:
                return False

            # Check per-second window
            if now - bucket.per_second_window >= 1.0:
                bucket.per_second_count = 0
                bucket.per_second_window = now
            if bucket.per_second_count >= self.config.messages_per_second:
                return False

            # Check per-minute window
            if now - bucket.per_minute_window >= 60.0:
                bucket.per_minute_count = 0
                bucket.per_minute_window = now
            if bucket.per_minute_count >= self.config.messages_per_minute:
                return False

            # Consume
            bucket.tokens -= 1.0
            bucket.per_second_count += 1
            bucket.per_minute_count += 1
            return True

    def reset(self, connection_id: str) -> None:
        """Reset rate limit state for a connection."""
        buckets, lock = self._shard_for(connection_id)
        with lock:
            buckets.pop(connection_id, None)

    def cleanup_stale(self, max_idle_seconds: float = 300.0) -> int:
        """Remove buckets that have been idle longer than max_idle_seconds.

        Call this periodically to prevent unbounded memory growth from
        disconnected connections whose buckets were never explicitly reset.

        Returns:
            Number of stale buckets removed.
        """
        now = time.time()
        cutoff = now - max_idle_seconds
        removed = 0
        for buckets, lock in zip(self._shards, self._locks):
            with lock:
                stale_ids = [
                    cid
                    for cid, bucket in buckets.items()
                    if bucket.last_refill < cutoff
                ]
                for cid in stale_ids:
                    del buckets[cid]
                    removed += 1
        return removed

    def get_stats(self, connection_id: str) -> dict[str, int | float]:
        """Get rate limit statistics for a connection.

        Returns:
            Dict with tokens_remaining, per_second_count, per_minute_count,
            burst_size, messages_per_second, messages_per_minute.
        """
        now = time.time()

        buckets, lock = self._shard_for(connection_id)
        with lock:
            bucket = buckets.get(connection_id)
            if bucket is None:
                return {
                    "tokens_remaining": float(self.config.burst_size),
                    "per_second_count": 0,
                    "per_minute_count": 0,
                    "burst_size": self.config.burst_size,
                    "messages_per_second": self.config.messages_per_second,
                    "messages_per_minute": self.config.messages_per_minute,
                }

            self._refill(bucket, now)

            # Reset window counts if expired
            per_sec = bucket.per_second_count
            if now - bucket.per_second_window >= 1.0:
                per_sec = 0
            per_min = bucket.per_minute_count
            if now - bucket.per_minute_window >= 60.0:
                per_min = 0

            return {
                "tokens_remaining": bucket.tokens,
                "per_second_count": per_sec,
                "per_minute_count": per_min,
                "burst_size": self.config.burst_size,
                "messages_per_second": self.config.messages_per_second,
                "messages_per_minute": self.config.messages_per_minute,
            }


# ---------------------------------------------------------------------------
# WebSocket Auth
# ---------------------------------------------------------------------------


def _default_ws_session_auth() -> object:
    """Build the default SessionAuth verifier for @ws_authenticated.

    Uses SECRET_KEY + the default session store — the SAME signing key and
    store the HTTP session middleware uses — so a WebSocket presents the same
    signed session cookie/token an HTTP request would, and it is verified
    identically (HMAC signature + session-store lookup). Imported lazily to
    keep it off the module-import hot path (cold start is import-dominated).
    """
    from hyperdjango.auth.sessions import SessionAuth
    from hyperdjango.conf import get_setting

    return SessionAuth(secret=get_setting("SECRET_KEY"))


async def _resolve_ws_principal(
    ws: WebSocket,
    session_auth: object | None,
    allow_insecure_raw_token: bool,
) -> object | None:
    """Resolve the VERIFIED principal for a WebSocket, or None if unauthenticated.

    The token is taken from ?token= (preferred) or the session cookie, then —
    unless the loudly-named dev-only raw-token mode is opted into — it is
    HMAC-verified against the session signing key and looked up in the session
    store, exactly like guard/websocket.py `_authenticate_ws`. The raw client
    value is NEVER returned as an identity; only a store-backed SessionUser is.
    """
    token = ws_auth_from_query(ws)
    if token is None:
        token = ws_auth_from_cookie(ws)
    if token is None:
        return None

    if allow_insecure_raw_token:
        # DEV-ONLY, opt-in: trust the raw ?token=/cookie value as the principal
        # WITHOUT signature verification. NEVER enable in production — anyone can
        # send ?token=admin and be authenticated as "admin". The flag name is
        # deliberately loud and the secure path below is the default.
        _logger.warning(
            "ws_authenticated: INSECURE allow_insecure_raw_token mode is active — "
            "client identity is NOT verified. Use only in development."
        )
        return token

    from hyperdjango.auth.sessions import _is_user_session
    from hyperdjango.auth.user import SessionUser

    auth = session_auth if session_auth is not None else _default_ws_session_auth()
    # HMAC-verify the signed cookie/token and extract the session id.
    session_id = auth._verify_session_cookie(token)
    if not session_id:
        return None
    # Session-store lookup — an unforgeable signature over a session id that no
    # longer exists (logged out / expired) must still be rejected.
    result = auth.store.get(session_id)
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        result = await result
    if result is None:
        return None
    # Identity gate — mirror the HTTP path (auth/sessions.py: SessionAuth only
    # promotes to SessionUser when _is_user_session(data)). A legitimately-signed
    # ANONYMOUS session (flash/cart/wizard state, no user_id/id/pk/username) must
    # NOT authenticate: fail closed so the connection is denied exactly as a
    # missing session (close 4001).
    if not _is_user_session(result):
        return None
    return SessionUser(result)


def ws_authenticated(
    handler: Callable | None = None,
    *,
    session_auth: object | None = None,
    allow_insecure_raw_token: bool = False,
) -> Callable:
    """Decorator requiring VERIFIED WebSocket authentication.

    The ?token= query value (preferred) or the session cookie is treated as a
    signed session token: it is HMAC-verified against the session signing key
    and looked up in the session store — the same verification guard_websocket()
    performs. On success the handler receives the VERIFIED ``SessionUser`` as
    its second argument; an unsigned/forged token is rejected with close 4001.

    Usage:
        @app.websocket("/ws/chat")
        @ws_authenticated
        async def chat(ws, user):        # user is a verified SessionUser
            await ws.accept()
            ...

        # Explicit verifier (app-configured signing key / store):
        @ws_authenticated(session_auth=my_session_auth)
        async def chat(ws, user): ...

    Args:
        handler: The WebSocket handler (supplied when used bare, ``@ws_authenticated``).
        session_auth: SessionAuth used to verify + look up the session. Defaults
            to one built from SECRET_KEY + the default store.
        allow_insecure_raw_token: DEV-ONLY. When True, the raw client-supplied
            token/cookie value is trusted as the principal WITHOUT verification.
            Never enable in production — it lets any client claim any identity.

    If verification fails, accepts then closes the WebSocket with code 4001
    (ASGI requires accept before close).
    """

    def _decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(ws: WebSocket, *args: object, **kwargs: object) -> object:
            # CSWSH defense first: reject a disallowed cross-origin handshake
            # before touching credentials (a hijacking page's WS never authenticates).
            if not is_ws_origin_allowed(ws):
                await ws.accept()
                await ws.close(code=4403, reason="Origin not allowed")
                return None
            user = await _resolve_ws_principal(
                ws, session_auth, allow_insecure_raw_token
            )
            if user is None:
                # ASGI requires accepting the WebSocket before closing it.
                # Sending close without accept causes protocol errors.
                await ws.accept()
                await ws.close(code=4001, reason="Authentication required")
                return None
            return await func(ws, user, *args, **kwargs)

        return wrapper

    # Bare form: @ws_authenticated. Parameterized form: @ws_authenticated(...).
    if handler is not None:
        return _decorate(handler)
    return _decorate


def ws_auth_from_query(ws: WebSocket) -> str | None:
    """Extract the raw, UNVERIFIED auth token from the query string: ?token=...

    SECURITY: the returned value is attacker-controlled input, NOT an
    authenticated identity. It must be signature-verified and resolved against
    the session store before use — see ``ws_authenticated``. Never treat this
    return value as a user id.

    Args:
        ws: WebSocket instance.

    Returns:
        Raw token string if present, None otherwise.
    """
    qs = ws.query_string
    if not qs:
        return None
    params = parse_qs(qs)
    tokens = params.get("token")
    if tokens and tokens[0]:
        return tokens[0]
    return None


def ws_auth_from_cookie(ws: WebSocket, cookie_name: str = "sessionid") -> str | None:
    """Extract the raw, UNVERIFIED session cookie value.

    SECURITY: the returned value is attacker-controlled input, NOT an
    authenticated identity. It must be signature-verified and resolved against
    the session store before use — see ``ws_authenticated``. Never treat this
    return value as a user id.

    Uses simple semicolon-split parsing. Does not handle quoted cookie
    values or escaped semicolons (these are extremely rare in session IDs
    and not needed for standard session cookie extraction).

    Args:
        ws: WebSocket instance.
        cookie_name: Name of the session cookie (default: "sessionid").

    Returns:
        Raw cookie value if present, None otherwise.
    """
    cookie_header = ws.headers.get("cookie", "")
    if not cookie_header:
        return None

    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip() == cookie_name:
            val = value.strip()
            if val:
                return val
    return None
