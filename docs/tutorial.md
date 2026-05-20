# Tutorial: Build a Blog API from Scratch

A step-by-step guide to building a production-ready blog API with HyperDjango. Covers installation, models, views, routing, authentication, and deployment.

---

## Prerequisites

- Python 3.14+ (free-threaded recommended)
- PostgreSQL 16+ running locally
- Zig 0.16+ (for the native extension)
- uv package manager

---

## Step 1: Create the Project

```bash
uv init blog && cd blog
uv add hyperdjango

# Build the native Zig extension (required -- there are no fallbacks)
uv run hyper-build --install --release
```

Or use the scaffold command for a fully configured project:

```bash
uv run hyper new blog --full
cd blog
```

The `--full` preset generates database config, auth, admin, templates, and static files. For this tutorial, we will build from scratch to understand every piece.

### Verify the Build

```bash
uv run hyper doctor
```

This runs 30 diagnostic checks across 7 categories (build, python, database, performance, config, filesystem, security) and reports any issues.

---

## Step 2: Define the Application

Create `app.py` at the project root:

```python
# app.py
from hyperdjango import HyperApp

app = HyperApp(
    title="Blog API",
    database="postgres://localhost/blog",
    templates="templates/",
    static="static/",
    debug=True,
)
```

HyperApp is the single entry point for routing, middleware, database lifecycle, and the native Zig HTTP server.

### Configure Settings

HyperDjango has 114 configurable settings with validation, defaults, and environment variable support. Create a `.env` file in your project root:

```bash
# .env
SECRET_KEY=your-production-secret-key-here
DEBUG=true
DATABASE_URL=postgres://localhost/blog
POOL_SIZE=0
PREPARED_STATEMENTS=true
CACHE_BACKEND=memory
CACHE_TTL=300
```

Settings are loaded automatically from `.env` and can be overridden with `HYPER_*` environment variables:

```bash
HYPER_SECRET_KEY=mysecret HYPER_DEBUG=false uv run hyper run
```

Or set them in Django-style via `HYPERDJANGO_*` in your app config:

```python
# app.py
app = HyperApp(
    title="Blog API",
    database="postgres://localhost/blog",
    debug=True,
)
# Settings are also configurable at the app level:
# HYPERDJANGO_POOL_SIZE = 0           # 0 = auto-tune (THREAD_POOL_SIZE + headroom)
# HYPERDJANGO_STATEMENT_CACHE_SIZE = 256
# HYPERDJANGO_CONNECT_TIMEOUT = 10000  # ms
```

Key setting categories: Database (pool size, timeouts, prepared statements), Security (secret key, HSTS, CSP, CORS), CSRF (cookie flags, trusted origins), Cache (backend, TTL, max bytes), Auth (password hashers, session cookies, login URLs), Rate Limiting, Email, Logging, and Static Files. See the [Settings Reference](settings.md) for the full list.

---

## Step 3: Define Models

Create `models.py`:

```python
# models.py
from datetime import datetime
from typing import ClassVar

from hyperdjango.models import Field, ManyToManyField, Model


class Author(Model):
    class Meta:
        table = "authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    email: str = Field(unique=True)
    bio: str = Field(default="")
    created_at: datetime | None = Field(default=None)


class Tag(Model):
    class Meta:
        table = "tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
    slug: str = Field(unique=True)


class Post(Model):
    class Meta:
        table = "posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    slug: str = Field(unique=True, index=True)
    body: str = Field(default="")
    published: bool = Field(default=False)
    author_id: int = Field(foreign_key=Author)
    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)
    tags: ClassVar = ManyToManyField("tags")


class Comment(Model):
    class Meta:
        table = "comments"

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field(foreign_key=Post)
    author_name: str = Field()
    body: str = Field()
    created_at: datetime | None = Field(default=None)
```

### Anti-Enumeration with PublicIDMixin

Avoid exposing sequential integer IDs in your API — they let anyone enumerate your data and read off table sizes. Use `PublicIDMixin` for opaque, non-sequential public identifiers (defense-in-depth against _enumeration_). Note this is **not** access control: still authorize every access (ownership/tenancy/permissions) — opaque IDs alone do not prevent IDOR/BOLA:

```python
# models.py (enhanced with PublicIDMixin)
from hyperdjango.public_id import PublicIDMixin, generate_alphabet, IDMode
from hyperdjango.models import Field, Model


class Post(PublicIDMixin, Model):
    class Meta:
        table = "posts"

    class PublicIDConfig:
        # Generate a unique alphabet once: print(generate_alphabet("olc32"))
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        mode = IDMode.SIGNED  # HMAC-signed encoded PKs (tamper-proof)
        strategy = IDStrategy.ENCODED_PK

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    slug: str = Field(unique=True, index=True)
    # ... other fields
```

With `IDMode.SIGNED`, your API returns HMAC-signed opaque IDs instead of `1`, `2`, `3`:

```python
post = await Post.objects.get(id=1)
print(post.public_id)  # "W9gx3P.hmac_signature" — not guessable

# Look up by public ID
post = await Post.get_by_public_id("W9gx3P.hmac_signature")
```

Available modes: `RAW` (plain integer), `ENCODED` (base-encoded PK), `SIGNED` (HMAC-signed encoded PK), `RANDOM` (random string stored in DB column). Available strategies: `random`, `uuid7`, `encoded_pk`.

### Key Differences from Django

| Django                                         | HyperDjango                                    |
| ---------------------------------------------- | ---------------------------------------------- |
| `models.CharField(max_length=200)`             | `name: str = Field()`                          |
| `models.ForeignKey(Author, on_delete=CASCADE)` | `author_id: int = Field(foreign_key=Author)`   |
| `models.ManyToManyField(Tag)`                  | `tags: ClassVar = ManyToManyField("tags")`     |
| `models.AutoField(primary_key=True)`           | `id: int = Field(primary_key=True, auto=True)` |
| `models.BooleanField(default=False)`           | `published: bool = Field(default=False)`       |

HyperDjango uses Python type annotations instead of field classes. The type IS the schema.

---

## Step 4: Run Migrations

```bash
uv run hyper migrate
```

This introspects your models, diffs against the database schema, and generates + applies migrations. The migration system supports rollback, snapshots, and dry-run mode.

---

## Step 5: Create Views

### Function-Based Views

Add views to `app.py`:

```python
# app.py (continued)
from hyperdjango.shortcuts import get_object_or_404, redirect
from hyperdjango.response import Response

from models import Author, Comment, Post, Tag


# --- Posts ---


@app.get("/api/posts")
async def post_list(request):
    """List published posts with author info, paginated."""
    page = int(request.GET.get("page", "1"))
    per_page = int(request.GET.get("per_page", "20"))

    posts = await (
        Post.objects.filter(published=True)
        .select_related("author")
        .order_by("-created_at")
        .limit(per_page)
        .offset((page - 1) * per_page)
        .all()
    )

    return Response.json(
        {
            "posts": [
                {
                    "id": p.id,
                    "title": p.title,
                    "slug": p.slug,
                    "author": {"id": p.author.id, "name": p.author.name},
                    "created_at": str(p.created_at),
                }
                for p in posts
            ],
            "page": page,
        }
    )


@app.get("/api/posts/{slug:slug}")
async def post_detail(request, slug: str):
    """Single post with comments."""
    post = await get_object_or_404(Post, slug=slug, published=True)

    comments = await (
        Comment.objects.filter(post_id=post.id).order_by("created_at").all()
    )

    return Response.json(
        {
            "post": {
                "id": post.id,
                "title": post.title,
                "body": post.body,
                "author_id": post.author_id,
                "created_at": str(post.created_at),
            },
            "comments": [
                {
                    "author": c.author_name,
                    "body": c.body,
                    "created_at": str(c.created_at),
                }
                for c in comments
            ],
        }
    )


@app.post("/api/posts")
async def post_create(request):
    """Create a new post (requires auth)."""
    data = request.json
    post = await Post.objects.create(
        title=data["title"],
        slug=data["slug"],
        body=data.get("body", ""),
        author_id=data["author_id"],
        published=data.get("published", False),
    )
    return Response.json({"id": post.id, "slug": post.slug}, status=201)


@app.put("/api/posts/{id:int}")
async def post_update(request, id: int):
    """Update a post."""
    post = await get_object_or_404(Post, id=id)
    data = request.json
    updated = await Post.objects.filter(id=id).update(**data)
    return Response.json({"updated": updated})


@app.delete("/api/posts/{id:int}")
async def post_delete(request, id: int):
    """Delete a post."""
    deleted = await Post.objects.filter(id=id).delete()
    return Response.json({"deleted": deleted})


# --- Comments ---


@app.post("/api/posts/{post_id:int}/comments")
async def comment_create(request, post_id: int):
    """Add a comment to a post."""
    await get_object_or_404(Post, id=post_id, published=True)
    data = request.json
    comment = await Comment.objects.create(
        post_id=post_id,
        author_name=data["author_name"],
        body=data["body"],
    )
    return Response.json({"id": comment.id}, status=201)


# --- Authors ---


@app.get("/api/authors/{id:int}")
async def author_detail(request, id: int):
    """Author profile with recent posts."""
    author = await get_object_or_404(Author, id=id)
    posts = await (
        Post.objects.filter(author_id=author.id, published=True)
        .order_by("-created_at")
        .limit(10)
        .all()
    )
    return Response.json(
        {
            "author": {"id": author.id, "name": author.name, "bio": author.bio},
            "recent_posts": [
                {"id": p.id, "title": p.title, "slug": p.slug} for p in posts
            ],
        }
    )
```

### URL Parameter Types

HyperDjango's router supports typed path parameters:

| Syntax        | Type  | Pattern                |
| ------------- | ----- | ---------------------- |
| `{id:int}`    | `int` | `\d+`                  |
| `{slug:slug}` | `str` | `[-\w]+`               |
| `{uuid:uuid}` | `str` | UUID format            |
| `{name:str}`  | `str` | `[^/]+` (default)      |
| `{path:path}` | `str` | `.+` (matches slashes) |

---

## Step 6: Add Middleware

```python
# app.py (continued)
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    LoggingMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)

# Order matters: outermost middleware runs first
app.use(LoggingMiddleware())
app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=True, csp="default-src 'self'"))
app.use(
    CORSMiddleware(
        origins=["https://myblog.com"],
        methods=["GET", "POST", "PUT", "DELETE"],
        headers=["Content-Type", "Authorization"],
    )
)
```

Custom middleware follows a simple protocol:

```python
@app.middleware
async def custom_header(request, call_next):
    response = await call_next(request)
    response.headers["X-Powered-By"] = "HyperDjango"
    return response
```

---

## Step 7: Add Authentication

```python
# app.py (continued)
from hyperdjango.auth import (
    SessionAuth,
    hash_password,
    require_auth,
    verify_password,
)
from hyperdjango.auth.db_sessions import DatabaseSessionStore

# Production: database-backed sessions (PostgreSQL UNLOGGED table)
store = DatabaseSessionStore(app.db, max_age=86400)
app.use(SessionAuth(secret="change-this-to-a-real-secret", store=store))


@app.post("/api/login")
async def login(request):
    data = request.json
    users = await Author.objects.filter(email=data["email"]).all()
    if not users:
        return Response.json({"error": "Invalid credentials"}, status=401)

    user = users[0]
    # In production, use the User model from hyperdjango.auth instead
    request.session["user_id"] = user.id
    request.session["username"] = user.name
    return Response.json({"message": "Logged in", "user": user.name})


@app.post("/api/logout")
async def logout(request):
    request.session.clear()
    return Response.json({"message": "Logged out"})


@app.get("/api/me")
@require_auth()
async def me(request):
    return Response.json({"user": request.user})
```

---

## Step 8: Add Admin Interface

```python
# app.py (continued)
from hyperdjango.admin import HyperAdmin

admin = HyperAdmin(app, prefix="/admin")
admin.register(Post)
admin.register(Author)
admin.register(Tag)
admin.register(Comment)
```

Visit `/admin` to get a full CRUD interface with search, filters, pagination, and HTMX-powered interactions -- auto-generated from your models.

---

## Step 9: Run the Server

```python
# app.py (bottom of file)
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
```

```bash
uv run hyper run
```

The Zig native HTTP server starts with a 24-thread pool and radix trie router. Benchmark: 13,000+ requests/second on a single machine.

---

## Step 10: Verify Routes

```bash
uv run hyper routes
```

Output:

```
GET    /api/posts                        post_list
GET    /api/posts/{slug:slug}            post_detail
POST   /api/posts                        post_create
PUT    /api/posts/{id:int}               post_update
DELETE /api/posts/{id:int}               post_delete
POST   /api/posts/{post_id:int}/comments comment_create
GET    /api/authors/{id:int}             author_detail
POST   /api/login                        login
POST   /api/logout                       logout
GET    /api/me                           me
```

---

## Step 11: REST API with ModelViewSet

Instead of writing each endpoint by hand (Step 5), use `ModelViewSet` and `APIRouter` for full CRUD with pagination, filtering, search, and permissions in a few lines:

```python
# api.py
from hyperdjango.rest import (
    APIRouter,
    ModelSerializer,
    ModelViewSet,
    ReadOnlyModelViewSet,
)

from models import Author, Comment, Post, Tag


class PostSerializer(ModelSerializer):
    class Meta:
        model = Post
        fields = ["id", "title", "slug", "body", "published", "author_id", "created_at"]
        read_only_fields = ["id", "created_at"]


class CommentSerializer(ModelSerializer):
    class Meta:
        model = Comment
        fields = ["id", "post_id", "author_name", "body", "created_at"]
        read_only_fields = ["id", "created_at"]


class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    queryset = Post.objects.filter(published=True)
    filterset_fields = ["author_id", "published"]
    search_fields = ["title", "body"]
    ordering_fields = ["created_at", "title"]


class CommentViewSet(ReadOnlyModelViewSet):
    serializer_class = CommentSerializer
    queryset = Comment.objects


# Wire up the router
router = APIRouter()
router.register("posts", PostViewSet)
router.register("comments", CommentViewSet)
app.include_router(router, prefix="/api/v1")
```

This generates: `GET/POST /api/v1/posts/`, `GET/PUT/PATCH/DELETE /api/v1/posts/{id}/`, `GET /api/v1/comments/`, `GET /api/v1/comments/{id}/` -- all with pagination (page number, limit/offset, keyset cursor, or server-side DECLARE CURSOR), filtering, search, ordering, throttling, content negotiation, and ETag caching.

---

## Step 12: Custom Management Commands

Define project-specific CLI commands with the `@command` decorator:

```python
# commands.py
from hyperdjango.commands import command

from models import Author, Post


@command(name="seed", help="Seed the database with sample blog data")
async def seed_command(count: int = 10, verbose: bool = False):
    """Create sample authors and posts for development."""
    for i in range(count):
        author = await Author.objects.create(
            name=f"Author {i}",
            email=f"author{i}@example.com",
        )
        await Post.objects.create(
            title=f"Post {i}",
            slug=f"post-{i}",
            body=f"Content for post {i}.",
            author_id=author.id,
            published=True,
        )
        if verbose:
            print(f"Created author and post {i}")
    print(f"Seeded {count} authors and posts.")


@command(help="Clear expired sessions and stale data")
async def cleanup():
    """Periodic maintenance task."""
    # Your cleanup logic here
    print("Cleanup complete.")
```

Register and run:

```bash
# Discover commands from a module
uv run hyper seed --count=50 --verbose
uv run hyper cleanup
```

Arguments are typed automatically from function signatures -- `int`, `str`, `bool` flags all work.

---

## Step 13: Humanize Filters in Templates

Load the `humanize` library for human-friendly formatting in templates:

```python
# app.py — register the humanize library with your template engine
engine = app.template_engine
engine.load_library("humanize")
```

Then use in templates:

```html
<!-- templates/posts/detail.html -->
<p>{{ post.view_count|intcomma }} views</p>
<!-- "1,234,567" -->
<p>Published {{ post.created_at|naturaltime }}</p>
<!-- "3 hours ago" -->
<p>{{ post.file_size|filesizeformat }}</p>
<!-- "4.2 MB" -->
<p>{{ post.rank|ordinal }}</p>
<!-- "3rd" -->
<p>{{ revenue|intword }}</p>
<!-- "1.2 million" -->
```

Available humanize filters: `ordinal`, `intcomma`, `intword`, `naturaltime`, `filesizeformat`.

---

## Step 14: Database Fixtures

Seed your development database or export production snapshots with the fixture system:

```bash
# Export all posts and authors to JSON
uv run hyper dumpdata posts authors -o fixtures/blog.json

# Load fixtures (with FK dependency sorting and UPSERT semantics)
uv run hyper loaddata fixtures/blog.json
```

Programmatic usage:

```python
from hyperdjango.fixtures import dumpdata, loaddata

# Dump specific models
json_str = await dumpdata([Post, Author])

# Load from file (creates or updates via UPSERT)
result = await loaddata("fixtures/blog.json")
print(f"Created: {result.created}, Updated: {result.updated}")
```

Fixtures support natural keys, FK dependency sorting, and UPSERT semantics (insert new records, update existing ones by PK).

---

## Step 15: Testing

Write tests using the built-in `TestClient`:

```python
# tests/test_blog.py
from hyperdjango.testing import TestClient

from app import app


client = TestClient(app)


def test_post_list():
    response = client.get("/api/v1/posts/")
    assert response.status == 200
    data = response.json()
    assert "results" in data


def test_post_create():
    response = client.post(
        "/api/v1/posts/",
        json={
            "title": "Test Post",
            "slug": "test-post",
            "body": "Hello world.",
            "author_id": 1,
        },
    )
    assert response.status == 201


def test_auth_required():
    response = client.get("/api/me")
    assert response.status == 401

    client.login(username="admin", password="secret")
    response = client.get("/api/me")
    assert response.status == 200
```

Run tests:

```bash
# Run all tests
uv run hyper-test

# Run tests matching a pattern
uv run hyper-test blog
uv run hyper-test rest admin

# List available test files
uv run hyper-test --list
```

---

## Step 16: Production Build and Deployment

### Build for Production

```bash
# Build the native Zig extension in ReleaseFast mode
uv run hyper-build --release

# Verify the build and configuration
uv run hyper doctor
```

### Run in Production

```bash
# Set production environment variables
HYPER_SECRET_KEY=your-real-secret \
HYPER_DEBUG=false \
HYPER_DATABASE_URL=postgres://user:pass@dbhost/blog \
HYPER_ALLOWED_HOSTS='["blog.example.com"]' \
HYPER_SECURE_SSL_REDIRECT=true \
HYPER_SECURE_HSTS_SECONDS=31536000 \
uv run hyper run
```

Or use a `.env` file:

```bash
# .env (production)
SECRET_KEY=your-real-secret
DEBUG=false
DATABASE_URL=postgres://user:pass@dbhost/blog
ALLOWED_HOSTS=["blog.example.com"]
SECURE_SSL_REDIRECT=true
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=true
SECURE_CONTENT_TYPE_NOSNIFF=true
CSRF_COOKIE_SECURE=true
SESSION_COOKIE_SECURE=true
```

### Deployment Checklist

1. **Build**: `uv run hyper-build --release` (ReleaseFast Zig binary)
2. **Diagnostics**: `uv run hyper doctor` (full environment diagnosis)
3. **Secret key**: Set `HYPER_SECRET_KEY` to a real random value
4. **Debug off**: `HYPER_DEBUG=false`
5. **Allowed hosts**: Restrict `HYPER_ALLOWED_HOSTS` to your domain(s)
6. **HTTPS**: Enable `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`
7. **Cookie security**: Enable `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_SECURE`
8. **Database**: Use connection pooling (auto-tuned by default)
9. **Static files**: Run `uv run hyper collectstatic`
10. **Migrations**: Run `uv run hyper migrate`

See the [Deployment Guide](deployment-guide.md) for Docker, systemd, and reverse proxy configuration.

---

## Next Steps

- [Models & Fields Guide](models-guide.md) -- field types, relationships, inheritance
- [Database Queries Guide](queries-guide.md) -- filtering, aggregation, raw SQL
- [Views & Routing Guide](views-guide.md) -- CBVs, URL namespaces, shortcuts
- [Auth Guide](auth-guide.md) -- sessions, API keys, OAuth2, RBAC
- [Deployment Guide](deployment-guide.md) -- production builds, Docker, systemd
- [Security Guide](security-guide.md) -- CSRF, CORS, rate limiting, headers
