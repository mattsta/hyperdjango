# Model Mixins

Production data pattern mixins that add common behaviors to Model classes. All are abstract and composable.

## Quick Start

```python
from hyperdjango import Model, Field
from hyperdjango.mixins import (
    TimestampMixin,
    SoftDeleteMixin,
    OwnershipMixin,
    VersionedMixin,
)


class Article(TimestampMixin, SoftDeleteMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field()


article = Article(title="Hello", body="World")
await article.save()
print(article.created_at)  # Auto-set on INSERT
print(article.updated_at)  # Auto-set on every save
```

Mixins use Python's standard multiple inheritance with `class Meta: abstract = True`, so they never create their own database tables. They inject fields, override `save()` and `delete()` hooks, and attach custom manager methods to the model's `objects` QuerySet.

## Available Mixins

| Mixin             | Fields Added                         | Purpose                           |
| ----------------- | ------------------------------------ | --------------------------------- |
| `TimestampMixin`  | `created_at`, `updated_at`           | Automatic datetime tracking       |
| `SoftDeleteMixin` | `is_deleted`, `deleted_at`           | Mark-as-deleted instead of DROP   |
| `OwnershipMixin`  | `created_by`, `updated_by`           | Track which user mutated a record |
| `VersionedMixin`  | `version`, `is_current`, `entity_id` | Append-only audit trail           |

---

## TimestampMixin

Adds `created_at` and `updated_at` fields with automatic management. Timestamps are always set server-side in Python (not via database `DEFAULT`) so the values are available on the model instance immediately after `save()`.

```python
class Article(TimestampMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
```

**Fields added:**

| Field        | Type               | Behavior                                   |
| ------------ | ------------------ | ------------------------------------------ |
| `created_at` | `datetime \| None` | Set on first `save()`, never changed after |
| `updated_at` | `datetime \| None` | Set on every `save()` (INSERT and UPDATE)  |

Both fields use `datetime.datetime.now(datetime.timezone.utc)` so they are always timezone-aware UTC timestamps.

### Timestamp behavior on save

```python
article = Article(title="Hello")
await article.save()
assert article.created_at is not None
assert article.updated_at is not None

first_updated = article.updated_at
article.title = "Updated"
await article.save()
assert article.created_at == article.created_at  # Unchanged
assert article.updated_at > first_updated  # Bumped
```

### How it works internally

On `save()`, the mixin's `save()` hook runs before the model's own `save()`:

1. If `created_at` is `None` (first insert), set `created_at = now()`.
2. Always set `updated_at = now()`.
3. Call `super().save()` to perform the actual INSERT or UPDATE.

This means you can override `created_at` on first save if needed (e.g., importing historical data), but `updated_at` is always overwritten.

### Querying by timestamp

```python
from datetime import datetime, timezone, timedelta

# Articles created in the last 24 hours
yesterday = datetime.now(timezone.utc) - timedelta(days=1)
recent = await Article.objects.filter(created_at__gte=yesterday).all()

# Articles not modified in 30 days (stale content)
stale_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
stale = await Article.objects.filter(updated_at__lt=stale_cutoff).all()

# Order by most recently updated
latest = await Article.objects.order_by("-updated_at").all()
```

### Database column types

The migration system generates these as `TIMESTAMPTZ` columns in PostgreSQL:

```sql
created_at TIMESTAMPTZ NULL,
updated_at TIMESTAMPTZ NULL
```

---

## SoftDeleteMixin

Marks records as deleted instead of removing them from the database. Default QuerySets auto-exclude soft-deleted rows, so your application code works transparently. The actual row stays in the database for audit trails, undo functionality, and compliance.

```python
class Article(SoftDeleteMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
```

**Fields added:**

| Field        | Type               | Default | Description                        |
| ------------ | ------------------ | ------- | ---------------------------------- |
| `is_deleted` | `bool`             | `False` | Whether the record is soft-deleted |
| `deleted_at` | `datetime \| None` | `None`  | UTC timestamp of soft deletion     |

### Methods

| Method                        | Description                                             |
| ----------------------------- | ------------------------------------------------------- |
| `await article.delete()`      | Soft delete: sets `is_deleted=True`, `deleted_at=now()` |
| `await article.hard_delete()` | Permanent `DELETE FROM` the database                    |
| `await article.restore()`     | Restore: sets `is_deleted=False`, `deleted_at=None`     |

### Soft delete vs hard delete

```python
article = Article(title="Temporary")
await article.save()

# Soft delete — row stays in database
await article.delete()
assert article.is_deleted is True
assert article.deleted_at is not None

# Row is still in the database, just hidden from default queries
all_rows = await Article.objects.with_deleted().all()
assert article in [a for a in all_rows if a.id == article.id]

# Hard delete — row is permanently removed
await article.hard_delete()
# Row is gone from the database entirely
```

### QuerySet filtering

The SoftDeleteMixin installs a custom manager that automatically excludes soft-deleted rows from all default queries. You never have to remember to add `is_deleted=False` to your filters.

```python
# Default: excludes soft-deleted rows (most common case)
articles = await Article.objects.all()
# Equivalent to: SELECT * FROM articles WHERE is_deleted = FALSE

# Include soft-deleted rows (admin views, reporting)
all_articles = await Article.objects.with_deleted().all()
# Equivalent to: SELECT * FROM articles

# Only soft-deleted rows (trash/recycle bin view)
deleted = await Article.objects.only_deleted().all()
# Equivalent to: SELECT * FROM articles WHERE is_deleted = TRUE
```

These manager methods compose with all other QuerySet methods:

```python
# Filter within soft-deleted records
recently_deleted = (
    await Article.objects.only_deleted()
    .filter(deleted_at__gte=yesterday)
    .order_by("-deleted_at")
    .all()
)

# Count active vs deleted
active_count = await Article.objects.count()
deleted_count = await Article.objects.only_deleted().count()
```

### Restore

```python
article = await Article.objects.with_deleted().get(id=42)
await article.restore()
assert article.is_deleted is False
assert article.deleted_at is None
# Article is now visible in default queries again
```

### Purge (bulk hard delete)

To permanently remove all soft-deleted records older than a threshold:

```python
from datetime import datetime, timezone, timedelta

cutoff = datetime.now(timezone.utc) - timedelta(days=90)
old_deleted = await Article.objects.only_deleted().filter(deleted_at__lt=cutoff).all()

for article in old_deleted:
    await article.hard_delete()
```

### Signals

Signals (`pre_delete`, `post_delete`) are fired on soft delete, not just hard delete. This means your signal receivers run whether the delete is soft or hard. The signal kwargs include `soft=True` or `soft=False` so receivers can distinguish:

```python
from hyperdjango.signals import post_delete, receiver


@receiver(post_delete, sender=Article)
async def on_article_delete(sender, instance, soft, **kwargs):
    if soft:
        await log_event("article_soft_deleted", instance.id)
    else:
        await log_event("article_hard_deleted", instance.id)
```

---

## OwnershipMixin

Tracks who created and last modified a record. Stores user IDs (integers) rather than foreign key references, so it works with any authentication system.

```python
class Article(OwnershipMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
```

**Fields added:**

| Field        | Type          | Behavior                                                   |
| ------------ | ------------- | ---------------------------------------------------------- |
| `created_by` | `int \| None` | User ID set on first `save_as()` call, never changed after |
| `updated_by` | `int \| None` | User ID set on every `save_as()` call                      |

Both fields are `editable=False`: they are set only by `save_as()` (or privileged
direct assignment), **never** writable through a `ModelSerializer`. A request body
of `{"created_by": <other_user_id>}` is ignored — a user cannot forge ownership
and defeat the owner-based `ObjectPermission` check.

### The save_as() method

The mixin adds `save_as(user)` which accepts either a User object (anything with an `.id` attribute) or an integer user ID directly:

```python
article = Article(title="Hello")
await article.save_as(user)  # Accepts User object
assert article.created_by == user.id
assert article.updated_by == user.id

article.title = "Updated"
await article.save_as(other_user)
assert article.created_by == user.id  # Unchanged (first creator preserved)
assert article.updated_by == other_user.id  # Updated to latest editor

# Also works with raw user IDs
await article.save_as(42)
assert article.updated_by == 42
```

### Using save_as in views

```python
@app.post("/articles/")
async def create_article(request):
    data = await request.json()
    article = Article(title=data["title"], body=data["body"])
    await article.save_as(request.user)
    return Response.json({"id": article.id}, status=201)


@app.put("/articles/{id:int}/")
async def update_article(request, id: int):
    article = await Article.objects.get(id=id)
    data = await request.json()
    article.title = data["title"]
    await article.save_as(request.user)
    return Response.json({"id": article.id})
```

### Querying by ownership

```python
# All articles created by a specific user
my_articles = await Article.objects.filter(created_by=user.id).all()

# Articles modified by someone other than the creator
edited_by_others = await Article.objects.exclude(updated_by=F("created_by")).all()
```

### Plain save() still works

You can still call `save()` without a user. The ownership fields will be `None` for records that have never been saved via `save_as()`. This is useful for system-generated records or migrations.

```python
article = Article(title="System Generated")
await article.save()  # created_by and updated_by remain None
```

---

## VersionedMixin

Append-only versioning where every save creates a new version row. Old versions are preserved for a complete audit trail. This is the immutable history pattern -- you can always see what a record looked like at any point in time.

```python
class Document(VersionedMixin, Model):
    class Meta:
        table = "documents"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    content: str = Field()
```

**Fields added:**

| Field        | Type          | Description                                    |
| ------------ | ------------- | ---------------------------------------------- |
| `version`    | `int`         | Auto-incremented version number (starts at 1)  |
| `is_current` | `bool`        | `True` only on the latest version              |
| `entity_id`  | `int \| None` | Groups all versions of the same logical entity |

### How it works

1. **First save:** Inserts row with `version=1`, `is_current=True`, `entity_id=pk`
2. **Subsequent saves:** Marks old row as `is_current=False`, inserts new row with `version=N+1`, `is_current=True`, same `entity_id`
3. **Concurrent version conflicts** are prevented via `SELECT ... FOR UPDATE` -- if two requests try to update the same entity simultaneously, one will wait for the other to commit

```python
doc = Document(title="v1", content="First")
await doc.save()
assert doc.version == 1
assert doc.entity_id == doc.id

doc.title = "v2"
await doc.save()
assert doc.version == 2
# Original row (version=1) is preserved with is_current=False
# New row (version=2) is the current version
```

### QuerySet filtering

```python
# Default: only current versions (what you almost always want)
docs = await Document.objects.all()
# Equivalent to: SELECT * FROM documents WHERE is_current = TRUE

# All versions of all documents
all_versions = await Document.objects.with_versions().all()
# Equivalent to: SELECT * FROM documents
```

### Version history

```python
history = await doc.get_history()
# Returns list of all versions ordered by version ASC
for version in history:
    print(f"v{version.version}: {version.title}")
```

Output:

```
v1: v1
v2: v2
```

### Version diffing

Compare two versions to see what changed:

```python
history = await doc.get_history()
v1 = history[0]
v2 = history[1]

# Manual comparison
if v1.title != v2.title:
    print(f"title: '{v1.title}' -> '{v2.title}'")
if v1.content != v2.content:
    print(f"content changed")
```

### Reverting to a previous version

To restore an old version, load it from history and save it as a new version:

```python
history = await doc.get_history()
old_version = history[0]  # version 1

# Create new version with old content
doc.title = old_version.title
doc.content = old_version.content
await doc.save()
# Now version=3 with the same content as version=1
```

This preserves the full history -- you can see that version 3 was a revert, not an erasure of version 2.

---

## Composing Mixins

Mixins can be combined freely. They use cooperative multiple inheritance (`super()` calls chain correctly):

```python
class Article(TimestampMixin, SoftDeleteMixin, OwnershipMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()


article = Article(title="Hello")
await article.save_as(user)

# Has all features:
print(article.created_at)  # TimestampMixin
print(article.created_by)  # OwnershipMixin
await article.delete()  # SoftDeleteMixin (soft)
await article.restore()  # SoftDeleteMixin (restore)
```

### All four mixins together

```python
class AuditedDocument(
    TimestampMixin, SoftDeleteMixin, OwnershipMixin, VersionedMixin, Model
):
    class Meta:
        table = "audited_documents"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    content: str = Field()


doc = AuditedDocument(title="Contract", content="...")
await doc.save_as(user)

# TimestampMixin: created_at, updated_at set
# OwnershipMixin: created_by, updated_by set to user.id
# VersionedMixin: version=1, is_current=True
# SoftDeleteMixin: is_deleted=False

doc.content = "Amended..."
await doc.save_as(other_user)

# updated_at bumped, updated_by changed, version=2 created
# Version 1 preserved with is_current=False
```

### Mixin resolution order

Python's MRO (Method Resolution Order) determines which mixin's `save()` runs first. List mixins left-to-right before `Model`. Each mixin calls `super().save()` to chain to the next:

```
TimestampMixin.save()  →  SoftDeleteMixin.save()  →  OwnershipMixin.save()  →  Model.save()
```

---

## Custom Mixin Pattern

You can create your own mixins following the same pattern. A mixin is an abstract model that:

1. Defines fields
2. Overrides `save()` and/or `delete()` with calls to `super()`
3. Optionally adds custom manager methods

```python
from hyperdjango import Model, Field
from datetime import datetime, timezone


class PublishableMixin(Model):
    class Meta:
        abstract = True

    published_at: datetime | None = Field(default=None)
    is_published: bool = Field(default=False)

    async def publish(self):
        self.is_published = True
        self.published_at = datetime.now(timezone.utc)
        await self.save()

    async def unpublish(self):
        self.is_published = False
        self.published_at = None
        await self.save()
```

Use it like any other mixin:

```python
class BlogPost(TimestampMixin, PublishableMixin, Model):
    class Meta:
        table = "blog_posts"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field()


post = BlogPost(title="Draft", body="...")
await post.save()
assert post.is_published is False

await post.publish()
assert post.is_published is True
assert post.published_at is not None
```

### Custom mixin with manager methods

To add custom QuerySet methods, override the model's manager:

```python
class TenantMixin(Model):
    class Meta:
        abstract = True

    tenant_id: int = Field()

    @classmethod
    def for_tenant(cls, tenant_id: int):
        """Return QuerySet scoped to a specific tenant."""
        return cls.objects.filter(tenant_id=tenant_id)
```

```python
class Invoice(TenantMixin, Model):
    class Meta:
        table = "invoices"

    id: int = Field(primary_key=True, auto=True)
    amount: int = Field()


# Scoped query
invoices = await Invoice.for_tenant(42).all()
```

---

## Abstract Model Inheritance

All mixins use `class Meta: abstract = True` so they do not create their own database tables. This is the same mechanism as defining any abstract base model:

```python
class BaseModel(Model):
    class Meta:
        abstract = True

    id: int = Field(primary_key=True, auto=True)
    created_at: datetime | None = Field(default=None)


class Article(BaseModel):
    class Meta:
        table = "articles"

    title: str = Field()


# Article has: id, created_at, title
# No "basemodel" table is created
```

Abstract models can define methods, class methods, and properties that all subclasses inherit. The difference from a mixin is conceptual -- a mixin adds a single behavior (timestamps, soft delete), while an abstract base model defines a shared foundation.

### When to use abstract models vs mixins

- **Abstract base model:** When you have a common set of fields and behaviors that form the identity of a group of models (e.g., `BaseModel` with `id` and `created_at`).
- **Mixin:** When you have a single cross-cutting concern that can be added to any model independently (e.g., `SoftDeleteMixin`, `OwnershipMixin`).

Both use the same mechanism (`abstract = True`). The distinction is organizational.

---

## Migration Support

All mixin fields are included in migrations automatically. When you add a mixin to an existing model, `hyper makemigrations` detects the new fields and generates an `AddField` operation for each one:

```bash
uv run hyper makemigrations --name "add_soft_delete"
```

```
Detected changes:
  articles:
    + Add field is_deleted (bool, default=False)
    + Add field deleted_at (timestamptz, nullable)
```

When removing a mixin, the reverse migration drops the fields. The migration system treats mixin fields identically to fields defined directly on the model.
