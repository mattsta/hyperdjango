# REST API Framework

HyperDjango's REST module provides a DRF-equivalent REST API layer natively integrated with the Model, QuerySet, Serializer, Router, and auth systems. Zero Django dependency.

```python
from hyperdjango.rest import (
    ModelSerializer,
    ModelViewSet,
    ReadOnlyModelViewSet,
    APIRouter,
    PageNumberPagination,
    LimitOffsetPagination,
    CursorPagination,
    ServerCursorPagination,
    FieldFilter,
    SearchFilter,
    OrderingFilter,
    IsAuthenticated,
    IsAdminUser,
    IsAuthenticatedOrReadOnly,
    AllowAny,
    BasePermission,
    ModelPermission,
    ObjectPermission,
    AnonRateThrottle,
    UserRateThrottle,
    ScopedRateThrottle,
    DatabaseThrottle,
    DatabaseAnonThrottle,
    DatabaseUserThrottle,
    DatabaseScopedThrottle,
    SessionAuthentication,
    APIKeyAuthentication,
    TokenAuthentication,
    JSONParser,
    FormParser,
    MultiPartParser,
    JSONRenderer,
    CSVRenderer,
    URLPathVersioning,
    HeaderVersioning,
    QueryParamVersioning,
    PrimaryKeyRelatedField,
    SlugRelatedField,
    SerializerMethodField,
    SimpleMetadata,
    DateTimeField,
    DateField,
    TimeField,
    ChoiceField,
    MultipleChoiceField,
    UUIDField,
    DecimalField,
    EmailField,
    URLField,
    IPAddressField,
    ReadOnlyField,
    HiddenField,
    CurrentUserDefault,
    FileUploadField,
    ImageUploadField,
    CacheableMixin,
    MeteringMixin,
    BulkCreateMixin,
    BulkUpdateMixin,
    BulkDestroyMixin,
    BulkModelViewSet,
    NestedRouter,
    NestedViewSetMixin,
    GenericAPIView,
    CreateAPIView,
    ListAPIView,
    RetrieveAPIView,
    DestroyAPIView,
    UpdateAPIView,
    ListCreateAPIView,
    RetrieveUpdateAPIView,
    RetrieveUpdateDestroyAPIView,
    RetrieveDestroyAPIView,
    action,
)
```

---

## Quick Start

```python
from hyperdjango import HyperApp, Model, Field
from hyperdjango.rest import (
    ModelSerializer,
    ModelViewSet,
    APIRouter,
    PageNumberPagination,
)
from hyperdjango.openapi import mount_docs

app = HyperApp(title="Blog API", database="postgres://...")


class Post(Model):
    class Meta:
        table = "posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    content: str = Field()
    status: str = Field(default="draft")


class PostSerializer(ModelSerializer):
    class Meta:
        model = Post
        fields = "__all__"
        read_only_fields = ["id"]


class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    model = Post
    pagination_class = PageNumberPagination


router = APIRouter(prefix="/api/v1")
router.register("posts", PostViewSet)
router.mount(app.router)

mount_docs(app)  # GET /docs, GET /openapi.json
app.run()
```

This generates:

| Method | URL                  | Action           |
| ------ | -------------------- | ---------------- |
| GET    | `/api/v1/posts`      | List (paginated) |
| POST   | `/api/v1/posts`      | Create           |
| GET    | `/api/v1/posts/{id}` | Retrieve         |
| PUT    | `/api/v1/posts/{id}` | Full update      |
| PATCH  | `/api/v1/posts/{id}` | Partial update   |
| DELETE | `/api/v1/posts/{id}` | Delete           |

---

## ModelSerializer

Auto-generates serializer fields from your Model's `_meta.fields` and `__annotations__`.

```python
class UserSerializer(ModelSerializer):
    class Meta:
        model = User
        fields = "__all__"  # or ["id", "name", "email"]
        read_only_fields = ["id"]  # auto fields are auto-read-only
        extra_kwargs = {
            "name": {"min_length": 2, "max_length": 100},
            "email": {"required": True},
        }
        depth = 0  # 0=flat PKs, 1+=nested serializers for FKs
```

### Meta Options

| Option             | Type                       | Description                                                                                         |
| ------------------ | -------------------------- | --------------------------------------------------------------------------------------------------- |
| `model`            | `type`                     | The Model class to introspect                                                                       |
| `fields`           | `list[str]` or `"__all__"` | Which fields to include                                                                             |
| `read_only_fields` | `list[str]`                | Fields that cannot be written (auto fields are always read-only)                                    |
| `extra_kwargs`     | `dict[str, dict]`          | Per-field overrides (min_length, max_length, required, etc.)                                        |
| `depth`            | `int`                      | Auto-generate nested serializers for FK fields (0=flat PKs, 1=one level nested, 2=two levels, etc.) |

> **Mass-assignment guard.** A model field declared `Field(editable=False)` is
> **always** read-only in a `ModelSerializer` — even under `fields="__all__"` and
> regardless of `read_only_fields`. Mark security-sensitive columns this way so a
> request body can never set them. The built-in `User` model already marks
> `is_staff`, `is_superuser`, `is_active`, `last_login`, and `password_hash`
> `editable=False`; privileged code sets them by assigning the instance directly.

### Explicit Field Overrides

Explicitly declared fields take precedence over auto-generated ones:

```python
class UserSerializer(ModelSerializer):
    full_name: str = SerializerField(read_only=True, source="compute_name")

    class Meta:
        model = User
        fields = ["id", "full_name", "email"]

    def compute_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"
```

### Nested Serializers (depth)

When `depth > 0`, ModelSerializer auto-generates nested serializers for FK fields:

```python
class PostSerializer(ModelSerializer):
    class Meta:
        model = Post
        fields = "__all__"
        read_only_fields = ["id"]
        depth = 1  # author FK becomes a nested UserSerializer


# Output: {"id": 1, "title": "Hello", "author": {"id": 42, "name": "Alice"}}
```

### Explicit Nested Serializers

For fine-grained control, declare nested serializer instances directly. These are preserved and not overridden by `depth`:

```python
class PostSerializer(ModelSerializer):
    author = UserSerializer(read_only=True)

    class Meta:
        model = Post
        fields = "__all__"
```

### Writable Nested Serializers

Nested dicts in validated_data are handled automatically during create/update:

```python
# Create: nested dicts create related objects first
serializer = PostSerializer(input_data={"title": "X", "author": {"name": "Alice"}})
if serializer.is_valid():
    post = await serializer.create(serializer.validated_data)
    # Creates User(name="Alice") first, then Post(title="X", author=user.id)

# Update: nested dicts update existing related objects
serializer = PostSerializer(input_data={"author": {"name": "Bob"}}, partial=True)
if serializer.is_valid():
    post = await serializer.update(existing_post, serializer.validated_data)
```

### get_field_names()

Returns the cached list of all serializer field names:

```python
UserSerializer.get_field_names()  # ["id", "name", "email", "created_at"]
```

### Field Resolution Caching

ModelSerializer pre-computes field metadata at class creation time for O(1) lookups during serialization:

- `_is_identity_serializer` -- True when all fields map 1:1 to DB columns (no computed/method/nested fields)
- `_read_only_fields` -- frozenset of read-only field names
- `_write_fields` -- frozenset of writable field names
- `_column_field_map` -- DB column name to serializer field name mapping

---

## ViewSet and GenericAPIView

Two base classes for building API endpoints, each with a different dispatch model:

### ViewSet

Groups related API actions into a single class. Actions are mapped to HTTP methods at route registration time via `as_view(actions={"get": "list", "post": "create"})`.

```python
class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    model = Post
    permission_classes = [IsAuthenticated]
    pagination_class = PageNumberPagination
    filter_backends = [FieldFilter, SearchFilter, OrderingFilter]
    filterset_fields = ["status", "author_id"]
    search_fields = ["title", "content"]
    ordering_fields = ["created_at", "title"]
    ordering = ["-created_at"]
    lookup_field = "id"  # default
```

**Dispatch flow** (executed in order for every request):

1. Versioning -- `versioning_class.determine_version(request)` sets `request.version`
2. Authentication -- `authentication_classes` tried in order; first success sets `request.user`
3. Permissions -- all `permission_classes` must pass
4. Throttling -- all `throttle_classes` must allow
5. OPTIONS handling -- returns `SimpleMetadata` if method is OPTIONS
6. Handler dispatch -- calls the mapped action method
7. Input validation -- if `@action` declares `input_serializer`, auto-validates before handler

**Class attributes:**

| Attribute                | Default                                     | Description                               |
| ------------------------ | ------------------------------------------- | ----------------------------------------- |
| `serializer_class`       | `None`                                      | Serializer class for read/write           |
| `model`                  | `None`                                      | Model class (auto-creates queryset)       |
| `queryset`               | `None`                                      | Explicit queryset (overrides model)       |
| `lookup_field`           | `"id"`                                      | Field name for detail lookups             |
| `lookup_url_kwarg`       | `None`                                      | URL kwarg name (defaults to lookup_field) |
| `permission_classes`     | `(AllowAny,)`                               | Permission check classes                  |
| `authentication_classes` | `()`                                        | Authentication classes                    |
| `throttle_classes`       | `()`                                        | Rate limiting classes                     |
| `pagination_class`       | `None`                                      | Pagination strategy                       |
| `filter_backends`        | `()`                                        | Filter backend classes                    |
| `parser_classes`         | `(JSONParser, FormParser, MultiPartParser)` | Request body parsers                      |
| `versioning_class`       | `None`                                      | API versioning strategy                   |
| `metadata_class`         | `SimpleMetadata`                            | OPTIONS metadata generator                |
| `renderer_classes`       | `(JSONRenderer,)`                           | Response renderers                        |
| `filterset_fields`       | `()`                                        | Fields allowed for FieldFilter            |
| `search_fields`          | `()`                                        | Fields for SearchFilter                   |
| `ordering_fields`        | `()`                                        | Fields allowed for OrderingFilter         |
| `ordering`               | `()`                                        | Default ordering                          |
| `use_native_json`        | `False`                                     | Enable Zig native JSON fast path          |

**Overriding methods:**

```python
class PostViewSet(ModelViewSet):
    def get_queryset(self):
        # Filter to user's own posts
        return Post.objects.filter(author_id=self.request.user["id"])

    def get_serializer_class(self):
        if self.action == "list":
            return PostListSerializer
        return PostDetailSerializer
```

### GenericAPIView

Dispatches by HTTP method name (get/post/put/patch/delete) rather than action mapping. Identical capabilities (serializer, queryset, permissions, auth, throttling, pagination, filtering) but simpler when you need a single endpoint rather than a full resource.

```python
class BookList(ListAPIView):
    serializer_class = BookSerializer
    model = Book
    pagination_class = PageNumberPagination
```

---

## CRUD Mixins

Each mixin adds a single action to your ViewSet. Compose them to build exactly the interface you need.

### ListMixin

Adds `list()` -- paginated, filtered list of objects.

```python
async def list(self, request, **kwargs) -> Response:
    # 1. Apply filter backends to queryset
    # 2. If pagination_class set, paginate
    # 3. Serialize with get_serializer(many=True)
    # 4. Apply HTTP cache headers if CacheableMixin is present
```

### CreateMixin

Adds `create()` -- validate input and create object.

```python
async def create(self, request, **kwargs) -> Response:
    # 1. Parse request body
    # 2. Validate with serializer
    # 3. Call perform_create(serializer)
    # 4. Return 201 with serialized instance
```

**Hook:** `perform_create(serializer)` -- override to inject logic (e.g., set owner from request.user).

### RetrieveMixin

Adds `retrieve()` -- get single object by lookup field.

```python
async def retrieve(self, request, **kwargs) -> Response:
    # 1. get_object() (lookup by ID, check object permissions)
    # 2. Serialize
    # 3. Apply HTTP cache headers if CacheableMixin present
```

### UpdateMixin

Adds `update()` and `partial_update()`.

- `PUT` calls `update()` with full replacement
- `PATCH` calls `partial_update()` which delegates to `update()` with `partial=True`

**Hook:** `perform_update(serializer, instance)` -- override to add logging, side effects, etc.

### DestroyMixin

Adds `destroy()` -- delete object and return 204.

**Hook:** `perform_destroy(instance)` -- override for soft delete, audit logging, etc.

### perform\_\* Hooks

Override these methods to inject logic without overriding entire actions:

```python
class PostViewSet(ModelViewSet):
    async def perform_create(self, serializer):
        data = dict(serializer.validated_data, author_id=self.request.user["id"])
        return await serializer.create(data)

    async def perform_update(self, serializer, instance):
        logger.info(f"Updating post {instance.id}")
        return await serializer.update(instance, serializer.validated_data)

    async def perform_destroy(self, instance):
        instance.is_deleted = True
        await instance.save()
```

---

## Shortcut Views

Pre-composed GenericAPIView subclasses for common endpoint patterns. Use when you need a single URL endpoint rather than a full ViewSet resource.

| Class                          | Methods                 | Mixins                                   | Use Case                                    |
| ------------------------------ | ----------------------- | ---------------------------------------- | ------------------------------------------- |
| `CreateAPIView`                | POST                    | CreateMixin                              | Write-only endpoint (e.g., event ingestion) |
| `ListAPIView`                  | GET                     | ListMixin                                | Read-only list (e.g., public catalog)       |
| `RetrieveAPIView`              | GET                     | RetrieveMixin                            | Single object read (e.g., profile page)     |
| `DestroyAPIView`               | DELETE                  | DestroyMixin                             | Delete-only endpoint                        |
| `UpdateAPIView`                | PUT, PATCH              | UpdateMixin                              | Update-only endpoint                        |
| `ListCreateAPIView`            | GET, POST               | ListMixin, CreateMixin                   | Collection endpoint                         |
| `RetrieveUpdateAPIView`        | GET, PUT, PATCH         | RetrieveMixin, UpdateMixin               | Detail read+write                           |
| `RetrieveUpdateDestroyAPIView` | GET, PUT, PATCH, DELETE | RetrieveMixin, UpdateMixin, DestroyMixin | Full detail endpoint                        |
| `RetrieveDestroyAPIView`       | GET, DELETE             | RetrieveMixin, DestroyMixin              | Read + delete only                          |

**When to use ViewSet vs shortcut views:**

- **ViewSet** -- when you need a full CRUD resource registered on an `APIRouter`, with `@action` custom endpoints, and auto-generated OpenAPI docs. One class handles all 6 CRUD operations plus custom actions.
- **Shortcut views** -- when you need a single URL with specific HTTP methods, outside the router/resource pattern. Simpler to reason about for one-off endpoints.

```python
# Shortcut view: single endpoint, explicit HTTP method routing
class BookList(ListCreateAPIView):
    serializer_class = BookSerializer
    model = Book
    pagination_class = PageNumberPagination


app.router.add("GET", "/books", BookList.as_view())
app.router.add("POST", "/books", BookList.as_view())
```

---

## Bulk Operations

Three mixins for batch create, update, and delete. `BulkModelViewSet` composes all three with full CRUD.

### BulkCreateMixin

```python
# POST /items/bulk
# Body: [{"name": "a"}, {"name": "b"}]
# Returns: 201 with list of created items
```

### BulkUpdateMixin

```python
# PATCH /items/bulk
# Body: [{"id": 1, "name": "new"}, {"id": 2, "name": "updated"}]
# Returns: 200 with list of updated items
```

### BulkDestroyMixin

```python
# DELETE /items/bulk
# Body: [1, 2, 3] or [{"id": 1}, {"id": 2}]
# Returns: 200 with {"deleted": [1, 2, 3]}
```

### BulkModelViewSet

Full CRUD plus bulk operations:

```python
class ItemViewSet(BulkModelViewSet):
    serializer_class = ItemSerializer
    model = Item
    max_bulk_size = 200  # default: 100
```

**Configuration:**

| Attribute       | Default | Description                    |
| --------------- | ------- | ------------------------------ |
| `max_bulk_size` | `100`   | Maximum items per bulk request |

**Error format:** partial failures return 400 with per-index errors:

```json
{
  "detail": "Validation failed for some items.",
  "errors": {
    "0": { "name": ["This field is required"] },
    "2": { "email": ["Invalid email format"] }
  }
}
```

The router auto-generates `/bulk` routes when bulk mixins are detected:

| Method | URL           | Action       |
| ------ | ------------- | ------------ |
| POST   | `/items/bulk` | bulk_create  |
| PATCH  | `/items/bulk` | bulk_update  |
| DELETE | `/items/bulk` | bulk_destroy |

---

## APIRouter

Auto-registers ViewSets into URL patterns with a native Zig radix trie router.

```python
router = APIRouter(prefix="/api/v1")
router.register("posts", PostViewSet)
router.register("users", UserViewSet, basename="user")
router.register("comments", CommentViewSet)

# Mount on app router
router.mount(app.router, namespace="api")
```

### register()

```python
router.register(prefix, viewset_class, basename=None)
```

- `prefix` -- URL prefix without slashes (e.g., `"posts"`, `"users"`)
- `viewset_class` -- the ViewSet class to register
- `basename` -- base name for route names (defaults to model table name, singularized)

### mount()

```python
router.mount(app_router, namespace=None)
```

Mounts all registered ViewSets onto a `Router` instance. Optional `namespace` for route name prefixing.

### get_urls()

Returns a list of `(method, pattern, handler, name)` tuples for all registered ViewSets, including an API root view at `/`.

### Auto-Generated Routes

For a `ModelViewSet` registered as `router.register("posts", PostViewSet)`:

| Method | Pattern           | Action         | Route Name            |
| ------ | ----------------- | -------------- | --------------------- |
| GET    | `/posts`          | list           | `post-list`           |
| POST   | `/posts`          | create         | `post-create`         |
| GET    | `/posts/{id:int}` | retrieve       | `post-retrieve`       |
| PUT    | `/posts/{id:int}` | update         | `post-update`         |
| PATCH  | `/posts/{id:int}` | partial_update | `post-partial_update` |
| DELETE | `/posts/{id:int}` | destroy        | `post-destroy`        |

Custom `@action` methods generate additional routes:

- Detail action: `/posts/{id:int}/{url_path}`
- List action: `/posts/{url_path}`

### API Root View

Auto-generated discovery endpoint at the prefix root:

```
GET /api/v1/
```

```json
{
  "post": "/api/v1/posts",
  "user": "/api/v1/users",
  "comment": "/api/v1/comments"
}
```

### get_schema()

Generates an OpenAPI 3.1 specification from all registered ViewSets. See the [OpenAPI](#openapi) section.

---

## Nested Routers

`NestedRouter` generates parent/child URL structures for related resources.

```python
router = APIRouter(prefix="/api/v1")
router.register("posts", PostViewSet)

comments_router = NestedRouter(router, "posts", lookup="post_id")
comments_router.register("comments", CommentViewSet)

router.mount(app.router, namespace="api")
comments_router.mount(app.router, namespace="api")
```

This generates:

| Method | Pattern                                         | Action         |
| ------ | ----------------------------------------------- | -------------- |
| GET    | `/api/v1/posts/{post_id:int}/comments`          | list           |
| POST   | `/api/v1/posts/{post_id:int}/comments`          | create         |
| GET    | `/api/v1/posts/{post_id:int}/comments/{id:int}` | retrieve       |
| PUT    | `/api/v1/posts/{post_id:int}/comments/{id:int}` | update         |
| PATCH  | `/api/v1/posts/{post_id:int}/comments/{id:int}` | partial_update |
| DELETE | `/api/v1/posts/{post_id:int}/comments/{id:int}` | destroy        |

### NestedViewSetMixin

Auto-filters the queryset by the parent resource FK:

```python
class CommentViewSet(NestedViewSetMixin, ModelViewSet):
    parent_lookup_field = "post_id"  # model FK field name
    serializer_class = CommentSerializer
    model = Comment
```

When a request hits `/posts/42/comments`, the mixin adds `.filter(post_id=42)` to the queryset automatically.

### Multi-Level Nesting

Chain multiple NestedRouters for deeper hierarchies:

```python
router = APIRouter(prefix="/api/v1")
router.register("organizations", OrgViewSet)

teams_router = NestedRouter(router, "organizations", lookup="org_id")
teams_router.register("teams", TeamViewSet)

members_router = NestedRouter(teams_router, "teams", lookup="team_id")
members_router.register("members", MemberViewSet)

# GET /api/v1/organizations/{org_id}/teams/{team_id}/members
```

### Configuration

| Attribute       | Default    | Description                                          |
| --------------- | ---------- | ---------------------------------------------------- |
| `parent_prefix` | (required) | Parent resource URL prefix                           |
| `lookup`        | (required) | URL parameter name for parent ID (e.g., `"post_id"`) |
| `lookup_type`   | `"int"`    | URL parameter type hint (`"int"`, `"str"`)           |

---

## Pagination

Four pagination strategies for different use cases.

### Comparison Table

| Property        | PageNumber            | LimitOffset          | Cursor                      | ServerCursor                    |
| --------------- | --------------------- | -------------------- | --------------------------- | ------------------------------- |
| Query params    | `?page=N`             | `?limit=N&offset=N`  | `?cursor=<token>`           | `?server_cursor=<token>`        |
| COUNT query     | Yes                   | Yes                  | No                          | No                              |
| State           | Stateless             | Stateless            | Stateless (in URL token)    | Stateful (pinned DB connection) |
| Page cost       | O(n) offset scan      | O(n) offset scan     | O(log n) index seek         | O(1) FETCH from position        |
| Server affinity | Not needed            | Not needed           | Not needed                  | Required                        |
| HMAC signed     | N/A                   | N/A                  | Yes (128-bit)               | Yes (128-bit)                   |
| User-bound      | No                    | No                   | No                          | Yes (user_id or IP)             |
| Per-user limit  | No                    | No                   | No                          | Yes (max 5 default)             |
| Best for        | Small/medium datasets | Simple offset paging | Public APIs, large datasets | Data export, premium API        |

### PageNumberPagination

Standard page-number pagination with count.

```python
class PostViewSet(ModelViewSet):
    pagination_class = PageNumberPagination
```

```
GET /posts?page=2&page_size=10
```

```json
{
    "count": 150,
    "next": "/posts?page=3&page_size=10",
    "previous": "/posts?page=1&page_size=10",
    "results": [...]
}
```

**Configuration:**

| Attribute               | Default       | Description                          |
| ----------------------- | ------------- | ------------------------------------ |
| `page_size`             | 25            | Default items per page               |
| `max_page_size`         | 100           | Maximum allowed page_size            |
| `page_query_param`      | `"page"`      | Query parameter name for page number |
| `page_size_query_param` | `"page_size"` | Query parameter name for page size   |

### LimitOffsetPagination

Classic limit/offset pagination.

```
GET /posts?limit=20&offset=40
```

```json
{
    "count": 150,
    "next": "/posts?limit=20&offset=60",
    "previous": "/posts?limit=20&offset=20",
    "results": [...]
}
```

**Configuration:**

| Attribute            | Default    | Description            |
| -------------------- | ---------- | ---------------------- |
| `default_limit`      | 25         | Default items per page |
| `max_limit`          | 100        | Maximum allowed limit  |
| `limit_query_param`  | `"limit"`  | Query parameter name   |
| `offset_query_param` | `"offset"` | Query parameter name   |

### CursorPagination (Keyset)

Efficient keyset pagination for large datasets. No COUNT query, no OFFSET scanning. Uses HMAC-signed cursor tokens.

```python
class PostViewSet(ModelViewSet):
    pagination_class = CursorPagination
```

```
GET /posts                              -> first page + signed cursor token
GET /posts?cursor=<hmac-signed-token>   -> next page
```

```json
{
    "next": "/posts?cursor=<signed-token>",
    "previous": "/posts?cursor=<signed-token>",
    "results": [...]
}
```

**How it works:**

1. First page: `SELECT * FROM posts ORDER BY id DESC LIMIT 26` (fetch N+1 to detect next page)
2. Encode the last row's `id` value: `base64(hmac_sign("next:int:42"))`
3. Next request: decode cursor, verify HMAC, apply `WHERE id < 42 ORDER BY id DESC LIMIT 26`
4. No COUNT query. No OFFSET. No server state.

**Security:**

- HMAC-SHA256 signed -- users cannot forge, tamper, or inject values
- Type-tagged -- `direction:type:value:signature` format; type coercion happens AFTER signature verification
- Parameterized -- cursor values go through ORM `.filter()` with `$N` placeholders (defense in depth)
- Tamper detection -- `hmac.compare_digest()` for constant-time comparison; tampered cursors silently reset to page 1
- Stateless -- works across all servers as long as `HYPER_SECRET_KEY` is the same

**Supported ordering field types:** int, float, datetime, date, Decimal, UUID, string.

**Configuration:**

| Attribute            | Default    | Description                                |
| -------------------- | ---------- | ------------------------------------------ |
| `page_size`          | 25         | Items per page                             |
| `ordering`           | `"-id"`    | Ordering field (prefix `-` for descending) |
| `cursor_query_param` | `"cursor"` | Query parameter name                       |

**Environment variables:**

| Variable              | Description                                                                         |
| --------------------- | ----------------------------------------------------------------------------------- |
| `HYPER_SECRET_KEY`    | Shared secret for HMAC signing (16+ chars). Must be identical across cluster nodes. |
| `HYPER_CURSOR_SECRET` | Optional override for cursor-specific signing (falls back to HYPER_SECRET_KEY)      |

### ServerCursorPagination (DECLARE CURSOR)

Real PostgreSQL `DECLARE CURSOR / FETCH` pagination. Pins a pool connection for the cursor's lifetime. Each FETCH is O(1).

```python
class LargeExportViewSet(ModelViewSet):
    pagination_class = ServerCursorPagination

    def get_queryset(self):
        return MyModel.objects.using("replica")  # Pin to read replica
```

```
GET /api/exports                                -> DECLARE CURSOR, FETCH first page
GET /api/exports?server_cursor=<cursor_id>      -> FETCH next page from same cursor
```

```json
{
    "cursor_id": "<signed-token>",
    "results": [...],
    "exhausted": false
}
```

**How it works:**

1. First request: `DECLARE cursor_name CURSOR FOR <sql>`, `FETCH 100 FROM cursor_name`, return HMAC-signed cursor_id
2. Subsequent requests: `FETCH 100 FROM cursor_name` via the pinned connection (O(1), no re-query)
3. Cursor auto-expires after idle timeout (5 min) or max lifetime (30 min)
4. On exhaustion or expiry: `CLOSE cursor_name`, `COMMIT`, release connection back to pool

**Security:**

- User-bound -- cursor_id embeds user_id (or IP for anonymous), HMAC-signed
- Anonymous isolation -- anon users identified by IP address, not shared namespace
- Per-user limit -- max 5 concurrent cursors per user (configurable); thread-safe via lock-protected registry
- Atomic check-and-register -- per-user limit + cursor registration under single lock (no TOCTOU race)
- Idle timeout -- 5 minutes of inactivity auto-closes cursor
- Max lifetime -- 30 minutes absolute auto-closes cursor

**Configuration:**

| Attribute              | Default           | Description                     |
| ---------------------- | ----------------- | ------------------------------- |
| `page_size`            | 100               | Items per FETCH                 |
| `max_idle_seconds`     | 300               | Idle timeout (5 min)            |
| `max_lifetime_seconds` | 1800              | Absolute lifetime (30 min)      |
| `max_per_user`         | 5                 | Max concurrent cursors per user |
| `cursor_query_param`   | `"server_cursor"` | Query parameter name            |

**Cleanup:** call `await cleanup_expired_server_cursors()` periodically (e.g., every minute via background task) to close expired cursors and release pool connections.

**Distributed systems:** server affinity required. Use `ConsistentHashRing.get_node(cursor_id)` for deterministic routing. Wrong server returns 404 "Cursor not found", and the client restarts the query (graceful degradation).

---

## Filtering

Chain multiple filter backends. Each receives the queryset from the previous.

```python
class PostViewSet(ModelViewSet):
    filter_backends = [FieldFilter, SearchFilter, OrderingFilter]
```

### FieldFilter

Filter by exact values and lookups from query parameters:

```
GET /posts?status=active              -> qs.filter(status="active")
GET /posts?price__gte=10&price__lte=50 -> qs.filter(price__gte=10, price__lte=50)
GET /posts?status__in=active,pending   -> qs.filter(status__in=["active", "pending"])
GET /posts?author__isnull=true         -> qs.filter(author__isnull=True)
```

Only fields listed in `filterset_fields` are allowed.

**Supported lookups (17):**

| Lookup           | Description                   |
| ---------------- | ----------------------------- |
| (none) / `exact` | Exact match                   |
| `iexact`         | Case-insensitive exact match  |
| `gt`             | Greater than                  |
| `gte`            | Greater than or equal         |
| `lt`             | Less than                     |
| `lte`            | Less than or equal            |
| `contains`       | Case-sensitive contains       |
| `icontains`      | Case-insensitive contains     |
| `startswith`     | Case-sensitive starts with    |
| `istartswith`    | Case-insensitive starts with  |
| `endswith`       | Case-sensitive ends with      |
| `iendswith`      | Case-insensitive ends with    |
| `in`             | Value in comma-separated list |
| `isnull`         | Is null check (true/false)    |
| `range`          | Within range                  |

### SearchFilter

Multi-term, multi-field text search with AND/OR semantics and prefix operators:

```
GET /posts?search=django rest          -> (title ILIKE '%django%') AND (title ILIKE '%rest%')
GET /posts?search="exact phrase"       -> (title ILIKE '%exact phrase%')
```

Configure with `search_fields`. Prefix operators control matching behavior:

| Prefix | Behavior                 | SQL              |
| ------ | ------------------------ | ---------------- |
| (none) | Contains (default)       | `ILIKE '%term%'` |
| `^`    | Starts with              | `ILIKE 'term%'`  |
| `=`    | Exact (case-insensitive) | `ILIKE 'term'`   |
| `$`    | Regex (case-insensitive) | `~* 'term'`      |

```python
class PostViewSet(ModelViewSet):
    search_fields = ["title", "^slug", "=status", "$content"]
```

**Search term handling:**

- Multiple terms are AND-ed; each term is OR-ed across fields
- Quoted phrases (`"exact phrase"`) treated as single terms via `_smart_split`
- ILIKE metacharacters (`%`, `_`) are escaped via `_escape_like()`
- Search terms capped at 200 characters
- Regex patterns (`$` prefix) are sanitized: quantifier stacking (`a++`, `a**`, `a{1000}`) is blocked, patterns capped at 100 chars
- All values are SQL-parameterized (no injection possible)

### OrderingFilter

Dynamic ordering from query parameters:

```
GET /posts?ordering=-created_at,title -> qs.order_by("-created_at", "title")
```

Only fields in `ordering_fields` are allowed. `ordering` sets the default.

```python
class PostViewSet(ModelViewSet):
    ordering_fields = ["created_at", "title", "id"]
    ordering = ["-created_at"]  # default
```

### FullTextSearchFilter

PostgreSQL-native full-text search using `tsvector` and `tsquery`. Unlike `SearchFilter`
(which uses `ILIKE`), this uses the `@@` match operator for language-aware stemming,
ranking, and index support.

```python
from hyperdjango.rest import FullTextSearchFilter


class ArticleViewSet(ModelViewSet):
    serializer_class = ArticleSerializer
    model = Article
    filter_backends = [FullTextSearchFilter]
    search_fields = ["title", "body"]
    search_config = "english"  # PostgreSQL text search configuration
    search_type = "websearch"  # plain, phrase, raw, websearch
```

```
GET /articles?search=web framework
# Generates: WHERE (to_tsvector('english', COALESCE("title", '')) ||
#   to_tsvector('english', COALESCE("body", ''))) @@
#   websearch_to_tsquery('english', $1)
```

| Attribute       | Default       | Description                                             |
| --------------- | ------------- | ------------------------------------------------------- |
| `search_param`  | `"search"`    | Query parameter name                                    |
| `search_config` | `"english"`   | PostgreSQL text search config (set on ViewSet)          |
| `search_type`   | `"websearch"` | tsquery function: `plain`, `phrase`, `raw`, `websearch` |

Search type mapping:

- `plain` -- `plainto_tsquery` (splits on spaces, AND semantics)
- `phrase` -- `phraseto_tsquery` (proximity/phrase search)
- `raw` -- `to_tsquery` (raw tsquery syntax with `&`, `|`, `!`)
- `websearch` -- `websearch_to_tsquery` (Google-style: quotes, `-` exclusion, `OR`)

For best performance, create a GIN index on the tsvector expression:

```sql
CREATE INDEX idx_articles_search ON articles
  USING GIN (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(body, '')));
```

### SearchRankOrderingFilter

Orders results by full-text search relevance using `ts_rank()`. Typically used
alongside `FullTextSearchFilter` to sort results by match quality.

```python
from hyperdjango.rest import FullTextSearchFilter, SearchRankOrderingFilter


class ArticleViewSet(ModelViewSet):
    serializer_class = ArticleSerializer
    model = Article
    filter_backends = [FullTextSearchFilter, SearchRankOrderingFilter]
    search_fields = ["title", "body"]
    search_config = "english"
    search_type = "websearch"
```

```
GET /articles?search=web framework
# 1. FullTextSearchFilter adds WHERE ... @@ websearch_to_tsquery(...)
# 2. SearchRankOrderingFilter adds ORDER BY ts_rank(...) DESC
```

When used alone (without `FullTextSearchFilter`), `SearchRankOrderingFilter` adds both
the `@@` match condition and the rank ordering. When used together, it only adds the
rank ordering since the match condition is already applied.

### Combining Filters

Filters chain sequentially. The output queryset of each backend feeds into the next:

```python
class PostViewSet(ModelViewSet):
    filter_backends = [FieldFilter, SearchFilter, OrderingFilter]
    filterset_fields = ["status", "category"]
    search_fields = ["title", "content"]
    ordering_fields = ["created_at", "title"]
    ordering = ["-created_at"]
```

```
GET /posts?status=active&search=django&ordering=-title
# 1. FieldFilter: .filter(status="active")
# 2. SearchFilter: .where_raw("(title ILIKE $1 OR content ILIKE $2)", "%django%", "%django%")
# 3. OrderingFilter: .order_by("-title")
```

---

## Public ID Integration

ViewSets automatically detect models that use `IDMixin` or `PublicIDMixin` and
handle encoding/decoding of external IDs at the API boundary. No manual
configuration is needed.

### IDMixin Model Detection

When a ViewSet's `model` uses `IDMixin`, the ViewSet:

1. **Auto-encodes** -- List and retrieve responses replace integer PKs with external IDs.
2. **Auto-decodes** -- Detail URL lookups decode the external ID back to an integer PK for database queries.
3. **Returns 404 for invalid IDs** -- Forged, malformed, or expired external IDs return HTTP 404 (never 400), preventing information leakage.

```python
from hyperdjango.public_id import IDMixin


class Post(IDMixin, Model):
    class Meta:
        table = "posts"

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "W9gx3PJhF7Xc5MrQfp2vRV8mGCwq6j4"
        hmac_keys = ["key-2025-q1"]

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()


class PostSerializer(ModelSerializer):
    class Meta:
        model = Post
        fields = "__all__"


class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    model = Post
```

```
GET /api/posts
# [{"id": "cX9.a1b2c3d4e5f6a1b2", "title": "Hello"}, ...]

GET /api/posts/cX9.a1b2c3d4e5f6a1b2
# {"id": "cX9.a1b2c3d4e5f6a1b2", "title": "Hello"}
# Internally resolves to: Post.objects.get(pk=42)

GET /api/posts/cX9.forged_signature
# 404 Not Found (indistinguishable from "object not found")
```

### Per-User Signed IDs

When `IDConfig.include_user = True`, the ViewSet automatically passes
`request.user["id"]` during encode and decode. Each user sees different
external IDs for the same objects.

### Mode-Specific Behavior

| ID Mode   | Encode                        | Decode Lookup                         |
| --------- | ----------------------------- | ------------------------------------- |
| `signed`  | `IDManager.encode(pk)`        | `IDManager.decode(external_id)` to PK |
| `encoded` | `IDManager.encode(pk)`        | `IDManager.decode(external_id)` to PK |
| `raw`     | String of integer PK          | Parse as integer                      |
| `random`  | Use stored `public_id` column | `WHERE public_id = $1`                |

### PublicIDMixin (Legacy)

Models using the legacy `PublicIDMixin` are also detected. The `encoded_pk`
strategy decodes to an integer PK; `random` and `uuid7` strategies perform a
`WHERE public_id = $1` lookup against the stored column.

---

## Permissions

### Built-in Classes

| Class                       | Rule                                                                       |
| --------------------------- | -------------------------------------------------------------------------- |
| `AllowAny`                  | No restrictions                                                            |
| `IsAuthenticated`           | `request.user` must not be None                                            |
| `IsAdminUser`               | `request.user.is_staff` must be True                                       |
| `IsAuthenticatedOrReadOnly` | GET/HEAD/OPTIONS: anyone. Mutations: authenticated only                    |
| `ModelPermission`           | Maps HTTP methods to RBAC model-level permissions (view/add/change/delete) |
| `ObjectPermission`          | Owner-based object-level access control                                    |

### Composition Operators

Permissions support `&` (AND), `|` (OR), and `~` (NOT) composition:

```python
# Both must pass
permission_classes = [IsAuthenticated & IsAdminUser]

# Either can pass
permission_classes = [IsAuthenticated | IsReadOnly]

# Inverted check
permission_classes = [~IsBlacklisted]

# Complex composition
permission_classes = [(IsAuthenticated & IsAdminUser) | IsSuperUser]
```

### ModelPermission

Maps HTTP methods to RBAC model-level permissions:

| HTTP Method        | Required Permission |
| ------------------ | ------------------- |
| GET, HEAD, OPTIONS | `view_{table}`      |
| POST               | `add_{table}`       |
| PUT, PATCH         | `change_{table}`    |
| DELETE             | `delete_{table}`    |

```python
class PostViewSet(ModelViewSet):
    permission_classes = [ModelPermission]
    # GET /posts requires "view_posts" permission
    # POST /posts requires "add_posts" permission
```

### ObjectPermission

Per-object permission check using the owner pattern:

```python
class PostViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated, ObjectPermission]
    # ObjectPermission checks: obj.owner_id == request.user.id
```

The `owner_field` attribute (default: `"owner_id"`) specifies which model field to compare.

### Custom Permissions

```python
class IsOwner(BasePermission):
    async def has_permission(self, request, view):
        return True  # view-level check (before object retrieval)

    async def has_object_permission(self, request, view, obj):
        return obj.author_id == request.user["id"]  # per-object check
```

---

## Authentication

Per-view authentication classes. Tried in order; first success populates `request.user` and `request.auth`.

```python
class PostViewSet(ModelViewSet):
    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (IsAuthenticated,)
```

### SessionAuthentication

Picks up `request.user` from session middleware. No additional header needed -- relies on cookie-based sessions.

### APIKeyAuthentication

Validates the `X-API-Key` header against the API key system:

```
X-API-Key: hyp_abc123...
```

### TokenAuthentication

Validates `Authorization: Token <key>` header. Override `get_user_for_token()` to implement token lookup:

```python
class MyTokenAuth(TokenAuthentication):
    async def get_user_for_token(self, token):
        return await User.objects.filter(api_token=token).first()
```

### Custom Authentication

```python
class CustomAuth(BaseAuthentication):
    async def authenticate(self, request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[7:]
        user = await verify_token(token)
        if user is None:
            raise AuthenticationFailed("Invalid token")
        return AuthResult(user=user, auth_info=token)
```

---

## Throttling

Per-view rate limiting backed by `InMemoryRateLimitBackend` (default) or `DatabaseRateLimitBackend` (PostgreSQL UNLOGGED table).

### In-Memory Throttles

| Class                | Key                         | Default Rate |
| -------------------- | --------------------------- | ------------ |
| `AnonRateThrottle`   | IP address (anonymous only) | 100/hour     |
| `UserRateThrottle`   | User ID (or IP for anon)    | 1000/hour    |
| `ScopedRateThrottle` | `throttle_scope` + user/IP  | 100/hour     |

```python
class PostViewSet(ModelViewSet):
    throttle_classes = (AnonRateThrottle, UserRateThrottle)
```

### Database Throttles

Persistent rate limiting backed by PostgreSQL UNLOGGED tables. Survives restarts and works across processes.

| Class                    | Key                         | Default Rate |
| ------------------------ | --------------------------- | ------------ |
| `DatabaseAnonThrottle`   | IP address (anonymous only) | 100/hour     |
| `DatabaseUserThrottle`   | User ID (or IP for anon)    | 1000/hour    |
| `DatabaseScopedThrottle` | `throttle_scope` + user/IP  | 100/hour     |

Setup:

```python
from hyperdjango.ratelimit import DatabaseRateLimitBackend

backend = DatabaseRateLimitBackend(db)
await backend.ensure_table()
DatabaseThrottle.set_backend(backend)
```

### SimpleRateThrottle

Base class for custom throttles:

```python
class BurstRateThrottle(SimpleRateThrottle):
    rate = "10/second"

    def get_cache_key(self, request, view):
        return f"burst:{request.user['id']}"
```

**Rate format:** `"N/period"` where period is `second`, `minute`, `hour`, `day` (or abbreviations `s`, `m`, `h`, `d`).

When throttled, the ViewSet returns 429 with retry information:

```json
{ "detail": "Request was throttled. Retry after 42 seconds", "status": 429 }
```

### ScopedRateThrottle

Per-endpoint rate limiting using `throttle_scope`:

```python
class UploadViewSet(ModelViewSet):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "uploads"


class ScopedUploadThrottle(ScopedRateThrottle):
    rate = "10/minute"
```

---

## Content Negotiation

### Parsers

ViewSets automatically select the right parser based on `Content-Type`:

| Parser            | Content-Type                        | Notes                            |
| ----------------- | ----------------------------------- | -------------------------------- |
| `JSONParser`      | `application/json`                  | SIMD-accelerated via Zig         |
| `FormParser`      | `application/x-www-form-urlencoded` | Standard form data               |
| `MultiPartParser` | `multipart/form-data`               | Files + fields (native Zig SIMD) |

Unsupported content types return `415 Unsupported Media Type`.

### Renderers

Response format negotiation via URL suffix or Accept header:

| Renderer       | Media Type         | Format Suffix |
| -------------- | ------------------ | ------------- |
| `JSONRenderer` | `application/json` | `.json`       |
| `CSVRenderer`  | `text/csv`         | `.csv`        |

**Priority:** URL suffix > Accept header > first renderer (default)

```python
class PostViewSet(ModelViewSet):
    renderer_classes = (JSONRenderer, CSVRenderer)


# GET /api/posts         -> JSON (default)
# GET /api/posts.csv     -> CSV download (Content-Disposition: attachment)
# Accept: text/csv       -> CSV
```

CSVRenderer automatically extracts `results` from paginated responses and flattens values to strings.

---

## HTTP Caching

`CacheableMixin` adds ETag, If-None-Match, and Cache-Control support to list and retrieve actions.

```python
class PostViewSet(CacheableMixin, ModelViewSet):
    cache_max_age = 60  # Cache-Control: max-age=60
    cache_private = True  # Cache-Control: private
    cache_no_cache = False  # If True, Cache-Control: no-cache
```

**How it works:**

1. After serialization, computes a weak ETag from SHA-256 of response content: `W/"<16-char-hex>"`
2. Adds `Cache-Control` and `ETag` headers to response
3. On GET/HEAD, checks `If-None-Match` header -- if ETag matches, returns 304 Not Modified with no body

**Configuration:**

| Attribute        | Default | Description                                   |
| ---------------- | ------- | --------------------------------------------- |
| `cache_max_age`  | `0`     | Seconds for `max-age` directive (0 = omitted) |
| `cache_private`  | `True`  | `private` vs `public` cache scope             |
| `cache_no_cache` | `False` | If True, forces revalidation on every request |

---

## Versioning

Three strategies for API version negotiation. Set `versioning_class` on your ViewSet, then access `request.version` in handlers.

| Strategy               | Detection          | Example                                                    |
| ---------------------- | ------------------ | ---------------------------------------------------------- |
| `URLPathVersioning`    | URL path parameter | `/api/v1/posts` -> version `"1"`                           |
| `HeaderVersioning`     | Accept header      | `Accept: application/json; version=2.0` -> version `"2.0"` |
| `QueryParamVersioning` | Query parameter    | `?version=1.0` -> version `"1.0"`                          |

```python
class PostViewSet(ModelViewSet):
    versioning_class = QueryParamVersioning
```

**Configuration (all strategies):**

| Attribute          | Default | Description                                            |
| ------------------ | ------- | ------------------------------------------------------ |
| `default_version`  | `"1.0"` | Fallback when no version detected                      |
| `allowed_versions` | `()`    | If non-empty, validates version against this whitelist |

URLPathVersioning strips the `v` prefix: `"v1"` becomes `"1"`, `"v2.1"` becomes `"2.1"`.

---

## Custom Actions

The `@action` decorator marks ViewSet methods as routable custom endpoints.

```python
class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    model = Post

    @action(methods=["POST"], detail=True, url_path="publish")
    async def publish(self, request, **kwargs):
        post = await self.get_object()
        post.status = "published"
        await post.save()
        return Response.json(self.get_serializer(obj=post).data)

    @action(methods=["GET"], detail=False, url_path="recent")
    async def recent(self, request, **kwargs):
        posts = await self.get_queryset().order_by("-created_at").limit(10).all()
        serializer = self.get_serializer(obj=posts, many=True)
        return Response.json(serializer.data)
```

Generates:

- `POST /posts/{id}/publish` -- detail action (operates on a specific post)
- `GET /posts/recent` -- list action (operates on the collection)

### Parameters

| Parameter           | Default                       | Description                                                                    |
| ------------------- | ----------------------------- | ------------------------------------------------------------------------------ |
| `methods`           | (required)                    | HTTP methods this action responds to: `["GET"]`, `["POST"]`, etc.              |
| `detail`            | `False`                       | `True` for detail routes (`/{pk}/action`), `False` for list routes (`/action`) |
| `url_path`          | method name                   | URL path suffix                                                                |
| `url_name`          | method name (with `_` -> `-`) | Route name suffix                                                              |
| `input_serializer`  | `None`                        | Serializer class for auto-validating request body before the handler is called |
| `output_serializer` | `None`                        | Serializer class for OpenAPI schema generation (does not affect runtime)       |

### input_serializer

When an action declares `input_serializer`, the request body is automatically validated before the handler runs. Validated data is available on `request._validated_data` and `self.validated_data`:

```python
class PublishInput(Serializer):
    publish_date: str = SerializerField(required=False)
    notify_subscribers: bool = SerializerField(required=False)


class PostViewSet(ModelViewSet):
    @action(methods=["POST"], detail=True, input_serializer=PublishInput)
    async def publish(self, request, **kwargs):
        data = self.validated_data  # already validated
        post = await self.get_object()
        post.status = "published"
        if data.get("publish_date"):
            post.publish_date = data["publish_date"]
        await post.save()
        return Response.json({"status": "published"})
```

On validation failure, returns 400 with field-level errors before the handler is called.

---

## Typed Fields

Specialized serializer fields with type-safe serialization and validation.

### All Field Types

| Field                   | Input                         | Output                       | Notes                                         |
| ----------------------- | ----------------------------- | ---------------------------- | --------------------------------------------- |
| `DateTimeField`         | ISO 8601 string or `datetime` | ISO 8601 string              | `format_str="iso"` (default)                  |
| `DateField`             | ISO date string or `date`     | ISO date string              |                                               |
| `TimeField`             | ISO time string or `time`     | ISO time string              |                                               |
| `ChoiceField`           | One of `choices`              | Pass-through                 | `ChoiceField(choices=["draft", "published"])` |
| `MultipleChoiceField`   | List of values from `choices` | Pass-through                 | Auto-deduplicates before validation           |
| `UUIDField`             | UUID string or `UUID`         | String                       |                                               |
| `DecimalField`          | Number string                 | String                       | `max_digits`, `decimal_places`                |
| `EmailField`            | Email string                  | Pass-through                 | Validates `@` present                         |
| `URLField`              | URL string                    | Pass-through                 | Validates `http://` or `https://` prefix      |
| `IPAddressField`        | IP string                     | Pass-through                 | IPv4 (validated) and IPv6 (format trusted)    |
| `ReadOnlyField`         | Never accepted                | Pass-through                 | Always `read_only=True`, `required=False`     |
| `HiddenField`           | Not in representation         | Included in `validated_data` | Use with `CurrentUserDefault()`               |
| `FileUploadField`       | File upload or bytes          | URL string                   | Validates size and extension                  |
| `ImageUploadField`      | Image upload or bytes         | URL string                   | Validates magic bytes + extension + size      |
| `SerializerMethodField` | N/A (read-only)               | Method return value          | Calls `get_{field_name}(obj)`                 |

### Examples

```python
class EventSerializer(ModelSerializer):
    start_time: datetime = DateTimeField()
    category: str = ChoiceField(choices=["conference", "meetup", "workshop"])
    tags: list = MultipleChoiceField(choices=["python", "rust", "zig", "web"])
    price: Decimal = DecimalField(max_digits=8, decimal_places=2)
    contact_email: str = EmailField()
    event_id: UUID = UUIDField(read_only=True)
    host_ip: str = IPAddressField(required=False)
    created_by: int = HiddenField(default=CurrentUserDefault())

    class Meta:
        model = Event
        fields = "__all__"
```

### CurrentUserDefault

Auto-injects the current user ID from request context:

```python
class PostSerializer(ModelSerializer):
    author_id: int = HiddenField(default=CurrentUserDefault())
    # On create: author_id is set to request.user.id automatically
```

### SerializerMethodField

Read-only computed fields. Convention: field named `full_name` calls `self.get_full_name(obj)`:

```python
class UserSerializer(Serializer):
    full_name: str = SerializerMethodField()
    custom: str = SerializerMethodField(method_name="compute_custom")

    def get_full_name(self, obj):
        return f"{obj['first_name']} {obj['last_name']}"

    def compute_custom(self, obj):
        return "custom_value"
```

### Source Traversal

Dotted `source` paths resolve nested attributes:

```python
class PostSerializer(ModelSerializer):
    author_name: str = SerializerField(read_only=True, source="author.name")
    # Resolves: obj.author.name (or obj["author"]["name"] for dicts)
```

---

## File Uploads

### FileUploadField

Validates uploaded files by size and extension:

```python
class DocumentSerializer(Serializer):
    file: bytes = FileUploadField(
        max_size=10 * 1024 * 1024,  # 10 MB (default)
        allowed_extensions=frozenset({"pdf", "docx", "txt"}),
    )
```

**Default allowed extensions:** jpg, jpeg, png, gif, webp, bmp, svg, pdf, txt, csv, json, xml, zip.

### ImageUploadField

Extends FileUploadField with magic bytes validation:

```python
class AvatarSerializer(Serializer):
    image: bytes = ImageUploadField(
        max_size=5 * 1024 * 1024,  # 5 MB
        allowed_extensions=frozenset({"png", "jpeg", "webp"}),
        verify_magic_bytes=True,  # default: True
    )
```

**Recognized image signatures:**

| Format | Magic Bytes         |
| ------ | ------------------- |
| PNG    | `\x89PNG\r\n\x1a\n` |
| JPEG   | `\xff\xd8\xff`      |
| GIF    | `GIF87a`, `GIF89a`  |
| WebP   | `RIFF`              |
| BMP    | `BM`                |
| SVG    | `<?xml`, `<svg`     |

When `verify_magic_bytes=True`, the file header is checked against these signatures. Files that don't match any signature are rejected with a validation error, preventing disguised file uploads.

---

## OpenAPI

`APIRouter.get_schema()` generates a complete OpenAPI 3.1 specification from all registered ViewSets.

```python
router = APIRouter(prefix="/api/v1")
router.register("posts", PostViewSet)
router.register("users", UserViewSet)

schema = router.get_schema(
    title="Blog API",
    version="1.0.0",
    description="A blog API built with HyperDjango",
)
```

### What It Generates

- **Paths** for every registered ViewSet (list, create, retrieve, update, partial_update, destroy) and custom `@action` endpoints
- **Component schemas** from ModelSerializer fields (both input and output variants)
- **Parameters** for filter fields, search, ordering, and pagination
- **Request bodies** for POST/PUT/PATCH operations with `$ref` to input schemas
- **Response schemas** with `$ref` to output schemas

### Swagger UI Integration

Mount the OpenAPI docs with Swagger UI:

```python
from hyperdjango.openapi import mount_docs

mount_docs(app)
# GET /docs       -> Swagger UI
# GET /openapi.json -> Raw OpenAPI 3.1 spec
```

### Custom Action Schemas

Use `input_serializer` and `output_serializer` on `@action` for OpenAPI docs:

```python
@action(
    methods=["POST"],
    detail=True,
    input_serializer=PublishInput,
    output_serializer=PublishOutput,
)
async def publish(self, request, **kwargs): ...
```

---

## Performance

### Native JSON Fast Path

When `use_native_json=True` and the serializer is an identity serializer (all fields map 1:1 to DB columns with no computed/method/nested fields), list and retrieve actions skip Python serialization entirely and use Zig `_db_query_json` to build JSON directly from the PostgreSQL wire protocol.

```python
class PostViewSet(ModelViewSet):
    serializer_class = PostSerializer
    model = Post
    use_native_json = True  # Enable Zig native JSON path
```

**Requirements for native JSON path:**

- `use_native_json = True` on the ViewSet
- Serializer must be a `ModelSerializer`
- `_is_identity_serializer` must be `True` (no SerializerMethodField, no TypedField, no nested serializers, no computed sources)

When active, the list endpoint executes the SQL query and builds the JSON response entirely in Zig, bypassing Python serialization. The retrieve endpoint also uses this path, unwrapping the single-element array.

Because this path serializes straight from the PostgreSQL wire protocol, a few column types map to a JSON form chosen for losslessness rather than the "obvious" one — notably `NUMERIC` and non-finite floats become **strings**, and `bytea` becomes a `\x`-hex string. See [Result serialization to JSON](database.md#result-serialization-to-json) for the full type→JSON table. If a client needs `NUMERIC` as a JSON number, keep `use_native_json` off so Python serialization applies your `SerializerField` coercions instead.

Non-finite floats (`NaN`/`±Infinity`) are emitted as the same lossless strings (`"NaN"`/`"Infinity"`/`"-Infinity"`) on **both** paths — the Python serializer and the native path agree, so a value is never silently dropped to `null` regardless of `use_native_json`. Only `NUMERIC`-as-number is affected by the flag.

### Field Resolution Caching

ModelSerializer pre-computes field metadata at class creation time (via `ModelSerializerMeta`):

- Read-only field set -- O(1) lookup to skip read-only fields during write
- Write field set -- O(1) lookup for writable fields
- Column-to-field mapping -- maps DB column names to serializer field names
- Identity detection -- flags serializers where all fields map directly to DB columns

This avoids per-request field introspection overhead.

---

## Metering

`MeteringMixin` auto-records API usage events via the MeterEngine.

```python
from hyperdjango.rest import MeteringMixin, ModelViewSet


class PostViewSet(MeteringMixin, ModelViewSet):
    metering_meter_name = "api_usage"  # meter name (default: "api_usage")
    metering_enabled = True  # toggle metering (default: True)
    serializer_class = PostSerializer
    model = Post
```

**What it records:**

Each request records three dimensions:

| Dimension        | Value                            |
| ---------------- | -------------------------------- |
| `requests`       | `1` (request count)              |
| `response_bytes` | Length of response body          |
| `duration_ms`    | Request duration in milliseconds |

Events are keyed by `account_id` (from `request.user.id`, or `"anonymous"` for unauthenticated requests).

Metering failures are silently logged and never break the request.

**Configuration:**

| Attribute             | Default       | Description                         |
| --------------------- | ------------- | ----------------------------------- |
| `metering_meter_name` | `"api_usage"` | Meter name for recording events     |
| `metering_enabled`    | `True`        | Enable/disable metering per ViewSet |

---

## Relational Fields

### PrimaryKeyRelatedField

Serializes FK as integer PK. Validates PK exists on write.

```python
class PostSerializer(ModelSerializer):
    # Auto-detected for FK fields, or declare explicitly:
    author_id: int = PrimaryKeyRelatedField(queryset=User.objects)

    class Meta:
        model = Post
        fields = "__all__"
```

ModelSerializer auto-detects FK fields and generates `PrimaryKeyRelatedField` with the target model's queryset.

### SlugRelatedField

Serializes FK as a slug field value. Resolves by slug on write.

```python
class PostSerializer(ModelSerializer):
    author: str = SlugRelatedField(queryset=User.objects, slug_field="username")

    class Meta:
        model = Post
        fields = ["id", "title", "author"]
```

Both support `many=True` for M2M relationships:

```python
tags: list = PrimaryKeyRelatedField(queryset=Tag.objects, many=True)
```

---

## Exception Handling

ViewSet automatically catches `APIException` subclasses and returns structured JSON:

```json
{ "detail": "Not found", "status": 404 }
```

Validation errors include field-level details:

```json
{
  "detail": "Validation failed",
  "status": 400,
  "errors": {
    "email": ["This field is required"],
    "age": ["Minimum value is 0"]
  }
}
```

### Exception Classes

| Class                  | Status | Use                            |
| ---------------------- | ------ | ------------------------------ |
| `ValidationError`      | 400    | Input validation failure       |
| `AuthenticationFailed` | 401    | Missing or invalid credentials |
| `PermissionDenied`     | 403    | Insufficient permissions       |
| `NotFound`             | 404    | Resource not found             |
| `MethodNotAllowed`     | 405    | Wrong HTTP method              |
| `Throttled`            | 429    | Rate limit exceeded            |

Raise directly in handlers:

```python
from hyperdjango.rest import NotFound, ValidationError


@action(methods=["POST"], detail=True)
async def approve(self, request, **kwargs):
    obj = await self.get_object()
    if obj.status != "pending":
        raise ValidationError("Only pending items can be approved")
    ...
```

Unhandled exceptions return a generic 500 with no information leakage:

```json
{ "detail": "Internal server error", "status": 500 }
```

---

## OPTIONS / SimpleMetadata

Every ViewSet automatically responds to OPTIONS requests with field introspection metadata:

```python
class PostViewSet(ModelViewSet):
    metadata_class = SimpleMetadata  # default
```

```
OPTIONS /api/v1/posts
```

```json
{
  "name": "PostViewSet",
  "description": "...",
  "allowed_methods": ["get", "post"],
  "actions": {
    "fields": {
      "id": { "type": "integer", "required": false, "read_only": true },
      "title": {
        "type": "string",
        "required": true,
        "read_only": false,
        "max_length": 200
      },
      "status": {
        "type": "string",
        "required": false,
        "read_only": false,
        "choices": ["draft", "published"]
      }
    }
  }
}
```

Field metadata includes: type, required, read_only, label, help_text, min_length, max_length, min_value, max_value, choices (when applicable).

---

## Serializer Features

### many=True Deserialization

Validate a list of items for bulk create/update:

```python
serializer = PostSerializer(
    input_data=[
        {"title": "Post 1", "content": "..."},
        {"title": "Post 2", "content": "..."},
    ],
    many=True,
)

if serializer.is_valid():
    for item in serializer.validated_data:
        await Post.objects.create(**item)
```

Errors are indexed by position: `{"0": {"title": "required"}, "2": {"content": "too short"}}`.

### is_valid(raise_exception=True)

```python
serializer.is_valid(raise_exception=True)  # raises ValueError if invalid
```

### serializer.save()

Dispatches to `create()` or `update()` based on whether an instance was passed. Extra kwargs are merged into validated_data:

```python
serializer = PostSerializer(input_data=request_data)
serializer.is_valid(raise_exception=True)
post = await serializer.save(author_id=request.user["id"])
```

---

## QuerySet.where_raw

The REST module adds `where_raw()` to QuerySet for OR-based search conditions:

```python
qs = Post.objects.where_raw(
    "(title ILIKE {idx} OR content ILIKE {idx})", "%search_term%"
)
```

`{idx}` placeholders are replaced with parameterized `$N` indices. Used internally by `SearchFilter` but also available for custom filter backends.
