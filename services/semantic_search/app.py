"""
Semantic Search — pgvector service.

Production-grade showcase of HyperDjango's native pgvector integration with
an OpenAI-compatible embeddings API:
  - VectorField with HNSW cosine index (auto-created by `hyper setup`)
  - Real dense embeddings from any OpenAI-compatible API (OpenAI, Ollama, vLLM, etc.)
  - Nearest-neighbor search via parameterized cosine distance
  - ORM QuerySet for structured filters (category, count)
  - Article CRUD with auto-embedding on submit
  - Similar articles via vector distance on detail page
  - Query timing display showing sub-millisecond HNSW performance
  - Full middleware stack, session auth, startup hook

Configuration (environment variables):
    EMBEDDINGS_API_URL   — Base URL for embeddings endpoint (default: https://api.openai.com/v1)
    EMBEDDINGS_API_KEY   — API key for the embeddings service (required)
    EMBEDDINGS_MODEL     — Model name (default: text-embedding-3-small)
    VECTOR_DIM           — Embedding dimensions (default: 1536, must match model output)
    DATABASE_URL         — PostgreSQL connection string

Setup:
    export EMBEDDINGS_API_KEY=sk-...
    uv run hyper setup --app services.semantic_search.app:app --seed services.semantic_search.seed:run
    uv run hyper run --app services.semantic_search.app:app --port 8200

Supported providers:
    OpenAI:     EMBEDDINGS_API_URL=https://api.openai.com/v1  EMBEDDINGS_MODEL=text-embedding-3-small
    Ollama:     EMBEDDINGS_API_URL=http://localhost:11434/v1   EMBEDDINGS_MODEL=nomic-embed-text
    vLLM:       EMBEDDINGS_API_URL=http://localhost:8000/v1    EMBEDDINGS_MODEL=BAAI/bge-small-en-v1.5
    Together:   EMBEDDINGS_API_URL=https://api.together.xyz/v1 EMBEDDINGS_MODEL=togethercomputer/m2-bert-80M-8k-retrieval
"""

import time
from enum import Enum
from pathlib import Path

import httpx

from hyperdjango import BaseModel as ValidatedModel
from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.admin.fields import Fieldset
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.db.pgzig_connection import DatabaseError, IntegrityError
from hyperdjango.guard import Require, guard
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model, VectorField
from hyperdjango.openapi import mount_docs
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.validation.core.fields import Field as VField
from hyperdjango.validation.core.validator import ValidationErrors

_APP_DIR = Path(__file__).resolve().parent

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

DATABASE_URL = get_setting("DATABASE_URL")

# ---------------------------------------------------------------------------
# Embeddings API configuration
# ---------------------------------------------------------------------------

EMBEDDINGS_API_URL = get_setting("EMBEDDINGS_API_URL", "https://api.openai.com/v1")
EMBEDDINGS_API_KEY = get_setting("EMBEDDINGS_API_KEY", "")
EMBEDDINGS_MODEL = get_setting("EMBEDDINGS_MODEL", "text-embedding-3-small")
VECTOR_DIM = get_setting("EMBEDDINGS_VECTOR_DIM", 1536)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = HyperApp(
    title="Semantic Search",
    database=DATABASE_URL,
    templates=str(_APP_DIR / "templates"),
    debug=get_setting("DEBUG"),
)

# Middleware (outermost first)
app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=False))
app.use(
    CSRFMiddleware(
        secret=get_setting("CSRF_SECRET"),
        exempt_paths={"/api/search", "/api/embed"},
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


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    logger.exception("Unhandled error: {exc}", exc=exc)
    return Response.json({"detail": "Internal server error"}, status=500)


# HyperAdmin — auto-CRUD panel
admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Semantic Search Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)

# Shared httpx client for embedding API calls
_http_client = httpx.AsyncClient(timeout=30.0)


# ---------------------------------------------------------------------------
# Startup hook — verify pgvector + embeddings config
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    db = get_db()
    has_vector = await db.query_val(
        "SELECT COUNT(*) FROM pg_extension WHERE extname = 'vector'"
    )
    if not has_vector:
        logger.warning(
            "pgvector extension not installed — run: CREATE EXTENSION vector"
        )
    else:
        try:
            count = await db.query_val("SELECT COUNT(*) FROM vs_articles")
            logger.info("Semantic search ready: {count} articles indexed", count=count)
        except DatabaseError:
            logger.warning("vs_articles table not found — run: hyper setup")

    if not EMBEDDINGS_API_KEY:
        logger.warning(
            "EMBEDDINGS_API_KEY not set — search and article creation will fail. "
            "Set it to use OpenAI, Ollama, or any compatible provider."
        )
    else:
        logger.info(
            "Embeddings: {model} via {url} ({dim}-dim)",
            model=EMBEDDINGS_MODEL,
            url=EMBEDDINGS_API_URL,
            dim=VECTOR_DIM,
        )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Category(Enum):
    GENERAL = "general"
    WEB = "web"
    DATABASE = "database"
    API = "api"
    ML = "ml"
    DEVOPS = "devops"
    PYTHON = "python"
    SECURITY = "security"


class User(TimestampMixin, Model):
    class Meta:
        table = "vs_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    password_hash: str = Field(exclude=True)


class Article(TimestampMixin, Model):
    class Meta:
        table = "vs_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field(default="")
    category: Category = Field(default=Category.GENERAL)
    author_id: int = Field(default=0)
    embedding: list[float] = VectorField(
        dimensions=VECTOR_DIM,
        index_type="hnsw",
        index_ops="vector_cosine_ops",
        index_params={"m": 16, "ef_construction": 64},
    )


# ---------------------------------------------------------------------------
# Validation schemas
# ---------------------------------------------------------------------------


class LoginSchema(ValidatedModel):
    username: str = VField(min_length=1, strip_whitespace=True)
    password: str = VField(min_length=1)


class RegisterSchema(ValidatedModel):
    username: str = VField(
        min_length=1, max_length=30, pattern=r"^[a-zA-Z0-9_-]+$", strip_whitespace=True
    )
    password: str = VField(min_length=8)


class SubmitArticleSchema(ValidatedModel):
    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    body: str = VField(default="", max_length=10000, strip_whitespace=True)
    category: str = VField(default="general", strip_whitespace=True)


class SearchRequestSchema(ValidatedModel):
    """JSON API search request."""

    text: str = VField(default="")
    vector: list[float] | None = None
    category: str = VField(default="")
    limit: int = VField(default=10, ge=1, le=50)


class EmbedRequestSchema(ValidatedModel):
    text: str = VField(min_length=1)


# ---------------------------------------------------------------------------
# Embeddings API client — OpenAI-compatible
# ---------------------------------------------------------------------------


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts via the OpenAI-compatible embeddings API.

    Sends a batch request and returns one embedding per input text.
    Supports any provider that implements POST /embeddings with the
    standard OpenAI request/response format.

    Raises HTTPException(503) if the API key is not configured.
    Raises HTTPException(502) if the upstream API returns an error.
    """
    if not EMBEDDINGS_API_KEY:
        raise HTTPException(
            503,
            "Embeddings API not configured — set EMBEDDINGS_API_KEY environment variable",
        )

    url = f"{EMBEDDINGS_API_URL.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {EMBEDDINGS_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "input": texts,
        "model": EMBEDDINGS_MODEL,
    }

    resp = await _http_client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        logger.error(
            "Embeddings API error: {status} {body}",
            status=resp.status_code,
            body=resp.text[:500],
        )
        raise HTTPException(502, f"Embeddings API returned {resp.status_code}")

    data = resp.json()
    try:
        embeddings = sorted(data["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in embeddings]
    except (KeyError, TypeError, IndexError) as exc:
        logger.error("Unexpected embeddings API response: {exc}", exc=exc)
        raise HTTPException(502, "Unexpected response from embeddings API")


async def embed_text(text: str) -> list[float]:
    """Embed a single text string. Convenience wrapper around embed_texts()."""
    results = await embed_texts([text])
    return results[0]


def format_vector(vec: list[float]) -> str:
    """Format a float vector for pgvector parameterized queries."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def _ctx(request, **extra) -> dict:
    """Build a template context that always includes `csrf_token`.

    Every template render MUST go through this so POST forms can
    include `<input type="hidden" name="_csrf_token" value="{{ csrf_token }}">`.
    The token is the double-submit cookie value, which CSRFMiddleware
    compares against the form field.
    """
    ctx: dict = {"csrf_token": request.cookies.get("csrftoken", "")}
    ctx.update(extra)
    return ctx


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


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def get_uid_or_none(request) -> int | None:
    """Extract user ID, returning None for anonymous users."""
    return request.user.id if request.user is not None else None


async def get_current_user(request) -> User | None:
    uid = get_uid_or_none(request)
    if uid is None:
        return None
    return await User.objects.filter(id=uid).first()


# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------


@app.get("/login")
async def login_page(request):
    return app.render("login.html", _ctx(request, error=""))


@app.post("/login")
async def login_submit(request):
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        return app.render(
            "login.html",
            _ctx(request, error="Too many login attempts — please wait a few minutes"),
        )
    try:
        data = await validate_form(request, LoginSchema)
    except ValidationErrors as exc:
        return app.render("login.html", _ctx(request, error=str(exc)))
    user = await User.objects.filter(username=data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        auth.record_failed_login(client_ip)
        return app.render("login.html", _ctx(request, error="Invalid credentials"))
    auth.clear_login_attempts(client_ip)
    resp = Response.redirect("/")
    session = await build_session_data(user.id, get_db(), username=user.username)
    auth.login(resp, session, request)
    return resp


@app.get("/register")
async def register_page(request):
    return app.render("register.html", _ctx(request, error=""))


@app.post("/register")
async def register_submit(request):
    try:
        data = await validate_form(request, RegisterSchema)
    except ValidationErrors as exc:
        return app.render("register.html", _ctx(request, error=str(exc)))
    pw_hash = hash_password(data.password)
    user = User(username=data.username, password_hash=pw_hash)
    try:
        await user.save()
    except IntegrityError:
        return app.render("register.html", _ctx(request, error="Username taken"))
    resp = Response.redirect("/")
    session = await build_session_data(user.id, get_db(), username=data.username)
    auth.login(resp, session, request)
    return resp


@app.post("/logout")
async def logout_post(request):
    resp = Response.redirect("/")
    if request.session_id:
        auth.logout(resp, request.session_id)
    return resp


# ---------------------------------------------------------------------------
# Routes: Search + Browse
# ---------------------------------------------------------------------------


@app.get("/")
async def index(request):
    """Home page: search box + recent articles."""
    user = await get_current_user(request)
    total_articles = await Article.objects.count()
    recent_articles = await Article.objects.order_by("-id").limit(10).all()
    recent = [
        {
            "id": a.id,
            "title": a.title,
            "body": a.body,
            "category": a.category.value
            if isinstance(a.category, Category)
            else a.category,
            "created_at": a.created_at,
        }
        for a in recent_articles
    ]
    return app.render(
        "index.html",
        {
            "user": user,
            "results": [],
            "recent": recent,
            "query": "",
            "category": "",
            "categories": [c.value for c in Category],
            "total": 0,
            "total_articles": total_articles,
            "timing_ms": 0,
        },
    )


@app.get("/search")
async def search(request):
    """Nearest-neighbor search with optional category filter."""
    user = await get_current_user(request)
    q = request.query("q", "")
    cat = request.query("category", "")

    if not q:
        return Response.redirect("/")

    query_vec = await embed_text(q)
    vec_str = format_vector(query_vec)
    total_articles = await Article.objects.count()

    db = get_db()
    t0 = time.perf_counter()

    if cat and cat != "all":
        rows = await db.query(
            "SELECT id, title, body, category, created_at, "
            "embedding <=> $1::vector AS distance "
            "FROM vs_articles WHERE category = $2 "
            "ORDER BY embedding <=> $1::vector LIMIT 20",
            vec_str,
            cat,
        )
    else:
        rows = await db.query(
            "SELECT id, title, body, category, created_at, "
            "embedding <=> $1::vector AS distance "
            "FROM vs_articles "
            "ORDER BY embedding <=> $1::vector LIMIT 20",
            vec_str,
        )

    timing_ms = (time.perf_counter() - t0) * 1000

    results = [
        {
            "id": r["id"],
            "title": r["title"],
            "body": r["body"][:300],
            "category": r["category"],
            "created_at": r["created_at"],
            "similarity": round(max(0, 1.0 - r["distance"]) * 100, 1),
        }
        for r in rows
    ]

    return app.render(
        "index.html",
        {
            "user": user,
            "results": results,
            "recent": [],
            "query": q,
            "category": cat,
            "categories": [c.value for c in Category],
            "total": len(results),
            "total_articles": total_articles,
            "timing_ms": round(timing_ms, 2),
        },
    )


# ---------------------------------------------------------------------------
# Routes: Article CRUD
# ---------------------------------------------------------------------------


@app.get("/submit")
@guard(Require.authenticated(redirect_url="/login"))
async def submit_page(request):
    user = await get_current_user(request)
    return app.render(
        "submit.html",
        _ctx(
            request,
            user=user,
            categories=[c.value for c in Category],
            error="",
        ),
    )


@app.post("/submit")
@guard(Require.authenticated(redirect_url="/login"))
async def submit_article(request):
    user = await get_current_user(request)
    try:
        data = await validate_form(request, SubmitArticleSchema)
    except ValidationErrors as exc:
        return app.render(
            "submit.html",
            _ctx(
                request,
                user=user,
                categories=[c.value for c in Category],
                error=str(exc),
            ),
        )
    try:
        Category(data.category)
    except ValueError:
        data.category = Category.GENERAL.value
    vec = await embed_text(data.title + " " + data.body)
    # ORM write via Article(...).save() — the VectorField serialization
    # is handled by Model._format_vector (v0.14.18) so the raw
    # list[float] flows straight through without manual `::vector`
    # casting. TimestampMixin sets created_at automatically.
    article = Article(
        title=data.title,
        body=data.body,
        category=data.category,
        author_id=user.id,
        embedding=vec,
    )
    await article.save()
    return Response.redirect("/")


@app.get("/article/{id:int}")
async def article_detail(request, id):
    user = await get_current_user(request)
    db = get_db()
    article = await db.query_one(
        "SELECT a.id, a.title, a.body, a.category, a.created_at, "
        "u.username AS author "
        "FROM vs_articles a LEFT JOIN vs_users u ON a.author_id = u.id "
        "WHERE a.id = $1",
        id,
    )
    if article is None:
        raise HTTPException(404, "Article not found")

    t0 = time.perf_counter()
    similar = await db.query(
        "WITH target AS (SELECT embedding FROM vs_articles WHERE id = $1) "
        "SELECT a.id, a.title, a.category, "
        "a.embedding <=> t.embedding AS distance "
        "FROM vs_articles a, target t WHERE a.id != $1 "
        "ORDER BY a.embedding <=> t.embedding LIMIT 5",
        id,
    )
    similar_ms = round((time.perf_counter() - t0) * 1000, 2)

    similar_list = [
        {
            "id": s["id"],
            "title": s["title"],
            "category": s["category"],
            "similarity": round(max(0, 1.0 - s["distance"]) * 100, 1),
        }
        for s in similar
    ]

    return app.render(
        "detail.html",
        {
            "user": user,
            "article": article,
            "similar": similar_list,
            "similar_ms": similar_ms,
        },
    )


# ---------------------------------------------------------------------------
# Routes: JSON API
# ---------------------------------------------------------------------------


@app.post("/api/search")
@guard(Require.authenticated())
async def api_search(request):
    """JSON API: search by text or raw vector. Requires authentication."""
    body = await request.json()
    try:
        data = SearchRequestSchema.model_validate(body)
    except ValidationErrors as exc:
        return Response.json({"error": str(exc)}, status=400)

    if data.text:
        query_vec = await embed_text(data.text)
    elif data.vector is not None:
        if len(data.vector) != VECTOR_DIM:
            return Response.json(
                {"error": f"Vector must have {VECTOR_DIM} dimensions"},
                status=400,
            )
        query_vec = data.vector
    else:
        return Response.json({"error": "Provide 'text' or 'vector'"}, status=400)

    vec_str = format_vector(query_vec)

    db = get_db()
    t0 = time.perf_counter()

    if data.category and data.category != "all":
        rows = await db.query(
            "SELECT id, title, body, category, "
            "embedding <=> $1::vector AS distance "
            "FROM vs_articles WHERE category = $2 "
            "ORDER BY embedding <=> $1::vector LIMIT $3",
            vec_str,
            data.category,
            data.limit,
        )
    else:
        rows = await db.query(
            "SELECT id, title, body, category, "
            "embedding <=> $1::vector AS distance "
            "FROM vs_articles "
            "ORDER BY embedding <=> $1::vector LIMIT $2",
            vec_str,
            data.limit,
        )

    timing_ms = (time.perf_counter() - t0) * 1000

    return Response.json(
        {
            "query": data.text or "<vector>",
            "timing_ms": round(timing_ms, 2),
            "results": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "body": r["body"][:200],
                    "category": r["category"],
                    "similarity": round(max(0, 1.0 - r["distance"]) * 100, 1),
                }
                for r in rows
            ],
        }
    )


@app.post("/api/embed")
@guard(Require.authenticated())
async def api_embed(request):
    """Return the embedding vector for a text input. Requires authentication."""
    body = await request.json()
    try:
        data = EmbedRequestSchema.model_validate(body)
    except ValidationErrors as exc:
        return Response.json({"error": str(exc)}, status=400)
    vec = await embed_text(data.text)
    return Response.json(
        {
            "text": data.text,
            "model": EMBEDDINGS_MODEL,
            "dimensions": len(vec),
            "vector": vec,
        }
    )


@app.get("/stats")
async def stats(request):
    """System stats: article count, model config, index info."""
    total = await Article.objects.count()
    return Response.json(
        {
            "articles": total,
            "vector_dimensions": VECTOR_DIM,
            "embeddings_model": EMBEDDINGS_MODEL,
            "embeddings_api": EMBEDDINGS_API_URL,
            "index_type": "hnsw",
            "distance_metric": "cosine",
            "configured": bool(EMBEDDINGS_API_KEY),
        }
    )


app.mount_health()
mount_docs(app)


# ---------------------------------------------------------------------------
# HyperAdmin model registration
# ---------------------------------------------------------------------------

admin.register(
    User,
    list_display=["id", "username"],
    search_fields=["username"],
    fieldsets=[
        Fieldset(title="Account", fields=["username"]),
    ],
)

admin.register(
    Article,
    list_display=["id", "title", "category", "author_id", "created_at"],
    search_fields=["title", "body"],
    list_filter=["category"],
    ordering="-created_at",
    exclude_fields=["embedding"],
    fieldsets=[
        Fieldset(title="Article", fields=["title", "body", "category"]),
        Fieldset(title="Author", fields=["author_id"]),
        Fieldset(title="Metadata", fields=["created_at"], classes=["collapse"]),
    ],
)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8200)
