"""
Content Hub — CMS example showcasing Q objects, OneToOneField, and Single-Table Inheritance.

Demonstrates:
  - Single-Table Inheritance: Article, Video, Link share one `contents` table
  - OneToOneField: User profiles (one profile per user, UNIQUE FK)
  - Q objects: Complex content queries (OR, NOT, nested conditions)
  - Enum fields: ContentStatus, ContentType, Role
  - CursorPagination on list endpoints
  - Session auth with CSRF
  - Full middleware stack

Setup:
    uv run hyper setup --app services.content_hub.app:app --seed services.content_hub.seed:run

Run:
    uv run hyper run --app services.content_hub.app:app --port 8300
"""

import sys
from enum import Enum

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin, display
from hyperdjango.admin.fields import Action, Fieldset, InlineConfig
from hyperdjango.auth import verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.expressions import Q
from hyperdjango.guard import Require, guard
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model, OneToOneField
from hyperdjango.openapi import mount_docs
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.rest import CursorPagination
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

app = HyperApp(
    title="Content Hub",
    database=get_setting("DATABASE_URL"),
    debug=get_setting("DEBUG"),
)

app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=False))
app.use(
    CSRFMiddleware(
        secret=get_setting("CSRF_SECRET"),
        exempt_paths={"/api/contents", "/api/search"},
        exempt_prefixes={"/admin"},
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


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ContentStatus(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ContentType(Enum):
    ARTICLE = "article"
    VIDEO = "video"
    LINK = "link"


class Role(Enum):
    READER = "reader"
    EDITOR = "editor"
    ADMIN = "admin"


# ---------------------------------------------------------------------------
# Models — STI, OneToOneField, Enums
# ---------------------------------------------------------------------------


class User(TimestampMixin, Model):
    class Meta:
        table = "hub_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    password_hash: str = Field(exclude=True)
    role: Role = Field(default=Role.READER)


class UserProfile(TimestampMixin, Model):
    """One-to-one with User — each user has exactly one profile."""

    class Meta:
        table = "hub_profiles"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = OneToOneField(User, related_name="profile")
    display_name: str = Field(default="")
    bio: str = Field(default="")
    website: str = Field(default="")
    avatar_url: str = Field(default="")


class Content(TimestampMixin, Model):
    """Base content model — parent for STI.

    All content types share this table with a `type` discriminator column.
    """

    class Meta:
        table = "hub_contents"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    slug: str = Field(default="")
    body: str = Field(default="")
    type: ContentType = Field(default=ContentType.ARTICLE)
    status: ContentStatus = Field(default=ContentStatus.DRAFT)
    author_id: int = Field(foreign_key=User)
    featured: bool = Field(default=False)
    view_count: int = Field(default=0)


class Article(Content):
    """Long-form written content. STI child of Content."""

    class Meta:
        sti = True
        sti_type = "article"

    reading_time_mins: int = Field(default=0)


class Video(Content):
    """Video content with URL and duration. STI child of Content."""

    class Meta:
        sti = True
        sti_type = "video"

    video_url: str = Field(default="")
    duration_secs: int = Field(default=0)


class Link(Content):
    """External link with description. STI child of Content."""

    class Meta:
        sti = True
        sti_type = "link"

    external_url: str = Field(default="")


class Tag(TimestampMixin, Model):
    class Meta:
        table = "hub_tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)


# STI model dispatch — maps type string to the correct model class
_STI_MODEL_MAP: dict[str, type] = {
    "article": Article,
    "video": Video,
    "link": Link,
}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    count = await Content.objects.count()
    logger.info("Content Hub ready: {count} content items", count=count)


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.redirect("/api/contents")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.post("/auth/login")
async def login(request):
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        return Response.json(
            {"error": "Too many login attempts — please wait a few minutes"}, status=429
        )
    data = await request.json()
    username = data.get("username", "")
    password = data.get("password", "")
    if not username or not password:
        return Response.json({"error": "username and password required"}, status=400)
    user = await User.objects.filter(username=username).first()
    if user is None or not verify_password(password, user.password_hash):
        auth.record_failed_login(client_ip)
        return Response.json({"error": "Invalid credentials"}, status=401)
    auth.clear_login_attempts(client_ip)
    # user.role is always a Role enum — TimestampMixin + validator +
    # from_record enum-coercer guarantee this. Trust the type.
    role = user.role.value
    resp = Response.json({"message": "Logged in", "username": username, "role": role})
    session = await build_session_data(user.id, get_db(), username=username, role=role)
    auth.login(resp, session)
    return resp


def get_uid_or_none(request) -> int | None:
    """Extract user ID, returning None for anonymous users."""
    return request.user.id if request.user is not None else None


@app.get("/auth/me")
@guard(Require.authenticated())
async def me(request):
    uid = request.user["id"]
    user = await User.objects.filter(id=uid).first()
    if user is None:
        return Response.json({"error": "User not found"}, status=404)

    # Include profile via OneToOneField ORM lookup
    profile = await UserProfile.objects.filter(user_id=uid).first()
    return Response.json(
        {
            "id": user.id,
            "username": user.username,
            "role": user.role.value,
            "profile": {
                "display_name": profile.display_name,
                "bio": profile.bio,
                "website": profile.website,
                "avatar_url": profile.avatar_url,
            }
            if profile
            else None,
        }
    )


# ---------------------------------------------------------------------------
# Content CRUD — showcasing Q objects and STI
# ---------------------------------------------------------------------------


def _content_to_json(row) -> dict:
    """Convert a Content model instance or dict to JSON-safe dict."""
    if isinstance(row, dict):
        return {
            "id": row["id"],
            "title": row["title"],
            "slug": row.get("slug", ""),
            "body": row.get("body", "")[:300],
            "type": row["type"],
            "status": row["status"],
            "author_id": row["author_id"],
            "featured": row.get("featured", False),
            "view_count": row.get("view_count", 0),
            "created_at": str(row.get("created_at", "")),
        }
    # Enum fields CAN be either an Enum instance (when loaded from
    # the DB via from_record which runs the enum coercer) OR a raw
    # string (when the instance was freshly constructed via
    # model_cls(status="draft") and the STI child class bypassed the
    # validator's enum coercion — a known edge case in content_hub's
    # Article/Video STI path). Handle both shapes safely. FK int
    # columns like author_id always stay as ints unless
    # select_related was used, so no guard there.
    ctype = row.type
    cstatus = row.status
    return {
        "id": row.id,
        "title": row.title,
        "slug": row.slug,
        "body": (row.body or "")[:300],
        "type": ctype.value if isinstance(ctype, Enum) else ctype,
        "status": cstatus.value if isinstance(cstatus, Enum) else cstatus,
        "author_id": row.author_id,
        "featured": row.featured,
        "view_count": row.view_count,
        "created_at": str(row.created_at or ""),
    }


@app.get("/api/contents")
async def list_contents(request):
    """List all content with cursor pagination and Q-based filtering.

    Query params:
        type: article|video|link (filter by content type)
        status: draft|published|archived
        featured: true|false
        q: search query (title or body icontains)
    """
    paginator = CursorPagination()
    paginator.page_size = 20
    paginator.ordering = "-id"

    qs = Content.objects

    # Build Q-based filter from query params
    content_type = request.query("type", "")
    status = request.query("status", "")
    featured = request.query("featured", "")
    search = request.query("q", "")

    if content_type:
        try:
            ContentType(content_type)
            qs = qs.filter(type=content_type)
        except ValueError:
            return Response.json({"error": f"Invalid type: {content_type}"}, status=400)

    if status:
        try:
            ContentStatus(status)
            qs = qs.filter(status=status)
        except ValueError:
            return Response.json({"error": f"Invalid status: {status}"}, status=400)

    if featured == "true":
        qs = qs.filter(featured=True)

    if search:
        # Q object: search title OR body
        qs = qs.filter(Q(title__icontains=search) | Q(body__icontains=search))

    items = await paginator.paginate_queryset(qs, request)

    # Build response with author usernames (batch lookup)
    author_ids = list({item.author_id for item in items})
    authors = {}
    if author_ids:
        author_rows = await User.objects.filter(id__in=author_ids).all()
        authors = {u.id: u.username for u in author_rows}

    data = []
    for item in items:
        d = {
            "id": item.id,
            "title": item.title,
            "type": item.type,
            "status": item.status,
            "featured": item.featured,
            "author": authors.get(item.author_id, "unknown"),
            "view_count": item.view_count,
            "created_at": str(item.created_at),
        }
        data.append(d)

    return paginator.get_paginated_response(data)


@app.get("/api/contents/{id:int}")
async def get_content(request, id):
    """Get single content item with author profile (OneToOneField join)."""
    content = await Content.objects.select_related("author_id").filter(id=id).first()
    if content is None:
        raise HTTPException(404, "Content not found")

    result = _content_to_json(content)
    result["body"] = content.body  # Full body for detail view

    # Author is loaded via select_related
    author = content.author_id
    if isinstance(author, User):
        profile = await UserProfile.objects.filter(user_id=author.id).first()
        result["author"] = {
            "username": author.username,
            "display_name": profile.display_name if profile else "",
            "bio": profile.bio if profile else "",
        }
    else:
        result["author"] = {"username": "unknown", "display_name": "", "bio": ""}
    return Response.json(result)


@app.post("/api/contents")
@guard(Require.authenticated())
async def create_content(request):
    """Create content. Requires authentication. Editor or Admin role."""
    user = request.user
    if user.get("role") not in (Role.EDITOR.value, Role.ADMIN.value):
        return Response.json({"error": "Editor or Admin role required"}, status=403)

    data = await request.json()
    title = data.get("title", "").strip()
    if not title:
        return Response.json({"error": "Title is required"}, status=400)

    content_type = data.get("type", "article")
    try:
        ContentType(content_type)
    except ValueError:
        return Response.json({"error": f"Invalid type: {content_type}"}, status=400)

    status_val = data.get("status", "draft")
    try:
        ContentStatus(status_val)
    except ValueError:
        return Response.json({"error": f"Invalid status: {status_val}"}, status=400)

    # Use the correct STI model class for the content type
    model_cls = _STI_MODEL_MAP.get(content_type, Content)
    content = model_cls(
        title=title,
        slug=data.get("slug", title.lower().replace(" ", "-")[:80]),
        body=data.get("body", ""),
        status=status_val,
        author_id=user["id"],
        featured=data.get("featured", False),
    )
    await content.save()
    return Response.json(_content_to_json(content), status=201)


# ---------------------------------------------------------------------------
# Advanced Q object search
# ---------------------------------------------------------------------------


@app.post("/api/search")
async def search_content(request):
    """Advanced search with Q objects.

    Body: {
        "q": "search text",
        "types": ["article", "video"],     // optional, OR across types
        "exclude_archived": true,           // optional
        "featured_only": false,             // optional
        "min_views": 0                      // optional
    }
    """
    data = await request.json()
    search = data.get("q", "").strip()
    types = data.get("types", [])
    exclude_archived = data.get("exclude_archived", True)
    featured_only = data.get("featured_only", False)
    min_views = data.get("min_views", 0)

    qs = Content.objects

    # Text search with Q OR
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(body__icontains=search))

    # Type filter with Q OR across multiple types
    if types:
        type_q = Q(type=types[0])
        for t in types[1:]:
            type_q = type_q | Q(type=t)
        qs = qs.filter(type_q)

    # Exclude archived with NOT
    if exclude_archived:
        qs = qs.filter(~Q(status=ContentStatus.ARCHIVED.value))

    # Featured only
    if featured_only:
        qs = qs.filter(featured=True)

    # Minimum views
    if min_views > 0:
        qs = qs.filter(view_count__gte=min_views)

    results = await qs.order_by("-id").all()

    return Response.json(
        {
            "count": len(results),
            "results": [
                {
                    "id": r.id,
                    "title": r.title,
                    "type": r.type,
                    "status": r.status,
                    "featured": r.featured,
                    "view_count": r.view_count,
                }
                for r in results
            ],
        }
    )


# ---------------------------------------------------------------------------
# STI-specific endpoints
# ---------------------------------------------------------------------------


@app.get("/api/articles")
async def list_articles(request):
    """List only articles (STI auto-filters by discriminator)."""
    paginator = CursorPagination()
    paginator.page_size = 20
    paginator.ordering = "-id"
    items = await paginator.paginate_queryset(Article.objects, request)
    data = [
        {
            "id": a.id,
            "title": a.title,
            "status": a.status,
            "reading_time_mins": a.reading_time_mins,
        }
        for a in items
    ]
    return paginator.get_paginated_response(data)


@app.get("/api/videos")
async def list_videos(request):
    """List only videos (STI auto-filters by discriminator)."""
    paginator = CursorPagination()
    paginator.page_size = 20
    paginator.ordering = "-id"
    items = await paginator.paginate_queryset(Video.objects, request)
    data = [
        {
            "id": v.id,
            "title": v.title,
            "status": v.status,
            "video_url": v.video_url,
            "duration_secs": v.duration_secs,
        }
        for v in items
    ]
    return paginator.get_paginated_response(data)


@app.get("/api/links")
async def list_links(request):
    """List only links (STI auto-filters by discriminator)."""
    paginator = CursorPagination()
    paginator.page_size = 20
    paginator.ordering = "-id"
    items = await paginator.paginate_queryset(Link.objects, request)
    data = [
        {
            "id": l.id,
            "title": l.title,
            "status": l.status,
            "external_url": l.external_url,
        }
        for l in items
    ]
    return paginator.get_paginated_response(data)


# ---------------------------------------------------------------------------
# Profile endpoints (OneToOneField)
# ---------------------------------------------------------------------------


@app.get("/api/profiles/{user_id:int}")
async def get_profile(request, user_id):
    """Get a user's profile via OneToOneField."""
    profile = await UserProfile.objects.filter(user_id=user_id).first()
    if profile is None:
        raise HTTPException(404, "Profile not found")
    return Response.json(
        {
            "user_id": profile.user_id,
            "display_name": profile.display_name,
            "bio": profile.bio,
            "website": profile.website,
            "avatar_url": profile.avatar_url,
        }
    )


@app.put("/api/profiles/{user_id:int}")
@guard(Require.authenticated())
async def update_profile(request, user_id):
    """Update own profile. Requires auth + must be own profile."""
    if request.user["id"] != user_id:
        return Response.json({"error": "Can only update own profile"}, status=403)

    data = await request.json()

    # Upsert profile via OneToOneField. Previously this did 3 DB
    # roundtrips: SELECT existing → UPDATE → SELECT-again-to-return.
    # v0.14.15 `update(returning=[...])` collapses the UPDATE + final
    # SELECT into one call; the insert path needs no re-fetch because
    # save() populates the instance.
    updates: dict[str, str] = {}
    for field in ("display_name", "bio", "website", "avatar_url"):
        if field in data:
            updates[field] = data[field]

    existing = await UserProfile.objects.filter(user_id=user_id).first()
    if existing is not None:
        if updates:
            rows = await UserProfile.objects.filter(user_id=user_id).update(
                returning=["user_id", "display_name", "bio", "website", "avatar_url"],
                **updates,
            )
            row = rows[0]
        else:
            # No changes — return the existing row as-is.
            row = {
                "user_id": existing.user_id,
                "display_name": existing.display_name,
                "bio": existing.bio,
                "website": existing.website,
                "avatar_url": existing.avatar_url,
            }
    else:
        profile = UserProfile(
            user_id=user_id,
            display_name=data.get("display_name", ""),
            bio=data.get("bio", ""),
            website=data.get("website", ""),
            avatar_url=data.get("avatar_url", ""),
        )
        await profile.save()
        row = {
            "user_id": profile.user_id,
            "display_name": profile.display_name,
            "bio": profile.bio,
            "website": profile.website,
            "avatar_url": profile.avatar_url,
        }

    return Response.json(row)


# ---------------------------------------------------------------------------
# Stats + Health
# ---------------------------------------------------------------------------


@app.get("/api/stats")
async def stats(request):
    """Content statistics — showcases Q object counting."""
    total = await Content.objects.count()
    articles = await Article.objects.count()
    videos = await Video.objects.count()
    links = await Link.objects.count()
    published = await Content.objects.filter(
        status=ContentStatus.PUBLISHED.value
    ).count()
    featured = await Content.objects.filter(featured=True).count()

    # Q: published AND (article OR video) — complex count
    pub_av = await Content.objects.filter(
        Q(status=ContentStatus.PUBLISHED.value)
        & (Q(type=ContentType.ARTICLE.value) | Q(type=ContentType.VIDEO.value))
    ).count()

    return Response.json(
        {
            "total": total,
            "by_type": {"articles": articles, "videos": videos, "links": links},
            "published": published,
            "featured": featured,
            "published_articles_and_videos": pub_av,
        }
    )


app.mount_health()
mount_docs(app)


# ---------------------------------------------------------------------------
# HyperAdmin — auto-CRUD panel with RBAC, custom actions, fieldsets
# ---------------------------------------------------------------------------

admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Content Hub Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)


# Custom actions
async def publish_selected(adm, config, selected_ids, request):
    """Publish selected content items."""
    for pk in selected_ids:
        await Content.objects.filter(id=int(pk)).update(
            status=ContentStatus.PUBLISHED.value
        )
    return f"Published {len(selected_ids)} item(s)"


async def archive_selected(adm, config, selected_ids, request):
    """Archive selected content items."""
    for pk in selected_ids:
        await Content.objects.filter(id=int(pk)).update(
            status=ContentStatus.ARCHIVED.value
        )
    return f"Archived {len(selected_ids)} item(s)"


async def feature_selected(adm, config, selected_ids, request):
    """Mark selected content as featured."""
    for pk in selected_ids:
        await Content.objects.filter(id=int(pk)).update(featured=True)
    return f"Featured {len(selected_ids)} item(s)"


# Register User with profile inline
admin.register(
    User,
    list_display=["id", "username", "role", "created_at"],
    search_fields=["username"],
    list_filter=["role"],
    fieldsets=[
        Fieldset(title="Account", fields=["username", "role"]),
        Fieldset(title="Security", fields=["password_hash"], classes=["collapse"]),
    ],
    inlines=[
        InlineConfig(
            model_class=UserProfile,
            fields=["display_name", "bio", "website", "avatar_url"],
            extra=1,
            max_num=1,
            can_delete=False,
        ),
    ],
)

# ── @display decorator for computed columns ──────────────────────────────


@display(description="Popularity", ordering="view_count")
def popularity(obj):
    views = obj.get("view_count", 0)
    if views > 100:
        return f"{views} views (trending)"
    return f"{views} views"


# ── Dynamic hooks ────────────────────────────────────────────────────────


def content_readonly_fields(request, obj):
    """Lock slug after creation — prevents URL breakage."""
    if obj is not None:
        return ["slug"]
    return []


# Register Content with actions, fieldsets, and new admin features
admin.register(
    Content,
    list_display=[
        "id",
        "title",
        "type",
        "status",
        "featured",
        "popularity",
    ],
    list_display_callables={"popularity": popularity},
    list_display_links=["id", "title"],
    search_fields=["title", "body"],
    list_filter=["type", "status", "featured"],
    ordering="-id",
    fieldsets=[
        Fieldset(title="Content", fields=["title", "slug", "body"]),
        Fieldset(title="Classification", fields=["type", "status", "featured"]),
        Fieldset(
            title="Metadata", fields=["author_id", "view_count"], classes=["collapse"]
        ),
    ],
    actions=[
        Action(name="publish", label="Publish selected", handler=publish_selected),
        Action(
            name="archive",
            label="Archive selected",
            handler=archive_selected,
            confirm=True,
        ),
        Action(name="feature", label="Mark featured", handler=feature_selected),
    ],
    # New v0.16.0 features
    get_readonly_fields=content_readonly_fields,
    save_as=True,
    view_on_site=lambda obj: f"/content/{obj.get('slug', obj.get('id'))}",
    radio_fields={"status": "horizontal"},
    response_add="continue",
    empty_value_display="(not set)",
)

# Register Tag
admin.register(
    Tag,
    list_display=["id", "name"],
    search_fields=["name"],
)

# Register RBAC models for self-managing permissions
admin.register_auth_models()


if __name__ == "__main__":
    _port = int(sys.argv[1]) if len(sys.argv) > 1 else get_setting("PORT", 8300)
    app.run(port=_port)
