"""
Bookstore API — Premier REST Framework Showcase.

Demonstrates every major REST framework feature end-to-end:
  - ModelSerializer with nested serializers, computed fields, validation
  - ModelViewSet with full CRUD + custom @action endpoints
  - PageNumberPagination and CursorPagination
  - FieldFilter, SearchFilter, OrderingFilter
  - Permission classes (IsAuthenticated, AllowAny for reads)
  - OpenAPI 3.1 / Swagger UI at /docs
  - ETag / conditional caching (304 Not Modified)
  - Nested router (authors/{id}/books)
  - Session auth + API key auth
  - perform_create/perform_update hooks
  - SerializerMethodField for computed output

Run:
    uv run hyper setup --app services.bookstore_api.app:app --seed services.bookstore_api.seed:run
    uv run hyper run --app services.bookstore_api.app:app --port 8900

API:
    GET  /docs                    → Swagger UI
    GET  /openapi.json            → OpenAPI 3.1 spec
    GET  /api/v1/                 → API root (endpoint discovery)
    GET  /api/v1/books/           → List books (paginated, filterable, searchable)
    POST /api/v1/books/           → Create book (auth required)
    GET  /api/v1/books/{id}       → Book detail (ETag cached)
    PUT  /api/v1/books/{id}       → Update book (auth required)
    PATCH /api/v1/books/{id}      → Partial update (auth required)
    DELETE /api/v1/books/{id}     → Delete book (auth required)
    POST /api/v1/books/{id}/publish   → Publish a book (custom action)
    POST /api/v1/books/{id}/feature   → Feature a book (custom action)
    GET  /api/v1/authors/         → List authors
    GET  /api/v1/authors/{id}     → Author detail
    GET  /api/v1/authors/{author_id}/books/  → Books by author (nested)
    GET  /api/v1/categories/      → List categories
    GET  /api/v1/reviews/         → List reviews (cursor-paginated)
    POST /api/v1/reviews/         → Create review (auth required)
"""

import time as _time
from pathlib import Path

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.admin.fields import Action, Fieldset
from hyperdjango.auth.api_keys import APIKeyAuth
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.auth.user import User
from hyperdjango.cache import LocMemCache
from hyperdjango.cache_adapters import StampedeProtection, TwoTierCache
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.dataloader import DataLoader
from hyperdjango.expressions import Avg, Count
from hyperdjango.guard import Require, guard, guard_action
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.performance import PerformanceMiddleware, set_perf_middleware
from hyperdjango.profiling import get_store
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.rest import (
    AllowAny,
    APIRouter,
    BulkModelViewSet,
    CacheableMixin,
    CursorPagination,
    FieldFilter,
    FullTextSearchFilter,
    IsAuthenticatedOrReadOnly,
    ModelSerializer,
    ModelViewSet,
    NestedRouter,
    NotFound,
    OrderingFilter,
    PageNumberPagination,
    ReadOnlyModelViewSet,
    SearchFilter,
    SerializerMethodField,
    ValidationError,
    action,
)
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
    VersionMiddleware,
)
from hyperdjango.telemetry import configure_from_settings

_APP_DIR = Path(__file__).resolve().parent

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

DATABASE_URL = get_setting("DATABASE_URL")
_DEBUG = get_setting("DEBUG")

app = HyperApp(
    title="Bookstore API",
    database=DATABASE_URL,
    templates=str(_APP_DIR / "templates"),
    debug=_DEBUG,
)

# ---------------------------------------------------------------------------
# Middleware (outermost first)
# ---------------------------------------------------------------------------

app.use(VersionMiddleware())
app.use(TimingMiddleware())
_perf = PerformanceMiddleware(
    slow_query_threshold_ms=50, dashboard_path="/debug/performance"
)
app.use(_perf)
set_perf_middleware(_perf)

# --- Native telemetry (v0.15.1) -----------------------------------------------
if _DEBUG:
    DEFAULTS["TELEMETRY_ENABLED"] = True
    DEFAULTS["TELEMETRY_SAMPLE_RATIO"] = 1.0
_telemetry = configure_from_settings(app)
if _telemetry is not None and _telemetry.prometheus_sink is not None:
    app.get("/metrics")(_telemetry.prometheus_sink.handler)

app.use(SecurityHeadersMiddleware(hsts=False))
app.use(
    CORSMiddleware(
        origins=["*"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        headers=["Content-Type", "Authorization", "X-API-Key"],
    )
)
if get_setting("LOAD_TEST"):
    logger.warning("HYPER_LOAD_TEST=1: rate limiting DISABLED (load test mode)")
else:
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
app.use(APIKeyAuth(valid_keys={get_setting("API_KEY")}))

# OpenAPI docs
mount_docs(
    app,
    title="Bookstore API",
    version="1.0.0",
    description="Premier REST framework showcase — ViewSets, serializers, pagination, filtering, caching",
)

# HyperAdmin — auto-CRUD panel
admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Bookstore Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)


# ---------------------------------------------------------------------------
# Exception handlers — consistent JSON error format
# ---------------------------------------------------------------------------


@app.exception_handler(ValidationError)
async def _handle_validation(request, exc):
    # Platform convention: `{"detail": "..."}` only — the HTTP status
    # code is canonical on the wire, no need to duplicate it in the
    # JSON body. Applies to all exception handlers across examples.
    return Response.json({"detail": str(exc)}, status=400)


@app.exception_handler(Exception)
async def _handle_generic(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    db = get_db()
    # Ensure RBAC tables exist (hyper_users, hyper_groups, etc.)
    checker = PermissionChecker(db)
    await checker.ensure_tables()
    count = await Book.objects.count()
    logger.info("Bookstore API ready: {count} books", count=count)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Author(TimestampMixin, Model):
    class Meta:
        table = "bk_authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    bio: str = Field(default="")
    website: str = Field(default="")


class Category(TimestampMixin, Model):
    class Meta:
        table = "bk_categories"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
    slug: str = Field(unique=True)
    description: str = Field(default="")


class Book(TimestampMixin, Model):
    class Meta:
        table = "bk_books"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    isbn: str = Field(unique=True)
    description: str = Field(default="")
    price: str = Field(default="0.00")
    pages: int = Field(default=0)
    published: bool = Field(default=False)
    featured: bool = Field(default=False)
    author_id: int = Field(foreign_key=Author)
    category_id: int = Field(foreign_key=Category)


class Review(TimestampMixin, Model):
    class Meta:
        table = "bk_reviews"

    id: int = Field(primary_key=True, auto=True)
    book_id: int = Field(foreign_key=Book)
    reviewer_name: str = Field()
    rating: int = Field(default=5)
    comment: str = Field(default="")


# ── Cache infrastructure ──────────────────────────────────────────────────────
# StampedeProtection prevents thundering herd on popular endpoints.
# TwoTierCache: L1 (in-process LocMemCache) + L2 (shared LocMemCache for demo).
_stampede_cache = StampedeProtection(backend=LocMemCache(max_size=256), beta=1.0)
_two_tier = TwoTierCache(
    l1=LocMemCache(max_size=128),
    l2=LocMemCache(max_size=512),
    l1_ttl=5,
)


# Auth: uses framework hyper_users table + RBAC groups via PermissionChecker.
# No app-specific user table — auth managed by the RBAC system.


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


class AuthorSerializer(ModelSerializer):
    class Meta:
        model = Author
        fields = "__all__"
        read_only_fields = ["id"]


class CategorySerializer(ModelSerializer):
    class Meta:
        model = Category
        fields = "__all__"
        read_only_fields = ["id"]


class BookListSerializer(ModelSerializer):
    """Lightweight serializer for list views — no description, no nested objects."""

    class Meta:
        model = Book
        fields = [
            "id",
            "title",
            "isbn",
            "price",
            "pages",
            "published",
            "featured",
            "author_id",
            "category_id",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class BookDetailSerializer(ModelSerializer):
    """Full serializer for detail views — includes computed fields."""

    author_name: str = SerializerMethodField()
    category_name: str = SerializerMethodField()
    review_count: int = SerializerMethodField()
    avg_rating: float = SerializerMethodField()

    class Meta:
        model = Book
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_author_name(self, obj):
        if isinstance(obj, dict):
            return obj.get("_author_name", "")
        return ""

    def get_category_name(self, obj):
        if isinstance(obj, dict):
            return obj.get("_category_name", "")
        return ""

    def get_review_count(self, obj):
        if isinstance(obj, dict):
            return obj.get("_review_count", 0)
        return 0

    def get_avg_rating(self, obj):
        if isinstance(obj, dict):
            return obj.get("_avg_rating", 0.0)
        return 0.0


class BookWriteSerializer(ModelSerializer):
    """Write serializer — validates input for create/update, includes id in output."""

    class Meta:
        model = Book
        fields = [
            "id",
            "title",
            "isbn",
            "description",
            "price",
            "pages",
            "published",
            "featured",
            "author_id",
            "category_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "published", "featured", "created_at", "updated_at"]


class ReviewSerializer(ModelSerializer):
    class Meta:
        model = Review
        fields = "__all__"
        read_only_fields = ["id", "created_at"]


class ReviewWriteSerializer(ModelSerializer):
    class Meta:
        model = Review
        fields = ["id", "book_id", "reviewer_name", "rating", "comment", "created_at"]
        read_only_fields = ["id", "created_at"]


# ---------------------------------------------------------------------------
# Pagination classes
# ---------------------------------------------------------------------------


class BookPagination(PageNumberPagination):
    page_size = 10
    max_page_size = 50


class ReviewCursorPagination(CursorPagination):
    page_size = 10
    ordering = "-id"
    cursor_query_param = "cursor"


# ---------------------------------------------------------------------------
# ViewSets
# ---------------------------------------------------------------------------


class BookViewSet(CacheableMixin, BulkModelViewSet):
    """Full CRUD for books with filtering, search, pagination, and caching.

    Showcases:
    - ModelViewSet (list/create/retrieve/update/partial_update/destroy)
    - PageNumberPagination with configurable page_size
    - FieldFilter (filter by published, featured, category_id, author_id)
    - SearchFilter (full-text search on title + description)
    - OrderingFilter (sort by title, price, pages, created_at)
    - CacheableMixin (ETag + Cache-Control on retrieve)
    - IsAuthenticatedOrReadOnly (read = anyone, write = auth)
    - Custom @action endpoints (publish, feature)
    - perform_create hook (auto-set timestamps)
    """

    serializer_class = BookListSerializer
    model = Book
    permission_classes = (IsAuthenticatedOrReadOnly,)
    pagination_class = BookPagination
    filter_backends = (FieldFilter, FullTextSearchFilter, OrderingFilter)
    filterset_fields = ("published", "featured", "category_id", "author_id")
    search_fields = (
        "title",
        "description",
    )  # FullTextSearchFilter uses PostgreSQL tsvector/tsquery
    ordering_fields = ("id", "title", "price", "pages", "created_at")
    ordering = ("-id",)

    # ETag caching
    cache_max_age = 30
    cache_private = True

    def get_serializer_class(self):
        if self.action == "retrieve":
            return BookDetailSerializer
        if self.action in ("create", "update", "partial_update"):
            return BookWriteSerializer
        return BookListSerializer

    async def retrieve(self, request, **kwargs):
        """Override retrieve to enrich with author/category/review data.

        Uses select_related for FK JOINs + aggregate for review stats.
        """
        lookup_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_value = self.kwargs.get(lookup_kwarg)
        if lookup_value is None:
            raise NotFound("Not found")

        book = await (
            Book.objects.select_related("author_id", "category_id")
            .filter(id=lookup_value)
            .first()
        )
        if book is None:
            raise NotFound("Not found")

        # Review stats via ORM aggregate
        review_stats = await Review.objects.filter(book_id=book.id).aggregate(
            cnt=Count("id"),
            avg_rating=Avg("rating"),
        )

        obj = book.to_dict()
        # select_related replaces FK int with the full model instance
        author = book.author_id
        category = book.category_id
        obj["_author_name"] = author.name if isinstance(author, Author) else ""
        obj["_category_name"] = category.name if isinstance(category, Category) else ""
        obj["_review_count"] = review_stats.get("cnt", 0) or 0
        obj["_avg_rating"] = round(float(review_stats.get("avg_rating", 0) or 0), 1)

        serializer = BookDetailSerializer(obj=obj)
        response = Response.json(serializer.data)

        # Apply ETag caching
        if isinstance(response.body, str):
            body_bytes = response.body.encode()
        else:
            body_bytes = response.body
        response = self._apply_cache_headers(response, request, body_bytes)
        return response

    async def perform_create(self, serializer):
        """Create a book via ORM. Validates FK references exist."""
        data = dict(serializer.validated_data)

        # Validate FK references exist via ORM
        author = await Author.objects.filter(id=data.get("author_id", 0)).first()
        if author is None:
            raise ValidationError(
                "Author not found", errors={"author_id": ["Author not found"]}
            )
        cat = await Category.objects.filter(id=data.get("category_id", 0)).first()
        if cat is None:
            raise ValidationError(
                "Category not found", errors={"category_id": ["Category not found"]}
            )

        book = Book(
            title=data["title"],
            isbn=data["isbn"],
            description=data.get("description", ""),
            price=data.get("price", "0.00"),
            pages=data.get("pages", 0),
            author_id=data["author_id"],
            category_id=data["category_id"],
        )
        await book.save()
        return book

    async def perform_update(self, serializer, instance):
        """Auto-set updated_at on update."""
        data = dict(serializer.validated_data)
        for key, val in data.items():
            setattr(instance, key, val)
        await instance.save()
        return instance

    @action(methods=["POST"], detail=True, url_path="publish")
    @guard_action(Require.authenticated(), Require.staff())
    async def publish(self, request, **kwargs):
        """Publish a book — sets published=True. Requires staff."""
        instance = await self.get_object()
        if instance.published:
            return Response.json({"detail": "Already published"}, status=400)
        instance.published = True
        await instance.save()
        serializer = BookListSerializer(obj=instance)
        return Response.json(serializer.data)

    @action(methods=["POST"], detail=True, url_path="feature")
    @guard_action(Require.authenticated(), Require.staff())
    async def feature(self, request, **kwargs):
        """Toggle featured status on a book. Requires staff."""
        instance = await self.get_object()
        instance.featured = not instance.featured
        await instance.save()
        serializer = BookListSerializer(obj=instance)
        return Response.json(serializer.data)

    @action(methods=["GET"], detail=False, url_path="featured")
    async def list_featured(self, request, **kwargs):
        """List featured books with cursor pagination."""
        paginator = CursorPagination()
        paginator.page_size = 20
        paginator.ordering = "-id"
        qs = Book.objects.filter(featured=True)
        items = await paginator.paginate_queryset(qs, request)
        data = [BookListSerializer(obj=b).data for b in items]
        return paginator.get_paginated_response(data)

    @action(methods=["GET"], detail=False, url_path="stats")
    async def stats(self, request, **kwargs):
        """Book collection statistics via ORM aggregate with FILTER."""
        result = await Book.objects.aggregate(
            total=Count("id"),
            published=Count("id", filter_expr={"published": True}),
            featured=Count("id", filter_expr={"featured": True}),
            avg_pages=Avg("pages"),
        )
        return Response.json(
            {
                "total_books": result.get("total", 0) or 0,
                "published": result.get("published", 0) or 0,
                "featured": result.get("featured", 0) or 0,
                "avg_pages": round(float(result.get("avg_pages", 0) or 0), 1),
            }
        )

    @action(methods=["GET"], detail=False, url_path="enriched")
    async def list_enriched(self, request, **kwargs):
        """List books with author/category names — DataLoader N+1 prevention.

        Demonstrates: DataLoader batch loading to resolve N related objects
        in O(1) queries instead of O(N). Each unique author_id and category_id
        is fetched exactly once, regardless of how many books reference it.

        Compare: without DataLoader, listing 50 books would need 50 author
        queries + 50 category queries = 100 extra queries. With DataLoader,
        it's 1 author batch query + 1 category batch query = 2 extra queries.
        """

        async def _batch_authors(keys: list[int]) -> list[Author | None]:
            authors = await Author.objects.filter(id__in=keys).all()
            by_id = {a.id: a for a in authors}
            return [by_id.get(k) for k in keys]

        async def _batch_categories(keys: list[int]) -> list[Category | None]:
            cats = await Category.objects.filter(id__in=keys).all()
            by_id = {c.id: c for c in cats}
            return [by_id.get(k) for k in keys]

        author_loader = DataLoader(batch_fn=_batch_authors)
        category_loader = DataLoader(batch_fn=_batch_categories)

        paginator = self.pagination_class()
        qs = self.filter_queryset(self.get_queryset())
        books = await paginator.paginate_queryset(qs, request)

        # Batch load all authors and categories in 2 queries (not N)
        author_ids = [b.author_id for b in books]
        category_ids = [b.category_id for b in books]
        authors = await author_loader.load_many(author_ids)
        categories = await category_loader.load_many(category_ids)

        data = []
        for book, author, category in zip(books, authors, categories):
            d = book.to_dict()
            d["author_name"] = author.name if author else ""
            d["category_name"] = category.name if category else ""
            data.append(d)

        return paginator.get_paginated_response(data)

    @action(methods=["GET"], detail=False, url_path="cached-stats")
    async def cached_stats(self, request, **kwargs):
        """Book stats with StampedeProtection — XFetch prevents thundering herd.

        Demonstrates: StampedeProtection wrapping a LocMemCache. On cache miss,
        computes stats and stores with compute_time_ms so XFetch can scale
        early expiry probability. Response includes _cache hit/miss indicator.
        """
        cached = _stampede_cache.get("book_stats")
        if cached is not None:
            return Response.json({**cached, "_cache": "hit"})

        start = _time.monotonic()
        result = await Book.objects.aggregate(
            total=Count("id"),
            published=Count("id", filter_expr={"published": True}),
            featured=Count("id", filter_expr={"featured": True}),
            avg_pages=Avg("pages"),
        )
        compute_ms = (_time.monotonic() - start) * 1000

        stats_data = {
            "total_books": result.get("total", 0) or 0,
            "published": result.get("published", 0) or 0,
            "featured": result.get("featured", 0) or 0,
            "avg_pages": round(float(result.get("avg_pages", 0) or 0), 1),
        }
        _stampede_cache.set(
            "book_stats", stats_data, ttl=60, compute_time_ms=compute_ms
        )
        return Response.json(
            {**stats_data, "_cache": "miss", "_compute_ms": round(compute_ms, 2)}
        )

    @action(methods=["GET"], detail=False, url_path="two-tier-stats")
    async def two_tier_stats(self, request, **kwargs):
        """Book stats with TwoTierCache — L1 local + L2 shared with promotion.

        Demonstrates: TwoTierCache where L1 (fast in-process) caches hot data
        and L2 (shared) serves as fallback. First request misses both tiers,
        second hits L1 directly. Response includes tier stats.
        """
        cached = _two_tier.get("book_stats_v2")
        if cached is not None:
            tier_stats = _two_tier.get_stats()
            return Response.json({**cached, "_cache": "hit", "_tier_stats": tier_stats})

        result = await Book.objects.aggregate(
            total=Count("id"),
            published=Count("id", filter_expr={"published": True}),
        )
        stats_data = {
            "total_books": result.get("total", 0) or 0,
            "published": result.get("published", 0) or 0,
        }
        _two_tier.set("book_stats_v2", stats_data, ttl=120)
        tier_stats = _two_tier.get_stats()
        return Response.json(
            {**stats_data, "_cache": "miss", "_tier_stats": tier_stats}
        )


class AuthorViewSet(CacheableMixin, ReadOnlyModelViewSet):
    """Read-only ViewSet for authors with search."""

    serializer_class = AuthorSerializer
    model = Author
    permission_classes = (AllowAny,)
    pagination_class = PageNumberPagination
    filter_backends = (SearchFilter, OrderingFilter)
    search_fields = ("name",)
    ordering_fields = ("id", "name")
    ordering = ("name",)
    cache_max_age = 60


class AuthorBooksViewSet(ReadOnlyModelViewSet):
    """Nested ViewSet: list books by author (demonstrates NestedRouter)."""

    serializer_class = BookListSerializer
    model = Book
    permission_classes = (AllowAny,)
    pagination_class = BookPagination

    def get_queryset(self):
        author_id = self.kwargs.get("author_id")
        return Book.objects.filter(author_id=author_id)


class CategoryViewSet(ReadOnlyModelViewSet):
    """Read-only ViewSet for categories."""

    serializer_class = CategorySerializer
    model = Category
    permission_classes = (AllowAny,)
    pagination_class = PageNumberPagination
    filter_backends = (OrderingFilter,)
    ordering_fields = ("id", "name")
    ordering = ("name",)


class ReviewViewSet(ModelViewSet):
    """Reviews with cursor pagination (demonstrates CursorPagination).

    Showcases:
    - CursorPagination (opaque, tamper-resistant cursors)
    - FieldFilter (filter by book_id, rating)
    - OrderingFilter
    - perform_create hook (auto-set created_at)
    """

    serializer_class = ReviewSerializer
    model = Review
    permission_classes = (IsAuthenticatedOrReadOnly,)
    pagination_class = ReviewCursorPagination
    filter_backends = (FieldFilter, OrderingFilter)
    filterset_fields = ("book_id", "rating")
    ordering_fields = ("id", "rating", "created_at")
    ordering = ("-id",)

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ReviewWriteSerializer
        return ReviewSerializer

    async def perform_create(self, serializer):
        data = dict(serializer.validated_data)

        # Validate book exists via ORM
        book_exists = await Book.objects.filter(id=data.get("book_id", 0)).exists()
        if not book_exists:
            raise ValidationError(
                "Book not found", errors={"book_id": ["Book not found"]}
            )

        # Validate rating range
        rating = data.get("rating", 5)
        if rating < 1 or rating > 5:
            raise ValidationError(
                "Rating must be between 1 and 5",
                errors={"rating": ["Must be between 1 and 5"]},
            )

        review = Review(
            book_id=data["book_id"],
            reviewer_name=data["reviewer_name"],
            rating=rating,
            comment=data.get("comment", ""),
        )
        await review.save()
        return review


# ---------------------------------------------------------------------------
# Router — register all ViewSets
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1")
router.register("books", BookViewSet)
router.register("authors", AuthorViewSet)
router.register("categories", CategoryViewSet)
router.register("reviews", ReviewViewSet)

# Nested: /api/v1/authors/{author_id}/books/
author_books_router = NestedRouter(
    parent_router=router,
    parent_prefix="authors",
    lookup="author_id",
)
author_books_router.register("books", AuthorBooksViewSet)

# Mount onto app
router.mount(app.router, namespace="api")
author_books_router.mount(app.router, namespace="api")


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.redirect("/docs/")


# ---------------------------------------------------------------------------
# Auth endpoints (session-based)
# ---------------------------------------------------------------------------


@app.post("/auth/login")
async def login(request):
    """Login and create a session. Returns JSON."""
    data = await request.json()
    username = data.get("username", "")
    password = data.get("password", "")
    if not username or not password:
        raise HTTPException(400, "username and password required")

    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        raise HTTPException(429, "Too many login attempts")

    db = get_db()
    checker = PermissionChecker(db)
    user_dict = await checker.authenticate(username, password)
    if user_dict is None:
        auth.record_failed_login(client_ip)
        raise HTTPException(401, "Invalid credentials")

    auth.clear_login_attempts(client_ip)
    session = await build_session_data(
        user_dict["id"], db, username=user_dict["username"]
    )
    resp = Response.json(
        {
            "message": "Logged in",
            "username": user_dict["username"],
            "groups": session["groups"],
            "is_staff": session["is_staff"],
        }
    )
    auth.login(resp, session)
    return resp


@app.post("/auth/logout")
async def logout(request):
    """Logout and destroy session."""
    resp = Response.json({"message": "Logged out"})
    if request.session_id:
        auth.logout(resp, request.session_id)
    return resp


@app.post("/auth/register")
async def register(request):
    """Register a new user."""
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or len(password) < 8:
        raise HTTPException(400, "Username required, password min 8 chars")

    existing = await User.objects.filter(username=username).first()
    if existing:
        raise HTTPException(409, "Username taken")

    db = get_db()
    checker = PermissionChecker(db)
    user = await checker.create_user(username, password)

    # Add new users to "reader" group by default
    reader_group = await checker.get_group_by_name("reader")
    if reader_group is not None:
        await checker.add_user_to_group(user.id, reader_group.id)

    session = await build_session_data(user.id, db, username=username)

    resp = Response.json(
        {"message": "Registered", "username": username, "groups": session["groups"]},
        status=201,
    )
    auth.login(resp, session)
    return resp


# ---------------------------------------------------------------------------
# Admin endpoints (API key protected)
# ---------------------------------------------------------------------------


@app.get("/api/admin/stats")
@guard(Require.api_key())
async def admin_stats(request):
    """Admin statistics via ORM. Requires X-API-Key header."""
    books = await Book.objects.count()
    authors = await Author.objects.count()
    reviews = await Review.objects.count()
    users = await User.objects.count()
    return Response.json(
        {
            "books": books,
            "authors": authors,
            "reviews": reviews,
            "users": users,
        }
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Performance profiling endpoints
# ---------------------------------------------------------------------------


@app.get("/debug/performance")
async def perf_dashboard(request):
    """Performance dashboard HTML — query tracking, slow queries, N+1 detection."""
    return _perf._dashboard_response()


@app.get("/debug/performance/json")
async def perf_json(request):
    """Performance stats as JSON."""
    return Response.json(_perf.get_stats())


@app.get("/debug/performance/flamegraph")
async def flamegraph(request):
    """Collapsed stack format for flame graph visualization (speedscope compatible)."""
    data = get_store().get_flame_graph()
    return Response(body=data.encode(), content_type="text/plain")


@app.get("/debug/performance/profiles")
async def recent_profiles(request):
    """Recent request profiles as JSON."""
    store = get_store()
    slowest = store.get_slowest(20)
    return Response.json(
        {
            "profiles": [p.to_dict() for p in slowest],
            "total_stored": len(store.get_all()),
        }
    )


@app.get("/debug/pool/json")
async def pool_stats_json(request):
    """Native pg.zig pool stats snapshot.

    Exposes the contention counters added in task #193 (waiters,
    max_waiters, wait_count, wait_total_ns, wait_max_ns, acquire_count,
    timeout_count) alongside the core size/available/in_use/missing
    numbers. Intended for sampling during wrk runs to build a
    queue-depth histogram.
    """
    return Response.json(get_db().pool_stats())


# Health check
# ---------------------------------------------------------------------------


app.mount_health()
app.mount_version()


# ---------------------------------------------------------------------------
# HyperAdmin model registration
# ---------------------------------------------------------------------------


async def _publish_selected(adm, config, selected_ids, request):
    """Bulk publish selected books.

    Single `filter(id__in=...).update(...)` call instead of N per-row
    roundtrips. Collapses N DB ops into 1 regardless of selection size.
    """
    ids = [int(pk) for pk in selected_ids]
    await Book.objects.filter(id__in=ids).update(published=True)
    return f"Published {len(ids)} book(s)"


async def _feature_selected(adm, config, selected_ids, request):
    """Bulk mark selected books as featured — single roundtrip."""
    ids = [int(pk) for pk in selected_ids]
    await Book.objects.filter(id__in=ids).update(featured=True)
    return f"Featured {len(ids)} book(s)"


async def _unfeature_selected(adm, config, selected_ids, request):
    """Remove featured status from selected books — single roundtrip."""
    ids = [int(pk) for pk in selected_ids]
    await Book.objects.filter(id__in=ids).update(featured=False)
    return f"Unfeatured {len(ids)} book(s)"


admin.register(
    Author,
    list_display=["id", "name", "website"],
    search_fields=["name"],
    fieldsets=[
        Fieldset(title="Author Info", fields=["name", "bio", "website"]),
    ],
)

admin.register(
    Category,
    list_display=["id", "name", "slug"],
    search_fields=["name", "slug"],
    fieldsets=[
        Fieldset(title="Category", fields=["name", "slug", "description"]),
    ],
)

admin.register(
    Book,
    list_display=[
        "id",
        "title",
        "isbn",
        "price",
        "published",
        "featured",
        "author_id",
        "category_id",
    ],
    search_fields=["title", "isbn"],
    list_filter=["published", "featured", "category_id"],
    ordering="-created_at",
    fieldsets=[
        Fieldset(title="Book Info", fields=["title", "isbn", "description"]),
        Fieldset(title="Pricing & Pages", fields=["price", "pages"]),
        Fieldset(title="Status", fields=["published", "featured"]),
        Fieldset(title="Relations", fields=["author_id", "category_id"]),
        Fieldset(
            title="Timestamps",
            fields=["created_at", "updated_at"],
            classes=["collapse"],
        ),
    ],
    actions=[
        Action(name="publish", label="Publish selected", handler=_publish_selected),
        Action(name="feature", label="Mark featured", handler=_feature_selected),
        Action(name="unfeature", label="Remove featured", handler=_unfeature_selected),
    ],
)

admin.register(
    Review,
    list_display=["id", "book_id", "reviewer_name", "rating", "created_at"],
    search_fields=["reviewer_name", "comment"],
    list_filter=["rating"],
    ordering="-created_at",
    fieldsets=[
        Fieldset(title="Review", fields=["book_id", "reviewer_name", "rating"]),
        Fieldset(title="Comment", fields=["comment"]),
        Fieldset(title="Metadata", fields=["created_at"], classes=["collapse"]),
    ],
)


# User management is handled by HyperAdmin's built-in User/Group/Permission CRUD.
# No separate registration needed — hyper_users is auto-managed.


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8900)
