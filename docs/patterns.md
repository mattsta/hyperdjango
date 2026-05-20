# HyperDjango Patterns & Best Practices

Standard patterns for building correct, performant, secure HyperDjango applications. Follow these to avoid common pitfalls.

## Model Fields

### Use Enum types for constrained values

When a field has a fixed set of valid values, use Python `Enum` as the field type annotation. HyperAdmin auto-detects enums and renders `<select>` dropdowns. Forms auto-populate choices.

```python
from enum import Enum
from hyperdjango.models import Field, Model


class TaskStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Priority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class Task(Model):
    class Meta:
        table = "tasks"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    status: TaskStatus = Field(default=TaskStatus.TODO)
    priority: Priority = Field(default=Priority.NORMAL)
```

**Never do this:**

```python
# BAD: magic string sets with manual validation
VALID_STATUSES = frozenset({"todo", "in_progress", "done"})
status: str = Field(default="todo")
# ...later in route handler...
if status not in VALID_STATUSES:
    raise HTTPException(400, "Invalid status")
```

Validation for enum fields:

```python
try:
    TaskStatus(value)
except ValueError:
    raise HTTPException(
        400, f"Invalid status. Must be one of: {', '.join(s.value for s in TaskStatus)}"
    )
```

### No max_length on string Fields

PostgreSQL `TEXT` type has no performance benefit from length constraints. Artificial limits are worse-performing than unconstrained TEXT.

```python
# GOOD
name: str = Field()
title: str = Field()
description: str = Field(default="")

# BAD
name: str = Field(max_length=200)  # Pointless constraint
```

Don't add manual `len()` checks either unless you have a real business reason (not a database reason).

---

## Pagination

### Use CursorPagination for all list endpoints

Never use LIMIT/OFFSET pagination. It scans and discards N rows — O(N) cost that grows linearly with page depth.

**In ViewSets** (REST framework):

```python
from hyperdjango.rest import CursorPagination, ModelViewSet


class ReviewCursorPagination(CursorPagination):
    page_size = 20
    ordering = "-id"


class ReviewViewSet(ModelViewSet):
    pagination_class = ReviewCursorPagination
    model = Review
```

**In standalone route handlers** (non-ViewSet):

```python
from hyperdjango.rest import CursorPagination


@app.get("/api/items/")
async def list_items(request):
    paginator = CursorPagination()
    paginator.page_size = 50
    paginator.ordering = "-id"

    items = await paginator.paginate_queryset(Item.objects, request)
    data = [{"id": i.id, "name": i.name} for i in items]
    return paginator.get_paginated_response(data)
```

Response format: `{"results": [...], "next": "?cursor=...", "previous": "?cursor=..."}`

**Never do this:**

```python
# BAD: LIMIT/OFFSET pagination
page = int(request.query("page", "1"))
offset = (page - 1) * page_size
items = await Item.objects.order_by("-id").limit(page_size).offset(offset).all()

# BAD: unbounded .all()
items = await Item.objects.all()  # Returns EVERYTHING

# BAD: hard cap without pagination
items = await Item.objects.order_by("-id").limit(20).all()  # No way to get items 21+
```

**Exception:** Time-decaying computed scores (hot_score, trending) where the sort order changes between requests make keyset cursors unstable. In these rare cases, OFFSET with a comment explaining why is acceptable.

### Available pagination classes

| Class                    | Use case                           | Mechanism                              |
| ------------------------ | ---------------------------------- | -------------------------------------- |
| `CursorPagination`       | Most list endpoints                | Keyset with HMAC-signed opaque cursors |
| `PageNumberPagination`   | ViewSets with known-small datasets | COUNT + LIMIT/OFFSET                   |
| `ServerCursorPagination` | Large exports, streaming           | Real PostgreSQL DECLARE CURSOR         |

---

## Database Queries

### Single query over N+1

Fetch related data in one query using JOINs, not sequential queries:

```python
# GOOD: single JOIN query
row = await db.query_one(
    "SELECT b.*, a.name AS author_name, c.name AS category_name "
    "FROM books b "
    "JOIN authors a ON a.id = b.author_id "
    "JOIN categories c ON c.id = b.category_id "
    "WHERE b.id = $1",
    book_id,
)

# BAD: 3 sequential queries (N+1)
book = await Book.objects.filter(id=book_id).first()
author = await db.query_one("SELECT name FROM authors WHERE id = $1", book.author_id)
category = await db.query_one(
    "SELECT name FROM categories WHERE id = $1", book.category_id
)
```

### Use FILTER for multi-stat queries

```python
# GOOD: single query with FILTER
row = await db.query_one(
    "SELECT COUNT(*) AS total, "
    "COUNT(*) FILTER (WHERE published = true) AS published, "
    "COUNT(*) FILTER (WHERE featured = true) AS featured, "
    "COALESCE(AVG(pages), 0) AS avg_pages "
    "FROM books"
)

# BAD: 4 separate queries
total = await db.query_val("SELECT COUNT(*) FROM books")
published = await db.query_val("SELECT COUNT(*) FROM books WHERE published = true")
featured = await db.query_val("SELECT COUNT(*) FROM books WHERE featured = true")
avg_pages = await db.query_val("SELECT COALESCE(AVG(pages), 0) FROM books")
```

### Use SQL aggregation, not Python loops

```python
# GOOD: aggregate in SQL
row = await db.query_one(
    "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total "
    "FROM usage_logs WHERE user_id = $1",
    user_id,
)

# BAD: load all rows and sum in Python
logs = await UsageLog.objects.filter(user_id=user_id).all()
total = sum(log.input_tokens + log.output_tokens for log in logs)
```

### Parameterized intervals

```python
# GOOD: parameterized interval
await db.execute(
    "DELETE FROM logs WHERE created_at < NOW() - make_interval(days => $1)", days
)

# BAD: parameter inside string literal (won't parameterize)
await db.execute("DELETE FROM logs WHERE created_at < NOW() - INTERVAL '$1 days'", days)
```

### Use `.exists()` for presence-only checks

When you only need to know whether a row exists — not its fields —
use `QuerySet.exists()` instead of `.filter(...).first()`. `.exists()`
emits `SELECT 1 ... LIMIT 1` which avoids pulling every column over
the wire and constructing a full Model instance for a row that's
never read.

```python
# GOOD: SELECT 1 ... LIMIT 1 — no row materialization
if await User.objects.filter(username=username).exists():
    raise HTTPException(409, "Username taken")

# GOOD: existence gate before an unrelated query
if not await Task.objects.filter(id=task_id).exists():
    raise HTTPException(404, "Task not found")
qs = Comment.objects.filter(task_id=task_id)

# BAD: fetches the full row just to check presence
existing = await User.objects.filter(username=username).first()
if existing:
    raise HTTPException(409, "Username taken")
```

Use `.first()` when you actually read fields from the result;
use `.exists()` when the variable is only read in an `if`/`if not`
check.

---

## F Expressions — Atomic Updates

Use `F()` in `QuerySet.update()` for race-condition-free counter updates. The operation runs as a single SQL `SET col = col + $1` — no read-modify-write cycle.

```python
from hyperdjango.expressions import F

# Atomic increment — safe under concurrency
await Post.objects.filter(id=post_id).update(score=F("score") + 1)

# Multiple F expressions in one call
await Post.objects.filter(id=post_id).update(
    score=F("score") + delta,
    upvotes=F("upvotes") + 1,
)

# Atomic decrement with F expression
await Category.objects.filter(id=cat_id).update(note_count=F("note_count") - 1)
```

### UPDATE...RETURNING — Atomic Update + Fetch

When you need the updated values back (e.g., new score after voting), use `returning=`:

```python
rows = await Post.objects.filter(id=post_id).update(
    score=F("score") + 1,
    returning=["id", "score", "author_id"],
)
# rows = [{"id": 1, "score": 42, "author_id": 7}]
```

This executes a single `UPDATE ... RETURNING` query — one roundtrip instead of UPDATE + SELECT.

---

## `time_bucket_cached` — Memoize Pure-of-Time Helpers

Functions that format timestamps into human-friendly strings
(`"5 minutes ago"`, `"2 hours ago"`, etc.) are pure within a short
time window — they only need to recompute when the clock crosses
a bucket boundary. `hyperdjango.humanize.time_bucket_cached` is a
decorator that caches the result by `(value, int(monotonic() / bucket_seconds))`.

```python
from hyperdjango.humanize import time_bucket_cached


@time_bucket_cached(bucket_seconds=30)
def time_ago(timestamp_str) -> str:
    """Format a timestamp as '5 min ago' — cached for 30-second buckets."""
    # ... expensive date math here
    return humanize_relative(timestamp_str)
```

Under a page-render workload where the same `created_at` appears 20
times in a list, the decorated function runs once and returns the
cached string 19 times. Inline `OrderedDict` + `popitem(last=False)`
for O(1) FIFO eviction — faster than `functools.lru_cache` under
free-threaded Python 3.14t because it avoids the internal lock
acquire per call.

Measured: 615 ns/op → 185 ns/op (3.26× faster) on the hypernews
`time_ago` call. Applied to `naturaltime`, hypernews `time_ago`,
and hyperticket `time_ago`.

**When to use**: any helper where the output depends on the current
time + some small set of inputs, AND the acceptable staleness is
measured in seconds. **When NOT to use**: random tokens, nonces,
anything requiring sub-bucket precision.

---

## Cache Static Per-Process Outputs at Startup

When an expensive computation's inputs never change at runtime (app
routes, registered serializers, compile-time config), don't
recompute it per request. Build it once and serve the cached
payload.

```python
# hyperdjango/openapi.py — the mount_docs cache
@dataclass(slots=True)
class OpenAPISpecCache:
    title: str | None
    version: str
    description: str
    cached_bytes: bytes | None = None

    def invalidate(self) -> None:
        self.cached_bytes = None


def mount_docs(app, ...):
    cache = OpenAPISpecCache(title=title, version=version, description=description)
    app._openapi_caches.append(cache)

    @app.get(openapi_path)
    async def openapi_json(request):
        if cache.cached_bytes is None:
            spec = generate_openapi(app, ...)
            cache.cached_bytes = fast_json_dumps(spec)
        return Response(body=cache.cached_bytes, content_type="application/json")
```

Measured on bookstore_api (~90 routes): `GET /openapi.json` went
from 422 rps → 6,115 rps (**+1,349 %**, 14.5×). The uncached path
spent 18 % of its time in `typing.get_type_hints`, which runs 128
times per spec build. With the cache, the entire schema-generation
machinery (and its `get_type_hints` calls) is off the request
path entirely.

Cache invalidation: when the inputs DO change (e.g. tests add
routes after `mount_docs` is called, or hot-reload replaces a
handler), expose an `invalidate_xxx_cache(app)` helper that walks
all registered caches and resets them.

---

## Lazy-init Per-Instance Primitives

If an object creates a heavy primitive (`threading.Event`,
`threading.Lock`, file handle, asyncio.Event) that most instances
won't actually use, lazy-init it.

```python
# BEFORE: every TaskHandle pays for Event + Condition + internal
# Lock allocation, even for fire-and-forget tasks that never wait
class TaskHandle:
    def __init__(self, ...):
        self._done_event: threading.Event = threading.Event()  # eager

# AFTER: Event is allocated on first waiter; no-op for
# fire-and-forget callers
class TaskHandle:
    def __init__(self, ...):
        self._done_event: threading.Event | None = None

    def _ensure_event(self) -> threading.Event:
        """Called under self._lock on the slow path only."""
        if self._done_event is None:
            self._done_event = threading.Event()
            # Pre-signal if already done (catches the race where
            # completion happened before anyone waited)
            if self._is_terminal():
                self._done_event.set()
        return self._done_event

    def mark_done(self) -> None:
        """Called by the producer under the shared results lock."""
        if self._done_event is not None:
            self._done_event.set()  # no-op if no waiter showed up
```

Measured on bookstore_api task_queue enqueue:
**60,478 rps → 75,351 rps (+24.6 %)** on the fire-and-forget path.
`threading.Event.__init__` + `threading.Condition.__init__` were
9.4 % of enqueue self time — eliminated for callers who never wait.

Apply this pattern when: (a) the primitive is cheap to create but
not free, (b) most instances never use it, and (c) creation can
safely be deferred behind a lock that the producer already holds.

---

## Debugging ORM-generated SQL — `QuerySet.to_sql()`

Use `to_sql()` whenever you can't tell what SQL the ORM is producing
from looking at the chained calls. Sync, read-only, no DB access —
returns a typed `CompiledQuery` dataclass with `(sql, params, kind)`.

```python
qs = (
    Forum.objects.filter(is_public=True)
    .exclude(Exists(StatusEvent.objects.filter(entity_id=OuterRef("id"))))
    .select_related("owner_id")
    .order_by("name")
)

print(qs.to_sql())
# -- SELECT (1 params)
# SELECT id, name, ... FROM hn_forums LEFT JOIN ...
#   WHERE is_public = $1 AND NOT EXISTS (...)
#   ORDER BY hn_forums.name ASC
# -- params:
# --   $1 = True

print(qs.to_sql().inlined())  # $N substituted with Python repr — READ ONLY
```

UPDATE / DELETE preview is also supported:

```python
qs.filter(id=42).to_sql(
    kind="update",
    update_values={"is_active": False},
    update_returning=["id", "name"],
)
qs.filter(is_deleted=True).to_sql(kind="delete")
```

**When to use**: when ORM behavior surprises you, when verifying
`select_related` / `join_related` / `Exists` / `with_cte` SQL emission
matches your mental model, when copy-pasting a query into `psql` to
run `EXPLAIN ANALYZE`, when writing unit tests that assert on query
shape without needing a live DB.

**Never execute the `.inlined()` output.** It uses Python `repr()` for
values, which is not a safe SQL literal encoding. The inlined view is
read-only debugging only.

---

## `Exists` / `NotExists` / `OuterRef` — Correlated Subquery Filters

Use `Exists(inner_qs)` inside `filter()` or `exclude()` to express
correlated subquery conditions. `OuterRef("field")` references the
outer query's column at the inner queryset's filter position.

```python
from hyperdjango.expressions import Exists, OuterRef
from hyperdjango.timeline import StatusEvent

# "Public forums that do NOT have an active 'hidden' status event"
visible = (
    await Forum.objects.filter(is_public=True)
    .exclude(
        Exists(
            StatusEvent.objects.filter(
                entity_type="forum",
                entity_id=OuterRef("id"),  # ← hn_forums.id
                status="hidden",
                ended_at=None,
            )
        )
    )
    .all()
)
```

Compiles to:

```sql
SELECT * FROM hn_forums
WHERE is_public = $1
  AND NOT EXISTS (
      SELECT * FROM hyper_status_events
      WHERE entity_type = $2
        AND entity_id = hn_forums.id
        AND status = $3
        AND ended_at IS NULL
  )
```

`~Exists(...)` produces a `NotExists` instance — equivalent to passing
an `Exists` to `exclude()`. `OuterRef` may appear inside `Q(...)`
subexpressions on the inner queryset, not just at the top level.

**When to use**: visibility filters, "has at least one related row"
checks, "missing related row" filters. Prefer `Exists` over
`filter(id__in=Subquery(...))` for correlated cases — it's faster
because PostgreSQL can short-circuit after the first matching inner
row.

---

## `with_cte` — Common Table Expressions (incl. `WITH RECURSIVE`)

Use `QuerySet.with_cte(name, body_sql, *params, recursive=True)` to
prepend a CTE clause to an ORM query. The CTE body is raw SQL (with
`{idx}` placeholders for parameterized values) because recursive
CTE bodies with self-joining anchor + UNION ALL branches can't be
cleanly expressed via pure ORM constructs. The OUTER query stays
full ORM: filters, ordering, limits, `join_related`, etc.

```python
# Walk the tenant ancestor chain with a recursive CTE
rows = await (
    Tenant.objects.values("id")
    .with_cte(
        "ancestors",
        "SELECT id, parent_id FROM hyper_tenants WHERE id = {idx} "
        "UNION ALL "
        "SELECT t.id, t.parent_id FROM hyper_tenants t "
        "JOIN ancestors a ON t.id = a.parent_id",
        tenant_id,
        recursive=True,
    )
    .where_raw("id IN (SELECT id FROM ancestors)")
    .all()
)
```

Compiles to:

```sql
WITH RECURSIVE ancestors AS (
    SELECT id, parent_id FROM hyper_tenants WHERE id = $1
    UNION ALL
    SELECT t.id, t.parent_id FROM hyper_tenants t
    JOIN ancestors a ON t.id = a.parent_id
)
SELECT id FROM hyper_tenants WHERE id IN (SELECT id FROM ancestors)
```

Multiple `.with_cte()` calls accumulate into a comma-separated `WITH`
list in declaration order. Any `recursive=True` clause promotes the
entire `WITH` to `WITH RECURSIVE` (PostgreSQL semantics — recursive
is per-WITH, not per-clause).

**When to use**: hierarchical walks (RBAC role trees, tenant
ancestors, metering account rollup, org charts). **When NOT to
use**: simple JOINs that `select_related` can handle, filters that
`Exists` can handle without needing a CTE at all.

---

## `select_related` — JOIN Eager Loading

Avoid N+1 queries by loading FK relations in a single JOIN:

```python
# BAD: N+1 — one query per book to get author
books = await Book.objects.all()
for book in books:
    author = await Author.objects.filter(id=book.author_id).first()

# GOOD: single JOIN query
books = await Book.objects.select_related("author_id").all()
for book in books:
    print(book.author_id.name)  # Already loaded, no extra query
```

`select_related` replaces the FK integer with the full model instance via LEFT JOIN.

---

## DataLoader — Batch + Deduplicate Async Lookups

`select_related` handles the common FK case, but when you need to enrich a list of
parent objects with data from multiple unrelated tables (author names + category
names + review counts for 50 books), `DataLoader` collects per-key `load()` calls
into a single batched query.

```python
from hyperdjango.dataloader import DataLoader
from hyperdjango.rest import ModelViewSet, action


class BookViewSet(ModelViewSet):
    @action(methods=["GET"], detail=False, url_path="enriched")
    async def list_enriched(self, request, **kwargs):
        # Batch fns take a list of keys and must return a list in the SAME ORDER
        async def _batch_authors(keys: list[int]) -> list[Author | None]:
            authors = await Author.objects.filter(id__in=keys).all()
            by_id = {a.id: a for a in authors}
            return [by_id.get(k) for k in keys]

        async def _batch_categories(keys: list[int]) -> list[Category | None]:
            cats = await Category.objects.filter(id__in=keys).all()
            by_id = {c.id: c for c in cats}
            return [by_id.get(k) for k in keys]

        # Create per-request loaders — cache is request-scoped
        author_loader = DataLoader(batch_fn=_batch_authors)
        category_loader = DataLoader(batch_fn=_batch_categories)

        paginator = self.pagination_class()
        qs = self.filter_queryset(self.get_queryset())
        books = await paginator.paginate_queryset(qs, request)

        # load_many batches all keys into ONE call to batch_fn
        authors = await author_loader.load_many([b.author_id for b in books])
        categories = await category_loader.load_many([b.category_id for b in books])

        data = []
        for book, author, category in zip(books, authors, categories):
            d = book.to_dict()
            d["author_name"] = author.name if author else ""
            d["category_name"] = category.name if category else ""
            data.append(d)
        return paginator.get_paginated_response(data)
```

**Key rules:**

- **Create loaders per-request, not globally** — the cache is shared between
  `load()` calls, so a global loader would serve stale data across requests.
- **Batch function return order must match input key order** — map through a
  dict keyed by id so missing keys become `None` at the correct position.
- **Concurrent awaits batch into one call** — `await asyncio.gather(loader.load(1),
loader.load(2))` produces a single batch call with `[1, 2]`. Sequential
  `await` calls do NOT batch (they run in separate event loop ticks).
- **Use `load_many()` when you already have the key list** — it internally
  gathers the loads.

See `services/bookstore_api/app.py` `list_enriched` action and
`scripts/test_e2e_bookstore_api.py` for a working end-to-end example.

---

## FileField Lifecycle — Upload, Download, Cascade Delete

`FileField` and `ImageField` from `hyperdjango.models` store a file path string
while handing off the actual bytes to a `Storage` backend (`FileSystemStorage`,
`MemoryStorage`, or a custom adapter). Use `save_uploaded_file` /
`delete_uploaded_file` to keep the database row and the storage in sync.

```python
from hyperdjango.models import (
    Field,
    FileField,
    ImageField,
    Model,
    delete_uploaded_file,
    save_uploaded_file,
)
from hyperdjango.storage import MemoryStorage  # or FileSystemStorage
from hyperdjango.mixins import TimestampMixin

_storage = MemoryStorage(base_url="/files/")


class Attachment(TimestampMixin, Model):
    class Meta:
        table = "attachments"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    document: str = FileField(upload_to="documents/")
    thumbnail: str = ImageField(upload_to="thumbnails/")


@app.post("/api/attachments")
async def create_attachment(request):
    files = await request.files()
    form = await request.form()

    doc = files.get("document")
    if not doc or not doc.filename:
        raise HTTPException(400, "document required")

    att = Attachment(title=form.get("title", "Untitled"))

    # save_uploaded_file: persists bytes, sets att.document to the stored path
    await save_uploaded_file(att, "document", doc.data, doc.filename, _storage)

    # Optional thumbnail — ImageField validates extensions, cleanup on failure
    thumb = files.get("thumbnail")
    if thumb and thumb.filename:
        try:
            await save_uploaded_file(
                att, "thumbnail", thumb.data, thumb.filename, _storage
            )
        except ValueError as e:
            # Cleanup the already-saved document to avoid orphans
            await delete_uploaded_file(att, "document", _storage)
            raise HTTPException(400, str(e))

    await att.save()  # Now persist the database row — files are already on disk
    return Response.json(
        {"id": att.id, "document_url": _storage.url(att.document)}, status=201
    )


@app.delete("/api/attachments/{att_id:int}")
async def delete_attachment(request, att_id: int):
    att = await Attachment.objects.filter(id=att_id).first()
    if not att:
        raise HTTPException(404, "Not found")

    # Delete files FIRST — if DB delete fails later we re-do file delete (idempotent).
    # If we deleted DB first and file delete failed, we'd have orphan files.
    if att.document:
        await delete_uploaded_file(att, "document", _storage)
    if att.thumbnail:
        await delete_uploaded_file(att, "thumbnail", _storage)

    await Attachment.objects.filter(id=att_id).delete()
    return Response.json({"deleted": True})
```

**Key rules:**

- **Save files before the database row** — if the DB save fails, you can delete
  the orphan file. The reverse leaves broken references.
- **Delete files before the database row** — if the file delete fails, you still
  have the row and can retry. The reverse leaves orphan files on disk.
- **Always clean up on validation failure** — `ImageField` raises `ValueError`
  on bad extensions; catch it and delete any already-saved files (see thumbnail
  example above).
- **Use `MemoryStorage` for tests, `FileSystemStorage` for production** — the
  `Storage` interface is identical, so apps can swap via `set_storage()` at
  startup.

See `services/forms_demo/app.py` `Attachment` + `/api/attachments` endpoints
and `scripts/test_e2e_forms_demo.py` for the full upload/download/delete test flow.

---

## Security Audit Log — Event-Sourced Compliance Tracking

`SecurityLog` (from `hyperdjango.security`) is a PostgreSQL UNLOGGED table
tracking authentication, authorization, rate-limit, session, and suspicious
activity events. Wire it into your auth flow for compliance tracking and
real-time abuse detection.

```python
from hyperdjango.database import get_db
from hyperdjango.security import SecurityEvent, SecurityLog, set_security_log

_security_log: SecurityLog | None = None


@app.on_startup
async def _init_security_log():
    global _security_log
    _security_log = SecurityLog(get_db())
    await _security_log.ensure_table()
    set_security_log(_security_log)


@app.post("/auth/login")
async def login(request):
    data = await request.json()
    user = await User.objects.filter(username=data.get("username")).first()
    if user is None or not verify_password(
        data.get("password", ""), user.password_hash
    ):
        if _security_log:
            await _security_log.log_from_request(
                SecurityEvent.LOGIN_FAILED,
                request,
                detail=f"user={data.get('username', '?')}",
            )
        raise HTTPException(401, "Invalid credentials")

    if _security_log:
        await _security_log.log_from_request(
            SecurityEvent.LOGIN_SUCCESS,
            request,
            user_id=user.id,
        )
    # ... create session and return
```

**Query patterns:**

```python
# Recent events across all types
events = await _security_log.get_recent(limit=50)

# All LOGIN_FAILED events in the last hour
failed = await _security_log.get_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)

# Count failed logins from a single IP (brute-force detection)
count = await _security_log.count_by_ip(
    "1.2.3.4", SecurityEvent.LOGIN_FAILED, since_hours=1
)
if count > 10:
    # Lock out / alert
    ...

# All events for a specific user (compliance / audit trail)
user_events = await _security_log.get_for_user(user.id)
```

**What to log:**

- `LOGIN_SUCCESS` / `LOGIN_FAILED` on every auth attempt
- `LOGOUT` when sessions end
- `PASSWORD_CHANGED` on successful password updates
- `PERMISSION_DENIED` when a guard rejects a request
- `CSRF_VIOLATION` when CSRF middleware blocks a submission
- `RATE_LIMIT_HIT` when the rate limiter returns 429
- `SESSION_FIXATION_ATTEMPT` when session auth detects a fixation pattern

The table uses UNLOGGED storage (no WAL) for high-throughput writes; treat the
log as "durable for querying during the lifetime of the cluster" but not as a
permanent audit system — archive to cold storage on a schedule if regulators
require long retention.

See `services/rest_api/app.py` login handler + `/api/security/recent` and
`/api/security/failed-logins` endpoints for a working example.

---

## Full-Text Search ��� ORM Expressions

Use `SearchVector`, `SearchQuery`, `SearchRank` from `postgres.py` as ORM Expression objects in `annotate()`:

```python
from hyperdjango.postgres import SearchVector, SearchQuery, SearchRank

vector = SearchVector(["title", "body"], config="english")
query = SearchQuery("python web framework", search_type="websearch")

results = (
    await Article.objects.annotate(
        rank=SearchRank(vector, query),
    )
    .order_by("-rank")
    .limit(20)
    .all()
)
```

For trigram similarity (fuzzy matching):

```python
from hyperdjango.postgres import TrigramSimilarity

results = (
    await Article.objects.annotate(
        sim=TrigramSimilarity("title", "pythn"),  # typo-tolerant
    )
    .order_by("-sim")
    .limit(10)
    .all()
)
```

For simple single-field search, use the `__search` lookup:

```python
results = await Article.objects.filter(title__search="python").all()
```

---

## Security

### Auth enforcement on write routes

Every route that creates, updates, or deletes data must verify authentication:

```python
def require_auth(request):
    user = request.user
    if user is None or not isinstance(user, dict) or "id" not in user:
        raise HTTPException(401, "Authentication required")
    return user


@app.post("/api/items/")
async def create_item(request):
    user = require_auth(request)
    # ... create item ...
```

### Tenant isolation in raw SQL

When using raw SQL in a multi-tenant app, always include `tenant_id` in WHERE clauses — especially on UPDATE and DELETE:

```python
# GOOD: tenant_id in WHERE prevents cross-tenant writes
await db.query_one(
    "UPDATE tasks SET status = $1 WHERE id = $2 AND tenant_id = $3 RETURNING *",
    new_status,
    task_id,
    tenant.tenant_id,
)

# BAD: no tenant_id — any tenant's task can be modified
await db.query_one(
    "UPDATE tasks SET status = $1 WHERE id = $2 RETURNING *",
    new_status,
    task_id,
)
```

### Use REST framework exceptions in ViewSets

ViewSets catch `APIException` subclasses, not `HTTPException`:

```python
from hyperdjango.rest import NotFound, ValidationError

# GOOD: ViewSet handles these correctly
raise NotFound("Book not found")
raise ValidationError("Invalid data", errors={"field": ["error msg"]})

# BAD: ViewSet catches this as unhandled → 500
raise HTTPException(404, "Not found")
```

### Staff checks on privileged actions

```python
@action(methods=["POST"], detail=True, url_path="publish")
async def publish(self, request, **kwargs):
    user = request.user
    if not isinstance(user, dict) or not user.get("is_staff"):
        return Response.json({"detail": "Staff access required"}, status=403)
    # ... publish ...
```

---

## Async Patterns

### Never block async handlers

Use `run_in_executor` for blocking operations in async route handlers:

```python
# GOOD: non-blocking wait
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, handle.result, 10.0)

# BAD: blocks a server thread for up to 10 seconds
result = handle.result(timeout=10.0)
```

---

## Task Queue

### Wire lifecycle hooks to tasks

```python
def _on_success(result):
    logger.info("Task succeeded: {result}", result=result)


def _on_retry(exc, attempt):
    logger.warning("Retry attempt {attempt}: {err}", attempt=attempt, err=str(exc))


@app.task(
    max_retries=3,
    retry_delay=1.0,
    retry_on=(ConnectionError,),
    on_success=_on_success,
    on_retry=_on_retry,
)
async def fetch_data(url): ...
```

### Use TaskGroup.run() for parallel batch execution

```python
group = TaskGroup()
for item in items:
    group.add(process_task, item)

# Wait for all tasks to complete with timeout
results = group.run(timeout=30.0)
```

---

## General

### No dead code

Remove unused imports, unused models, unused functions. If something isn't called, delete it.

### Read the platform before implementing

Before building any feature, audit existing platform code for existing solutions. HyperDjango has 5,000+ lines of REST framework, 1,000+ lines of tenancy, 800+ lines of task queue, etc. The pattern you need probably already exists.

### Every change must be tested

Build → test → verify. Never guess at a fix and move on. Save output to files, read the full output, fix what actually failed.

### Profile before you optimize — always

**Rule**: Every performance change must be preceded by a profile that
identifies the hotspot and followed by a re-profile that proves the fix
worked. No "I think this will be faster" commits.

This is a **hard rule** because intuition about bottlenecks is wrong most of
the time. A function can look hot in a micro-benchmark and contribute nothing
at production load because it's never in the top 30 by self-time. Meanwhile
the real hotspot is often a small Python method (MRO walks, repeated dict
construction, dynamic attribute lookup) sitting unnoticed in a hot loop —
fixable with a metaclass cache for a double-digit-percent throughput win.

#### The Stability Rule

Before quoting **any** performance number, enforce these four
conditions. Unstable measurements lie.

1. **Each run is ≥ 5 seconds wall-clock.** Short runs are dominated
   by CPU frequency scaling, scheduler noise, and prepared-statement
   cache warmup. At e.g. 1500 rps, `ITERATIONS` must be ≥ 7500.
2. **Multi-run median, not single-run.** Run N = 3 (minimum) and
   take the median. Single-run numbers are unreliable.
3. **Jitter budget 5 %.** Compute
   `(max_rps - min_rps) / median_rps * 100 / 2`. If it's above 5 %,
   something is off — re-run, or investigate environmental noise.
   Never quote a number with jitter > 5 %.
4. **Release Zig build.** `uv run hyper-build --install --release`
   before profiling. Debug builds emit MB of trace output per
   request that dominates everything else.

`scripts/profile_list_cprofile.py` and all its siblings encode these
rules directly. When adding a new profile script, copy the existing
harness — don't reinvent the stability discipline.

Beware: **matched-state baselines matter**. If the machine's thermal
state, background process load, or PostgreSQL autovacuum state
drifts between your "before" run and your "after" run, the delta is
noise. Take a fresh baseline immediately before the change and
re-run the baseline immediately after to control for environment.
At the wire-speed benchmark level, variance within the same code
state can exceed ±20 %; per-call cProfile self-time numbers are
much more stable because they exclude DB I/O.

#### The six-phase workflow

1. **Release build**: `uv run hyper-build --install --release` — never profile a Debug build
2. **Baseline**: `uv run python scripts/bench_load_orm.py`, save `logs/load_orm_baseline.json`
3. **cProfile the target endpoint**: copy `scripts/profile_list_cprofile.py`,
   change the URL, run it. Read top-15 by **self-time**, not cumulative
4. **Identify target**: look for high `tottime × call_count` in pure Python
   code. If your assumed hotspot is not in the top 30, **stop working on it**
5. **Fix**: favor class-level caches, eliminate MRO walks in hot loops, avoid
   allocations in per-field code paths. Do NOT rewrite in Zig unless cProfile
   proves Python overhead is the bottleneck AND FFI overhead < savings
6. **Verify**: re-run cProfile + bench*load_orm. Both must show improvement.
   Record before/after numbers in `logs/profile*<target>\_report.md`

#### Tools (ranked by cost/value)

| Tool                                          | Use it for                                       |
| --------------------------------------------- | ------------------------------------------------ |
| `scripts/profile_list_cprofile.py` (template) | In-process cProfile, fastest path to top-15      |
| `scripts/profile_hypernews_cprofile.py`       | Multi-endpoint platform hotspots                 |
| `scripts/profile_admin_cprofile.py`           | HyperAdmin changelist / CRUD path                |
| `scripts/profile_openapi_cprofile.py`         | OpenAPI spec generation                          |
| `scripts/profile_queue_channel_cprofile.py`   | task_queue enqueue/execute + channel.publish     |
| `scripts/bench_bookstore_wrk.py`              | Wire-speed bookstore REST framework              |
| `scripts/bench_hypernews_wrk.py`              | Wire-speed hypernews (templates + N-query pages) |
| `scripts/bench_pool_queue_depth.py`           | pg.zig pool acquire contention under wrk fanout  |
| `scripts/bench_db_query_dicts.py`             | Row-shape microbench: tuples vs dicts            |
| `scripts/bench_load_orm.py`                   | Production load baseline                         |
| `scripts/_wrk_bench.py`                       | Shared wrk harness — import to build new ones    |

All wrk scripts respect `HYPER_POOL_SIZE` (forwarded to the
AppRunner subprocess via env var). All cProfile scripts use
per-endpoint iteration counts tuned for the ≥ 5 s stability rule.

Full methodology, rationale, worked example, and anti-patterns in
`docs/profiling.md` "Profile-Driven Optimization Workflow" section.

Anti-patterns:

- **Profiling a Debug build** — Zig trace output is megabytes of stderr that
  dominate wall time. `uv run hyper-build --release` first, always
- **Measuring one endpoint, optimizing another** — hotspots are endpoint-specific
- **Chasing microseconds on a millisecond call** — a 2μs function in a
  5ms request is 0.04% of total time. Fix the big numbers first
- **Optimizing without a re-profile** — you must prove the fix worked or
  you're guessing about the guess
- **Single-run before/after claims** — wire-speed wrk variance alone can be
  ±20 % between back-to-back runs. Take multi-run medians and demand
  jitter < 5 % before quoting a delta
- **Trusting wire-speed for small wins** — a 5 % wrk delta is almost
  certainly in the noise. For small structural wins, report the cProfile
  per-call self-time delta instead — it excludes DB variance and is much
  more stable
- **Profiling frames dominated by native FFI calls** — if `_db_query_dicts`
  is at the top of your self-time report, the time is mostly native DB
  roundtrip, not Python dict construction. Chase the caller pattern
  (query count, select_related, caching) instead of trying to shave the
  frame itself

## Q Objects — Composable Query Conditions

Use Q objects for OR, AND, and NOT conditions that can't be expressed with keyword filters:

```python
from hyperdjango.expressions import Q

# OR: match either condition
users = await User.objects.filter(Q(role="admin") | Q(role="moderator")).all()

# AND with NOT: active non-banned users
users = await User.objects.filter(Q(is_active=True) & ~Q(is_banned=True)).all()

# Nested: (OR) AND single
posts = await Post.objects.filter(
    (Q(category="python") | Q(category="web")) & Q(published=True)
).all()

# Mixed: Q + kwargs (kwargs are ANDed)
posts = await Post.objects.filter(
    Q(title__icontains="guide") | Q(title__icontains="tutorial"), published=True
).all()

# Q in exclude()
active = await User.objects.exclude(Q(status="banned") | Q(status="suspended")).all()
```

Q works with all lookups (`__gt`, `__icontains`, `__in`, etc.) and FK-spanning (`author__name="alice"`).

## Foreign Keys — Type-Safe Class References

Use Model classes directly in `foreign_key=` instead of raw table name strings:

```python
class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field()


class Post(Model):
    class Meta:
        table = "posts"

    id: int = Field(primary_key=True, auto=True)
    author_id: int = Field(foreign_key=User)  # Class ref — resolved at metaclass time
    title: str = Field()
```

For **forward references** (model defined later), use a PascalCase string:

```python
class Comment(Model):
    class Meta:
        table = "comments"

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field(foreign_key=Post)
    parent_id: int | None = Field(default=None, foreign_key="Comment")  # Self-ref
```

Forward refs are automatically resolved when the referenced model class is registered.

## OneToOneField — Unique Foreign Keys

For one-to-one relationships (e.g., user ↔ profile), use `OneToOneField`:

```python
from hyperdjango.models import Field, Model, OneToOneField


class UserProfile(Model):
    class Meta:
        table = "profiles"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = OneToOneField(User, related_name="profile")
    bio: str = Field(default="")
```

This creates a FK column with a UNIQUE constraint. Query via standard filter/select_related.

## Single-Table Inheritance (STI)

When multiple model types share the same table, use STI with a discriminator column:

```python
class Vehicle(Model):
    class Meta:
        table = "vehicles"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    type: str = Field(default="vehicle")  # discriminator


class Car(Vehicle):
    class Meta:
        sti = True
        sti_type = "car"

    doors: int = Field(default=4)


class Truck(Vehicle):
    class Meta:
        sti = True
        sti_type = "truck"

    payload_tons: int = Field(default=0)
```

- `Car.objects.all()` auto-filters to `WHERE type = 'car'`
- `Vehicle.objects.all()` returns all rows (cars + trucks + base vehicles)
- `car.save()` auto-sets `type = 'car'`
- All child columns are nullable on the shared table

## Intent-Driven Access Control

When a resource requires different levels of access for different operations (read vs write vs admin), use a purpose-based resolver with an intent enum. This centralizes scattered manual checks (null check, membership, archived, locked, role) into one call.

### Pattern: resolve_resource with Intent Enum

```python
from enum import Enum
from dataclasses import dataclass


class ForumIntent(Enum):
    READ = "read"  # View forum, list posts
    WRITE_POST = "write_post"  # Submit post (rejects archived+locked)
    WRITE_COMMENT = "write_comment"  # Add comment (rejects archived+locked)
    MODERATE = "moderate"  # Pin, flair, etc. (requires mod/admin)
    ADMIN = "admin"  # Settings, automod, member management


@dataclass
class ForumAccess:
    forum: Forum
    is_member: bool
    is_mod: bool
    membership: ForumMember | None


async def resolve_forum(request, forum_name: str, intent: ForumIntent) -> ForumAccess:
    """Fetch forum and enforce intent-specific access control in ONE call."""
    forum = await get_forum_by_name(forum_name)
    if not forum:
        raise HTTPException(404, "Forum not found")

    uid = get_uid_or_none(request)
    # ... resolve membership ...

    if not forum.is_public and not is_member:
        raise HTTPException(403, "This forum is private")

    if intent in _WRITE_INTENTS:
        if forum.is_archived:
            raise HTTPException(403, "This forum is archived")

    if intent == ForumIntent.MODERATE:
        if not is_mod:
            raise HTTPException(403, "Moderator access required")

    return ForumAccess(forum, is_member, is_mod, membership)
```

Usage in route handlers:

```python
@app.post("/f/{forum_name}/submit")
async def submit_post(request, forum_name: str):
    access = await resolve_forum(request, forum_name, ForumIntent.WRITE_POST)
    # All checks passed — access.forum is guaranteed writable
    # access.membership is available for further logic
    ...


@app.post("/f/{forum_name}/settings")
async def forum_settings(request, forum_name: str):
    access = await resolve_forum(request, forum_name, ForumIntent.ADMIN)
    # Only forum admins and site staff reach this point
    ...
```

**Why this matters:**

- **One call, all checks.** No scattered `if forum.is_archived`, `if not is_member`, `if not is_mod` across 30+ routes
- **Intent documents purpose.** Reading the route handler immediately tells you what access level is required
- **Returns context.** The `ForumAccess` result carries the membership and role info, avoiding redundant queries downstream
- **Never forget a check.** Adding a new constraint (e.g., rate-limited forums) only changes the resolver, not every route

**Never do this:**

```python
# BAD: scattered checks duplicated across routes
@app.post("/f/{forum_name}/submit")
async def submit_post(request, forum_name: str):
    forum = await get_forum_by_name(forum_name)
    if not forum:
        raise HTTPException(404, "Forum not found")
    if not forum.is_public:
        member = await ForumMember.objects.filter(...).first()
        if not member:
            raise HTTPException(403, "Private")
    if forum.is_archived:
        raise HTTPException(403, "Archived")
    if forum.is_locked:
        raise HTTPException(403, "Locked")
    # ... 10 more lines before any business logic
```

## Token Signing — Sessions & API Keys

Use `TokenEngine` for all session cookies and `SignedAPIKeyMixin` for database-backed API keys.

### Session Cookies with TokenEngine

Every app with `SessionAuth` should configure a `TokenEngine`:

```python
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.signing import SigningKey, TokenEngine

_session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=os.environ.get("SESSION_SIGNING_KEY", "app-session-2026-q2"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=os.environ.get("SESSION_SECRET", "dev-only-change-me"),
    token_engine=_session_engine,
)
app.use(auth)
```

This signs session cookies with HMAC + per-token salt + XOR obfuscation. Forged cookies are rejected without touching the database.

### API Key Models with SignedAPIKeyMixin

For database-backed API keys, use `SignedAPIKeyMixin` instead of manual hashing:

```python
from hyperdjango.signing import SignedAPIKeyMixin, SigningKey
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field


class APIKey(SignedAPIKeyMixin, TimestampMixin):
    class Meta:
        table = "api_keys"

    class TokenConfig:
        keys = [SigningKey(secret="apikey-2026-q2", version=1)]
        key_display_prefix = "sk_myapp_"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field()
    name: str = Field(default="")
```

Generate and verify keys:

```python
# Generate (show raw_key to user ONCE)
result = await APIKey.generate(user_id=user.id, name="Production Key")
# result.raw_key = "sk_myapp_1rKx9mP4qR7wN8..."

# Verify (HMAC-first, then DB lookup)
api_key = await APIKey.verify(raw_key)
if api_key is None:
    raise HTTPException(401, "Invalid API key")
```

**Never do this:**

```python
# BAD: manual hashing, no HMAC verification, DB hit on every request
raw_key = f"sk-{secrets.token_hex(32)}"
key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

# BAD: forged keys hit the database before being rejected
api_key = await APIKey.objects.filter(
    key_hash=hashlib.sha256(incoming.encode()).hexdigest()
).first()
```

### Key Rotation

Add new keys by prepending to the list. Old tokens remain valid until their key is removed:

```python
_session_engine = TokenEngine(
    keys=[
        SigningKey(secret="new-key-2026-q3", version=2),  # signs new tokens
        SigningKey(secret="old-key-2026-q2", version=1),  # still verifies old tokens
    ]
)
```

### Stateless Data Tokens

For email verification, CSRF, or short-lived auth without a database:

```python
engine = TokenEngine(keys=[SigningKey(secret="app-key", version=1)])

# Encode data with 1-hour TTL
token = engine.encode_data({"user_id": 42, "action": "verify_email"}, ttl=3600)

# Any server can verify + decode without DB
data = engine.decode_data(token)
# {"user_id": 42, "action": "verify_email"} or None if expired/tampered
```
