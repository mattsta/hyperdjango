"""
Production Deployment Example — Reference Application.

A minimal production-ready application demonstrating:
  - Production configuration via environment variables
  - Health and readiness probes for load balancers
  - Graceful shutdown with connection draining
  - Structured logging
  - Security middleware stack
  - Database connection management

This is the app that the deployment templates (systemd, nginx, logrotate)
are configured to run. Deploy with:

    # 1. Build release
    uv run hyper-build --install --release

    # 2. Run diagnostics
    uv run hyper doctor

    # 3. Create tables
    uv run hyper setup --app services.deployment.app:app --seed services.deployment.seed:run

    # 4. Install systemd service
    uv run hyper systemd install --app services.deployment.app:app --enable

    # 5. Verify
    curl http://localhost:8000/health
    curl http://localhost:8000/_ready
"""

from enum import Enum
from pathlib import Path

from hyperdjango import HyperApp, Response
from hyperdjango.auth import verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.rest import CursorPagination
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)

_APP_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Configuration — all from environment, no hardcoded secrets
# ---------------------------------------------------------------------------

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

DATABASE_URL = get_setting("DATABASE_URL")
SECRET_KEY = get_setting("SECRET_KEY")
DEBUG = get_setting("DEBUG")
ALLOWED_ORIGINS = get_setting("CORS_ORIGINS", ["*"])

app = HyperApp(
    title="Production App",
    database=DATABASE_URL,
    debug=DEBUG,
)

# ---------------------------------------------------------------------------
# Middleware — production-grade stack
# ---------------------------------------------------------------------------

app.use(TimingMiddleware())
app.use(
    SecurityHeadersMiddleware(
        hsts=not DEBUG,  # Enable HSTS in production (behind TLS terminator)
        content_type_nosniff=True,
        frame_options="DENY",
    )
)
if ALLOWED_ORIGINS:
    app.use(
        CORSMiddleware(
            origins=ALLOWED_ORIGINS,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            headers=["Content-Type", "Authorization"],
        )
    )
app.use(RateLimitMiddleware(max_requests=60, window=60))

_session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=get_setting("SESSION_SIGNING_KEY"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=SECRET_KEY,
    token_engine=_session_engine,
)
app.use(auth)


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    logger.exception("Unhandled error: {exc}", exc=exc)
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ItemStatus(Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Item(TimestampMixin, Model):
    class Meta:
        table = "deploy_items"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    status: ItemStatus = Field(default=ItemStatus.ACTIVE)


class User(TimestampMixin, Model):
    class Meta:
        table = "deploy_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    password_hash: str = Field(exclude=True)


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    count = await Item.objects.count()
    logger.info(
        "Production app ready: {count} items, debug={debug}", count=count, debug=DEBUG
    )


# ---------------------------------------------------------------------------
# Health probes — used by load balancers and systemd
# ---------------------------------------------------------------------------


app.mount_health()


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.redirect("/health")


# ---------------------------------------------------------------------------
# Routes — minimal CRUD to verify deployment
# ---------------------------------------------------------------------------


@app.get("/api/items/")
async def list_items(request):
    """List items with cursor pagination."""
    paginator = CursorPagination()
    paginator.page_size = 20
    paginator.ordering = "-id"
    items = await paginator.paginate_queryset(Item.objects, request)
    # Use Model.to_dict() to keep serialization in one place. The
    # platform handles datetime → string conversion and respects
    # Field(exclude=True) metadata, so adding a sensitive field to
    # the model won't silently leak through a reference deployment.
    data = [i.to_dict(include={"id", "name", "status", "created_at"}) for i in items]
    return paginator.get_paginated_response(data)


def get_uid_or_none(request) -> int | None:
    """Extract user ID, returning None for anonymous users."""
    return request.user.id if request.user is not None else None


@app.post("/api/items/")
async def create_item(request):
    """Create an item."""
    if get_uid_or_none(request) is None:
        return Response.json({"error": "Authentication required"}, status=401)

    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return Response.json({"error": "name required"}, status=400)

    item = Item(name=name, status=ItemStatus.ACTIVE)
    await item.save()
    return Response.json({"id": item.id, "name": item.name}, status=201)


@app.post("/auth/login")
async def login(request):
    data = await request.json()
    username = data.get("username", "")
    password = data.get("password", "")
    if not username or not password:
        return Response.json({"error": "username and password required"}, status=400)

    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        return Response.json({"error": "Too many login attempts"}, status=429)

    user = await User.objects.filter(username=username).first()
    if user is None or not verify_password(password, user.password_hash):
        auth.record_failed_login(client_ip)
        return Response.json({"error": "Invalid credentials"}, status=401)

    auth.clear_login_attempts(client_ip)
    resp = Response.json({"message": "Logged in", "username": user.username})
    session = await build_session_data(user.id, get_db(), username=user.username)
    auth.login(resp, session, request)
    return resp


if __name__ == "__main__":
    # Production deployments launch via systemd / supervisord / uv run
    # hyper start, NOT direct `python app.py`. HOST and PORT are read
    # from environment variables so the same binary can be redeployed
    # across staging / production without code changes. Defaults are
    # safe enough for a local `python app.py` smoke test.
    app.run(
        host=get_setting("HOST", "0.0.0.0"),
        port=get_setting("PORT", 8000),
    )
