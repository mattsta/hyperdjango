"""
Notes API — Intermediate HyperDjango Example.

A step between hello (34 lines) and rest_api (278 lines). Demonstrates the
core patterns every HyperDjango app needs in ~170 lines:

  - 3 models with FK relationships (User → Category → Note)
  - Session auth (register/login/logout) with signed cookies
  - JSON REST endpoints with CursorPagination
  - F expression atomic updates (Category.note_count)
  - to_dict() serialization with Field(exclude=True)
  - HyperAdmin auto-CRUD panel
  - Health endpoint

Run:
    uv run hyper setup --app services.notes_api.app:app --seed services.notes_api.seed:run
    uv run hyper run --app services.notes_api.app:app --port 8700

API:
    POST /auth/register          → Register (username + password)
    POST /auth/login             → Login (returns session cookie)
    POST /auth/logout            → Logout
    GET  /api/notes/             → List notes (cursor-paginated)
    POST /api/notes/             → Create note (auth required)
    GET  /api/notes/{id}         → Get single note
    DELETE /api/notes/{id}       → Delete note (owner only)
    GET  /api/categories/        → List categories with note counts
    GET  /health                 → Health check
    GET  /admin/                 → HyperAdmin panel
"""

import sys

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.expressions import F
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.postgres import SearchQuery, SearchRank, SearchVector
from hyperdjango.rest import CursorPagination
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import TimingMiddleware

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

DATABASE_URL = get_setting("DATABASE_URL")

app = HyperApp(title="Notes API", database=DATABASE_URL, debug=True)
app.use(TimingMiddleware())


# ---------------------------------------------------------------------------
# Exception handler — platform convention: log full exception, return generic
# JSON 500 without leaking internal details. `HTTPException` (raised by
# handlers for expected failure modes like 401/403/404) flows through the
# framework's built-in handler and is NOT caught here.
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _handle_generic(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    return Response.json({"detail": "Internal server error"}, status=500)


_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=get_setting("SESSION_SIGNING_KEY"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=get_setting("SESSION_SECRET"),
    token_engine=_engine,
)
app.use(auth)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(TimestampMixin, Model):
    class Meta:
        table = "notes_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    password_hash: str = Field(exclude=True)


class Category(TimestampMixin, Model):
    class Meta:
        table = "notes_categories"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
    note_count: int = Field(default=0)


class Note(TimestampMixin, Model):
    class Meta:
        table = "notes_notes"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field(default="")
    author_id: int = Field(foreign_key=User)
    category_id: int = Field(foreign_key=Category)


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.redirect("/admin/")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.post("/auth/register")
async def register(request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or len(password) < 8:
        raise HTTPException(400, "Username required, password min 8 chars")

    existing = await User.objects.filter(username=username).exists()
    if existing:
        raise HTTPException(409, "Username taken")

    user = User(username=username, password_hash=hash_password(password))
    await user.save()

    session = await build_session_data(user.id, get_db(), username=username)
    resp = Response.json(
        {"message": "Registered", "id": user.id, "username": username}, status=201
    )
    auth.login(resp, session)
    return resp


@app.post("/auth/login")
async def login(request):
    data = await request.json()
    username = data.get("username", "")
    password = data.get("password", "")
    if not username or not password:
        raise HTTPException(400, "Username and password required")

    user = await User.objects.filter(username=username).first()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")

    session = await build_session_data(user.id, get_db(), username=username)
    resp = Response.json({"message": "Logged in", "username": username})
    auth.login(resp, session)
    return resp


@app.post("/auth/logout")
async def logout(request):
    resp = Response.json({"message": "Logged out"})
    if request.session_id:
        auth.logout(resp, request.session_id)
    return resp


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


def _require_auth(request):
    """Extract authenticated user or raise 401.

    Returns the SessionUser (or User) which supports dict-like access
    (``user["id"]``, ``user["username"]``) and property access
    (``.id``, ``.username``, ``.is_authenticated``).
    """
    user = request.user
    if user is None or not user.is_authenticated:
        raise HTTPException(401, "Authentication required")
    return user


class NotePagination(CursorPagination):
    page_size = 20
    ordering = "-id"


@app.get("/api/notes/")
async def list_notes(request):
    """List notes with cursor pagination."""
    paginator = NotePagination()
    notes = await paginator.paginate_queryset(Note.objects, request)
    data = [n.to_dict() for n in notes]
    return paginator.get_paginated_response(data)


@app.get("/api/notes/search")
async def search_notes(request):
    """Full-text search notes by title + body using FTS Expression classes.

    Demonstrates: SearchVector, SearchQuery, SearchRank in annotate() + order_by().

    Usage: GET /api/notes/search?q=python
    """
    q = request.query("q", "").strip()
    if not q or len(q) < 2:
        raise HTTPException(400, "Search query must be at least 2 characters")

    vector = SearchVector(["title", "body"], config="english")
    query = SearchQuery(q, search_type="websearch")
    rank = SearchRank(vector, query)

    results = await Note.objects.annotate(rank=rank).order_by("-rank").limit(20).all()
    return Response.json([n.to_dict() for n in results])


@app.post("/api/notes/")
async def create_note(request):
    """Create a note. Atomically increments category.note_count via F()."""
    user = _require_auth(request)
    data = await request.json()
    title = data.get("title", "").strip()
    category_id = data.get("category_id")
    if not title:
        raise HTTPException(400, "Title is required")
    if not category_id:
        raise HTTPException(400, "category_id is required")

    cat = await Category.objects.filter(id=category_id).first()
    if cat is None:
        raise HTTPException(404, "Category not found")

    note = Note(
        title=title,
        body=data.get("body", ""),
        author_id=user["id"],
        category_id=category_id,
    )
    await note.save()

    # Atomic increment — no race conditions
    await Category.objects.filter(id=category_id).update(note_count=F("note_count") + 1)

    return Response.json(note.to_dict(), status=201)


@app.get("/api/notes/{note_id:int}")
async def get_note(request, note_id: int):
    """Get a single note by ID."""
    note = await Note.objects.filter(id=note_id).first()
    if note is None:
        raise HTTPException(404, "Note not found")
    return Response.json(note.to_dict())


@app.delete("/api/notes/{note_id:int}")
async def delete_note(request, note_id: int):
    """Delete a note. Owner only. Decrements category.note_count."""
    user = _require_auth(request)
    note = await Note.objects.filter(id=note_id, author_id=user["id"]).first()
    if note is None:
        raise HTTPException(404, "Note not found or not yours")

    cat_id = note.category_id
    await Note.objects.filter(id=note_id).delete()
    await Category.objects.filter(id=cat_id).update(note_count=F("note_count") - 1)

    return Response.json({"deleted": True})


@app.get("/api/categories/")
async def list_categories(request):
    """List all categories with note counts."""
    cats = await Category.objects.order_by("name").all()
    return Response.json([c.to_dict() for c in cats])


# ---------------------------------------------------------------------------
# Admin + Health
# ---------------------------------------------------------------------------

admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Notes Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)
admin.register(
    User, list_display=["id", "username", "created_at"], search_fields=["username"]
)
admin.register(
    Category, list_display=["id", "name", "note_count"], search_fields=["name"]
)
admin.register(
    Note,
    list_display=["id", "title", "author_id", "category_id", "created_at"],
    search_fields=["title"],
    ordering="-created_at",
)

app.mount_health()
mount_docs(app)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8700
    app.run(host="127.0.0.1", port=port)
