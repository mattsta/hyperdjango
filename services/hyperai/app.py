"""
HyperAI — Multi-conversation AI chat service.

Showcases HyperDjango's real-time SSE streaming, API key management,
tiered rate limiting, and OpenAI-compatible REST endpoint.

Usage:
    uv run hyper run services/hyperai/app.py
"""

import asyncio
import json
import re
import secrets
import sys as _sys
import threading
import time
from pathlib import Path

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.cache import LocMemCache
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.expressions import F
from hyperdjango.guard import Require, guard
from hyperdjango.logging import logger
from hyperdjango.openapi import mount_docs
from hyperdjango.ratelimit import (
    DatabaseRateLimitBackend,
    InMemoryRateLimitBackend,
    RateLimitMiddleware,
)
from hyperdjango.rest import CursorPagination
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.telemetry import configure_from_settings

from .models import APIKey, Conversation, Message, Tier, UsageLog, User

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parent

_DEBUG = get_setting("DEBUG")

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = get_setting("DATABASE_URL") or "postgres://localhost/hyperai"

app = HyperApp(
    title="HyperAI",
    database=get_setting("DATABASE_URL"),
    templates=str(_APP_DIR / "templates"),
    static=str(_APP_DIR / "static"),
    debug=_DEBUG,
    secret_key=get_setting("SECRET_KEY"),
)

# --- Native telemetry (v0.15.1) -----------------------------------------------
if _DEBUG:
    DEFAULTS["TELEMETRY_ENABLED"] = True
    DEFAULTS["TELEMETRY_SAMPLE_RATIO"] = 1.0
_telemetry = configure_from_settings(app)
if _telemetry is not None and _telemetry.prometheus_sink is not None:
    app.get("/metrics")(_telemetry.prometheus_sink.handler)

# Middleware stack (outermost first)
app.use(TimingMiddleware())
app.use(
    SecurityHeadersMiddleware(
        hsts=not _DEBUG,
        csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:",
    )
)
app.use(
    CORSMiddleware(
        origins=get_setting("CORS_ORIGINS", ["*"]),
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        headers=["Content-Type", "Authorization", "X-API-Key"],
    )
)
app.use(
    CSRFMiddleware(
        secret=get_setting("CSRF_SECRET"),
        exempt_paths={
            "/api/v1/chat/completions",
        },
    )
)
app.use(RateLimitMiddleware(max_requests=120, window=60))

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

_cache = LocMemCache(max_size=256)

# HyperAdmin
admin = HyperAdmin(
    app,
    prefix="/admin",
    title="HyperAI Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)

# ---------------------------------------------------------------------------
# Tier limits — requests per minute
# ---------------------------------------------------------------------------

TIER_LIMITS: dict[Tier, int] = {
    Tier.FREE: 20,
    Tier.PRO: 100,
    Tier.ENTERPRISE: 1000,
}

TIER_TOKEN_LIMITS: dict[Tier, int] = {
    Tier.FREE: 4_000,
    Tier.PRO: 32_000,
    Tier.ENTERPRISE: 128_000,
}

# Per-user rate limiter — InMemory for single-server, Database for distributed.
# Set RATE_LIMIT_BACKEND=database to use PostgreSQL UNLOGGED table (multi-server).
_RATE_BACKEND = get_setting("RATE_LIMIT_BACKEND", "memory")
_tier_limiter: InMemoryRateLimitBackend | DatabaseRateLimitBackend = (
    InMemoryRateLimitBackend()
)

# ---------------------------------------------------------------------------
# Simulated AI responses — realistic multi-topic responses
# ---------------------------------------------------------------------------

SIMULATED_RESPONSES: list[str] = [
    (
        "That's a great question! Let me break this down step by step.\n\n"
        "First, it's important to understand the underlying principles. "
        "When we talk about building scalable web applications, we need to "
        "consider several key factors: database design, caching strategy, "
        "connection pooling, and horizontal scaling.\n\n"
        "For database design, I recommend starting with a well-normalized "
        "schema and then selectively denormalizing based on your read patterns. "
        "PostgreSQL is an excellent choice here because it offers both relational "
        "integrity and JSON flexibility when you need it.\n\n"
        "For caching, use PostgreSQL UNLOGGED tables for fast key-value "
        "lookups. This keeps your infrastructure simpler with no external "
        "dependencies.\n\n"
        "Would you like me to dive deeper into any of these areas?"
    ),
    (
        "Here's a Python example that demonstrates the pattern:\n\n"
        "```python\n"
        "from hyperdjango import HyperApp, Response\n"
        "from hyperdjango.models import Model, Field\n\n"
        "class Task(Model):\n"
        "    class Meta:\n"
        '        table = "tasks"\n\n'
        "    id: int = Field(primary_key=True, auto=True)\n"
        "    title: str = Field(max_length=200)\n"
        "    done: bool = Field(default=False)\n\n"
        "app = HyperApp(title='Task API')\n\n"
        '@app.get("/tasks")\n'
        "async def list_tasks(request):\n"
        "    tasks = await Task.objects.all()\n"
        "    return [t.model_dump() for t in tasks]\n"
        "```\n\n"
        "This gives you a fully typed, validated model with automatic "
        "SQL generation, native Zig-accelerated query execution, and "
        "SIMD-validated fields. The performance difference compared to "
        "traditional ORMs is significant — we're talking microsecond-level "
        "query times versus milliseconds."
    ),
    (
        "I'd be happy to help you understand that concept better.\n\n"
        "Server-Sent Events (SSE) provide a simple, efficient mechanism for "
        "server-to-client streaming over HTTP. Unlike WebSockets, SSE uses "
        "a standard HTTP connection and is inherently unidirectional — the "
        "server pushes events to the client.\n\n"
        "The key advantages of SSE for AI chat applications are:\n\n"
        "1. **Automatic reconnection** — the browser handles reconnects natively\n"
        "2. **Event IDs** — allows resuming from the last received event\n"
        "3. **Simplicity** — no upgrade handshake, works through proxies\n"
        "4. **Back-pressure** — TCP flow control prevents buffer overflow\n\n"
        "For streaming AI responses, SSE is the ideal transport because the "
        "communication is naturally one-directional: the model generates tokens "
        "and streams them to the client. The user sends new messages via "
        "standard POST requests.\n\n"
        "This is exactly the pattern used by OpenAI, Anthropic, and other "
        "major AI API providers."
    ),
    (
        "Let me walk you through the architecture.\n\n"
        "The system is built around three core layers:\n\n"
        "**1. Request Layer** — Handles incoming HTTP requests with native Zig "
        "parsing. The radix-trie router resolves routes in under 500 nanoseconds. "
        "Request bodies are lazily parsed (JSON, form data, multipart) only "
        "when accessed.\n\n"
        "**2. Business Logic Layer** — Your async Python handlers process the "
        "request. The ORM provides a Django-like QuerySet API with native "
        "PostgreSQL execution via pg.zig. Prepared statements are cached and "
        "reused automatically.\n\n"
        "**3. Response Layer** — Supports JSON, HTML, streaming, SSE, file "
        "downloads, and redirects. The native JSON serializer is SIMD-accelerated, "
        "producing responses 6x faster than stdlib json.\n\n"
        "Each layer is designed to minimize allocations and maximize throughput. "
        "The result is a framework that handles 13,000+ requests per second "
        "on a single machine."
    ),
    (
        "Absolutely, security is paramount. Here are the key practices:\n\n"
        "**Authentication**: Use session-based auth with HMAC-signed cookies. "
        "Never use JWT for session management — they can't be revoked and "
        "increase your attack surface.\n\n"
        "**Password Storage**: Always use argon2id with appropriate memory "
        "and iteration parameters. Never roll your own hashing.\n\n"
        "**CSRF Protection**: Double-submit cookie pattern on all state-changing "
        "endpoints. API endpoints authenticated via API keys or OAuth2 tokens "
        "are exempt since they use non-cookie credentials.\n\n"
        "**Rate Limiting**: Tiered per-user limits prevent abuse. Use sliding "
        "window counters stored in PostgreSQL UNLOGGED tables for multi-server "
        "coordination.\n\n"
        "**Input Validation**: SIMD-accelerated validation at the model layer "
        "catches malformed data before it reaches your database. Email, URL, "
        "and string length validation run at 50+ million operations per second.\n\n"
        "Shall I elaborate on any of these topics?"
    ),
]

_response_index = 0
_response_index_lock = threading.Lock()


def _pick_response() -> str:
    """Round-robin through simulated responses."""
    global _response_index
    with _response_index_lock:
        response = SIMULATED_RESPONSES[_response_index % len(SIMULATED_RESPONSES)]
        _response_index += 1
    return response


def _count_tokens(text: str) -> int:
    """Approximate token count (words as proxy)."""
    return len(text.split())


def _ctx(request, **extra) -> dict:
    """Build template context with CSRF token and user info."""
    ctx: dict = {
        "csrf_token": request.cookies.get("csrftoken", ""),
        "user": None,
    }
    if request.user is not None and request.user.is_authenticated:
        ctx["user"] = request.user
    ctx.update(extra)
    return ctx


async def _get_user_from_session(request) -> User | None:
    """Extract user from session data."""
    if not request.user:
        return None
    user_id = request.user.get("id")
    if not user_id:
        return None
    return await User.objects.filter(id=user_id).first()


async def _verify_api_key(request) -> User | None:
    """Verify API key from Authorization header, return user or None.

    Uses SignedAPIKeyMixin.verify() — HMAC check rejects forgeries instantly
    without touching the database, then SHA-256 hash lookup confirms the key.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    raw_key = auth_header[7:]
    api_key = await APIKey.verify(raw_key)
    if not api_key:
        return None
    # Update last_used timestamp
    await APIKey.objects.filter(id=api_key.id).update(last_used=str(int(time.time())))
    return await User.objects.filter(id=api_key.user_id).first()


_tier_limiter_initialized = False
_tier_limiter_lock = asyncio.Lock()


async def _get_tier_limiter() -> InMemoryRateLimitBackend | DatabaseRateLimitBackend:
    """Return the rate limiter, initializing the DB backend lazily if configured.

    Thread-safe via asyncio.Lock. Falls back to in-memory if DB init fails.
    """
    global _tier_limiter, _tier_limiter_initialized
    if _tier_limiter_initialized:
        return _tier_limiter
    async with _tier_limiter_lock:
        if _tier_limiter_initialized:
            return _tier_limiter
        if _RATE_BACKEND == "database":
            try:
                db_limiter = DatabaseRateLimitBackend(db=get_db())
                await db_limiter.ensure_table()
                _tier_limiter = db_limiter
            except Exception as exc:
                logger.error(
                    "Failed to init database rate limiter, using in-memory: {err}",
                    err=str(exc),
                )
        _tier_limiter_initialized = True
    return _tier_limiter


async def _check_tier_limit(user: User) -> None:
    """Enforce per-minute rate limit based on user tier.

    Supports both InMemoryRateLimitBackend (single-server, sync) and
    DatabaseRateLimitBackend (multi-server, async via PostgreSQL UNLOGGED).
    Set RATE_LIMIT_BACKEND=database for multi-server coordination.
    """
    limiter = await _get_tier_limiter()
    limit = TIER_LIMITS.get(user.tier, 20)
    key = f"tier:{user.id}"
    if limiter._is_async:
        allowed, remaining, reset = await limiter.check_and_increment(
            key, limit, window=60
        )
    else:
        allowed, remaining, reset = limiter.check_and_increment(key, limit, window=60)
    if not allowed:
        raise HTTPException(
            429,
            f"Rate limit exceeded for {user.tier.value} tier ({limit} requests/minute)",
            headers={
                "Retry-After": str(reset),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset),
            },
        )


async def _log_usage(
    user: User,
    conversation: Conversation,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Record usage and update user counter."""
    cost = (input_tokens + output_tokens) * 1  # 1 hundredth-cent per token
    log = UsageLog(
        user_id=user.id,
        conversation_id=conversation.id,
        model_name=conversation.model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_cents=cost,
    )
    await log.save()
    # Atomic counter increment via F() — the previous read-modify-write
    # pattern (`usage_count=user.usage_count + 1`) was vulnerable to
    # lost updates when concurrent messages from the same user landed
    # on different worker threads.
    await User.objects.filter(id=user.id).update(usage_count=F("usage_count") + 1)


# ---------------------------------------------------------------------------
# Exception handlers — consistent JSON error format for API endpoints
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _handle_generic(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    # Platform convention: `{"detail": "..."}` only — HTTP status code
    # is canonical on the wire.
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/login")
async def login_page(request):
    return app.render("login.html", _ctx(request, error=None))


@app.post("/login")
async def login_submit(request):
    data = await request.form()
    username = (data.get("username") or [""])[0]
    password = (data.get("password") or [""])[0]

    if not username or not password:
        return app.render(
            "login.html", _ctx(request, error="Username and password required")
        )

    # Brute force protection
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        return app.render(
            "login.html",
            _ctx(request, error="Too many login attempts — please wait a few minutes"),
        )

    user = await User.objects.filter(username=username).first()
    if not user or not verify_password(password, user.password_hash):
        auth.record_failed_login(client_ip)
        return app.render(
            "login.html", _ctx(request, error="Invalid username or password")
        )

    auth.clear_login_attempts(client_ip)
    resp = Response.redirect("/")
    session = await build_session_data(user.id, get_db(), username=user.username)
    auth.login(resp, session, request)
    return resp


@app.get("/register")
async def register_page(request):
    return app.render("register.html", _ctx(request, error=None))


@app.post("/register")
async def register_submit(request):
    data = await request.form()
    username = (data.get("username") or [""])[0]
    email = (data.get("email") or [""])[0]
    password = (data.get("password") or [""])[0]

    if not username or not email or not password:
        return app.render(
            "register.html", _ctx(request, error="All fields are required")
        )

    if not re.match(r"^[a-zA-Z0-9_-]{1,50}$", username):
        return app.render(
            "register.html",
            _ctx(
                request,
                error="Username: 1-50 chars, letters/numbers/hyphens/underscores only",
            ),
        )

    if "@" not in email:
        return app.render("register.html", _ctx(request, error="Invalid email address"))

    if len(password) < 8:
        return app.render(
            "register.html",
            _ctx(request, error="Password must be at least 8 characters"),
        )

    existing = await User.objects.filter(username=username).first()
    if existing:
        return app.render(
            "register.html",
            _ctx(
                request,
                error="Registration failed — please try a different username",
            ),
        )

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
    )
    await user.save()

    resp = Response.redirect("/")
    session = await build_session_data(user.id, get_db(), username=user.username)
    auth.login(resp, session, request)
    return resp


@app.post("/logout")
@guard(Require.authenticated(redirect_url="/login"))
async def logout(request):
    resp = Response.redirect("/login")
    await auth.logout_async(resp, request.session_id)
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/")
@guard(Require.authenticated(redirect_url="/login"))
async def dashboard(request):
    user = await _get_user_from_session(request)
    if not user:
        return Response.redirect("/login")

    paginator = CursorPagination()
    paginator.page_size = 20
    paginator.ordering = "-id"
    conversations = await paginator.paginate_queryset(
        Conversation.objects.filter(user_id=user.id), request
    )

    total_messages = (
        await Message.objects.filter(
            conversation_id__in=[c.id for c in conversations]
        ).count()
        if conversations
        else 0
    )

    # Aggregate total tokens in SQL instead of loading all rows into memory
    db = get_db()
    token_row = await db.query_one(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total "
        "FROM ai_usage_logs WHERE user_id = $1",
        user.id,
    )
    total_tokens = token_row["total"] if token_row else 0

    # Add external IDs for template links
    for conv in conversations:
        conv.cid = conv.get_external_id()

    return app.render(
        "dashboard.html",
        _ctx(
            request,
            user=user,
            conversations=conversations,
            total_messages=total_messages,
            total_tokens=total_tokens,
            total_conversations=len(conversations),
        ),
    )


# ---------------------------------------------------------------------------
# Chat routes
# ---------------------------------------------------------------------------


@app.get("/chat/new")
@guard(Require.authenticated(redirect_url="/login"))
async def new_chat(request):
    user = await _get_user_from_session(request)
    if not user:
        return Response.redirect("/login")

    conversation = Conversation(user_id=user.id)
    await conversation.save()
    return Response.redirect(f"/chat/{conversation.get_external_id()}")


@app.get("/chat/{cid}")
@guard(Require.authenticated(redirect_url="/login"))
async def chat_view(request, cid: str):
    user = await _get_user_from_session(request)
    if not user:
        return Response.redirect("/login")

    try:
        conv_id = Conversation.decode_external_id(cid)
    except ValueError:
        raise HTTPException(404, "Conversation not found")

    conversation = await Conversation.objects.filter(id=conv_id).first()
    if not conversation or conversation.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    messages = (
        await Message.objects.filter(conversation_id=conv_id)
        .order_by("created_at")
        .all()
    )

    conversations = (
        await Conversation.objects.filter(user_id=user.id)
        .order_by("-updated_at")
        .limit(20)
        .all()
    )

    # Add external ID to conversation for template use
    conversation.cid = conversation.get_external_id()
    # Add external IDs to sidebar conversations
    for conv in conversations:
        conv.cid = conv.get_external_id()

    return app.render(
        "chat.html",
        _ctx(
            request,
            user=user,
            conversation=conversation,
            messages=messages,
            conversations=conversations,
        ),
    )


@app.post("/chat/{cid}/send")
@guard(Require.authenticated(redirect_url="/login"))
async def send_message(request, cid: str):
    """Send a message and stream the AI response via SSE."""
    user = await _get_user_from_session(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    try:
        conv_id = Conversation.decode_external_id(cid)
    except ValueError:
        raise HTTPException(404, "Conversation not found")

    conversation = await Conversation.objects.filter(id=conv_id).first()
    if not conversation or conversation.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    await _check_tier_limit(user)

    data = await request.form()
    content = (data.get("content") or [""])[0].strip()
    if not content:
        raise HTTPException(400, "Message content is required")
    if len(content) > 32_000:
        raise HTTPException(400, "Message must be 32,000 characters or less")

    # Save user message
    input_tokens = _count_tokens(content)
    user_msg = Message(
        conversation_id=conv_id,
        role="user",
        content=content,
        token_count=input_tokens,
    )
    await user_msg.save()

    # Update conversation title from first message
    existing_messages = await Message.objects.filter(conversation_id=conv_id).count()
    if existing_messages <= 1:
        title = content[:80] + ("..." if len(content) > 80 else "")
        await Conversation.objects.filter(id=conv_id).update(title=title)

    # Generate simulated AI response
    ai_response = _pick_response()
    output_tokens = _count_tokens(ai_response)

    async def generate():
        """Stream AI response token by token."""
        words = ai_response.split(" ")
        accumulated = []
        for i, word in enumerate(words):
            token = word + (" " if i < len(words) - 1 else "")
            accumulated.append(token)
            yield {"data": json.dumps({"token": token})}
            await asyncio.sleep(0.03)

        # Save the complete assistant message
        full_response = "".join(accumulated)
        assistant_msg = Message(
            conversation_id=conv_id,
            role="assistant",
            content=full_response,
            token_count=output_tokens,
        )
        await assistant_msg.save()

        # Log usage
        await _log_usage(user, conversation, input_tokens, output_tokens)

        yield {"data": json.dumps({"done": True, "token_count": output_tokens})}

    return Response.sse(generate())


@app.post("/chat/{cid}/delete")
@guard(Require.authenticated(redirect_url="/login"))
async def delete_conversation(request, cid: str):
    user = await _get_user_from_session(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    try:
        conv_id = Conversation.decode_external_id(cid)
    except ValueError:
        raise HTTPException(404, "Conversation not found")

    conversation = await Conversation.objects.filter(id=conv_id).first()
    if not conversation or conversation.user_id != user.id:
        raise HTTPException(404, "Conversation not found")

    await Message.objects.filter(conversation_id=conv_id).delete()
    await UsageLog.objects.filter(conversation_id=conv_id).delete()
    await Conversation.objects.filter(id=conv_id).delete()

    return Response.redirect("/")


# ---------------------------------------------------------------------------
# Account & API keys
# ---------------------------------------------------------------------------


@app.get("/account")
@guard(Require.authenticated(redirect_url="/login"))
async def account_page(request):
    user = await _get_user_from_session(request)
    if not user:
        return Response.redirect("/login")

    api_keys = (
        await APIKey.objects.filter(user_id=user.id, is_active=True)
        .order_by("-created_at")
        .all()
    )

    usage_logs = (
        await UsageLog.objects.filter(user_id=user.id)
        .order_by("-created_at")
        .limit(20)
        .all()
    )

    # Add external IDs for revoke links
    for key in api_keys:
        key.kid = key.get_external_id()

    return app.render(
        "account.html",
        _ctx(
            request,
            user=user,
            api_keys=api_keys,
            usage_logs=usage_logs,
            tier_limits=TIER_LIMITS,
            tier_token_limits=TIER_TOKEN_LIMITS,
        ),
    )


@app.post("/api-keys/create")
@guard(Require.authenticated(redirect_url="/login"))
async def create_api_key(request):
    user = await _get_user_from_session(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    data = await request.form()
    name = (data.get("name") or [""])[0].strip()
    if not name:
        raise HTTPException(400, "API key name is required")

    # Generate a signed API key via SignedAPIKeyMixin
    result = await APIKey.generate(user_id=user.id, name=name)
    raw_key = result.raw_key

    # Return the raw key — this is the only time it's shown
    api_keys = await APIKey.objects.filter(user_id=user.id, is_active=True).all()
    for key in api_keys:
        key.kid = key.get_external_id()
    usage_logs = (
        await UsageLog.objects.filter(user_id=user.id)
        .order_by("-created_at")
        .limit(20)
        .all()
    )
    return app.render(
        "account.html",
        _ctx(
            request,
            user=user,
            api_keys=api_keys,
            usage_logs=usage_logs,
            tier_limits=TIER_LIMITS,
            tier_token_limits=TIER_TOKEN_LIMITS,
            new_key=raw_key,
        ),
    )


@app.post("/api-keys/{kid}/revoke")
@guard(Require.authenticated(redirect_url="/login"))
async def revoke_api_key(request, kid: str):
    user = await _get_user_from_session(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    try:
        key_id = APIKey.decode_external_id(kid)
    except ValueError:
        raise HTTPException(404, "API key not found")

    api_key = await APIKey.objects.filter(id=key_id).first()
    if not api_key or api_key.user_id != user.id:
        raise HTTPException(404, "API key not found")

    await APIKey.objects.filter(id=key_id).update(is_active=False)
    return Response.redirect("/account")


# ---------------------------------------------------------------------------
# OpenAI-compatible REST API
# ---------------------------------------------------------------------------


@app.post("/api/v1/chat/completions")
async def api_completions(request):
    """OpenAI-compatible chat completions endpoint with SSE streaming.

    Authenticate via Bearer token in the Authorization header.

    Request body:
        {
            "model": "hyper-4",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": true
        }

    Streams SSE events matching OpenAI's format:
        data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"token"}}]}
        ...
        data: [DONE]
    """
    # Authenticate via API key
    api_user = await _verify_api_key(request)
    if not api_user:
        raise HTTPException(401, "Invalid or missing API key")

    await _check_tier_limit(api_user)

    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "hyper-4")
    stream = body.get("stream", True)

    if not messages or not isinstance(messages, list):
        raise HTTPException(400, "messages array is required")

    # Extract the last user message for token counting
    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_msg = msg.get("content", "")
            break

    input_tokens = _count_tokens(last_user_msg)

    # Create or use a conversation for API usage tracking
    conversation = Conversation(
        user_id=api_user.id,
        title=last_user_msg[:80] if last_user_msg else "API Request",
        model_name=model,
    )
    await conversation.save()

    # Save incoming messages
    for msg in messages:
        m = Message(
            conversation_id=conversation.id,
            role=msg.get("role", "user"),
            content=msg.get("content", ""),
            token_count=_count_tokens(msg.get("content", "")),
        )
        await m.save()

    ai_response = _pick_response()
    output_tokens = _count_tokens(ai_response)
    completion_id = f"chatcmpl-{secrets.token_hex(12)}"

    if not stream:
        # Non-streaming response
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=ai_response,
            token_count=output_tokens,
        )
        await assistant_msg.save()
        await _log_usage(api_user, conversation, input_tokens, output_tokens)

        return Response.json(
            {
                "id": completion_id,
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ai_response},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }
        )

    # Streaming response
    async def generate():
        words = ai_response.split(" ")
        accumulated = []
        for i, word in enumerate(words):
            token = word + (" " if i < len(words) - 1 else "")
            accumulated.append(token)
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": token},
                        "finish_reason": None,
                    }
                ],
            }
            yield {"data": json.dumps(chunk)}
            await asyncio.sleep(0.03)

        # Final chunk with finish_reason
        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }
        yield {"data": json.dumps(final_chunk)}

        # Save assistant message and log usage
        full_response = "".join(accumulated)
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=full_response,
            token_count=output_tokens,
        )
        await assistant_msg.save()
        await _log_usage(api_user, conversation, input_tokens, output_tokens)

        yield {"data": "[DONE]"}

    return Response.sse(generate())


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


app.mount_health()
mount_docs(app)


@app.get("/robots.txt")
async def robots_txt(request):
    body = "User-agent: *\nAllow: /\nDisallow: /account\nDisallow: /chat/\nDisallow: /api/\n"
    return Response(body=body.encode(), content_type="text/plain")


@app.get("/.well-known/security.txt")
async def security_txt(request):
    body = "Contact: security@example.com\nPreferred-Languages: en\n"
    return Response(body=body.encode(), content_type="text/plain")


# ---------------------------------------------------------------------------
# HyperAdmin model registration
# ---------------------------------------------------------------------------

admin.register(
    User,
    list_display=["id", "username", "email", "tier", "usage_count", "created_at"],
    search_fields=["username", "email"],
    list_filter=["tier"],
)

admin.register(
    Conversation,
    list_display=["id", "user_id", "title", "model_name", "created_at"],
    search_fields=["title"],
    ordering="-created_at",
)

admin.register(
    Message,
    list_display=["id", "conversation_id", "role", "token_count", "created_at"],
    list_filter=["role"],
    ordering="-created_at",
)

admin.register(
    APIKey,
    list_display=["id", "user_id", "name", "key_prefix", "is_active", "created_at"],
    search_fields=["name", "key_prefix"],
    list_filter=["is_active"],
)

admin.register(
    UsageLog,
    list_display=[
        "id",
        "user_id",
        "model_name",
        "input_tokens",
        "output_tokens",
        "cost_cents",
    ],
    list_filter=["model_name"],
    ordering="-created_at",
)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _port = int(_sys.argv[1]) if len(_sys.argv) > 1 else get_setting("PORT", 8000)
    app.run(port=_port)
