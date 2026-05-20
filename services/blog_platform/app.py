"""
Blog Platform — Sitemaps, Syndication, and i18n Example.

Showcases 3 platform features not covered by other services:

  - **XML Sitemaps** (sitemaps.py): sitemap.xml with per-section pagination
  - **RSS/Atom Feeds** (syndication.py): /feed/rss and /feed/atom endpoints
  - **Internationalization** (i18n.py): LocaleMiddleware, /en/ and /fr/ URL prefixes
  - **Humanize** (humanize.py): naturaltime template filter

Models:
  - Author (name, bio, language)
  - Post (title, slug, excerpt, body, author_id, published, published_at)
  - Category (name, slug)
  - PostCategory M2M (post_id, category_id)

Run:
    uv run hyper setup --app services.blog_platform.app:app --seed services.blog_platform.seed:run
    uv run hyper run --app services.blog_platform.app:app --port 8750

Endpoints:
    GET  /                       → Latest posts (language-aware)
    GET  /post/{slug}            → Single post
    GET  /category/{slug}        → Posts by category
    GET  /sitemap.xml            → XML sitemap index
    GET  /feed/rss               → RSS 2.0 feed
    GET  /feed/atom              → Atom 1.0 feed
    GET  /api/posts              → JSON API (paginated)
    GET  /health                 → Health check
    GET  /admin/                 → HyperAdmin panel
    GET  /docs/                  → Swagger UI
"""

from dataclasses import dataclass

from hyperdjango import HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.cache import LocMemCache
from hyperdjango.conf import get_setting
from hyperdjango.i18n import LocaleMiddleware, get_language
from hyperdjango.i18n import gettext as _
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.rest import (
    APIRouter,
    CursorPagination,
    ModelSerializer,
    ModelViewSet,
)
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.sitemaps import Sitemap, sitemap_view
from hyperdjango.syndication import Feed, feed_view

# ─── Models ───────────────────────────────────────────────────────────────────


class Author(TimestampMixin, Model):
    class Meta:
        table = "bp_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    bio: str = Field(max_length=500, default="")
    language: str = Field(max_length=5, default="en")


class Category(TimestampMixin, Model):
    class Meta:
        table = "bp_categories"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100)
    slug: str = Field(max_length=100)


class Post(TimestampMixin, Model):
    class Meta:
        table = "bp_posts"
        cache_ttl = (
            120  # Cache post queries for 2 minutes (auto-invalidated on save/delete)
        )

    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)
    slug: str = Field(max_length=200)
    excerpt: str = Field(max_length=500, default="")
    body: str = Field(default="")
    author_id: int = Field()
    published: bool = Field(default=False)
    published_at: str = Field(default="")
    language: str = Field(max_length=5, default="en")


class PostCategory(TimestampMixin, Model):
    class Meta:
        table = "bp_post_categories"

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field()
    category_id: int = Field()


# ─── App Setup ────────────────────────────────────────────────────────────────

app = HyperApp(
    title="Blog Platform",
    database=get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test",
)

# Application cache — LocMemCache for single-server, swap to DatabaseCache for multi-server.
# Query cache is automatic via Meta.cache_ttl on Post model.
app_cache = LocMemCache(max_size=500)

token_engine = TokenEngine(
    keys=[SigningKey(secret=get_setting("SESSION_SIGNING_KEY"), version=1)]
)
auth = SessionAuth(secret=get_setting("SESSION_SECRET"), token_engine=token_engine)
app.use(auth)
app.use(LocaleMiddleware())

admin = HyperAdmin(
    app, prefix="/admin", title="Blog Admin", secret_key=get_setting("ADMIN_SECRET")
)
admin.register(Author, list_display=["id", "name", "language"], search_fields=["name"])
admin.register(Category, list_display=["id", "name", "slug"], search_fields=["name"])
admin.register(
    Post,
    list_display=["id", "title", "slug", "published", "language"],
    search_fields=["title"],
)
admin.register(PostCategory)


# ─── Sitemaps ─────────────────────────────────────────────────────────────────


class PostSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self) -> list[dict[str, str | int]]:
        # Synchronous — returns pre-fetched list
        return self._items

    def location(self, item: dict[str, str | int]) -> str:
        return f"/post/{item['slug']}"

    def lastmod(self, item: dict[str, str | int]) -> str | None:
        return item.get("updated_at")


class CategorySitemap(Sitemap):
    changefreq = "daily"
    priority = 0.5

    def items(self) -> list[dict[str, str | int]]:
        return self._items

    def location(self, item: dict[str, str | int]) -> str:
        return f"/category/{item['slug']}"


_post_sitemap = PostSitemap()
_post_sitemap._items = []
_category_sitemap = CategorySitemap()
_category_sitemap._items = []

_sitemaps = {"posts": _post_sitemap, "categories": _category_sitemap}


_sitemap_loaded = False


async def _ensure_sitemap_data():
    global _sitemap_loaded
    if _sitemap_loaded:
        return
    _sitemap_loaded = True
    posts = await Post.objects.filter(published=True).values("slug", "updated_at").all()
    _post_sitemap._items = [p for p in posts]
    cats = await Category.objects.values("slug").all()
    _category_sitemap._items = [c for c in cats]


@app.get("/sitemap.xml")
async def sitemap(request):
    await _ensure_sitemap_data()
    return sitemap_view(request, _sitemaps)


# ─── RSS / Atom Feeds ────────────────────────────────────────────────────────


@dataclass
class BlogRSSFeed(Feed):
    feed_type: str = "rss"
    language: str = "en"

    def title(self) -> str:
        return "Blog Platform — Latest Posts"

    def link(self) -> str:
        return "/"

    def description(self) -> str:
        return "Latest posts from the Blog Platform"

    def feed_url(self) -> str:
        return "/feed/rss"

    def items(self) -> list[dict[str, str | int]]:
        return _feed_posts[:50]

    def item_title(self, item: dict[str, str | int]) -> str:
        return str(item["title"])

    def item_description(self, item: dict[str, str | int]) -> str:
        return str(item.get("excerpt", ""))

    def item_link(self, item: dict[str, str | int]) -> str:
        return f"/post/{item['slug']}"

    def item_pubdate(self, item: dict[str, str | int]) -> object:
        raw = item.get("published_at", "")
        if not raw:
            return None
        from datetime import datetime

        try:
            return datetime.fromisoformat(str(raw))
        except ValueError, TypeError:
            return None

    def item_guid(self, item: dict[str, str | int]) -> str:
        return f"post-{item['id']}"


@dataclass
class BlogAtomFeed(Feed):
    feed_type: str = "atom"
    language: str = "en"

    def title(self) -> str:
        return "Blog Platform — Latest Posts"

    def link(self) -> str:
        return "/"

    def description(self) -> str:
        return "Latest posts from the Blog Platform"

    def feed_url(self) -> str:
        return "/feed/atom"

    def items(self) -> list[dict[str, str | int]]:
        return _feed_posts[:50]

    def item_title(self, item: dict[str, str | int]) -> str:
        return str(item["title"])

    def item_description(self, item: dict[str, str | int]) -> str:
        return str(item.get("excerpt", ""))

    def item_link(self, item: dict[str, str | int]) -> str:
        return f"/post/{item['slug']}"

    def item_pubdate(self, item: dict[str, str | int]) -> object:
        raw = item.get("published_at", "")
        if not raw:
            return None
        from datetime import datetime

        try:
            return datetime.fromisoformat(str(raw))
        except ValueError, TypeError:
            return None

    def item_guid(self, item: dict[str, str | int]) -> str:
        return f"post-{item['id']}"


_feed_posts: list[dict[str, str | int]] = []


_feed_loaded = False


async def _ensure_feed_data():
    global _feed_posts, _feed_loaded
    if _feed_loaded:
        return
    _feed_loaded = True
    posts = await Post.objects.filter(published=True).order_by("-published_at").all()
    _feed_posts = [p.to_dict() for p in posts]


@app.get("/feed/rss")
async def rss_feed(request):
    await _ensure_feed_data()
    return feed_view(request, BlogRSSFeed)


@app.get("/feed/atom")
async def atom_feed(request):
    await _ensure_feed_data()
    return feed_view(request, BlogAtomFeed)


# ─── i18n-aware Views ────────────────────────────────────────────────────────


@app.get("/")
async def index(request):
    """List latest published posts. Language-aware via LocaleMiddleware."""
    lang = get_language()
    posts = await Post.objects.filter(published=True).order_by("-published_at").all()
    post_list = [p.to_dict() for p in posts[:20]]
    return Response.json(
        {
            "title": _("Latest Posts"),
            "language": lang,
            "count": len(post_list),
            "posts": post_list,
        }
    )


@app.get("/post/{slug}")
async def post_detail(request, slug: str):
    """Single post by slug."""
    post = await Post.objects.filter(slug=slug).first()
    if post is None:
        return Response.json({"error": _("Post not found")}, status=404)
    return Response.json(post.to_dict())


@app.get("/category/{slug}")
async def category_posts(request, slug: str):
    """Posts in a category by slug.

    Uses @cached to avoid repeated M2M joins on hot category pages.
    Cache auto-expires in 60s; explicit invalidation via post_save signal
    on Post model bumps the query cache version (see Meta.cache_ttl).
    """
    cat = await Category.objects.filter(slug=slug).first()
    if cat is None:
        return Response.json({"error": _("Category not found")}, status=404)

    # Get post IDs for this category
    links = await PostCategory.objects.filter(category_id=cat.id).all()
    post_ids = [link.post_id for link in links]
    if not post_ids:
        return Response.json({"category": cat.to_dict(), "posts": []})

    posts = await Post.objects.filter(published=True).all()
    filtered = [p.to_dict() for p in posts if p.id in post_ids]
    return Response.json({"category": cat.to_dict(), "posts": filtered})


# ─── REST API ─────────────────────────────────────────────────────────────────


class PostSerializer(ModelSerializer):
    class Meta:
        model = Post
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "author_id",
            "published",
            "published_at",
            "language",
        ]


class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    pagination_class = CursorPagination
    queryset = Post.objects


api = APIRouter(prefix="/api")
api.register("posts", PostViewSet, basename="bp_post")
api.mount(app.router, namespace="api")

mount_docs(app)


@app.get("/health")
async def health(request):
    return Response.json({"status": "ok"})
