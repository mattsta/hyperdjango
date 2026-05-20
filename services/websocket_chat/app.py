"""
Example: WebSocket Chat with HyperDjango.

Demonstrates:
- HyperApp with native Zig HTTP server + RFC 6455 WebSocket (SIMD XOR unmasking)
- Channel pub/sub (InMemoryChannelLayer) with presence tracking and message history
- Room system with member roles, rate limiting, typing indicators
- ConnectionManager for WebSocket lifecycle management
- WebSocketRateLimiter with token bucket + sliding window
- Session auth on both HTTP and WebSocket connections
- CSRF protection (double-submit cookie pattern)
- SecurityHeaders middleware (X-Frame-Options, CSP, HSTS)
- htmx for interactive room management (no page reloads)
- Model definitions with validation, database persistence
- Template rendering with the Zig template engine
- Self-managing setup via `hyper setup`
- Cooperative channel->WebSocket delivery (asyncio.Queue fed via
  call_soon_threadsafe, writer as a task) — no thread parked per connection,
  so it runs correctly under the default shared event-loop model
  (WEBSOCKET_CONCURRENCY=shared) as well as the thread-per-connection opt-out
  (WEBSOCKET_CONCURRENCY=thread). See docs/server.md and docs/realtime.md.

Setup:
    createdb chat
    uv run hyper setup --app services.websocket_chat.app:app --seed services.websocket_chat.seed:run

Run:
    uv run hyper run --app services.websocket_chat.app:app
"""

import asyncio
import contextlib
import html as _html
import threading
from pathlib import Path
from urllib.parse import parse_qs

from hyperdjango import BaseModel as ValidatedModel
from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.channels import InMemoryChannelLayer, set_channel_layer
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.db.pgzig_connection import IntegrityError
from hyperdjango.guard import Require, guard, guard_websocket
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.realtime import (
    ConnectionManager,
    LiveQuery,
    Room,
    RoomConfig,
    WebSocketRateLimiter,
    WSRateLimitConfig,
)
from hyperdjango.signals import post_delete, post_save
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.validation.core.fields import Field as VField
from hyperdjango.validation.core.validator import ValidationErrors
from hyperdjango.websocket import WebSocketDisconnect

_APP_DIR = Path(__file__).parent

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = get_setting("DATABASE_URL") or "postgres://localhost/chat"

DATABASE_URL = get_setting("DATABASE_URL")

MAX_MESSAGE_LENGTH = 4000


# --- Models ---


class User(TimestampMixin, Model):
    class Meta:
        table = "chat_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    password_hash: str = Field(exclude=True)


class ChatRoom(TimestampMixin, Model):
    class Meta:
        table = "chat_rooms"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
    description: str = Field(default="")
    created_by: int = Field(foreign_key=User)


class ChatMessage(TimestampMixin, Model):
    class Meta:
        table = "chat_messages"

    id: int = Field(primary_key=True, auto=True)
    room_id: int = Field(foreign_key=ChatRoom)
    user_id: int = Field(foreign_key=User)
    username: str = Field()
    content: str = Field()


# --- App setup ---

app = HyperApp(
    title="WebSocket Chat",
    database=DATABASE_URL,
    templates=str(_APP_DIR / "templates"),
    static=str(_APP_DIR / "static"),
    debug=True,
)

# Middleware — order matters: outermost first
app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware())

csrf = CSRFMiddleware(
    secret=get_setting("CSRF_SECRET"),
    exempt_paths={"/health", "/ws/chat", "/ws/live", "/api/rooms", "/api/live"},
)
app.use(csrf)

_session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=get_setting("SESSION_SIGNING_KEY"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=get_setting("SESSION_SECRET"),
    token_engine=_session_engine,
)
app.use(auth)


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    logger.exception("Unhandled error: {exc}", exc=exc)
    return Response.json({"detail": "Internal server error"}, status=500)


# HyperAdmin
admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Chat Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)

# Channel layer + real-time infrastructure
layer = InMemoryChannelLayer()
set_channel_layer(layer)

rate_limiter = WebSocketRateLimiter(
    WSRateLimitConfig(
        messages_per_second=5,
        messages_per_minute=60,
        burst_size=10,
    )
)

conn_manager = ConnectionManager(layer)

# Room cache — lazily populated on first WebSocket connect per room
_rooms: dict[int, Room] = {}
_rooms_lock = threading.Lock()
_room_config = RoomConfig(
    max_members=100,
    history_size=100,
    rate_limit=30,
    require_auth=True,
)


def _get_room(room_id: int) -> Room:
    """Get or create a Room instance for the given room ID."""
    with _rooms_lock:
        if room_id not in _rooms:
            _rooms[room_id] = Room(str(room_id), layer, _room_config)
        return _rooms[room_id]


# --- LiveQuery: model change subscriptions over WebSocket ---
live = LiveQuery(layer)


@post_save.connect(dispatch_uid="livequery_post_save")
async def _livequery_on_save(sender, instance=None, created=False, **kwargs):
    """Bridge post_save signals into LiveQuery notifications."""
    if instance is None:
        return
    model_name = type(instance).__name__
    if model_name not in ("ChatMessage", "ChatRoom"):
        return
    data = instance.to_dict()
    if created:
        await live.notify_create(model_name, instance.id, data)
    else:
        await live.notify_update(model_name, instance.id, data, list(data.keys()))


@post_delete.connect(dispatch_uid="livequery_post_delete")
async def _livequery_on_delete(sender, instance=None, **kwargs):
    """Bridge post_delete signals into LiveQuery notifications."""
    if instance is None:
        return
    model_name = type(instance).__name__
    if model_name in ("ChatMessage", "ChatRoom"):
        await live.notify_delete(model_name, instance.id)


def _parse_live_filter(spec: str) -> dict[str, object]:
    """Parse a filter query param spec into a dict.

    Format: "field1:value1,field2:value2" with optional type suffix:
      room_id:5      → {"room_id": 5}   (int auto-detected)
      status:active  → {"status": "active"}
      is_pinned:true → {"is_pinned": True}  (bool auto-detected)
    """
    filters: dict[str, object] = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        key, _, raw = pair.partition(":")
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        # Auto-detect scalar type
        if raw.lower() == "true":
            filters[key] = True
        elif raw.lower() == "false":
            filters[key] = False
        elif raw.lower() in ("null", "none"):
            filters[key] = None
        elif raw.lstrip("-").isdigit():
            filters[key] = int(raw)
        else:
            filters[key] = raw
    return filters


@app.websocket("/ws/live")
async def live_query_ws(ws):
    """LiveQuery WebSocket — subscribe to filtered model change events.

    Connect:
      ws://host/ws/live?models=ChatMessage
      ws://host/ws/live?models=ChatMessage&filter_ChatMessage=room_id:5
      ws://host/ws/live?models=ChatMessage,ChatRoom&filter_ChatMessage=room_id:5

    Receives: {"type":"model_change","model_name":"ChatMessage","action":"create",...}

    Demonstrates LiveQuery with filters — LiveQuery.watch(model_name, filters=...)
    gates events at the channel subscription level, so clients only receive
    changes matching all filter key/value pairs.
    """
    params = parse_qs(ws.query_string)
    models_param = params.get("models", ["ChatMessage"])
    model_names = [m.strip() for m in models_param[0].split(",") if m.strip()]

    # Parse per-model filter specs: filter_ModelName=field:value,field:value
    filters_by_model: dict[str, dict[str, object]] = {}
    for model_name in model_names:
        filter_param = params.get(f"filter_{model_name}")
        if filter_param and filter_param[0]:
            filters_by_model[model_name] = _parse_live_filter(filter_param[0])

    sub_ids: list[str] = []
    channel_subs: list[tuple[str, str]] = []
    # Cooperative outgoing bridge (see the /ws/chat handler for the rationale):
    # channel callbacks fire from other threads, so hop each onto this
    # connection's loop with call_soon_threadsafe into an asyncio.Queue — never
    # a blocking thread-queue get. Works under both server models.
    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue = asyncio.Queue(maxsize=500)

    def _offer(data):
        if not outgoing.full():
            outgoing.put_nowait(data)

    def _on_msg(msg):
        with contextlib.suppress(RuntimeError):  # loop closing/closed
            loop.call_soon_threadsafe(_offer, msg.data)

    for model_name in model_names:
        model_filters = filters_by_model.get(model_name)
        sub_id = live.watch(model_name, filters=model_filters)
        sub_ids.append(sub_id)
        ch = live._model_channel(model_name)

        # Create a filter_fn that mirrors LiveQuery.watch's filtering logic
        # (the channel subscribe filter_fn only receives messages that match).
        if model_filters:
            frozen = dict(model_filters)

            def _match(msg, _f=frozen):
                data = msg.data.get("data") if isinstance(msg.data, dict) else None
                if not isinstance(data, dict):
                    return False
                for k, v in _f.items():
                    if data.get(k) != v:
                        return False
                return True

            csid = ch.subscribe(_on_msg, filter_fn=_match)
        else:
            csid = ch.subscribe(_on_msg)
        channel_subs.append((model_name, csid))

    await ws.send_json(
        {
            "type": "subscribed",
            "models": model_names,
            "filters": filters_by_model,
        }
    )

    try:

        async def _read():
            try:
                async for data in ws.iter_json():
                    if isinstance(data, dict) and data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
            except WebSocketDisconnect, OSError, Exception:
                pass

        async def _write():
            while True:
                msg = await outgoing.get()
                try:
                    await ws.send_json(msg)
                except WebSocketDisconnect, OSError, Exception:
                    break

        writer = asyncio.create_task(_write())
        try:
            await _read()
        finally:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
    finally:
        for model_name, csid in channel_subs:
            live._model_channel(model_name).unsubscribe(csid)
        for sid in sub_ids:
            live.unwatch(sid)


@app.get("/api/live/status")
async def live_status(request):
    """LiveQuery subscription status (for debugging/monitoring)."""
    return Response.json(
        {
            "active_subscriptions": live.subscription_count(),
            "watched_models": live.watched_models(),
        }
    )


# --- Helpers ---


async def validate_form(request, schema_cls):
    raw = await request.form()
    flat = {}
    for key, val in raw.items():
        if key == "_csrf_token":
            continue
        if isinstance(val, list):
            flat[key] = val[0] if val else ""
        else:
            flat[key] = val
    return schema_cls.model_validate_strings(flat)


# --- Validation schemas ---


class LoginSchema(ValidatedModel):
    username: str = VField(min_length=1, strip_whitespace=True)
    password: str = VField(min_length=1)


class RegisterSchema(ValidatedModel):
    username: str = VField(min_length=1, max_length=30, strip_whitespace=True)
    password: str = VField(min_length=8)


class CreateRoomSchema(ValidatedModel):
    name: str = VField(min_length=1, max_length=100, strip_whitespace=True)
    description: str = VField(default="", max_length=200, strip_whitespace=True)


async def _get_room_history(room_id, limit=50):
    """Fetch and format message history for a room."""
    messages = (
        await ChatMessage.objects.filter(room_id=room_id)
        .order_by("-id")
        .limit(limit)
        .all()
    )
    return [
        {
            "id": m.id,
            "username": m.username,
            "content": m.content,
            "timestamp": str(m.created_at),
        }
        for m in reversed(messages)
    ]


def _user_id(request):
    """Extract integer user ID from request.user dict."""
    return (request.user.id or 0) if request.user is not None else 0


# --- Startup hook ---


@app.on_startup
async def _startup():
    try:
        room_count = await ChatRoom.objects.count()
        msg_count = await ChatMessage.objects.count()
        logger.info(
            "Chat ready: {rooms} rooms, {msgs} messages",
            rooms=room_count,
            msgs=msg_count,
        )
    except Exception:
        logger.warning(
            "Chat tables not found — run: "
            "uv run hyper setup --app services.websocket_chat.app:app "
            "--seed services.websocket_chat.seed:run"
        )


# --- Auth routes ---


@app.get("/login")
async def login_page(request):
    if request.user:
        return Response.redirect("/")
    return app.render(
        "login.html",
        {
            "user": None,
            "error": None,
            "csrf_token": request.cookies.get("csrftoken", ""),
        },
    )


@app.post("/login")
async def login_submit(request):
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        return app.render(
            "login.html",
            {
                "user": None,
                "error": "Too many login attempts — please wait a few minutes",
                "csrf_token": request.cookies.get("csrftoken", ""),
            },
        )

    try:
        data = await validate_form(request, LoginSchema)
    except ValidationErrors as exc:
        return app.render(
            "login.html",
            {
                "user": None,
                "error": str(exc),
                "csrf_token": request.cookies.get("csrftoken", ""),
            },
        )

    user = await User.objects.filter(username=data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        auth.record_failed_login(client_ip)
        return app.render(
            "login.html",
            {
                "user": None,
                "error": "Invalid username or password",
                "csrf_token": request.cookies.get("csrftoken", ""),
            },
        )

    auth.clear_login_attempts(client_ip)
    resp = Response.redirect("/")
    session = await build_session_data(user.id, get_db(), username=user.username)
    auth.login(resp, session, request)
    return resp


@app.get("/register")
async def register_page(request):
    if request.user:
        return Response.redirect("/")
    return app.render(
        "register.html",
        {
            "user": None,
            "error": None,
            "csrf_token": request.cookies.get("csrftoken", ""),
        },
    )


@app.post("/register")
async def register_submit(request):
    try:
        data = await validate_form(request, RegisterSchema)
    except ValidationErrors as exc:
        return app.render(
            "register.html",
            {
                "user": None,
                "error": str(exc),
                "csrf_token": request.cookies.get("csrftoken", ""),
            },
        )

    pw_hash = hash_password(data.password)
    user = User(username=data.username, password_hash=pw_hash)
    try:
        await user.save()
    except IntegrityError:
        return app.render(
            "register.html",
            {
                "user": None,
                "error": "Username already taken",
                "csrf_token": request.cookies.get("csrftoken", ""),
            },
        )

    resp = Response.redirect("/")
    session = await build_session_data(user.id, get_db(), username=data.username)
    auth.login(resp, session, request)
    return resp


@app.post("/logout")
async def logout_post(request):
    resp = Response.redirect("/login")
    if request.session_id:
        auth.logout(resp, request.session_id)
    return resp


# --- Room routes ---


@app.get("/")
@guard(Require.authenticated(redirect_url="/login"))
async def room_list(request):
    rooms = await ChatRoom.objects.order_by("id").all()
    return app.render(
        "index.html",
        {
            "user": request.user,
            "rooms": rooms,
            "room_count": len(rooms),
            "csrf_token": request.cookies.get("csrftoken", ""),
        },
    )


@app.post("/rooms/create")
@guard(Require.authenticated(redirect_url="/login"))
async def create_room(request):
    """Create a new room. Returns HTML partial for htmx or redirects."""
    try:
        data = await validate_form(request, CreateRoomSchema)
    except ValidationErrors as exc:
        if request.headers.get("hx-request"):
            return Response.html(f'<div class="error">{_html.escape(str(exc))}</div>')
        return Response.redirect("/")

    room = ChatRoom(
        name=data.name,
        description=data.description,
        created_by=_user_id(request),
    )
    try:
        await room.save()
    except IntegrityError:
        if request.headers.get("hx-request"):
            return Response.html('<div class="error">Room already exists</div>')
        return Response.redirect("/")

    # htmx request — return the new room row for in-place insertion (escaped!)
    if request.headers.get("hx-request"):
        ename = _html.escape(str(room.name))
        edesc = _html.escape(str(room.description))
        etime = _html.escape(str(room.created_at))
        return Response.html(
            f'<tr style="border-bottom: 1px solid #eee;">'
            f'<td style="padding: 0.5rem; font-weight: 500;">{ename}</td>'
            f'<td style="padding: 0.5rem; color: #666;">{edesc}</td>'
            f'<td style="padding: 0.5rem; color: #999; font-size: 0.85rem;">{etime}</td>'
            f'<td style="padding: 0.5rem;"><a href="/rooms/{room.id}" class="btn btn-sm">Join</a></td>'
            f"</tr>"
        )

    return Response.redirect("/")


@app.get("/rooms/{room_id:int}")
@guard(Require.authenticated(redirect_url="/login"))
async def room_page(request, room_id):
    room = await ChatRoom.objects.filter(id=room_id).first()
    if not room:
        raise HTTPException(404, "Room not found")

    return app.render(
        "room.html",
        {
            "user": request.user,
            "room_id": room.id,
            "room_name": room.name,
            "csrf_token": request.cookies.get("csrftoken", ""),
        },
    )


# --- API routes ---


@app.get("/api/rooms/")
async def api_list_rooms(request):
    """List all rooms with member counts (JSON API)."""
    rooms = await ChatRoom.objects.order_by("id").all()
    result = []
    for r in rooms:
        msg_count = await ChatMessage.objects.filter(room_id=r.id).count()
        result.append(
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "message_count": msg_count,
                "created_at": str(r.created_at),
            }
        )
    return Response.json(result)


@app.get("/api/rooms/{room_id:int}/history")
async def room_history(request, room_id):
    """Fetch message history for a room (JSON API)."""
    history = await _get_room_history(room_id)
    return Response.json(history)


@app.get("/api/rooms/{room_id:int}/search")
@guard(Require.authenticated(redirect_url="/login"))
async def room_search(request, room_id):
    """Search messages within a room by keyword."""
    q = request.query("q", "").strip()
    if not q or len(q) < 2:
        raise HTTPException(400, "Search query must be at least 2 characters")

    messages = await (
        ChatMessage.objects.filter(room_id=room_id, content__icontains=q)
        .order_by("-id")
        .limit(50)
        .all()
    )
    return Response.json(
        [
            {
                "id": m.id,
                "username": m.username,
                "content": m.content,
                "timestamp": str(m.created_at),
            }
            for m in messages
        ]
    )


app.mount_health()
mount_docs(app)


# --- WebSocket handler ---


@app.websocket("/ws/chat")
@guard_websocket(auth, Require.authenticated())
async def chat_ws(ws):
    """Main chat WebSocket handler.

    Demonstrates: @guard_websocket auth, Channel pub/sub with presence,
    Room system with typing indicators, rate limiting, message persistence,
    and bidirectional async message loop.
    """
    user = ws.user
    user_id = str(user["id"])
    username = user["username"]

    # Parse and validate room_id from query string
    params = parse_qs(ws.query_string)
    room_id_list = params.get("room_id", [])
    if not room_id_list:
        await ws.send_json({"type": "error", "message": "room_id required"})
        await ws.close(4002, "room_id required")
        return

    try:
        room_id = int(room_id_list[0])
        if room_id < 1 or room_id > 2147483647:  # PostgreSQL integer range
            raise ValueError("out of range")
    except ValueError, IndexError, OverflowError:
        await ws.send_json({"type": "error", "message": "Invalid room_id"})
        await ws.close(4002, "Invalid room_id")
        return

    # Verify room exists in database
    room_exists = await ChatRoom.objects.filter(id=room_id).first()
    if not room_exists:
        await ws.send_json({"type": "error", "message": "Room not found"})
        await ws.close(4004, "Room not found")
        return

    # Get Room instance (lazy creation with channel layer)
    room = _get_room(room_id)

    # Register with ConnectionManager for lifecycle tracking
    conn_info = await conn_manager.connect(ws, user_id=user_id)
    connection_id = conn_info.connection_id

    # Join room — triggers presence broadcast to existing members
    await room.join(user_id, username, ws=ws)

    # Send message history from database
    history = await _get_room_history(room_id)
    await ws.send_json({"type": "history", "messages": history})

    # Send current presence list
    members = room.get_members()
    await ws.send_json(
        {
            "type": "presence",
            "members": [
                {"user_id": m.user_id, "display_name": m.display_name} for m in members
            ],
        }
    )

    # Subscribe to the room's channel for outgoing messages.
    #
    # Channel callbacks fire from arbitrary threads (other users' request
    # contexts / server worker threads), so we hop each message onto THIS
    # connection's event loop with call_soon_threadsafe and hand it to an
    # asyncio.Queue. This is a fully COOPERATIVE bridge: it never parks an OS
    # thread per connection waiting on a blocking queue. That matters because
    # it works identically under both server models — the default
    # thread-per-connection model AND the shared event-loop pool
    # (HYPER_WEBSOCKET_CONCURRENCY=shared), where a blocking
    # `run_in_executor(None, ...)` get would exhaust the shared loop's executor
    # and stall delivery. Prefer this pattern in your own handlers.
    channel = room._channel
    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def _offer(msg):
        if not outgoing.full():
            outgoing.put_nowait(msg)

    def on_channel_msg(msg):
        # Runs on some other thread → schedule onto this connection's loop.
        with contextlib.suppress(RuntimeError):  # loop closing/closed
            loop.call_soon_threadsafe(_offer, msg)

    sub_id = channel.subscribe(on_channel_msg)

    # Bidirectional message loop
    try:

        async def read_ws():
            """Read from WebSocket, publish to room."""
            try:
                async for data in ws.iter_json():
                    msg_type = data.get("type", "")

                    if msg_type == "message":
                        # Rate limit check (token bucket + sliding window)
                        if not rate_limiter.check(connection_id):
                            await ws.send_json(
                                {
                                    "type": "rate_limited",
                                    "message": "Too many messages — slow down",
                                }
                            )
                            continue

                        content = data.get("content", "").strip()
                        if not content:
                            continue
                        if len(content) > MAX_MESSAGE_LENGTH:
                            await ws.send_json(
                                {
                                    "type": "error",
                                    "message": f"Message too long (max {MAX_MESSAGE_LENGTH})",
                                }
                            )
                            continue

                        # Persist message to database via ORM
                        msg = ChatMessage(
                            room_id=room_id,
                            user_id=int(user_id),
                            username=username,
                            content=content,
                        )
                        await msg.save()

                        # Broadcast via Room (publishes to channel, adds to history)
                        try:
                            await room.send_message(user_id, content)
                        except PermissionError:
                            await ws.send_json(
                                {
                                    "type": "rate_limited",
                                    "message": "Room rate limit exceeded",
                                }
                            )
                        except ValueError as ve:
                            await ws.send_json({"type": "error", "message": str(ve)})

                    elif msg_type == "typing":
                        await room.set_typing(user_id, data.get("typing", False))

            except WebSocketDisconnect:
                pass

        async def write_ws():
            """Drain the channel queue, send to WebSocket (cooperative)."""
            while True:
                msg = await outgoing.get()
                try:
                    await ws.send_json(msg.data)
                except WebSocketDisconnect:
                    break

        # Run the writer as a task and cancel it when the reader ends (client
        # disconnected) — no sentinel value needed, no thread parked on a
        # blocking get.
        writer = asyncio.create_task(write_ws())
        try:
            await read_ws()
        finally:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    except WebSocketDisconnect:
        pass
    finally:
        channel.unsubscribe(sub_id)
        await room.leave(user_id)
        rate_limiter.reset(connection_id)
        await conn_manager.disconnect(connection_id)


# ---------------------------------------------------------------------------
# HyperAdmin model registration
# ---------------------------------------------------------------------------

admin.register(
    User,
    list_display=["id", "username", "created_at"],
    search_fields=["username"],
)

admin.register(
    ChatRoom,
    list_display=["id", "name", "created_by", "created_at"],
    search_fields=["name"],
)

admin.register(
    ChatMessage,
    list_display=["id", "room_id", "username", "content", "created_at"],
    search_fields=["username", "content"],
    list_filter=["room_id"],
    ordering="-created_at",
)


if __name__ == "__main__":
    app.run()
