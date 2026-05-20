"""
Example: REST API with HyperDjango.

Demonstrates:
- HyperApp with native Zig HTTP server
- Model definitions with validation
- CRUD endpoints with HMAC-signed opaque IDs (unforgeable + enumeration-resistant; authorization still required — not IDOR protection alone)
- Session auth (login/logout/protected routes)
- API key auth on admin endpoints
- CORS middleware
- OpenAPI docs at /docs
- Database configuration (pg.zig native)
- Error handling with HTTPException
"""

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.api_keys import APIKeyAuth
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import get_setting
from hyperdjango.database import get_db
from hyperdjango.guard import Require, guard
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.public_id import IDMixin, IDMode, KeySlot
from hyperdjango.rest import CursorPagination
from hyperdjango.security import SecurityEvent, SecurityLog, set_security_log
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import CORSMiddleware, TimingMiddleware
from hyperdjango.validation.core import EmailStr

# --- App setup ---

app = HyperApp(
    title="Blog API",
    # Honor an externally-provided DATABASE_URL (the e2e test runner points
    # each app at an isolated per-run database this way) and fall back to the
    # local dev database only when nothing is configured. Passing a bare
    # literal here would write DEFAULTS["DATABASE_URL"], which outranks the
    # env the runner sets — so the app would ignore its isolated database and
    # every environment without a local "blog" db (CI) would fail to connect.
    database=get_setting("DATABASE_URL") or "postgres://localhost/blog",
    debug=True,
)

# Middleware — order matters: outermost first
app.use(TimingMiddleware())
app.use(
    CORSMiddleware(
        origins=["http://localhost:3000", "https://myblog.example.com"],
        methods=["GET", "POST", "PUT", "DELETE"],
        headers=["Content-Type", "Authorization", "X-API-Key"],
    )
)


@app.get("/")
async def root(request):
    return Response.redirect("/docs/")


# Auth
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
app.use(APIKeyAuth(valid_keys={"sk_live_demo_key_123"}))

# OpenAPI docs at /docs
mount_docs(app)


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    return Response.json({"detail": "Internal server error"}, status=500)


# --- Models ---


class User(TimestampMixin, Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    email: EmailStr = Field(unique=True)
    password_hash: str = Field(exclude=True)
    is_active: bool = Field(default=True)


class Post(IDMixin, TimestampMixin, Model):
    class Meta:
        table = "posts"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "jFwQGrxX8V2Pv6mHgM359cpqf4RJWCh7"
        hmac_keys = [KeySlot(key="blog-posts-key-2026-q1", offset=3000)]

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field()
    author_id: int = Field(foreign_key=User)
    published: bool = Field(default=False)


# --- Auth endpoints ---


@app.post("/auth/register")
async def register(request):
    """Register a new user."""
    data = await request.json()

    if not data.get("username") or not data.get("email") or not data.get("password"):
        raise HTTPException(400, "username, email, and password are required")

    password_hash = hash_password(data["password"])
    user = User(
        username=data["username"],
        email=data["email"],
        password_hash=password_hash,
    )
    await user.save()
    return Response.json({"username": user.username}, status=201)


@app.post("/auth/login")
async def login(request):
    """Login and create a session."""
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        raise HTTPException(429, "Too many login attempts — please wait a few minutes")

    data = await request.json()
    user = await User.objects.filter(username=data.get("username")).first()
    if user is None or not verify_password(
        data.get("password", ""), user.password_hash
    ):
        auth.record_failed_login(client_ip)
        if _security_log:
            await _security_log.log_from_request(
                SecurityEvent.LOGIN_FAILED,
                request,
                detail=f"user={data.get('username', '?')}",
            )
        raise HTTPException(401, "Invalid credentials")

    auth.clear_login_attempts(client_ip)
    if _security_log:
        await _security_log.log_from_request(
            SecurityEvent.LOGIN_SUCCESS,
            request,
            user_id=user.id,
        )
    resp = Response.json({"message": "Logged in", "username": user.username})
    session = await build_session_data(user.id, get_db(), username=user.username)
    auth.login(resp, session, request)
    return resp


@app.post("/auth/logout")
@guard(Require.authenticated())
async def logout(request):
    """Logout and destroy session."""
    resp = Response.json({"message": "Logged out"})
    auth.logout(resp, request.session_id)
    return resp


# --- Post CRUD endpoints ---


def _post_to_json(post: Post) -> dict[str, str | int | bool]:
    """Serialize post with opaque external ID instead of integer PK."""
    return {
        "id": post.get_external_id(),
        "title": post.title,
        "body": post.body,
        "author_id": post.author_id,
        "published": post.published,
    }


@app.get("/api/posts")
async def list_posts(request):
    """List posts with cursor pagination."""
    paginator = CursorPagination()
    paginator.page_size = 20
    paginator.ordering = "-id"
    items = await paginator.paginate_queryset(Post.objects, request)
    data = [_post_to_json(p) for p in items]
    return paginator.get_paginated_response(data)


@app.get("/api/posts/{pid}")
async def get_post(request, pid: str):
    """Get a single post by opaque ID."""
    try:
        post_id = Post.decode_external_id(pid)
    except ValueError:
        raise HTTPException(404, "Post not found")
    post = await Post.objects.filter(id=post_id).first()
    if post is None:
        raise HTTPException(404, "Post not found")
    return _post_to_json(post)


@app.post("/api/posts")
@guard(Require.authenticated())
async def create_post(request):
    """Create a new post. Requires authentication."""
    data = await request.json()
    if not data.get("title") or not data.get("body"):
        raise HTTPException(400, "title and body are required")

    post = Post(
        title=data["title"],
        body=data["body"],
        author_id=request.user["id"],
    )
    await post.save()
    return Response.json(_post_to_json(post), status=201)


@app.put("/api/posts/{pid}")
@guard(Require.authenticated())
async def update_post(request, pid: str):
    """Update a post. Requires authentication and ownership."""
    try:
        post_id = Post.decode_external_id(pid)
    except ValueError:
        raise HTTPException(404, "Post not found")
    data = await request.json()
    # Whitelist updatable fields — never allow author_id or id override
    safe = {k: v for k, v in data.items() if k in ("title", "body")}
    if not safe:
        raise HTTPException(400, "No valid fields to update")
    count = await Post.objects.filter(id=post_id, author_id=request.user["id"]).update(
        **safe
    )
    if count == 0:
        raise HTTPException(404, "Post not found or not authorized")
    return Response.json({"updated": True})


@app.delete("/api/posts/{pid}")
@guard(Require.authenticated())
async def delete_post(request, pid: str):
    """Delete a post. Requires authentication and ownership."""
    try:
        post_id = Post.decode_external_id(pid)
    except ValueError:
        raise HTTPException(404, "Post not found")
    count = await Post.objects.filter(id=post_id, author_id=request.user["id"]).delete()
    if count == 0:
        raise HTTPException(404, "Post not found or not authorized")
    return Response.empty()


# --- Admin endpoints (API key protected) ---


@app.get("/api/admin/stats")
@guard(Require.api_key())
async def admin_stats(request):
    """Admin stats endpoint. Requires API key via X-API-Key header."""
    user_count = await User.objects.count()
    post_count = await Post.objects.count()
    return {
        "user_count": user_count,
        "post_count": post_count,
    }


@app.get("/api/admin/users")
@guard(Require.api_key())
async def admin_list_users(request):
    """List all users. Requires API key. Cursor-paginated."""
    paginator = CursorPagination()
    paginator.page_size = 50
    paginator.ordering = "id"
    items = await paginator.paginate_queryset(User.objects, request)
    data = [
        {"username": u.username, "email": u.email, "is_active": u.is_active}
        for u in items
    ]
    return paginator.get_paginated_response(data)


# --- Health check ---


# --- Security Audit Log ---

_security_log: SecurityLog | None = None


@app.on_startup
async def _init_security_log():
    global _security_log
    db = get_db()
    _security_log = SecurityLog(db)
    await _security_log.ensure_table()
    set_security_log(_security_log)


@app.get("/api/security/recent")
async def security_recent(request):
    """Recent security events (last 50)."""
    if _security_log is None:
        raise HTTPException(503, "Security log not initialized")
    events = await _security_log.get_recent(limit=50)
    return Response.json({"events": events, "count": len(events)})


@app.get("/api/security/failed-logins")
async def security_failed_logins(request):
    """Failed login attempts in the last hour."""
    if _security_log is None:
        raise HTTPException(503, "Security log not initialized")
    events = await _security_log.get_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)
    count = await _security_log.count_by_event(
        SecurityEvent.LOGIN_FAILED, since_hours=1
    )
    return Response.json({"events": events, "count": count})


app.mount_health()


if __name__ == "__main__":
    app.run()
