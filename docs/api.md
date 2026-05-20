# API Reference

## HyperApp

```python
from hyperdjango import HyperApp

app = HyperApp(
    title="My App",
    database="postgres://localhost/mydb",
    views_dir="views",  # auto-discover file-based routes
)

# Route decorators
@app.get("/path")
@app.post("/path")
@app.put("/path")
@app.patch("/path")
@app.delete("/path")
@app.route("/path", methods=["GET", "POST"])

# Middleware
app.use(middleware_instance)

# Run
app.run(host="0.0.0.0", port=8000)
```

## Model

```python
from hyperdjango import Model, Field


class User(Model):
    class Meta:
        table = "users"
        database = "default"  # optional: multi-db routing

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    email: str = Field(unique=True)
    age: int = Field(ge=0, default=0)
    bio: str | None = Field(default=None)
    author_id: int = Field(foreign_key=Author)


# Instance methods
await user.save()
await user.delete()
await user.refresh_from_db()
user.pk  # primary key value
```

## QuerySet

```python
# Terminal methods (execute query)
items = await Model.objects.all()
item = await Model.objects.get(id=1)
item = await Model.objects.first()
item = await Model.objects.last()
count = await Model.objects.count()
exists = await Model.objects.exists()

# Chainable methods
qs = Model.objects.filter(age__gte=18)
qs = qs.exclude(name="Bob")
qs = qs.order_by("-created_at")
qs = qs.limit(10).offset(20)
qs = qs.distinct()
qs = qs.values("name", "email")
qs = qs.values_list("name", flat=True)
qs = qs.select_related("author")
qs = qs.prefetch_related("tags")
qs = qs.annotate(total=Sum("price"))
qs = qs.using("replica")
qs = qs.cache(ttl=60)

# Write operations
item = await Model.objects.create(name="New")
count = await Model.objects.filter(active=False).update(active=True)
count = await Model.objects.filter(id=1).delete()
items = await Model.objects.bulk_create([...])

# Aggregation
stats = await Model.objects.aggregate(
    total=Sum("price"),
    avg=Avg("price"),
    count=Count("id"),
)
```

## Lookups

```python
# Comparison
filter(age__gt=18, age__lte=65)
filter(age__range=(18, 65))

# String
filter(name__contains="ali")
filter(name__icontains="ALI")
filter(name__startswith="Al")
filter(email__endswith="@example.com")
filter(name__iexact="alice")

# Membership
filter(id__in=[1, 2, 3])
filter(bio__isnull=True)
filter(name__regex=r"^[A-Z]")

# Transforms
filter(created__year=2024)
filter(created__year__gte=2020)
filter(name__lower__contains="ali")
filter(name__length__gte=5)
```

## Response

```python
from hyperdjango.response import Response

Response.json(data, status=200, headers={})
Response.html(html_string, status=200)
Response.text(text_string, status=200)
Response.redirect(url, status=302)
Response.file(path)
Response.attachment(path, filename="download.pdf")
Response.stream(async_generator)
Response.sse(event_generator)
```

## Forms

```python
from hyperdjango.forms import Form, ModelForm, CharField, IntegerField, FormSet


class MyForm(Form):
    name = CharField(max_length=100, required=True)
    age = IntegerField(min_value=0, required=False)


form = MyForm(data=request.json)
if form.is_valid():
    print(form.cleaned_data)

# FormSet
formset = FormSet(MyForm, data=[...], extra=2, can_delete=True)
```

## Paginator

```python
from hyperdjango.paginator import Paginator

paginator = Paginator(queryset, per_page=25)
page = await paginator.page(1)

page.items, page.number, page.num_pages, page.count
page.has_next, page.has_previous
page.start_index, page.end_index, page.page_range
```

## Class-Based Views

```python
from hyperdjango.views import ListView, DetailView, CreateView, UpdateView, DeleteView


class UserList(ListView):
    model = User
    per_page = 25


app.route("/users")(UserList.as_view())
```

## Model Mixins

```python
from hyperdjango.mixins import (
    TimestampMixin,
    SoftDeleteMixin,
    OwnershipMixin,
    VersionedMixin,
)


# TimestampMixin — auto-managed created_at/updated_at (TIMESTAMPTZ)
class Article(TimestampMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()


article = Article(title="Hello")
await article.save()  # created_at and updated_at set automatically


# SoftDeleteMixin — .delete() marks as deleted, auto-filtered QuerySet
class Post(SoftDeleteMixin, Model):
    class Meta:
        table = "posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()


await post.delete()  # Soft delete (UPDATE is_deleted=TRUE)
await post.hard_delete()  # Real DELETE
await post.restore()  # Undo soft delete
posts = await Post.objects.all()  # Excludes soft-deleted
posts = await Post.objects.with_deleted().all()  # Includes all
posts = await Post.objects.only_deleted().all()  # Just deleted


# OwnershipMixin — tracks created_by/updated_by
class Doc(OwnershipMixin, Model): ...


await doc.save_as(user)  # Sets created_by on first save, updated_by on every save
await doc.save_as(user_id=42)  # Accepts int or user object


# VersionedMixin — append-only versioning
class Config(VersionedMixin, Model): ...


await config.save()  # version=1, is_current=True
config.value = "updated"
await config.save()  # version=2 (new row), old row is_current=False
history = await config.get_history()  # All versions ordered by version
current = await Config.objects.all()  # Only is_current=True
all_ver = await Config.objects.with_versions().all()  # All versions


# Compose multiple mixins
class FullModel(TimestampMixin, SoftDeleteMixin, OwnershipMixin, Model): ...
```

## Query Cache

```python
from hyperdjango.query_cache import get_query_cache, configure_query_cache

# Configure (typically in app setup)
configure_query_cache(default_ttl=60)

# Per-query opt-in
users = await User.objects.cache(ttl=120).filter(active=True).all()


# Per-model default via Meta
class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300  # All queries auto-cached 5 min


# Disable caching for specific query
products = await Product.objects.cache(False).all()

# Auto-invalidation: save/delete triggers signal → cache invalidated
await product.save()  # Automatically invalidates Product query cache

# Manual invalidation
qc = get_query_cache()
qc.invalidate_table("products")
qc.invalidate_row("products", pk=42)
qc.invalidate_all()

# Cache stats
print(qc.stats.hit_rate)  # 0.85
print(qc.stats.hits)  # 1700
print(qc.stats.misses)  # 300
print(qc.stats.invalidations)  # 42

# Cache warming
qc.warm(key, precomputed_data, ttl=300)
```

## Security

```python
from hyperdjango.security import SecurityLog, SecurityEvent
from hyperdjango.standalone_middleware import SecurityHeadersMiddleware

# Security headers (all on by default)
app.use(
    SecurityHeadersMiddleware(
        hsts=True,
        csp="default-src 'self'",
        referrer_policy="strict-origin-when-cross-origin",
        permissions_policy="camera=(), microphone=()",
        cross_origin_opener_policy="same-origin",
    )
)

# Security event audit log
log = SecurityLog(db)
await log.ensure_table()

await log.log(SecurityEvent.LOGIN_FAILED, ip="1.2.3.4", detail="bad password")
await log.log_from_request(SecurityEvent.PERMISSION_DENIED, request, detail="no admin")

events = await log.get_recent(limit=50)
user_events = await log.get_for_user(42)
ip_events = await log.get_for_ip("1.2.3.4")
count = await log.count_by_ip("1.2.3.4", SecurityEvent.LOGIN_FAILED, since_hours=1)
```

## Pool Optimization

```python
from hyperdjango.pool import SlowQueryLog, QueryTimer, PoolHealthChecker

# Persistent slow query log (PostgreSQL UNLOGGED)
slow_log = SlowQueryLog(db, threshold_ms=100)
await slow_log.ensure_table()
recent = await slow_log.get_recent(limit=20)
slowest = await slow_log.get_slowest(limit=10)
stats = await slow_log.get_stats()  # {total, avg_ms, max_ms, min_ms}

# Auto-timing wrapper (patches db.query/execute)
timer = QueryTimer(db, slow_log=slow_log, threshold_ms=100)
timer.install()
# Now all queries are automatically timed and slow ones logged
stats = timer.get_stats()  # {total_queries, avg_query_ms, in_flight}

# Graceful drain before shutdown
success = await timer.drain(timeout_seconds=30)

# Pool health checks
checker = PoolHealthChecker(db, interval_seconds=30)
healthy = await checker.check()  # SELECT 1 validation
checker.start()  # Background periodic checks
stats = checker.get_stats()  # {healthy, checks, failures}
```

## Cache Adapters

```python
from hyperdjango.cache_adapters import (
    TwoTierCache,
    ConsistentHashRing,
    StampedeProtection,
    CacheMiddleware,
    register_adapter,
    get_adapter,
)
from hyperdjango.cache import LocMemCache, DatabaseCache

# Two-tier: fast L1 (in-process) + shared L2 (database)
cache = TwoTierCache(
    l1=LocMemCache(max_size=1000),
    l2=DatabaseCache(db),
    l1_ttl=10,
)
cache.set("key", value, ttl=300)
result = cache.get("key")  # L1 first, L2 fallback, auto-promote
stats = cache.get_stats()  # {l1_hits, l2_hits, misses, overall_hit_rate}

# Consistent hashing for sharded caches
ring = ConsistentHashRing(
    nodes={
        "shard1": LocMemCache(),
        "shard2": LocMemCache(),
        "shard3": LocMemCache(),
    }
)
node = ring.get_node("user:42")  # Deterministic routing
node.set("user:42", data)

# Stampede protection (XFetch algorithm)
cache = StampedeProtection(backend=LocMemCache(), beta=1.0)
cache.set("popular", data, ttl=300, compute_time_ms=50)
# Near expiry: probabilistic early recompute prevents thundering herd

# Full-page response caching middleware
app.use(CacheMiddleware(cache, ttl=60, exclude=["/admin", "/api/auth"]))

# Custom adapter registration
register_adapter("custom", MyCustomCacheAdapter)
adapter_cls = get_adapter("custom")
```

## Logging

```python
from hyperdjango.logging import logger, AccessLogMiddleware

# Basic logging (kwargs captured in extra)
logger.info("User logged in", user_id=42, ip="10.0.0.1")
logger.warning("Slow query", duration_ms=1500, sql="SELECT ...")
logger.error("Connection failed", host="db.example.com")

# All 7 levels
logger.trace("Detailed trace")
logger.debug("Debug info")
logger.info("Normal event")
logger.success("Operation completed")
logger.warning("Potential issue")
logger.error("Error occurred")
logger.critical("System failure")

# Message formatting with positional args
logger.info("User {} logged in from {}", username, ip)

# Bind context (returns new logger, original unchanged)
log = logger.bind(request_id="abc-123", user_id=42)
log.info("Processing request")  # extra has request_id + user_id

# Options: lazy eval, depth control, raw mode, exception capture
logger.opt(lazy=True).debug("Expensive: {x}", x=lambda: heavy_compute())
logger.opt(exception=True).error("Failed")  # Attaches traceback
logger.opt(depth=1).info("From wrapper")  # Skip frame for caller detection
logger.opt(raw=True).info("No formatting {}")
logger.opt(capture=False).info("Test {x}", x=42)  # x not in extra

# Context manager (async/thread-safe via contextvars)
with logger.contextualize(task_id=123, step="validate"):
    logger.info("In context")  # extra has task_id + step

# Patch records globally
import socket

patched = logger.patch(lambda r: r["extra"].update(hostname=socket.gethostname()))


# Exception decorator
@logger.catch()
def risky():
    1 / 0  # Logged as ERROR with full traceback


@logger.catch(level="CRITICAL", message="Task crashed")
async def background_task(): ...


# Custom levels
logger.level("AUDIT", no=35, color="\033[35m", icon="📋")
logger.log("AUDIT", "Password changed", user_id=1)

# Module enable/disable
logger.disable("noisy_library")
logger.enable("noisy_library.important")

# Add sinks
logger.add(sys.stderr, level="INFO")  # Console
logger.add(sys.stderr, serialize=True)  # JSON to stderr
logger.add("app.log", rotation="100 MB", retention=10)  # File with rotation
logger.add("app.log", rotation="daily", compression="gz")  # Daily + gzip
logger.add(logging_handler, level="WARNING")  # stdlib bridge
logger.add(my_async_func, level="ERROR")  # Async sink

# Dynamic format
logger.add(sys.stderr, format=lambda r: f"[{r['level'].name}] {r['message']}")

# Dict-based per-module filtering
logger.add(sys.stderr, filter={"": "WARNING", "myapp": "DEBUG", "noisy": False})

# Bulk configure
logger.configure(
    handlers=[
        {"sink": sys.stderr, "level": "INFO"},
        {"sink": "app.log", "level": "DEBUG", "rotation": "100 MB"},
    ],
    extra={"app": "myservice", "version": "1.0"},
    activation=[("noisy_lib", False)],
)

# System metrics
stats = logger.stats()  # {handlers, levels, min_level, writer_alive, queue_depth}

# Access log middleware
app.use(AccessLogMiddleware())  # Auto-logs: GET /api/users 200 12.3ms

# Record fields available in format strings:
# {time}, {level}, {message}, {name}, {function}, {file}, {line},
# {module}, {thread}, {process}, {elapsed}, {extra}, {exception}
```

## Static Files

```python
from hyperdjango.staticfiles import (
    StaticFilesMiddleware,
    StaticFilesFinder,
    ManifestStaticFilesStorage,
    get_static_url,
    set_manifest_storage,
)

# Development: serve with caching headers
app.use(
    StaticFilesMiddleware(
        static_dirs=["static", "node_modules"],
        prefix="/static/",
        max_age=3600,
        gzip_min_size=1024,
    )
)

# Production: collect with hashed filenames
storage = ManifestStaticFilesStorage(
    static_dirs=["static"],
    static_root="staticfiles",
)
result = storage.collectstatic()  # {"copied": 42, ...}
url = storage.url("css/styles.css")  # "css/styles.a1b2c3d4e5f6.css"
name = storage.stored_name("app.js")  # ValueError if missing (strict=True)

# Load manifest (auto-loads from staticfiles.json)
manifest = storage.load_manifest()  # {"css/styles.css": "css/styles.hash.css", ...}

# Global helper for templates
set_manifest_storage(storage)
get_static_url("css/styles.css")  # "/static/css/styles.a1b2c3d4e5f6.css"

# Template usage:
# {{ static('css/styles.css') }}
# {{ 'css/styles.css'|static }}

# Finder: locate files across directories
finder = StaticFilesFinder(dirs=["static", ("vendor", "/path/to/vendor")])
abs_path = finder.find("css/style.css")
all_files = finder.list_all()  # [(rel_path, abs_path), ...]

# Serve collected files with immutable cache (production)
app.use(
    StaticFilesMiddleware(
        static_root="staticfiles",
        prefix="/static/",
        max_age=31536000,
        immutable=True,
    )
)
```

## Channels (WebSocket Pub/Sub)

```python
from hyperdjango.channels import (
    InMemoryChannelLayer,
    PgChannelLayer,
    Message,
    websocket_channel_handler,
    set_channel_layer,
    get_channel_layer,
)

# In-memory (single-process)
layer = InMemoryChannelLayer(default_history_size=100)

# PostgreSQL (multi-process)
layer = PgChannelLayer(database_url="postgres://localhost/mydb")
await layer.connect()

# Channels
channel = layer.channel("chat:room1")
sub_id = channel.subscribe(callback)  # Sync or async callback
sub_id = channel.subscribe(cb, filter_fn=my_filter)  # Filtered
sub_id = channel.subscribe(cb, user_id="alice")  # With auth check
await channel.publish({"text": "Hello!"}, sender="alice")
channel.unsubscribe(sub_id)
channel.subscriber_count()

# Per-channel auth
channel = layer.channel("private:staff", auth_fn=lambda ch, uid: uid in admins)

# Presence tracking
channel.join("user42", metadata={"name": "Alice"})
members = channel.presence()  # [{"user_id": ..., "name": ..., "joined_at": ...}]
channel.presence_count()
channel.leave("user42")

# Message history
recent = channel.history(limit=50)
channel.clear_history()

# Groups (fan-out)
group = layer.group("notifications")
group.add("user:1")
group.add("user:2")
await group.publish({"type": "alert", "text": "Update"})
group.discard("user:1")


# WebSocket bridge
@app.websocket("/ws/chat/{room}")
async def chat(ws):
    await ws.accept()
    ch = layer.channel(f"chat:{ws.path_params['room']}")
    await websocket_channel_handler(ws, ch, user_id="user42")


# Message serialization
msg = Message(channel="test", data={"key": "val"}, sender="alice")
json_str = msg.to_json()
restored = Message.from_json(json_str)

# Global singleton
set_channel_layer(layer)
layer = get_channel_layer()

# Layer management
layer.channel_names()  # ["chat:room1", ...]
layer.group_names()  # ["notifications", ...]
layer.remove_channel("old")
layer.remove_group("old")
```
